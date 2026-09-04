import asyncio
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeVar

import pytest
from langgraph.errors import NodeCancelledError
from pydantic import BaseModel
from ruamel.yaml import YAML

from repotrial.agent import graph as graph_module
from repotrial.agent.graph import ainvoke_run, aresume_run, build_run_graph
from repotrial.agent.state import GraphContext, GraphState
from repotrial.compose.mutations import apply_mutation
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    Journey,
    JourneyAssertion,
    JourneyStep,
    Mutation,
    PinnedRepo,
    RepoRef,
    RunState,
)
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.journey_artifact import JourneyArtifactError

ModelT = TypeVar("ModelT", bound=BaseModel)


def _journey(*, expected_status: int = 200) -> Journey:
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
                        expected=expected_status,
                    )
                ],
            )
        ],
    )


def _compose_hash(path: Path) -> str:
    material = canonical_compose_json(load_compose(path)).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _write_compose(path: Path, compose: dict[str, object]) -> None:
    yaml = YAML(typ="rt", pure=True)
    with path.open("x", encoding="utf-8") as output:
        yaml.dump(compose, output)


def _compose_text(risks: tuple[str, ...]) -> str:
    user = "'0'" if "root_user" in risks else "'1000'"
    cap_add = "    cap_add: [NET_RAW]\n" if "cap_add" in risks else ""
    cap_drop = "" if "cap_add" in risks else "    cap_drop: [ALL]\n"
    return (
        "services:\n"
        "  web:\n"
        "    image: example/web:1\n"
        f"    user: {user}\n"
        "    read_only: true\n"
        f"{cap_drop}"
        f"{cap_add}"
    )


def _state(workspace: Path, *, run_id: str = "run-1") -> RunState:
    del workspace
    return RunState(
        run_id=run_id,
        repo_url="https://example.invalid/repo.git",
        commit_sha="a" * 40,
    )


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


class GraphProvider(FakeSandboxProvider):
    def __init__(
        self,
        *,
        baseline_boots: list[tuple[bool, str]] | None = None,
        candidate_healthy: bool = True,
        host_port: int | None = None,
        candidate_publish: bool = True,
        startup_adapter_result: ExecResult | None = None,
    ) -> None:
        super().__init__(ports={} if host_port is None else {8080: host_port})
        self.baseline_boots = list(baseline_boots or [(True, "")])
        self.candidate_healthy = candidate_healthy
        self.candidate_publish = candidate_publish
        self.startup_adapter_result = startup_adapter_result or ExecResult(
            exit_code=0, stdout="root=/workspace\nmode=600\n", stderr=""
        )
        self._healthy: dict[str, bool] = {}
        self._logs: dict[str, str] = {}
        self._roles: dict[str, str] = {}

    async def create(self, workspace: Path, name: str) -> str:
        sandbox_id = await super().create(workspace, name)
        role = "baseline" if name.startswith("repotrial-baseline-") else "candidate"
        self._roles[sandbox_id] = role
        if role == "baseline":
            healthy, logs = self.baseline_boots.pop(0)
        else:
            healthy, logs = self.candidate_healthy, ""
        self._healthy[sandbox_id] = healthy
        self._logs[sandbox_id] = logs
        return sandbox_id

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self._require_active(sandbox_id)
        snapshot = tuple(argv)
        self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
        if "repotrial-startup-input" in snapshot:
            return self.startup_adapter_result
        if snapshot[-3:] == ("config", "--format", "json"):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps({"services": {"web": {"image": "example/web:1"}}}),
                stderr="",
            )
        healthy = self._healthy[sandbox_id]
        if snapshot[-5:] == ("up", "-d", "--wait", "--wait-timeout", "60"):
            return ExecResult(exit_code=0 if healthy else 1, stdout="", stderr="")
        if snapshot[-4:] == ("ps", "--all", "--format", "json"):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "Service": "web",
                        "State": "running" if healthy else "exited",
                        "Health": "healthy" if healthy else "",
                        "ExitCode": 0 if healthy else 1,
                    }
                ),
                stderr="",
            )
        if snapshot[-4:] == ("logs", "--no-color", "--tail", "200"):
            return ExecResult(
                exit_code=0,
                stdout=self._logs[sandbox_id],
                stderr="",
            )
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

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        if self._roles[sandbox_id] == "candidate" and not self.candidate_publish:
            self.calls.append(("publish_port", sandbox_id, container_port))
            raise KeyError("candidate port is unavailable")
        return await super().publish_port(sandbox_id, container_port)


class CancelOnceProvider(GraphProvider):
    def __init__(self, *, host_port: int) -> None:
        super().__init__(host_port=host_port)
        self.cancel_next_candidate = True

    async def create(self, workspace: Path, name: str) -> str:
        if name.startswith("repotrial-candidate-") and self.cancel_next_candidate:
            self.cancel_next_candidate = False
            raise asyncio.CancelledError
        return await super().create(workspace, name)


class AlwaysCancelCandidateProvider(GraphProvider):
    async def create(self, workspace: Path, name: str) -> str:
        if name.startswith("repotrial-candidate-"):
            raise asyncio.CancelledError
        return await super().create(workspace, name)


