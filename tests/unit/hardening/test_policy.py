from copy import deepcopy

import pytest

from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    JourneyResult,
    Mutation,
    ObservationSnapshot,
    RiskFinding,
    RunState,
)
from repotrial.hardening import policy


def _state(**overrides: object) -> RunState:
    values: dict[str, object] = {
        "run_id": "policy-run",
        "repo_url": "https://example.invalid/repo.git",
        "baseline_config_hash": "baseline-hash",
        "risk_findings": [],
        "baseline_journey_results": [_journey_result(Verdict.PASS)],
        "experiments": [],
    }
    values.update(overrides)
    return RunState(**values)


def _journey_result(verdict: Verdict) -> JourneyResult:
    return JourneyResult(
        journey_id="health",
        verdict=verdict,
        passed_steps=1 if verdict is Verdict.PASS else 0,
        total_steps=1,
    )


def _finding(kind: str, service: str, severity: int = 50) -> RiskFinding:
    return RiskFinding(
        finding_id=f"{kind}-{service}-{severity}",
        kind=kind,
        service=service,
        severity=severity,
        evidence={"source": "test"},
    )


def _mutation(mutation_type: MutationType, service: str, mutation_id: str) -> Mutation:
    return Mutation(mutation_id=mutation_id, type=mutation_type, service=service)


def _record(
    mutation: Mutation,
    verdict: ExperimentVerdict,
    *,
    parent: str = "baseline-hash",
    candidate: str = "candidate-hash",
    before: ObservationSnapshot | None = None,
    after: ObservationSnapshot | None = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=mutation.mutation_id,
        parent_config_hash=parent,
        candidate_config_hash=candidate,
        mutation=mutation,
        boot=Verdict.PASS,
        journeys=[_journey_result(Verdict.PASS)],
        before=before,
        after=after,
        verdict=verdict,
        reason="test",
    )


def _assert_mutation(
    state: RunState,
    mutation_type: MutationType,
    service: str,
    mutation_id: str,
) -> None:
    decision = policy.propose_mutation(state)

    assert decision.stop_reason is None
    assert decision.mutation == _mutation(mutation_type, service, mutation_id)
    assert decision.mutation.params == {}


@pytest.mark.parametrize(
    "baseline_results",
    [
        [],
        [_journey_result(Verdict.FAIL)],
        [_journey_result(Verdict.UNSUPPORTED)],
    ],
)
def test_propose_mutation_requires_an_explicit_baseline_pass(
    baseline_results: list[JourneyResult],
) -> None:
    state = _state(
        baseline_journey_results=baseline_results,
        risk_findings=[_finding("privileged", "web")],
    )

    decision = policy.propose_mutation(state)

    assert decision.mutation is None
    assert decision.stop_reason == "insufficient_coverage"


def test_propose_mutation_uses_the_exact_normal_type_priority() -> None:
    findings = [
        _finding("host_network", "network"),
        _finding("writable_rootfs", "rootfs"),
        _finding("cap_add", "caps"),
        _finding("root_user_possible", "possible-root"),
        _finding("root_user", "root"),
        _finding("docker_socket_rw", "socket"),
        _finding("privileged", "privileged"),
    ]
    state = _state(risk_findings=findings)
    expected = [
        (MutationType.DROP_PRIVILEGED, "privileged"),
        (MutationType.REMOVE_DOCKER_SOCKET, "socket"),
        (MutationType.SET_NON_ROOT, "possible-root"),
        (MutationType.SET_NON_ROOT, "root"),
        (MutationType.DROP_ALL_CAPS, "caps"),
        (MutationType.SET_READ_ONLY, "rootfs"),
        (MutationType.BRIDGE_NETWORK, "network"),
    ]

    actual: list[tuple[MutationType, str]] = []
    for mutation_type, service in expected:
        decision = policy.propose_mutation(state)
        assert decision.mutation is not None
        actual.append((decision.mutation.type, decision.mutation.service))
        state.experiments.append(
            _record(
                _mutation(mutation_type, service, f"caller-selected-{service}"),
                ExperimentVerdict.KEEP,
            )
        )

    assert actual == expected


