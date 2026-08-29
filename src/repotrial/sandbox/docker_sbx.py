"""Fail-closed Docker Sandboxes provider."""

import asyncio
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

from .base import (
    ExecResult,
    NetworkLogResult,
    SandboxProvider,
    attach_partial_create_cleanup_context,
)

MAX_OUTPUT_BYTES = 65_536
MAX_NETWORK_EVENTS = 100
REAP_TIMEOUT_SECONDS = 5
_TRUNCATION_MARKER = b"\n...[truncated]"
# Calibrated against Docker Sandboxes v0.39.0 on Windows. Root and Docker
# filesystems work at the smallest positive integer MiB policy value. The
# cloned-workspace floor is the smallest value that clones this trusted fixture
# without warnings and passes git fsck with positive free capacity.
ROOT_FLOOR_MB = 1
DOCKER_FLOOR_MB = 1
WORKSPACE_FLOOR_MB = 5
_DISK_SIZE_ENVIRONMENT_VARIABLES = (
    "DOCKER_SANDBOXES_ROOT_SIZE",
    "DOCKER_SANDBOXES_DOCKER_SIZE",
    "DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE",
)
_CREATE_FLAGS = (
    "--name",
    "--clone",
    "--cpus",
    "--memory",
    "--deny-network",
)
PID_HARD_BOUND_LIMITATION = "pid_hard_bound_unsupported"
_PORT_KEYS = {"host_ip", "host_port", "sandbox_port", "protocol"}
_NETWORK_EVENT_KEYS = {
    "sandbox",
    "decision",
    "host",
    "proxy",
    "rule",
    "reason",
    "last_seen",
    "count",
}
_NETWORK_PROXIES = {
    "forward",
    "forward-bypass",
    "transparent",
    "network",
    "browser-open",
}