class CancelOnceBaselineProvider(GraphProvider):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_baseline = True
        self.baseline_create_attempts = 0

    async def create(self, workspace: Path, name: str) -> str:
        if name.startswith("repotrial-baseline-"):
            self.baseline_create_attempts += 1
            if self.cancel_baseline:
                self.cancel_baseline = False
                raise asyncio.CancelledError
        return await super().create(workspace, name)


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
    provider: SandboxProvider,
    *,
    risks: tuple[str, ...] = (),
    journeys: list[Journey] | None = None,
    model: FakeModelAdapter | None = None,
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
    source.write_text(_compose_text(risks), encoding="utf-8")
    declared = [_journey()] if journeys is None else journeys
    (workspace / "repotrial.journeys.json").write_text(
        json.dumps(
            {"journeys": [journey.model_dump(mode="json") for journey in declared]}
        ),
        encoding="utf-8",
    )
    return (
        GraphContext(
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
        ),
        source,
    )


def _run(state: RunState, context: GraphContext) -> GraphState:
    return asyncio.run(ainvoke_run(build_run_graph(), state, context=context))


def _candidate_create_calls(provider: FakeSandboxProvider) -> list[tuple[object, ...]]:
    return [
        call
        for call in provider.calls
        if call[0] == "create" and str(call[2]).startswith("repotrial-candidate-")
    ]


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


def test_required_startup_input_is_materialized_before_baseline_boot(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    env_file:\n      - .env\n", encoding="utf-8"
    )
    (context.workspace / ".env.sample").write_text("APP_MODE=test\n", encoding="utf-8")

    result = _run(_state(source.parent, run_id="startup-input-order"), context)

    calls = provider.calls
    create_index = next(
        index for index, call in enumerate(calls) if call[0] == "create"
    )
    adapter_index = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "exec" and "repotrial-startup-input" in call[2]
    )
    config_index = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "exec" and call[2][-3:] == ("config", "--format", "json")
    )
    up_index = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "exec"
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
    )
    destroy_index = next(
        index for index, call in enumerate(calls) if call[0] == "destroy"
    )
    assert create_index < adapter_index < config_index < up_index < destroy_index
    compose_calls = [
        call[2]
        for call in calls
        if call[0] == "exec" and "docker" in call[2] and "compose" in call[2]
    ]
    assert compose_calls
    assert all(argv[:3] == ("env", "-u", "APP_MODE") for argv in compose_calls)
    assert all(
        argv[3:7] == ("docker", "compose", "--project-directory", ".")
        for argv in compose_calls
    )
    run_dir = graph_module._run_evidence_directory(result.run, context)
    assert (run_dir / "startup-input-identity.json").is_file()
    assert (
        len(list(context.artifact_dir.glob("baseline-*/startup-input-attempt.jsonl")))
        == 1
    )


def test_guest_startup_input_failure_is_unsupported_and_still_destroys(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        startup_adapter_result=ExecResult(exit_code=25, stdout="", stderr="")
    )
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    env_file:\n      - .env\n", encoding="utf-8"
    )
    (context.workspace / ".env.sample").write_text("APP_MODE=test\n", encoding="utf-8")

    result = _run(_state(source.parent, run_id="startup-input-failure"), context)

    assert result.boot_verdict is Verdict.UNSUPPORTED
    assert result.run.stop_reason == "boot_unsupported"
    assert len([call for call in provider.calls if call[0] == "create"]) == 1
    assert len([call for call in provider.calls if call[0] == "destroy"]) == 1
    assert not any(
        call[0] == "exec"
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
        for call in provider.calls
    )


def test_host_startup_input_rejection_records_evidence_without_sandbox(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    env_file:\n      - .env\n", encoding="utf-8"
    )
    (context.workspace / ".env.sample").write_text("APP_MODE=test\n", encoding="utf-8")
    (context.workspace / ".env").write_text("APP_MODE=host\n", encoding="utf-8")

    result = _run(_state(source.parent, run_id="startup-input-rejection"), context)

    assert result.boot_verdict is Verdict.UNSUPPORTED
    assert result.run.stop_reason == "boot_unsupported"
    assert [call for call in provider.calls if call[0] == "create"] == []
    evidence = list(context.artifact_dir.glob("baseline-*/startup-input-attempt.jsonl"))
    assert len(evidence) == 1
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert [row["outcome"] for row in rows] == ["start", "terminal"]
    assert rows[1]["reason"] == "target_preexisting"


def test_startup_source_change_after_cancel_fails_before_second_create(
    tmp_path: Path,
) -> None:
    provider = CancelOnceBaselineProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    env_file:\n      - .env\n", encoding="utf-8"
    )
    sample = context.workspace / ".env.sample"
    sample.write_text("APP_MODE=first\n", encoding="utf-8")
    graph = build_run_graph()
    state = _state(source.parent, run_id="startup-source-change")

    with pytest.raises(NodeCancelledError):
        asyncio.run(ainvoke_run(graph, state, context=context))
    sample.write_text("APP_MODE=second\n", encoding="utf-8")
    resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.boot_verdict is Verdict.UNSUPPORTED
    assert resumed.run.stop_reason == "boot_unsupported"
    assert provider.baseline_create_attempts == 1
    rejection = max(context.artifact_dir.glob("baseline-*/startup-input-attempt.jsonl"))
    rows = [json.loads(line) for line in rejection.read_text().splitlines()]
    assert rows[-1]["reason"] == "startup_identity_mismatch"


