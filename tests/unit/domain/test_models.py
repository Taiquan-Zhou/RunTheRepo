from pathlib import Path

import pytest

from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    Journey,
    JourneyAssertion,
    JourneyResult,
    JourneyStep,
    Mutation,
    ObservationSnapshot,
    PinnedRepo,
    RepoRef,
    RiskFinding,
    RunState,
)


def test_mutation_roundtrip_json() -> None:
    m = Mutation(mutation_id="m1", type=MutationType.SET_READ_ONLY, service="app")

    restored = Mutation.model_validate_json(m.model_dump_json())

    assert restored == m


def test_run_state_roundtrip_json_with_every_declared_field() -> None:
    assertion = JourneyAssertion(kind="status", target="/health", expected=200)
    step = JourneyStep(
        step_id="step-1",
        tool="browser",
        action="get",
        params={"url": "http://app/health"},
        assertions=[assertion],
    )
    journey = Journey(journey_id="journey-1", name="Health check", steps=[step])
    journey_result = JourneyResult(
        journey_id="journey-1",
        verdict=Verdict.PASS,
        passed_steps=1,
        total_steps=1,
        evidence_paths=["artifacts/health.json"],
        failure_reason=None,
    )
    mutation = Mutation(
        mutation_id="mutation-1",
        type=MutationType.SET_NON_ROOT,
        service="app",
        params={"user": "1000:1000"},
    )
    observation = ObservationSnapshot(
        inspect={"State": {"Running": True}},
        file_changes=[{"path": "/tmp/output", "operation": "created"}],
        process_events=[{"pid": 1, "event": "start"}],
        network_events=[{"destination": "db", "port": 5432}],
        unsupported_collectors=["ebpf"],
    )
    experiment = ExperimentRecord(
        experiment_id="experiment-1",
        parent_config_hash="parent-hash",
        candidate_config_hash="candidate-hash",
        mutation=mutation,
        boot=Verdict.PASS,
        journeys=[journey_result],
        before=observation,
        after=observation,
        verdict=ExperimentVerdict.KEEP,
        reason="journey passed",
    )
    state = RunState(
        run_id="run-1",
        repo_url="https://github.com/acme/demo",
        commit_sha="a" * 40,
        compose_path="docker-compose.yml",
        sandbox_id="sandbox-1",
        baseline_config_hash="baseline-hash",
        current_config_hash="candidate-hash",
        risk_findings=[
            RiskFinding(
                finding_id="risk-1",
                kind="privileged",
                service="app",
                severity=90,
                evidence={"compose": {"privileged": True}},
            )
        ],
        journeys=[journey],
        baseline_journey_results=[journey_result],
        baseline_observation=observation,
        experiments=[experiment],
        artifacts=["artifacts/run.json"],
        stop_reason="completed",
    )

    restored = RunState.model_validate_json(state.model_dump_json())

    assert restored == state


def test_risk_finding_rejects_severity_above_100() -> None:
    with pytest.raises(ValueError):
        RiskFinding(
            finding_id="risk-1",
            kind="privileged",
            service="app",
            severity=101,
            evidence={},
        )


@pytest.mark.parametrize("commit_sha", ["a" * 39, "a" * 41])
def test_pinned_repo_rejects_commit_sha_outside_exact_length(commit_sha: str) -> None:
    with pytest.raises(ValueError):
        PinnedRepo(
            repo=RepoRef(
                url="https://github.com/acme/demo",
                owner="acme",
                repo="demo",
            ),
            commit_sha=commit_sha,
            local_path=Path("repos/demo"),
        )


def test_mutable_default_collections_are_independent() -> None:
    first_step = JourneyStep(step_id="step-1", tool="browser", action="get", params={})
    second_step = JourneyStep(step_id="step-2", tool="browser", action="get", params={})
    first_result = JourneyResult(
        journey_id="journey-1", verdict=Verdict.PASS, passed_steps=1, total_steps=1
    )
    second_result = JourneyResult(
        journey_id="journey-2", verdict=Verdict.PASS, passed_steps=1, total_steps=1
    )
    first_mutation = Mutation(
        mutation_id="mutation-1", type=MutationType.ADD_TMPFS, service="app"
    )
    second_mutation = Mutation(
        mutation_id="mutation-2", type=MutationType.ADD_TMPFS, service="app"
    )
    first_observation = ObservationSnapshot()
    second_observation = ObservationSnapshot()
    first_state = RunState(run_id="run-1", repo_url="https://github.com/acme/one")
    second_state = RunState(run_id="run-2", repo_url="https://github.com/acme/two")

    first_step.assertions.append(
        JourneyAssertion(kind="status", target="/", expected=200)
    )
    first_result.evidence_paths.append("artifacts/one.json")
    first_mutation.params["size"] = "64m"
    first_observation.inspect["State"] = {"Running": True}
    first_observation.file_changes.append({"path": "/tmp/file"})
    first_observation.process_events.append({"pid": 1})
    first_observation.network_events.append({"port": 80})
    first_observation.unsupported_collectors.append("ebpf")
    first_state.risk_findings.append(
        RiskFinding(
            finding_id="risk-1",
            kind="privileged",
            service="app",
            severity=90,
            evidence={},
        )
    )
    first_state.journeys.append(
        Journey(journey_id="journey-1", name="Health", steps=[])
    )
    first_state.baseline_journey_results.append(first_result)
    first_state.experiments.append(
        ExperimentRecord(
            experiment_id="experiment-1",
            parent_config_hash="parent",
            candidate_config_hash="candidate",
            mutation=first_mutation,
            boot=Verdict.PASS,
            journeys=[],
            verdict=ExperimentVerdict.KEEP,
            reason="passed",
        )
    )
    first_state.artifacts.append("artifacts/run.json")

    assert second_step.assertions == []
    assert second_result.evidence_paths == []
    assert second_mutation.params == {}
    assert second_observation.inspect == {}
    assert second_observation.file_changes == []
    assert second_observation.process_events == []
    assert second_observation.network_events == []
    assert second_observation.unsupported_collectors == []
    assert second_state.risk_findings == []
    assert second_state.journeys == []
    assert second_state.baseline_journey_results == []
    assert second_state.experiments == []
    assert second_state.artifacts == []
