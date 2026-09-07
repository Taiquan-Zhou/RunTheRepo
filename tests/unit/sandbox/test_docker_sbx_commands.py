import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from repotrial.sandbox import docker_sbx
from repotrial.sandbox.base import (
    ExecResult,
    NetworkLogResult,
    RuntimeTemplateAudit,
    RuntimeTemplateIdentity,
    get_sandbox_failure_evidence,
    serialize_sandbox_failure_evidence,
)
from repotrial.sandbox.docker_sbx import (
    _RUNTIME_TEMPLATE_TAG,
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
    + " ".join((*CREATE_FLAGS, "--template")),
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
    ("sbx", "template", "save", "--help"): "Usage: sbx template save SANDBOX TAG",
    ("sbx", "template", "ls", "--help"): "Usage: sbx template ls --json",
    ("sbx", "template", "rm", "--help"): "Usage: sbx template rm TAG|ID",
    ("sbx", "stop", "--help"): "Usage: sbx stop SANDBOX [SANDBOX...]",
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
    ("sbx", "template", "save", "--help"),
    ("sbx", "template", "ls", "--help"),
    ("sbx", "template", "rm", "--help"),
    ("sbx", "stop", "--help"),
    ("sbx", "policy", "log", "--help"),
]
HOST_HEAD = b"0123456789abcdef0123456789abcdef01234567\n"
GUEST_WORKSPACE = b"/workspace\n"
OBSERVED_ABSENCE_STDERR = (
    b"WARN: could not acquire docker hub refresh lock, proceeding without cross-process lock: "
    b"context deadline exceeded\n"
    b"Error: sandbox \x27sandbox-17\x27 not found "
    b"(run \x27sbx ls\x27 to see your sandboxes)\n"
)


@dataclass(frozen=True)
class _Outcome:
    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    hang: bool = False
    kill_error: OSError | None = None
    reap_hang: bool = False
    reap_error: OSError | None = None


class _PathFlags:
    def __init__(self, *, is_symlink: bool, file_attributes: int) -> None:
        self._is_symlink = is_symlink
        self._file_attributes = file_attributes

    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_file_attributes=self._file_attributes)

    def is_symlink(self) -> bool:
        return self._is_symlink


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
        self.stdin = _FakeStdin()
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


class _FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _SbxSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []
        self.processes: list[_FakeProcess] = []
        self.overrides: dict[tuple[str, ...], _Outcome | BaseException] = {}
        self.handler: Callable[[tuple[str, ...]], _Outcome | BaseException] | None = (
            None
        )
        self.intercept: Callable[[tuple[str, ...]], _Outcome | BaseException] | None = (
            None
        )
        self.before_spawn: Callable[[tuple[str, ...]], None] | None = None
        self.before_spawn_with_kwargs: (
            Callable[[tuple[str, ...], dict[str, object]], None] | None
        ) = None

    async def __call__(self, *argv: str, **kwargs: object) -> _FakeProcess:
        command = tuple(argv)
        if command[:2] == ("sbx", "exec") and command[-3:] == (
            "docker",
            "image",
            "load",
        ):
            assert command[2] == "-i", "sbx exec requires -i to forward stdin"
        if self.before_spawn is not None:
            self.before_spawn(command)
        if self.before_spawn_with_kwargs is not None:
            self.before_spawn_with_kwargs(command, kwargs)
        self.calls.append(command)
        self.kwargs.append(kwargs)
        outcome: _Outcome | BaseException
        if command in self.overrides:
            outcome = self.overrides[command]
        elif self.intercept is not None:
            outcome = self.intercept(command)
        elif command[:1] == ("git",):
            outcome = _Outcome(stdout=HOST_HEAD)
        elif command == ("sbx", "version"):
            outcome = _Outcome(stdout=b"sbx 99.0.0\n")
        elif command in HELP_OUTPUTS:
            outcome = _Outcome(stdout=HELP_OUTPUTS[command].encode())
        elif command[:2] == ("sbx", "exec") and _is_guest_verification_call(command):
            outcome = _guest_verification_outcome(command)
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


def _active_provider(
    monkeypatch: pytest.MonkeyPatch,
    spawner: _SbxSpawner,
    *,
    sandbox_id: str = "sandbox-17",
    deadline: float = 300.0,
    command_timeout_s: float = 5,
    total_duration_s: int = 300,
) -> tuple[DockerSbxProvider, str]:
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=command_timeout_s,
        total_duration_s=total_duration_s,
    )
    provider._sandbox_states[sandbox_id] = docker_sbx._SandboxState.ACTIVE
    provider._sandbox_deadlines[sandbox_id] = deadline
    return provider, sandbox_id


def _create(provider: DockerSbxProvider, workspace: Path) -> str:
    return asyncio.run(provider.create(workspace, "trial"))


def _actual_create_call(spawner: _SbxSpawner) -> tuple[str, ...]:
    return next(
        call
        for call in spawner.calls
        if call[:2] == ("sbx", "create") and call[-1] != "--help"
    )


def _actual_create_config_path(spawner: _SbxSpawner) -> Path:
    create_call = _actual_create_call(spawner)
    create_index = spawner.calls.index(create_call)
    environment = cast(dict[str, str], spawner.kwargs[create_index]["env"])
    return Path(environment["DOCKER_CONFIG"])


def _guest_verification_outcome(command: tuple[str, ...]) -> _Outcome:
    argv = command[4:]
    if argv == ("pwd",):
        return _Outcome(stdout=GUEST_WORKSPACE)
    if argv == ("git", "rev-parse", "--show-toplevel"):
        return _Outcome(stdout=GUEST_WORKSPACE)
    if argv == ("git", "rev-parse", "--is-inside-work-tree"):
        return _Outcome(stdout=b"true\n")
    if argv == ("git", "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"):
        return _Outcome(stdout=HOST_HEAD)
    if argv == (
        "git",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    ):
        return _Outcome()
    return _Outcome()


def _is_guest_verification_call(command: tuple[str, ...]) -> bool:
    return command[4:] in {
        ("pwd",),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "rev-parse", "--is-inside-work-tree"),
        ("git", "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
        (
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ),
    }


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
        "localhost",
        "metadata.google.internal",
    }

    assert policy.deny_network == MANDATORY_DENY_NETWORK | {"api.example.test"}
    assert required_resources <= policy.deny_network
    with pytest.raises((AttributeError, TypeError)):
        policy.memory_mb = 1024


@pytest.mark.parametrize(
    ("disk_mb", "expected_docker_mb", "expected_workspace_mb"),
    [
        (7, 1, 5),
        (512, 447, 64),
        (2048, 1919, 128),
        (4096, 3839, 256),
    ],
)
def test_disk_allocation_preserves_budget_and_uses_calibrated_floors(
    disk_mb: int, expected_docker_mb: int, expected_workspace_mb: int
) -> None:
    allocation = calculate_disk_allocation(disk_mb)

    assert ROOT_FLOOR_MB == 1
    assert DOCKER_FLOOR_MB == 1
    assert WORKSPACE_FLOOR_MB == 5
    assert allocation.root_mb == 1
    assert allocation.docker_mb == expected_docker_mb
    assert allocation.workspace_mb == expected_workspace_mb
    assert (
        allocation.root_mb + allocation.docker_mb + allocation.workspace_mb == disk_mb
    )
    assert not all(
        value == disk_mb
        for value in (
            allocation.root_mb,
            allocation.docker_mb,
            allocation.workspace_mb,
        )
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


def test_create_binds_resolved_host_head_and_guest_clone_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable, value in {
        "GIT_DIR": "host-git-dir-must-not-leak",
        "GIT_CONFIG_GLOBAL": "host-git-config-must-not-leak",
        "GIT_ASKPASS": "host-git-askpass-must-not-leak",
        "SSH_ASKPASS": "host-ssh-askpass-must-not-leak",
    }.items():
        monkeypatch.setenv(variable, value)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested_workspace = workspace / ".."
    resolved_workspace = requested_workspace.resolve(strict=True)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    sandbox_id = _create(provider, requested_workspace)

    host_command = (
        "git",
        "-C",
        str(resolved_workspace),
        "rev-parse",
        "--verify",
        "--end-of-options",
        "HEAD^{commit}",
    )
    host_indices = [
        index for index, command in enumerate(spawner.calls) if command == host_command
    ]
    assert len(host_indices) == 2
    create_call = _actual_create_call(spawner)
    create_index = spawner.calls.index(create_call)
    assert host_indices[0] < len(PROBE_CALLS) < host_indices[1] == create_index - 1
    assert create_call[-2:] == ("shell", str(resolved_workspace))
    expected_guest_calls = [
        ("sbx", "exec", sandbox_id, "--", "pwd"),
        ("sbx", "exec", sandbox_id, "--", "git", "rev-parse", "--show-toplevel"),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--is-inside-work-tree",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ),
    ]
    allow_index = next(
        index
        for index, command in enumerate(spawner.calls)
        if command[:5] == ("sbx", "policy", "allow", "network", "--sandbox")
    )
    assert spawner.calls[create_index + 1 : allow_index] == expected_guest_calls
    for index in host_indices:
        environment = cast(dict[str, str], spawner.kwargs[index]["env"])
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GCM_INTERACTIVE"] == "never"
        assert "SSH_ASKPASS" not in {key.upper() for key in environment}
        assert {
            key.upper() for key in environment if key.upper().startswith("GIT_")
        } == {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT"}


@pytest.mark.parametrize(
    "host_output",
    [
        b" 0123456789abcdef0123456789abcdef01234567\n",
        b"0123456789abcdef0123456789abcdef01234567\nextra\n",
        b"0123456789abcdef0123456789abcdef0123456g\n",
        b"0123456789abcdef0123456789abcdef01234567\xff\n",
    ],
)
def test_create_rejects_malformed_host_head_before_sbx_create(
    host_output: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    host_command = (
        "git",
        "-C",
        str(tmp_path.resolve(strict=True)),
        "rev-parse",
        "--verify",
        "--end-of-options",
        "HEAD^{commit}",
    )
    spawner.overrides[host_command] = _Outcome(stdout=host_output)
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "host_head_invalid",
    )
    assert _non_help_create_calls(spawner) == []
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}


@pytest.mark.parametrize(
    ("is_symlink", "file_attributes"),
    [
        pytest.param(True, 0, id="symlink"),
        pytest.param(False, 0x400, id="windows-reparse-point"),
    ],
)
def test_workspace_link_or_reparse_flags_are_rejected(
    is_symlink: bool, file_attributes: int
) -> None:
    workspace = _PathFlags(
        is_symlink=is_symlink,
        file_attributes=file_attributes,
    )

    assert docker_sbx._is_link_or_reparse_point(workspace) is True


def test_create_rejects_workspace_link_or_reparse_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    monkeypatch.setattr(
        docker_sbx,
        "_is_link_or_reparse_point",
        lambda _: True,
    )

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "workspace_invalid",
    )
    assert spawner.calls == []


def test_create_rejects_changed_workspace_identity_before_sbx_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identities = iter(((1, 1), (1, 1), (2, 2)))
    monkeypatch.setattr(
        docker_sbx,
        "_workspace_identity",
        lambda _: next(identities),
    )
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "workspace_changed",
    )
    assert _non_help_create_calls(spawner) == []


def test_create_rejects_second_host_head_change_before_sbx_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_heads = iter(
        (
            HOST_HEAD,
            b"ffffffffffffffffffffffffffffffffffffffff\n",
        )
    )
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=next(host_heads))
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "host_head_changed",
    )
    assert _non_help_create_calls(spawner) == []


@pytest.mark.parametrize(
    ("guest_argv", "outcome", "expected_reason"),
    [
        (("pwd",), _Outcome(returncode=7), "nonzero_exit"),
        (
            ("git", "rev-parse", "--show-toplevel"),
            _Outcome(stdout=b"/other\n"),
            "guest_workspace_mismatch",
        ),
        (
            ("git", "rev-parse", "--is-inside-work-tree"),
            _Outcome(stdout=b"false\n"),
            "guest_not_work_tree",
        ),
        (
            ("git", "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
            _Outcome(stdout=b"ffffffffffffffffffffffffffffffffffffffff\n"),
            "guest_head_mismatch",
        ),
        (
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=no",
            ),
            _Outcome(stdout=b"?? unexpected\n"),
            "guest_status_not_clean",
        ),
    ],
)
def test_create_cleans_up_when_guest_clone_postcondition_fails(
    guest_argv: tuple[str, ...],
    outcome: _Outcome,
    expected_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec") and command[4:] == guest_argv:
            return outcome
        if command[:2] == ("sbx", "exec"):
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        expected_reason,
    )
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert not any(
        command[:5] == ("sbx", "policy", "allow", "network", "--sandbox")
        for command in spawner.calls
    )
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}


