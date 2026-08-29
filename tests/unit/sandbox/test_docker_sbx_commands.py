import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from repotrial.sandbox import docker_sbx
from repotrial.sandbox.base import ExecResult, NetworkLogResult
from repotrial.sandbox.docker_sbx import (
    DOCKER_FLOOR_MB,
    MANDATORY_DENY_NETWORK,
    ROOT_FLOOR_MB,
    WORKSPACE_FLOOR_MB,
    DockerSbxError,
    DockerSbxPolicy,
    DockerSbxProvider,
    DockerSbxUnsupportedError,
    calculate_disk_allocation,
)
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.sandbox.lifecycle import CleanupError, managed_sandbox

CREATE_FLAGS = (
    "--name",
    "--clone",
    "--cpus",
    "--memory",
    "--deny-network",
)
HELP_OUTPUTS = {
    ("sbx", "create", "--help"): "Usage: sbx create [flags] AGENT PATH\n"
    + " ".join(CREATE_FLAGS),
    ("sbx", "create", "shell", "--help"): ("Usage: sbx create [flags] shell [PATH]"),
    ("sbx", "exec", "--help"): "Usage: sbx exec SANDBOX -- COMMAND [ARG...]",
    ("sbx", "ports", "--help"): "Usage: sbx ports SANDBOX [--publish PORT] [--json]",
    ("sbx", "cp", "--help"): "Usage: sbx cp SRC DST",
    ("sbx", "rm", "--help"): "Usage: sbx rm --force SANDBOX",
    ("sbx", "policy", "allow", "network", "--help"): (
        "Usage: sbx policy allow network [--sandbox SANDBOX] RESOURCES\n"
        'Use "**" to allow all hosts.'
    ),
    ("sbx", "policy", "log", "--help"): (
        "Usage: sbx policy log SANDBOX --type network --json"
    ),
}
PROBE_CALLS = [
    ("sbx", "version"),
    ("sbx", "create", "--help"),
    ("sbx", "create", "shell", "--help"),
    ("sbx", "exec", "--help"),
    ("sbx", "ports", "--help"),
    ("sbx", "cp", "--help"),
    ("sbx", "rm", "--help"),
    ("sbx", "policy", "allow", "network", "--help"),
    ("sbx", "policy", "log", "--help"),
]


@dataclass(frozen=True)
class _Outcome:
    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    hang: bool = False
    kill_error: OSError | None = None
    reap_hang: bool = False
    reap_error: OSError | None = None


class _FakeStream:
    def __init__(self, data: bytes, release: asyncio.Event) -> None:
        self._data = data
        self._offset = 0
        self._release = release

    async def read(self, size: int = -1) -> bytes:
        if self._release.is_set() is False:
            await self._release.wait()
        if self._offset >= len(self._data):
            return b""
        end = len(self._data) if size < 0 else self._offset + size
        chunk = self._data[self._offset : end]
        self._offset += len(chunk)
        return chunk


class _ScriptedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.read_count = 0

    async def read(self, _: int = -1) -> bytes:
        self.read_count += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeProcess:
    def __init__(self, outcome: _Outcome) -> None:
        self._outcome = outcome
        self._release = asyncio.Event()
        if not outcome.hang:
            self._release.set()
        self.stdout = _FakeStream(outcome.stdout, self._release)
        self.stderr = _FakeStream(outcome.stderr, self._release)
        self.returncode: int | None = None
        self.killed = False
        self.waited = False
        self.reap_release = asyncio.Event()

    async def wait(self) -> int:
        await self._release.wait()
        if self.killed and self._outcome.reap_hang:
            await self.reap_release.wait()
        if self.killed and self._outcome.reap_error is not None:
            raise self._outcome.reap_error
        self.waited = True
        if self.returncode is None:
            self.returncode = self._outcome.returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self._outcome.kill_error is not None:
            raise self._outcome.kill_error
        self.returncode = -9
        self._release.set()


class _SbxSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []
        self.processes: list[_FakeProcess] = []
        self.overrides: dict[tuple[str, ...], _Outcome | BaseException] = {}
        self.handler: Callable[[tuple[str, ...]], _Outcome | BaseException] | None = (
            None
        )
        self.before_spawn: Callable[[tuple[str, ...]], None] | None = None

    async def __call__(self, *argv: str, **kwargs: object) -> _FakeProcess:
        command = tuple(argv)
        if self.before_spawn is not None:
            self.before_spawn(command)
        self.calls.append(command)
        self.kwargs.append(kwargs)
        outcome: _Outcome | BaseException
        if command in self.overrides:
            outcome = self.overrides[command]
        elif command == ("sbx", "version"):
            outcome = _Outcome(stdout=b"sbx 99.0.0\n")
        elif command in HELP_OUTPUTS:
            outcome = _Outcome(stdout=HELP_OUTPUTS[command].encode())
        elif self.handler is not None:
            outcome = self.handler(command)
        else:
            outcome = _Outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        process = _FakeProcess(outcome)
        self.processes.append(process)
        return process


class _Clock:
    def __init__(self, value: float = 0) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


class _RecordedTimeout:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _TimeoutRecorder:
    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, value: float) -> _RecordedTimeout:
        self.values.append(value)
        return _RecordedTimeout()


class _TimeoutAfterProcessCreation:
    def __init__(self, process_created: asyncio.Event) -> None:
        self._process_created = process_created
        self._canceller: asyncio.Task[None] | None = None
        self._task: asyncio.Task[object] | None = None
        self._timed_out = False

    async def __aenter__(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._task = cast(asyncio.Task[object], task)
        self._canceller = asyncio.create_task(self._cancel_after_process_creation())

    async def __aexit__(self, *args: object) -> bool:
        if self._canceller is not None and not self._canceller.done():
            self._canceller.cancel()
            try:
                await self._canceller
            except asyncio.CancelledError:
                pass
        if self._timed_out and args[0] is asyncio.CancelledError:
            assert self._task is not None
            self._task.uncancel()
            raise TimeoutError
        return False

    async def _cancel_after_process_creation(self) -> None:
        await self._process_created.wait()
        assert self._task is not None
        self._timed_out = True
        self._task.cancel()


def _install_deterministic_clock(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> _TimeoutRecorder:
    monkeypatch.setattr(
        docker_sbx,
        "time",
        SimpleNamespace(monotonic=clock.monotonic),
        raising=False,
    )
    recorder = _TimeoutRecorder()
    monkeypatch.setattr(docker_sbx.asyncio, "timeout", recorder)
    return recorder


def _timeouts_by_command(
    spawner: _SbxSpawner, recorder: _TimeoutRecorder
) -> list[tuple[tuple[str, ...], float]]:
    return list(zip(spawner.calls, recorder.values, strict=True))


def _policy(
    *, deny_network: frozenset[str] = frozenset(), total_duration_s: int = 300
) -> DockerSbxPolicy:
    return DockerSbxPolicy(
        cpus=1,
        memory_mb=512,
        pids_limit=64,
        disk_mb=2048,
        total_duration_s=total_duration_s,
        deny_network=deny_network,
    )


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    spawner: _SbxSpawner,
    *,
    command_timeout_s: float = 5,
    total_duration_s: int = 300,
) -> DockerSbxProvider:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    provider = DockerSbxProvider(
        _policy(
            deny_network=frozenset({"example.internal"}),
            total_duration_s=total_duration_s,
        ),
        command_timeout_s=command_timeout_s,
    )
    return provider


def _create(provider: DockerSbxProvider, workspace: Path) -> str:
    return asyncio.run(provider.create(workspace, "trial"))


def _actual_create_call(spawner: _SbxSpawner) -> tuple[str, ...]:
    return next(
        call
        for call in spawner.calls
        if call[:2] == ("sbx", "create") and call[-1] != "--help"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("cpus", 0),
        ("cpus", float("inf")),
        ("memory_mb", 0),
        ("pids_limit", -1),
        ("disk_mb", 0),
        ("total_duration_s", False),
    ],
)
def test_policy_rejects_non_positive_or_non_finite_limits(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "cpus": 1,
        "memory_mb": 512,
        "pids_limit": 64,
        "disk_mb": 2048,
        "total_duration_s": 300,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError), match=field):
        DockerSbxPolicy(**values)


