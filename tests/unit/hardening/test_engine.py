import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from repotrial.compose.mutations import apply_mutation
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    Journey,
    JourneyResult,
    JourneyStep,
    Mutation,
    ObservationSnapshot,
    RunState,
)
from repotrial.hardening import engine as engine_module
from repotrial.hardening.engine import ExperimentContext, run_experiment
from repotrial.sandbox.base import ExecResult, NetworkLogResult
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.sandbox.lifecycle import CleanupError
from repotrial.trial.boot import BootResult
from repotrial.trial.observer import ObservationCollectionError


def _exec_result(*, exit_code: int = 0, stdout: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr="")


def _journey(journey_id: str, *, tool: str = "http") -> Journey:
    if tool == "http":
        step = JourneyStep(
            step_id=f"{journey_id}-step",
            tool="http",
            action="get",
            params={"path": "/health"},
            assertions=[],
        )
    else:
        step = JourneyStep(
            step_id=f"{journey_id}-step",
            tool="browser",
            action="goto",
            params={"path": "/"},
            assertions=[],
        )
    return Journey(journey_id=journey_id, name=journey_id, steps=[step])


def _journey_result(journey_id: str, verdict: Verdict) -> JourneyResult:
    return JourneyResult(
        journey_id=journey_id,
        verdict=verdict,
        passed_steps=1 if verdict is Verdict.PASS else 0,
        total_steps=1,
    )


class RecordingProvider(FakeSandboxProvider):
    def __init__(
        self,
        *,
        scripts: dict[tuple[str, ...], ExecResult] | None = None,
        ports: dict[int, int] | None = None,
        expected_overlay: Path | None = None,
        source_env: dict[str, str] | None = None,
        publish_failure: BaseException | None = None,
        destroy_failure: BaseException | None = None,
    ) -> None:
        super().__init__(
            scripts=scripts,
            ports=ports,
            network_result=NetworkLogResult(
                events=[], supported=False, unsupported_reason="not observed"
            ),
        )
        self.expected_overlay = expected_overlay
        self.overlay_at_create: str | None = None
        self.source_env = source_env
        self.publish_failure = publish_failure
        self.destroy_failure = destroy_failure

    async def create(self, workspace: Path, name: str) -> str:
        if self.expected_overlay is not None:
            assert self.expected_overlay.is_file()
            self.overlay_at_create = self.expected_overlay.read_text(encoding="utf-8")
        if self.source_env is not None:
            self.source_env["APP_MODE"] = "mutated-after-first-await"
        return await super().create(workspace, name)

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        if self.publish_failure is not None:
            self.calls.append(("publish_port", sandbox_id, container_port))
            raise self.publish_failure
        return await super().publish_port(sandbox_id, container_port)

    async def destroy(self, sandbox_id: str) -> None:
        if self.destroy_failure is not None:
            self.calls.append(("destroy", sandbox_id))
            raise self.destroy_failure
        await super().destroy(sandbox_id)


def _compose_text() -> str:
    return "services:\n  web:\n    image: example/web:1\n"


def _case(
    tmp_path: Path,
    *,
    journeys: list[Journey] | None = None,
    baseline_results: list[JourneyResult] | None = None,
    env: dict[str, str] | None = None,
    overlay_name: str = "candidate.overlay.yaml",
) -> tuple[RunState, Mutation, ExperimentContext]:
    workspace = tmp_path / "workspace"
    artifact_dir = tmp_path / "artifacts"
    workspace.mkdir()
    artifact_dir.mkdir()
    (workspace / "compose.yaml").write_text(_compose_text(), encoding="utf-8")
    if journeys is None:
        journeys = [_journey("health")]
    if baseline_results is None:
        baseline_results = [_journey_result("health", Verdict.PASS)]
    state = RunState(
        run_id="untrusted run id ../../ secret",
        repo_url="https://example.invalid/repo.git",
        compose_path="compose.yaml",
        journeys=journeys,
        baseline_journey_results=baseline_results,
        baseline_observation=ObservationSnapshot(inspect={"baseline": []}),
    )
    mutation = Mutation(
        mutation_id="mutation-read-only",
        type=MutationType.SET_READ_ONLY,
        service="web",
    )
    context = ExperimentContext(
        workspace=workspace,
        overlay_path=workspace / overlay_name,
        artifact_dir=artifact_dir,
        env=env or {},
        container_port=8080,
    )
    return state, mutation, context