@pytest.mark.parametrize(
    ("guest_argv", "outcome", "expected_reason"),
    [
        pytest.param(
            ("pwd",),
            _Outcome(stdout=b"\xff\n"),
            "guest_pwd_invalid",
            id="pwd-invalid-utf8",
        ),
        pytest.param(
            ("git", "rev-parse", "--show-toplevel"),
            _Outcome(stdout=b"/workspace"),
            "guest_top_level_invalid",
            id="top-level-missing-newline",
        ),
    ],
)
def test_create_forces_partial_cleanup_after_malformed_guest_workspace_output(
    guest_argv: tuple[str, ...],
    outcome: _Outcome,
    expected_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec") and command[4:] == guest_argv:
            return outcome
        if command[:2] == ("sbx", "exec"):
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        expected_reason,
    )
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert not any(
        command[:5] == ("sbx", "policy", "allow", "network", "--sandbox")
        for command in spawner.calls
    )
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}


@pytest.mark.parametrize(
    ("outcome", "expected_reason"),
    [
        (_Outcome(hang=True), "timeout"),
        (_Outcome(hang=True, reap_hang=True), "process_cleanup_unconfirmed"),
    ],
)
def test_host_git_timeout_reaps_before_create_or_reports_unconfirmed_cleanup(
    outcome: _Outcome,
    expected_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if outcome.reap_hang:
        monkeypatch.setattr("repotrial.sandbox.docker_sbx.REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    spawner.intercept = lambda command: (
        outcome if command[:1] == ("git",) else _Outcome()
    )
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        expected_reason,
    )
    assert spawner.processes[0].killed is True
    assert spawner.processes[0].waited is (expected_reason == "timeout")
    assert not any(command[:1] == ("sbx",) for command in spawner.calls)


def test_cancelled_host_git_reaps_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    spawner.intercept = lambda command: (
        _Outcome(hang=True) if command[:1] == ("git",) else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        task = asyncio.create_task(provider.create(tmp_path, "trial"))
        while not any(command[:1] == ("git",) for command in spawner.calls):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert spawner.processes[0].killed is True
    assert spawner.processes[0].waited is True
    assert not any(command[:1] == ("sbx",) for command in spawner.calls)


def test_guest_malformed_head_enters_partial_create_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec") and command[4:] == (
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        ):
            return _Outcome(stdout=b"not-a-commit\n")
        if command[:2] == ("sbx", "exec"):
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "guest_head_invalid",
    )
    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


def test_guest_timeout_enters_partial_create_cleanup_without_network_or_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec") and command[4:] == ("pwd",):
            return _Outcome(hang=True)
        if command[:2] == ("sbx", "exec"):
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "timeout",
    )
    assert spawner.processes[-2].killed is True
    assert spawner.processes[-2].waited is True
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}


def test_cancelled_guest_clone_verification_enters_partial_create_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec") and command[4:] == ("pwd",):
            return _Outcome(hang=True)
        if command[:2] == ("sbx", "exec"):
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        task = asyncio.create_task(provider.create(tmp_path, "trial"))
        while not any(
            command[:2] == ("sbx", "exec") and command[4:] == ("pwd",)
            for command in spawner.calls
        ):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}


def test_guest_clone_cleanup_failure_retains_partial_create_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec") and command[4:] == (
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        ):
            return _Outcome(stdout=b"not-a-commit\n")
        if command[:3] == ("sbx", "rm", "--force"):
            return _Outcome(returncode=9, stderr=b"cleanup blocked")
        if command[:2] == ("sbx", "exec"):
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "clone_verification",
        "guest_head_invalid",
    )
    context = raised.value.partial_create_cleanup
    assert context.create_failure is raised.value
    assert "cleanup blocked" in str(context.cleanup_failure)
    assert provider._trial_deadline is None
    assert provider._sandbox_deadlines == {}


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
    assert spawner.calls[-1] == ("sbx", "version")
    assert spawner.calls[0][:3] == ("git", "-C", str(tmp_path.resolve(strict=True)))


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
    assert spawner.calls[0][0] == "git"


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


def test_runtime_template_probe_requires_all_template_help_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    assert asyncio.run(provider._probe(time.monotonic() + 300.0)) is True

    assert ("sbx", "create", "--help") in spawner.calls
    assert ("sbx", "template", "save", "--help") in spawner.calls
    assert ("sbx", "template", "ls", "--help") in spawner.calls
    assert ("sbx", "template", "rm", "--help") in spawner.calls


@pytest.mark.parametrize(
    ("command", "output", "expected_reason"),
    [
        (
            ("sbx", "template", "save", "--help"),
            "Usage: sbx template save SANDBOX",
            "template_save_missing_capability:TAG",
        ),
        (
            ("sbx", "template", "rm", "--help"),
            "Usage: sbx template rm IMAGE",
            "template_rm_missing_capability:TAG|ID",
        ),
        (
            ("sbx", "stop", "--help"),
            "Usage: sbx stop [NAME...]",
            "stop_missing_capability:SANDBOX",
        ),
    ],
)
def test_runtime_template_probe_rejects_incomplete_help_contract(
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    output: str,
    expected_reason: str,
) -> None:
    spawner = _SbxSpawner()
    spawner.overrides[command] = _Outcome(stdout=output.encode())
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxUnsupportedError) as raised:
        asyncio.run(provider._probe(time.monotonic() + 300.0))

    assert raised.value.reason == expected_reason


def test_runtime_template_lifecycle_uses_owned_tag_and_exact_image_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    image_id = "a" * 12
    saved_tag: str | None = None
    template_saved = False

    def template_handler(command: tuple[str, ...]) -> _Outcome:
        nonlocal saved_tag, template_saved
        if command == ("sbx", "template", "ls", "--json"):
            if not template_saved:
                return _Outcome(stdout=b'{"images":[]}')
            assert saved_tag is not None
            payload = json.dumps(
                {
                    "images": [
                        {
                            "id": image_id,
                            "repository": "docker.io/library/repotrial-runtime",
                            "tag": saved_tag.split(":", 1)[1],
                            "flavor": "shell",
                            "created_at": "2026-09-06T00:00:00Z",
                            "size": 1,
                        }
                    ]
                }
            ).encode()
            return _Outcome(stdout=payload)
        if command[:3] == ("sbx", "template", "save"):
            saved_tag = command[-1]
            template_saved = True
            return _Outcome()
        if command[:3] == ("sbx", "template", "rm"):
            assert command[-1] == saved_tag
            template_saved = False
            return _Outcome()
        return _Outcome()

    spawner.handler = template_handler

    async def exercise() -> None:
        await provider.activate_runtime_template(sandbox_id, "b" * 64)
        assert provider.expected_image_identity_sha256() == "b" * 64
        await provider.finalize_runtime_template()

    asyncio.run(exercise())

    template_calls = [call for call in spawner.calls if call[:2] == ("sbx", "template")]
    assert template_calls[0] == ("sbx", "template", "ls", "--json")
    assert template_calls[1][:3] == ("sbx", "template", "save")
    assert template_calls[1][-1] == saved_tag
    assert template_calls[2] == ("sbx", "template", "ls", "--json")
    assert template_calls[3] == ("sbx", "template", "ls", "--json")
    assert template_calls[4] == ("sbx", "template", "rm", saved_tag)
    assert template_calls[5] == ("sbx", "template", "ls", "--json")
    assert saved_tag is not None and _RUNTIME_TEMPLATE_TAG.fullmatch(saved_tag)
    assert provider.expected_image_identity_sha256() is None
    stop_index = spawner.calls.index(("sbx", "stop", sandbox_id))
    save_index = spawner.calls.index(template_calls[1])
    assert stop_index < save_index


def test_stage_runtime_image_bundle_streams_export_to_private_bounded_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    archive = b"streamed image archive"
    spawner.handler = lambda command: (
        _Outcome(stdout=archive)
        if command[:2] == ("sbx", "exec")
        else _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )

    asyncio.run(
        provider.stage_runtime_image_bundle(
            sandbox_id,
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "a" * 64,),
        )
    )

    try:
        export_calls = [call for call in spawner.calls if call[:2] == ("sbx", "exec")]
        assert export_calls == [
            (
                "sbx",
                "exec",
                sandbox_id,
                "--",
                "docker",
                "image",
                "save",
                "docker.io/library/alpine:latest",
                "sha256:" + "a" * 64,
            )
        ]
        bundle_path = provider._runtime_image_bundle_path
        assert bundle_path is not None
        assert bundle_path.read_bytes() == archive
        assert bundle_path.stat().st_mode & 0o777 == 0o600
        assert (
            provider.runtime_template_audit().bundle_sha256
            == hashlib.sha256(archive).hexdigest()
        )
        assert provider.runtime_template_audit().bundle_size == len(archive)
    finally:
        asyncio.run(provider.finalize_runtime_template())


@pytest.mark.parametrize(
    "references,ids,reason",
    [
        ((), ("sha256:" + "a" * 64,), "image_bundle_empty"),
        (("alpine:latest",), ("sha256:" + "a" * 64,), "image_bundle_reference_invalid"),
        (
            ("-unsafe:latest",),
            ("sha256:" + "a" * 64,),
            "image_bundle_reference_invalid",
        ),
        (
            (
                "docker.io/library/zulu:latest",
                "docker.io/library/alpine:latest",
            ),
            ("sha256:" + "a" * 64,),
            "image_bundle_references_unsorted",
        ),
        (
            (
                "docker.io/library/alpine:latest",
                "docker.io/library/alpine:latest",
            ),
            ("sha256:" + "a" * 64,),
            "image_bundle_references_duplicate",
        ),
        (
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "A" * 64,),
            "image_bundle_id_invalid",
        ),
    ],
)
def test_stage_runtime_image_bundle_rejects_unsafe_or_ambiguous_identity(
    monkeypatch: pytest.MonkeyPatch,
    references: tuple[str, ...],
    ids: tuple[str, ...],
    reason: str,
) -> None:
    provider, sandbox_id = _active_provider(
        monkeypatch, _SbxSpawner(), deadline=time.monotonic() + 300.0
    )

    with pytest.raises(DockerSbxError, match=reason):
        asyncio.run(provider.stage_runtime_image_bundle(sandbox_id, references, ids))


@pytest.mark.parametrize(
    "repository", ["docker.io/library/alpine", "localhost:5000/team/image"]
)
def test_bundle_validator_accepts_canonical_digest(repository: str) -> None:
    reference = repository + "@sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    assert docker_sbx._validate_image_bundle_inputs((reference,), (image_id,)) == (
        (reference,),
        (image_id,),
    )


@pytest.mark.parametrize(
    "reference",
    [
        "alpine@sha256:" + "a" * 64,
        "library/alpine@sha256:" + "a" * 64,
        "docker.io/library/alpine@sha256:" + "a" * 63,
        "docker.io/library/alpine@sha256:" + "A" * 64,
        "docker.io/library/alpine@sha512:" + "a" * 64,
        "docker.io/library/alpine@@sha256:" + "a" * 64,
    ],
)
def test_bundle_validator_rejects_malformed_or_short_digest(reference: str) -> None:
    with pytest.raises(DockerSbxError, match="image_bundle_reference_invalid"):
        docker_sbx._validate_image_bundle_inputs((reference,), ("sha256:" + "b" * 64,))


def test_stage_runtime_image_bundle_rejects_oversize_and_cleans_process_and_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    provider._policy = DockerSbxPolicy(
        cpus=1,
        memory_mb=512,
        pids_limit=64,
        disk_mb=7,
        total_duration_s=300,
    )
    archive = b"x" * (
        calculate_disk_allocation(provider._policy.disk_mb).docker_mb * 1024 * 1024 + 1
    )
    spawner.handler = lambda command: (
        _Outcome(stdout=archive)
        if command[:2] == ("sbx", "exec")
        else _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )

    with pytest.raises(DockerSbxError, match="image_bundle_oversize"):
        asyncio.run(
            provider.stage_runtime_image_bundle(
                sandbox_id,
                ("docker.io/library/alpine:latest",),
                ("sha256:" + "a" * 64,),
            )
        )

    assert spawner.processes[0].killed is True
    assert spawner.processes[0].waited is True
    assert provider._runtime_image_bundle_path is None


def test_runtime_image_bundle_import_uses_exact_argv_and_hashes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    archive = b"bundle-for-import"
    spawner.handler = lambda command: _Outcome(stdout=archive)
    asyncio.run(
        provider.stage_runtime_image_bundle(
            sandbox_id,
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "a" * 64,),
        )
    )

    try:
        asyncio.run(
            provider._import_runtime_image_bundle(
                "sandbox-new", time.monotonic() + 300.0
            )
        )

        assert spawner.calls[-1] == (
            "sbx",
            "exec",
            "-i",
            "sandbox-new",
            "--",
            "docker",
            "image",
            "load",
        )
        assert spawner.processes[-1].stdin.data == archive
        assert spawner.processes[-1].stdin.closed is True
    finally:
        asyncio.run(provider.finalize_runtime_template())