@pytest.mark.parametrize("cpus", [1.0, 1.5])
def test_policy_rejects_non_integer_cpu_values(cpus: float) -> None:
    with pytest.raises(ValueError, match="cpus must be a positive integer"):
        DockerSbxPolicy(
            cpus=cpus,
            memory_mb=512,
            pids_limit=64,
            disk_mb=2048,
            total_duration_s=300,
        )


def test_policy_is_immutable_and_mandatory_denies_cannot_be_removed() -> None:
    policy = _policy(deny_network=frozenset({"api.example.test"}))
    required_resources = {
        "10.0.0.0/8",
        "100.100.100.200/32",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "169.254.169.254/32",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "gateway.docker.internal",
        "host-gateway",
        "host.containers.internal",
        "host.docker.internal",
        "metadata.google.internal",
    }

    assert policy.deny_network == MANDATORY_DENY_NETWORK | {"api.example.test"}
    assert required_resources <= policy.deny_network
    with pytest.raises((AttributeError, TypeError)):
        policy.memory_mb = 1024


@pytest.mark.parametrize(
    ("disk_mb", "expected_docker_mb"),
    [(7, 1), (8, 2), (2048, 2042), (4096, 4090)],
)
def test_disk_allocation_preserves_budget_and_uses_calibrated_floors(
    disk_mb: int, expected_docker_mb: int
) -> None:
    allocation = calculate_disk_allocation(disk_mb)

    assert ROOT_FLOOR_MB == 1
    assert DOCKER_FLOOR_MB == 1
    assert WORKSPACE_FLOOR_MB == 5
    assert allocation.root_mb == 1
    assert allocation.docker_mb == expected_docker_mb
    assert allocation.workspace_mb == 5
    assert (
        allocation.root_mb + allocation.docker_mb + allocation.workspace_mb == disk_mb
    )


def test_disk_allocation_rejects_budget_below_calibrated_floors() -> None:
    with pytest.raises(DockerSbxUnsupportedError, match="disk_budget_insufficient"):
        calculate_disk_allocation(
            ROOT_FLOOR_MB + WORKSPACE_FLOOR_MB + DOCKER_FLOOR_MB - 1
        )


def test_insufficient_disk_budget_fails_before_any_sbx_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    provider = DockerSbxProvider(
        DockerSbxPolicy(
            cpus=1,
            memory_mb=512,
            pids_limit=64,
            disk_mb=6,
            total_duration_s=300,
        )
    )

    with pytest.raises(DockerSbxUnsupportedError, match="disk_budget_insufficient"):
        _create(provider, tmp_path)

    assert spawner.calls == []


@pytest.mark.parametrize("bad_resource", ["", "two hosts", "line\nbreak"])
def test_policy_rejects_ambiguous_network_deny_resources(bad_resource: str) -> None:
    with pytest.raises(ValueError, match="deny_network"):
        _policy(deny_network=frozenset({bad_resource}))


def test_missing_sbx_executable_is_explicitly_unsupported_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "version")] = FileNotFoundError()
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == "sbx_unavailable"
    assert spawner.calls == [("sbx", "version")]


def test_nonzero_probe_preserves_bounded_stderr_on_unsupported_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "version")] = _Outcome(returncode=2, stderr=b"x" * 70_000)
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == "version_probe_failed"
    assert raised.value.stderr.count("\n...[truncated]") == 1
    assert raised.value.stderr.endswith("x" * 64)
    assert len(raised.value.stderr.encode()) <= 65_536


@pytest.mark.parametrize(
    "failed_call",
    [
        ("sbx", "version"),
        ("sbx", "create", "--help"),
        ("sbx", "create", "shell", "--help"),
        ("sbx", "exec", "--help"),
        ("sbx", "ports", "--help"),
        ("sbx", "cp", "--help"),
        ("sbx", "rm", "--help"),
        ("sbx", "policy", "allow", "network", "--help"),
    ],
)
def test_nonzero_required_probe_fails_closed_before_create(
    failed_call: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[failed_call] = _Outcome(returncode=2, stderr=b"not supported")
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError, match="unsupported"):
        _create(provider, tmp_path)

    assert _non_help_create_calls(spawner) == []


def test_required_probe_propagates_unconfirmed_process_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repotrial.sandbox.docker_sbx.REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "create", "--help")] = _Outcome(
        hang=True, kill_error=OSError("kill denied")
    )
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert raised.value.operation == "probe_create"
    assert raised.value.reason == "process_cleanup_unconfirmed"
    assert "kill denied" in raised.value.cleanup_error
    assert "reap_timeout" in raised.value.cleanup_error
    assert _non_help_create_calls(spawner) == []