def _compose_argv(
    context: ExperimentContext, env: dict[str, str] | None = None
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    prefix: tuple[str, ...] = ()
    if env:
        prefix = ("env", *(f"{key}={value}" for key, value in sorted(env.items())))
    base = ("docker", "compose", "-f", "compose.yaml", "-f", context.overlay_path.name)
    return (
        (*prefix, *base, "up", "-d"),
        (*prefix, *base, "ps", "--all", "--format", "json"),
        (*prefix, *base, "logs", "--no-color", "--tail", "200"),
        (
            *base,
            "ps",
            "--all",
            "--no-trunc",
            "--orphans=false",
            "--format",
            "json",
        ),
    )


def _healthy_scripts(
    context: ExperimentContext, env: dict[str, str] | None = None
) -> dict[tuple[str, ...], ExecResult]:
    up, ps, logs, discovery = _compose_argv(context, env)
    return {
        up: _exec_result(),
        ps: _exec_result(
            stdout=json.dumps(
                {
                    "Service": "web",
                    "State": "running",
                    "Health": "healthy",
                    "ExitCode": 0,
                }
            )
        ),
        logs: _exec_result(),
        discovery: _exec_result(),
    }


def _install_http_runner(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, Verdict],
    calls: list[tuple[str, str, Path, tuple[tuple[object, ...], ...]]],
    provider: FakeSandboxProvider,
) -> None:
    async def fake_runner(
        journey: Journey, *, base_url: str, evidence_dir: Path
    ) -> JourneyResult:
        assert not evidence_dir.exists()
        evidence_dir.mkdir()
        evidence = evidence_dir / "result.json"
        evidence.write_text(journey.journey_id, encoding="utf-8")
        calls.append(
            (journey.journey_id, base_url, evidence_dir, tuple(provider.calls))
        )
        return JourneyResult(
            journey_id=journey.journey_id,
            verdict=outcomes[journey.journey_id],
            passed_steps=1 if outcomes[journey.journey_id] is Verdict.PASS else 0,
            total_steps=1,
            evidence_paths=[str(evidence)],
        )

    monkeypatch.setattr(engine_module, "run_http_journey", fake_runner)


def _run(
    state: RunState,
    mutation: Mutation,
    provider: FakeSandboxProvider,
    context: ExperimentContext,
):
    return asyncio.run(run_experiment(state, mutation, provider, context=context))


def test_no_baseline_pass_stops_before_overlay_provider_or_artifacts(
    tmp_path: Path,
) -> None:
    state, mutation, context = _case(
        tmp_path,
        baseline_results=[
            _journey_result("health", Verdict.FAIL),
            _journey_result("unsupported", Verdict.UNSUPPORTED),
        ],
        journeys=[_journey("health"), _journey("unsupported")],
    )
    provider = RecordingProvider(expected_overlay=context.overlay_path)

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.STOP
    assert record.reason == "insufficient_coverage"
    assert record.boot is Verdict.UNSUPPORTED
    assert record.journeys == []
    assert provider.calls == []
    assert not context.overlay_path.exists()
    assert list(context.artifact_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("journeys", "results", "reason"),
    [
        (
            [_journey("health")],
            [
                _journey_result("health", Verdict.PASS),
                _journey_result("health", Verdict.PASS),
            ],
            "invalid_baseline:duplicate_result_id",
        ),
        (
            [_journey("health"), _journey("health")],
            [_journey_result("health", Verdict.PASS)],
            "invalid_baseline:duplicate_journey_id",
        ),
        (
            [_journey("other")],
            [_journey_result("health", Verdict.PASS)],
            "invalid_baseline:missing_or_ambiguous_journey",
        ),
    ],
)
def test_inconsistent_baseline_stops_before_side_effects(
    tmp_path: Path,
    journeys: list[Journey],
    results: list[JourneyResult],
    reason: str,
) -> None:
    state, mutation, context = _case(
        tmp_path, journeys=journeys, baseline_results=results
    )
    provider = RecordingProvider()

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.STOP
    assert record.reason == reason
    assert provider.calls == []
    assert not context.overlay_path.exists()
    assert list(context.artifact_dir.iterdir()) == []