def test_cancelled_runtime_image_bundle_stage_reaps_process_and_removes_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    spawner.handler = lambda command: _Outcome(hang=True)

    async def exercise() -> None:
        task = asyncio.create_task(
            provider.stage_runtime_image_bundle(
                sandbox_id,
                ("docker.io/library/alpine:latest",),
                ("sha256:" + "a" * 64,),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert spawner.processes[0].killed is True
    assert spawner.processes[0].waited is True
    assert provider._runtime_image_bundle_path is None


def test_begin_invocation_rejects_unconfirmed_image_bundle_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())
    provider._runtime_image_bundle_path = Path(
        tempfile.mkstemp(prefix="repotrial-test-residue-")[1]
    )
    try:
        with pytest.raises(RuntimeError, match="bundle"):
            asyncio.run(provider.begin_invocation())
    finally:
        provider._runtime_image_bundle_path.unlink(missing_ok=True)


def test_finalize_runtime_template_attempts_template_and_bundle_cleanup_on_both_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())
    calls: list[str] = []

    async def fail_template() -> None:
        calls.append("template")
        raise DockerSbxError("template_finalize", "template_failure")

    async def fail_bundle() -> None:
        calls.append("bundle")
        raise DockerSbxError("template_finalize", "bundle_failure")

    monkeypatch.setattr(provider, "_finalize_runtime_template_only", fail_template)
    monkeypatch.setattr(provider, "_cleanup_runtime_image_bundle", fail_bundle)

    with pytest.raises(DockerSbxError, match="dual_cleanup_failed") as error:
        asyncio.run(provider.finalize_runtime_template())

    assert calls == ["template", "bundle"]
    assert "template_failure" in str(error.value)
    assert "bundle_failure" in str(error.value)
    assert provider._runtime_template_finalization_confirmed is False


@pytest.mark.parametrize(
    "stop_outcome",
    [_Outcome(returncode=1), _Outcome(hang=True), asyncio.CancelledError()],
)
def test_uncertain_stop_failure_blocks_exec_but_allows_destroy(
    monkeypatch: pytest.MonkeyPatch, stop_outcome: _Outcome | BaseException
) -> None:
    spawner = _SbxSpawner()
    command_timeout_s = (
        0.01 if isinstance(stop_outcome, _Outcome) and stop_outcome.hang else 5
    )
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
        command_timeout_s=command_timeout_s,
    )
    spawner.overrides[("sbx", "stop", sandbox_id)] = stop_outcome
    spawner.handler = lambda command: (
        _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )

    with pytest.raises((DockerSbxError, asyncio.CancelledError)):
        asyncio.run(provider.activate_runtime_template(sandbox_id, "b" * 64))

    with pytest.raises(RuntimeError, match="stopped"):
        asyncio.run(provider.exec(sandbox_id, ["true"]))
    assert not any(call[:2] == ("sbx", "exec") for call in spawner.calls)

    asyncio.run(provider.destroy(sandbox_id))
    assert ("sbx", "rm", "--force", sandbox_id) in spawner.calls


@pytest.mark.parametrize(
    "stop_outcome", [_Outcome(returncode=1), asyncio.CancelledError()]
)
def test_runtime_template_stop_failure_prevents_save(
    monkeypatch: pytest.MonkeyPatch, stop_outcome: _Outcome | BaseException
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    spawner.overrides[("sbx", "stop", sandbox_id)] = stop_outcome

    spawner.handler = lambda command: (
        _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )
    with pytest.raises((DockerSbxError, asyncio.CancelledError)):
        asyncio.run(provider.activate_runtime_template(sandbox_id, "b" * 64))

    assert not any(call[:3] == ("sbx", "template", "save") for call in spawner.calls)


def test_runtime_template_stop_timeout_prevents_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
        command_timeout_s=0.01,
    )
    spawner.overrides[("sbx", "stop", sandbox_id)] = _Outcome(hang=True)
    spawner.handler = lambda command: (
        _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )

    with pytest.raises(DockerSbxError, match="timeout"):
        asyncio.run(provider.activate_runtime_template(sandbox_id, "b" * 64))

    assert not any(call[:3] == ("sbx", "template", "save") for call in spawner.calls)


def test_runtime_template_cleanup_after_save_uses_unbounded_deadline_and_confirms_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    list_count = 0
    cleanup_deadlines: list[float | None] = []

    async def listed(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        nonlocal list_count
        list_count += 1
        if list_count == 1:
            return ()
        if list_count == 2:
            raise DockerSbxError("template_ls", "timeout")
        return ()

    async def remove(_: str, *, deadline: float | None) -> None:
        cleanup_deadlines.append(deadline)

    monkeypatch.setattr(provider, "_list_runtime_templates", listed)
    monkeypatch.setattr(provider, "_remove_runtime_template", remove)

    with pytest.raises(DockerSbxError, match="timeout"):
        asyncio.run(provider.activate_runtime_template(sandbox_id, "b" * 64))

    assert cleanup_deadlines == [None]
    assert list_count == 3


def test_runtime_template_finalize_retries_pending_tag_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    list_count = 0
    remove_count = 0
    saved_tag: str | None = None
    removed = False

    async def listed(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        nonlocal list_count
        list_count += 1
        if list_count == 1:
            return ()
        if list_count == 2:
            raise DockerSbxError("template_ls", "template_list_invalid")
        if removed:
            return ()
        assert saved_tag is not None
        return (
            docker_sbx._RuntimeTemplate(
                "docker.io/library/repotrial-runtime",
                saved_tag.split(":", 1)[1],
                "a" * 12,
            ),
        )

    async def remove(reference: str, *, deadline: float | None) -> None:
        del deadline
        nonlocal remove_count, removed
        pending_tag = provider._runtime_template_pending_tag
        assert pending_tag is not None
        assert reference == pending_tag
        remove_count += 1
        if remove_count == 1:
            raise DockerSbxError("template_rm", "io_error")
        removed = True

    monkeypatch.setattr(provider, "_list_runtime_templates", listed)
    monkeypatch.setattr(provider, "_remove_runtime_template", remove)

    with pytest.raises(DockerSbxError, match="template_list_invalid"):
        asyncio.run(provider.activate_runtime_template(sandbox_id, "b" * 64))

    saved_tag = provider._runtime_template_pending_tag
    assert saved_tag is not None
    assert provider._runtime_template_identity is None
    assert provider.runtime_template_audit().identity is None

    asyncio.run(provider.finalize_runtime_template())

    assert list_count == 4
    assert remove_count == 2
    assert provider._runtime_template_pending_tag is None
    assert provider.runtime_template_audit().identity is None
    assert provider.runtime_template_audit().removal_confirmed is True


def test_runtime_template_finalize_removes_by_owned_tag_and_preserves_shared_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, _ = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    owned_tag = "repotrial-runtime:" + "e" * 32
    shared_id = "f" * 12
    provider._runtime_template_tag = owned_tag
    provider._runtime_template_image_id = shared_id
    provider._runtime_template_expected_identity = "a" * 64
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="e" * 32,
        image_id=shared_id,
        image_identity_sha256="a" * 64,
    )
    provider._runtime_template_identity = identity
    provider._runtime_template_audit = RuntimeTemplateAudit(
        identity=identity,
        bundle_sha256=provider._runtime_image_bundle_sha256,
        bundle_size=provider._runtime_image_bundle_size,
    )
    removed: list[str] = []
    removed_owned = False

    async def listed(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        if removed_owned:
            return (docker_sbx._RuntimeTemplate("shell-docker", "latest", shared_id),)
        return (
            docker_sbx._RuntimeTemplate(
                "docker.io/library/repotrial-runtime", "e" * 32, shared_id
            ),
            docker_sbx._RuntimeTemplate("shell-docker", "latest", shared_id),
        )

    async def remove(reference: str, *, deadline: float | None) -> None:
        nonlocal removed_owned
        del deadline
        removed.append(reference)
        removed_owned = True

    monkeypatch.setattr(provider, "_list_runtime_templates", listed)
    monkeypatch.setattr(provider, "_remove_runtime_template", remove)

    asyncio.run(provider.finalize_runtime_template())

    assert removed == [owned_tag]
    assert provider.expected_image_identity_sha256() is None


def test_runtime_template_finalize_keeps_state_when_owned_tag_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, _ = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    owned_tag = "repotrial-runtime:" + "1" * 32
    image_id = "2" * 12
    provider._runtime_template_tag = owned_tag
    provider._runtime_template_image_id = image_id
    provider._runtime_template_expected_identity = "3" * 64
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="1" * 32,
        image_id=image_id,
        image_identity_sha256="3" * 64,
    )
    provider._runtime_template_identity = identity
    provider._runtime_template_audit = RuntimeTemplateAudit(identity=identity)

    async def listed(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        return (
            docker_sbx._RuntimeTemplate(
                "docker.io/library/repotrial-runtime", "1" * 32, image_id
            ),
        )

    monkeypatch.setattr(provider, "_list_runtime_templates", listed)

    async def remove(reference: str, *, deadline: float | None) -> None:
        del reference, deadline

    monkeypatch.setattr(provider, "_remove_runtime_template", remove)

    with pytest.raises(DockerSbxError, match="template_still_present"):
        asyncio.run(provider.finalize_runtime_template())

    assert provider._runtime_template_tag == owned_tag


@pytest.mark.parametrize(
    "failure", [DockerSbxError("template_rm", "nonzero_exit"), asyncio.CancelledError()]
)
def test_runtime_template_finalize_propagates_remove_failure_and_uses_tag(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    spawner = _SbxSpawner()
    provider, _ = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    owned_tag = "repotrial-runtime:" + "7" * 32
    provider._runtime_template_tag = owned_tag
    image_id = "8" * 12
    provider._runtime_template_image_id = image_id
    provider._runtime_template_expected_identity = "9" * 64
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="7" * 32,
        image_id=image_id,
        image_identity_sha256="9" * 64,
    )
    provider._runtime_template_identity = identity
    provider._runtime_template_audit = RuntimeTemplateAudit(identity=identity)
    references: list[str] = []

    async def remove(reference: str, *, deadline: float | None) -> None:
        del deadline
        references.append(reference)
        raise failure

    async def listed(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        return (
            docker_sbx._RuntimeTemplate(
                "docker.io/library/repotrial-runtime",
                "7" * 32,
                provider._runtime_template_image_id or "",
            ),
        )

    monkeypatch.setattr(provider, "_list_runtime_templates", listed)
    monkeypatch.setattr(provider, "_remove_runtime_template", remove)

    with pytest.raises(type(failure)):
        asyncio.run(provider.finalize_runtime_template())

    assert references == [owned_tag]
    assert provider._runtime_template_tag == owned_tag


def test_runtime_template_activation_is_single_use_even_after_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    image_id = "4" * 12
    saved_tag: str | None = None
    template_saved = False

    def handler(command: tuple[str, ...]) -> _Outcome:
        nonlocal saved_tag, template_saved
        if command == ("sbx", "template", "ls", "--json"):
            if not template_saved:
                return _Outcome(stdout=b'{"images":[]}')
            assert saved_tag is not None
            return _Outcome(
                stdout=json.dumps(
                    {
                        "images": [
                            {
                                "id": image_id,
                                "repository": "docker.io/library/repotrial-runtime",
                                "tag": saved_tag.split(":", 1)[1],
                                "flavor": "shell",
                                "created_at": "2026-09-06T00:00:00Z",
                                "size": 1,
                            }
                        ]
                    }
                ).encode()
            )
        if command[:3] == ("sbx", "template", "save"):
            saved_tag = command[-1]
            template_saved = True
        if command[:3] == ("sbx", "template", "rm"):
            template_saved = False
        return _Outcome()

    spawner.handler = handler

    async def exercise() -> None:
        await provider.activate_runtime_template(sandbox_id, "5" * 64)
        await provider.finalize_runtime_template()
        with pytest.raises(RuntimeError, match="already invoked"):
            await provider.activate_runtime_template(sandbox_id, "6" * 64)

    asyncio.run(exercise())


def test_runtime_template_invocation_begin_resets_activation_guard_and_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    provider = _provider(monkeypatch, _SbxSpawner(), total_duration_s=10)
    first_id = _create(provider, tmp_path)
    asyncio.run(provider.destroy(first_id))
    provider._runtime_template_activation_used = True

    asyncio.run(provider.begin_invocation())

    assert provider._runtime_template_activation_used is False
    assert provider._trial_deadline is None
    clock.value = 20
    second_id = _create(provider, tmp_path)
    assert provider._sandbox_deadlines[second_id] == 30.0
    asyncio.run(provider.destroy(second_id))


def test_runtime_template_invocation_begin_requires_previous_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())

    asyncio.run(provider.begin_invocation())
    with pytest.raises(RuntimeError, match="finalization"):
        asyncio.run(provider.begin_invocation())

    asyncio.run(provider.finalize_runtime_template())
    asyncio.run(provider.begin_invocation())


@pytest.mark.parametrize(
    "field",
    ("_runtime_template_tag", "_runtime_template_pending_tag"),
)
def test_runtime_template_invocation_begin_rejects_template_state(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())
    setattr(provider, field, "repotrial-runtime:" + "a" * 32)

    with pytest.raises(RuntimeError, match="template"):
        asyncio.run(provider.begin_invocation())


def test_runtime_template_invocation_begin_rejects_uncleaned_sandbox_or_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())
    provider._sandbox_states["sandbox-17"] = docker_sbx._SandboxState.ACTIVE

    with pytest.raises(RuntimeError, match="sandbox"):
        asyncio.run(provider.begin_invocation())

    provider._sandbox_states["sandbox-17"] = docker_sbx._SandboxState.CLEANED
    provider._sandbox_deadlines["sandbox-17"] = 10.0
    with pytest.raises(RuntimeError, match="deadline"):
        asyncio.run(provider.begin_invocation())


def test_create_uses_active_runtime_template_with_clone_and_policy_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider, _ = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    spawner.handler = lambda command: (
        _Outcome(stdout=b"runtime-image-bundle")
        if command[:2] == ("sbx", "exec")
        else _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )
    warmup_id = next(iter(provider._sandbox_states))
    asyncio.run(
        provider.stage_runtime_image_bundle(
            warmup_id,
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "a" * 64,),
        )
    )
    provider._runtime_template_tag = "repotrial-runtime:" + "c" * 32
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="c" * 32,
        image_id="c" * 12,
        image_identity_sha256="a" * 64,
    )
    provider._runtime_template_identity = identity
    provider._runtime_template_image_id = identity.image_id
    provider._runtime_template_expected_identity = identity.image_identity_sha256
    provider._runtime_template_audit = RuntimeTemplateAudit(
        identity=identity,
        bundle_sha256=provider._runtime_image_bundle_sha256,
        bundle_size=provider._runtime_image_bundle_size,
    )

    try:
        sandbox_id = _create(provider, tmp_path)

        create_call = _actual_create_call(spawner)
        assert create_call[4] == "--clone"
        assert ("--template", provider._runtime_template_tag) == create_call[-4:-2]
        assert create_call[-2:] == ("shell", str(tmp_path))
        assert sandbox_id in create_call
        load_call = ("sbx", "exec", "-i", sandbox_id, "--", "docker", "image", "load")
        load_index = spawner.calls.index(load_call)
        assert spawner.calls.index(create_call) < load_index
        assert spawner.processes[load_index].stdin.data == b"runtime-image-bundle"
    finally:
        asyncio.run(provider.finalize_runtime_template())