def test_startup_compose_change_after_cancel_fails_before_second_create(
    tmp_path: Path,
) -> None:
    provider = CancelOnceBaselineProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    env_file:\n      - .env\n", encoding="utf-8"
    )
    (context.workspace / ".env.sample").write_text("APP_MODE=test\n", encoding="utf-8")
    graph = build_run_graph()
    state = _state(source.parent, run_id="startup-compose-change")

    with pytest.raises(NodeCancelledError):
        asyncio.run(ainvoke_run(graph, state, context=context))
    source.write_text(
        _compose_text(()).replace("example/web:1", "example/web:changed")
        + "    env_file:\n      - .env\n",
        encoding="utf-8",
    )
    resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.boot_verdict is Verdict.UNSUPPORTED
    assert resumed.run.stop_reason == "boot_unsupported"
    assert provider.baseline_create_attempts == 1
    rejection = max(context.artifact_dir.glob("baseline-*/startup-input-attempt.jsonl"))
    rows = [json.loads(line) for line in rejection.read_text().splitlines()]
    assert rows[-1]["reason"] == "compose_identity_mismatch"


def test_boot_failure_uses_bounded_recovery_then_returns_to_boot(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        baseline_boots=[
            (False, "APP_REQUIRED_TOKEN is required"),
            (True, ""),
        ]
    )
    context, source = _context(tmp_path, provider, journeys=[])

    result = _run(_state(source.parent), context)

    create_calls = [call for call in provider.calls if call[0] == "create"]
    assert len(create_calls) == 2
    boot_commands = [
        call[2]
        for call in provider.calls
        if call[0] == "exec"
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
    ]
    assert boot_commands[0][0] == "docker"
    assert boot_commands[1][0:2] == (
        "env",
        "APP_REQUIRED_TOKEN=repotrial-synthetic-value",
    )
    assert result.stage_history[:4] == ["intake", "baseline", "boot", "boot"]
    assert result.run.stop_reason == "insufficient_coverage"
    evidence_paths = sorted(
        context.artifact_dir.glob("baseline-*/baseline-boot-attempt.json")
    )
    assert len(evidence_paths) == 2
    first_payload = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    assert first_payload["recovery"] == {
        "action": "set_env",
        "disposition": "applied",
        "reason": "missing allowlisted environment variable",
        "stop_reason": None,
    }


def test_boot_derives_declared_environment_after_intake_without_readme_expansion(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        baseline_boots=[(False, "APP_DECLARED_TOKEN is required"), (True, "")]
    )
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    environment:\n      TOKEN: ${APP_DECLARED_TOKEN}\n",
        encoding="utf-8",
    )
    context = replace(
        context,
        allowed_env_keys=frozenset(),
        readme_excerpt="${README_MUST_NOT_AUTHORIZE}",
    )

    result = _run(_state(source.parent), context)

    up_commands = [
        call[2]
        for call in provider.calls
        if call[0] == "exec"
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
    ]
    assert up_commands[1][:2] == (
        "env",
        "APP_DECLARED_TOKEN=repotrial-synthetic-value",
    )
    assert result.run.journeys == []
    assert context.readme_excerpt == "${README_MUST_NOT_AUTHORIZE}"


def test_explicit_allowed_environment_keys_override_pinned_declarations(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(baseline_boots=[(False, "APP_DECLARED_TOKEN is required")])
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    environment:\n      TOKEN: ${APP_DECLARED_TOKEN}\n",
        encoding="utf-8",
    )
    context = replace(context, allowed_env_keys=frozenset({"EXPLICIT_ONLY"}))

    result = _run(_state(source.parent), context)

    assert result.run.stop_reason == "boot_recovery_stopped"
    assert len([call for call in provider.calls if call[0] == "create"]) == 1


def test_boot_passes_only_projected_large_evidence_to_recovery(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        baseline_boots=[
            (False, "x" * 65_000 + " APP_REQUIRED_TOKEN is required"),
            (True, ""),
        ]
    )
    context, source = _context(tmp_path, provider, journeys=[])

    result = _run(_state(source.parent), context)

    assert len([call for call in provider.calls if call[0] == "create"]) == 2
    assert result.run.stop_reason == "insufficient_coverage"


def test_repeated_boot_error_stops_after_four_attempts_without_retry_storm(
    tmp_path: Path,
) -> None:
    repeated = (False, "APP_REQUIRED_TOKEN is required")
    provider = GraphProvider(baseline_boots=[repeated] * 4)
    context, source = _context(tmp_path, provider, journeys=[])

    result = _run(_state(source.parent), context)

    assert len([call for call in provider.calls if call[0] == "create"]) == 4
    assert result.stage_history.count("boot") == 4
    assert result.run.stop_reason == "boot_recovery_stopped"
    assert "journeys" not in result.stage_history


