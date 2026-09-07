import asyncio
import base64
import hashlib
import json
import posixpath
import re
import subprocess
import threading
import unicodedata
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, TypeVar

import pytest
from fixture_harness import parse_fixture_materialization
from pydantic import BaseModel
from typer.testing import CliRunner

from repotrial import cli
from repotrial.agent.state import GraphContext, GraphState
from repotrial.cli import create_app
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    JourneyResult,
    Mutation,
    PinnedRepo,
    RepoRef,
    RunState,
)
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult
from repotrial.sandbox.docker_sbx import DockerSbxUnsupportedError
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial import compatibility as _compatibility

ModelT = TypeVar("ModelT", bound=BaseModel)
FIXED_RUN_ID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "a" * 12
_CLI_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(.*)\Z")
_CLI_DEFAULT_COMPOSE_PATH = "compose.yaml"
_CLI_DEFAULT_ALLOWED_ENV_KEYS = frozenset()
_CLI_COMPOSE_ROUTES = {
    ("up", "-d", "--wait", "--wait-timeout", "60"): "up",
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


def _cli_safe_relative_path(value: str) -> bool:
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


class FakeModelAdapter:
    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT:
        del system, user
        return schema.model_validate(
            RecoveryAction(action="stop", params={}, reason="fixture stop").model_dump()
        )


class FixtureProvider(FakeSandboxProvider):
    def __init__(
        self,
        *,
        host_port: int | None = None,
        container_port: int = 8080,
        baseline_healthy: bool = True,
        candidate_boot_unavailable: bool = False,
        create_error: BaseException | None = None,
        expected_compose_path: str = _CLI_DEFAULT_COMPOSE_PATH,
        allowed_env_keys: Collection[str] = _CLI_DEFAULT_ALLOWED_ENV_KEYS,
    ) -> None:
        super().__init__(ports={} if host_port is None else {container_port: host_port})
        self.baseline_healthy = baseline_healthy
        self.candidate_boot_unavailable = candidate_boot_unavailable
        self.create_error = create_error
        self.expected_compose_path = expected_compose_path
        self.allowed_env_keys = frozenset(allowed_env_keys)
        self.roles: dict[str, str] = {}
        self.workspaces: dict[str, Path] = {}
        self.materialized_experiments: set[str] = set()
        self.experiment_guest_files: dict[str, bytes] = {}
        self.accepted_guest_files: dict[str, dict[str, bytes]] = {}
        self.accepted_expected_sha256: dict[str, dict[str, str]] = {}
        self.active_compose_files: dict[str, tuple[str, ...]] = {}
        self.active_env: dict[str, dict[str, str]] = {}
        self.discovered_container_ids: dict[str, str] = {}
        self.active_sandboxes: set[str] = set()

    async def create(self, workspace: Path, name: str) -> str:
        if self.create_error is not None:
            raise self.create_error
        sandbox_id = await super().create(workspace, name)
        self.roles[sandbox_id] = (
            "baseline" if name.startswith("repotrial-baseline-") else "candidate"
        )
        self.workspaces[sandbox_id] = workspace
        self.active_sandboxes.add(sandbox_id)
        return sandbox_id

    async def destroy(self, sandbox_id: str) -> None:
        await super().destroy(sandbox_id)
        self.active_sandboxes.discard(sandbox_id)
        self.workspaces.pop(sandbox_id, None)
        self.materialized_experiments.discard(sandbox_id)
        self.experiment_guest_files.pop(sandbox_id, None)
        self.accepted_guest_files.pop(sandbox_id, None)
        self.accepted_expected_sha256.pop(sandbox_id, None)
        self.active_compose_files.pop(sandbox_id, None)
        self.active_env.pop(sandbox_id, None)
        self.discovered_container_ids.pop(sandbox_id, None)

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self._require_active(sandbox_id)
        command = tuple(argv)
        self.calls.append(("exec", sandbox_id, command, timeout_s))
        materialization = self._experiment_materialization(sandbox_id, argv)
        if materialization is not None:
            return materialization
        accepted_materialization = self._accepted_materialization(sandbox_id, argv)
        if accepted_materialization is not None:
            return accepted_materialization
        verification = self._experiment_verification(sandbox_id, argv)
        if verification is not None:
            return verification
        accepted_verification = self._accepted_verification(sandbox_id, argv)
        if accepted_verification is not None:
            return accepted_verification
        route = self._route_compose_command(sandbox_id, argv)
        if route is not None:
            route_name, compose_files, active_env = route
            if route_name == "up":
                self.active_compose_files[sandbox_id] = compose_files
                self.active_env[sandbox_id] = active_env
                if (
                    self.roles[sandbox_id] == "candidate"
                    and self.candidate_boot_unavailable
                ):
                    raise KeyError("candidate backend capability is unavailable")
            elif (
                self.active_compose_files.get(sandbox_id) != compose_files
                or self.active_env.get(sandbox_id) != active_env
            ):
                raise AssertionError(
                    "compose readiness/observer command does not match active state"
                )

            healthy = self.baseline_healthy or self.roles[sandbox_id] == "candidate"
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
                return ExecResult(exit_code=0, stdout="", stderr="")
            if route_name == "observer_ps":
                self.discovered_container_ids[sandbox_id] = CONTAINER_ID
                return ExecResult(
                    exit_code=0,
                    stdout=json.dumps({"Service": "web", "ID": CONTAINER_ID}),
                    stderr="",
                )
        if (
            command == ("docker", "inspect", CONTAINER_ID)
            and self.discovered_container_ids.get(sandbox_id) == CONTAINER_ID
        ):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps([{"Config": {"Env": []}}]),
                stderr="",
            )
        if (
            command == ("docker", "diff", CONTAINER_ID)
            and self.discovered_container_ids.get(sandbox_id) == CONTAINER_ID
        ):
            return ExecResult(exit_code=0, stdout="", stderr="")
        if (
            command
            == (
                "docker",
                "top",
                CONTAINER_ID,
                "-eo",
                "pid,ppid,user,comm",
            )
            and self.discovered_container_ids.get(sandbox_id) == CONTAINER_ID
        ):
            return ExecResult(
                exit_code=0,
                stdout="PID PPID USER COMMAND\n",
                stderr="",
            )
        raise AssertionError(f"unexpected provider command: {command!r}")

    def _route_compose_command(
        self, sandbox_id: str, argv: list[str]
    ) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
        if not argv or any(not isinstance(item, str) for item in argv):
            return None
        command = list(argv)
        active_env: dict[str, str] = {}
        if command[0] == "env":
            try:
                docker_index = command.index("docker", 1)
            except ValueError:
                return None
            assignments = command[1:docker_index]
            if not assignments:
                return None
            for assignment in assignments:
                match = _CLI_ENV_ASSIGNMENT.fullmatch(assignment)
                if match is None:
                    return None
                key, value = assignment.split("=", 1)
                if key not in self.allowed_env_keys or key in active_env:
                    return None
                active_env[key] = value
            command = command[docker_index:]
        if command[:2] != ["docker", "compose"]:
            return None
        compose_files: list[str] = []
        index = 2
        while index < len(command) and command[index] == "-f":
            if index + 1 >= len(command) or not _cli_safe_relative_path(
                command[index + 1]
            ):
                return None
            compose_files.append(command[index + 1])
            index += 2
        if len(compose_files) not in {1, 2} or len(compose_files) != len(
            set(compose_files)
        ):
            return None
        base_path = compose_files[0]
        accepted_match = _compatibility._ACCEPTED_COMPOSE_PATTERN.fullmatch(base_path)
        if base_path != self.expected_compose_path and accepted_match is None:
            return None
        if (
            accepted_match is not None
            and base_path not in self.accepted_guest_files.get(sandbox_id, {})
        ):
            return None
        if len(compose_files) == 2 and (
            compose_files[1] != _compatibility._EXPERIMENT_RELATIVE_PATH
            or sandbox_id not in self.materialized_experiments
        ):
            return None
        route_name = _CLI_COMPOSE_ROUTES.get(tuple(command[index:]))
        if route_name is None:
            return None
        return route_name, tuple(compose_files), active_env

    def _experiment_materialization(
        self, sandbox_id: str, argv: list[str]
    ) -> ExecResult | None:
        if len(argv) < 5 or argv[4] != "repotrial-experiment-overlay":
            return None
        parsed = parse_fixture_materialization(argv)
        if parsed is None:
            return None
        if isinstance(parsed, int):
            return self._materialization_failure(parsed)
        _command_name, relative_path, expected_sha256, payload = parsed
        if sandbox_id in self.materialized_experiments:
            return self._materialization_failure(24)
        self.experiment_guest_files[sandbox_id] = payload
        self.materialized_experiments.add(sandbox_id)
        return self._materialization_success(relative_path, expected_sha256)

    def _experiment_verification(
        self, sandbox_id: str, argv: list[str]
    ) -> ExecResult | None:
        if len(argv) != 3 or argv[:2] != ["sha256sum", "--"]:
            return None
        relative_path = argv[2]
        if relative_path != _compatibility._EXPERIMENT_RELATIVE_PATH:
            return None
        if sandbox_id not in self.materialized_experiments:
            return None
        try:
            payload = self.experiment_guest_files[sandbox_id]
        except KeyError:
            return self._materialization_failure(1)
        return ExecResult(
            exit_code=0,
            stdout=f"{hashlib.sha256(payload).hexdigest()}  {relative_path}\n",
            stderr="",
        )

    def _accepted_materialization(
        self, sandbox_id: str, argv: list[str]
    ) -> ExecResult | None:
        if len(argv) < 5 or argv[4] != "repotrial-accepted-compose":
            return None
        parsed = parse_fixture_materialization(argv)
        if parsed is None:
            return None
        if isinstance(parsed, int):
            return self._materialization_failure(parsed)
        _command_name, relative_path, expected_sha256, payload = parsed
        guest_files = self.accepted_guest_files.setdefault(sandbox_id, {})
        if relative_path in guest_files:
            return self._materialization_failure(24)
        guest_files[relative_path] = payload
        self.accepted_expected_sha256.setdefault(sandbox_id, {})[relative_path] = (
            expected_sha256
        )
        return self._materialization_success(relative_path, expected_sha256)

    def _accepted_verification(
        self, sandbox_id: str, argv: list[str]
    ) -> ExecResult | None:
        if len(argv) != 3 or argv[:2] != ["sha256sum", "--"]:
            return None
        relative_path = argv[2]
        if _compatibility._ACCEPTED_COMPOSE_PATTERN.fullmatch(relative_path) is None:
            return None
        try:
            payload = self.accepted_guest_files[sandbox_id][relative_path]
            expected_sha256 = self.accepted_expected_sha256[sandbox_id][relative_path]
        except KeyError:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            return self._materialization_failure(31)
        return ExecResult(
            exit_code=0,
            stdout=f"{digest}  {relative_path}\n",
            stderr="",
        )

    @staticmethod
    def _materialization_failure(exit_code: int) -> ExecResult:
        return ExecResult(exit_code=exit_code, stdout="", stderr="")

    @staticmethod
    def _materialization_success(
        relative_path: str, expected_sha256: str
    ) -> ExecResult:
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