def test_create_rejects_active_runtime_template_without_complete_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider, _ = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    tag = "repotrial-runtime:" + "c" * 32
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="c" * 32,
        image_id="c" * 12,
        image_identity_sha256="a" * 64,
    )
    provider._runtime_template_tag = tag
    provider._runtime_template_identity = identity
    provider._runtime_template_image_id = identity.image_id
    provider._runtime_template_expected_identity = identity.image_identity_sha256
    provider._runtime_template_audit = RuntimeTemplateAudit(identity=identity)

    with pytest.raises(DockerSbxError, match="image_bundle"):
        _create(provider, tmp_path)

    assert provider.runtime_template_audit().uses == ()


def test_finalize_keeps_bundle_audit_after_template_and_bundle_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    spawner.handler = lambda command: _Outcome(stdout=b"bundle")
    asyncio.run(
        provider.stage_runtime_image_bundle(
            sandbox_id,
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "a" * 64,),
        )
    )
    expected_hash = hashlib.sha256(b"bundle").hexdigest()

    asyncio.run(provider.finalize_runtime_template())

    audit = provider.runtime_template_audit()
    assert audit.bundle_sha256 == expected_hash
    assert audit.bundle_size == len(b"bundle")
    assert audit.removal_confirmed is True


def test_finalize_retries_bundle_after_template_cleanup_was_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())
    owned_tag = "repotrial-runtime:" + "a" * 32
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="a" * 32,
        image_id="b" * 12,
        image_identity_sha256="c" * 64,
    )
    provider._runtime_template_tag = owned_tag
    provider._runtime_template_image_id = identity.image_id
    provider._runtime_template_expected_identity = identity.image_identity_sha256
    provider._runtime_template_identity = identity
    provider._runtime_template_audit = RuntimeTemplateAudit(identity=identity)
    fd, raw_path = tempfile.mkstemp(prefix="repotrial-image-bundle-")
    bundle = os.fdopen(fd, "w+b")
    bundle.write(b"bundle")
    bundle.flush()
    bundle.seek(0)
    provider._runtime_image_bundle_path = Path(raw_path)
    provider._runtime_image_bundle_file = bundle
    bundle_stat = os.fstat(bundle.fileno())
    provider._runtime_image_bundle_file_identity = (
        bundle_stat.st_dev,
        bundle_stat.st_ino,
    )
    provider._runtime_image_bundle_sha256 = hashlib.sha256(b"bundle").hexdigest()
    provider._runtime_image_bundle_size = len(b"bundle")
    removed = False

    async def listed(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        if removed:
            return ()
        return (
            docker_sbx._RuntimeTemplate(
                "docker.io/library/repotrial-runtime", "a" * 32, "b" * 12
            ),
        )

    async def remove(reference: str, *, deadline: float | None) -> None:
        nonlocal removed
        del deadline
        assert reference == owned_tag
        removed = True

    first_bundle_cleanup = True
    original_bundle_cleanup = provider._cleanup_runtime_image_bundle

    async def cleanup_bundle() -> None:
        nonlocal first_bundle_cleanup
        if first_bundle_cleanup:
            first_bundle_cleanup = False
            raise DockerSbxError("template_finalize", "bundle_cleanup_failed")
        await original_bundle_cleanup()

    monkeypatch.setattr(provider, "_list_runtime_templates", listed)
    monkeypatch.setattr(provider, "_remove_runtime_template", remove)
    monkeypatch.setattr(provider, "_cleanup_runtime_image_bundle", cleanup_bundle)

    with pytest.raises(DockerSbxError, match="bundle_cleanup_failed"):
        asyncio.run(provider.finalize_runtime_template())
    asyncio.run(provider.finalize_runtime_template())
    assert provider.runtime_template_audit().removal_confirmed is True


def test_stage_rejects_temp_root_inside_current_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    monkeypatch.setattr(docker_sbx.tempfile, "gettempdir", lambda: str(Path.cwd()))
    spawner.handler = lambda command: _Outcome(stdout=b"bundle")

    with pytest.raises(DockerSbxError, match="temp_root"):
        asyncio.run(
            provider.stage_runtime_image_bundle(
                sandbox_id,
                ("docker.io/library/alpine:latest",),
                ("sha256:" + "a" * 64,),
            )
        )


def test_finalize_retries_unlink_after_close_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    spawner.handler = lambda command: (
        _Outcome(stdout=b"bundle")
        if command[:2] == ("sbx", "exec")
        else _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )
    asyncio.run(
        provider.stage_runtime_image_bundle(
            sandbox_id,
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "a" * 64,),
        )
    )
    bundle_path = provider._runtime_image_bundle_path
    assert bundle_path is not None
    original_unlink = Path.unlink
    fail_once = True

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal fail_once
        if path == bundle_path and fail_once:
            fail_once = False
            raise PermissionError("synthetic unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(DockerSbxError, match="image_bundle_cleanup_failed"):
        asyncio.run(provider.finalize_runtime_template())
    asyncio.run(provider.finalize_runtime_template())
    assert provider.runtime_template_audit().removal_confirmed is True


def test_mixed_cleanup_preserves_bundle_audit_when_template_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    spawner.handler = lambda command: (
        _Outcome(stdout=b"bundle")
        if command[:2] == ("sbx", "exec")
        else _Outcome(stdout=b'{"images":[]}')
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )
    asyncio.run(
        provider.stage_runtime_image_bundle(
            sandbox_id,
            ("docker.io/library/alpine:latest",),
            ("sha256:" + "a" * 64,),
        )
    )
    identity = RuntimeTemplateIdentity(
        repository="docker.io/library/repotrial-runtime",
        tag="a" * 32,
        image_id="b" * 12,
        image_identity_sha256="c" * 64,
    )
    provider._runtime_template_tag = "repotrial-runtime:" + "a" * 32
    provider._runtime_template_identity = identity
    provider._runtime_template_image_id = identity.image_id
    provider._runtime_template_expected_identity = identity.image_identity_sha256
    provider._runtime_template_audit = RuntimeTemplateAudit(
        identity=identity,
        bundle_sha256=provider._runtime_image_bundle_sha256,
        bundle_size=provider._runtime_image_bundle_size,
    )

    async def fail_template_remove(reference: str, *, deadline: float | None) -> None:
        del reference, deadline
        raise DockerSbxError("template_rm", "template_cleanup_failed")

    async def list_owned_template(deadline: float | None = None) -> tuple[object, ...]:
        del deadline
        return (
            docker_sbx._RuntimeTemplate(
                "docker.io/library/repotrial-runtime", "a" * 32, "b" * 12
            ),
        )

    monkeypatch.setattr(provider, "_list_runtime_templates", list_owned_template)
    monkeypatch.setattr(provider, "_remove_runtime_template", fail_template_remove)
    with pytest.raises(DockerSbxError, match="template_cleanup_failed"):
        asyncio.run(provider.finalize_runtime_template())
    audit = provider.runtime_template_audit()
    assert audit.bundle_sha256 == hashlib.sha256(b"bundle").hexdigest()
    assert audit.bundle_size == len(b"bundle")
    assert audit.removal_confirmed is False


def test_stage_fstat_failure_refuses_to_remove_unknown_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch, spawner, deadline=time.monotonic() + 300.0
    )
    monkeypatch.setattr(docker_sbx, "_image_bundle_temp_root", lambda _: str(tmp_path))

    def fail_fstat(fd: int) -> os.stat_result:
        del fd
        raise OSError("synthetic fstat failure")

    monkeypatch.setattr(os, "fstat", fail_fstat)
    with pytest.raises(DockerSbxError):
        asyncio.run(
            provider.stage_runtime_image_bundle(
                sandbox_id,
                ("docker.io/library/alpine:latest",),
                ("sha256:" + "a" * 64,),
            )
        )
    assert provider._runtime_image_bundle_path is not None
    assert provider._runtime_image_bundle_path.is_file()
    assert provider._runtime_template_audit.removal_confirmed is False


def test_bundle_cleanup_preserves_regular_file_when_identity_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch, _SbxSpawner())
    sentinel = tmp_path / "replacement.tar"
    sentinel.write_bytes(b"unrelated replacement")
    provider._runtime_image_bundle_path = sentinel

    with pytest.raises(DockerSbxError, match="image_bundle_cleanup_failed"):
        asyncio.run(provider.finalize_runtime_template())

    assert sentinel.read_bytes() == b"unrelated replacement"
    assert provider._runtime_image_bundle_path == sentinel
    assert provider._runtime_template_audit.removal_confirmed is False


def test_runtime_template_rejects_preexisting_owned_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=time.monotonic() + 300.0,
    )
    monkeypatch.setattr(
        docker_sbx.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="d" * 32),
    )
    existing = json.dumps(
        {
            "images": [
                {
                    "id": "a" * 12,
                    "repository": "docker.io/library/repotrial-runtime",
                    "tag": "d" * 32,
                    "flavor": "shell",
                    "created_at": "2026-09-06T00:00:00Z",
                    "size": 1,
                }
            ]
        }
    ).encode()
    spawner.handler = lambda command: (
        _Outcome(stdout=existing)
        if command == ("sbx", "template", "ls", "--json")
        else _Outcome()
    )

    with pytest.raises(DockerSbxError, match="template_tag_collision"):
        asyncio.run(provider.activate_runtime_template(sandbox_id, "b" * 64))

    assert ("sbx", "template", "save", sandbox_id) not in spawner.calls


@pytest.mark.parametrize(
    "output",
    [
        b"{",
        (
            b'{"images":[{"id":"bad","repository":"docker.io/library/repotrial-runtime",'
            b'"tag":"x","flavor":"shell","created_at":"now","size":1}]}'
        ),
        (
            b'{"images":[{"id":"aaaaaaaaaaaa","repository":"docker.io/library/repotrial-runtime",'
            b'"tag":"x","flavor":"shell","created_at":"now","size":1,"id":"bbbbbbbbbbbbbb"}]}'
        ),
    ],
)
def test_runtime_template_listing_rejects_malformed_or_duplicate_records(
    output: bytes,
) -> None:
    with pytest.raises(DockerSbxError, match="template_list_invalid"):
        docker_sbx._parse_runtime_templates(output)