def test_unsafe_model_recovery_cannot_bypass_propose_recovery_policy(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(baseline_boots=[(False, "unclassified failure")])
    model = FakeModelAdapter(
        RecoveryAction(
            action="shell",
            params={"command": "cat ~/.ssh/id_rsa"},
            reason="untrusted proposal",
        )
    )
    context, source = _context(tmp_path, provider, journeys=[], model=model)

    result = _run(_state(source.parent), context)

    assert model.calls == 1
    assert len([call for call in provider.calls if call[0] == "create"]) == 1
    assert result.run.stop_reason == "boot_recovery_stopped"


def test_recovery_evidence_redacts_every_runtime_env_value_before_persistence(
    tmp_path: Path,
) -> None:
    secret = "postgresql://alice:recovery-secret@db.invalid/app"
    provider = GraphProvider(baseline_boots=[(False, "unclassified failure")])
    model = FakeModelAdapter(
        RecoveryAction(action="stop", params={}, reason=f"stop with {secret}")
    )
    context, source = _context(tmp_path, provider, journeys=[], model=model)
    context = replace(context, env={"PUBLIC_DSN": secret})

    _run(_state(source.parent), context)

    evidence_path = next(
        context.artifact_dir.glob("baseline-*/baseline-boot-attempt.json")
    )
    assert secret not in evidence_path.read_text(encoding="utf-8")


def test_validated_retry_action_returns_to_boot(tmp_path: Path) -> None:
    provider = GraphProvider(
        baseline_boots=[(False, "unclassified failure"), (True, "")]
    )
    model = FakeModelAdapter(RecoveryAction(action="retry", params={}, reason="retry"))
    context, source = _context(tmp_path, provider, journeys=[], model=model)

    result = _run(_state(source.parent), context)

    assert len([call for call in provider.calls if call[0] == "create"]) == 2
    assert result.run.stop_reason == "insufficient_coverage"


def test_schema_valid_wait_stops_without_host_sleep_or_retry(tmp_path: Path) -> None:
    provider = GraphProvider(
        baseline_boots=[(False, "unclassified failure"), (True, "")]
    )
    model = FakeModelAdapter(
        RecoveryAction(action="wait", params={"seconds": 1}, reason="wait")
    )
    context, source = _context(tmp_path, provider, journeys=[], model=model)

    result = _run(_state(source.parent), context)

    assert len([call for call in provider.calls if call[0] == "create"]) == 1
    assert result.run.stop_reason == "boot_recovery_stopped"
    evidence_path = next(
        context.artifact_dir.glob("baseline-*/baseline-boot-attempt.json")
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["recovery"] == {
        "action": "wait",
        "disposition": "unsupported",
        "reason": "wait",
        "stop_reason": "boot_recovery_stopped",
    }


def test_total_boot_attempt_cap_stops_alternating_retry_errors(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        baseline_boots=[(False, f"different failure {index}") for index in range(4)]
    )
    model = FakeModelAdapter(
        RecoveryAction(action="retry", params={}, reason="bounded retry")
    )
    context, source = _context(tmp_path, provider, journeys=[], model=model)

    result = _run(_state(source.parent), context)

    assert len([call for call in provider.calls if call[0] == "create"]) == 4
    assert result.stage_history.count("boot") == 5
    assert result.run.stop_reason == "boot_recovery_stopped"


def test_journey_existence_without_actual_pass_stops_before_experiment(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(
            tmp_path,
            provider,
            risks=("root_user",),
            journeys=[_journey(expected_status=201)],
        )

        result = _run(_state(source.parent), context)

    assert result.run.stop_reason == "insufficient_coverage"
    assert result.run.experiments == []
    assert "experiment" not in result.stage_history
    assert _candidate_create_calls(provider) == []


@pytest.mark.parametrize(
    ("healthy", "publish", "expected_verdict", "expected_reason"),
    [
        (False, True, ExperimentVerdict.ROLLBACK, "boot_regression"),
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
    with _healthy_server() as (_, port):
        provider = GraphProvider(
            candidate_healthy=healthy,
            host_port=port,
            candidate_publish=publish,
        )
        context, source = _context(tmp_path, provider, risks=("root_user",))

        result = _run(_state(source.parent), context)

    assert len(result.run.experiments) == 1
    assert result.run.experiments[0].verdict is expected_verdict
    assert result.run.experiments[0].reason == expected_reason
    assert result.run.current_config_hash == result.run.baseline_config_hash
    if expected_verdict is ExperimentVerdict.STOP:
        assert result.run.stop_reason == f"experiment:{expected_reason}"


def test_two_keeps_materialize_full_compose_chain_without_mutating_source(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user", "cap_add"))
        original = source.read_bytes()

        result = _run(_state(source.parent), context)

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
    assert len(_candidate_create_calls(provider)) == 2
    assert not any(
        record.reason == "parent_hash_mismatch" for record in result.run.experiments
    )


def test_keep_rejects_accepted_directory_reached_through_intermediate_link(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        real_parent = context.workspace / "real-parent"
        real_parent.mkdir()
        accepted = real_parent / "accepted"
        accepted.mkdir()
        linked_parent = context.workspace / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        context = replace(context, accepted_compose_dir=linked_parent / accepted.name)

        with pytest.raises(ValueError, match="link"):
            _run(_state(source.parent), context)

    assert list(accepted.iterdir()) == []


@pytest.mark.parametrize("matching", [True, False])
def test_keep_materialization_replay_reuses_only_matching_candidate(
    tmp_path: Path,
    matching: bool,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
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
            result = _run(_state(source.parent), context)
            assert (
                result.run.compose_path
                == target.relative_to(context.workspace).as_posix()
            )
            assert _compose_hash(target) == candidate_hash
        else:
            with pytest.raises(ValueError, match="replay hash mismatch"):
                _run(_state(source.parent), context)


def test_checkpoint_resume_is_scoped_to_run_id_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(
            baseline_boots=[(True, ""), (True, "")], host_port=port
        )
        context, source = _context(tmp_path, provider)
        graph = build_run_graph(interrupt_after=("boot",))
        first_state = _state(source.parent, run_id="checkpoint-run")

        interrupted = asyncio.run(ainvoke_run(graph, first_state, context=context))
        resumed = asyncio.run(aresume_run(graph, "checkpoint-run", context=context))
        other = _state(source.parent, run_id="other-run")
        other_result = asyncio.run(ainvoke_run(graph, other, context=context))

    assert interrupted.stage_history == ["intake", "baseline", "boot"]
    assert resumed.run.run_id == "checkpoint-run"
    assert resumed.stage_history[-1] == "report_or_next"
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


def test_stop_before_overlay_appends_duplicate_baseline_record_once(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        state = _state(source.parent, run_id="duplicate-baseline")
        state.journeys = [_journey(), _journey()]

        result = _run(state, context)

    assert len(result.run.experiments) == 1
    assert result.run.experiments[0].verdict is ExperimentVerdict.STOP
    assert result.run.experiments[0].reason == "invalid_baseline:duplicate_result_id"
    assert result.run.stop_reason == "experiment:invalid_baseline:duplicate_result_id"
    assert not list(context.overlay_dir.iterdir())
    assert _candidate_create_calls(provider) == []


def test_stop_before_overlay_appends_parent_mismatch_record_once(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("propose_mutation",))
        state = _state(source.parent, run_id="parent-mismatch")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        source.write_text(
            _compose_text(("root_user",)).replace(
                "example/web:1", "example/web:changed"
            ),
            encoding="utf-8",
        )
        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert interrupted.pending_mutation is not None
    assert len(resumed.run.experiments) == 1
    assert resumed.run.experiments[0].verdict is ExperimentVerdict.STOP
    assert resumed.run.experiments[0].reason == "parent_hash_mismatch"
    assert resumed.run.stop_reason == "experiment:parent_hash_mismatch"
    assert not list(context.overlay_dir.iterdir())
    assert _candidate_create_calls(provider) == []


def test_cancelled_experiment_resume_uses_fresh_attempt_namespace(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = CancelOnceProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("propose_mutation",))
        state = _state(source.parent, run_id="cancelled-experiment")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        with pytest.raises(NodeCancelledError):
            asyncio.run(aresume_run(graph, state.run_id, context=context))
        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert interrupted.pending_mutation is not None
    assert resumed.run.experiments[0].verdict is ExperimentVerdict.KEEP
    assert resumed.run.stop_reason == "no_remaining_mutations"
    overlay_files = sorted(context.overlay_dir.glob("*.overlay.yaml"))
    assert len(overlay_files) == 2
    lifecycle_files = sorted(context.artifact_dir.rglob("candidate-*-lifecycle.jsonl"))
    assert len(lifecycle_files) == 2
    assert all(path.is_file() for path in [*overlay_files, *lifecycle_files])


def test_experiment_attempts_fail_closed_after_four_cancellations(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = AlwaysCancelCandidateProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("propose_mutation",))
        state = _state(source.parent, run_id="exhausted-experiment")

        asyncio.run(ainvoke_run(graph, state, context=context))
        for _ in range(4):
            with pytest.raises(NodeCancelledError):
                asyncio.run(aresume_run(graph, state.run_id, context=context))
        with pytest.raises(ValueError, match="attempt slots exhausted"):
            asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert len(list(context.overlay_dir.glob("*.overlay.yaml"))) == 4
    assert len(list(context.artifact_dir.rglob("candidate-*-lifecycle.jsonl"))) == 4


def test_stop_rejects_selected_overlay_replaced_by_link(tmp_path: Path) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("experiment",))
        state = _state(source.parent, run_id="unsafe-stop-overlay")
        state.journeys = [_journey(), _journey()]

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.pending_overlay_path is not None
        selected = context.workspace / interrupted.pending_overlay_path
        outside = tmp_path / "outside-overlay.yaml"
        outside.write_text("services: {}\n", encoding="utf-8")
        selected.symlink_to(outside)

        with pytest.raises(ValueError, match="regular file"):
            asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert _candidate_create_calls(provider) == []


def test_materialized_stop_overlay_deleted_after_checkpoint_fails_closed(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port, candidate_publish=False)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("experiment",))
        state = _state(source.parent, run_id="deleted-stop-overlay")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.pending_experiment is not None
        assert interrupted.pending_experiment.verdict is ExperimentVerdict.STOP
        assert interrupted.pending_experiment.reason == "publish_failed"
        assert interrupted.pending_overlay_path is not None
        assert interrupted.pending_overlay_materialized is True
        selected = context.workspace / interrupted.pending_overlay_path
        assert selected.is_file()
        selected.unlink()

        with pytest.raises(ValueError, match="overlay"):
            asyncio.run(aresume_run(graph, state.run_id, context=context))


def test_prematerialization_stop_overlay_inserted_after_checkpoint_fails_closed(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("experiment",))
        state = _state(source.parent, run_id="inserted-stop-overlay")
        state.journeys = [_journey(), _journey()]

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.pending_experiment is not None
        assert interrupted.pending_experiment.verdict is ExperimentVerdict.STOP
        assert (
            interrupted.pending_experiment.reason
            == "invalid_baseline:duplicate_result_id"
        )
        assert interrupted.pending_overlay_path is not None
        assert interrupted.pending_overlay_materialized is False
        selected = context.workspace / interrupted.pending_overlay_path
        assert not selected.exists()
        selected.write_text("services: {}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="overlay"):
            asyncio.run(aresume_run(graph, state.run_id, context=context))


def test_default_graph_composes_real_baseline_services_in_one_sandbox(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider)
        state = RunState(
            run_id="concrete-baseline",
            repo_url="https://example.invalid/repo.git",
            commit_sha="a" * 40,
        )

        result = _run(state, context)

    assert result.run.compose_path == source.relative_to(context.workspace).as_posix()
    assert result.run.baseline_config_hash == _compose_hash(source)
    assert result.run.current_config_hash == _compose_hash(source)
    assert result.run.risk_findings == []
    assert result.run.journeys == [_journey()]
    assert result.run.baseline_journey_results[0].verdict is Verdict.PASS
    assert result.run.baseline_observation is not None
    assert result.run.stop_reason == "no_remaining_mutations"
    assert len([call for call in provider.calls if call[0] == "create"]) == 1
    assert len([call for call in provider.calls if call[0] == "destroy"]) == 1
    assert result.run.sandbox_id is None
    evidence_paths = list(
        context.artifact_dir.glob("baseline-*/baseline-observation-boundary.jsonl")
    )
    assert len(evidence_paths) == 1
    evidence_rows = [
        json.loads(line)
        for line in evidence_paths[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [(row["operation"], row["outcome"]) for row in evidence_rows] == [
        ("discovery", "start"),
        ("discovery", "success"),
        ("network", "start"),
        ("network", "success"),
        ("audit", "start"),
        ("audit", "success"),
    ]
    assert not any("observation-boundary" in item for item in result.run.artifacts)
    assert "collector_succeeded" not in result.run.model_dump_json()


def test_baseline_records_loopback_compatibility_before_sandbox_and_replays_exact_file(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, journeys=[])
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n"
                f"    ports:\n      - '127.0.0.1:{port}:8080'\n",
            ),
            encoding="utf-8",
        )
        source_before = source.read_bytes()
        graph = build_run_graph(interrupt_after=("baseline",))
        state = _state(source.parent, run_id="compatibility-baseline")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))

        assert [call for call in provider.calls if call[0] == "create"] == []
        assert interrupted.run.compatibility_overlay_path == (
            ".repotrial-overlays/compatibility.overlay.yaml"
        )
        assert interrupted.run.compatibility_overlay_sha256 is not None
        compatibility = context.workspace / interrupted.run.compatibility_overlay_path
        assert compatibility.is_file()
        assert hashlib.sha256(compatibility.read_bytes()).hexdigest() == (
            interrupted.run.compatibility_overlay_sha256
        )
        assert (
            interrupted.run.artifacts.count(interrupted.run.compatibility_overlay_path)
            == 1
        )
        assert source.read_bytes() == source_before

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    compose_calls = [
        call[2]
        for call in provider.calls
        if call[0] == "exec" and "docker" in call[2] and "compose" in call[2]
    ]
    assert compose_calls
    assert all(
        ".repotrial-overlays/compatibility.overlay.yaml" in argv
        for argv in compose_calls
    )
    assert resumed.run.compatibility_overlay_path == (
        interrupted.run.compatibility_overlay_path
    )


def test_candidate_preserves_compatibility_identity_and_hash_policy(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        source.write_text(
            _compose_text(("root_user",)).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n"
                f"    ports:\n      - '127.0.0.1:{port}:8080'\n",
            ),
            encoding="utf-8",
        )
        source_before = source.read_bytes()
        state = _state(source.parent, run_id="compatibility-candidate")

        result = _run(state, context)

    assert result.run.compatibility_overlay_path == (
        ".repotrial-overlays/compatibility.overlay.yaml"
    )
    assert result.run.compatibility_overlay_sha256 is not None
    assert result.run.artifacts.count(result.run.compatibility_overlay_path) == 1
    assert source.read_bytes() == source_before
    assert result.run.experiments
    experiment = result.run.experiments[0]
    assert experiment.parent_config_hash == _compose_hash(source)
    candidate = apply_mutation(load_compose(source), experiment.mutation)
    assert experiment.candidate_config_hash == (
        "sha256:"
        + hashlib.sha256(canonical_compose_json(candidate).encode("utf-8")).hexdigest()
    )
    compose_calls = [
        call[2]
        for call in provider.calls
        if call[0] == "exec" and "docker" in call[2] and "compose" in call[2]
    ]
    candidate_calls = [
        argv
        for argv in compose_calls
        if sum(item.endswith(".overlay.yaml") for item in argv) >= 2
        and ".repotrial-overlays/compatibility.overlay.yaml" in argv
    ]
    assert candidate_calls
    for argv in candidate_calls:
        compatibility_index = argv.index(
            ".repotrial-overlays/compatibility.overlay.yaml"
        )
        overlay_indices = [
            index
            for index, item in enumerate(argv)
            if item.endswith(".overlay.yaml")
            and item != ".repotrial-overlays/compatibility.overlay.yaml"
        ]
        assert overlay_indices
        assert compatibility_index < overlay_indices[0]


def test_compatibility_planning_rejection_reports_without_creating_sandbox(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        "services:\n"
        "  web:\n"
        "    image: example/web:1\n"
        "    ports:\n"
        "      - '127.0.0.1:5000:8080'\n"
        "      - '[::1]:5001:8080'\n",
        encoding="utf-8",
    )

    result = _run(_state(source.parent, run_id="compatibility-rejection"), context)

    assert result.run.stop_reason == "compatibility:ambiguous_loopback_binding"
    assert result.stage_history == [
        "intake",
        "baseline",
        "report_or_next",
    ]
    assert [call for call in provider.calls if call[0] == "create"] == []
    assert result.run.compatibility_overlay_path is None
    assert result.run.compatibility_overlay_sha256 is None
    assert not (context.overlay_dir / "compatibility.overlay.yaml").exists()


def test_no_eligible_loopback_mapping_keeps_compatibility_artifact_absent(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])

    result = _run(_state(source.parent, run_id="compatibility-noop"), context)

    assert result.run.compatibility_overlay_path is None
    assert result.run.compatibility_overlay_sha256 is None
    assert "compatibility.overlay.yaml" not in result.run.artifacts
    assert not (context.overlay_dir / "compatibility.overlay.yaml").exists()


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("delete", "artifact_missing"),
        ("replace", "artifact_hash_mismatch"),
        ("symlink", "artifact_linked"),
    ],
)
def test_checkpoint_resume_rejects_tampered_compatibility_artifact(
    tmp_path: Path, tamper: str, reason: str
) -> None:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()).replace(
            "    image: example/web:1\n",
            "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
        ),
        encoding="utf-8",
    )
    graph = build_run_graph(interrupt_after=("baseline",))
    state = _state(source.parent, run_id=f"compatibility-tamper-{tamper}")
    interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
    assert interrupted.run.compatibility_overlay_path is not None
    artifact = context.workspace / interrupted.run.compatibility_overlay_path
    if tamper == "delete":
        artifact.unlink()
    elif tamper == "replace":
        artifact.write_text("services: {}\n", encoding="utf-8")
    else:
        replacement = tmp_path / "replacement.overlay.yaml"
        replacement.write_text("services: {}\n", encoding="utf-8")
        artifact.unlink()
        artifact.symlink_to(replacement)

    resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == f"compatibility:{reason}"
    assert [call for call in provider.calls if call[0] == "create"] == []


def test_graph_persists_baseline_journeys_before_workload_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider)
        graph = build_run_graph(interrupt_after=("baseline",))

        asyncio.run(
            ainvoke_run(
                graph, _state(source.parent, run_id="journey-guard"), context=context
            )
        )
        artifact = graph_module._baseline_journey_artifact(
            _state(source.parent, run_id="journey-guard"), context
        )
        document = json.loads(artifact.read_text(encoding="utf-8"))
        document["journeys"][0]["steps"][0]["params"]["path"] = "/tampered"
        artifact.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(
            JourneyArtifactError, match="baseline journey artifact hash mismatch"
        ):
            asyncio.run(aresume_run(graph, "journey-guard", context=context))

    assert [call for call in provider.calls if call[0] == "create"] == []


