import asyncio
import base64
import binascii
import hashlib
import json
import os
import posixpath
import re
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeVar

import pytest
from fixture_harness import parse_fixture_materialization
from langgraph.errors import NodeCancelledError
from langgraph.runtime import Runtime
from pydantic import BaseModel
from ruamel.yaml import YAML

from repotrial.agent import graph as graph_module
from repotrial.agent.graph import ainvoke_run, aresume_run, build_run_graph
from repotrial.agent.state import GraphContext, GraphState
from repotrial.compose.compatibility import CompatibilityError
from repotrial.compose.mutations import apply_mutation
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    Journey,
    JourneyAssertion,
    JourneyStep,
    Mutation,
    PinnedRepo,
    RepoRef,
    RunState,
)
from repotrial.hardening.policy import MutationPolicyDecision
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import (
    ExecResult,
    RuntimeImagePlan,
    RuntimeTemplateAudit,
    RuntimeTemplateIdentity,
    SandboxFailureEvidence,
    SandboxProvider,
)
from repotrial.sandbox.docker_sbx import DockerSbxError
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.sandbox.lifecycle import CleanupError
from repotrial.trial import compatibility as _compatibility
from repotrial.trial import startup_inputs as _startup_inputs
from repotrial.trial.image_template import ImageTemplateError
from repotrial.trial.journey_artifact import JourneyArtifactError

ModelT = TypeVar("ModelT", bound=BaseModel)
_GRAPH_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(.*)\Z")
_GRAPH_ALLOWED_ENV_KEYS = frozenset(
    {
        "APP_DECLARED_TOKEN",
        "APP_MODE",
        "APP_REQUIRED_TOKEN",
        "PINNED_TOKEN",
        "PUBLIC_DSN",
        "WAKAPI_DB_PASSWORD",
    }
)
_RUNTIME_TEMPLATE_ALLOWED_ENV_PREFIXES: tuple[tuple[str, ...], ...] = (
    (),
    ("env", "-u", "APP_MODE"),
)


def _is_allowed_runtime_template_command(
    snapshot: tuple[str, ...], command: tuple[str, ...]
) -> bool:
    return any(
        snapshot == (*prefix, *command)
        for prefix in _RUNTIME_TEMPLATE_ALLOWED_ENV_PREFIXES
    )


_GRAPH_COMPOSE_ROUTES = {
    ("config", "--format", "json"): "config_format",
    ("config", "--services"): "config_services",
    ("config", "--quiet"): "config_quiet",
    ("up", "-d", "--wait", "--wait-timeout", "60"): "up",
    (
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "60",
        "--no-build",
        "--no-recreate",
    ): "up",
    ("ps", "--all", "--format", "json"): "boot_ps",
    (
        "ps",
        "--all",
        "--no-trunc",
        "--orphans=false",
        "--format",
        "json",
    ): "observer_ps",
    ("logs", "--no-color", "--tail", "200"): "logs",
}