def test_runtime_template_listing_accepts_real_schema_and_normalized_match() -> None:
    tag = "repotrial-runtime:" + "a" * 32
    output = json.dumps(
        {
            "images": [
                {
                    "id": "b" * 12,
                    "repository": "docker.io/library/repotrial-runtime",
                    "tag": "a" * 32,
                    "flavor": "shell",
                    "created_at": "2026-09-06T00:00:00Z",
                    "size": 123,
                }
            ]
        }
    ).encode()

    templates = docker_sbx._parse_runtime_templates(output)

    assert len(templates) == 1
    assert docker_sbx._runtime_template_matches(templates[0], tag)
    assert not docker_sbx._runtime_template_matches(
        docker_sbx._RuntimeTemplate("repotrial-runtime", "a" * 32, "b" * 12),
        tag,
    )


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
    host_docker_config = tmp_path / "host-docker-config"
    host_docker_config.mkdir()
    (host_docker_config / "config.json").write_text(
        '{"auths":{"registry.example":{"auth":"must-not-leak"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(host_docker_config))
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "host-auth-must-not-leak")
    monkeypatch.setenv("REGISTRY_AUTH_FILE", "host-registry-auth-must-not-leak")
    monkeypatch.setenv("REPOTRIAL_TEST_SENTINEL", "preserved")
    spawner = _SbxSpawner()

    def assert_anonymous_config_is_live_and_empty(
        command: tuple[str, ...], kwargs: dict[str, object]
    ) -> None:
        if command[:2] != ("sbx", "create") or command[-1] == "--help":
            return
        environment = cast(dict[str, str], kwargs["env"])
        docker_config = Path(environment["DOCKER_CONFIG"])
        assert docker_config.is_dir()
        assert list(docker_config.iterdir()) == []

    spawner.before_spawn_with_kwargs = assert_anonymous_config_is_live_and_empty
    provider = _provider(monkeypatch, spawner)

    sandbox_id = _create(provider, tmp_path)

    assert spawner.calls[1 : len(PROBE_CALLS) + 1] == PROBE_CALLS
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
    assert spawner.calls[create_index + 1 : create_index + 6] == [
        ("sbx", "exec", sandbox_id, "--", "pwd"),
        ("sbx", "exec", sandbox_id, "--", "git", "rev-parse", "--show-toplevel"),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--is-inside-work-tree",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ),
    ]
    assert "--pids-limit" not in _actual_create_call(spawner)
    assert sandbox_id.startswith("repotrial-trial-")
    assert all("shell" not in kwargs for kwargs in spawner.kwargs)
    create_env = cast(dict[str, str], spawner.kwargs[create_index].get("env"))
    assert create_env["DOCKER_SANDBOXES_ROOT_SIZE"] == "1m"
    assert create_env["DOCKER_SANDBOXES_DOCKER_SIZE"] == "1919m"
    assert create_env["DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE"] == "128m"
    anonymous_docker_config = Path(create_env["DOCKER_CONFIG"])
    assert anonymous_docker_config != host_docker_config
    assert anonymous_docker_config.name.startswith("repotrial-docker-config-")
    assert not anonymous_docker_config.exists()
    assert "DOCKER_AUTH_CONFIG" not in create_env
    assert "REGISTRY_AUTH_FILE" not in create_env
    assert create_env["REPOTRIAL_TEST_SENTINEL"] == "preserved"
    for index, kwargs in enumerate(spawner.kwargs):
        if index == create_index:
            continue
        environment = cast(dict[str, str], kwargs.get("env"))
        assert environment["REPOTRIAL_TEST_SENTINEL"] == "preserved"
        assert environment["DOCKER_CONFIG"] == str(host_docker_config)
        assert environment["DOCKER_AUTH_CONFIG"] == "host-auth-must-not-leak"
        assert environment["REGISTRY_AUTH_FILE"] == "host-registry-auth-must-not-leak"
        assert disk_keys.isdisjoint(environment)
    assert not any(
        call[:2] == ("sbx", "exec")
        and any("df -B1 -P" in argument for argument in call)
        for call in spawner.calls
    )


def test_anonymous_config_creation_failure_is_structured_before_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    def fail_mkdtemp(*_args: object, **_kwargs: object) -> str:
        raise OSError("anonymous config unavailable")

    monkeypatch.setattr(docker_sbx.tempfile, "mkdtemp", fail_mkdtemp)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert (raised.value.operation, raised.value.reason) == (
        "create",
        "anonymous_config_io",
    )
    assert _non_help_create_calls(spawner) == []
    assert provider._sandbox_states == {}


def test_unreaped_create_retains_anonymous_config_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anonymous_config = tmp_path / "retained-anonymous-config"

    def make_known_config(*_args: object, **_kwargs: object) -> str:
        anonymous_config.mkdir()
        return str(anonymous_config)

    monkeypatch.setattr(docker_sbx.tempfile, "mkdtemp", make_known_config)
    monkeypatch.setattr(docker_sbx, "REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True, reap_hang=True)
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner, command_timeout_s=0.05)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == "process_cleanup_unconfirmed"
    assert anonymous_config.is_dir()
    assert "anonymous Docker config retained" in " ".join(raised.value.__notes__)
    anonymous_config.rmdir()


def test_cancelled_unreaped_create_retains_anonymous_config_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anonymous_config = tmp_path / "retained-anonymous-config"

    def make_known_config(*_args: object, **_kwargs: object) -> str:
        anonymous_config.mkdir()
        return str(anonymous_config)

    monkeypatch.setattr(docker_sbx.tempfile, "mkdtemp", make_known_config)
    monkeypatch.setattr(docker_sbx, "REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(hang=True, kill_error=OSError("kill denied"))
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    async def exercise() -> None:
        task = asyncio.create_task(provider.create(tmp_path, "trial"))
        while not _non_help_create_calls(spawner):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        notes = " ".join(raised.value.__notes__)
        assert "process cleanup unconfirmed" in notes
        assert "anonymous Docker config retained" in notes

    asyncio.run(exercise())

    assert anonymous_config.is_dir()
    anonymous_config.rmdir()


def test_anonymous_config_cleanup_failure_aborts_and_cleans_partial_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anonymous_config = tmp_path / "anonymous-config"

    def make_known_config(*_args: object, **_kwargs: object) -> str:
        anonymous_config.mkdir()
        return str(anonymous_config)

    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(docker_sbx.tempfile, "mkdtemp", make_known_config)
    monkeypatch.setattr(docker_sbx.shutil, "rmtree", fail_cleanup)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    assert (raised.value.operation, raised.value.reason) == (
        "create",
        "anonymous_config_cleanup",
    )
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)
    assert provider._sandbox_states[sandbox_id] is docker_sbx._SandboxState.CLEANED
    anonymous_config.rmdir()


def test_anonymous_config_cleanup_failure_does_not_mask_create_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anonymous_config = tmp_path / "anonymous-config"

    def make_known_config(*_args: object, **_kwargs: object) -> str:
        anonymous_config.mkdir()
        return str(anonymous_config)

    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(docker_sbx.tempfile, "mkdtemp", make_known_config)
    monkeypatch.setattr(docker_sbx.shutil, "rmtree", fail_cleanup)
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(returncode=7, stderr=b"create failed")
        if command[:2] == ("sbx", "create") and command[-1] != "--help"
        else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    assert raised.value.reason == "nonzero_exit"
    assert "anonymous Docker config cleanup failed" in " ".join(raised.value.__notes__)
    anonymous_config.rmdir()


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
    assert spawner.calls[-2:] == [
        allow_call,
        ("sbx", "rm", "--force", sandbox_id),
    ]
    assert provider._trial_deadline is None
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
    assert provider._trial_deadline is None
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
    assert not _actual_create_config_path(spawner).exists()
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
    primary_evidence = get_sandbox_failure_evidence(raised.value)
    assert primary_evidence is not None
    assert primary_evidence.operation == "create"
    assert primary_evidence.reason == "nonzero_exit"
    assert primary_evidence.returncode == 7
    assert primary_evidence.sandbox_id == sandbox_id
    assert primary_evidence.deadline_limited is False
    assert primary_evidence.subprocess_started is True
    assert primary_evidence.trial_elapsed_s is not None
    assert primary_evidence.trial_remaining_s is not None
    cleanup_evidence = get_sandbox_failure_evidence(context.cleanup_failure)
    assert cleanup_evidence is not None
    assert cleanup_evidence.operation == "cleanup"
    assert cleanup_evidence.reason == "nonzero_exit"
    assert cleanup_evidence.returncode == 8
    assert cleanup_evidence.sandbox_id == sandbox_id
    assert cleanup_evidence.deadline_limited is False
    assert cleanup_evidence.subprocess_started is True
    assert cleanup_evidence.trial_elapsed_s is None
    assert cleanup_evidence.trial_remaining_s is None
    assert cleanup_evidence is not primary_evidence

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
    create_index = spawner.calls.index(create_call)
    assert spawner.processes[create_index].killed is True
    assert spawner.processes[create_index].waited is True
    assert not _actual_create_config_path(spawner).exists()


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
    assert not _actual_create_config_path(spawner).exists()
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
    assert {key: value for key, value in events[1].items() if key != "failure"} == {
        "cleanup_exception_type": "DockerSbxError",
        "event": "create_cleanup_unsafe",
        "exception_type": "DockerSbxError",
        "sandbox_id": sandbox_id,
        "state": "cleanup_unsafe",
    }
    failure_evidence = cast(dict[str, object], events[1]["failure"])
    assert {
        key: failure_evidence[key]
        for key in (
            "deadline_limited",
            "operation",
            "reason",
            "returncode",
            "sandbox_id",
            "subprocess_started",
        )
    } == {
        "deadline_limited": False,
        "operation": "create",
        "reason": "nonzero_exit",
        "returncode": 7,
        "sandbox_id": sandbox_id,
        "subprocess_started": True,
    }
    assert isinstance(failure_evidence["trial_elapsed_s"], float)
    assert isinstance(failure_evidence["trial_remaining_s"], float)
    assert events[2:] == [
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
    assert {key: value for key, value in events[-1].items() if key != "failure"} == {
        "event": "cleanup_retry_failure",
        "exception_type": "DockerSbxError",
        "sandbox_id": sandbox_id,
    }
    failure_evidence = cast(dict[str, object], events[-1]["failure"])
    assert {
        key: failure_evidence[key]
        for key in (
            "deadline_limited",
            "operation",
            "reason",
            "returncode",
            "sandbox_id",
            "subprocess_started",
        )
    } == {
        "deadline_limited": False,
        "operation": "destroy",
        "reason": "nonzero_exit",
        "returncode": 8,
        "sandbox_id": sandbox_id,
        "subprocess_started": True,
    }
    assert "trial_elapsed_s" not in failure_evidence
    assert "trial_remaining_s" not in failure_evidence
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
        pytest.param(b"[]", id="missing-requested-mapping"),
        pytest.param(b"not-json", id="malformed-json"),
        pytest.param(b'{"host_ip":"127.0.0.1"}', id="non-list-json"),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":0,'
            b'"sandbox_port":8080,"protocol":"tcp4"}]',
            id="host-port-out-of-range",
        ),
        pytest.param(
            b'[{"host_ip":"0.0.0.0","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"tcp4"}]',
            id="unsafe-host-ip",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"tcp4","extra":1}]',
            id="extra-key",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"tcp4"},'
            b'{"host_ip":"127.0.0.1","host_port":49153,'
            b'"sandbox_port":8080,"protocol":"tcp4"}]',
            id="ambiguous-requested-mapping",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"tcp"}]',
            id="requested-tcp",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"udp"}]',
            id="requested-unknown-protocol",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":[]}]',
            id="malformed-protocol-type",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":true,'
            b'"sandbox_port":9418,"protocol":"tcp"},'
            b'{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"tcp4"}]',
            id="unrelated-non-integer-port",
        ),
        pytest.param(
            b'[{"host_ip":"127.0.0.1","host_port":49153,'
            b'"sandbox_port":9418,"protocol":"udp"},'
            b'{"host_ip":"127.0.0.1","host_port":49152,'
            b'"sandbox_port":8080,"protocol":"tcp4"}]',
            id="unrelated-unknown-protocol",
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


def test_publish_port_rejects_duplicate_json_key_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        b'[{"host_ip":"127.0.0.1","host_port":49151,'
        b'"host_port":49152,"sandbox_port":8080,"protocol":"tcp4"}]'
    )
    spawner = _SbxSpawner()
    spawner.handler = lambda command: (
        _Outcome(stdout=payload) if command[-1:] == ("--json",) else _Outcome()
    )
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)

    with pytest.raises(DockerSbxError, match="port_mapping"):
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


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