def _cli_compose_argv(
    *command: str,
    env: tuple[str, ...] = (),
    compose_files: tuple[str, ...] = (_CLI_DEFAULT_COMPOSE_PATH,),
) -> list[str]:
    argv = [*env, "docker", "compose"]
    for compose_file in compose_files:
        argv.extend(("-f", compose_file))
    argv.extend(command)
    return argv


def _cli_experiment_argv(payload: bytes) -> list[str]:
    return [
        "sh",
        "-eu",
        "-c",
        _compatibility._EXPERIMENT_ADAPTER_SCRIPT,
        "repotrial-experiment-overlay",
        _compatibility._EXPERIMENT_RELATIVE_PATH,
        hashlib.sha256(payload).hexdigest(),
        base64.b64encode(payload).decode("ascii"),
    ]


def _cli_accepted_compose_argv(payload: bytes, relative_path: str) -> list[str]:
    return [
        "sh",
        "-eu",
        "-c",
        _compatibility._ACCEPTED_COMPOSE_ADAPTER_SCRIPT,
        "repotrial-accepted-compose",
        relative_path,
        hashlib.sha256(payload).hexdigest(),
        base64.b64encode(payload).decode("ascii"),
    ]


def test_cli_fixture_rejects_shell_wrapped_compose_up_and_unallowlisted_env(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def exercise() -> None:
        sandbox_id = await provider.create(workspace, "candidate-shell-router")
        try:
            with pytest.raises(AssertionError, match="unexpected provider command"):
                await provider.exec(
                    sandbox_id,
                    [
                        "sh",
                        "-c",
                        "docker compose -f compose.yml",
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "60",
                    ],
                )
            with pytest.raises(AssertionError, match="unexpected provider command"):
                await provider.exec(
                    sandbox_id,
                    _cli_compose_argv(
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "60",
                        env=("env", "APP_MODE=fixture", "HOST_SSH_KEY=secret"),
                    ),
                )
        finally:
            await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_cli_fixture_observer_requires_the_active_environment(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def exercise() -> None:
        sandbox_id = await provider.create(workspace, "candidate-observer-router")
        try:
            up = await provider.exec(
                sandbox_id,
                _cli_compose_argv(
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                ),
            )
            assert up.exit_code == 0
            with pytest.raises(
                AssertionError, match="active state|unexpected provider command"
            ):
                await provider.exec(
                    sandbox_id,
                    _cli_compose_argv(
                        "ps",
                        "--all",
                        "--no-trunc",
                        "--orphans=false",
                        "--format",
                        "json",
                        env=("env", "APP_MODE=fixture"),
                    ),
                )
        finally:
            await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_cli_fixture_keeps_experiment_overlay_state_per_sandbox(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_target = workspace / _compatibility._EXPERIMENT_RELATIVE_PATH
    host_target.parent.mkdir()
    host_target.write_bytes(b"pre-existing host artifact")
    first_payload = b"first candidate overlay"
    second_payload = b"second candidate overlay"

    async def exercise() -> tuple[ExecResult, ExecResult, ExecResult, ExecResult]:
        first = await provider.create(workspace, "candidate-first")
        second = await provider.create(workspace, "candidate-second")
        try:
            first_result = await provider.exec(
                first, _cli_experiment_argv(first_payload)
            )
            second_result = await provider.exec(
                second, _cli_experiment_argv(second_payload)
            )
            first_verify = await provider.exec(
                first, ["sha256sum", "--", _compatibility._EXPERIMENT_RELATIVE_PATH]
            )
            await provider.exec(
                second, ["sha256sum", "--", _compatibility._EXPERIMENT_RELATIVE_PATH]
            )
            await provider.destroy(first)
            after_first_destroy = await provider.exec(
                second, ["sha256sum", "--", _compatibility._EXPERIMENT_RELATIVE_PATH]
            )
            return first_result, second_result, first_verify, after_first_destroy
        finally:
            if second in provider.active_sandboxes:
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
    assert host_target.read_bytes() == b"pre-existing host artifact"


def test_cli_fixture_binds_the_authoritative_empty_environment(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider(
        expected_compose_path="compose.yaml",
        allowed_env_keys=frozenset(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def exercise() -> None:
        sandbox_id = await provider.create(workspace, "candidate-authority-env")
        try:
            up = await provider.exec(
                sandbox_id,
                _cli_compose_argv("up", "-d", "--wait", "--wait-timeout", "60"),
            )
            assert up.exit_code == 0
            with pytest.raises(AssertionError):
                await provider.exec(
                    sandbox_id,
                    _cli_compose_argv(
                        "ps",
                        "--all",
                        "--no-trunc",
                        "--orphans=false",
                        "--format",
                        "json",
                        env=("env", "APP_MODE=fixture"),
                    ),
                )
            with pytest.raises(AssertionError):
                await provider.exec(
                    sandbox_id,
                    _cli_compose_argv(
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "60",
                        env=("env", "APP_MODE=fixture"),
                    ),
                )
        finally:
            await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_cli_fixture_rejects_a_non_authoritative_compose_identity(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider(
        expected_compose_path="compose.yaml",
        allowed_env_keys=frozenset(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def exercise() -> None:
        sandbox_id = await provider.create(workspace, "candidate-authority-compose")
        try:
            with pytest.raises(AssertionError):
                await provider.exec(
                    sandbox_id,
                    _cli_compose_argv(
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "60",
                        compose_files=("wrong-but-safe.yaml",),
                    ),
                )
        finally:
            await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_cli_fixture_rejects_an_unmaterialized_experiment_identity(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider(
        expected_compose_path="compose.yaml",
        allowed_env_keys=frozenset(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def exercise() -> None:
        sandbox_id = await provider.create(workspace, "candidate-authority-overlay")
        try:
            with pytest.raises(AssertionError):
                await provider.exec(
                    sandbox_id,
                    _cli_compose_argv(
                        "up",
                        "-d",
                        "--wait",
                        "--wait-timeout",
                        "60",
                        compose_files=(
                            "compose.yaml",
                            _compatibility._EXPERIMENT_RELATIVE_PATH,
                        ),
                    ),
                )
        finally:
            await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_cli_fixture_replays_an_accepted_compose_chain_per_sandbox(
    tmp_path: Path,
) -> None:
    provider = FixtureProvider(
        expected_compose_path="compose.yaml",
        allowed_env_keys=frozenset(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    accepted_relative = (
        ".repotrial-accepted/accepted-0001-0123456789abcdef.compose.yaml"
    )
    accepted_payload = b"services:\n  web:\n    image: example/web:1\n"
    experiment_payload = b"services:\n  web:\n    read_only: true\n"

    async def exercise() -> tuple[ExecResult, ExecResult, ExecResult, ExecResult]:
        sandbox_id = await provider.create(workspace, "candidate-accepted-chain")
        try:
            accepted_materialization = await provider.exec(
                sandbox_id,
                _cli_accepted_compose_argv(accepted_payload, accepted_relative),
            )
            accepted_verify = await provider.exec(
                sandbox_id, ["sha256sum", "--", accepted_relative]
            )
            experiment_materialization = await provider.exec(
                sandbox_id, _cli_experiment_argv(experiment_payload)
            )
            compose_up = await provider.exec(
                sandbox_id,
                _cli_compose_argv(
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                    compose_files=(
                        accepted_relative,
                        _compatibility._EXPERIMENT_RELATIVE_PATH,
                    ),
                ),
            )
            return (
                accepted_materialization,
                accepted_verify,
                experiment_materialization,
                compose_up,
            )
        finally:
            await provider.destroy(sandbox_id)

    (
        accepted_materialization,
        accepted_verify,
        experiment_materialization,
        compose_up,
    ) = asyncio.run(exercise())

    assert accepted_materialization.exit_code == 0
    assert accepted_verify.stdout == (
        f"{hashlib.sha256(accepted_payload).hexdigest()}  {accepted_relative}\n"
    )
    assert experiment_materialization.exit_code == 0
    assert compose_up.exit_code == 0
    assert provider.accepted_guest_files == {}
    assert provider.accepted_expected_sha256 == {}
    assert provider.experiment_guest_files == {}
    assert provider.materialized_experiments == set()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _BusinessHandler(BaseHTTPRequestHandler):
    item: ClassVar[dict[str, object] | None] = None
    operations: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        if self.path != "/items/fixture":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            self.send_error(400)
            return
        type(self).item = {"id": "fixture", "name": payload.get("name")}
        type(self).operations.append("POST")
        self._json_response(201, self.item)

    def do_GET(self) -> None:
        if self.path != "/items/fixture":
            self.send_error(404)
            return
        type(self).operations.append("GET-200" if type(self).item else "GET-404")
        if type(self).item is None:
            self._json_response(404, {"detail": "missing"})
        else:
            self._json_response(200, type(self).item)

    def do_DELETE(self) -> None:
        if self.path != "/items/fixture":
            self.send_error(404)
            return
        type(self).operations.append("DELETE")
        type(self).item = None
        self.send_response(204)
        self.end_headers()

    def _json_response(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def healthy_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def business_server() -> Iterator[int]:
    _BusinessHandler.item = None
    _BusinessHandler.operations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BusinessHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def fixed_run_id() -> str:
    return FIXED_RUN_ID


def create_fixture_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    (repository / "compose.yaml").write_text(
        "services:\n  web:\n    image: example/web:1\n    user: '0'\n    read_only: true\n    cap_drop: [ALL]\n",
        encoding="utf-8",
    )
    (repository / "repotrial.journeys.json").write_text(
        json.dumps(
            {
                "journeys": [
                    {
                        "journey_id": "health",
                        "name": "Health",
                        "steps": [
                            {
                                "step_id": "health-request",
                                "tool": "http",
                                "action": "request",
                                "params": {"method": "GET", "path": "/health"},
                                "assertions": [
                                    {
                                        "kind": "status_code",
                                        "target": "response.status",
                                        "expected": 200,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    for arguments in (
        ("init",),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    return repository


def make_app(artifacts_root: Path, provider: FixtureProvider):
    return create_app(
        artifacts_root=artifacts_root,
        run_id_generator=fixed_run_id,
        provider_factory=lambda name: provider,
        model=FakeModelAdapter(),
    )


def _patch_attempt_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    utc_values = iter(
        (
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 102.5))
    monkeypatch.setattr(cli, "_utc_now", lambda: next(utc_values), raising=False)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
        raising=False,
    )


def _attempt_result(artifacts_root: Path) -> dict[str, object]:
    return json.loads(
        (artifacts_root / FIXED_RUN_ID / "attempt-result.json").read_text(
            encoding="utf-8"
        )
    )


def _assert_attempt_timing(attempt: dict[str, object]) -> None:
    assert attempt["started_at_utc"] == "2026-08-29T12:00:00+00:00"
    assert attempt["ended_at_utc"] == "2026-08-29T12:01:00+00:00"
    assert attempt["monotonic_duration_s"] == 2.5


def _assert_timing_error(attempt: dict[str, object]) -> None:
    assert attempt["timing_status"] == "evidence_timing_error"
    assert attempt["started_at_utc"] is None
    assert attempt["ended_at_utc"] is None
    assert attempt["monotonic_duration_s"] is None


def test_inspect_runs_local_git_fixture_to_a_report_and_cleans_up(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port, container_port=3000)
        source_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            [
                "inspect",
                str(source),
                "--provider",
                "fake",
                "--max-experiments",
                "8",
                "--commit-sha",
                source_head,
                "--container-port",
                "3000",
                "--compose-path",
                "compose.yaml",
            ],
        )

    run_path = artifacts_root / FIXED_RUN_ID
    report_path = run_path / "report" / "trial-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    workspace = run_path / "workspace"
    assert result.exit_code == 0, result.output
    assert (workspace / ".git").is_dir()
    assert (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
        ).strip()
        == source_head
    )
    assert report["identity"]["commit_sha"] == source_head
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert attempt["actual_verified_sha"] == source_head
    assert attempt["expected_sha"] == source_head
    assert attempt["container_port"] == 3000
    assert attempt["compose_path"] == "compose.yaml"
    assert attempt["stop_reason"] == "no_remaining_mutations"
    assert attempt["known_limitations"] == ["pid_hard_bound_unsupported"]
    assert f"attempt_evidence={run_path / 'attempt-result.json'}" in result.output
    assert (workspace / "compose.yaml").is_file()
    overlays = report["artifacts"]["experiment_overlays"]
    assert overlays
    for overlay in overlays:
        overlay_path = (workspace / overlay).resolve(strict=True)
        assert overlay_path.is_relative_to(workspace.resolve())
        assert overlay_path.is_file()
    assert (run_path / "report" / "trial-report.html").is_file()
    assert list((run_path / "evidence").rglob("*.json"))
    assert any(call[0] == "create" for call in provider.calls)
    assert any(call[0] == "publish_port" and call[2] == 3000 for call in provider.calls)
    assert len([call for call in provider.calls if call[0] == "create"]) == len(
        [call for call in provider.calls if call[0] == "destroy"]
    )
    assert provider.active_sandboxes == set()


def test_inspect_replays_operator_business_journey_for_baseline_and_candidates(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    operator_file = tmp_path / "operator-journeys.json"
    operator_file.write_text(
        json.dumps(
            {
                "journeys": [
                    {
                        "journey_id": "business-crud",
                        "name": "Operator CRUD flow",
                        "steps": [
                            {
                                "step_id": "create",
                                "tool": "http",
                                "action": "request",
                                "params": {
                                    "method": "POST",
                                    "path": "/items/fixture",
                                    "json": {"name": "operator"},
                                },
                                "assertions": [
                                    {
                                        "kind": "status_code",
                                        "target": "response.status",
                                        "expected": 201,
                                    }
                                ],
                            },
                            {
                                "step_id": "read",
                                "tool": "http",
                                "action": "request",
                                "params": {
                                    "method": "GET",
                                    "path": "/items/fixture",
                                },
                                "assertions": [
                                    {
                                        "kind": "status_code",
                                        "target": "response.status",
                                        "expected": 200,
                                    },
                                    {
                                        "kind": "json_path_equals",
                                        "target": "id",
                                        "expected": "fixture",
                                    },
                                ],
                            },
                            {
                                "step_id": "delete",
                                "tool": "http",
                                "action": "request",
                                "params": {
                                    "method": "DELETE",
                                    "path": "/items/fixture",
                                },
                                "assertions": [
                                    {
                                        "kind": "status_code",
                                        "target": "response.status",
                                        "expected": 204,
                                    }
                                ],
                            },
                            {
                                "step_id": "confirm-deleted",
                                "tool": "http",
                                "action": "request",
                                "params": {
                                    "method": "GET",
                                    "path": "/items/fixture",
                                },
                                "assertions": [
                                    {
                                        "kind": "status_code",
                                        "target": "response.status",
                                        "expected": 404,
                                    }
                                ],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    artifacts_root = tmp_path / "artifacts"
    source_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()

    with business_server() as port:
        provider = FixtureProvider(host_port=port, container_port=3000)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            [
                "inspect",
                str(source),
                "--provider",
                "fake",
                "--commit-sha",
                source_head,
                "--container-port",
                "3000",
                "--journeys-file",
                str(operator_file),
            ],
        )

    run_path = artifacts_root / FIXED_RUN_ID
    assert result.exit_code == 0, result.output
    report = json.loads(
        (run_path / "report" / "trial-report.json").read_text(encoding="utf-8")
    )
    assert report["coverage"]["journeys"] == [
        {
            "classification": "PASS",
            "journey_id": "business-crud",
            "name": "Operator CRUD flow",
        }
    ]
    assert report["operator_journey_provenance"]["source_kind"] == "operator-authored"
    assert report["operator_journey_provenance"]["schema_version"] == 1
    assert str(operator_file) not in json.dumps(report)
    assert not (run_path / "workspace" / operator_file.name).exists()
    operations = _BusinessHandler.operations
    chunks = [
        operations[index : index + 4]
        for index, operation in enumerate(operations)
        if operation == "POST"
    ]
    assert len(chunks) >= 2
    assert all(chunk == ["POST", "GET-200", "DELETE", "GET-404"] for chunk in chunks)


def test_relative_artifacts_root_uses_absolute_graph_context_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    invocation_cwd = tmp_path / "invocation-cwd"
    invocation_cwd.mkdir()
    monkeypatch.chdir(invocation_cwd)
    artifacts_root = Path("artifacts")
    contexts: list[GraphContext] = []
    original_ainvoke_run = cli.ainvoke_run

    async def capture_context(
        graph: object, state: RunState, *, context: GraphContext
    ) -> GraphState:
        contexts.append(context)
        return await original_ainvoke_run(graph, state, context=context)

    monkeypatch.setattr(cli, "ainvoke_run", capture_context)
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    relative_run_path = artifacts_root / FIXED_RUN_ID
    absolute_run_path = (invocation_cwd / relative_run_path).resolve(strict=True)
    assert result.exit_code == 0, result.output
    assert f"artifact_path={relative_run_path}" in result.output
    assert len(contexts) == 1
    context = contexts[0]
    assert context.workspace == absolute_run_path / "workspace"
    assert context.overlay_dir == context.workspace / ".repotrial-overlays"
    assert context.accepted_compose_dir == context.workspace / ".repotrial-accepted"
    assert context.artifact_dir == absolute_run_path / "evidence"
    assert all(
        path.is_absolute()
        for path in (
            context.workspace,
            context.overlay_dir,
            context.accepted_compose_dir,
            context.artifact_dir,
        )
    )
    assert context.overlay_dir.is_relative_to(context.workspace)
    assert context.accepted_compose_dir.is_relative_to(context.workspace)
    assert not context.artifact_dir.is_relative_to(context.workspace)
    assert (absolute_run_path / "report" / "trial-report.json").is_file()
    assert provider.active_sandboxes == set()


def test_graph_terminal_evidence_records_utc_and_monotonic_attempt_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def terminal_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(run=state.model_copy(update={"stop_reason": "completed"}))

    monkeypatch.setattr(cli, "ainvoke_run", terminal_run)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 3, result.output
    _assert_attempt_timing(_attempt_result(artifacts_root))


def test_start_timing_failure_persists_sanitized_evidence_without_running_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    graph_started = False

    def fail_start_clock() -> datetime:
        raise RuntimeError("clock secret=do-not-persist")

    async def unexpected_graph(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        nonlocal graph_started
        del graph, state, context
        graph_started = True
        raise AssertionError("timing failure reached graph execution")

    monkeypatch.setattr(cli, "_utc_now", fail_start_clock)
    monkeypatch.setattr(cli, "ainvoke_run", unexpected_graph)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == 4, result.output
    assert not graph_started
    assert attempt["exception_type"] == "EvidenceTimingError"
    assert attempt["stop_reason"] == "internal:evidence_timing_error"
    _assert_timing_error(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


@pytest.mark.parametrize("failure", ["end_utc", "end_monotonic"])
def test_terminal_timing_completion_failure_fails_closed_with_evidence(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    timestamps = iter(
        (
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 102.5))

    def utc_now() -> datetime:
        if failure == "end_utc" and len(calls) == 0:
            calls.append("start")
            return next(timestamps)
        if failure == "end_utc":
            raise RuntimeError("end UTC secret=do-not-persist")
        return next(timestamps)

    def monotonic() -> float:
        if failure == "end_monotonic" and len(calls) == 0:
            calls.append("start")
            return next(monotonic_values)
        if failure == "end_monotonic":
            raise RuntimeError("end monotonic secret=do-not-persist")
        return next(monotonic_values)

    calls: list[str] = []
    monkeypatch.setattr(cli, "_utc_now", utc_now)
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=monotonic))

    async def terminal_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(run=state.model_copy(update={"stop_reason": "completed"}))

    monkeypatch.setattr(cli, "ainvoke_run", terminal_run)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == 4, result.output
    assert attempt["exception_type"] == "EvidenceTimingError"
    assert attempt["stop_reason"] == "internal:evidence_timing_error"
    assert attempt["report_paths"]["json"] == str(
        artifacts_root / FIXED_RUN_ID / "report" / "trial-report.json"
    )
    assert attempt["report_paths"]["html"] == str(
        artifacts_root / FIXED_RUN_ID / "report" / "trial-report.html"
    )
    _assert_timing_error(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


def test_missing_graph_terminal_stop_reason_fails_closed_with_sanitized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def missing_stop_reason(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(run=state)

    monkeypatch.setattr(cli, "ainvoke_run", missing_stop_reason)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == 4, result.output
    assert attempt["stop_reason"] == "internal:valueerror"
    assert attempt["terminal_outcome"] == "exception"
    _assert_attempt_timing(attempt)


@pytest.mark.parametrize(
    ("raised", "expected_exit", "expected_stop_reason"),
    [
        (
            DockerSbxUnsupportedError("capability_missing"),
            2,
            "sandbox_unsupported:capability_missing",
        ),
        (RuntimeError("secret=never-persist"), 4, "internal:runtimeerror"),
    ],
)
def test_exception_attempt_evidence_records_timing_without_error_text(
    raised: Exception,
    expected_exit: int,
    expected_stop_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def failing_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, state, context
        raise raised

    monkeypatch.setattr(cli, "ainvoke_run", failing_run)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == expected_exit, result.output
    assert attempt["stop_reason"] == expected_stop_reason
    assert "never-persist" not in json.dumps(attempt)
    _assert_attempt_timing(attempt)


@pytest.mark.parametrize(
    ("raised", "expected_stop_reason", "expected_cli_exit"),
    [
        (asyncio.CancelledError(), "control:cancelled", None),
        (KeyboardInterrupt(), "control:keyboard_interrupt", 130),
        (SystemExit(17), "control:system_exit", 17),
        (SystemExit(), "control:system_exit", 0),
        (SystemExit("secret=do-not-persist"), "control:system_exit", 1),
        (SystemExit(True), "control:system_exit", 1),
        (SystemExit(False), "control:system_exit", 0),
    ],
)
def test_control_flow_exit_persists_sanitized_attempt_evidence_before_propagating(
    raised: BaseException,
    expected_stop_reason: str,
    expected_cli_exit: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def interrupted_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, state, context
        raise raised

    monkeypatch.setattr(cli, "ainvoke_run", interrupted_run)
    if expected_cli_exit is None:
        with pytest.raises(asyncio.CancelledError) as propagated:
            CliRunner().invoke(
                make_app(artifacts_root, FixtureProvider()),
                [
                    "inspect",
                    str(source),
                    "--provider",
                    "fake",
                    "--max-experiments",
                    "8",
                ],
            )
        assert propagated.value is raised
    else:
        result = CliRunner().invoke(
            make_app(artifacts_root, FixtureProvider()),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )
        assert result.exit_code == expected_cli_exit

    attempt = _attempt_result(artifacts_root)
    assert attempt["exception_type"] == type(raised).__name__
    assert attempt["stop_reason"] == expected_stop_reason
    assert attempt["known_limitations"] == ["pid_hard_bound_unsupported"]
    if isinstance(raised, SystemExit):
        assert attempt["exit_code"] == expected_cli_exit
        assert type(attempt["exit_code"]) is int
        assert "do-not-persist" not in json.dumps(attempt)
    _assert_attempt_timing(attempt)


@pytest.mark.parametrize(
    ("raised", "expected_stop_reason", "expected_cli_exit"),
    [
        (asyncio.CancelledError(), "control:cancelled", None),
        (KeyboardInterrupt(), "control:keyboard_interrupt", 130),
        (SystemExit(17), "control:system_exit", 17),
    ],
)
def test_control_flow_timing_failure_persists_evidence_without_replacing_control_flow(
    raised: BaseException,
    expected_stop_reason: str,
    expected_cli_exit: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    start = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    monotonic_values = iter((100.0,))

    def utc_now() -> datetime:
        if len(calls) == 0:
            calls.append("start")
            return start
        raise RuntimeError("end UTC secret=do-not-persist")

    calls: list[str] = []
    monkeypatch.setattr(cli, "_utc_now", utc_now)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    async def interrupted_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, state, context
        raise raised

    monkeypatch.setattr(cli, "ainvoke_run", interrupted_run)
    if expected_cli_exit is None:
        with pytest.raises(asyncio.CancelledError) as propagated:
            CliRunner().invoke(
                make_app(artifacts_root, FixtureProvider()),
                [
                    "inspect",
                    str(source),
                    "--provider",
                    "fake",
                    "--max-experiments",
                    "8",
                ],
            )
        assert propagated.value is raised
    else:
        result = CliRunner().invoke(
            make_app(artifacts_root, FixtureProvider()),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )
        assert result.exit_code == expected_cli_exit

    attempt = _attempt_result(artifacts_root)
    assert attempt["exception_type"] == type(raised).__name__
    assert attempt["stop_reason"] == expected_stop_reason
    _assert_timing_error(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


def test_inspect_rejects_unfrozen_experiment_budget_before_artifacts(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    provider = FixtureProvider()

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "7"],
    )

    assert result.exit_code != 0
    assert not artifacts_root.exists()
    assert provider.calls == []


def test_dry_run_rejects_unfrozen_experiment_budget_before_artifacts(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        [
            "inspect",
            "--dry-run",
            "--max-experiments",
            "7",
            "https://github.com/a/b",
        ],
    )

    assert result.exit_code == 2, result.output
    assert not artifacts_root.exists()


def test_inspect_returns_unsupported_for_a_capability_unavailable_trial(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port, candidate_boot_unavailable=True)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    assert result.exit_code == 2, result.output
    assert provider.active_sandboxes == set()


def test_inspect_returns_trial_failed_for_a_baseline_failure(tmp_path: Path) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    provider = FixtureProvider(baseline_healthy=False)

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 3, result.output
    assert provider.active_sandboxes == set()


def test_inspect_returns_internal_error_for_an_unexpected_provider_failure(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    provider = FixtureProvider(
        create_error=RuntimeError("unexpected fixture failure secret=do-not-persist")
    )

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 4, result.output
    attempt = json.loads(
        (artifacts_root / FIXED_RUN_ID / "attempt-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt["exception_type"] == "RuntimeError"
    assert attempt["stop_reason"] == "internal:runtimeerror"
    assert "unexpected fixture failure" not in json.dumps(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


def test_inspect_rejects_pinner_sha_mismatch_before_provider_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    expected_sha = "a" * 40
    actual_sha = "b" * 40
    provider = FixtureProvider()

    async def mismatched_pinner(
        url: str, destination: Path, requested_ref: str | None = None
    ) -> PinnedRepo:
        assert requested_ref == expected_sha
        destination.mkdir()
        return PinnedRepo(
            repo=RepoRef(
                url=url,
                owner="owner",
                repo="repo",
                requested_ref=requested_ref,
            ),
            commit_sha=actual_sha,
            local_path=destination,
        )

    monkeypatch.setattr(cli, "pin_repository", mismatched_pinner)
    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        [
            "inspect",
            "--provider",
            "fake",
            "--commit-sha",
            expected_sha,
            "https://github.com/owner/repo",
        ],
    )

    attempt = json.loads(
        (artifacts_root / FIXED_RUN_ID / "attempt-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.exit_code == 4, result.output
    assert provider.calls == []
    assert attempt["actual_verified_sha"] == actual_sha
    assert attempt["stop_reason"] == "intake:commit_sha_mismatch"


def test_inspect_returns_internal_error_when_report_rendering_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    def fail_render(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unexpected report failure")

    monkeypatch.setattr(cli, "render_trial_report", fail_render)
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    assert result.exit_code == 4, result.output


def test_inspect_returns_unsupported_for_an_unsupported_baseline_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    async def unsupported_baseline(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(
            run=state.model_copy(
                update={
                    "baseline_journey_results": [
                        JourneyResult(
                            journey_id="unsupported-baseline",
                            verdict=Verdict.UNSUPPORTED,
                            passed_steps=0,
                            total_steps=1,
                        )
                    ],
                    "stop_reason": "insufficient_coverage",
                }
            )
        )

    monkeypatch.setattr(cli, "ainvoke_run", unsupported_baseline)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 2, result.output


def test_inspect_derives_a_baseline_journey_from_the_pinned_root_readme(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    (source / "repotrial.journeys.json").unlink()
    (source / "README.md").write_text("[health](/health)\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "repotrial.journeys.json", "README.md"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "root readme journey"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    artifacts_root = tmp_path / "artifacts"

    with healthy_server() as port:
        result = CliRunner().invoke(
            make_app(artifacts_root, FixtureProvider(host_port=port)),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    assert result.exit_code == 0, result.output
    report = json.loads(
        (artifacts_root / FIXED_RUN_ID / "report" / "trial-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["coverage"]["journeys"] == [
        {
            "classification": "PASS",
            "journey_id": "readme-1",
            "name": "GET /health",
        }
    ]


def test_inspect_returns_unsupported_for_an_unsupported_experiment_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    async def unsupported_experiment(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(
            run=state.model_copy(
                update={
                    "baseline_journey_results": [
                        JourneyResult(
                            journey_id="baseline",
                            verdict=Verdict.PASS,
                            passed_steps=1,
                            total_steps=1,
                        )
                    ],
                    "experiments": [
                        ExperimentRecord(
                            experiment_id="unsupported-experiment",
                            parent_config_hash="sha256:parent",
                            candidate_config_hash="sha256:candidate",
                            mutation=Mutation(
                                mutation_id="policy:set_non_root:web",
                                type=MutationType.SET_NON_ROOT,
                                service="web",
                            ),
                            boot=Verdict.PASS,
                            journeys=[
                                JourneyResult(
                                    journey_id="unsupported-experiment",
                                    verdict=Verdict.UNSUPPORTED,
                                    passed_steps=0,
                                    total_steps=1,
                                )
                            ],
                            verdict=ExperimentVerdict.STOP,
                            reason="journey_unsupported",
                        )
                    ],
                    "stop_reason": "experiment:journey_unsupported",
                }
            )
        )

    monkeypatch.setattr(cli, "ainvoke_run", unsupported_experiment)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 2, result.output


def test_inspect_rejects_uninjected_fake_provider_before_artifacts(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        create_app(artifacts_root=artifacts_root, run_id_generator=fixed_run_id),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 2, result.output
    assert "--provider fake is unavailable without test injection" in result.output
    assert not artifacts_root.exists()


def test_inspect_returns_internal_error_when_run_layout_creation_fails(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    (artifacts_root / FIXED_RUN_ID).mkdir(parents=True)
    provider = FixtureProvider()

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 4, result.output
    assert "inspect failed: FileExistsError" in result.output
    assert provider.calls == []