def _graph_safe_relative_path(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and posixpath.normpath(value) == value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


def _graph_compose_path(value: str) -> bool:
    return value == "/tmp/repotrial-runtime-image.overlay.json" or (
        _graph_safe_relative_path(value)
    )


def _split_graph_env_prefix(
    snapshot: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if not snapshot or snapshot[0] != "env":
        return (), snapshot
    index = 1
    seen: set[str] = set()
    while index < len(snapshot) and snapshot[index] == "-u":
        if index + 1 >= len(snapshot):
            return None
        key = snapshot[index + 1]
        if key not in _GRAPH_ALLOWED_ENV_KEYS or key in seen:
            return None
        seen.add(key)
        index += 2
    while index < len(snapshot):
        assignment = snapshot[index]
        match = _GRAPH_ENV_ASSIGNMENT.fullmatch(assignment)
        if match is None:
            break
        key, _ = assignment.split("=", 1)
        if key not in _GRAPH_ALLOWED_ENV_KEYS or key in seen:
            return None
        seen.add(key)
        index += 1
    if index == 1 or index >= len(snapshot):
        return None
    return snapshot[:index], snapshot[index:]


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


def _graph_experiment_argv(
    payload: bytes,
    *,
    adapter_script: str | None = None,
) -> list[str]:
    return [
        "sh",
        "-eu",
        "-c",
        _compatibility._EXPERIMENT_ADAPTER_SCRIPT
        if adapter_script is None
        else adapter_script,
        "repotrial-experiment-overlay",
        _compatibility._EXPERIMENT_RELATIVE_PATH,
        hashlib.sha256(payload).hexdigest(),
        base64.b64encode(payload).decode("ascii"),
    ]


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
        compatibility_mismatch_roles: set[str] | None = None,
        compatibility_swap_roles: set[str] | None = None,
    ) -> None:
        super().__init__(ports={} if host_port is None else {8080: host_port})
        self.baseline_boots = list(baseline_boots or [(True, "")])
        self.candidate_healthy = candidate_healthy
        self.candidate_publish = candidate_publish
        self.startup_adapter_result = startup_adapter_result or ExecResult(
            exit_code=0, stdout="root=/workspace\nmode=600\n", stderr=""
        )
        self.compatibility_mismatch_roles = set(compatibility_mismatch_roles or ())
        self.compatibility_swap_roles = set(compatibility_swap_roles or ())
        self._healthy: dict[str, bool] = {}
        self._logs: dict[str, str] = {}
        self._roles: dict[str, str] = {}
        self._workspaces: dict[str, Path] = {}
        self._experiment_expected_sha256: dict[str, str] = {}
        self._accepted_expected_sha256: dict[str, dict[str, str]] = {}
        self._compatibility_guest_files: dict[str, bytes] = {}
        self._experiment_guest_files: dict[str, bytes] = {}
        self._accepted_guest_files: dict[str, dict[str, bytes]] = {}

    async def create(self, workspace: Path, name: str) -> str:
        sandbox_id = await super().create(workspace, name)
        role = "baseline" if name.startswith("repotrial-baseline-") else "candidate"
        self._roles[sandbox_id] = role
        self._workspaces[sandbox_id] = workspace
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
        materialization = self._guest_materialization(sandbox_id, snapshot)
        if materialization is not None:
            return materialization
        if self._is_startup_input(snapshot):
            return self.startup_adapter_result
        if len(snapshot) == 3 and snapshot[:2] == ("sha256sum", "--"):
            relative_path = snapshot[2]
            if (
                relative_path == ".repotrial-overlays/compatibility.overlay.yaml"
                and self._roles[sandbox_id] in self.compatibility_mismatch_roles
            ):
                digest = "0" * 64
            else:
                if relative_path == _compatibility._COMPATIBILITY_RELATIVE_PATH:
                    try:
                        content = self._compatibility_guest_files[sandbox_id]
                    except KeyError as error:
                        raise AssertionError(
                            "compatibility guest target was not materialized"
                        ) from error
                elif relative_path == _compatibility._EXPERIMENT_RELATIVE_PATH:
                    try:
                        content = self._experiment_guest_files[sandbox_id]
                    except KeyError as error:
                        raise AssertionError(
                            "experiment guest target was not materialized"
                        ) from error
                elif relative_path.startswith(".repotrial-accepted/"):
                    try:
                        content = self._accepted_guest_files[sandbox_id][relative_path]
                    except KeyError as error:
                        raise AssertionError(
                            "accepted guest target was not materialized"
                        ) from error
                else:
                    target = self._workspaces[sandbox_id] / relative_path
                    if not target.is_file() or target.is_symlink():
                        raise AssertionError(
                            "guest verifier target was not materialized"
                        )
                    content = target.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if (
                    relative_path == ".repotrial-overlays/experiment.overlay.yaml"
                    and self._experiment_expected_sha256.get(sandbox_id) != digest
                ):
                    raise AssertionError("experiment verifier hash mismatch")
                if (
                    relative_path.startswith(".repotrial-accepted/")
                    and self._accepted_expected_sha256.get(sandbox_id, {}).get(
                        relative_path
                    )
                    != digest
                ):
                    raise AssertionError("accepted verifier hash mismatch")
            return ExecResult(
                exit_code=0,
                stdout=f"{digest}  {relative_path}\n",
                stderr="",
            )
        route = self._route_compose_command(sandbox_id, snapshot)
        if route is not None:
            route_name, _compose_files, _prefix = route
            if route_name == "config_format":
                image = "example/web:1"
                runtime_plan = getattr(self, "_runtime_image_plan", None)
                if runtime_plan is not None:
                    image = runtime_plan.bindings[0].alias
                return ExecResult(
                    exit_code=0,
                    stdout=json.dumps({"services": {"web": {"image": image}}}),
                    stderr="",
                )
            if route_name == "config_services":
                return ExecResult(exit_code=0, stdout="web\n", stderr="")
            if route_name == "config_images":
                image = "docker.io/example/web:1"
                runtime_plan = getattr(self, "_runtime_image_plan", None)
                if runtime_plan is not None:
                    image = runtime_plan.bindings[0].alias
                return ExecResult(exit_code=0, stdout=f"{image}\n", stderr="")
            healthy = self._healthy[sandbox_id]
            if route_name == "config_quiet":
                return ExecResult(
                    exit_code=0 if healthy else 1,
                    stdout="",
                    stderr="" if healthy else self._logs[sandbox_id],
                )
            if route_name == "up":
                return ExecResult(exit_code=0 if healthy else 1, stdout="", stderr="")
            if route_name == "boot_ps":
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
            if route_name == "logs":
                return ExecResult(
                    exit_code=0,
                    stdout=self._logs[sandbox_id],
                    stderr="",
                )
            if route_name == "observer_ps":
                return ExecResult(exit_code=0, stdout="", stderr="")
        raise AssertionError(f"unexpected provider command: {snapshot!r}")

    def _route_compose_command(
        self, sandbox_id: str, snapshot: tuple[str, ...]
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
        split = _split_graph_env_prefix(snapshot)
        if split is None:
            return None
        prefix, command = split
        if command[:2] != ("docker", "compose"):
            return None
        index = 2
        if command[index : index + 2] == ("--project-directory", "."):
            index += 2
        compose_files: list[str] = []
        while index < len(command) and command[index] == "-f":
            if index + 1 >= len(command) or not _graph_compose_path(command[index + 1]):
                return None
            compose_files.append(command[index + 1])
            index += 2
        if len(compose_files) not in {1, 2, 3} or len(compose_files) != len(
            set(compose_files)
        ):
            return None
        command_tail = tuple(command[index:])
        route_name = _GRAPH_COMPOSE_ROUTES.get(command_tail)
        if (
            route_name is None
            and command_tail[:2] == ("config", "--images")
            and (len(command_tail) == 2 or command_tail[2] == "web")
        ):
            route_name = "config_images"
        if route_name is None:
            return None
        files = tuple(compose_files)
        for relative_path in files:
            if relative_path.startswith(".repotrial-accepted/") and (
                _compatibility._ACCEPTED_COMPOSE_PATTERN.fullmatch(relative_path)
                is None
                or relative_path not in self._accepted_guest_files.get(sandbox_id, {})
            ):
                raise AssertionError("accepted compose was not materialized in guest")
        return route_name, files, prefix

    @staticmethod
    def _is_startup_input(snapshot: tuple[str, ...]) -> bool:
        split = _split_graph_env_prefix(snapshot)
        if split is None:
            return False
        _prefix, command = split
        if len(command) != 10 or command[:5] != (
            "sh",
            "-eu",
            "-c",
            _startup_inputs._ADAPTER_SCRIPT,
            "repotrial-startup-input",
        ):
            return False
        source_path, target_path, source_sha256, output_sha256, payload = command[5:]
        if source_path != ".env.sample" or target_path != ".env":
            return False
        if (
            _compatibility._SHA256_PATTERN.fullmatch(source_sha256) is None
            or _compatibility._SHA256_PATTERN.fullmatch(output_sha256) is None
        ):
            return False
        if payload == _startup_inputs._EMPTY_PAYLOAD_SENTINEL:
            decoded = b""
        else:
            if len(payload) > _startup_inputs._MAX_PAYLOAD_BYTES:
                return False
            try:
                decoded = base64.b64decode(payload, validate=True)
            except (UnicodeError, ValueError, binascii.Error):
                return False
        return hashlib.sha256(decoded).hexdigest() == output_sha256

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        if self._roles[sandbox_id] == "candidate" and not self.candidate_publish:
            self.calls.append(("publish_port", sandbox_id, container_port))
            raise KeyError("candidate port is unavailable")
        return await super().publish_port(sandbox_id, container_port)

    async def destroy(self, sandbox_id: str) -> None:
        self._compatibility_guest_files.pop(sandbox_id, None)
        self._experiment_guest_files.pop(sandbox_id, None)
        self._experiment_expected_sha256.pop(sandbox_id, None)
        self._accepted_expected_sha256.pop(sandbox_id, None)
        self._accepted_guest_files.pop(sandbox_id, None)
        await super().destroy(sandbox_id)

    def _guest_materialization(
        self, sandbox_id: str, snapshot: tuple[str, ...]
    ) -> ExecResult | None:
        parsed = parse_fixture_materialization(snapshot)
        if parsed is None:
            return None
        if isinstance(parsed, int):
            return ExecResult(exit_code=parsed, stdout="", stderr="")
        command_name, relative_path, expected_sha256, content = parsed

        if command_name == "repotrial-compatibility-overlay":
            if sandbox_id in self._compatibility_guest_files:
                return ExecResult(exit_code=24, stdout="", stderr="")
            if self._roles[sandbox_id] in self.compatibility_swap_roles:
                self._compatibility_guest_files[sandbox_id] = b"guest-clone-tampered"
            else:
                self._compatibility_guest_files[sandbox_id] = content
        elif command_name == "repotrial-experiment-overlay":
            if sandbox_id in self._experiment_guest_files:
                return ExecResult(exit_code=24, stdout="", stderr="")
            self._experiment_guest_files[sandbox_id] = content
            self._experiment_expected_sha256[sandbox_id] = expected_sha256
        else:
            guest_files = self._accepted_guest_files.setdefault(sandbox_id, {})
            if relative_path in guest_files:
                return ExecResult(exit_code=24, stdout="", stderr="")
            guest_files[relative_path] = content
            self._accepted_expected_sha256.setdefault(sandbox_id, {})[relative_path] = (
                expected_sha256
            )
        return ExecResult(
            exit_code=0,
            stdout=(
                "root=/workspace\n"
                f"path={relative_path}\n"
                "mode=600\n"
                f"sha256={expected_sha256}\n"
            ),
            stderr="",
        )


def test_graph_fixture_provider_rejects_marker_with_untrusted_script(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = b"services:\n  web:\n    image: example/web:1\n"

    async def exercise() -> None:
        sandbox_id = await provider.create(workspace, "candidate-marker")
        try:
            with pytest.raises(AssertionError, match="unexpected provider command"):
                await provider.exec(
                    sandbox_id,
                    _graph_experiment_argv(payload, adapter_script="printf marker"),
                )
        finally:
            await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_graph_fixture_provider_keeps_experiment_guest_state_per_sandbox(
    tmp_path: Path,
) -> None:
    provider = GraphProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.yaml"
    target = workspace / _compatibility._EXPERIMENT_RELATIVE_PATH
    target.parent.mkdir()
    target.symlink_to(outside)
    first_payload = b"first graph candidate"
    second_payload = b"second graph candidate"

    async def exercise() -> tuple[ExecResult, ExecResult, ExecResult, ExecResult]:
        first = await provider.create(workspace, "candidate-first")
        second = await provider.create(workspace, "candidate-second")
        try:
            first_result = await provider.exec(
                first, _graph_experiment_argv(first_payload)
            )
            second_result = await provider.exec(
                second, _graph_experiment_argv(second_payload)
            )
            first_verify = await provider.exec(
                first, ["sha256sum", "--", _compatibility._EXPERIMENT_RELATIVE_PATH]
            )
            second_verify = await provider.exec(
                second, ["sha256sum", "--", _compatibility._EXPERIMENT_RELATIVE_PATH]
            )
            assert second_verify.stdout == (
                f"{hashlib.sha256(second_payload).hexdigest()}  "
                f"{_compatibility._EXPERIMENT_RELATIVE_PATH}\n"
            )
            await provider.destroy(first)
            second_after_destroy = await provider.exec(
                second, ["sha256sum", "--", _compatibility._EXPERIMENT_RELATIVE_PATH]
            )
            return first_result, second_result, first_verify, second_after_destroy
        finally:
            if second in provider._active_sandboxes:
                await provider.destroy(second)

    first_result, second_result, first_verify, second_after_destroy = asyncio.run(
        exercise()
    )

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert first_verify.stdout == (
        f"{hashlib.sha256(first_payload).hexdigest()}  "
        f"{_compatibility._EXPERIMENT_RELATIVE_PATH}\n"
    )
    assert second_after_destroy.stdout == (
        f"{hashlib.sha256(second_payload).hexdigest()}  "
        f"{_compatibility._EXPERIMENT_RELATIVE_PATH}\n"
    )
    assert target.is_symlink()
    assert not outside.exists()


class PreflightObservationProvider(GraphProvider):
    def __init__(self, error: str) -> None:
        super().__init__(baseline_boots=[(True, "")])
        self.error = error
        self.config_calls = 0
        self.observation_argv: tuple[str, ...] | None = None

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        snapshot = tuple(argv)
        if snapshot[-2:] == ("config", "--quiet"):
            self.config_calls += 1
            self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
            if self.config_calls == 1:
                return ExecResult(exit_code=1, stdout="", stderr=self.error)
            return ExecResult(exit_code=0, stdout="", stderr="")
        if snapshot[-6:] == (
            "ps",
            "--all",
            "--no-trunc",
            "--orphans=false",
            "--format",
            "json",
        ):
            self.observation_argv = snapshot
            assert snapshot[:2] == (
                "env",
                "WAKAPI_DB_PASSWORD=repotrial-synthetic-value",
            )
        return await super().exec(sandbox_id, argv, timeout_s)


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


def test_boot_recovers_quoted_compose_secret_environment_and_deduplicates_keys(
    tmp_path: Path,
) -> None:
    provider = GraphProvider(
        baseline_boots=[
            (
                False,
                (
                    'environment variable "WAKAPI_DB_PASSWORD" required by secret '
                    '"workspace_db_password" is not set'
                ),
            ),
            (
                False,
                (
                    'environment variable "WAKAPI_DB_PASSWORD" required by secret '
                    '"workspace_db_password" is not set'
                ),
            ),
            (True, ""),
        ]
    )
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(())
        + "secrets:\n"
        + "  workspace_db_password:\n"
        + "    environment: WAKAPI_DB_PASSWORD\n",
        encoding="utf-8",
    )
    context = replace(context, allowed_env_keys=frozenset())

    result = _run(_state(source.parent), context)

    up_commands = [
        call[2]
        for call in provider.calls
        if call[0] == "exec"
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
    ]
    assert len(up_commands) == 3
    assert up_commands[1][:2] == (
        "env",
        "WAKAPI_DB_PASSWORD=repotrial-synthetic-value",
    )
    assert up_commands[2][:2] == (
        "env",
        "WAKAPI_DB_PASSWORD=repotrial-synthetic-value",
    )
    assert result.recovery_env == {"WAKAPI_DB_PASSWORD": "repotrial-synthetic-value"}
    assert result.run.recovery_env_keys == ["WAKAPI_DB_PASSWORD"]


def test_declared_secret_preflight_reuses_one_sandbox_and_projects_key_names(
    tmp_path: Path,
) -> None:
    error = (
        'environment variable "WAKAPI_DB_PASSWORD" required by secret '
        '"workspace_db_password" is not set'
    )
    provider = GraphProvider(baseline_boots=[(False, error), (True, "")])
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(())
        + "secrets:\n"
        + "  workspace_db_password:\n"
        + "    environment: WAKAPI_DB_PASSWORD\n",
        encoding="utf-8",
    )
    context = replace(context, allowed_env_keys=frozenset())

    result = _run(_state(source.parent), context)

    baseline_sandboxes = [
        call[2]
        for call in provider.calls
        if call[0] == "create" and str(call[2]).startswith("repotrial-baseline-")
    ]
    compose_calls = [
        call
        for call in provider.calls
        if call[0] == "exec" and "docker" in call[2] and "compose" in call[2]
    ]
    assert len(baseline_sandboxes) == 2
    assert result.recovery_env == {"WAKAPI_DB_PASSWORD": "repotrial-synthetic-value"}
    assert result.run.recovery_env_keys == ["WAKAPI_DB_PASSWORD"]
    first_config = next(
        call for call in compose_calls if call[2][-2:] == ("config", "--quiet")
    )
    assert first_config[1] == "sandbox-1"
    assert first_config[1] != "sandbox-2"
    first_up = next(
        call[2]
        for call in compose_calls
        if call[1] == first_config[1]
        and call[2][-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
    )
    assert first_up[:2] == (
        "env",
        "WAKAPI_DB_PASSWORD=repotrial-synthetic-value",
    )


def test_boot_applies_preflight_env_before_baseline_observation(
    tmp_path: Path,
) -> None:
    error = (
        'environment variable "WAKAPI_DB_PASSWORD" required by secret '
        '"workspace_db_password" is not set'
    )
    provider = PreflightObservationProvider(error)
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(())
        + "secrets:\n"
        + "  workspace_db_password:\n"
        + "    environment: WAKAPI_DB_PASSWORD\n",
        encoding="utf-8",
    )
    context = replace(context, allowed_env_keys=frozenset())

    result = _run(_state(source.parent), context)

    assert result.boot_verdict is Verdict.PASS
    assert provider.config_calls == 1
    assert provider.observation_argv is not None
    assert result.recovery_env == {"WAKAPI_DB_PASSWORD": "repotrial-synthetic-value"}


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
        (context.overlay_dir / "hardened.overlay.yaml").write_text(
            "services: {}\n", encoding="utf-8"
        )
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
    assert result.run.hardened_overlay_provenance is not None
    hardened_reference = (
        result.run.hardened_overlay_provenance.overlay_relative_reference
    )
    overlay_artifacts = [
        context.workspace / path
        for path in result.run.artifacts
        if path.endswith(".overlay.yaml") and path != hardened_reference
    ]
    assert len(overlay_artifacts) == 2
    assert all(path.is_file() for path in overlay_artifacts)
    assert len(_candidate_create_calls(provider)) == 2
    assert not any(
        record.reason == "parent_hash_mismatch" for record in result.run.experiments
    )
    assert result.run.baseline_compose_path == "compose.yaml"
    final_overlay = context.workspace / hardened_reference
    assert final_overlay.is_file()
    assert (
        result.run.baseline_config_hash.removeprefix("sha256:")[:16]
        in hardened_reference
    )
    assert (
        result.run.current_config_hash.removeprefix("sha256:")[:16]
        in hardened_reference
    )


def test_graph_terminal_overlay_replays_two_keeps_and_one_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions = iter(
        [
            MutationPolicyDecision(
                mutation=Mutation(
                    mutation_id="keep-user",
                    type=MutationType.SET_NON_ROOT,
                    service="web",
                ),
                stop_reason=None,
            ),
            MutationPolicyDecision(
                mutation=Mutation(
                    mutation_id="keep-worker-caps",
                    type=MutationType.DROP_ALL_CAPS,
                    service="worker",
                ),
                stop_reason=None,
            ),
            MutationPolicyDecision(
                mutation=Mutation(
                    mutation_id="rollback-tmpfs",
                    type=MutationType.ADD_TMPFS,
                    service="web",
                ),
                stop_reason=None,
            ),
            MutationPolicyDecision(mutation=None, stop_reason="done"),
        ]
    )
    monkeypatch.setattr(graph_module, "propose_mutation", lambda state: next(decisions))

    class SequenceProvider(GraphProvider):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.candidate_number = 0

        async def create(self, workspace: Path, name: str) -> str:
            sandbox_id = await super().create(workspace, name)
            if name.startswith("repotrial-candidate-"):
                self.candidate_number += 1
                self._healthy[sandbox_id] = self.candidate_number != 3
            return sandbox_id

    with _healthy_server() as (_, port):
        provider = SequenceProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user", "cap_add"))
        source.write_text(
            source.read_text(encoding="utf-8") + "  worker:\n    image: busybox\n",
            encoding="utf-8",
        )
        result = _run(_state(source.parent), context)

    assert [record.verdict for record in result.run.experiments] == [
        ExperimentVerdict.KEEP,
        ExperimentVerdict.KEEP,
        ExperimentVerdict.ROLLBACK,
    ]
    provenance = result.run.hardened_overlay_provenance
    assert provenance is not None
    overlay = load_compose(context.workspace / provenance.overlay_relative_reference)
    web_overlay = overlay["services"]["web"]
    worker_overlay = overlay["services"]["worker"]
    assert "user" in web_overlay
    assert "cap_drop" in worker_overlay
    assert "tmpfs" not in web_overlay
    assert "tmpfs" not in worker_overlay
    assert result.run.artifacts.count(provenance.overlay_relative_reference) == 1


def test_hardened_overlay_replay_rejects_tampered_baseline_source(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        result = _run(_state(source.parent), context)

    source.write_text(
        source.read_text(encoding="utf-8") + "\nname: tampered\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="baseline compose hash mismatch"):
        graph_module._materialize_hardened_overlay(result.run, context)


def _terminal_keep_state(tmp_path: Path) -> tuple[GraphContext, RunState]:
    provider = GraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    baseline = load_compose(source)
    mutation = Mutation(
        mutation_id="keep-user",
        type=MutationType.SET_NON_ROOT,
        service="web",
    )
    candidate = apply_mutation(baseline, mutation)
    baseline_hash = _compose_hash(source)
    candidate_material = canonical_compose_json(candidate).encode("utf-8")
    candidate_hash = "sha256:" + hashlib.sha256(candidate_material).hexdigest()
    accepted = context.accepted_compose_dir / "accepted-final.compose.yaml"
    _write_compose(accepted, candidate)
    record = ExperimentRecord(
        experiment_id=mutation.mutation_id,
        parent_config_hash=baseline_hash,
        candidate_config_hash=candidate_hash,
        mutation=mutation,
        boot=Verdict.PASS,
        journeys=[],
        verdict=ExperimentVerdict.KEEP,
        reason="passed",
    )
    state = _state(source.parent, run_id="terminal-boundary").model_copy(
        update={
            "compose_path": accepted.relative_to(context.workspace).as_posix(),
            "baseline_compose_path": source.name,
            "baseline_config_hash": baseline_hash,
            "current_config_hash": candidate_hash,
            "experiments": [record],
            "stop_reason": "terminal-stop",
        }
    )
    return context, state


@pytest.mark.parametrize(
    "failure",
    [
        "missing_baseline",
        "final_hash_mismatch",
        "overlay_symlink",
        "parent_hash",
        "candidate_hash",
    ],
)
def test_terminal_report_hook_fails_closed_without_artifact_registration(
    tmp_path: Path, failure: str
) -> None:
    context, state = _terminal_keep_state(tmp_path)
    if failure == "missing_baseline":
        state.baseline_compose_path = None
    elif failure == "final_hash_mismatch":
        state.current_config_hash = "sha256:" + "0" * 64
    elif failure == "parent_hash":
        state.experiments[0].parent_config_hash = "sha256:" + "0" * 64
    elif failure == "candidate_hash":
        state.experiments[0].candidate_config_hash = "sha256:" + "0" * 64
    elif failure == "overlay_symlink":
        initial = graph_module._report_or_next(
            GraphState(run=state), Runtime(context=context)
        )["run"]
        assert isinstance(initial, RunState)
        assert initial.hardened_overlay_provenance is not None
        state = initial
        target = (
            context.workspace
            / state.hardened_overlay_provenance.overlay_relative_reference
        )
        target.unlink()
        target.symlink_to(context.workspace / state.compose_path)

    update = graph_module._report_or_next(
        GraphState(run=state), Runtime(context=context)
    )
    returned = update["run"]
    assert isinstance(returned, RunState)
    assert returned.hardened_overlay_provenance is None
    assert returned.stop_reason == "terminal-stop"
    assert returned.artifacts == state.artifacts
    report_dir = tmp_path / "report"
    from repotrial.report.render import render_trial_report

    report = json.loads(
        render_trial_report(returned, report_dir).json_path.read_text(encoding="utf-8")
    )
    assert report["artifacts"]["hardened_overlay"]["status"] == "unavailable"


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


def test_host_compatibility_reader_rejects_lstat_to_open_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    overlay_dir = workspace / ".repotrial-overlays"
    workspace.mkdir()
    overlay_dir.mkdir()
    artifact = overlay_dir / "compatibility.overlay.yaml"
    artifact.write_bytes(b"safe")
    original_lstat = Path.lstat
    swapped = False

    def racing_lstat(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == artifact and not swapped:
            swapped = True
            artifact.unlink()
            artifact.write_bytes(b"swapped")
        return result

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    with pytest.raises(CompatibilityError) as error:
        graph_module._read_compatibility_artifact(
            artifact, workspace, overlay_dir, max_bytes=4
        )

    assert error.value.reason == "artifact_changed"


def test_host_compatibility_reader_rechecks_identity_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    overlay_dir = workspace / ".repotrial-overlays"
    workspace.mkdir()
    overlay_dir.mkdir()
    artifact = overlay_dir / "compatibility.overlay.yaml"
    artifact.write_bytes(b"safe")
    original_read = os.read
    swapped = False

    def racing_read(fd: int, count: int) -> bytes:
        nonlocal swapped
        data = original_read(fd, count)
        if not swapped:
            swapped = True
            artifact.unlink()
            artifact.write_bytes(b"swapped")
        return data

    monkeypatch.setattr(graph_module.os, "read", racing_read)
    with pytest.raises(CompatibilityError) as error:
        graph_module._read_compatibility_artifact(
            artifact, workspace, overlay_dir, max_bytes=4
        )

    assert error.value.reason == "artifact_changed"


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [("oversize", "artifact_oversize"), ("fifo", "artifact_not_regular")],
)
def test_host_compatibility_reader_bounds_and_rejects_non_regular_files(
    tmp_path: Path, kind: str, expected_reason: str
) -> None:
    workspace = tmp_path / "workspace"
    overlay_dir = workspace / ".repotrial-overlays"
    workspace.mkdir()
    overlay_dir.mkdir()
    artifact = overlay_dir / "compatibility.overlay.yaml"
    if kind == "oversize":
        artifact.write_bytes(b"safe!")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO support is unavailable")
        os.mkfifo(artifact)

    with pytest.raises(CompatibilityError) as error:
        graph_module._read_compatibility_artifact(
            artifact, workspace, overlay_dir, max_bytes=4
        )

    assert error.value.reason == expected_reason


def test_baseline_guest_compatibility_is_verified_before_workload_compose(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider)
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        result = _run(_state(source.parent, run_id="guest-order"), context)

    baseline_id = next(
        sandbox_id for sandbox_id, role in provider._roles.items() if role == "baseline"
    )
    baseline_calls = [
        call for call in provider.calls if call[0] == "exec" and call[1] == baseline_id
    ]
    verify_index = next(
        index for index, call in enumerate(baseline_calls) if call[2][0] == "sha256sum"
    )
    compose_index = next(
        index
        for index, call in enumerate(baseline_calls)
        if "docker" in call[2] and "compose" in call[2]
    )
    assert verify_index < compose_index
    assert result.run.stop_reason == "no_remaining_mutations"


def test_baseline_materializes_compatibility_before_guest_hash_and_compose(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, journeys=[])
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        _run(_state(source.parent, run_id="materialize-baseline"), context)

    baseline_id = next(
        sandbox_id for sandbox_id, role in provider._roles.items() if role == "baseline"
    )
    baseline_calls = [
        call for call in provider.calls if call[0] == "exec" and call[1] == baseline_id
    ]
    materialize_index = next(
        index
        for index, call in enumerate(baseline_calls)
        if "repotrial-compatibility-overlay" in call[2]
    )
    verify_index = next(
        index for index, call in enumerate(baseline_calls) if call[2][0] == "sha256sum"
    )
    compose_index = next(
        index
        for index, call in enumerate(baseline_calls)
        if "docker" in call[2] and "compose" in call[2]
    )
    assert materialize_index < verify_index < compose_index


def test_candidate_materializes_compatibility_before_guest_hash_and_compose(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        source.write_text(
            _compose_text(("root_user",)).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        _run(_state(source.parent, run_id="materialize-candidate"), context)

    candidate_id = next(
        sandbox_id
        for sandbox_id, role in provider._roles.items()
        if role == "candidate"
    )
    candidate_calls = [
        call for call in provider.calls if call[0] == "exec" and call[1] == candidate_id
    ]
    materialize_index = next(
        index
        for index, call in enumerate(candidate_calls)
        if "repotrial-compatibility-overlay" in call[2]
    )
    compatibility_verify_index = next(
        index for index, call in enumerate(candidate_calls) if call[2][0] == "sha256sum"
    )
    experiment_materialize_index = next(
        index
        for index, call in enumerate(candidate_calls)
        if "repotrial-experiment-overlay" in call[2]
    )
    experiment_verify_index = next(
        index
        for index, call in enumerate(candidate_calls)
        if call[2][:3]
        == ("sha256sum", "--", ".repotrial-overlays/experiment.overlay.yaml")
    )
    compose_index = next(
        index
        for index, call in enumerate(candidate_calls)
        if "docker" in call[2] and "compose" in call[2]
    )
    assert (
        materialize_index
        < compatibility_verify_index
        < experiment_materialize_index
        < experiment_verify_index
        < compose_index
    )


def test_baseline_guest_compatibility_mismatch_cleans_and_reports_before_compose(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(
            host_port=port, compatibility_mismatch_roles={"baseline"}
        )
        context, source = _context(tmp_path, provider, journeys=[])
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        result = _run(_state(source.parent, run_id="guest-baseline-mismatch"), context)

    assert result.run.stop_reason == "compatibility:guest_hash_mismatch"
    assert result.stage_history[-2:] == ["boot", "report_or_next"]
    baseline_id = next(
        sandbox_id for sandbox_id, role in provider._roles.items() if role == "baseline"
    )
    assert any(
        call[0] == "destroy" and call[1] == baseline_id for call in provider.calls
    )
    assert not any(
        call[0] == "exec"
        and call[1] == baseline_id
        and "docker" in call[2]
        and "compose" in call[2]
        for call in provider.calls
    )


def test_guest_compatibility_detects_verify_to_clone_swap_before_compose(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port, compatibility_swap_roles={"baseline"})
        context, source = _context(tmp_path, provider, journeys=[])
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        result = _run(_state(source.parent, run_id="guest-clone-swap"), context)

    assert result.run.stop_reason == "compatibility:guest_hash_mismatch"
    baseline_id = next(
        sandbox_id for sandbox_id, role in provider._roles.items() if role == "baseline"
    )
    assert any(
        call[0] == "destroy" and call[1] == baseline_id for call in provider.calls
    )
    assert not any(
        call[0] == "exec"
        and call[1] == baseline_id
        and "docker" in call[2]
        and "compose" in call[2]
        for call in provider.calls
    )


def test_candidate_guest_compatibility_mismatch_cleans_and_reports_before_compose(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(
            host_port=port, compatibility_mismatch_roles={"candidate"}
        )
        context, source = _context(tmp_path, provider, risks=("root_user",))
        source.write_text(
            _compose_text(("root_user",)).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        result = _run(_state(source.parent, run_id="guest-candidate-mismatch"), context)

    assert result.run.stop_reason == "compatibility:guest_hash_mismatch"
    assert result.stage_history[-2:] == ["experiment", "report_or_next"]
    candidate_id = next(
        sandbox_id
        for sandbox_id, role in provider._roles.items()
        if role == "candidate"
    )
    candidate_calls = [
        call for call in provider.calls if call[0] == "exec" and call[1] == candidate_id
    ]
    assert any(
        call[0] == "destroy" and call[1] == candidate_id for call in provider.calls
    )
    assert not any(
        "docker" in call[2] and "compose" in call[2] for call in candidate_calls
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
            )
            + "    env_file:\n      - .env\n",
            encoding="utf-8",
        )
        (context.workspace / ".env.sample").write_text(
            "APP_REQUIRED_TOKEN=sample\n", encoding="utf-8"
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
    candidate_id = next(
        sandbox_id
        for sandbox_id, role in provider._roles.items()
        if role == "candidate"
    )
    candidate_exec_calls = [
        call for call in provider.calls if call[0] == "exec" and call[1] == candidate_id
    ]
    guest_verify_index = next(
        index
        for index, call in enumerate(candidate_exec_calls)
        if call[2][0] == "sha256sum"
    )
    first_compose_index = next(
        index
        for index, call in enumerate(candidate_exec_calls)
        if "docker" in call[2] and "compose" in call[2]
    )
    assert guest_verify_index < first_compose_index
    compose_calls = [
        call[2]
        for call in provider.calls
        if call[0] == "exec"
        and call[1] == candidate_id
        and "docker" in call[2]
        and "compose" in call[2]
    ]
    expected_suffixes = {
        "config": ("config", "--format", "json"),
        "up": ("up", "-d", "--wait", "--wait-timeout", "60"),
        "ps": ("ps", "--all", "--format", "json"),
        "logs": ("logs", "--no-color", "--tail", "200"),
        "observation": (
            "ps",
            "--all",
            "--no-trunc",
            "--orphans=false",
            "--format",
            "json",
        ),
    }
    assert len(compose_calls) == len(expected_suffixes)
    for suffix in expected_suffixes.values():
        matching = [argv for argv in compose_calls if argv[-len(suffix) :] == suffix]
        assert len(matching) == 1
        argv = matching[0]
        overlay_files = [
            argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-f"
        ]
        assert len(overlay_files) == 3
        assert overlay_files[:2] == [
            "compose.yaml",
            ".repotrial-overlays/compatibility.overlay.yaml",
        ]
        assert overlay_files[2].endswith(".overlay.yaml")
        assert overlay_files[2] != ".repotrial-overlays/compatibility.overlay.yaml"


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


def test_checkpoint_resume_rejects_absent_to_present_compatibility_plan(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider)
        graph = build_run_graph(interrupt_after=("baseline",))
        state = _state(source.parent, run_id="compatibility-absent-to-present")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.run.compatibility_overlay_path is None

        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "compatibility:plan_changed"
    assert resumed.stage_history[-1] == "report_or_next"
    assert [call for call in provider.calls if call[0] == "create"] == []
    assert not (context.overlay_dir / "compatibility.overlay.yaml").exists()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("absent", "plan_changed"),
        ("different", "plan_changed"),
        ("port", "plan_changed"),
        ("rejected", "ambiguous_loopback_binding"),
    ],
)
def test_checkpoint_resume_rejects_changed_present_compatibility_plan(
    tmp_path: Path, change: str, reason: str
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider)
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )
        graph = build_run_graph(interrupt_after=("baseline",))
        state = _state(source.parent, run_id=f"compatibility-plan-change-{change}")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.run.compatibility_overlay_path is not None
        resumed_context = context
        if change == "absent":
            updated = _compose_text(())
        elif change == "different":
            updated = _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5001:8080'\n",
            )
        elif change == "port":
            updated = source.read_text(encoding="utf-8")
            resumed_context = replace(context, container_port=9090)
        else:
            updated = _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n"
                "    ports:\n"
                "      - '127.0.0.1:5000:8080'\n"
                "      - '[::1]:5001:8080'\n",
            )
        source.write_text(updated, encoding="utf-8")

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=resumed_context))

    assert resumed.run.stop_reason == f"compatibility:{reason}"
    assert resumed.stage_history[-1] == "report_or_next"
    assert [call for call in provider.calls if call[0] == "create"] == []


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("delete", "artifact_missing"),
        ("replace", "artifact_hash_mismatch"),
        ("symlink", "artifact_linked"),
        ("path", "planned_artifact_mismatch"),
    ],
)
def test_candidate_checkpoint_rejects_tampered_compatibility_before_create(
    tmp_path: Path, tamper: str, reason: str
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        source.write_text(
            _compose_text(("root_user",)).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )
        graph = build_run_graph(interrupt_after=("propose_mutation",))
        state = _state(source.parent, run_id=f"compatibility-candidate-{tamper}")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.pending_mutation is not None
        assert interrupted.run.compatibility_overlay_path is not None
        artifact = context.workspace / interrupted.run.compatibility_overlay_path
        resumed_context = context
        if tamper == "delete":
            artifact.unlink()
        elif tamper == "replace":
            artifact.write_text("services: {}\n", encoding="utf-8")
        elif tamper == "symlink":
            replacement = tmp_path / "replacement.overlay.yaml"
            replacement.write_text("services: {}\n", encoding="utf-8")
            artifact.unlink()
            artifact.symlink_to(replacement)
        else:
            alternate = context.workspace / "alternate-overlays"
            alternate.mkdir()
            resumed_context = replace(context, overlay_dir=alternate)

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=resumed_context))

    assert resumed.run.stop_reason == f"compatibility:{reason}"
    assert resumed.stage_history[-1] == "report_or_next"
    assert _candidate_create_calls(provider) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [("absent", "plan_changed"), ("rejected", "ambiguous_loopback_binding")],
)
def test_candidate_checkpoint_rejects_changed_plan_before_create(
    tmp_path: Path, change: str, reason: str
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        source.write_text(
            _compose_text(("root_user",)).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )
        graph = build_run_graph(interrupt_after=("propose_mutation",))
        state = _state(source.parent, run_id=f"compatibility-candidate-plan-{change}")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.pending_mutation is not None
        if change == "absent":
            updated = _compose_text(("root_user",))
        else:
            updated = _compose_text(("root_user",)).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n"
                "    ports:\n"
                "      - '127.0.0.1:5000:8080'\n"
                "      - '[::1]:5001:8080'\n",
            )
        source.write_text(updated, encoding="utf-8")

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == f"compatibility:{reason}"
    assert resumed.stage_history[-1] == "report_or_next"
    assert _candidate_create_calls(provider) == []


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [("missing", "artifact_missing"), ("symlink", "artifact_linked")],
)
def test_checkpoint_resume_normalizes_compatibility_directory_tamper(
    tmp_path: Path, tamper: str, reason: str
) -> None:
    with _healthy_server() as (_, port):
        provider = GraphProvider(host_port=port)
        context, source = _context(tmp_path, provider)
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n    ports:\n      - '127.0.0.1:5000:8080'\n",
            ),
            encoding="utf-8",
        )
        graph = build_run_graph(interrupt_after=("baseline",))
        state = _state(source.parent, run_id=f"compatibility-directory-{tamper}")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.run.compatibility_overlay_path is not None
        if tamper == "missing":
            (context.workspace / interrupted.run.compatibility_overlay_path).unlink()
            context.overlay_dir.rmdir()
        else:
            replacement = tmp_path / "replacement-overlays"
            context.overlay_dir.rename(replacement)
            context.overlay_dir.symlink_to(replacement, target_is_directory=True)

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == f"compatibility:{reason}"
    assert resumed.stage_history[-1] == "report_or_next"
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