def test_propose_mutation_orders_same_type_by_severity_then_service_and_deduplicates() -> (
    None
):
    state = _state(
        risk_findings=[
            _finding("privileged", "zebra", 10),
            _finding("privileged", "alpha", 10),
            _finding("privileged", "middle", 90),
            _finding("privileged", "middle", 20),
            _finding("unrecognised", "ignored", 100),
        ]
    )

    _assert_mutation(
        state,
        MutationType.DROP_PRIVILEGED,
        "middle",
        "policy:drop_privileged:middle",
    )
    state.experiments.append(
        _record(
            _mutation(MutationType.DROP_PRIVILEGED, "middle", "caller-id"),
            ExperimentVerdict.KEEP,
        )
    )
    _assert_mutation(
        state,
        MutationType.DROP_PRIVILEGED,
        "alpha",
        "policy:drop_privileged:alpha",
    )


@pytest.mark.parametrize(
    "verdict", [ExperimentVerdict.KEEP, ExperimentVerdict.ROLLBACK]
)
def test_propose_mutation_never_retries_a_normal_semantic_identity(
    verdict: ExperimentVerdict,
) -> None:
    state = _state(
        risk_findings=[_finding("privileged", "web")],
        experiments=[
            _record(
                _mutation(MutationType.DROP_PRIVILEGED, "web", "caller-chosen-id"),
                verdict,
            )
        ],
    )

    decision = policy.propose_mutation(state)

    assert decision.mutation is None
    assert decision.stop_reason == "no_remaining_mutations"


def test_propose_mutation_stops_after_three_trailing_rollbacks_but_not_two() -> None:
    state = _state(risk_findings=[_finding("privileged", "web")])
    state.experiments.extend(
        [
            _record(
                _mutation(MutationType.SET_NON_ROOT, "a", "one"),
                ExperimentVerdict.ROLLBACK,
            ),
            _record(
                _mutation(MutationType.SET_NON_ROOT, "b", "two"),
                ExperimentVerdict.ROLLBACK,
            ),
        ]
    )

    _assert_mutation(
        state, MutationType.DROP_PRIVILEGED, "web", "policy:drop_privileged:web"
    )

    state.experiments.append(
        _record(
            _mutation(MutationType.SET_NON_ROOT, "c", "three"),
            ExperimentVerdict.ROLLBACK,
        )
    )
    decision = policy.propose_mutation(state)

    assert decision.mutation is None
    assert decision.stop_reason == "consecutive_failures"


def test_propose_mutation_keep_resets_the_rollback_streak() -> None:
    state = _state(risk_findings=[_finding("privileged", "web")])
    state.experiments.extend(
        [
            _record(
                _mutation(MutationType.SET_NON_ROOT, "a", "one"),
                ExperimentVerdict.ROLLBACK,
            ),
            _record(
                _mutation(MutationType.SET_NON_ROOT, "b", "two"), ExperimentVerdict.KEEP
            ),
            _record(
                _mutation(MutationType.SET_NON_ROOT, "c", "three"),
                ExperimentVerdict.ROLLBACK,
            ),
            _record(
                _mutation(MutationType.SET_NON_ROOT, "d", "four"),
                ExperimentVerdict.ROLLBACK,
            ),
        ]
    )

    _assert_mutation(
        state, MutationType.DROP_PRIVILEGED, "web", "policy:drop_privileged:web"
    )


def test_propose_mutation_honours_experiment_budget_before_follow_up_work() -> None:
    tmp_change = ObservationSnapshot(
        file_changes=[{"service": "web", "path": "/tmp/cache"}]
    )
    rollback = _record(
        _mutation(MutationType.SET_READ_ONLY, "web", "old-read-only"),
        ExperimentVerdict.ROLLBACK,
        before=tmp_change,
    )
    seven = [
        _record(
            _mutation(MutationType.SET_NON_ROOT, f"s{i}", str(i)),
            ExperimentVerdict.KEEP,
        )
        for i in range(6)
    ]
    state = _state(experiments=[*seven, rollback])

    _assert_mutation(
        state, MutationType.ADD_TMPFS, "web", "policy:add_tmpfs:web:after-read-only"
    )

    state.experiments.append(
        _record(
            _mutation(MutationType.SET_NON_ROOT, "last", "seven"),
            ExperimentVerdict.KEEP,
        )
    )
    decision = policy.propose_mutation(state)

    assert decision.mutation is None
    assert decision.stop_reason == "experiment_budget_exhausted"


