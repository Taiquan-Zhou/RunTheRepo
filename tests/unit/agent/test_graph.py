import asyncio
import hashlib
import json
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel
from ruamel.yaml import YAML

from repotrial.agent.graph import (
    ainvoke_run,
    aresume_run,
    build_run_graph,
)
from repotrial.agent.state import GraphContext, GraphState
from repotrial.compose.mutations import apply_mutation
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    Journey,
    JourneyAssertion,
    JourneyResult,
    JourneyStep,
    Mutation,
    ObservationSnapshot,
    RiskFinding,
    RunState,
)
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.boot import BootResult

ModelT = TypeVar("ModelT", bound=BaseModel)


def _compose_text() -> str:
    return "services:\n  web:\n    image: example/web:1\n"


def _compose_hash(path: Path) -> str:
    material = canonical_compose_json(load_compose(path)).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _write_compose(path: Path, compose: dict[str, object]) -> None:
    yaml = YAML(typ="rt", pure=True)
    with path.open("x", encoding="utf-8") as output:
        yaml.dump(compose, output)


def _journey() -> Journey:
    return Journey(
        journey_id="health",
        name="Health",
        steps=[
            JourneyStep(
                step_id="health-request",
                tool="http",
                action="request",
                params={"method": "GET", "path": "/health"},
                assertions=[
                    JourneyAssertion(
                        kind="status_code",
                        target="response.status",
                        expected=200,
                    )
                ],
            )
        ],
    )


def _journey_result(verdict: Verdict) -> JourneyResult:
    return JourneyResult(
        journey_id="health",
        verdict=verdict,
        passed_steps=1 if verdict is Verdict.PASS else 0,
        total_steps=1,
    )


def _risk(kind: str) -> RiskFinding:
    return RiskFinding(
        finding_id=f"{kind}-web",
        kind=kind,
        service="web",
        severity=50,
        evidence={"source": "test"},
    )


def _state(workspace: Path, *, run_id: str = "run-1") -> RunState:
    compose_path = workspace / "compose.yaml"
    compose_hash = _compose_hash(compose_path)
    return RunState(
        run_id=run_id,
        repo_url="https://example.invalid/repo.git",
        commit_sha="a" * 40,
        compose_path="compose.yaml",
        baseline_config_hash=compose_hash,
        current_config_hash=compose_hash,
        journeys=[_journey()],
    )


def _boot(
    verdict: Verdict, attempt: int, logs: dict[str, str] | None = None
) -> BootResult:
    return BootResult(
        verdict=verdict,
        service_states={"web": "running" if verdict is Verdict.PASS else "exited"},
        logs=logs or {},
        attempt=attempt,
    )


class FakeStageOperations:
    def __init__(
        self,
        *,
        boots: list[tuple[Verdict, dict[str, str]]] | None = None,
        journey_verdict: Verdict = Verdict.PASS,
    ) -> None:
        self.boots = list(boots or [(Verdict.PASS, {})])
        self.journey_verdict = journey_verdict
        self.calls: list[str] = []
        self.boot_inputs: list[tuple[SandboxProvider, dict[str, str], int]] = []

    async def intake(self, state: RunState) -> RunState:
        self.calls.append("intake")
        return state.model_copy(deep=True)

    async def baseline(self, state: RunState) -> RunState:
        self.calls.append("baseline")
        return state.model_copy(deep=True)

    async def boot(
        self,
        state: RunState,
        provider: SandboxProvider,
        env: Mapping[str, str],
        attempt: int,
    ) -> BootResult:
        del state
        self.calls.append("boot")
        self.boot_inputs.append((provider, dict(env), attempt))
        verdict, logs = self.boots.pop(0)
        return _boot(verdict, attempt, logs)

    async def journeys(
        self, state: RunState, provider: SandboxProvider
    ) -> list[JourneyResult]:
        del state, provider
        self.calls.append("journeys")
        return [_journey_result(self.journey_verdict)]

    async def observe(
        self, state: RunState, provider: SandboxProvider
    ) -> ObservationSnapshot:
        del state, provider
        self.calls.append("observe")
        return ObservationSnapshot(unsupported_collectors=["network_runtime"])