_TEMPLATE_IMAGE_OUTPUT = (
    '{"ID":"sha256:' + "a" * 64 + '","Repository":"example/web",'
    '"Tag":"1","Digest":"<none>"}\n'
)


class RuntimeTemplateGraphProvider(GraphProvider):
    def __init__(
        self,
        *,
        host_port: int | None = None,
        guest_root: str = "/workspace/repo",
        git_root: str | None = None,
        fail_prepare: bool = False,
        fail_warmup_destroy: bool = False,
        cancel_prepare: bool = False,
        finalize_failure: BaseException | None = None,
        prepare_error: BaseException | None = None,
        fail_activate: bool = False,
        wrong_guest_workspace: bool = False,
        prepare_cancellation: asyncio.CancelledError | None = None,
        warmup_destroy_error: BaseException | None = None,
        finalize_audit_identity: RuntimeTemplateIdentity | None = None,
    ) -> None:
        super().__init__(host_port=host_port)
        self.events: list[str] = []
        self.guest_root = guest_root
        self.git_root = guest_root if git_root is None else git_root
        self.fail_prepare = fail_prepare
        self.fail_warmup_destroy = fail_warmup_destroy
        self.cancel_prepare = cancel_prepare
        self.finalize_failure = finalize_failure
        self.prepare_error = prepare_error
        self.fail_activate = fail_activate
        self.wrong_guest_workspace = wrong_guest_workspace
        self.prepare_cancellation = prepare_cancellation
        self.warmup_destroy_error = warmup_destroy_error
        self.finalize_audit_identity = finalize_audit_identity
        self._template_identity: str | None = None
        self._template_runtime_identity: RuntimeTemplateIdentity | None = None
        self._template_audit = RuntimeTemplateAudit(removal_confirmed=True)
        self._warmup_prepared = False
        self.candidate_template_identities: list[str | None] = []
        self._runtime_template_activation_used = False
        self._runtime_template_finalization_confirmed = True
        self._trial_deadline: int | None = None
        self.staged_bundle: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self._runtime_image_plan: RuntimeImagePlan | None = None

    @property
    def supports_runtime_templates(self) -> bool:
        return True

    def expected_image_identity_sha256(self) -> str | None:
        return self._template_identity

    def runtime_template_audit(self) -> RuntimeTemplateAudit:
        return self._template_audit

    async def begin_invocation(self) -> None:
        if not self._runtime_template_finalization_confirmed:
            raise RuntimeError("runtime template finalization is not confirmed")
        if self._template_identity is not None:
            raise RuntimeError("runtime template cleanup is not confirmed")
        if self._active_sandboxes:
            raise RuntimeError("sandbox cleanup is not confirmed")
        self._runtime_template_activation_used = False
        self._template_runtime_identity = None
        self._template_audit = RuntimeTemplateAudit()
        self._trial_deadline = None
        self._runtime_template_finalization_confirmed = False
        self.staged_bundle = None
        self._runtime_image_plan = None

    async def create(self, workspace: Path, name: str) -> str:
        if self._trial_deadline is None:
            self._trial_deadline = 1
        if name.startswith("repotrial-warmup-"):
            self.events.append("warmup_create")
        elif name.startswith("repotrial-baseline-"):
            self.events.append("baseline_create")
        elif name.startswith("repotrial-candidate-"):
            if self._template_identity is None:
                raise AssertionError("candidate create requires active template")
            self.candidate_template_identities.append(self._template_identity)
            self.events.append("candidate_create")
        sandbox_id = await super().create(workspace, name)
        if self._template_runtime_identity is not None and not name.startswith(
            "repotrial-warmup-"
        ):
            self._template_audit = self._template_audit.with_use(sandbox_id)
        if name.startswith("repotrial-warmup-"):
            self._roles[sandbox_id] = "warmup"
        return sandbox_id

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        snapshot = tuple(argv)
        if snapshot[-4:] == (
            "inspect",
            "--format",
            "{{.Id}}",
            "docker.io/example/web:1",
        ):
            self._require_active(sandbox_id)
            self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
            return ExecResult(
                exit_code=0,
                stdout="sha256:" + "a" * 64 + "\n",
                stderr="",
            )
        if "image" in snapshot and "ls" in snapshot:
            self._require_active(sandbox_id)
            self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
            return ExecResult(exit_code=0, stdout=_TEMPLATE_IMAGE_OUTPUT, stderr="")
        if snapshot[-8:] == (
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "60",
            "--pull",
            "never",
            "--no-build",
        ):
            self._require_active(sandbox_id)
            self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
            return ExecResult(exit_code=0, stdout="", stderr="")
        if self._roles.get(sandbox_id) == "warmup":
            if "pull" in snapshot and "--ignore-buildable" in snapshot:
                self._require_active(sandbox_id)
                self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
                if self.cancel_prepare:
                    raise self.prepare_cancellation or asyncio.CancelledError(
                        "warmup cancelled"
                    )
                if self.prepare_error is not None:
                    raise self.prepare_error
                if self.fail_prepare:
                    return ExecResult(exit_code=1, stdout="", stderr="")
                self.events.append("prepare")
                self._warmup_prepared = True
                return ExecResult(exit_code=0, stdout="", stderr="")
            if snapshot[-1:] == ("build",):
                self._require_active(sandbox_id)
                self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
                return ExecResult(exit_code=0, stdout="", stderr="")
            if snapshot[-2:] == ("pwd", "-P"):
                command = snapshot[-2:]
                if not _is_allowed_runtime_template_command(snapshot, command):
                    raise AssertionError(f"unexpected guest-root command: {snapshot!r}")
                self._require_active(sandbox_id)
                self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "/wrong\n"
                        if self.wrong_guest_workspace
                        else f"{self.guest_root}\n"
                    ),
                    stderr="",
                )
            if snapshot[-3:] == ("git", "rev-parse", "--show-toplevel"):
                command = snapshot[-3:]
                if not _is_allowed_runtime_template_command(snapshot, command):
                    raise AssertionError(f"unexpected guest-root command: {snapshot!r}")
                self._require_active(sandbox_id)
                self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
                return ExecResult(
                    exit_code=0,
                    stdout=f"{self.git_root}\n",
                    stderr="",
                )
            if snapshot[-5:] in {
                ("find", self.guest_root, "-mindepth", "1", "-delete"),
                ("find", self.git_root, "-mindepth", "1", "-delete"),
            }:
                command = snapshot[-5:]
                if not _is_allowed_runtime_template_command(snapshot, command):
                    raise AssertionError(f"unexpected guest-root command: {snapshot!r}")
                self._require_active(sandbox_id)
                self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
                return ExecResult(exit_code=0, stdout="", stderr="")
            if snapshot[-6:] in {
                ("find", self.guest_root, "-mindepth", "1", "-print", "-quit"),
                ("find", self.git_root, "-mindepth", "1", "-print", "-quit"),
            }:
                command = snapshot[-6:]
                if not _is_allowed_runtime_template_command(snapshot, command):
                    raise AssertionError(f"unexpected guest-root command: {snapshot!r}")
                self._require_active(sandbox_id)
                self.calls.append(("exec", sandbox_id, snapshot, timeout_s))
                return ExecResult(exit_code=0, stdout="", stderr="")
        return await super().exec(sandbox_id, argv, timeout_s)

    async def stage_runtime_image_bundle(
        self,
        sandbox_id: str,
        image_references: tuple[str, ...],
        image_ids: tuple[str, ...],
        *,
        runtime_image_plan: RuntimeImagePlan | None = None,
    ) -> None:
        self._require_active(sandbox_id)
        if self._roles.get(sandbox_id) != "warmup":
            raise AssertionError("image bundle must stage in warmup sandbox")
        if not self._warmup_prepared:
            raise AssertionError("image bundle staging requires prepared warmup")
        expected = (
            ("docker.io/example/web:1",),
            ("sha256:" + "a" * 64,),
        )
        if (image_references, image_ids) != expected or runtime_image_plan is None:
            raise AssertionError(
                f"unexpected image bundle identity: {image_references!r}, {image_ids!r}"
            )
        if self.staged_bundle is not None:
            raise AssertionError("image bundle staging invoked more than once")
        self.staged_bundle = (image_references, image_ids)
        self._runtime_image_plan = runtime_image_plan

    def runtime_image_plan(self) -> RuntimeImagePlan | None:
        return self._runtime_image_plan

    async def prepare_runtime_image_bindings(self, sandbox_id: str) -> str | None:
        self._require_active(sandbox_id)
        if self._runtime_image_plan is None:
            return None
        return "/tmp/repotrial-runtime-image.overlay.json"

    async def activate_runtime_template(
        self, sandbox_id: str, image_identity_sha256: str
    ) -> None:
        if self._runtime_template_activation_used:
            raise RuntimeError("runtime template activation already invoked")
        self._runtime_template_activation_used = True
        self._require_active(sandbox_id)
        assert self._warmup_prepared
        if self.fail_activate:
            raise ImageTemplateError("template_activation_failed")
        self._template_identity = image_identity_sha256
        self._template_runtime_identity = RuntimeTemplateIdentity(
            repository="docker.io/library/repotrial-runtime",
            tag="a" * 32,
            image_id="a" * 12,
            image_identity_sha256=image_identity_sha256,
        )
        self._template_audit = RuntimeTemplateAudit(
            identity=self._template_runtime_identity
        )
        self.events.append("activate")

    async def destroy(self, sandbox_id: str) -> None:
        if self._roles.get(sandbox_id) == "warmup":
            self.events.append("warmup_destroy")
            if self.fail_warmup_destroy:
                raise self.warmup_destroy_error or RuntimeError("warmup destroy failed")
        await super().destroy(sandbox_id)

    async def finalize_runtime_template(self) -> None:
        if self.finalize_failure is not None:
            raise self.finalize_failure
        if self._template_identity is not None:
            self.events.append("template_remove")
            self._template_identity = None
            identity = self._template_runtime_identity
            if identity is not None:
                self._template_audit = RuntimeTemplateAudit(
                    identity=identity,
                    uses=self._template_audit.uses,
                    removal_confirmed=True,
                )
                self._template_runtime_identity = None
        self._template_audit = RuntimeTemplateAudit(
            identity=self._template_audit.identity,
            uses=self._template_audit.uses,
            removal_confirmed=True,
        )
        if self.finalize_audit_identity is not None:
            self._template_audit = RuntimeTemplateAudit(
                identity=self.finalize_audit_identity,
                removal_confirmed=True,
            )
        self._runtime_template_finalization_confirmed = True