def test_propose_mutation_treats_any_prior_stop_as_terminal() -> None:
    state = _state(
        risk_findings=[_finding("privileged", "web")],
        experiments=[
            _record(
                _mutation(MutationType.SET_NON_ROOT, "web", "stopped"),
                ExperimentVerdict.STOP,
            )
        ],
    )

    decision = policy.propose_mutation(state)

    assert decision.mutation is None
    assert decision.stop_reason == "experiment_stopped"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (ObservationSnapshot(file_changes=[{"service": "web", "path": "/tmp"}]), None),
        (
            None,
            ObservationSnapshot(
                file_changes=[{"service": "web", "path": "/tmp/cache"}]
            ),
        ),
    ],
)
def test_propose_mutation_adds_tmpfs_once_after_a_qualifying_read_only_rollback(
    before: ObservationSnapshot | None,
    after: ObservationSnapshot | None,
) -> None:
    state = _state(
        risk_findings=[_finding("privileged", "ordinary")],
        experiments=[
            _record(
                _mutation(MutationType.SET_READ_ONLY, "web", "prior-read-only"),
                ExperimentVerdict.ROLLBACK,
                before=before,
                after=after,
            )
        ],
    )

    _assert_mutation(
        state, MutationType.ADD_TMPFS, "web", "policy:add_tmpfs:web:after-read-only"
    )


@pytest.mark.parametrize(
    ("observation", "current_hash", "prior_tmpfs", "is_latest"),
    [
        (
            ObservationSnapshot(file_changes=[{"service": "web", "path": "/tmp2"}]),
            None,
            False,
            True,
        ),
        (
            ObservationSnapshot(
                file_changes=[{"service": "other", "path": "/tmp/cache"}]
            ),
            None,
            False,
            True,
        ),
        (None, None, False, True),
        (
            ObservationSnapshot(
                file_changes=[{"service": "web", "path": "/tmp/cache"}]
            ),
            "other-hash",
            False,
            True,
        ),
        (
            ObservationSnapshot(
                file_changes=[{"service": "web", "path": "/tmp/cache"}]
            ),
            None,
            True,
            True,
        ),
        (
            ObservationSnapshot(
                file_changes=[{"service": "web", "path": "/tmp/cache"}]
            ),
            None,
            False,
            False,
        ),
    ],
)
def test_propose_mutation_rejects_nonqualifying_tmpfs_followups(
    observation: ObservationSnapshot | None,
    current_hash: str | None,
    prior_tmpfs: bool,
    is_latest: bool,
) -> None:
    rollback = _record(
        _mutation(MutationType.SET_READ_ONLY, "web", "prior-read-only"),
        ExperimentVerdict.ROLLBACK,
        before=observation,
    )
    experiments = [rollback]
    if not is_latest:
        experiments.append(
            _record(
                _mutation(MutationType.SET_NON_ROOT, "other", "later"),
                ExperimentVerdict.KEEP,
            )
        )
    if prior_tmpfs:
        experiments.insert(
            0,
            _record(
                _mutation(MutationType.ADD_TMPFS, "web", "prior-tmpfs"),
                ExperimentVerdict.KEEP,
            ),
        )
    state = _state(
        current_config_hash=current_hash,
        risk_findings=[_finding("privileged", "ordinary")],
        experiments=experiments,
    )

    _assert_mutation(
        state,
        MutationType.DROP_PRIVILEGED,
        "ordinary",
        "policy:drop_privileged:ordinary",
    )


def test_propose_mutation_retries_read_only_once_after_matching_tmpfs_keep() -> None:
    tmp_change = ObservationSnapshot(
        file_changes=[{"service": "web", "path": "/tmp/cache"}]
    )
    read_only = _record(
        _mutation(MutationType.SET_READ_ONLY, "web", "original-read-only"),
        ExperimentVerdict.ROLLBACK,
        parent="baseline-hash",
        candidate="bad-read-only-hash",
        after=tmp_change,
    )
    tmpfs = _record(
        _mutation(
            MutationType.ADD_TMPFS, "web", "policy:add_tmpfs:web:after-read-only"
        ),
        ExperimentVerdict.KEEP,
        parent="baseline-hash",
        candidate="tmpfs-hash",
    )
    state = _state(current_config_hash="tmpfs-hash", experiments=[read_only, tmpfs])

    _assert_mutation(
        state, MutationType.SET_READ_ONLY, "web", "policy:set_read_only:web:after-tmpfs"
    )


