"""Fail-closed Docker Sandboxes provider."""

import asyncio
import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ExecResult, NetworkLogResult, SandboxProvider

MAX_OUTPUT_BYTES = 65_536
MAX_NETWORK_EVENTS = 100
REAP_TIMEOUT_SECONDS = 5
_TRUNCATION_MARKER = b"\n...[truncated]"
_CREATE_FLAGS = (
    "--name",
    "--cpus",
    "--memory",
    "--deny-network",
    "--pids-limit",
    "--disk-limit",
    "--total-duration",
)
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
    ) -> None:
        self.operation = operation
        self.reason = reason
        self.returncode = returncode
        self.stderr = stderr
        message = f"docker sandboxes {operation} failed: {reason}"
        if returncode is not None:
            message = f"{message} (returncode={returncode})"
        if stderr:
            message = f"{message}: {stderr}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DockerSbxPolicy:
    """Immutable resource and network limits applied to every new sandbox."""

    cpus: float
    memory_mb: int
    pids_limit: int
    disk_mb: int
    total_duration_s: int
    deny_network: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            isinstance(self.cpus, bool)
            or not isinstance(self.cpus, (int, float))
            or not math.isfinite(self.cpus)
            or self.cpus <= 0
        ):
            raise ValueError("cpus must be positive and finite")
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
    stdout: str
    stderr: str


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
        self._active_sandboxes: set[str] = set()
        self._network_log_sandboxes: set[str] = set()

    async def create(self, workspace: Path, name: str) -> str:
        network_log_supported = await self._probe()
        sandbox_id = _new_sandbox_id(name)
        arguments = [
            "create",
            "--name",
            sandbox_id,
            "--cpus",
            _format_cpus(self._policy.cpus),
            "--memory",
            f"{self._policy.memory_mb}m",
            "--pids-limit",
            str(self._policy.pids_limit),
            "--disk-limit",
            f"{self._policy.disk_mb}m",
            "--total-duration",
            f"{self._policy.total_duration_s}s",
        ]
        for resource in sorted(self._policy.deny_network):
            arguments.extend(("--deny-network", resource))
        arguments.extend(("shell", str(workspace)))
        result = await self._run("create", arguments, self._command_timeout_s)
        _require_success("create", result)
        self._active_sandboxes.add(sandbox_id)
        if network_log_supported:
            self._network_log_sandboxes.add(sandbox_id)
        return sandbox_id

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
            "exec", ["exec", sandbox_id, "--", *argv], float(timeout_s)
        )
        return ExecResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        self._require_active(sandbox_id)
        if (
            isinstance(container_port, bool)
            or not isinstance(container_port, int)
            or not 1 <= container_port <= 65_535
        ):
            raise ValueError("container_port must be between 1 and 65535")
        published = await self._run(
            "publish_port",
            ["ports", sandbox_id, "--publish", f"{container_port}/tcp4"],
            self._command_timeout_s,
        )
        _require_success("publish_port", published)
        listed = await self._run(
            "publish_port",
            ["ports", sandbox_id, "--json"],
            self._command_timeout_s,
        )
        _require_success("publish_port", listed)
        return _parse_published_port(listed.stdout, container_port)

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        self._require_active(sandbox_id)
        if not isinstance(remote_path, str) or not remote_path:
            raise ValueError("remote_path must be a non-empty string")
        result = await self._run(
            "copy",
            ["cp", f"{sandbox_id}:{remote_path}", str(local_path)],
            self._command_timeout_s,
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
            )
        except DockerSbxError:
            return _unsupported_network_log("network_log_command_failed")
        if result.returncode != 0:
            return _unsupported_network_log("network_log_command_failed")
        events = _parse_network_events(result.stdout, sandbox_id)
        if events is None:
            return _unsupported_network_log("network_log_invalid_json_contract")
        return NetworkLogResult(events=events, supported=True)

    async def destroy(self, sandbox_id: str) -> None:
        self._require_active(sandbox_id)
        result = await self._run(
            "destroy",
            ["rm", "--force", sandbox_id],
            self._command_timeout_s,
        )
        _require_success("destroy", result)
        self._active_sandboxes.remove(sandbox_id)
        self._network_log_sandboxes.discard(sandbox_id)

    async def _probe(self) -> bool:
        version = await self._probe_call("version", ["version"])
        if version.returncode != 0:
            raise DockerSbxUnsupportedError(
                "version_probe_failed", stderr=version.stderr
            )

        required_help = (
            ("create", ["create", "--help"], _CREATE_FLAGS),
            ("exec", ["exec", "--help"], ("--",)),
            ("ports", ["ports", "--help"], ("--publish", "--json")),
            ("cp", ["cp", "--help"], ()),
            ("rm", ["rm", "--help"], ("--force",)),
        )
        for capability, arguments, tokens in required_help:
            result = await self._probe_call(capability, arguments)
            if result.returncode != 0:
                raise DockerSbxUnsupportedError(
                    f"{capability}_probe_failed", stderr=result.stderr
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
            )
        except DockerSbxError:
            return False
        return log_help.returncode == 0 and all(
            _has_token(log_help.stdout, token) for token in ("--type", "--json")
        )

    async def _probe_call(
        self, capability: str, arguments: list[str]
    ) -> _CommandResult:
        try:
            return await self._run(
                f"probe_{capability}", arguments, self._command_timeout_s
            )
        except DockerSbxError as error:
            if capability == "version" and error.reason == "executable_unavailable":
                raise DockerSbxUnsupportedError(
                    "sbx_unavailable", stderr=error.stderr
                ) from error
            raise DockerSbxUnsupportedError(
                f"{capability}_probe_failed", stderr=error.stderr
            ) from error

    async def _run(
        self, operation: str, arguments: list[str], timeout_s: float
    ) -> _CommandResult:
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(timeout_s):
                process = await asyncio.create_subprocess_exec(
                    "sbx",
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if process.stdout is None or process.stderr is None:
                    raise OSError("sbx pipes unavailable")
                stdout_bytes, stderr_bytes, returncode = await asyncio.gather(
                    _read_bounded(process.stdout),
                    _read_bounded(process.stderr),
                    process.wait(),
                )
        except TimeoutError as error:
            if process is not None:
                await _kill_and_reap(process)
            raise DockerSbxError(operation, "timeout") from error
        except asyncio.CancelledError:
            if process is not None:
                await _kill_and_reap(process)
            raise
        except FileNotFoundError as error:
            raise DockerSbxError(operation, "executable_unavailable") from error
        except OSError as error:
            if process is not None:
                await _kill_and_reap(process)
            raise DockerSbxError(operation, "io_error") from error

        return _CommandResult(
            returncode=returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    def _require_active(self, sandbox_id: str) -> None:
        if sandbox_id not in self._active_sandboxes:
            raise RuntimeError(f"sandbox is not active: {sandbox_id}")


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
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=REAP_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        pass


def _require_success(operation: str, result: _CommandResult) -> None:
    if result.returncode != 0:
        raise DockerSbxError(
            operation,
            "nonzero_exit",
            returncode=result.returncode,
            stderr=result.stderr,
        )


def _has_token(output: str, token: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", output) is not None


def _format_cpus(cpus: float) -> str:
    return str(int(cpus)) if float(cpus).is_integer() else str(cpus)


def _new_sandbox_id(name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9.-]+", "-", str(name)).strip(".-")[:24]
    if not safe_name:
        safe_name = "trial"
    return f"repotrial-{safe_name}-{uuid.uuid4().hex[:12]}"


def _parse_published_port(output: str, container_port: int) -> int:
    try:
        decoded: Any = json.loads(output)
    except (json.JSONDecodeError, UnicodeError):
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


def _parse_network_events(output: str, sandbox_id: str) -> list[dict[str, Any]] | None:
    try:
        decoded: Any = json.loads(output)
    except (json.JSONDecodeError, UnicodeError):
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