def test_required_probe_confirmed_timeout_remains_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "create", "--help")] = _Outcome(hang=True)
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    with pytest.raises(DockerSbxUnsupportedError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == "create_probe_failed"
    assert any(process.killed and process.waited for process in spawner.processes)
    assert _non_help_create_calls(spawner) == []


@pytest.mark.parametrize("missing_flag", CREATE_FLAGS)
def test_each_missing_create_boundary_fails_closed_without_target_execution(
    missing_flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "target-must-not-run"
    spawner = _SbxSpawner()
    help_text = HELP_OUTPUTS[("sbx", "create", "--help")].replace(missing_flag, "")
    spawner.overrides[("sbx", "create", "--help")] = _Outcome(stdout=help_text.encode())
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == f"create_missing_capability:{missing_flag}"
    assert _non_help_create_calls(spawner) == []
    assert not marker.exists()
    assert all(call[0] == "sbx" for call in spawner.calls)


@pytest.mark.parametrize(
    ("help_call", "missing_token"),
    [
        (("sbx", "create", "shell", "--help"), "PATH"),
        (("sbx", "ports", "--help"), "--publish"),
        (("sbx", "ports", "--help"), "--json"),
        (("sbx", "rm", "--help"), "--force"),
    ],
)
def test_missing_required_operation_flag_prevents_create(
    help_call: tuple[str, ...],
    missing_token: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[help_call] = _Outcome(
        stdout=HELP_OUTPUTS[help_call].replace(missing_token, "").encode()
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError):
        _create(provider, tmp_path)

    assert _non_help_create_calls(spawner) == []


@pytest.mark.parametrize("missing_token", ["--sandbox", '"**"'])
def test_missing_policy_allow_network_capability_prevents_create(
    missing_token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    help_call = ("sbx", "policy", "allow", "network", "--help")
    spawner = _SbxSpawner()
    help_text = HELP_OUTPUTS[help_call].replace(missing_token, "")
    spawner.overrides[help_call] = _Outcome(stdout=help_text.encode())
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == (
        f"policy_allow_network_missing_capability:{missing_token}"
    )
    assert _non_help_create_calls(spawner) == []


def test_current_exec_help_shape_allows_create_with_argv_delimiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "exec", "--help")] = _Outcome(
        stdout=b"Usage: sbx exec [flags] SANDBOX COMMAND [ARG...]"
    )
    provider = _provider(monkeypatch, spawner)

    _create(provider, tmp_path)

    assert len(_non_help_create_calls(spawner)) == 1


def test_supported_capabilities_allow_create_without_pid_hard_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    _create(provider, tmp_path)

    assert len(_non_help_create_calls(spawner)) == 1


def test_successful_probe_builds_exact_policy_create_argv_and_owns_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk_keys = {
        "DOCKER_SANDBOXES_ROOT_SIZE",
        "DOCKER_SANDBOXES_DOCKER_SIZE",
        "DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE",
    }
    for key in disk_keys:
        monkeypatch.setenv(key, "host-value-must-not-leak")
    monkeypatch.setenv("REPOTRIAL_TEST_SENTINEL", "preserved")
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    sandbox_id = _create(provider, tmp_path)

    assert spawner.calls[: len(PROBE_CALLS)] == PROBE_CALLS
    expected = [
        "sbx",
        "create",
        "--name",
        sandbox_id,
        "--clone",
        "--cpus",
        "1",
        "--memory",
        "512m",
    ]
    for resource in sorted(MANDATORY_DENY_NETWORK | {"example.internal"}):
        expected.extend(("--deny-network", resource))
    expected.extend(("shell", str(tmp_path)))
    assert _actual_create_call(spawner) == tuple(expected)
    create_index = spawner.calls.index(tuple(expected))
    assert spawner.calls[create_index + 1] == (
        "sbx",
        "policy",
        "allow",
        "network",
        "--sandbox",
        sandbox_id,
        "**",
    )
    assert "--pids-limit" not in _actual_create_call(spawner)
    assert sandbox_id.startswith("repotrial-trial-")
    assert all("shell" not in kwargs for kwargs in spawner.kwargs)
    create_env = cast(dict[str, str], spawner.kwargs[create_index].get("env"))
    assert create_env["DOCKER_SANDBOXES_ROOT_SIZE"] == "1m"
    assert create_env["DOCKER_SANDBOXES_DOCKER_SIZE"] == "2042m"
    assert create_env["DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE"] == "5m"
    assert create_env["REPOTRIAL_TEST_SENTINEL"] == "preserved"
    for index, kwargs in enumerate(spawner.kwargs):
        if index == create_index:
            continue
        environment = cast(dict[str, str], kwargs.get("env"))
        assert environment["REPOTRIAL_TEST_SENTINEL"] == "preserved"
        assert disk_keys.isdisjoint(environment)
    assert not any(
        call[:2] == ("sbx", "exec")
        and any("df -B1 -P" in argument for argument in call)
        for call in spawner.calls
    )


@pytest.mark.parametrize(
    ("failure", "outcome", "expected_reason"),
    [
        ("nonzero", _Outcome(returncode=7, stderr=b"allow failed"), "nonzero_exit"),
        ("timeout", _Outcome(hang=True), "timeout"),
        ("io", OSError("allow io"), "io_error"),
    ],
)
def test_policy_allow_failure_force_removes_pending_sandbox_without_deadline(
    failure: str,
    outcome: _Outcome | BaseException,
    expected_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()

    def handle(command: tuple[str, ...]) -> _Outcome | BaseException:
        if command[:5] == ("sbx", "policy", "allow", "network", "--sandbox"):
            return outcome
        return _Outcome()

    spawner.handler = handle
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=0.05 if failure == "timeout" else 5,
    )

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    allow_call = (
        "sbx",
        "policy",
        "allow",
        "network",
        "--sandbox",
        sandbox_id,
        "**",
    )
    assert (raised.value.operation, raised.value.reason) == (
        "allow_network",
        expected_reason,
    )
    assert spawner.calls[-3:] == [
        create_call,
        allow_call,
        ("sbx", "rm", "--force", sandbox_id),
    ]
    assert provider._sandbox_deadlines == {}


def test_cancelled_policy_allow_force_removes_pending_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True)
        if command[:5] == ("sbx", "policy", "allow", "network", "--sandbox")
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        task = asyncio.create_task(provider.create(tmp_path, "trial"))
        while not any(
            call[:5] == ("sbx", "policy", "allow", "network", "--sandbox")
            for call in spawner.calls
        ):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert spawner.processes[-2].killed is True
    assert spawner.processes[-2].waited is True
    assert provider._sandbox_deadlines == {}


def test_optional_network_log_probe_does_not_weaken_create_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "policy", "log", "--help")] = _Outcome(returncode=1)
    provider = _provider(monkeypatch, spawner)

    sandbox_id = _create(provider, tmp_path)
    calls_before_log = len(spawner.calls)
    result = asyncio.run(provider.network_log(sandbox_id))

    assert result == NetworkLogResult(
        events=[],
        supported=False,
        unsupported_reason="network_log_capability_unavailable",
    )
    assert len(spawner.calls) == calls_before_log


def test_optional_network_log_probe_cleanup_failure_aborts_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repotrial.sandbox.docker_sbx.REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "policy", "log", "--help")] = _Outcome(
        hang=True, kill_error=OSError("kill denied")
    )
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert raised.value.operation == "probe_network_log"
    assert raised.value.reason == "process_cleanup_unconfirmed"
    assert "kill denied" in raised.value.cleanup_error
    assert "reap_timeout" in raised.value.cleanup_error
    assert _non_help_create_calls(spawner) == []


def test_optional_network_log_probe_confirmed_timeout_remains_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[("sbx", "policy", "log", "--help")] = _Outcome(hang=True)
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    sandbox_id = _create(provider, tmp_path)
    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_capability_unavailable"
    assert any(process.killed and process.waited for process in spawner.processes)
    assert len(_non_help_create_calls(spawner)) == 1