def test_real_overlay_hashes_ordered_replay_and_environment_snapshot_yield_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env = {"APP_MODE": "baseline"}
    journeys = [
        _journey("nonpass-fail"),
        _journey("second-pass"),
        _journey("first-pass"),
        _journey("nonpass-unsupported"),
    ]
    results = [
        _journey_result("first-pass", Verdict.PASS),
        _journey_result("nonpass-fail", Verdict.FAIL),
        _journey_result("second-pass", Verdict.PASS),
        _journey_result("nonpass-unsupported", Verdict.UNSUPPORTED),
    ]
    state, mutation, context = _case(
        tmp_path, journeys=journeys, baseline_results=results, env=source_env
    )
    provider = RecordingProvider(
        scripts=_healthy_scripts(context, {"APP_MODE": "baseline"}),
        ports={8080: 45123},
        expected_overlay=context.overlay_path,
        source_env=source_env,
    )
    runner_calls: list[tuple[str, str, Path, tuple[tuple[object, ...], ...]]] = []
    _install_http_runner(
        monkeypatch,
        {"first-pass": Verdict.PASS, "second-pass": Verdict.PASS},
        runner_calls,
        provider,
    )
    state_before = state.model_copy(deep=True)
    mutation_before = mutation.model_copy(deep=True)
    base = load_compose(context.workspace / "compose.yaml")
    candidate = apply_mutation(base, mutation)
    parent_hash = (
        "sha256:"
        + hashlib.sha256(canonical_compose_json(base).encode("utf-8")).hexdigest()
    )
    candidate_hash = (
        "sha256:"
        + hashlib.sha256(canonical_compose_json(candidate).encode("utf-8")).hexdigest()
    )

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.KEEP
    assert record.reason == "baseline_pass_journeys_preserved"
    assert record.parent_config_hash == parent_hash
    assert record.candidate_config_hash == candidate_hash
    assert record.experiment_id == mutation.mutation_id
    assert record.before is state.baseline_observation
    assert record.after is not None
    assert record.after.unsupported_collectors == ["network_runtime"]
    assert [result.journey_id for result in record.journeys] == [
        "first-pass",
        "second-pass",
    ]
    assert [call[0] for call in runner_calls] == ["first-pass", "second-pass"]
    assert all(call[1] == "http://127.0.0.1:45123" for call in runner_calls)
    assert all(
        any(item[0] == "network_log" for item in call[3]) for call in runner_calls
    )
    assert all(
        any(item[0] == "publish_port" for item in call[3]) for call in runner_calls
    )
    assert provider.overlay_at_create is not None
    assert "read_only: true" in provider.overlay_at_create
    create_call = provider.calls[0]
    assert create_call[0] == "create"
    assert create_call[1] == context.workspace
    assert cast(str, create_call[2]).startswith("repotrial-candidate-")
    assert "untrusted" not in cast(str, create_call[2])
    assert provider.calls[-1] == ("destroy", "sandbox-1")
    assert source_env == {"APP_MODE": "mutated-after-first-await"}
    assert any(
        call[0] == "exec" and "APP_MODE=baseline" in cast(tuple[str, ...], call[2])
        for call in provider.calls
    )
    assert state == state_before
    assert mutation == mutation_before