class FakeModelAdapter:
    def __init__(self, action: RecoveryAction) -> None:
        self.action = action
        self.calls = 0

    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT:
        del system, user
        self.calls += 1
        return schema.model_validate(self.action.model_dump())


class WrongAttemptOperations(FakeStageOperations):
    async def boot(
        self,
        state: RunState,
        provider: SandboxProvider,
        env: Mapping[str, str],
        attempt: int,
    ) -> BootResult:
        result = await super().boot(state, provider, env, attempt)
        return result.model_copy(update={"attempt": attempt + 1})


class WrongRunOperations(FakeStageOperations):
    async def intake(self, state: RunState) -> RunState:
        returned = await super().intake(state)
        return returned.model_copy(update={"run_id": "different-run"})


class GraphProvider(FakeSandboxProvider):
    def __init__(self, *, healthy: bool = True, host_port: int | None = None) -> None:
        super().__init__(ports={} if host_port is None else {8080: host_port})
        self.healthy = healthy

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self._require_active(sandbox_id)
        snapshot = tuple(argv)
        self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
        if snapshot[-2:] == ("up", "-d"):
            return ExecResult(exit_code=0 if self.healthy else 1, stdout="", stderr="")
        if snapshot[-4:] == ("ps", "--all", "--format", "json"):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "Service": "web",
                        "State": "running" if self.healthy else "exited",
                        "Health": "healthy" if self.healthy else "",
                        "ExitCode": 0 if self.healthy else 1,
                    }
                ),
                stderr="",
            )
        if snapshot[-4:] == ("logs", "--no-color", "--tail", "200"):
            return ExecResult(exit_code=0, stdout="", stderr="")
        if snapshot[-6:] == (
            "ps",
            "--all",
            "--no-trunc",
            "--orphans=false",
            "--format",
            "json",
        ):
            return ExecResult(exit_code=0, stdout="", stderr="")
        raise AssertionError(f"unexpected provider command: {snapshot!r}")


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _healthy_server() -> tuple[str, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield str(host), int(port)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _context(
    tmp_path: Path,
    operations: FakeStageOperations,
    provider: SandboxProvider,
    *,
    model: FakeModelAdapter | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[GraphContext, Path]:
    workspace = tmp_path / "workspace"
    artifact_dir = tmp_path / "artifacts"
    overlay_dir = workspace / ".repotrial-overlays"
    accepted_dir = workspace / ".repotrial-accepted"
    workspace.mkdir()
    artifact_dir.mkdir()
    overlay_dir.mkdir()
    accepted_dir.mkdir()
    source = workspace / "compose.yaml"
    source.write_text(_compose_text(), encoding="utf-8")
    return (
        GraphContext(
            operations=operations,
            provider=provider,
            model=model,
            workspace=workspace,
            artifact_dir=artifact_dir,
            overlay_dir=overlay_dir,
            accepted_compose_dir=accepted_dir,
            env={},
            allowed_env_keys=frozenset({"APP_REQUIRED_TOKEN"}),
            readme_excerpt="",
            container_port=8080,
            sleep=sleeper,
        ),
        source,
    )


def _run(state: RunState, context: GraphContext) -> GraphState:
    return asyncio.run(ainvoke_run(build_run_graph(), state, context=context))


def test_graph_contains_only_the_frozen_named_stages() -> None:
    graph = build_run_graph()

    names = set(graph.get_graph().nodes) - {"__start__", "__end__"}

    assert names == {
        "intake",
        "baseline",
        "boot",
        "journeys",
        "observe",
        "propose_mutation",
        "experiment",
        "decide",
        "report_or_next",
    }


def test_boot_failure_uses_bounded_recovery_then_returns_to_boot(
    tmp_path: Path,
) -> None:
    operations = FakeStageOperations(
        boots=[
            (Verdict.FAIL, {"logs": "APP_REQUIRED_TOKEN is required"}),
            (Verdict.PASS, {}),
        ]
    )
    provider = FakeSandboxProvider()
    context, source = _context(tmp_path, operations, provider)

    result = _run(_state(source.parent), context)

    assert [item[2] for item in operations.boot_inputs] == [1, 2]
    assert operations.boot_inputs[0][0] is provider
    assert operations.boot_inputs[0][1] == {}
    assert operations.boot_inputs[1][1] == {
        "APP_REQUIRED_TOKEN": "repotrial-synthetic-value"
    }
    assert result.stage_history[:5] == [
        "intake",
        "baseline",
        "boot",
        "boot",
        "journeys",
    ]
    assert result.run.stop_reason == "no_remaining_mutations"


def test_repeated_boot_error_stops_after_four_attempts_without_retry_storm(
    tmp_path: Path,
) -> None:
    repeated = (Verdict.FAIL, {"logs": "APP_REQUIRED_TOKEN is required"})
    operations = FakeStageOperations(boots=[repeated, repeated, repeated, repeated])
    context, source = _context(tmp_path, operations, FakeSandboxProvider())

    result = _run(_state(source.parent), context)

    assert [item[2] for item in operations.boot_inputs] == [1, 2, 3, 4]
    assert result.stage_history.count("boot") == 4
    assert result.run.stop_reason == "boot_recovery_stopped"
    assert "journeys" not in result.stage_history


def test_unsafe_model_recovery_cannot_bypass_propose_recovery_policy(
    tmp_path: Path,
) -> None:
    operations = FakeStageOperations(
        boots=[(Verdict.FAIL, {"logs": "unclassified failure"})]
    )
    model = FakeModelAdapter(
        RecoveryAction(
            action="shell",
            params={"command": "cat ~/.ssh/id_rsa"},
            reason="untrusted proposal",
        )
    )
    context, source = _context(tmp_path, operations, FakeSandboxProvider(), model=model)

    result = _run(_state(source.parent), context)

    assert model.calls == 1
    assert len(operations.boot_inputs) == 1
    assert result.run.stop_reason == "boot_recovery_stopped"


@pytest.mark.parametrize(
    ("action", "expected_sleeps"),
    [
        (RecoveryAction(action="retry", params={}, reason="retry"), []),
        (
            RecoveryAction(action="wait", params={"seconds": 1}, reason="wait"),
            [1.0],
        ),
    ],
)
def test_validated_retry_and_wait_actions_return_to_boot_without_real_sleep(
    tmp_path: Path,
    action: RecoveryAction,
    expected_sleeps: list[float],
) -> None:
    operations = FakeStageOperations(
        boots=[
            (Verdict.FAIL, {"logs": "unclassified failure"}),
            (Verdict.PASS, {}),
        ]
    )
    model = FakeModelAdapter(action)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    context, source = _context(
        tmp_path,
        operations,
        FakeSandboxProvider(),
        model=model,
        sleeper=fake_sleep,
    )

    result = _run(_state(source.parent), context)

    assert len(operations.boot_inputs) == 2
    assert sleeps == expected_sleeps
    assert result.run.stop_reason == "no_remaining_mutations"


def test_unsupported_boot_is_terminal_without_recovery(tmp_path: Path) -> None:
    operations = FakeStageOperations(boots=[(Verdict.UNSUPPORTED, {})])
    context, source = _context(tmp_path, operations, FakeSandboxProvider())

    result = _run(_state(source.parent), context)

    assert len(operations.boot_inputs) == 1
    assert result.run.stop_reason == "boot_unsupported"
    assert result.stage_history[-1] == "report_or_next"
    assert "journeys" not in result.stage_history


def test_boot_result_cannot_claim_a_different_attempt(tmp_path: Path) -> None:
    operations = WrongAttemptOperations()
    context, source = _context(tmp_path, operations, FakeSandboxProvider())

    with pytest.raises(ValueError, match="attempt does not match"):
        _run(_state(source.parent), context)


def test_stage_operation_cannot_change_checkpoint_identity(tmp_path: Path) -> None:
    operations = WrongRunOperations()
    context, source = _context(tmp_path, operations, FakeSandboxProvider())

    with pytest.raises(ValueError, match="cannot change run_id"):
        _run(_state(source.parent), context)


def test_total_boot_attempt_cap_stops_alternating_retry_errors(tmp_path: Path) -> None:
    operations = FakeStageOperations(
        boots=[
            (Verdict.FAIL, {"logs": f"different failure {index}"}) for index in range(4)
        ]
    )
    model = FakeModelAdapter(
        RecoveryAction(action="retry", params={}, reason="bounded retry")
    )
    context, source = _context(tmp_path, operations, FakeSandboxProvider(), model=model)

    result = _run(_state(source.parent), context)

    assert len(operations.boot_inputs) == 4
    assert result.stage_history.count("boot") == 5
    assert result.run.stop_reason == "boot_recovery_stopped"


def test_journey_existence_without_actual_pass_stops_before_experiment(
    tmp_path: Path,
) -> None:
    operations = FakeStageOperations(journey_verdict=Verdict.FAIL)
    provider = FakeSandboxProvider()
    context, source = _context(tmp_path, operations, provider)
    state = _state(source.parent)
    state.risk_findings = [_risk("root_user")]

    result = _run(state, context)

    assert result.run.stop_reason == "insufficient_coverage"
    assert result.run.experiments == []
    assert "experiment" not in result.stage_history
    assert operations.calls == ["intake", "baseline", "boot", "journeys", "observe"]
    assert provider.calls == []


@pytest.mark.parametrize(
    ("healthy", "publish", "expected_verdict", "expected_reason"),
    [
        (False, False, ExperimentVerdict.ROLLBACK, "boot_regression"),
        (True, False, ExperimentVerdict.STOP, "publish_failed"),
    ],
)
def test_run_experiment_is_the_only_verdict_engine_and_nonkeep_preserves_hash(
    tmp_path: Path,
    healthy: bool,
    publish: bool,
    expected_verdict: ExperimentVerdict,
    expected_reason: str,
) -> None:
    operations = FakeStageOperations()
    provider = GraphProvider(healthy=healthy, host_port=9 if publish else None)
    context, source = _context(tmp_path, operations, provider)
    state = _state(source.parent)
    original_hash = state.current_config_hash
    state.risk_findings = [_risk("root_user")]

    result = _run(state, context)

    assert len(result.run.experiments) == 1
    assert result.run.experiments[0].verdict is expected_verdict
    assert result.run.experiments[0].reason == expected_reason
    assert result.run.current_config_hash == original_hash
    if expected_verdict is ExperimentVerdict.STOP:
        assert result.run.stop_reason == f"experiment:{expected_reason}"


def test_two_keeps_materialize_full_compose_chain_without_mutating_source(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        operations = FakeStageOperations()
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, operations, provider)
        original = source.read_bytes()
        state = _state(source.parent)
        state.risk_findings = [_risk("root_user"), _risk("cap_add")]

        result = _run(state, context)

    assert [record.verdict for record in result.run.experiments] == [
        ExperimentVerdict.KEEP,
        ExperimentVerdict.KEEP,
    ]
    assert (
        result.run.current_config_hash
        == result.run.experiments[-1].candidate_config_hash
    )
    assert result.run.compose_path is not None
    accepted = context.workspace / result.run.compose_path
    assert accepted.is_file()
    assert _compose_hash(accepted) == result.run.current_config_hash
    assert source.read_bytes() == original
    overlay_artifacts = [
        context.workspace / path
        for path in result.run.artifacts
        if path.endswith(".overlay.yaml")
    ]
    assert len(overlay_artifacts) == 2
    assert all(path.is_file() for path in overlay_artifacts)
    assert len([call for call in provider.calls if call[0] == "create"]) == 2
    assert len([call for call in provider.calls if call[0] == "destroy"]) == 2
    assert not any(
        record.reason == "parent_hash_mismatch" for record in result.run.experiments
    )
    assert result.stage_history == [
        "intake",
        "baseline",
        "boot",
        "journeys",
        "observe",
        "propose_mutation",
        "experiment",
        "decide",
        "report_or_next",
        "propose_mutation",
        "experiment",
        "decide",
        "report_or_next",
        "propose_mutation",
        "report_or_next",
    ]


def test_keep_rejects_accepted_directory_reached_through_intermediate_link(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        operations = FakeStageOperations()
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, operations, provider)
        real_parent = context.workspace / "real-parent"
        real_parent.mkdir()
        accepted = real_parent / "accepted"
        accepted.mkdir()
        linked_parent = context.workspace / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        context = replace(context, accepted_compose_dir=linked_parent / accepted.name)
        state = _state(source.parent)
        state.risk_findings = [_risk("root_user")]

        with pytest.raises(ValueError, match="link"):
            _run(state, context)

    assert list(accepted.iterdir()) == []


@pytest.mark.parametrize("matching", [True, False])
def test_keep_materialization_replay_reuses_only_matching_candidate(
    tmp_path: Path,
    matching: bool,
) -> None:
    with _healthy_server() as (_, port):
        operations = FakeStageOperations()
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, operations, provider)
        state = _state(source.parent)
        state.risk_findings = [_risk("root_user")]
        mutation = Mutation(
            mutation_id="policy:set_non_root:web",
            type=MutationType.SET_NON_ROOT,
            service="web",
        )
        candidate = apply_mutation(load_compose(source), mutation)
        candidate_material = canonical_compose_json(candidate).encode("utf-8")
        candidate_hash = f"sha256:{hashlib.sha256(candidate_material).hexdigest()}"
        target = (
            context.accepted_compose_dir
            / f"accepted-0001-{candidate_hash.removeprefix('sha256:')[:16]}.compose.yaml"
        )
        _write_compose(target, candidate if matching else load_compose(source))

        if matching:
            result = _run(state, context)
            assert (
                result.run.compose_path
                == target.relative_to(context.workspace).as_posix()
            )
            assert _compose_hash(target) == candidate_hash
        else:
            with pytest.raises(ValueError, match="replay hash mismatch"):
                _run(state, context)


def test_checkpoint_resume_is_scoped_to_run_id_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    operations = FakeStageOperations(boots=[(Verdict.PASS, {}), (Verdict.PASS, {})])
    context, source = _context(tmp_path, operations, FakeSandboxProvider())
    graph = build_run_graph(interrupt_after=("boot",))
    first_state = _state(source.parent, run_id="checkpoint-run")

    interrupted = asyncio.run(ainvoke_run(graph, first_state, context=context))
    resumed = asyncio.run(aresume_run(graph, "checkpoint-run", context=context))

    assert interrupted.stage_history == ["intake", "baseline", "boot"]
    assert resumed.run.run_id == "checkpoint-run"
    assert resumed.stage_history[-1] == "report_or_next"
    other = _state(source.parent, run_id="other-run")
    other_result = asyncio.run(ainvoke_run(graph, other, context=context))
    assert other_result.run.run_id == "other-run"
    assert other_result.stage_history == ["intake", "baseline", "boot"]

    with pytest.raises(ValueError, match="thread_id must equal run_id"):
        asyncio.run(
            ainvoke_run(
                graph,
                first_state,
                context=context,
                config={"configurable": {"thread_id": "conflicting-run"}},
            )
        )