class IncompleteRuntimeTemplateProvider(FakeSandboxProvider):
    @property
    def supports_runtime_templates(self) -> bool:
        return True

    async def begin_invocation(self) -> None:
        return

    async def activate_runtime_template(
        self, sandbox_id: str, image_identity_sha256: str
    ) -> None:
        del sandbox_id, image_identity_sha256


def test_runtime_template_warmup_precedes_baseline_and_finalizes_once(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))

        result = _run(_state(source.parent, run_id="runtime-template-order"), context)

    assert result.run.stop_reason == "no_remaining_mutations"
    assert provider.events == [
        "warmup_create",
        "prepare",
        "activate",
        "warmup_destroy",
        "baseline_create",
        "candidate_create",
        "template_remove",
    ]
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    assert len(evidence) == 1
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert [(row["event"], row["outcome"]) for row in rows] == [
        ("warmup_create", "started"),
        ("warmup_create", "success"),
        ("prepare", "started"),
        ("prepare", "success"),
        ("activate", "success"),
        ("warmup_destroy", "success"),
        ("template_use", "success"),
        ("template_use", "success"),
        ("template_remove", "success"),
    ]


def test_runtime_template_warmup_reuses_startup_input_materialization(
    tmp_path: Path,
) -> None:
    provider = RuntimeTemplateGraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    source.write_text(
        _compose_text(()) + "    env_file:\n      - .env\n", encoding="utf-8"
    )
    (context.workspace / ".env.sample").write_text("APP_MODE=test\n", encoding="utf-8")

    _run(_state(source.parent, run_id="runtime-template-startup"), context)

    adapter_calls = [
        index
        for index, call in enumerate(provider.calls)
        if call[0] == "exec" and "repotrial-startup-input" in call[2]
    ]
    pull_calls = [
        index
        for index, call in enumerate(provider.calls)
        if call[0] == "exec" and "--ignore-buildable" in call[2]
    ]
    assert len(adapter_calls) == 2
    assert len(pull_calls) == 1
    assert adapter_calls[0] < pull_calls[0]
    assert len(list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))) == 1
    assert (
        len(
            list(
                context.artifact_dir.glob(
                    "baseline-*/warmup-startup-input-attempt.jsonl"
                )
            )
        )
        == 1
    )