def test_failed_create_force_removes_pending_id_and_cleaned_destroy_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(returncode=7, stderr=b"create failed")
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    remove_call = ("sbx", "rm", "--force", sandbox_id)
    assert raised.value.reason == "nonzero_exit"
    assert spawner.calls[-1] == remove_call
    calls_before_destroy = len(spawner.calls)
    asyncio.run(provider.destroy(sandbox_id))
    assert len(spawner.calls) == calls_before_destroy


def test_failed_create_cleanup_failure_is_visible_and_retained_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            return _Outcome(returncode=7, stderr=b"create failed")
        if command[:3] == ("sbx", "rm", "--force"):
            return _Outcome(returncode=8, stderr=b"cleanup failed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert raised.value.reason == "nonzero_exit"
    context = raised.value.partial_create_cleanup
    assert context.create_failure is raised.value
    assert context.sandbox_id == sandbox_id
    assert "cleanup failed" in str(context.cleanup_failure)

    spawner.handler = lambda command: _Outcome()
    asyncio.run(provider.destroy(sandbox_id))
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_cancelled_create_force_removes_pending_id_and_preserves_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True)
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        task = asyncio.create_task(provider.create(tmp_path, "trial"))
        while not _non_help_create_calls(spawner):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert spawner.processes[len(PROBE_CALLS)].killed is True
    assert spawner.processes[len(PROBE_CALLS)].waited is True


@pytest.mark.parametrize("failure", ["timeout", "io_error"])
def test_create_timeout_or_io_error_force_removes_pending_id(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        (_Outcome(hang=True) if failure == "timeout" else OSError("create io"))
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else _Outcome()
    )
    provider = _provider(
        monkeypatch, spawner, command_timeout_s=0.05 if failure == "timeout" else 5
    )

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert raised.value.reason == failure
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_cancelled_create_cleanup_failure_reports_id_and_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True)
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else (
            _Outcome(returncode=9, stderr=b"cleanup blocked")
            if command[:3] == ("sbx", "rm", "--force")
            else _Outcome()
        )
    )
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        task = asyncio.create_task(provider.create(tmp_path, "trial"))
        while not _non_help_create_calls(spawner):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        create_call = _actual_create_call(spawner)
        sandbox_id = create_call[create_call.index("--name") + 1]
        notes = " ".join(raised.value.__notes__)
        assert sandbox_id in notes
        assert "cleanup blocked" in notes
        spawner.handler = lambda command: _Outcome()
        await provider.destroy(sandbox_id)

    asyncio.run(exercise())