def test_conflicting_recorded_parent_hash_stops_before_overlay_and_sandbox(
    tmp_path: Path,
) -> None:
    state, mutation, context = _case(tmp_path)
    state.current_config_hash = "sha256:" + ("0" * 64)
    provider = RecordingProvider()

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.STOP
    assert record.reason == "parent_hash_mismatch"
    assert provider.calls == []
    assert not context.overlay_path.exists()


def _patch_boot(
    monkeypatch: pytest.MonkeyPatch, verdict: Verdict
) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []

    async def fake_boot(
        provider: FakeSandboxProvider,
        sandbox_id: str,
        compose_path: str,
        env: dict[str, str],
        attempt: int,
        *,
        overlay_path: str | None = None,
    ) -> BootResult:
        del provider, sandbox_id, env, attempt
        calls.append((compose_path, overlay_path))
        return BootResult(verdict=verdict, service_states={}, logs={}, attempt=1)

    monkeypatch.setattr(engine_module, "boot_compose", fake_boot)
    return calls


def _patch_observer(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: ObservationSnapshot | BaseException,
) -> list[tuple[str, str | None, Path]]:
    calls: list[tuple[str, str | None, Path]] = []

    async def fake_observer(
        provider: FakeSandboxProvider,
        sandbox_id: str,
        compose_path: str,
        artifact_path: Path,
        *,
        overlay_path: str | None = None,
    ) -> ObservationSnapshot:
        del provider, sandbox_id
        calls.append((compose_path, overlay_path, artifact_path))
        if isinstance(snapshot, BaseException):
            raise snapshot
        artifact_path.write_text("observed", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(engine_module, "collect_observation", fake_observer)
    return calls


@pytest.mark.parametrize(
    ("boot_verdict", "expected_verdict", "reason"),
    [
        (Verdict.FAIL, ExperimentVerdict.ROLLBACK, "boot_regression"),
        (Verdict.UNSUPPORTED, ExperimentVerdict.STOP, "boot_unsupported"),
    ],
)
def test_boot_failure_short_circuits_observation_and_replay_but_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boot_verdict: Verdict,
    expected_verdict: ExperimentVerdict,
    reason: str,
) -> None:
    state, mutation, context = _case(tmp_path)
    provider = RecordingProvider(expected_overlay=context.overlay_path)
    boot_calls = _patch_boot(monkeypatch, boot_verdict)

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("later stage must not run")

    monkeypatch.setattr(engine_module, "collect_observation", forbidden)
    monkeypatch.setattr(engine_module, "run_http_journey", forbidden)

    record = _run(state, mutation, provider, context)

    assert record.verdict is expected_verdict
    assert record.reason == reason
    assert record.boot is boot_verdict
    assert boot_calls == [("compose.yaml", context.overlay_path.name)]
    assert provider.calls == [
        ("create", context.workspace, cast(str, provider.calls[0][2])),
        ("destroy", "sandbox-1"),
    ]


def test_observation_failure_stops_with_boot_pass_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, mutation, context = _case(tmp_path)
    provider = RecordingProvider(expected_overlay=context.overlay_path)
    _patch_boot(monkeypatch, Verdict.PASS)
    _patch_observer(monkeypatch, ObservationCollectionError("raw observation secret"))

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.STOP
    assert record.reason == "observation_failed"
    assert record.boot is Verdict.PASS
    assert record.after is None
    assert "secret" not in record.reason
    assert provider.calls[-1] == ("destroy", "sandbox-1")