def test_runtime_template_baseline_checkpoint_without_warmup_is_valid(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, journeys=[])
        graph = build_run_graph(interrupt_after=("baseline",))

        result = asyncio.run(
            ainvoke_run(
                graph,
                _state(source.parent, run_id="runtime-template-baseline-checkpoint"),
                context=context,
            )
        )

    assert result.stage_history == ["intake", "baseline"]
    assert not list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))


def test_runtime_template_warmup_failure_stops_before_baseline(
    tmp_path: Path,
) -> None:
    provider = RuntimeTemplateGraphProvider(fail_prepare=True)
    context, source = _context(tmp_path, provider, journeys=[])

    result = _run(_state(source.parent, run_id="runtime-template-failure"), context)

    assert result.boot_verdict is Verdict.UNSUPPORTED
    assert result.run.stop_reason == "image_prepare_failed"
    assert provider.events == ["warmup_create", "warmup_destroy"]
    assert not any(event == "baseline_create" for event in provider.events)
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert ("prepare", "failure") in {(row["event"], row["outcome"]) for row in rows}
    assert ("warmup_destroy", "failure") not in {
        (row["event"], row["outcome"]) for row in rows
    }


def test_ordinary_run_experiment_error_is_not_image_prepare_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))

        async def fail_experiment(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise ValueError("ordinary experiment failure")

        monkeypatch.setattr(graph_module, "run_experiment", fail_experiment)

        result = _run(
            _state(source.parent, run_id="runtime-template-ordinary-experiment"),
            context,
        )

    assert result.run.stop_reason == "operation_failed"


def test_capable_provider_without_audit_contract_fails_closed(
    tmp_path: Path,
) -> None:
    provider = IncompleteRuntimeTemplateProvider()
    context, _ = _context(tmp_path, provider, journeys=[])
    state = _state(context.workspace, run_id="runtime-template-missing-audit")

    class ActivatingGraph:
        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            del args
            await provider.activate_runtime_template("sandbox", "a" * 64)
            return GraphState(run=state).model_dump(mode="json")

    with pytest.raises(RuntimeError, match="runtime template"):
        asyncio.run(
            graph_module._invoke_graph_with_finalization(
                ActivatingGraph(),
                None,
                state.run_id,
                context=context,
                config=None,
            )
        )


def test_runtime_template_final_audit_identity_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    mismatched_identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="b" * 32,
        image_id="b" * 12,
        image_identity_sha256="c" * 64,
    )
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(
            host_port=host_port,
            finalize_audit_identity=mismatched_identity,
        )
        context, source = _context(tmp_path, provider, risks=("root_user",))

        with pytest.raises(graph_module._TemplateEvidenceError) as caught:
            _run(
                _state(source.parent, run_id="runtime-template-audit-mismatch"),
                context,
            )

    assert caught.value.reason == "runtime_template_evidence_failed"
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert rows[-1]["event"] == "template_remove"
    assert rows[-1]["outcome"] == "cleanup_failure"
    assert rows[-1]["removal_confirmed"] is False