def test_managed_sandbox_retries_partial_create_cleanup_and_preserves_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    remove_attempts = 0

    def outcome(command: tuple[str, ...]) -> _Outcome:
        nonlocal remove_attempts
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            return _Outcome(returncode=7, stderr=b"create failed")
        if command[:3] == ("sbx", "rm", "--force"):
            remove_attempts += 1
            if remove_attempts == 1:
                return _Outcome(returncode=8, stderr=b"initial cleanup failed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = tmp_path / "lifecycle.jsonl"

    async def exercise() -> None:
        async with managed_sandbox(
            provider, workspace, "trial", lifecycle_artifact=artifact
        ):
            raise AssertionError("partial create must not yield")

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(exercise())

    create_failure = raised.value
    context = create_failure.partial_create_cleanup
    sandbox_id = context.sandbox_id
    assert create_failure.reason == "nonzero_exit"
    assert context.create_failure is create_failure
    assert context.cleanup_failure.operation == "cleanup"
    assert remove_attempts == 2
    events = [json.loads(line) for line in artifact.read_text().splitlines()]
    assert events[1:] == [
        {
            "cleanup_exception_type": "DockerSbxError",
            "event": "create_cleanup_unsafe",
            "exception_type": "DockerSbxError",
            "sandbox_id": sandbox_id,
            "state": "cleanup_unsafe",
        },
        {"event": "cleanup_retry_attempt", "sandbox_id": sandbox_id},
        {"event": "cleanup_retry_success", "sandbox_id": sandbox_id},
    ]
    calls_before_repeat = len(spawner.calls)
    asyncio.run(provider.destroy(sandbox_id))
    assert len(spawner.calls) == calls_before_repeat


def test_managed_sandbox_retries_cancelled_partial_create_and_preserves_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    remove_attempts = 0

    def outcome(command: tuple[str, ...]) -> _Outcome:
        nonlocal remove_attempts
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            return _Outcome(hang=True)
        if command[:3] == ("sbx", "rm", "--force"):
            remove_attempts += 1
            if remove_attempts == 1:
                return _Outcome(returncode=9, stderr=b"cleanup blocked")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = tmp_path / "lifecycle.jsonl"

    async def exercise() -> asyncio.CancelledError:
        async def manage() -> None:
            async with managed_sandbox(
                provider, workspace, "trial", lifecycle_artifact=artifact
            ):
                raise AssertionError("partial create must not yield")

        task = asyncio.create_task(manage())
        while not _non_help_create_calls(spawner):
            await asyncio.sleep(0)
        task.cancel("original create cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        return raised.value

    cancellation = asyncio.run(exercise())
    context = cancellation.partial_create_cleanup
    assert context.create_failure is cancellation
    assert cancellation.args == ("original create cancellation",)
    assert remove_attempts == 2
    events = [json.loads(line) for line in artifact.read_text().splitlines()]
    assert events[1]["event"] == "create_cleanup_unsafe"
    assert events[1]["state"] == "cleanup_unsafe"
    assert events[1]["sandbox_id"] == context.sandbox_id
    assert events[-1] == {
        "event": "cleanup_retry_success",
        "sandbox_id": context.sandbox_id,
    }


def test_managed_sandbox_partial_create_retry_failure_retains_all_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            return _Outcome(returncode=7, stderr=b"create failed")
        if command[:3] == ("sbx", "rm", "--force"):
            return _Outcome(returncode=8, stderr=b"cleanup failed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = tmp_path / "lifecycle.jsonl"

    async def exercise() -> None:
        async with managed_sandbox(
            provider, workspace, "trial", lifecycle_artifact=artifact
        ):
            raise AssertionError("partial create must not yield")

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    cleanup_error = raised.value
    assert cleanup_error.create_failure.reason == "nonzero_exit"
    assert cleanup_error.initial_cleanup_failure.operation == "cleanup"
    assert cleanup_error.destroy_failure.operation == "destroy"
    assert cleanup_error.create_failure.partial_create_cleanup.create_failure is (
        cleanup_error.create_failure
    )
    assert cleanup_error.audit_failures == ()
    assert cleanup_error.secondary_failures == ()
    sandbox_id = cleanup_error.sandbox_id
    events = [json.loads(line) for line in artifact.read_text().splitlines()]
    assert events[-1] == {
        "event": "cleanup_retry_failure",
        "exception_type": "DockerSbxError",
        "sandbox_id": sandbox_id,
    }
    spawner.handler = lambda command: _Outcome()
    asyncio.run(provider.destroy(sandbox_id))
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_exec_preserves_argv_boundaries_and_never_runs_target_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "must-not-exist"
    argv = ["python", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(stdout=b"inside\n") if command[:2] == ("sbx", "exec") else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    sandbox_id = _create(provider, tmp_path)
    result = asyncio.run(provider.exec(sandbox_id, argv, timeout_s=7))

    assert result == ExecResult(exit_code=0, stdout="inside\n", stderr="")
    assert spawner.calls[-1] == ("sbx", "exec", sandbox_id, "--", *argv)
    assert not marker.exists()
    assert "shell" not in spawner.kwargs[-1]


@pytest.mark.parametrize("argv", [("whoami",), ["ok", 1], [], "whoami"])
def test_exec_rejects_invalid_argv_before_subprocess(
    argv: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    call_count = len(spawner.calls)

    with pytest.raises((TypeError, ValueError), match="argv"):
        asyncio.run(provider.exec(sandbox_id, cast(list[str], argv)))

    assert len(spawner.calls) == call_count


def test_unknown_ids_are_rejected_by_every_operation_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="not active"):
            await provider.exec("external", ["true"])
        with pytest.raises(RuntimeError, match="not active"):
            await provider.publish_port("external", 8080)
        with pytest.raises(RuntimeError, match="not active"):
            await provider.copy("external", "/tmp/a", tmp_path / "a")
        with pytest.raises(RuntimeError, match="not active"):
            await provider.network_log("external")
        with pytest.raises(RuntimeError, match="not active"):
            await provider.destroy("external")

    asyncio.run(exercise())
    assert spawner.calls == []


def test_exec_returns_nonzero_result_and_bounds_both_output_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(returncode=23, stdout=b"o" * 70_000, stderr=b"e" * 70_000)
        if command[:2] == ("sbx", "exec")
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    result = asyncio.run(provider.exec(sandbox_id, ["failing-command"]))

    assert result.exit_code == 23
    assert result.stdout.count("\n...[truncated]") == 1
    assert result.stderr.count("\n...[truncated]") == 1
    assert result.stdout.endswith("o" * 64)
    assert result.stderr.endswith("e" * 64)
    assert len(result.stdout.encode()) <= 65_536
    assert len(result.stderr.encode()) <= 65_536


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_read_bounded_retains_literal_head_and_tail_after_truncation(
    stream_name: str,
) -> None:
    stream = _ScriptedStream(
        [
            b"literal-head-" + (b"x" * 65_536),
            (b"y" * 65_536) + b"-literal-tail",
        ]
    )

    result = asyncio.run(docker_sbx._read_bounded(cast(asyncio.StreamReader, stream)))

    assert result.startswith(b"literal-head-")
    assert result.count(b"\n...[truncated]") == 1
    assert result.endswith(b"-literal-tail")
    assert len(result) <= docker_sbx.MAX_OUTPUT_BYTES
    assert stream.read_count == 3


def test_read_bounded_preserves_split_four_byte_utf8_at_head_and_tail() -> None:
    stream = _ScriptedStream(
        [
            b"head-\xf0\x9f",
            b"\x92\xa9" + (b"x" * 65_536),
            (b"y" * 65_536) + b"-tail-\xf0\x9f",
            b"\x92\xa9",
        ]
    )

    result = asyncio.run(docker_sbx._read_bounded(cast(asyncio.StreamReader, stream)))

    assert result.startswith("head-💩".encode())
    assert result.count(b"\n...[truncated]") == 1
    assert result.endswith("-tail-💩".encode())
    assert result.decode("utf-8")
    assert stream.read_count == 5


def test_exec_lossy_utf8_output_is_bounded_after_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(returncode=2, stdout=b"\xff" * 70_000, stderr=b"\xff" * 70_000)
        if command[:2] == ("sbx", "exec")
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    result = asyncio.run(provider.exec(sandbox_id, ["bad-bytes"]))

    assert result.stdout.count("\n...[truncated]") == 1
    assert result.stderr.count("\n...[truncated]") == 1
    assert result.stdout.endswith("\ufffd" * 64)
    assert result.stderr.endswith("\ufffd" * 64)
    assert len(result.stdout.encode("utf-8")) <= 65_536
    assert len(result.stderr.encode("utf-8")) <= 65_536


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"not-json",
        b'{"host_ip":"127.0.0.1"}',
        b'[{"host_ip":"127.0.0.1","host_port":0,"sandbox_port":8080,"protocol":"tcp"}]',
        b'[{"host_ip":"0.0.0.0","host_port":49152,"sandbox_port":8080,"protocol":"tcp"}]',
        b'[{"host_ip":"127.0.0.1","host_port":49152,"sandbox_port":8080,"protocol":"tcp","extra":1}]',
        (
            b'[{"host_ip":"127.0.0.1","host_port":49152,"sandbox_port":8080,"protocol":"tcp"},'
            b'{"host_ip":"::1","host_port":49152,"sandbox_port":8080,"protocol":"tcp"}]'
        ),
    ],
)
def test_publish_port_rejects_missing_malformed_unsafe_or_ambiguous_json(
    payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(stdout=payload)
        if command[-1:] == ("--json",) and command[:2] == ("sbx", "ports")
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    with pytest.raises(DockerSbxError, match="port_mapping"):
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert spawner.calls[-3:] == [
        ("sbx", "ports", sandbox_id, "--publish", "8080/tcp4"),
        ("sbx", "ports", sandbox_id, "--json"),
        ("sbx", "rm", "--force", sandbox_id),
    ]
    calls_before_destroy = len(spawner.calls)
    asyncio.run(provider.destroy(sandbox_id))
    assert len(spawner.calls) == calls_before_destroy


@pytest.mark.parametrize("failed_stage", ["publish", "list"])
def test_publish_command_failure_force_destroys_owned_sandbox(
    failed_stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:2] != ("sbx", "ports"):
            return _Outcome()
        is_list = command[-1:] == ("--json",)
        if (failed_stage == "list") == is_list:
            return _Outcome(returncode=4, stderr=b"port operation failed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert raised.value.reason == "nonzero_exit"
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_publish_validation_cleanup_failure_retains_id_for_destroy_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[-1:] == ("--json",):
            return _Outcome(stdout=b"not-json")
        if command[:3] == ("sbx", "rm", "--force"):
            return _Outcome(returncode=9, stderr=b"still exposed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert raised.value.reason == "cleanup_unconfirmed"
    assert raised.value.sandbox_id == sandbox_id
    assert "still exposed" in raised.value.cleanup_error
    spawner.handler = lambda command: _Outcome()
    asyncio.run(provider.destroy(sandbox_id))
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_cancelled_publish_force_destroys_and_preserves_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True)
        if command[:2] == ("sbx", "ports") and "--publish" in command
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    async def exercise() -> None:
        task = asyncio.create_task(provider.publish_port(sandbox_id, 8080))
        publish_call = ("sbx", "ports", sandbox_id, "--publish", "8080/tcp4")
        while publish_call not in spawner.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_publish_timeout_force_destroys_owned_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True)
        if command[:2] == ("sbx", "ports") and "--publish" in command
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)
    sandbox_id = _create(provider, tmp_path)

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert raised.value.reason == "timeout"
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_publish_port_returns_one_strict_ephemeral_loopback_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        b'[{"host_ip":"127.0.0.1","host_port":49152,'
        b'"sandbox_port":8080,"protocol":"tcp4"}]'
    )
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(stdout=payload) if command[-1:] == ("--json",) else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    assert asyncio.run(provider.publish_port(sandbox_id, 8080)) == 49152


def test_publish_port_rejects_invalid_utf8_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        b'[{"host_ip":"127.0.0.1","host_port":49152,'
        b'"sandbox_port":8080,"protocol":"tcp4\xff"}]'
    )
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(stdout=payload) if command[-1:] == ("--json",) else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert raised.value.reason == "port_mapping_invalid_utf8"