def test_publish_port_accepts_strict_unrelated_tcp_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        b'[{"host_ip":"127.0.0.1","host_port":49151,'
        b'"sandbox_port":9418,"protocol":"tcp"},'
        b'{"host_ip":"127.0.0.1","host_port":49152,'
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


@pytest.mark.parametrize(
    ("case", "stdout", "stderr", "should_clean"),
    [
        (
            "observed exact-ID absence",
            b"",
            OBSERVED_ABSENCE_STDERR,
            True,
        ),
        (
            "wrong ID",
            b"",
            (
                b"WARN: could not acquire docker hub refresh lock, proceeding without cross-process lock: "
                b"context deadline exceeded\n"
                b"Error: sandbox \x27other-id\x27 not found "
                b"(run \x27sbx ls\x27 to see your sandboxes)\n"
            ),
            False,
        ),
        (
            "generic not-found",
            b"",
            b"Error: sandbox not found\n",
            False,
        ),
        (
            "ambiguous suffix",
            b"",
            b"Error: sandbox \x27sandbox-17\x27 not found or unavailable\n",
            False,
        ),
        ("other error", b"", b"cleanup failed\n", False),
        (
            "invalid stderr",
            b"",
            b"Error: sandbox \x27sandbox-17\x27 not found\xff\n",
            False,
        ),
        ("nonempty stdout", b"unexpected\n", OBSERVED_ABSENCE_STDERR, False),
        ("invalid stdout", b"\xff", OBSERVED_ABSENCE_STDERR, False),
        (
            "extra error line",
            b"",
            OBSERVED_ABSENCE_STDERR + b"Error: cleanup failed\n",
            False,
        ),
        (
            "cross-line token",
            b"",
            b"Error: sandbox\n\x27sandbox-17\x27 not found\n",
            False,
        ),
    ],
)
def test_destroy_requires_strict_exact_id_absence_result(
    case: str,
    stdout: bytes,
    stderr: bytes,
    should_clean: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert case
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    remove_command = ("sbx", "rm", "--force", sandbox_id)
    spawner.overrides[remove_command] = _Outcome(
        returncode=1, stdout=stdout, stderr=stderr
    )

    if should_clean:
        asyncio.run(provider.destroy(sandbox_id))
        assert provider._sandbox_states[sandbox_id] is docker_sbx._SandboxState.CLEANED
        calls_before_destroy = len(spawner.calls)
        asyncio.run(provider.destroy(sandbox_id))
        assert len(spawner.calls) == calls_before_destroy
    else:
        with pytest.raises(DockerSbxError) as raised:
            asyncio.run(provider.destroy(sandbox_id))
        assert raised.value.reason == "nonzero_exit"
        assert (
            provider._sandbox_states[sandbox_id]
            is docker_sbx._SandboxState.CLEANUP_UNSAFE
        )
        del spawner.overrides[remove_command]
        asyncio.run(provider.destroy(sandbox_id))
        assert provider._sandbox_states[sandbox_id] is docker_sbx._SandboxState.CLEANED
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
        while command not in spawner.calls:
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
        while command not in spawner.calls:
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
        while command not in spawner.calls:
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


def _blocked_network_log_entry(sandbox_id: str) -> dict[str, object]:
    return {
        "host": "10.0.0.1",
        "vm_name": sandbox_id,
        "proxy_type": "network",
        "rule": "private",
        "last_seen": "2026-08-26T00:00:00Z",
        "since": "2026-08-25T23:59:00Z",
        "count_since": 2,
        "reason": "deny",
    }


def _allowed_network_log_entry(sandbox_id: str) -> dict[str, object]:
    return {
        "host": "api.example.test",
        "vm_name": sandbox_id,
        "proxy_type": "forward",
        "rule": "**",
        "last_seen": "2026-08-26T00:01:00Z",
        "since": "2026-08-26T00:00:30Z",
        "count_since": 1,
    }


def _network_log_wrapper(
    sandbox_id: str,
) -> dict[str, list[dict[str, object]]]:
    return {
        "blocked_hosts": [_blocked_network_log_entry(sandbox_id)],
        "allowed_hosts": [_allowed_network_log_entry(sandbox_id)],
    }


def _set_network_log_output(
    spawner: _SbxSpawner, sandbox_id: str, payload: bytes
) -> None:
    spawner.overrides[
        ("sbx", "policy", "log", sandbox_id, "--type", "network", "--json")
    ] = _Outcome(stdout=payload)


def test_network_log_returns_strict_observed_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    payload = json.dumps(
        {
            "blocked_hosts": [
                {
                    "host": "10.0.0.1",
                    "vm_name": sandbox_id,
                    "proxy_type": "network",
                    "rule": "private",
                    "last_seen": "2026-08-26T00:00:00Z",
                    "since": "2026-08-25T23:59:00Z",
                    "count_since": 2,
                    "reason": "deny",
                }
            ],
            "allowed_hosts": [
                {
                    "host": "api.example.test",
                    "vm_name": sandbox_id,
                    "proxy_type": "forward",
                    "rule": "**",
                    "last_seen": "2026-08-26T00:01:00Z",
                    "since": "2026-08-26T00:00:30Z",
                    "count_since": 1,
                }
            ],
        }
    ).encode()
    _set_network_log_output(spawner, sandbox_id, payload)

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
            "count": 2,
        },
        {
            "sandbox": sandbox_id,
            "decision": "allowed",
            "host": "api.example.test",
            "proxy": "forward",
            "rule": "**",
            "reason": "",
            "last_seen": "2026-08-26T00:01:00Z",
            "count": 1,
        },
    ]


def test_network_log_empty_wrapper_is_supported_observability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    _set_network_log_output(
        spawner,
        sandbox_id,
        b'{"blocked_hosts":[],"allowed_hosts":[]}',
    )

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result == NetworkLogResult(events=[], supported=True)


def test_network_log_rejects_duplicate_top_level_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    _set_network_log_output(
        spawner,
        sandbox_id,
        b'{"blocked_hosts":[],"blocked_hosts":[],"allowed_hosts":[]}',
    )

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_invalid_json_contract"


def test_network_log_rejects_duplicate_nested_entry_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    payload = (
        '{"blocked_hosts":[],"allowed_hosts":[{'
        '"host":"api.example.test","host":"api.example.test",'
        f'"vm_name":"{sandbox_id}","proxy_type":"forward","rule":"**",'
        '"last_seen":"2026-08-26T00:01:00Z",'
        '"since":"2026-08-26T00:00:30Z","count_since":1'
        "}]}"
    ).encode()
    _set_network_log_output(spawner, sandbox_id, payload)

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_invalid_json_contract"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not-json", id="invalid-json"),
        pytest.param(b"[]", id="fictional-flat-list"),
        pytest.param(b"{}", id="missing-wrapper-keys"),
        pytest.param(
            b'{"blocked_hosts":[],"allowed_hosts":[],"extra":[]}',
            id="extra-wrapper-key",
        ),
        pytest.param(
            b'{"blocked_hosts":{},"allowed_hosts":[]}',
            id="blocked-hosts-not-list",
        ),
        pytest.param(
            b'{"blocked_hosts":[],"allowed_hosts":null}',
            id="allowed-hosts-not-list",
        ),
        pytest.param(
            b'{"blocked_hosts":[null],"allowed_hosts":[]}',
            id="entry-not-object",
        ),
    ],
)
def test_network_log_rejects_malformed_top_level_contract(
    payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    _set_network_log_output(spawner, sandbox_id, payload)

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.events == []
    assert result.supported is False
    assert result.unsupported_reason == "network_log_invalid_json_contract"


@pytest.mark.parametrize(
    ("bucket", "operation", "key"),
    [
        pytest.param("blocked_hosts", "remove", "reason", id="blocked-missing-key"),
        pytest.param("blocked_hosts", "add", "extra", id="blocked-extra-key"),
        pytest.param("allowed_hosts", "remove", "since", id="allowed-missing-key"),
        pytest.param("allowed_hosts", "add", "reason", id="allowed-extra-key"),
    ],
)
def test_network_log_rejects_inexact_entry_keys(
    bucket: str,
    operation: str,
    key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    wrapper = _network_log_wrapper(sandbox_id)
    entry = wrapper[bucket][0]
    if operation == "remove":
        entry.pop(key)
    else:
        entry[key] = "unexpected"
    _set_network_log_output(spawner, sandbox_id, json.dumps(wrapper).encode())

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_invalid_json_contract"


@pytest.mark.parametrize(
    ("bucket", "key", "value"),
    [
        pytest.param("blocked_hosts", "vm_name", "wrong", id="wrong-vm"),
        pytest.param("allowed_hosts", "proxy_type", "unknown", id="unknown-proxy"),
        pytest.param("blocked_hosts", "count_since", 0, id="zero-count"),
        pytest.param("allowed_hosts", "count_since", True, id="boolean-count"),
        pytest.param("blocked_hosts", "count_since", "1", id="string-count"),
        pytest.param("blocked_hosts", "host", 1, id="non-string-host"),
        pytest.param("allowed_hosts", "rule", None, id="non-string-rule"),
        pytest.param("blocked_hosts", "reason", [], id="non-string-reason"),
        pytest.param("blocked_hosts", "host", "", id="empty-host"),
        pytest.param("allowed_hosts", "last_seen", "", id="empty-last-seen"),
        pytest.param("blocked_hosts", "since", "", id="empty-since"),
        pytest.param("allowed_hosts", "rule", "x" * 1025, id="unbounded-string"),
    ],
)
def test_network_log_rejects_invalid_entry_values(
    bucket: str,
    key: str,
    value: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    wrapper = _network_log_wrapper(sandbox_id)
    wrapper[bucket][0][key] = value
    _set_network_log_output(spawner, sandbox_id, json.dumps(wrapper).encode())

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_invalid_json_contract"


def test_network_log_rejects_combined_event_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    wrapper = {
        "blocked_hosts": [_blocked_network_log_entry(sandbox_id) for _ in range(50)],
        "allowed_hosts": [_allowed_network_log_entry(sandbox_id) for _ in range(51)],
    }
    _set_network_log_output(spawner, sandbox_id, json.dumps(wrapper).encode())

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.unsupported_reason == "network_log_invalid_json_contract"


def test_network_log_accepts_combined_event_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    wrapper = {
        "blocked_hosts": [_blocked_network_log_entry(sandbox_id) for _ in range(50)],
        "allowed_hosts": [_allowed_network_log_entry(sandbox_id) for _ in range(50)],
    }
    _set_network_log_output(spawner, sandbox_id, json.dumps(wrapper).encode())

    result = asyncio.run(provider.network_log(sandbox_id))

    assert result.supported is True
    assert len(result.events) == 100


def test_network_log_rejects_invalid_utf8_that_lossy_decode_would_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)
    sandbox_id = _create(provider, tmp_path)
    payload = (
        json.dumps(_network_log_wrapper(sandbox_id))
        .encode()
        .replace(b'"deny"', b'"\xff"')
    )
    _set_network_log_output(spawner, sandbox_id, payload)

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
    provider._sandbox_deadlines[sandbox_id] = time.monotonic() + 0.1

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
        total_duration_s=23,
    )

    sandbox_id = _create(provider, tmp_path)

    timed_commands = _timeouts_by_command(spawner, recorder)
    assert timed_commands[0][0][:2] == ("git", "-C")
    assert timed_commands[0][1] == 23.0
    assert timed_commands[1 : len(PROBE_CALLS) + 1] == list(
        zip(
            PROBE_CALLS,
            (
                22.0,
                21.0,
                20.0,
                19.0,
                18.0,
                17.0,
                16.0,
                15.0,
                14.0,
                13.0,
                12.0,
                11.0,
                10.0,
            ),
            strict=True,
        )
    )
    create_call = _actual_create_call(spawner)
    assert timed_commands[len(PROBE_CALLS) + 1][0][:2] == ("git", "-C")
    assert timed_commands[len(PROBE_CALLS) + 1][1] == 9.0
    assert [timeout for _, timeout in timed_commands[-7:]] == [
        8.0,
        7.0,
        6.0,
        5.0,
        4.0,
        3.0,
        2.0,
    ]
    assert [command for command, _ in timed_commands[-7:]] == [
        create_call,
        ("sbx", "exec", sandbox_id, "--", "pwd"),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--show-toplevel",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--is-inside-work-tree",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        ),
        (
            "sbx",
            "exec",
            sandbox_id,
            "--",
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ),
        ("sbx", "policy", "allow", "network", "--sandbox", sandbox_id, "**"),
    ]
    assert provider._sandbox_deadlines == {sandbox_id: 23.0}