def test_runtime_template_audit_identity_without_activation_fails_without_evidence(
    tmp_path: Path,
) -> None:
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="b" * 32,
        image_id="b" * 12,
        image_identity_sha256="c" * 64,
    )

    class FinalAuditIdentityProvider(IncompleteRuntimeTemplateProvider):
        def runtime_template_audit(self) -> RuntimeTemplateAudit:
            return RuntimeTemplateAudit(identity=identity, removal_confirmed=True)

        async def finalize_runtime_template(self) -> None:
            return

    provider = FinalAuditIdentityProvider()
    context, _ = _context(tmp_path, provider, journeys=[])
    state = _state(
        context.workspace, run_id="runtime-template-audit-without-activation"
    )

    class SuccessfulGraph:
        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return GraphState(run=state).model_dump(mode="json")

    with pytest.raises(graph_module._TemplateEvidenceError) as caught:
        asyncio.run(
            graph_module._invoke_graph_with_finalization(
                SuccessfulGraph(),
                None,
                state.run_id,
                context=context,
                config=None,
            )
        )

    assert caught.value.reason == "runtime_template_evidence_failed"


def test_runtime_template_audit_identity_without_activation_cannot_write_remove_success(
    tmp_path: Path,
) -> None:
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="b" * 32,
        image_id="b" * 12,
        image_identity_sha256="c" * 64,
    )
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(
            host_port=host_port,
            fail_activate=True,
            finalize_audit_identity=identity,
        )
        context, source = _context(tmp_path, provider, journeys=[])

        with pytest.raises(graph_module._TemplateEvidenceError) as caught:
            _run(
                _state(source.parent, run_id="runtime-template-audit-no-activate"),
                context,
            )

    assert caught.value.reason == "runtime_template_evidence_failed"
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert rows[-1]["event"] == "template_remove"
    assert rows[-1]["outcome"] == "cleanup_failure"
    assert rows[-1]["removal_confirmed"] is False


def test_runtime_template_resume_after_baseline_checkpoint_prepares_before_first_warmup(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("baseline",))
        state = _state(source.parent, run_id="runtime-template-baseline-resume")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.stage_history == ["intake", "baseline"]
        assert provider.events == []

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "no_remaining_mutations"
    assert provider.events[:4] == [
        "warmup_create",
        "prepare",
        "activate",
        "warmup_destroy",
    ]
    assert provider.events[-1] == "template_remove"
    assert len(provider.candidate_template_identities) == 1
    assert len(provider.candidate_template_identities[0] or "") == 64


def test_runtime_template_resume_after_boot_rebuilds_before_candidate_with_immutable_startup(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("boot",))
        state = _state(source.parent, run_id="runtime-template-boot-resume")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.stage_history == ["intake", "baseline", "boot"]
        assert provider.expected_image_identity_sha256() is None
        assert provider.events == [
            "warmup_create",
            "prepare",
            "activate",
            "warmup_destroy",
            "baseline_create",
            "template_remove",
        ]

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "no_remaining_mutations"
    assert provider.events[6:] == [
        "warmup_create",
        "prepare",
        "activate",
        "warmup_destroy",
        "candidate_create",
        "template_remove",
    ]
    assert len(provider.candidate_template_identities) == 1
    assert len(provider.candidate_template_identities[0] or "") == 64
    candidate_ids = {
        sandbox_id
        for sandbox_id, role in provider._roles.items()
        if role == "candidate"
    }
    candidate_up = [
        call[2]
        for call in provider.calls
        if call[0] == "exec"
        and call[1] in candidate_ids
        and call[2][-8:]
        == (
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "60",
            "--pull",
            "never",
            "--no-build",
        )
    ]
    assert len(candidate_up) == 1