def test_graph_rejects_tampered_baseline_journeys_before_candidate_replay(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("propose_mutation",))

        asyncio.run(
            ainvoke_run(
                graph,
                _state(source.parent, run_id="candidate-journey-guard"),
                context=context,
            )
        )
        artifact = graph_module._baseline_journey_artifact(
            _state(source.parent, run_id="candidate-journey-guard"), context
        )
        document = json.loads(artifact.read_text(encoding="utf-8"))
        document["journeys"][0]["name"] = "Tampered"
        artifact.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(
            JourneyArtifactError, match="baseline journey artifact hash mismatch"
        ):
            asyncio.run(aresume_run(graph, "candidate-journey-guard", context=context))

    assert _candidate_create_calls(provider) == []


def test_same_run_baseline_reentry_verifies_identical_canonical_journeys(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider)
    state = _state(source.parent, run_id="same-run-reentry")

    for _ in range(2):
        graph = build_run_graph(interrupt_after=("baseline",))
        result = asyncio.run(ainvoke_run(graph, state, context=context))
        assert result.stage_history == ["intake", "baseline"]

    artifact = graph_module._baseline_journey_artifact(state, context)
    assert artifact.is_file()
    assert [call for call in provider.calls if call[0] == "create"] == []


def test_missing_commit_uses_narrow_repository_pinner_then_real_baseline(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        workspace = tmp_path / "pinned-workspace"
        artifact_dir = tmp_path / "pinned-artifacts"
        artifact_dir.mkdir()

        async def fake_pinner(
            url: str, destination: Path, requested_ref: str | None
        ) -> PinnedRepo:
            del url, requested_ref
            destination.mkdir()
            (destination / "compose.yaml").write_text(
                _compose_text(()), encoding="utf-8"
            )
            (destination / "repotrial.journeys.json").write_text(
                json.dumps({"journeys": [_journey().model_dump(mode="json")]}),
                encoding="utf-8",
            )
            return PinnedRepo(
                repo=RepoRef(
                    url="https://github.com/example/repo",
                    owner="example",
                    repo="repo",
                ),
                commit_sha="b" * 40,
                local_path=destination,
            )

        context = GraphContext(
            provider=provider,
            workspace=workspace,
            artifact_dir=artifact_dir,
            overlay_dir=workspace / ".repotrial-overlays",
            accepted_compose_dir=workspace / ".repotrial-accepted",
            env={},
            allowed_env_keys=frozenset(),
            readme_excerpt="",
            container_port=8080,
            repository_pinner=fake_pinner,
        )
        state = RunState(
            run_id="pinned-intake",
            repo_url="https://github.com/example/repo",
        )

        result = _run(state, context)

    assert result.run.repo_url == "https://github.com/example/repo"
    assert result.run.commit_sha == "b" * 40
    assert result.run.compose_path == "compose.yaml"
    assert result.run.baseline_journey_results[0].verdict is Verdict.PASS
    assert result.run.sandbox_id is None


def test_pinned_intake_derives_root_readme_only_for_deterministic_journey_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        workspace = tmp_path / "pinned-workspace"
        artifact_dir = tmp_path / "pinned-artifacts"
        artifact_dir.mkdir()
        captured_recovery_excerpts: list[str] = []

        async def fake_pinner(
            url: str, destination: Path, requested_ref: str | None
        ) -> PinnedRepo:
            del url, requested_ref
            destination.mkdir()
            (destination / "compose.yaml").write_text(
                _compose_text(()), encoding="utf-8"
            )
            (destination / "README.md").write_text(
                "[health](/health)\n", encoding="utf-8"
            )
            return PinnedRepo(
                repo=RepoRef(
                    url="https://github.com/example/repo", owner="example", repo="repo"
                ),
                commit_sha="d" * 40,
                local_path=destination,
            )

        async def capture_recovery(
            logs: dict[str, str],
            readme_excerpt: str,
            allowed_env_keys: set[str],
            repeated_error_count: int,
            model: object = None,
        ) -> RecoveryAction:
            del logs, allowed_env_keys, repeated_error_count, model
            captured_recovery_excerpts.append(readme_excerpt)
            return RecoveryAction(action="stop", params={}, reason="stop")

        monkeypatch.setattr(graph_module, "propose_recovery", capture_recovery)
        context = GraphContext(
            provider=provider,
            workspace=workspace,
            artifact_dir=artifact_dir,
            overlay_dir=workspace / ".repotrial-overlays",
            accepted_compose_dir=workspace / ".repotrial-accepted",
            env={},
            allowed_env_keys=frozenset(),
            readme_excerpt="",
            container_port=8080,
            repository_pinner=fake_pinner,
        )

        result = _run(
            RunState(
                run_id="journey-after-intake", repo_url="https://example.invalid/repo"
            ),
            context,
        )

    assert result.run.commit_sha == "d" * 40
    assert [journey.journey_id for journey in result.run.journeys] == ["readme-1"]
    assert result.run.journeys[0].steps[0].params == {
        "method": "GET",
        "path": "/health",
    }
    assert captured_recovery_excerpts == []
    assert context.readme_excerpt == ""


def test_explicit_readme_excerpt_wins_without_filesystem_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, journeys=[])
        (context.workspace / "repotrial.journeys.json").unlink()
        context = replace(context, readme_excerpt="[explicit](/health)")

        def forbidden_derivation(workspace: Path) -> str:
            del workspace
            raise AssertionError(
                "explicit README must not trigger filesystem derivation"
            )

        monkeypatch.setattr(
            graph_module, "derive_journey_readme_excerpt", forbidden_derivation
        )

        result = _run(_state(source.parent), context)

    assert [journey.journey_id for journey in result.run.journeys] == ["readme-1"]