def test_copy_and_destroy_use_exact_argv_and_cleaned_destroy_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    local_path = tmp_path / "artifact.json"

    async def exercise() -> None:
        await provider.copy(sandbox_id, "/run/result.json", local_path)
        await provider.destroy(sandbox_id)
        await provider.destroy(sandbox_id)

    asyncio.run(exercise())

    assert spawner.calls[-2:] == [
        ("sbx", "cp", f"{sandbox_id}:/run/result.json", str(local_path)),
        ("sbx", "rm", "--force", sandbox_id),
    ]
    assert not local_path.exists()


@pytest.mark.parametrize("provider_kind", ["fake", "docker"])
def test_providers_share_retry_safe_destroy_contract(
    provider_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider = (
        FakeSandboxProvider()
        if provider_kind == "fake"
        else _provider(monkeypatch, spawner)
    )

    async def exercise() -> str:
        sandbox_id = await provider.create(tmp_path, "trial")
        await provider.destroy(sandbox_id)
        await provider.destroy(sandbox_id)
        with pytest.raises(RuntimeError, match="sandbox is not active"):
            await provider.destroy("never-owned")
        with pytest.raises(RuntimeError, match="sandbox is not active"):
            await provider.exec(sandbox_id, ["echo", "must-not-run"])
        return sandbox_id

    sandbox_id = asyncio.run(exercise())
    if provider_kind == "fake":
        assert provider.calls[-4:] == [
            ("destroy", sandbox_id),
            ("destroy", sandbox_id),
            ("destroy", "never-owned"),
            ("exec", sandbox_id, ("echo", "must-not-run"), 60),
        ]
    else:
        assert spawner.calls.count(("sbx", "rm", "--force", sandbox_id)) == 1


def test_failed_destroy_keeps_provider_state_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    remove_command = ("sbx", "rm", "--force", sandbox_id)
    spawner.overrides[remove_command] = _Outcome(returncode=1, stderr=b"busy")

    with pytest.raises(DockerSbxError, match="destroy"):
        asyncio.run(provider.destroy(sandbox_id))
    del spawner.overrides[remove_command]
    asyncio.run(provider.destroy(sandbox_id))

    assert spawner.calls[-2:] == [remove_command, remove_command]


def test_nonzero_command_error_has_deterministically_truncated_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    spawner.overrides[("sbx", "cp", f"{sandbox_id}:/a", str(tmp_path / "a"))] = (
        _Outcome(returncode=9, stderr=b"x" * 70_000)
    )

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/a", tmp_path / "a"))

    assert raised.value.reason == "nonzero_exit"
    assert raised.value.returncode == 9
    assert raised.value.stderr.count("\n...[truncated]") == 1
    assert raised.value.stderr.endswith("x" * 64)
    assert len(raised.value.stderr.encode()) <= 65_536


def test_timeout_kills_and_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)
    sandbox_id = _create(provider, tmp_path)
    command = ("sbx", "cp", f"{sandbox_id}:/a", str(tmp_path / "a"))
    spawner.overrides[command] = _Outcome(hang=True)

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/a", tmp_path / "a"))

    process = spawner.processes[-1]
    assert raised.value.reason == "timeout"
    assert process.killed is True
    assert process.waited is True


def test_cancellation_kills_and_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    command = ("sbx", "exec", sandbox_id, "--", "wait")
    spawner.overrides[command] = _Outcome(hang=True)

    async def exercise() -> None:
        task = asyncio.create_task(provider.exec(sandbox_id, ["wait"]))
        while len(spawner.processes) < len(PROBE_CALLS) + 3:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    process = spawner.processes[-1]
    assert process.killed is True
    assert process.waited is True