MANDATORY_DENY_NETWORK = frozenset(
    {
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
)


class DockerSbxUnsupportedError(RuntimeError):
    """The installed sbx backend cannot prove a required safety boundary."""

    def __init__(self, reason: str, *, stderr: str = "") -> None:
        self.reason = reason
        self.stderr = stderr
        message = f"docker sandboxes unsupported: {reason}"
        if stderr:
            message = f"{message}: {stderr}"
        super().__init__(message)


class DockerSbxError(RuntimeError):
    """A bounded sbx operation failed after provider compatibility was proven."""

    def __init__(
        self,
        operation: str,
        reason: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
        sandbox_id: str | None = None,
        cleanup_error: str = "",
    ) -> None:
        self.operation = operation
        self.reason = reason
        self.returncode = returncode
        self.stderr = stderr
        self.sandbox_id = sandbox_id
        self.cleanup_error = cleanup_error
        message = f"docker sandboxes {operation} failed: {reason}"
        if returncode is not None:
            message = f"{message} (returncode={returncode})"
        if stderr:
            message = f"{message}: {stderr}"
        if sandbox_id is not None:
            message = f"{message} (sandbox_id={sandbox_id})"
        if cleanup_error:
            message = f"{message}; cleanup: {cleanup_error}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DiskAllocation:
    root_mb: int
    docker_mb: int
    workspace_mb: int


def calculate_disk_allocation(disk_mb: int) -> DiskAllocation:
    """Allocate the policy disk budget across Docker Sandboxes filesystems."""
    if isinstance(disk_mb, bool) or not isinstance(disk_mb, int) or disk_mb <= 0:
        raise ValueError("disk_mb must be a positive integer")
    docker_mb = disk_mb - ROOT_FLOOR_MB - WORKSPACE_FLOOR_MB
    if docker_mb < DOCKER_FLOOR_MB:
        raise DockerSbxUnsupportedError("disk_budget_insufficient")
    return DiskAllocation(
        root_mb=ROOT_FLOOR_MB,
        docker_mb=docker_mb,
        workspace_mb=WORKSPACE_FLOOR_MB,
    )


@dataclass(frozen=True, slots=True)
class DockerSbxPolicy:
    """Immutable resource and network limits applied to every new sandbox."""

    cpus: int
    memory_mb: int
    pids_limit: int
    disk_mb: int
    total_duration_s: int
    deny_network: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            isinstance(self.cpus, bool)
            or not isinstance(self.cpus, int)
            or self.cpus <= 0
        ):
            raise ValueError("cpus must be a positive integer")
        for field_name in (
            "memory_mb",
            "pids_limit",
            "disk_mb",
            "total_duration_s",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        resources = frozenset(self.deny_network)
        if any(
            not isinstance(resource, str)
            or not resource
            or any(character.isspace() for character in resource)
            for resource in resources
        ):
            raise ValueError("deny_network resources must be non-empty single tokens")
        object.__setattr__(self, "deny_network", resources | MANDATORY_DENY_NETWORK)


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _SandboxState(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    CLEANUP_UNSAFE = "cleanup-unsafe"
    CLEANED = "cleaned"


class _ProcessCleanupError(RuntimeError):
    pass


class DockerSbxProvider(SandboxProvider):
    """Run Docker Sandboxes only after an exact capability probe succeeds."""

    def __init__(
        self, policy: DockerSbxPolicy, *, command_timeout_s: float = 30
    ) -> None:
        if (
            isinstance(command_timeout_s, bool)
            or not isinstance(command_timeout_s, (int, float))
            or not math.isfinite(command_timeout_s)
            or command_timeout_s <= 0
        ):
            raise ValueError("command_timeout_s must be positive and finite")
        self._policy = policy
        self._command_timeout_s = float(command_timeout_s)
        self._subprocess_environment = _sanitized_environment()
        self._sandbox_states: dict[str, _SandboxState] = {}
        self._sandbox_deadlines: dict[str, float] = {}
        self._network_log_sandboxes: set[str] = set()

    async def create(self, workspace: Path, name: str) -> str:
        deadline = time.monotonic() + self._policy.total_duration_s
        allocation = calculate_disk_allocation(self._policy.disk_mb)
        network_log_supported = await self._probe(deadline)
        sandbox_id = _new_sandbox_id(name)
        self._sandbox_states[sandbox_id] = _SandboxState.PENDING
        arguments = [
            "create",
            "--name",
            sandbox_id,
            "--clone",
            "--cpus",
            str(self._policy.cpus),
            "--memory",
            f"{self._policy.memory_mb}m",
        ]
        for resource in sorted(self._policy.deny_network):
            arguments.extend(("--deny-network", resource))
        arguments.extend(("shell", str(workspace)))
        try:
            result = await self._run(
                "create",
                arguments,
                self._command_timeout_s,
                env=self._disk_environment(allocation),
                deadline=deadline,
            )
            _require_success("create", result)
        except (DockerSbxError, asyncio.CancelledError) as error:
            await self._cleanup_after_uncertain_failure(
                sandbox_id,
                error,
                partial_create=True,
            )
        self._sandbox_states[sandbox_id] = _SandboxState.ACTIVE
        self._sandbox_deadlines[sandbox_id] = deadline
        if network_log_supported:
            self._network_log_sandboxes.add(sandbox_id)
        return sandbox_id

    def _disk_environment(self, allocation: DiskAllocation) -> dict[str, str]:
        environment = self._subprocess_environment.copy()
        environment.update(
            {
                "DOCKER_SANDBOXES_ROOT_SIZE": f"{allocation.root_mb}m",
                "DOCKER_SANDBOXES_DOCKER_SIZE": f"{allocation.docker_mb}m",
                "DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE": (
                    f"{allocation.workspace_mb}m"
                ),
            }
        )
        return environment

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        if not isinstance(argv, list) or not all(
            isinstance(argument, str) for argument in argv
        ):
            raise TypeError("argv must be a list of strings")
        if not argv:
            raise ValueError("argv must not be empty")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive integer")
        self._require_active(sandbox_id)
        result = await self._run(
            "exec",
            ["exec", sandbox_id, "--", *argv],
            float(timeout_s),
            deadline=self._require_deadline(sandbox_id),
            sandbox_id=sandbox_id,
        )
        return ExecResult(
            exit_code=result.returncode,
            stdout=_decode_human_output(result.stdout),
            stderr=_decode_human_output(result.stderr),
        )

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        self._require_active(sandbox_id)
        if (
            isinstance(container_port, bool)
            or not isinstance(container_port, int)
            or not 1 <= container_port <= 65_535
        ):
            raise ValueError("container_port must be between 1 and 65535")
        try:
            published = await self._run(
                "publish_port",
                ["ports", sandbox_id, "--publish", f"{container_port}/tcp4"],
                self._command_timeout_s,
                deadline=self._require_deadline(sandbox_id),
                sandbox_id=sandbox_id,
            )
            _require_success("publish_port", published)
            listed = await self._run(
                "publish_port",
                ["ports", sandbox_id, "--json"],
                self._command_timeout_s,
                deadline=self._require_deadline(sandbox_id),
                sandbox_id=sandbox_id,
            )
            _require_success("publish_port", listed)
            return _parse_published_port(listed.stdout, container_port)
        except (DockerSbxError, asyncio.CancelledError) as error:
            await self._cleanup_after_uncertain_failure(sandbox_id, error)

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        self._require_active(sandbox_id)
        if not isinstance(remote_path, str) or not remote_path:
            raise ValueError("remote_path must be a non-empty string")
        result = await self._run(
            "copy",
            ["cp", f"{sandbox_id}:{remote_path}", str(local_path)],
            self._command_timeout_s,
            deadline=self._require_deadline(sandbox_id),
            sandbox_id=sandbox_id,
        )
        _require_success("copy", result)

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        self._require_active(sandbox_id)
        if sandbox_id not in self._network_log_sandboxes:
            return _unsupported_network_log("network_log_capability_unavailable")
        try:
            result = await self._run(
                "network_log",
                [
                    "policy",
                    "log",
                    sandbox_id,
                    "--type",
                    "network",
                    "--json",
                ],
                self._command_timeout_s,
                deadline=self._require_deadline(sandbox_id),
                sandbox_id=sandbox_id,
            )
        except DockerSbxError as error:
            if error.reason in {
                "process_cleanup_unconfirmed",
                "total_duration_exhausted",
            }:
                raise
            return _unsupported_network_log("network_log_command_failed")
        if result.returncode != 0:
            return _unsupported_network_log("network_log_command_failed")
        events = _parse_network_events(result.stdout, sandbox_id)
        if events is None:
            return _unsupported_network_log("network_log_invalid_json_contract")
        return NetworkLogResult(events=events, supported=True)

    async def destroy(self, sandbox_id: str) -> None:
        state = self._sandbox_states.get(sandbox_id)
        if state is _SandboxState.CLEANED:
            return
        if state not in {_SandboxState.ACTIVE, _SandboxState.CLEANUP_UNSAFE}:
            raise RuntimeError(f"sandbox is not active: {sandbox_id}")
        try:
            await self._force_destroy(sandbox_id, operation="destroy")
        except (DockerSbxError, asyncio.CancelledError):
            self._sandbox_states[sandbox_id] = _SandboxState.CLEANUP_UNSAFE
            raise

    async def _force_destroy(self, sandbox_id: str, *, operation: str) -> None:
        result = await self._run(
            operation,
            ["rm", "--force", sandbox_id],
            self._command_timeout_s,
        )
        _require_success(operation, result)
        self._sandbox_states[sandbox_id] = _SandboxState.CLEANED
        self._sandbox_deadlines.pop(sandbox_id, None)
        self._network_log_sandboxes.discard(sandbox_id)

    async def _cleanup_after_uncertain_failure(
        self,
        sandbox_id: str,
        primary_error: DockerSbxError | asyncio.CancelledError,
        *,
        partial_create: bool = False,
    ) -> NoReturn:
        cleanup_task = asyncio.create_task(
            self._force_destroy(sandbox_id, operation="cleanup")
        )
        cancellation = (
            primary_error if isinstance(primary_error, asyncio.CancelledError) else None
        )
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except DockerSbxError:
                pass

        cleanup_error = cleanup_task.exception()
        if cleanup_error is not None:
            self._sandbox_states[sandbox_id] = _SandboxState.CLEANUP_UNSAFE
            detail = str(cleanup_error)
            if partial_create:
                attach_partial_create_cleanup_context(
                    primary_error,
                    sandbox_id,
                    cleanup_error,
                )
            if cancellation is not None:
                cancellation.add_note(
                    f"sandbox cleanup unconfirmed: {sandbox_id}: {detail}"
                )
                raise cancellation
            if partial_create:
                raise primary_error
            assert isinstance(primary_error, DockerSbxError)
            raise DockerSbxError(
                primary_error.operation,
                "cleanup_unconfirmed",
                sandbox_id=sandbox_id,
                cleanup_error=detail,
            ) from primary_error
        if cancellation is not None:
            raise cancellation
        raise primary_error

    async def _probe(self, deadline: float) -> bool:
        version = await self._probe_call("version", ["version"], deadline)
        if version.returncode != 0:
            raise DockerSbxUnsupportedError(
                "version_probe_failed", stderr=_decode_human_output(version.stderr)
            )

        required_help = (
            ("create", ["create", "--help"], _CREATE_FLAGS),
            ("create_shell", ["create", "shell", "--help"], ("PATH",)),
            ("exec", ["exec", "--help"], ()),
            ("ports", ["ports", "--help"], ("--publish", "--json")),
            ("cp", ["cp", "--help"], ()),
            ("rm", ["rm", "--help"], ("--force",)),
        )
        for capability, arguments, tokens in required_help:
            result = await self._probe_call(capability, arguments, deadline)
            if result.returncode != 0:
                raise DockerSbxUnsupportedError(
                    f"{capability}_probe_failed",
                    stderr=_decode_human_output(result.stderr),
                )
            for token in tokens:
                if not _has_token(result.stdout, token):
                    raise DockerSbxUnsupportedError(
                        f"{capability}_missing_capability:{token}"
                    )

        try:
            log_help = await self._run(
                "probe_network_log",
                ["policy", "log", "--help"],
                self._command_timeout_s,
                deadline=deadline,
            )
        except DockerSbxError as error:
            if error.reason in {
                "process_cleanup_unconfirmed",
                "total_duration_exhausted",
            }:
                raise
            return False
        return log_help.returncode == 0 and all(
            _has_token(log_help.stdout, token) for token in ("--type", "--json")
        )

    async def _probe_call(
        self, capability: str, arguments: list[str], deadline: float
    ) -> _CommandResult:
        try:
            return await self._run(
                f"probe_{capability}",
                arguments,
                self._command_timeout_s,
                deadline=deadline,
            )
        except DockerSbxError as error:
            if error.reason in {
                "process_cleanup_unconfirmed",
                "total_duration_exhausted",
            }:
                raise
            if capability == "version" and error.reason == "executable_unavailable":
                raise DockerSbxUnsupportedError(
                    "sbx_unavailable", stderr=error.stderr
                ) from error
            raise DockerSbxUnsupportedError(
                f"{capability}_probe_failed", stderr=error.stderr
            ) from error

    async def _run(
        self,
        operation: str,
        arguments: list[str],
        timeout_s: float,
        *,
        env: dict[str, str] | None = None,
        deadline: float | None = None,
        sandbox_id: str | None = None,
    ) -> _CommandResult:
        process: asyncio.subprocess.Process | None = None
        try:
            effective_timeout_s, deadline_limited = self._effective_timeout(
                operation,
                timeout_s,
                deadline=deadline,
                sandbox_id=sandbox_id,
            )
            async with asyncio.timeout(effective_timeout_s):
                spawn_kwargs: dict[str, Any] = {
                    "stdin": asyncio.subprocess.DEVNULL,
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "env": env if env is not None else self._subprocess_environment,
                }
                process = await asyncio.create_subprocess_exec(
                    "sbx",
                    *arguments,
                    **spawn_kwargs,
                )
                if process.stdout is None or process.stderr is None:
                    raise OSError("sbx pipes unavailable")
                stdout_bytes, stderr_bytes, returncode = await asyncio.gather(
                    _read_bounded(process.stdout),
                    _read_bounded(process.stderr),
                    process.wait(),
                )
        except TimeoutError as error:
            timeout_reason = (
                "total_duration_exhausted" if deadline_limited else "timeout"
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    timeout_reason,
                    process,
                    error,
                    sandbox_id=sandbox_id if deadline_limited else None,
                )
            raise DockerSbxError(
                operation,
                timeout_reason,
                sandbox_id=sandbox_id if deadline_limited else None,
            ) from error
        except asyncio.CancelledError as error:
            if process is not None:
                await _raise_after_process_cleanup(
                    operation, "cancelled", process, error
                )
            raise
        except FileNotFoundError as error:
            raise DockerSbxError(operation, "executable_unavailable") from error
        except OSError as error:
            if process is not None:
                await _raise_after_process_cleanup(
                    operation, "io_error", process, error
                )
            raise DockerSbxError(operation, "io_error") from error

        return _CommandResult(
            returncode=returncode, stdout=stdout_bytes, stderr=stderr_bytes
        )

    def _require_active(self, sandbox_id: str) -> None:
        if self._sandbox_states.get(sandbox_id) is not _SandboxState.ACTIVE:
            raise RuntimeError(f"sandbox is not active: {sandbox_id}")

    def _require_deadline(self, sandbox_id: str) -> float:
        return self._sandbox_deadlines[sandbox_id]

    def _effective_timeout(
        self,
        operation: str,
        timeout_s: float,
        *,
        deadline: float | None,
        sandbox_id: str | None,
    ) -> tuple[float, bool]:
        if deadline is None:
            return timeout_s, False
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise DockerSbxError(
                operation,
                "total_duration_exhausted",
                sandbox_id=sandbox_id,
            )
        if remaining_s <= timeout_s:
            return remaining_s, True
        return timeout_s, False


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    retained = bytearray()
    truncated = False
    payload_limit = MAX_OUTPUT_BYTES - len(_TRUNCATION_MARKER)
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = payload_limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    if truncated:
        retained.extend(_TRUNCATION_MARKER)
    return bytes(retained)


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    kill_error = ""
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError) as error:
            kill_error = f"kill_failed: {error}"
    try:
        await asyncio.wait_for(process.wait(), timeout=REAP_TIMEOUT_SECONDS)
    except TimeoutError as error:
        details = "; ".join(filter(None, (kill_error, "reap_timeout")))
        raise _ProcessCleanupError(details) from error
    except OSError as error:
        details = "; ".join(filter(None, (kill_error, f"reap_failed: {error}")))
        raise _ProcessCleanupError(details) from error


async def _raise_after_process_cleanup(
    operation: str,
    reason: str,
    process: asyncio.subprocess.Process,
    primary_error: BaseException,
    *,
    sandbox_id: str | None = None,
) -> NoReturn:
    cleanup_task = asyncio.create_task(_kill_and_reap(process))
    cancellation = (
        primary_error if isinstance(primary_error, asyncio.CancelledError) else None
    )
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except _ProcessCleanupError:
            pass

    cleanup_error = cleanup_task.exception()
    if cancellation is not None:
        if not isinstance(primary_error, asyncio.CancelledError):
            cancellation.add_note(
                f"sbx {operation} was cancelled while handling {reason}"
            )
        if cleanup_error is not None:
            cancellation.add_note(f"process cleanup unconfirmed: {cleanup_error}")
        raise cancellation
    if cleanup_error is not None:
        raise DockerSbxError(
            operation,
            "process_cleanup_unconfirmed",
            cleanup_error=str(cleanup_error),
        ) from primary_error
    raise DockerSbxError(operation, reason, sandbox_id=sandbox_id) from primary_error


def _decode_human_output(output: bytes) -> str:
    decoded = output.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return decoded
    prefix = encoded[: MAX_OUTPUT_BYTES - len(_TRUNCATION_MARKER)]
    while True:
        try:
            return prefix.decode("utf-8") + _TRUNCATION_MARKER.decode("ascii")
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _require_success(operation: str, result: _CommandResult) -> None:
    if result.returncode != 0:
        raise DockerSbxError(
            operation,
            "nonzero_exit",
            returncode=result.returncode,
            stderr=_decode_human_output(result.stderr),
        )


def _has_token(output: bytes, token: str) -> bool:
    try:
        decoded = output.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", decoded) is not None


def _sanitized_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in _DISK_SIZE_ENVIRONMENT_VARIABLES:
        environment.pop(variable, None)
    return environment


def _new_sandbox_id(name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9.-]+", "-", str(name)).strip(".-")[:24]
    if not safe_name:
        safe_name = "trial"
    return f"repotrial-{safe_name}-{uuid.uuid4().hex[:12]}"


def _parse_published_port(output: bytes, container_port: int) -> int:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise DockerSbxError("publish_port", "port_mapping_invalid_utf8") from None
    try:
        decoded: Any = json.loads(text)
    except json.JSONDecodeError:
        raise DockerSbxError("publish_port", "port_mapping_invalid") from None
    if not isinstance(decoded, list) or len(decoded) > 128:
        raise DockerSbxError("publish_port", "port_mapping_invalid")
    matches: list[int] = []
    for item in decoded:
        if not isinstance(item, dict) or set(item) != _PORT_KEYS:
            raise DockerSbxError("publish_port", "port_mapping_invalid")
        host_ip = item["host_ip"]
        host_port = item["host_port"]
        sandbox_port = item["sandbox_port"]
        protocol = item["protocol"]
        if (
            host_ip != "127.0.0.1"
            or type(host_port) is not int
            or not 1 <= host_port <= 65_535
            or type(sandbox_port) is not int
            or not 1 <= sandbox_port <= 65_535
            or protocol != "tcp4"
        ):
            raise DockerSbxError("publish_port", "port_mapping_invalid")
        if sandbox_port == container_port:
            matches.append(host_port)
    if len(matches) != 1:
        raise DockerSbxError("publish_port", "port_mapping_missing_or_ambiguous")
    return matches[0]


def _parse_network_events(
    output: bytes, sandbox_id: str
) -> list[dict[str, Any]] | None:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        decoded: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or len(decoded) > MAX_NETWORK_EVENTS:
        return None
    events: list[dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict) or set(item) != _NETWORK_EVENT_KEYS:
            return None
        string_fields = (
            "sandbox",
            "decision",
            "host",
            "proxy",
            "rule",
            "reason",
            "last_seen",
        )
        if any(
            not isinstance(item[field], str) or len(item[field]) > 1024
            for field in string_fields
        ):
            return None
        if (
            item["sandbox"] != sandbox_id
            or item["decision"] not in {"allowed", "blocked"}
            or not item["host"]
            or item["proxy"] not in _NETWORK_PROXIES
            or type(item["count"]) is not int
            or item["count"] < 1
        ):
            return None
        events.append(dict(item))
    return events


def _unsupported_network_log(reason: str) -> NetworkLogResult:
    return NetworkLogResult(events=[], supported=False, unsupported_reason=reason)