@pytest.mark.parametrize(
    ("replay_verdict", "mutation_type", "journey_id", "expected_verdict", "reason"),
    [
        (
            Verdict.FAIL,
            MutationType.DROP_ALL_CAPS,
            "create-item",
            ExperimentVerdict.ROLLBACK,
            "journey_regression:create-item",
        ),
        (
            Verdict.UNSUPPORTED,
            MutationType.SET_READ_ONLY,
            "health",
            ExperimentVerdict.STOP,
            "journey_unsupported:health",
        ),
    ],
)
def test_first_nonpass_replay_short_circuits_with_frozen_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replay_verdict: Verdict,
    mutation_type: MutationType,
    journey_id: str,
    expected_verdict: ExperimentVerdict,
    reason: str,
) -> None:
    state, mutation, context = _case(
        tmp_path,
        journeys=[_journey(journey_id), _journey("must-not-run")],
        baseline_results=[
            _journey_result(journey_id, Verdict.PASS),
            _journey_result("must-not-run", Verdict.PASS),
        ],
    )
    mutation.type = mutation_type
    provider = RecordingProvider(
        ports={8080: 45123}, expected_overlay=context.overlay_path
    )
    _patch_boot(monkeypatch, Verdict.PASS)
    snapshot = ObservationSnapshot(unsupported_collectors=["network_runtime"])
    _patch_observer(monkeypatch, snapshot)
    runner_calls: list[tuple[str, str, Path, tuple[tuple[object, ...], ...]]] = []
    _install_http_runner(
        monkeypatch, {journey_id: replay_verdict}, runner_calls, provider
    )

    record = _run(state, mutation, provider, context)

    assert record.verdict is expected_verdict
    assert record.reason == reason
    assert record.boot is Verdict.PASS
    assert record.after is snapshot
    assert [result.verdict for result in record.journeys] == [replay_verdict]
    assert [call[0] for call in runner_calls] == [journey_id]
    if mutation_type is MutationType.DROP_ALL_CAPS:
        assert provider.overlay_at_create is not None
        assert "cap_drop" in provider.overlay_at_create
    assert provider.calls[-1] == ("destroy", "sandbox-1")


def test_stage_cleanup_error_is_cleaned_then_propagated_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, mutation, context = _case(tmp_path)
    provider = RecordingProvider()
    destroy_failure = RuntimeError("nested destroy failure")
    cleanup_failure = CleanupError("nested", destroy_failure, None)

    async def fail_boot(*args: object, **kwargs: object) -> BootResult:
        raise cleanup_failure

    monkeypatch.setattr(engine_module, "boot_compose", fail_boot)

    with pytest.raises(CleanupError) as raised:
        _run(state, mutation, provider, context)

    assert raised.value is cleanup_failure
    assert provider.calls[-1] == ("destroy", "sandbox-1")


def test_browser_baseline_uses_public_unsupported_runner_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, mutation, context = _case(
        tmp_path,
        journeys=[_journey("browser", tool="browser")],
        baseline_results=[_journey_result("browser", Verdict.PASS)],
    )
    provider = RecordingProvider(ports={8080: 45123})
    _patch_boot(monkeypatch, Verdict.PASS)
    _patch_observer(monkeypatch, ObservationSnapshot())

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.STOP
    assert record.reason == "journey_unsupported:browser"
    assert [result.verdict for result in record.journeys] == [Verdict.UNSUPPORTED]
    assert provider.calls[-1] == ("destroy", "sandbox-1")


@pytest.mark.parametrize("failure_stage", ["publish", "journey"])
def test_ordinary_candidate_error_stops_without_raw_message_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    state, mutation, context = _case(tmp_path)
    publish_failure = (
        RuntimeError("raw publish secret") if failure_stage == "publish" else None
    )
    provider = RecordingProvider(ports={8080: 45123}, publish_failure=publish_failure)
    _patch_boot(monkeypatch, Verdict.PASS)
    _patch_observer(monkeypatch, ObservationSnapshot())

    async def journey_error(*args: object, **kwargs: object) -> JourneyResult:
        raise RuntimeError("raw journey secret")

    if failure_stage == "journey":
        monkeypatch.setattr(engine_module, "run_http_journey", journey_error)

    record = _run(state, mutation, provider, context)

    assert record.verdict is ExperimentVerdict.STOP
    assert record.reason == f"{failure_stage}_failed"
    assert "secret" not in record.reason
    assert provider.calls[-1] == ("destroy", "sandbox-1")