@pytest.mark.parametrize(
    ("outcome", "expected_detail"),
    [
        (_Outcome(hang=True, kill_error=OSError("kill denied")), "kill denied"),
        (_Outcome(hang=True, reap_hang=True), "reap_timeout"),
        (_Outcome(hang=True, reap_error=OSError("reap denied")), "reap denied"),
    ],
)
def test_timeout_exposes_unconfirmed_process_cleanup(
    outcome: _Outcome,
    expected_detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("repotrial.sandbox.docker_sbx.REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)
    sandbox_id = _create(provider, tmp_path)
    command = ("sbx", "cp", f"{sandbox_id}:/a", str(tmp_path / "a"))
    spawner.overrides[command] = outcome

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/a", tmp_path / "a"))

    assert raised.value.reason == "process_cleanup_unconfirmed"
    assert expected_detail in raised.value.cleanup_error


def test_process_cleanup_resists_second_cancellation_and_reaps_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    command = ("sbx", "exec", sandbox_id, "--", "wait")
    spawner.overrides[command] = _Outcome(hang=True, reap_hang=True)

    async def exercise() -> None:
        task = asyncio.create_task(provider.exec(sandbox_id, ["wait"]))
        while len(spawner.processes) < len(PROBE_CALLS) + 3:
            await asyncio.sleep(0)
        process = spawner.processes[-1]
        task.cancel()
        while not process.killed:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        process.reap_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.waited is True

    asyncio.run(exercise())


def test_cancelled_process_cleanup_failure_is_visible_after_second_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repotrial.sandbox.docker_sbx.REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    command = ("sbx", "exec", sandbox_id, "--", "wait")
    spawner.overrides[command] = _Outcome(hang=True, kill_error=OSError("kill denied"))

    async def exercise() -> None:
        task = asyncio.create_task(provider.exec(sandbox_id, ["wait"]))
        while len(spawner.processes) < len(PROBE_CALLS) + 3:
            await asyncio.sleep(0)
        process = spawner.processes[-1]
        task.cancel()
        while not process.killed:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        notes = " ".join(raised.value.__notes__)
        assert "process cleanup unconfirmed" in notes
        assert "kill denied" in notes

    asyncio.run(exercise())


def test_missing_executable_after_create_is_an_explicit_operation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    command = ("sbx", "exec", sandbox_id, "--", "true")
    spawner.overrides[command] = FileNotFoundError()

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.exec(sandbox_id, ["true"]))

    assert raised.value.reason == "executable_unavailable"


def test_network_log_returns_strict_observed_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    payload = (
        "["
        f'{{"sandbox":"{sandbox_id}","decision":"blocked",'
        '"host":"10.0.0.1","proxy":"network","rule":"private",'
        '"reason":"deny","last_seen":"2026-08-26T00:00:00Z","count":1}'
        "]"
    ).encode()
    spawner.overrides[
        ("sbx", "policy", "log", sandbox_id, "--type", "network", "--json")
    ] = _Outcome(stdout=payload)

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.supported is True
    assert result.unsupported_reason is None
    assert result.events == [
        {
            "sandbox": sandbox_id,
            "decision": "blocked",
            "host": "10.0.0.1",
            "proxy": "network",
            "rule": "private",
            "reason": "deny",
            "last_seen": "2026-08-26T00:00:00Z",
            "count": 1,
        }
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        b'[{"sandbox":"wrong"}]',
        b"[" + b"{}" + b",{}" * 100 + b"]",
        (
            b'[{"sandbox":"ID","decision":"maybe","host":"h","proxy":"network",'
            b'"rule":"r","reason":"x","last_seen":"now","count":1}]'
        ),
    ],
)
def test_network_log_malformed_or_unbounded_json_is_explicitly_unsupported(
    payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    command = (
        "sbx",
        "policy",
        "log",
        sandbox_id,
        "--type",
        "network",
        "--json",
    )
    spawner.overrides[command] = _Outcome(
        stdout=payload.replace(b"ID", sandbox_id.encode())
    )

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.events == []
    assert result.supported is False
    assert result.unsupported_reason == "network_log_invalid_json_contract"


def test_network_log_rejects_invalid_utf8_that_lossy_decode_would_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    payload = (
        b'[{"sandbox":"'
        + sandbox_id.encode()
        + b'","decision":"blocked","host":"10.0.0.1","proxy":"network",'
        b'"rule":"private","reason":"\xff","last_seen":"now","count":1}]'
    )
    spawner.overrides[
        ("sbx", "policy", "log", sandbox_id, "--type", "network", "--json")
    ] = _Outcome(stdout=payload)

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.supported is False
    assert result.unsupported_reason == "network_log_invalid_json_contract"


def test_network_log_command_failure_is_unsupported_not_observed_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    command = (
        "sbx",
        "policy",
        "log",
        sandbox_id,
        "--type",
        "network",
        "--json",
    )
    spawner.overrides[command] = _Outcome(returncode=4, stderr=b"unavailable")

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result == NetworkLogResult(
        events=[], supported=False, unsupported_reason="network_log_command_failed"
    )


def test_network_log_deadline_timeout_propagates_unconfirmed_process_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repotrial.sandbox.docker_sbx.REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, command_timeout_s=5)
    sandbox_id = _create(provider, tmp_path)
    command = (
        "sbx",
        "policy",
        "log",
        sandbox_id,
        "--type",
        "network",
        "--json",
    )
    spawner.overrides[command] = _Outcome(hang=True, kill_error=OSError("kill denied"))
    provider._sandbox_deadlines[sandbox_id] = time.monotonic() + 0.01

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.network_log(sandbox_id))

    assert raised.value.operation == "network_log"
    assert raised.value.reason == "process_cleanup_unconfirmed"
    assert "kill denied" in raised.value.cleanup_error
    assert "reap_timeout" in raised.value.cleanup_error


def test_network_log_confirmed_timeout_remains_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)
    sandbox_id = _create(provider, tmp_path)
    command = (
        "sbx",
        "policy",
        "log",
        sandbox_id,
        "--type",
        "network",
        "--json",
    )
    spawner.overrides[command] = _Outcome(hang=True)

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_command_failed"
    assert any(process.killed and process.waited for process in spawner.processes)


def test_network_log_in_flight_trial_deadline_timeout_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, command_timeout_s=5)
    sandbox_id = _create(provider, tmp_path)
    command = (
        "sbx",
        "policy",
        "log",
        sandbox_id,
        "--type",
        "network",
        "--json",
    )
    spawner.overrides[command] = _Outcome(hang=True)
    provider._sandbox_deadlines[sandbox_id] = 1.0
    process_created = asyncio.Event()

    def signal_network_log_spawn(spawned_command: tuple[str, ...]) -> None:
        if spawned_command == command:
            process_created.set()

    spawner.before_spawn = signal_network_log_spawn
    monkeypatch.setattr(
        docker_sbx.asyncio,
        "timeout",
        lambda _: _TimeoutAfterProcessCreation(process_created),
    )

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.network_log(sandbox_id))

    assert raised.value.operation == "network_log"
    assert raised.value.reason == "total_duration_exhausted"
    assert raised.value.sandbox_id == sandbox_id
    assert process_created.is_set()
    assert spawner.processes[-1].killed is True
    assert spawner.processes[-1].waited is True


def test_network_log_equal_trial_and_command_timeout_is_trial_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_timeout_s = 0.0625
    spawner = _SbxSpawner()
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=command_timeout_s,
    )
    sandbox_id = _create(provider, tmp_path)
    command = (
        "sbx",
        "policy",
        "log",
        sandbox_id,
        "--type",
        "network",
        "--json",
    )
    spawner.overrides[command] = _Outcome(hang=True)
    monkeypatch.setattr(
        docker_sbx,
        "time",
        SimpleNamespace(monotonic=lambda: 10.0),
    )
    provider._sandbox_deadlines[sandbox_id] = 10.0625
    assert provider._sandbox_deadlines[sandbox_id] - 10.0 == command_timeout_s

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.network_log(sandbox_id))

    assert raised.value.operation == "network_log"
    assert raised.value.reason == "total_duration_exhausted"
    assert raised.value.sandbox_id == sandbox_id
    assert spawner.processes[-1].killed is True
    assert spawner.processes[-1].waited is True


def _non_help_create_calls(spawner: _SbxSpawner) -> list[tuple[str, ...]]:
    return [
        call
        for call in spawner.calls
        if call[:2] == ("sbx", "create") and call[-1] != "--help"
    ]


def test_create_deadline_starts_before_probes_and_limits_create_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    recorder = _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()

    def advance_after_timeout_calculation(command: tuple[str, ...]) -> None:
        clock.value += 1

    spawner.before_spawn = advance_after_timeout_calculation
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=20,
    )

    sandbox_id = _create(provider, tmp_path)

    assert _timeouts_by_command(spawner, recorder)[: len(PROBE_CALLS)] == list(
        zip(
            PROBE_CALLS,
            (20.0, 19.0, 18.0, 17.0, 16.0, 15.0, 14.0, 13.0, 12.0),
            strict=True,
        )
    )
    create_call = _actual_create_call(spawner)
    assert _timeouts_by_command(spawner, recorder)[-2:] == [
        (create_call, 11.0),
        (
            (
                "sbx",
                "policy",
                "allow",
                "network",
                "--sandbox",
                sandbox_id,
                "**",
            ),
            10.0,
        ),
    ]
    assert provider._sandbox_deadlines == {sandbox_id: 20.0}