def test_derived_readme_is_never_passed_to_no_model_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GraphProvider(baseline_boots=[(False, "unclassified failure")])
    context, source = _context(tmp_path, provider, journeys=[])
    (context.workspace / "repotrial.journeys.json").unlink()
    (context.workspace / "README.md").write_text(
        "[health](/health)\n", encoding="utf-8"
    )
    captured: list[str] = []

    async def capture_recovery(
        logs: dict[str, str],
        readme_excerpt: str,
        allowed_env_keys: set[str],
        repeated_error_count: int,
        model: object = None,
    ) -> RecoveryAction:
        del logs, allowed_env_keys, repeated_error_count, model
        captured.append(readme_excerpt)
        return RecoveryAction(action="stop", params={}, reason="stop")

    monkeypatch.setattr(graph_module, "propose_recovery", capture_recovery)

    result = _run(_state(source.parent), context)

    assert [journey.journey_id for journey in result.run.journeys] == ["readme-1"]
    assert captured == [""]


def test_pinned_intake_derives_declared_environment_once_before_boot(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        baseline_boots=[(False, "PINNED_TOKEN is required"), (True, "")]
    )
    workspace = tmp_path / "pinned-workspace"
    artifact_dir = tmp_path / "pinned-artifacts"
    artifact_dir.mkdir()
    pinner_calls = 0

    async def fake_pinner(
        url: str, destination: Path, requested_ref: str | None
    ) -> PinnedRepo:
        nonlocal pinner_calls
        del url, requested_ref
        pinner_calls += 1
        destination.mkdir()
        (destination / "compose.yaml").write_text(
            _compose_text(()) + "    environment:\n      TOKEN: ${PINNED_TOKEN}\n",
            encoding="utf-8",
        )
        return PinnedRepo(
            repo=RepoRef(
                url="https://github.com/example/repo", owner="example", repo="repo"
            ),
            commit_sha="c" * 40,
            local_path=destination,
        )

    context = GraphContext(
        provider=provider,
        workspace=workspace,
        artifact_dir=artifact_dir,
        overlay_dir=workspace / ".repotrial-overlays",
        accepted_compose_dir=workspace / ".repotrial-accepted",
        env={},
        allowed_env_keys=frozenset(),
        readme_excerpt="${README_MUST_NOT_AUTHORIZE}",
        container_port=8080,
        repository_pinner=fake_pinner,
    )

    result = _run(
        RunState(run_id="derive-after-intake", repo_url="https://example.invalid/repo"),
        context,
    )

    up_commands = [
        call[2]
        for call in provider.calls
        if call[0] == "exec"
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
    ]
    assert pinner_calls == 1
    assert result.run.commit_sha == "c" * 40
    assert up_commands[1][:2] == ("env", "PINNED_TOKEN=repotrial-synthetic-value")
    assert result.run.journeys == []