def test_cancellation_waits_for_cleanup_then_propagates_same_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, mutation, context = _case(tmp_path)
    provider = RecordingProvider(ports={8080: 45123})
    _patch_boot(monkeypatch, Verdict.PASS)
    _patch_observer(monkeypatch, ObservationSnapshot())
    cancellation = asyncio.CancelledError("cancel experiment")

    async def cancel_runner(*args: object, **kwargs: object) -> JourneyResult:
        raise cancellation

    monkeypatch.setattr(engine_module, "run_http_journey", cancel_runner)

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(state, mutation, provider, context)

    assert raised.value is cancellation
    assert provider.calls[-1] == ("destroy", "sandbox-1")


def test_cleanup_error_is_not_hidden_as_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, mutation, context = _case(tmp_path)
    destroy_failure = RuntimeError("destroy failed")
    provider = RecordingProvider(ports={8080: 45123}, destroy_failure=destroy_failure)
    _patch_boot(monkeypatch, Verdict.PASS)
    _patch_observer(monkeypatch, ObservationSnapshot())
    runner_calls: list[tuple[str, str, Path, tuple[tuple[object, ...], ...]]] = []
    _install_http_runner(monkeypatch, {"health": Verdict.PASS}, runner_calls, provider)

    with pytest.raises(CleanupError) as raised:
        _run(state, mutation, provider, context)

    assert raised.value.destroy_failure is destroy_failure


def test_existing_experiment_artifacts_fail_before_second_candidate_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, mutation, first_context = _case(tmp_path)
    provider = RecordingProvider(ports={8080: 45123})
    _patch_boot(monkeypatch, Verdict.PASS)
    _patch_observer(monkeypatch, ObservationSnapshot())
    runner_calls: list[tuple[str, str, Path, tuple[tuple[object, ...], ...]]] = []
    _install_http_runner(monkeypatch, {"health": Verdict.PASS}, runner_calls, provider)
    first_record = _run(state, mutation, provider, first_context)
    assert first_record.verdict is ExperimentVerdict.KEEP
    calls_after_first = list(provider.calls)
    second_context = replace(
        first_context,
        overlay_path=first_context.workspace / "second.overlay.yaml",
    )

    with pytest.raises((FileExistsError, ValueError)):
        _run(state, mutation, provider, second_context)

    assert provider.calls == calls_after_first
    assert not second_context.overlay_path.exists()


@pytest.mark.parametrize(
    "context_change",
    [
        "missing_workspace",
        "absolute_compose",
        "compose_symlink",
        "existing_overlay",
        "artifact_inside_workspace",
        "missing_artifact_dir",
        "invalid_port",
        "bool_port",
        "invalid_env",
    ],
)
def test_invalid_runtime_boundary_rejects_before_overlay_or_provider(
    tmp_path: Path, context_change: str
) -> None:
    state, mutation, context = _case(tmp_path)
    if context_change == "missing_workspace":
        context = replace(context, workspace=tmp_path / "missing")
    elif context_change == "absolute_compose":
        state.compose_path = str((context.workspace / "compose.yaml").resolve())
    elif context_change == "compose_symlink":
        link = context.workspace / "linked.yaml"
        link.symlink_to(context.workspace / "compose.yaml")
        state.compose_path = "linked.yaml"
    elif context_change == "existing_overlay":
        context.overlay_path.write_text("sentinel", encoding="utf-8")
    elif context_change == "artifact_inside_workspace":
        context = replace(context, artifact_dir=context.workspace)
    elif context_change == "missing_artifact_dir":
        context = replace(context, artifact_dir=tmp_path / "missing-artifacts")
    elif context_change == "invalid_port":
        context = replace(context, container_port=65_536)
    elif context_change == "bool_port":
        context = replace(context, container_port=cast(int, True))
    else:
        context = replace(context, env={"PATH": "attacker"})
    provider = RecordingProvider()

    with pytest.raises((TypeError, ValueError)):
        _run(state, mutation, provider, context)

    assert provider.calls == []
    if context_change != "existing_overlay":
        assert not context.overlay_path.exists()