@pytest.mark.parametrize(
    "tmpfs_verdict, tmpfs_id, tmpfs_parent, current_hash, completed_retry",
    [
        (
            ExperimentVerdict.ROLLBACK,
            "policy:add_tmpfs:web:after-read-only",
            "baseline-hash",
            "tmpfs-hash",
            False,
        ),
        (ExperimentVerdict.KEEP, "wrong-id", "baseline-hash", "tmpfs-hash", False),
        (
            ExperimentVerdict.KEEP,
            "policy:add_tmpfs:web:after-read-only",
            "wrong-parent",
            "tmpfs-hash",
            False,
        ),
        (
            ExperimentVerdict.KEEP,
            "policy:add_tmpfs:web:after-read-only",
            "baseline-hash",
            "wrong-hash",
            False,
        ),
        (
            ExperimentVerdict.KEEP,
            "policy:add_tmpfs:web:after-read-only",
            "baseline-hash",
            "tmpfs-hash",
            True,
        ),
    ],
)
def test_propose_mutation_never_loops_the_read_only_tmpfs_chain(
    tmpfs_verdict: ExperimentVerdict,
    tmpfs_id: str,
    tmpfs_parent: str,
    current_hash: str,
    completed_retry: bool,
) -> None:
    observation = ObservationSnapshot(
        file_changes=[{"service": "web", "path": "/tmp/cache"}]
    )
    read_only = _record(
        _mutation(MutationType.SET_READ_ONLY, "web", "original-read-only"),
        ExperimentVerdict.ROLLBACK,
        before=observation,
    )
    tmpfs = _record(
        _mutation(MutationType.ADD_TMPFS, "web", tmpfs_id),
        tmpfs_verdict,
        parent=tmpfs_parent,
        candidate="tmpfs-hash",
    )
    experiments = [read_only, tmpfs]
    if completed_retry:
        experiments.append(
            _record(
                _mutation(
                    MutationType.SET_READ_ONLY,
                    "web",
                    "policy:set_read_only:web:after-tmpfs",
                ),
                ExperimentVerdict.ROLLBACK,
                parent="tmpfs-hash",
            )
        )
    state = _state(
        current_config_hash=current_hash,
        risk_findings=[_finding("privileged", "ordinary")],
        experiments=experiments,
    )

    _assert_mutation(
        state,
        MutationType.DROP_PRIVILEGED,
        "ordinary",
        "policy:drop_privileged:ordinary",
    )


def test_propose_mutation_is_deterministic_and_does_not_mutate_nested_state() -> None:
    observation = ObservationSnapshot(
        file_changes=[{"service": "web", "path": "/tmp/cache", "meta": {"x": 1}}]
    )
    mutation = _mutation(MutationType.SET_READ_ONLY, "web", "original-read-only")
    record = _record(mutation, ExperimentVerdict.ROLLBACK, before=observation)
    finding = _finding("privileged", "ordinary")
    state = _state(risk_findings=[finding], experiments=[record])
    risk_findings = state.risk_findings
    evidence = finding.evidence
    experiments = state.experiments
    params = mutation.params
    file_changes = observation.file_changes
    snapshot = deepcopy(state.model_dump())

    first = policy.propose_mutation(state)
    second = policy.propose_mutation(state)

    assert first == second
    assert state.model_dump() == snapshot
    assert state.risk_findings is risk_findings
    assert finding.evidence is evidence
    assert state.experiments is experiments
    assert mutation.params is params
    assert observation.file_changes is file_changes


def test_decide_candidate_regression_remains_unchanged() -> None:
    decision = policy.decide_candidate(
        Verdict.PASS,
        [_journey_result(Verdict.PASS)],
        expected_journeys=1,
    )

    assert decision == (ExperimentVerdict.KEEP, "baseline_pass_journeys_preserved")