@pytest.mark.parametrize(
    ("command_timeout_s", "total_duration_s", "expected_timeout_s"),
    [
        (None, 300, 120.0),
        (7, 300, 7.0),
        (None, 60, 60.0),
    ],
)
def test_create_timeout_uses_default_or_override_bounded_by_trial_duration(
    command_timeout_s: float | None,
    total_duration_s: int,
    expected_timeout_s: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    recorder = _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    provider_kwargs = (
        {} if command_timeout_s is None else {"command_timeout_s": command_timeout_s}
    )
    provider = DockerSbxProvider(
        _policy(total_duration_s=total_duration_s), **provider_kwargs
    )

    _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    create_timeout = dict(_timeouts_by_command(spawner, recorder))[create_call]
    assert create_timeout == expected_timeout_s


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

    assert raised.value.operation == "clone_verification"
    assert raised.value.reason == "total_duration_exhausted"
    assert spawner.calls == []


def test_exhausted_deadline_records_no_subprocess_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(10.0)
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=clock.value,
        total_duration_s=300,
    )
    provider._trial_deadline = clock.value
    command = ("sbx", "exec", sandbox_id, "--", "echo", "never-started")

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.exec(sandbox_id, ["echo", "never-started"]))

    assert (raised.value.operation, raised.value.reason) == (
        "exec",
        "total_duration_exhausted",
    )
    assert str(raised.value) == (
        "docker sandboxes exec failed: total_duration_exhausted "
        f"(sandbox_id={sandbox_id})"
    )
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "exec"
    assert evidence.reason == "total_duration_exhausted"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is True
    assert evidence.subprocess_started is False
    assert evidence.trial_elapsed_s == 300.0
    assert evidence.trial_remaining_s == 0.0
    assert command not in spawner.calls
    assert spawner.processes == []


def test_in_flight_deadline_timeout_records_subprocess_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(
        monkeypatch,
        spawner,
        deadline=10.0,
        command_timeout_s=30,
        total_duration_s=10,
    )
    command = ("sbx", "exec", sandbox_id, "--", "sleep", "forever")
    spawner.overrides[command] = _Outcome(hang=True)
    process_created = asyncio.Event()

    def signal_spawn(spawned_command: tuple[str, ...]) -> None:
        if spawned_command == command:
            clock.value = 10.0
            process_created.set()

    spawner.before_spawn = signal_spawn
    monkeypatch.setattr(
        docker_sbx.asyncio,
        "timeout",
        lambda _: _TimeoutAfterProcessCreation(process_created),
    )

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.exec(sandbox_id, ["sleep", "forever"]))

    assert (raised.value.operation, raised.value.reason) == (
        "exec",
        "total_duration_exhausted",
    )
    assert str(raised.value) == (
        "docker sandboxes exec failed: total_duration_exhausted "
        f"(sandbox_id={sandbox_id})"
    )
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "exec"
    assert evidence.reason == "total_duration_exhausted"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is True
    assert evidence.subprocess_started is True
    assert evidence.trial_elapsed_s == 10.0
    assert evidence.trial_remaining_s == 0.0
    assert process_created.is_set()
    assert spawner.processes[-1].killed is True
    assert spawner.processes[-1].waited is True


def test_create_exhausted_deadline_keeps_sandbox_id_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=10,
    )
    provider._trial_deadline = 10.0
    host_head_calls = 0

    def exhaust_before_create(command: tuple[str, ...]) -> None:
        nonlocal host_head_calls
        if command[:1] == ("git",):
            host_head_calls += 1
            if host_head_calls == 2:
                clock.value = 10.0

    spawner.before_spawn = exhaust_before_create

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    cleanup_call = next(
        call for call in spawner.calls if call[:3] == ("sbx", "rm", "--force")
    )
    sandbox_id = cleanup_call[-1]
    assert (raised.value.operation, raised.value.reason) == (
        "create",
        "total_duration_exhausted",
    )
    assert raised.value.sandbox_id is None
    assert str(raised.value) == (
        "docker sandboxes create failed: total_duration_exhausted"
    )
    assert sandbox_id not in str(raised.value)
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "create"
    assert evidence.reason == "total_duration_exhausted"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is True
    assert evidence.subprocess_started is False
    assert evidence.trial_remaining_s == 0.0
    assert not any(
        call[:2] == ("sbx", "create") and call[-1] != "--help" for call in spawner.calls
    )


def test_create_in_flight_deadline_timeout_keeps_sandbox_id_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=10,
    )
    provider._trial_deadline = 10.0
    host_head_calls = 0
    process_created = asyncio.Event()

    def prepare_create_timeout(command: tuple[str, ...]) -> None:
        nonlocal host_head_calls
        if command[:1] == ("git",):
            host_head_calls += 1
            if host_head_calls == 2:
                clock.value = 9.0
        elif command[:2] == ("sbx", "create") and command[-1] != "--help":
            clock.value = 10.0
            process_created.set()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:2] == ("sbx", "create") and command[-1] != "--help":
            return _Outcome(hang=True)
        return _Outcome()

    def timeout(value: float) -> _RecordedTimeout | _TimeoutAfterProcessCreation:
        if value == 1.0:
            return _TimeoutAfterProcessCreation(process_created)
        return _RecordedTimeout()

    spawner.before_spawn = prepare_create_timeout
    spawner.handler = outcome
    monkeypatch.setattr(docker_sbx.asyncio, "timeout", timeout)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    create_process = spawner.processes[spawner.calls.index(create_call)]
    assert (raised.value.operation, raised.value.reason) == (
        "create",
        "total_duration_exhausted",
    )
    assert raised.value.sandbox_id is None
    assert str(raised.value) == (
        "docker sandboxes create failed: total_duration_exhausted"
    )
    assert sandbox_id not in str(raised.value)
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "create"
    assert evidence.reason == "total_duration_exhausted"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is True
    assert evidence.subprocess_started is True
    assert evidence.trial_remaining_s == 0.0
    assert process_created.is_set()
    assert create_process.killed is True
    assert create_process.waited is True


def test_create_allow_network_exhausted_deadline_preserves_public_sandbox_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=10,
    )
    guest_status = (
        "git",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    )

    def exhaust_before_allow_network(command: tuple[str, ...]) -> None:
        if command[:2] == ("sbx", "exec") and command[4:] == guest_status:
            clock.value = 10.0

    spawner.before_spawn = exhaust_before_allow_network

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    allow_network_call = (
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
        "total_duration_exhausted",
    )
    assert raised.value.sandbox_id == sandbox_id
    assert str(raised.value) == (
        "docker sandboxes allow_network failed: total_duration_exhausted "
        f"(sandbox_id={sandbox_id})"
    )
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "allow_network"
    assert evidence.reason == "total_duration_exhausted"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is True
    assert evidence.subprocess_started is False
    assert evidence.trial_remaining_s == 0.0
    assert allow_network_call not in spawner.calls


def test_create_allow_network_in_flight_deadline_preserves_public_sandbox_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(
        monkeypatch,
        spawner,
        command_timeout_s=30,
        total_duration_s=10,
    )
    guest_status = (
        "git",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    )
    process_created = asyncio.Event()

    def prepare_allow_network_timeout(command: tuple[str, ...]) -> None:
        if command[:2] == ("sbx", "exec") and command[4:] == guest_status:
            clock.value = 9.0
        elif command[:5] == ("sbx", "policy", "allow", "network", "--sandbox"):
            clock.value = 10.0
            process_created.set()

    def outcome(command: tuple[str, ...]) -> _Outcome:
        if command[:5] == ("sbx", "policy", "allow", "network", "--sandbox"):
            return _Outcome(hang=True)
        return _Outcome()

    def timeout(value: float) -> _RecordedTimeout | _TimeoutAfterProcessCreation:
        if value == 1.0:
            return _TimeoutAfterProcessCreation(process_created)
        return _RecordedTimeout()

    spawner.before_spawn = prepare_allow_network_timeout
    spawner.handler = outcome
    monkeypatch.setattr(docker_sbx.asyncio, "timeout", timeout)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    allow_network_call = next(
        call
        for call in spawner.calls
        if call[:5] == ("sbx", "policy", "allow", "network", "--sandbox")
    )
    sandbox_id = allow_network_call[-2]
    allow_network_process = spawner.processes[spawner.calls.index(allow_network_call)]
    assert (raised.value.operation, raised.value.reason) == (
        "allow_network",
        "total_duration_exhausted",
    )
    assert raised.value.sandbox_id == sandbox_id
    assert str(raised.value) == (
        "docker sandboxes allow_network failed: total_duration_exhausted "
        f"(sandbox_id={sandbox_id})"
    )
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "allow_network"
    assert evidence.reason == "total_duration_exhausted"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is True
    assert evidence.subprocess_started is True
    assert evidence.trial_remaining_s == 0.0
    assert process_created.is_set()
    assert allow_network_process.killed is True
    assert allow_network_process.waited is True


def test_nonzero_exit_records_execution_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    local_path = tmp_path / "result"
    command = ("sbx", "cp", f"{sandbox_id}:/result", str(local_path))
    spawner.overrides[command] = _Outcome(returncode=7, stderr=b"copy failed")

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/result", local_path))

    assert (raised.value.operation, raised.value.reason) == (
        "copy",
        "nonzero_exit",
    )
    assert str(raised.value) == (
        "docker sandboxes copy failed: nonzero_exit (returncode=7): copy failed"
    )
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "copy"
    assert evidence.reason == "nonzero_exit"
    assert evidence.returncode == 7
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is False
    assert evidence.subprocess_started is True
    assert evidence.trial_elapsed_s == 0.0
    assert evidence.trial_remaining_s == 300.0
    assert spawner.processes[-1].killed is False
    assert spawner.processes[-1].waited is True


def test_missing_executable_records_pre_spawn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    local_path = tmp_path / "result"
    command = ("sbx", "cp", f"{sandbox_id}:/result", str(local_path))
    missing = FileNotFoundError("sbx executable missing")
    spawner.overrides[command] = missing

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/result", local_path))

    assert (raised.value.operation, raised.value.reason) == (
        "copy",
        "executable_unavailable",
    )
    assert str(raised.value) == "docker sandboxes copy failed: executable_unavailable"
    assert raised.value.__cause__ is missing
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "copy"
    assert evidence.reason == "executable_unavailable"
    assert evidence.returncode is None
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is False
    assert evidence.subprocess_started is False
    assert evidence.trial_elapsed_s == 0.0
    assert evidence.trial_remaining_s == 300.0
    assert spawner.processes == []


def test_pre_spawn_oserror_records_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    local_path = tmp_path / "result"
    command = ("sbx", "cp", f"{sandbox_id}:/result", str(local_path))
    spawn_failure = OSError("spawn I/O failed")
    spawner.overrides[command] = spawn_failure

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/result", local_path))

    assert (raised.value.operation, raised.value.reason) == ("copy", "io_error")
    assert str(raised.value) == "docker sandboxes copy failed: io_error"
    assert raised.value.__cause__ is spawn_failure
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "copy"
    assert evidence.reason == "io_error"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is False
    assert evidence.subprocess_started is False
    assert evidence.trial_elapsed_s == 0.0
    assert evidence.trial_remaining_s == 300.0
    assert spawner.processes == []


def test_post_spawn_oserror_records_started_process_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    local_path = tmp_path / "result"
    command = ("sbx", "cp", f"{sandbox_id}:/result", str(local_path))
    spawner.overrides[command] = _Outcome(hang=True)

    async def fail_read(_: object) -> bytes:
        raise OSError("post-spawn I/O failed")

    monkeypatch.setattr(docker_sbx, "_read_bounded", fail_read)

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.copy(sandbox_id, "/result", local_path))

    assert (raised.value.operation, raised.value.reason) == ("copy", "io_error")
    assert str(raised.value) == "docker sandboxes copy failed: io_error"
    assert isinstance(raised.value.__cause__, OSError)
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "copy"
    assert evidence.reason == "io_error"
    assert evidence.sandbox_id == sandbox_id
    assert evidence.deadline_limited is False
    assert evidence.subprocess_started is True
    assert evidence.trial_elapsed_s == 0.0
    assert evidence.trial_remaining_s == 300.0
    assert spawner.processes[-1].killed is True
    assert spawner.processes[-1].waited is True