def test_runtime_template_candidate_checkpoint_rebuilds_before_next_candidate(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user", "cap_add"))
        graph = build_run_graph(interrupt_after=("experiment",))
        state = _state(source.parent, run_id="runtime-template-candidate-resume")

        first = asyncio.run(ainvoke_run(graph, state, context=context))
        assert first.pending_experiment is not None
        assert provider.expected_image_identity_sha256() is None
        first_resume = asyncio.run(aresume_run(graph, state.run_id, context=context))
        assert first_resume.pending_experiment is not None
        assert provider.expected_image_identity_sha256() is None
        second_resume = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert second_resume.run.stop_reason == "no_remaining_mutations"
    assert len(provider.candidate_template_identities) == 2
    assert all(
        len(identity or "") == 64 for identity in provider.candidate_template_identities
    )
    assert provider.events == [
        "warmup_create",
        "prepare",
        "activate",
        "warmup_destroy",
        "baseline_create",
        "candidate_create",
        "template_remove",
        "warmup_create",
        "prepare",
        "activate",
        "warmup_destroy",
        "candidate_create",
        "template_remove",
    ]


def test_runtime_template_resume_tampered_accepted_compose_stops_before_rewarmup(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user", "cap_add"))
        graph = build_run_graph(interrupt_after=("decide",))
        state = _state(source.parent, run_id="runtime-template-accepted-tamper")

        checkpoint = asyncio.run(ainvoke_run(graph, state, context=context))
        assert len(checkpoint.run.experiments) == 1
        assert len(provider.candidate_template_identities) == 1
        assert checkpoint.run.compose_path is not None
        accepted = context.workspace / checkpoint.run.compose_path
        accepted.write_text(
            accepted.read_text(encoding="utf-8").replace(
                "image: example/web:1", "image: example/web:2"
            ),
            encoding="utf-8",
        )

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "experiment:parent_hash_mismatch"
    assert provider.events.count("warmup_create") == 1
    assert len(provider.candidate_template_identities) == 1


def test_runtime_template_resume_startup_input_drift_stops_before_rewarmup(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user", "cap_add"))
        source.write_text(
            _compose_text(("root_user", "cap_add")) + "    env_file:\n      - .env\n",
            encoding="utf-8",
        )
        (context.workspace / ".env.sample").write_text(
            "APP_MODE=test\n", encoding="utf-8"
        )
        graph = build_run_graph(interrupt_after=("decide",))
        state = _state(source.parent, run_id="runtime-template-startup-drift")

        checkpoint = asyncio.run(ainvoke_run(graph, state, context=context))
        assert len(checkpoint.run.experiments) == 1
        assert len(provider.candidate_template_identities) == 1
        identity = graph_module._run_evidence_directory(checkpoint.run, context) / (
            "startup-input-identity.json"
        )
        assert identity.is_file()
        (context.workspace / ".env.sample").write_text(
            "APP_MODE=changed\n", encoding="utf-8"
        )

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "experiment:startup_input_unsupported"
    assert provider.events.count("warmup_create") == 1
    assert len(provider.candidate_template_identities) == 1


def test_runtime_template_resume_without_followup_sandbox_does_not_rewarmup(
    tmp_path: Path,
) -> None:
    provider = RuntimeTemplateGraphProvider()
    context, source = _context(tmp_path, provider, journeys=[])
    graph = build_run_graph(interrupt_after=("boot",))
    state = _state(source.parent, run_id="runtime-template-no-candidate")

    interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
    assert interrupted.stage_history == ["intake", "baseline", "boot"]
    assert provider.expected_image_identity_sha256() is None
    resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "insufficient_coverage"
    assert provider.events.count("warmup_create") == 1
    assert "candidate_create" not in provider.events


def test_runtime_template_resume_rebuild_failure_is_bounded_and_fail_closed(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))
        graph = build_run_graph(interrupt_after=("boot",))
        state = _state(source.parent, run_id="runtime-template-rebuild-failure")

        interrupted = asyncio.run(ainvoke_run(graph, state, context=context))
        assert interrupted.stage_history == ["intake", "baseline", "boot"]
        provider.fail_prepare = True

        resumed = asyncio.run(aresume_run(graph, state.run_id, context=context))

    assert resumed.run.stop_reason == "image_prepare_failed"
    assert provider.events == [
        "warmup_create",
        "prepare",
        "activate",
        "warmup_destroy",
        "baseline_create",
        "template_remove",
        "warmup_create",
        "warmup_destroy",
    ]
    assert provider.candidate_template_identities == []
    assert provider.expected_image_identity_sha256() is None
    evidence = list(context.artifact_dir.glob("experiment-*/image-template.jsonl"))
    assert len(evidence) == 1
    assert (
        evidence[0].relative_to(context.artifact_dir).as_posix()
        in resumed.run.artifacts
    )
    assert any(item.startswith("baseline-") for item in resumed.run.artifacts)


def test_runtime_template_destroy_failure_preserves_cleanup_error(
    tmp_path: Path,
) -> None:
    provider = RuntimeTemplateGraphProvider(
        fail_warmup_destroy=True,
        cancel_prepare=True,
    )
    context, source = _context(tmp_path, provider, journeys=[])

    with pytest.raises(CleanupError) as caught:
        _run(_state(source.parent, run_id="runtime-template-cleanup"), context)

    assert isinstance(caught.value.body_failure, asyncio.CancelledError)
    assert isinstance(caught.value.destroy_failure, RuntimeError)
    assert not any(event == "baseline_create" for event in provider.events)


def test_runtime_template_finalization_evidence_is_run_scoped_and_classified(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    first = (
        artifact_dir
        / f"baseline-{graph_module._run_token('run-a')}-0000-attempt-01"
        / "image-template.jsonl"
    )
    second = (
        artifact_dir
        / f"baseline-{graph_module._run_token('run-b')}-0000-attempt-01"
        / "image-template.jsonl"
    )
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text('{"event":"prepare"}\n', encoding="utf-8")
    second.write_text('{"event":"prepare"}\n', encoding="utf-8")

    graph_module._append_template_finalization_evidence(
        artifact_dir,
        "run-a",
        evidence_path=first,
        body_failure=ValueError("graph failed"),
    )
    graph_module._append_template_finalization_evidence(
        artifact_dir,
        "run-b",
        evidence_path=second,
        cleanup_failure=RuntimeError("cleanup failed"),
    )

    first_rows = [json.loads(line) for line in first.read_text().splitlines()]
    second_rows = [json.loads(line) for line in second.read_text().splitlines()]
    assert first_rows[-1]["outcome"] == "body_failure"
    assert second_rows[-1]["outcome"] == "cleanup_failure"


def test_image_template_evidence_is_atomic_no_clobber_and_mode_bounded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "image-template.jsonl"
    path.write_text("sentinel\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        graph_module._ImageTemplateEvidence(path)
    assert path.read_text(encoding="utf-8") == "sentinel\n"

    path.unlink()
    evidence = graph_module._ImageTemplateEvidence(path)
    assert path.stat().st_mode & 0o777 == 0o600
    evidence.record("prepare", "started")
    evidence.close()
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "prepare"


class _BrokenTemplateArtifact:
    def write(self, _: str) -> None:
        raise OSError("evidence write failed")

    def flush(self) -> None:
        raise OSError("evidence flush failed")

    def close(self) -> None:
        raise OSError("evidence close failed")


def test_image_template_evidence_retains_audit_failures() -> None:
    evidence = graph_module._ImageTemplateEvidence.__new__(
        graph_module._ImageTemplateEvidence
    )
    evidence._artifact = _BrokenTemplateArtifact()
    evidence._size = 0
    evidence.audit_failures = []

    evidence.record("prepare", "started")
    evidence.close()

    assert [str(error) for error in evidence.audit_failures] == [
        "evidence write failed",
        "evidence close failed",
    ]


def test_template_finalization_evidence_failure_is_not_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    path = (
        artifact_dir
        / f"baseline-{graph_module._run_token('run-failure')}-0000-attempt-01"
        / "image-template.jsonl"
    )
    path.parent.mkdir()
    path.write_text('{"event":"prepare"}\n', encoding="utf-8")
    original_open = Path.open

    def fail_append(self: Path, *args: object, **kwargs: object):
        if args and args[0] == "ab":
            raise OSError("finalization evidence write failed")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_append)

    failure = graph_module._append_template_finalization_evidence(
        artifact_dir, "run-failure", evidence_path=path
    )
    assert isinstance(failure, OSError)


def test_graph_failure_and_template_cleanup_failure_preserve_both_objects(
    tmp_path: Path,
) -> None:
    cleanup_failure = OSError("template cleanup failed")
    provider = RuntimeTemplateGraphProvider(finalize_failure=cleanup_failure)
    context, _ = _context(tmp_path, provider, journeys=[])

    class FailingGraph:
        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            provider._template_identity = "a" * 64
            raise ValueError("graph body failed")

    with pytest.raises(graph_module.RuntimeTemplateCleanupError) as caught:
        asyncio.run(
            graph_module._invoke_graph_with_finalization(
                FailingGraph(),
                None,
                "runtime-template-dual-failure",
                context=context,
                config=None,
            )
        )

    assert isinstance(caught.value.body_failure, ValueError)
    assert caught.value.body_failure.args == ("graph body failed",)
    assert caught.value.cleanup_failure is cleanup_failure


@pytest.mark.parametrize(
    "failure", [ValueError("graph failed"), asyncio.CancelledError()]
)
def test_graph_failure_still_finalizes_active_template(
    tmp_path: Path, failure: BaseException
) -> None:
    provider = RuntimeTemplateGraphProvider()
    context, _ = _context(tmp_path, provider, journeys=[])

    class FailingGraph:
        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            provider._template_identity = "a" * 64
            raise failure

    with pytest.raises(type(failure)):
        asyncio.run(
            graph_module._invoke_graph_with_finalization(
                FailingGraph(),
                None,
                "runtime-template-finalize-failure",
                context=context,
                config=None,
            )
        )

    assert provider.events == ["template_remove"]


def test_successful_graph_preserves_template_cleanup_failure(
    tmp_path: Path,
) -> None:
    cleanup_failure = OSError("template removal failed")
    provider = RuntimeTemplateGraphProvider(finalize_failure=cleanup_failure)
    context, _ = _context(tmp_path, provider, journeys=[])
    state = _state(context.workspace, run_id="runtime-template-success-cleanup")

    class SuccessfulGraph:
        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            provider._template_identity = "a" * 64
            return GraphState(run=state).model_dump(mode="json")

    with pytest.raises(graph_module.RuntimeTemplateCleanupError) as caught:
        asyncio.run(
            graph_module._invoke_graph_with_finalization(
                SuccessfulGraph(),
                None,
                state.run_id,
                context=context,
                config=None,
            )
        )

    assert caught.value.body_failure is None
    assert caught.value.cleanup_failure is cleanup_failure


def test_runtime_template_warmup_reuses_compatibility_materialization(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, journeys=[])
        source.write_text(
            _compose_text(()).replace(
                "    image: example/web:1\n",
                "    image: example/web:1\n"
                f"    ports:\n      - '127.0.0.1:{host_port}:8080'\n",
            ),
            encoding="utf-8",
        )

        _run(_state(source.parent, run_id="runtime-template-compatibility"), context)

    warmup_id = next(
        sandbox_id for sandbox_id, role in provider._roles.items() if role == "warmup"
    )
    warmup_calls = [
        call for call in provider.calls if call[0] == "exec" and call[1] == warmup_id
    ]
    compatibility_index = next(
        index
        for index, call in enumerate(warmup_calls)
        if "repotrial-compatibility-overlay" in call[2]
    )
    prepare_index = next(
        index
        for index, call in enumerate(warmup_calls)
        if "--ignore-buildable" in call[2]
    )
    assert compatibility_index < prepare_index


def test_graph_and_cleanup_failure_retain_audit_failure_as_secondary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_failure = OSError("template removal failed")
    audit_failure = OSError("evidence append failed")
    provider = RuntimeTemplateGraphProvider(finalize_failure=cleanup_failure)
    context, _ = _context(tmp_path, provider, journeys=[])

    def fail_evidence(*args: object, **kwargs: object) -> BaseException:
        del args, kwargs
        return audit_failure

    monkeypatch.setattr(
        graph_module, "_append_template_finalization_evidence", fail_evidence
    )

    class FailingGraph:
        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            provider._template_identity = "a" * 64
            raise ValueError("graph body failed")

    with pytest.raises(graph_module.RuntimeTemplateCleanupError) as caught:
        asyncio.run(
            graph_module._invoke_graph_with_finalization(
                FailingGraph(),
                None,
                "runtime-template-audit-secondary",
                context=context,
                config=None,
            )
        )

    assert caught.value.cleanup_failure is cleanup_failure
    assert isinstance(caught.value.body_failure, ValueError)
    assert caught.value.secondary_failures == (audit_failure,)
    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
    assert audit_failure in caught.value.__cause__.exceptions


class _WriteFlushCloseFailingArtifact:
    def __init__(self) -> None:
        self.write_count = 0

    def write(self, value: str) -> int:
        self.write_count += 1
        if self.write_count == 1:
            raise OSError("evidence write failed")
        return len(value)

    def flush(self) -> None:
        raise OSError("evidence flush failed")

    def close(self) -> None:
        raise OSError("evidence close failed")


class _WriteFlushCloseFailingEvidence(graph_module._ImageTemplateEvidence):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._artifact.close()
        self._artifact = _WriteFlushCloseFailingArtifact()


def test_warmup_evidence_failures_preserve_managed_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = asyncio.CancelledError("cancel-raw-secret-marker")
    destroy_error = RuntimeError("destroy-raw-secret-marker")
    monkeypatch.setattr(
        graph_module, "_ImageTemplateEvidence", _WriteFlushCloseFailingEvidence
    )
    provider = RuntimeTemplateGraphProvider(
        fail_warmup_destroy=True,
        cancel_prepare=True,
        prepare_cancellation=cancellation,
        warmup_destroy_error=destroy_error,
    )
    context, source = _context(tmp_path, provider, journeys=[])

    with pytest.raises(CleanupError) as caught:
        _run(_state(source.parent, run_id="runtime-template-audit-cleanup"), context)

    assert caught.value.body_failure is cancellation
    assert caught.value.destroy_failure is destroy_error
    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
    audit_error = next(
        error
        for error in caught.value.__cause__.exceptions
        if isinstance(error, graph_module._TemplateEvidenceError)
    )
    assert [str(error) for error in audit_error.audit_failures] == [
        "evidence write failed",
        "evidence flush failed",
        "evidence flush failed",
        "evidence flush failed",
        "evidence flush failed",
        "evidence close failed",
    ]
    assert caught.value.__cause__.exceptions[0] is caught.value.destroy_failure
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert rows[-1]["event"] == "template_remove"
    assert all(len(json.dumps(row)) < 4096 for row in rows)
    assert "cancel-raw-secret-marker" not in json.dumps(rows)
    assert "destroy-raw-secret-marker" not in json.dumps(rows)


@pytest.mark.parametrize("mode", ["discovery", "size"])
def test_graph_finalization_evidence_boundary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    original_close = graph_module._ImageTemplateEvidence.close

    def sabotage_close(evidence: graph_module._ImageTemplateEvidence) -> None:
        original_close(evidence)
        if mode == "discovery":
            evidence.path.unlink()
        else:
            evidence.path.write_bytes(
                b"x" * graph_module._MAX_IMAGE_TEMPLATE_EVIDENCE_BYTES
            )

    monkeypatch.setattr(graph_module._ImageTemplateEvidence, "close", sabotage_close)
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))

        with pytest.raises(graph_module._TemplateEvidenceError) as caught:
            _run(
                _state(source.parent, run_id=f"runtime-template-evidence-{mode}"),
                context,
            )

    assert caught.value.reason == "runtime_template_evidence_failed"
    assert provider.events[-1] == "template_remove"


@pytest.mark.parametrize(
    ("provider_kwargs", "expected_reason", "expected_events", "failure_reason"),
    [
        (
            {"wrong_guest_workspace": True},
            "image_prepare_failed",
            [
                ("warmup_create", "started"),
                ("warmup_create", "success"),
                ("prepare", "started"),
                ("warmup_destroy", "success"),
                ("prepare", "failure"),
                ("template_remove", "success"),
            ],
            "guest_workspace_verification_failed",
        ),
        (
            {
                "prepare_error": DockerSbxError(
                    "prepare",
                    "total_duration_exhausted",
                    stderr="docker-raw-secret-marker",
                    failure_evidence=SandboxFailureEvidence(
                        operation="prepare",
                        reason="total_duration_exhausted",
                    ),
                )
            },
            "total_duration_exhausted",
            [
                ("warmup_create", "started"),
                ("warmup_create", "success"),
                ("prepare", "started"),
                ("warmup_destroy", "success"),
                ("prepare", "failure"),
                ("template_remove", "success"),
            ],
            "total_duration_exhausted",
        ),
        (
            {"fail_activate": True},
            "image_prepare_failed",
            [
                ("warmup_create", "started"),
                ("warmup_create", "success"),
                ("prepare", "started"),
                ("warmup_destroy", "success"),
                ("prepare", "failure"),
                ("template_remove", "success"),
            ],
            "template_activation_failed",
        ),
    ],
)
def test_boot_projects_bounded_warmup_failure_reason_and_evidence(
    tmp_path: Path,
    provider_kwargs: dict[str, object],
    expected_reason: str,
    expected_events: list[tuple[str, str]],
    failure_reason: str,
) -> None:
    provider = RuntimeTemplateGraphProvider(**provider_kwargs)
    context, source = _context(tmp_path, provider, journeys=[])

    result = _run(
        _state(source.parent, run_id=f"runtime-template-{expected_reason}"), context
    )

    assert result.run.stop_reason == expected_reason
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert [(row["event"], row["outcome"]) for row in rows] == expected_events
    failure_row = next(
        row for row in rows if row["event"] == "prepare" and row["outcome"] == "failure"
    )
    assert failure_row["reason"] == failure_reason
    assert "stderr" not in json.dumps(failure_row)
    if expected_reason == "total_duration_exhausted":
        assert failure_row["failure"] == {
            "operation": "prepare",
            "reason": "total_duration_exhausted",
        }
        assert "docker-raw-secret-marker" not in json.dumps(rows)


def test_boot_projects_cleanup_failure_with_real_warmup_lifecycle(
    tmp_path: Path,
) -> None:
    cancellation = asyncio.CancelledError("cleanup-cancel-raw-secret-marker")
    destroy_error = RuntimeError("cleanup-destroy-raw-secret-marker")
    provider = RuntimeTemplateGraphProvider(
        fail_warmup_destroy=True,
        cancel_prepare=True,
        prepare_cancellation=cancellation,
        warmup_destroy_error=destroy_error,
    )
    context, source = _context(tmp_path, provider, journeys=[])

    with pytest.raises(CleanupError) as caught:
        _run(_state(source.parent, run_id="runtime-template-cleanup-reason"), context)

    assert caught.value.body_failure is cancellation
    assert caught.value.destroy_failure is destroy_error
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    assert [(row["event"], row["outcome"]) for row in rows] == [
        ("warmup_create", "started"),
        ("warmup_create", "success"),
        ("prepare", "started"),
        ("prepare", "failure"),
        ("warmup_destroy", "failure"),
        ("template_body_failure", "body_failure"),
        ("template_remove", "success"),
    ]
    assert rows[3]["reason"] == "operation_failed"
    assert rows[4]["reason"] == "operation_failed"
    assert rows[5]["reason"] == "sandbox_cleanup_failed"
    assert "cleanup-cancel-raw-secret-marker" not in json.dumps(rows)
    assert "cleanup-destroy-raw-secret-marker" not in json.dumps(rows)


def test_runtime_template_evidence_persists_inventory_and_use_identity(
    tmp_path: Path,
) -> None:
    with _healthy_server() as (_, host_port):
        provider = RuntimeTemplateGraphProvider(host_port=host_port)
        context, source = _context(tmp_path, provider, risks=("root_user",))

        result = _run(
            _state(source.parent, run_id="runtime-template-audit-identity"), context
        )

    assert result.run.stop_reason == "no_remaining_mutations"
    evidence = list(context.artifact_dir.glob("baseline-*/image-template.jsonl"))
    assert len(evidence) == 1
    rows = [json.loads(line) for line in evidence[0].read_text().splitlines()]
    prepare = next(
        row for row in rows if row["event"] == "prepare" and row["outcome"] == "success"
    )
    activate = next(
        row
        for row in rows
        if row["event"] == "activate" and row["outcome"] == "success"
    )
    assert prepare["inventory_record_count"] == 1
    assert prepare["inventory_image_ids"] == ["sha256:" + "a" * 64]
    assert prepare["inventory_sha256"] == prepare["identity_sha256"]
    assert activate["inventory_record_count"] == prepare["inventory_record_count"]
    assert activate["inventory_image_ids"] == prepare["inventory_image_ids"]
    assert activate["inventory_sha256"] == prepare["inventory_sha256"]
    identity = activate["template_identity"]
    assert identity["repository"] == "docker.io/library/repotrial-runtime"
    assert len(identity["tag"]) == 32
    assert len(identity["image_id"]) == 12
    assert identity["image_identity_sha256"] == prepare["inventory_sha256"]
    uses = [row for row in rows if row["event"] == "template_use"]
    assert len(uses) == 2
    assert all(row["template_identity"] == identity for row in uses)
    assert rows[-1]["event"] == "template_remove"
    assert rows[-1]["outcome"] == "success"