def test_policy_allow_runs_before_active_state_and_deadline_are_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    def assert_pending(command: tuple[str, ...]) -> None:
        if command[:5] == ("sbx", "policy", "allow", "network", "--sandbox"):
            sandbox_id = command[-2]
            assert provider._sandbox_deadlines == {}
            with pytest.raises(RuntimeError, match="not active"):
                provider._require_active(sandbox_id)

    spawner.before_spawn = assert_pending

    sandbox_id = _create(provider, tmp_path)

    assert provider._sandbox_deadlines == {sandbox_id: 300.0}
    provider._require_active(sandbox_id)


def test_trial_exhaustion_before_first_probe_prevents_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = iter((0.0, 10.0))
    monkeypatch.setattr(
        docker_sbx,
        "time",
        SimpleNamespace(monotonic=lambda: next(readings)),
    )
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert raised.value.operation == "probe_version"
    assert raised.value.reason == "total_duration_exhausted"
    assert spawner.calls == []


def test_workload_operations_share_one_deadline_and_elapsed_time_reduces_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    recorder = _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=10,
    )
    sandbox_id = _create(provider, tmp_path)

    clock.value = 2
    asyncio.run(provider.exec(sandbox_id, ["first"]))
    clock.value = 7
    asyncio.run(provider.copy(sandbox_id, "/result", tmp_path / "result"))

    workload_timeouts = _timeouts_by_command(spawner, recorder)[-2:]
    assert workload_timeouts == [
        (("sbx", "exec", sandbox_id, "--", "first"), 8.0),
        (("sbx", "cp", f"{sandbox_id}:/result", str(tmp_path / "result")), 3.0),
    ]


def test_exec_keeps_smaller_per_command_timeout_than_trial_remaining_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    recorder = _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)
    sandbox_id = _create(provider, tmp_path)

    clock.value = 1
    asyncio.run(provider.exec(sandbox_id, ["short"], timeout_s=3))

    assert _timeouts_by_command(spawner, recorder)[-1] == (
        ("sbx", "exec", sandbox_id, "--", "short"),
        3.0,
    )


def test_expired_trial_prevents_new_workload_subprocess_and_network_log_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)
    sandbox_id = _create(provider, tmp_path)
    clock.value = 10
    calls_before_workloads = list(spawner.calls)

    with pytest.raises(DockerSbxError) as exec_error:
        asyncio.run(provider.exec(sandbox_id, ["must-not-spawn"]))
    with pytest.raises(DockerSbxError) as network_error:
        asyncio.run(provider.network_log(sandbox_id))

    assert (exec_error.value.operation, exec_error.value.reason) == (
        "exec",
        "total_duration_exhausted",
    )
    assert (network_error.value.operation, network_error.value.reason) == (
        "network_log",
        "total_duration_exhausted",
    )
    assert spawner.calls == calls_before_workloads


def test_destroy_after_shared_deadline_expiry_still_forces_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)
    sandbox_id = _create(provider, tmp_path)

    clock.value = 10
    asyncio.run(provider.destroy(sandbox_id))
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert sandbox_id not in provider._sandbox_deadlines


def test_managed_sandbox_cleans_up_after_in_flight_trial_deadline_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, command_timeout_s=5)
    artifact = tmp_path.parent / "deadline-timeout-managed-sandbox.jsonl"

    async def manage() -> None:
        async with managed_sandbox(
            provider,
            tmp_path,
            "managed",
            lifecycle_artifact=artifact,
        ) as managed_id:
            command = ("sbx", "exec", managed_id, "--", "hang")
            spawner.overrides[command] = _Outcome(hang=True)
            provider._sandbox_deadlines[managed_id] = time.monotonic() + 0.01
            await provider.exec(managed_id, ["hang"])

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(manage())

    create_call = _actual_create_call(spawner)
    managed_id = create_call[create_call.index("--name") + 1]
    assert raised.value.reason == "total_duration_exhausted"
    assert raised.value.sandbox_id == managed_id
    assert spawner.calls[-1] == ("sbx", "rm", "--force", managed_id)
    assert managed_id not in provider._sandbox_deadlines


def test_sequential_sandboxes_share_first_successful_trial_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)
    first_id = _create(provider, tmp_path)
    asyncio.run(provider.destroy(first_id))
    clock.value = 2
    second_id = _create(provider, tmp_path)

    assert provider._trial_deadline == 10.0
    assert provider._sandbox_deadlines[second_id] == 10.0


def test_expired_trial_prevents_second_create_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)
    first_id = _create(provider, tmp_path)
    asyncio.run(provider.destroy(first_id))
    clock.value = 10
    calls_before_second_create = list(spawner.calls)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == "total_duration_exhausted"
    assert spawner.calls == calls_before_second_create


def test_failed_first_create_leaves_no_trial_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            return _Outcome(returncode=7, stderr=b"create failed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner, total_duration_s=10)

    with pytest.raises(DockerSbxError, match="create failed"):
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_failed_later_create_does_not_reset_trial_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    create_attempts = 0

    def outcome(command: tuple[str, ...]) -> _Outcome:
        nonlocal create_attempts
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            create_attempts += 1
            if create_attempts == 2:
                return _Outcome(returncode=7, stderr=b"create failed")
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(monkeypatch, spawner, total_duration_s=10)
    _create(provider, tmp_path)
    clock.value = 2

    with pytest.raises(DockerSbxError, match="create failed"):
        _create(provider, tmp_path)

    assert provider._trial_deadline == 10.0


def test_separate_provider_instances_have_independent_trial_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    provider_a = _provider(monkeypatch, _SbxSpawner(), total_duration_s=10)
    _create(provider_a, tmp_path)
    clock.value = 2
    provider_b = _provider(monkeypatch, _SbxSpawner(), total_duration_s=10)
    _create(provider_b, tmp_path)

    assert provider_a._trial_deadline == 10.0
    assert provider_b._trial_deadline == 12.0


def test_publish_recomputes_timeout_for_each_subprocess_from_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    recorder = _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:2] != ("sbx", "ports"):
            return _Outcome()
        if command[-1:] == ("--json",):
            return _Outcome(
                stdout=(
                    b'[{"host_ip":"127.0.0.1","host_port":49152,'
                    b'"sandbox_port":8080,"protocol":"tcp4"}]'
                )
            )
        clock.value = 2
        return _Outcome()

    spawner.handler = outcome
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=10,
    )
    sandbox_id = _create(provider, tmp_path)

    assert asyncio.run(provider.publish_port(sandbox_id, 8080)) == 49152

    assert _timeouts_by_command(spawner, recorder)[-2:] == [
        (("sbx", "ports", sandbox_id, "--publish", "8080/tcp4"), 10.0),
        (("sbx", "ports", sandbox_id, "--json"), 8.0),
    ]