def test_process_cleanup_failure_preserves_original_provider_evidence_as_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    monkeypatch.setattr(docker_sbx, "REAP_TIMEOUT_SECONDS", 0.01)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    local_path = tmp_path / "result"
    command = ("sbx", "cp", f"{sandbox_id}:/result", str(local_path))
    spawner.overrides[command] = _Outcome(
        hang=True,
        kill_error=OSError("kill denied"),
        reap_hang=True,
    )
    process_created = asyncio.Event()
    spawner.before_spawn = lambda spawned_command: process_created.set()
    monkeypatch.setattr(
        docker_sbx.asyncio,
        "timeout",
        lambda _: _TimeoutAfterProcessCreation(process_created),
    )

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(
            provider._run(
                "copy",
                ["cp", f"{sandbox_id}:/result", str(local_path)],
                5.0,
                sandbox_id=sandbox_id,
            )
        )

    assert raised.value.reason == "process_cleanup_unconfirmed"
    assert str(raised.value) == (
        "docker sandboxes copy failed: process_cleanup_unconfirmed; "
        "cleanup: kill_failed: kill denied; reap_timeout"
    )
    cause = raised.value.__cause__
    assert isinstance(cause, DockerSbxError)
    assert (cause.operation, cause.reason) == ("copy", "timeout")
    assert str(cause) == "docker sandboxes copy failed: timeout"
    cause_evidence = get_sandbox_failure_evidence(cause)
    assert cause_evidence is not None
    assert cause_evidence.operation == "copy"
    assert cause_evidence.reason == "timeout"
    assert cause_evidence.sandbox_id == sandbox_id
    assert cause_evidence.deadline_limited is False
    assert cause_evidence.subprocess_started is True
    cleanup_evidence = get_sandbox_failure_evidence(raised.value)
    assert cleanup_evidence is not None
    assert cleanup_evidence.operation == "copy"
    assert cleanup_evidence.reason == "process_cleanup_unconfirmed"
    assert cleanup_evidence.sandbox_id == sandbox_id
    assert cleanup_evidence.deadline_limited is False
    assert cleanup_evidence.subprocess_started is True
    assert cleanup_evidence is not cause_evidence
    assert spawner.processes[-1].killed is True
    assert spawner.processes[-1].waited is False


def test_sandbox_cleanup_failure_has_cleanup_evidence_and_original_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider, sandbox_id = _active_provider(monkeypatch, spawner)
    publish_command = (
        "sbx",
        "ports",
        sandbox_id,
        "--publish",
        "8080/tcp4",
    )
    cleanup_command = ("sbx", "rm", "--force", sandbox_id)
    spawner.overrides[publish_command] = _Outcome(
        returncode=7,
        stderr=b"publish failed",
    )
    spawner.overrides[cleanup_command] = _Outcome(
        returncode=8,
        stderr=b"cleanup failed",
    )

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(provider.publish_port(sandbox_id, 8080))

    assert (raised.value.operation, raised.value.reason) == (
        "publish_port",
        "cleanup_unconfirmed",
    )
    assert str(raised.value) == (
        "docker sandboxes publish_port failed: cleanup_unconfirmed "
        f"(sandbox_id={sandbox_id}); cleanup: "
        "docker sandboxes cleanup failed: nonzero_exit "
        "(returncode=8): cleanup failed"
    )
    cause = raised.value.__cause__
    assert isinstance(cause, DockerSbxError)
    assert (cause.operation, cause.reason) == ("publish_port", "nonzero_exit")
    assert str(cause) == (
        "docker sandboxes publish_port failed: nonzero_exit "
        "(returncode=7): publish failed"
    )
    cause_evidence = get_sandbox_failure_evidence(cause)
    assert cause_evidence is not None
    assert cause_evidence.operation == "publish_port"
    assert cause_evidence.reason == "nonzero_exit"
    assert cause_evidence.returncode == 7
    assert cause_evidence.sandbox_id == sandbox_id
    assert cause_evidence.deadline_limited is False
    assert cause_evidence.subprocess_started is True
    cleanup_evidence = get_sandbox_failure_evidence(raised.value)
    assert cleanup_evidence is not None
    assert cleanup_evidence.operation == "publish_port"
    assert cleanup_evidence.reason == "cleanup_unconfirmed"
    assert cleanup_evidence.sandbox_id == sandbox_id
    assert cleanup_evidence.deadline_limited is False
    assert cleanup_evidence.subprocess_started is True
    assert cleanup_evidence is not cause_evidence
    assert spawner.processes[-2].waited is True
    assert spawner.processes[-1].waited is True


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


def test_dirty_guest_clone_retains_bounded_tracked_metadata_without_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox_id = "unused-until-create"
    spawner = _dirty_clone_spawner(
        status=b" M docker/script.sh\0",
        diff=b":100755 100644 <old-blob> <new-blob> M\0docker/script.sh\0",
        host_config={
            "core.autocrlf": b"false\n",
            "core.filemode": b"true\n",
            "core.symlinks": b"true\n",
        },
        guest_config={
            "core.autocrlf": b"false\n",
            "core.filemode": b"false\n",
            "core.symlinks": b"true\n",
        },
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.reason == "guest_status_not_clean"
    assert evidence.details["tracked_changes"] == [
        {
            "porcelain_status": " M",
            "diff_status": "M",
            "old_mode": "100755",
            "new_mode": "100644",
            "old_blob": "<old-blob>",
            "new_blob": "<new-blob>",
            "path": "docker/script.sh",
        }
    ]
    assert evidence.details["host_git_config"] == {
        "core.autocrlf": "false",
        "core.filemode": "true",
        "core.symlinks": "true",
    }
    assert evidence.details["guest_git_config"]["core.filemode"] == "false"
    assert evidence.details["truncated"] is False
    assert len(evidence.details["captured_bytes_sha256"]) == 64
    assert "file body secret" not in json.dumps(evidence.details)
    resolved_workspace = tmp_path.resolve(strict=True)
    assert all(
        command[0:3] == ("git", "-C", str(resolved_workspace))
        for command in spawner.calls
        if command[:1] == ("git",) and "config" in command
    )
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


@pytest.mark.parametrize(
    ("status", "expected_porcelain"),
    [
        (b"M  docker/script.sh\0", "M "),
        (b" M docker/script.sh\0", " M"),
    ],
)
def test_dirty_guest_clone_preserves_staged_and_unstaged_porcelain_status(
    status: bytes,
    expected_porcelain: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _dirty_clone_spawner(
        status=status,
        diff=b":100644 100644 <old> <new> M\0docker/script.sh\0",
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.details["tracked_changes"][0]["porcelain_status"] == (
        expected_porcelain
    )
    assert evidence.details["tracked_changes"][0]["diff_status"] == "M"


def test_dirty_guest_clone_retains_rename_gitlink_and_special_paths_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    special_path = "dir/\tline\nname.txt"
    status = (
        b"R  new-name.txt\0old-name.txt\0"
        + b" M submodule\0"
        + b" M "
        + special_path.encode()
        + b"\0"
    )
    diff = (
        b":100644 100644 <old-rename> <new-rename> R100\0"
        b"old-name.txt\0new-name.txt\0"
        b":160000 160000 <old-gitlink> <new-gitlink> M\0submodule\0"
        b":100644 100644 <old-special> <new-special> M\0"
        + special_path.encode()
        + b"\0"
    )
    spawner = _dirty_clone_spawner(status=status, diff=diff)
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    records = evidence.details["tracked_changes"]
    assert records[0]["porcelain_status"] == "R  "[:2]
    assert records[0]["diff_status"] == "R100"
    assert {records[0]["path"], records[0]["path2"]} == {
        "old-name.txt",
        "new-name.txt",
    }
    assert records[1]["old_mode"] == "160000"
    assert records[1]["new_mode"] == "160000"
    assert records[1]["path"] == "submodule"
    assert records[2]["path"] == special_path
    assert records[2].get("path2", True)


def test_dirty_guest_clone_matches_non_utf8_paths_by_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    non_utf8_path = b"safe/\xff"
    escaped_path = b"safe/\\xff"
    status = b"M  " + non_utf8_path + b"\0 M " + escaped_path + b"\0"
    diff = (
        b":100755 100644 <escaped-old> <escaped-new> M\0" + escaped_path + b"\0"
        b":100644 100755 <non-utf8-old> <non-utf8-new> M\0" + non_utf8_path + b"\0"
    )
    spawner = _dirty_clone_spawner(status=status, diff=diff)
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    records = evidence.details["tracked_changes"]
    assert records == [
        {
            "porcelain_status": "M ",
            "diff_status": "M",
            "old_mode": "100644",
            "new_mode": "100755",
            "old_blob": "<non-utf8-old>",
            "new_blob": "<non-utf8-new>",
            "path": r"safe/\xff",
        },
        {
            "porcelain_status": " M",
            "diff_status": "M",
            "old_mode": "100755",
            "new_mode": "100644",
            "old_blob": "<escaped-old>",
            "new_blob": "<escaped-new>",
            "path": r"safe/\xff",
        },
    ]


def test_dirty_guest_clone_diagnostics_truncate_by_entry_and_byte_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_parts: list[bytes] = []
    diff_parts: list[bytes] = []
    for index in range(40):
        path = f"tracked/{index:02d}/" + ("x" * 300) + ".txt"
        path_bytes = path.encode()
        status_parts.append(b" M " + path_bytes + b"\0")
        diff_parts.append(b":100644 100644 <old> <new> M\0" + path_bytes + b"\0")
    spawner = _dirty_clone_spawner(
        status=b"".join(status_parts),
        diff=b"".join(diff_parts),
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.details["truncated"] is True
    assert 0 < len(evidence.details["tracked_changes"]) < 32
    serialized = serialize_sandbox_failure_evidence(evidence)
    assert len(
        json.dumps(serialized, separators=(",", ":"), sort_keys=True).encode()
    ) <= (16_384)


def test_dirty_guest_clone_rejects_malformed_porcelain_without_relaxing_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _dirty_clone_spawner(
        status=b" M tracked.txt\0unterminated",
        diff=b":100644 100644 <old> <new> M\0tracked.txt\0",
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.reason == "guest_status_not_clean"
    assert evidence.details["diagnostic_status"] == "malformed"
    assert "unterminated" not in json.dumps(evidence.details)
    assert spawner.calls[-1] == ("sbx", "rm", "--force", sandbox_id)


@pytest.mark.parametrize(
    "unsafe_path", [b"../outside.txt", b"/absolute.txt", b"C:/host.txt"]
)
def test_dirty_guest_clone_rejects_unsafe_paths_fail_closed(
    unsafe_path: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _dirty_clone_spawner(
        status=b" M " + unsafe_path + b"\0",
        diff=b":100644 100644 <old> <new> M\0" + unsafe_path + b"\0",
    )
    provider = _provider(monkeypatch, spawner)

    with pytest.raises(DockerSbxError) as raised:
        _create(provider, tmp_path)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.reason == "guest_status_not_clean"
    assert evidence.details["diagnostic_status"] == "malformed"
    assert unsafe_path.decode() not in json.dumps(evidence.details)
    assert spawner.calls[-1][0:3] == ("sbx", "rm", "--force")


def test_clean_guest_clone_does_not_run_diagnostic_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner)

    _create(provider, tmp_path)

    create_call = _actual_create_call(spawner)
    sandbox_id = create_call[create_call.index("--name") + 1]
    guest_commands = [
        call[4:]
        for call in spawner.calls
        if call[:4] == ("sbx", "exec", sandbox_id, "--")
    ]
    assert guest_commands == [
        ("pwd",),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "rev-parse", "--is-inside-work-tree"),
        ("git", "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
        (
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ),
    ]


def _dirty_clone_spawner(
    *,
    status: bytes,
    diff: bytes,
    host_config: dict[str, bytes] | None = None,
    guest_config: dict[str, bytes] | None = None,
) -> _SbxSpawner:
    host_values = host_config or {
        "core.autocrlf": b"unset\n",
        "core.filemode": b"unset\n",
        "core.symlinks": b"unset\n",
    }
    guest_values = guest_config or {
        "core.autocrlf": b"unset\n",
        "core.filemode": b"unset\n",
        "core.symlinks": b"unset\n",
    }
    spawner = _SbxSpawner()
    guest_status = (
        "git",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    )
    guest_diff = ("git", "diff", "--raw", "-z", "--no-ext-diff", "HEAD", "--")

    def respond(command: tuple[str, ...]) -> _Outcome:
        if command[:1] == ("git",):
            if "config" in command:
                return _Outcome(stdout=host_values.get(command[-1], b"unset\n"))
            return _Outcome(stdout=HOST_HEAD)
        if command == ("sbx", "version"):
            return _Outcome(stdout=b"sbx 99.0.0\n")
        if command in HELP_OUTPUTS:
            return _Outcome(stdout=HELP_OUTPUTS[command].encode())
        if command[:2] == ("sbx", "exec"):
            argv = command[4:]
            if argv == guest_status:
                return _Outcome(stdout=status)
            if argv == guest_diff:
                return _Outcome(stdout=diff)
            if argv[:3] == ("git", "config", "--default"):
                return _Outcome(stdout=guest_values.get(argv[-1], b"unset\n"))
            return _guest_verification_outcome(command)
        return _Outcome()

    spawner.intercept = respond
    return spawner
