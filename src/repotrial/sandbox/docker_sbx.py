"""Fail-closed Docker Sandboxes provider."""

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NoReturn

from .base import (
    ExecResult,
    FailureEvidenceRecord,
    FailureEvidenceValue,
    NetworkLogResult,
    SandboxFailureEvidence,
    SandboxProvider,
    attach_partial_create_cleanup_context,
    attach_sandbox_failure_evidence,
    get_sandbox_failure_evidence,
    serialize_sandbox_failure_evidence,
)

MAX_OUTPUT_BYTES = 65_536
MAX_NETWORK_EVENTS = 100
REAP_TIMEOUT_SECONDS = 5
_TRUNCATION_MARKER = b"\n...[truncated]"
# Calibrated against Docker Sandboxes v0.39.0 on Windows. Root and Docker
# filesystems work at the smallest positive integer MiB policy value. The
# cloned-workspace floor is the minimum compatible allocation. Keep the
# calibrated one-eighth allocation through a stable platform plateau, then
# grow the workspace allocation for larger policies.
ROOT_FLOOR_MB = 1
DOCKER_FLOOR_MB = 1
WORKSPACE_FLOOR_MB = 5
_WORKSPACE_ALLOCATION_PLATEAU_MB = 128
_REPARSE_POINT_ATTRIBUTE = 0x400
_COMMIT_SHA_LINE = re.compile(rb"[0-9a-f]{40}\r?\n\Z")
_DISK_SIZE_ENVIRONMENT_VARIABLES = (
    "DOCKER_SANDBOXES_ROOT_SIZE",
    "DOCKER_SANDBOXES_DOCKER_SIZE",
    "DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE",
)
_MAX_CLONE_DIAGNOSTIC_ENTRIES = 32
_MAX_CLONE_DIAGNOSTIC_BYTES = 16_384
_MAX_CLONE_DIAGNOSTIC_VALUE_BYTES = 512
_GIT_CONFIG_KEYS = ("core.autocrlf", "core.filemode", "core.symlinks")
_GIT_STATUS_CODES = frozenset(" MARDTUC?!")
_GIT_DIFF_HEADER = re.compile(
    rb":(?P<old_mode>[0-7]{6}) (?P<new_mode>[0-7]{6}) "
    rb"(?P<old_blob>\S+) (?P<new_blob>\S+) "
    rb"(?P<status>[A-Z][0-9]{0,3})\Z"
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
_NETWORK_LOG_KEYS = {"blocked_hosts", "allowed_hosts"}
_NETWORK_LOG_COMMON_ENTRY_KEYS = {
    "host",
    "vm_name",
    "proxy_type",
    "rule",
    "last_seen",
    "since",
    "count_since",
}
_BLOCKED_NETWORK_LOG_ENTRY_KEYS = _NETWORK_LOG_COMMON_ENTRY_KEYS | {"reason"}
_ALLOWED_NETWORK_LOG_ENTRY_KEYS = _NETWORK_LOG_COMMON_ENTRY_KEYS
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
        "localhost",
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
        failure_evidence: SandboxFailureEvidence | None = None,
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
        if failure_evidence is not None:
            attach_sandbox_failure_evidence(self, failure_evidence)


@dataclass(frozen=True, slots=True)
class DiskAllocation:
    root_mb: int
    docker_mb: int
    workspace_mb: int


@dataclass(frozen=True, slots=True)
class _GitStatusEntry:
    porcelain_status: str
    path: bytes
    path2: bytes | None
    raw: bytes


@dataclass(frozen=True, slots=True)
class _GitDiffEntry:
    diff_status: str
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    path: bytes
    path2: bytes | None
    raw: bytes


@dataclass(frozen=True, slots=True)
class _GitChange:
    porcelain_status: str
    diff_status: str
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    path: bytes
    path2: bytes | None
    raw_status: bytes
    raw_diff: bytes


@dataclass(frozen=True, slots=True)
class _CloneDiagnostics:
    changes: tuple[_GitChange, ...]
    host_git_config: dict[str, str]
    guest_git_config: dict[str, str]
    captured_parts: tuple[bytes, ...]
    truncated: bool = False
    diagnostic_status: str | None = None


def calculate_disk_allocation(disk_mb: int) -> DiskAllocation:
    """Allocate the policy disk budget across Docker Sandboxes filesystems."""
    if isinstance(disk_mb, bool) or not isinstance(disk_mb, int) or disk_mb <= 0:
        raise ValueError("disk_mb must be a positive integer")
    workspace_mb = max(
        WORKSPACE_FLOOR_MB,
        min(disk_mb // 8, _WORKSPACE_ALLOCATION_PLATEAU_MB),
        disk_mb // 16,
    )
    docker_mb = disk_mb - ROOT_FLOOR_MB - workspace_mb
    if docker_mb < DOCKER_FLOOR_MB:
        raise DockerSbxUnsupportedError("disk_budget_insufficient")
    return DiskAllocation(
        root_mb=ROOT_FLOOR_MB,
        docker_mb=docker_mb,
        workspace_mb=workspace_mb,
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
class _CommandExecutionContext:
    operation: str
    sandbox_id: str | None
    deadline_limited: bool
    subprocess_started: bool
    trial_elapsed_s: float | None
    trial_remaining_s: float | None


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    _execution_context: _CommandExecutionContext


class _SandboxState(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    CLEANUP_UNSAFE = "cleanup-unsafe"
    CLEANED = "cleaned"


class _ProcessCleanupError(RuntimeError):
    pass


class _DuplicateJsonKeyError(ValueError):
    pass


class DockerSbxProvider(SandboxProvider):
    """Run Docker Sandboxes only after an exact capability probe succeeds."""

    def __init__(
        self,
        policy: DockerSbxPolicy,
        *,
        command_timeout_s: float = 120,
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
        self._trial_deadline: float | None = None
        self._network_log_sandboxes: set[str] = set()

    async def create(self, workspace: Path, name: str) -> str:
        deadline = self._trial_deadline
        first_successful_create = deadline is None
        if deadline is None:
            deadline = time.monotonic() + self._policy.total_duration_s
        resolved_workspace = _resolve_workspace(workspace)
        workspace_identity = _workspace_identity(resolved_workspace)
        allocation = calculate_disk_allocation(self._policy.disk_mb)
        host_head = await self._host_head(resolved_workspace, deadline)
        network_log_supported = await self._probe(deadline)
        self._require_workspace_unchanged(
            resolved_workspace,
            workspace_identity,
        )
        if await self._host_head(resolved_workspace, deadline) != host_head:
            raise DockerSbxError("clone_verification", "host_head_changed")
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
        arguments.extend(("shell", str(resolved_workspace)))
        try:
            result = await self._run(
                "create",
                arguments,
                self._command_timeout_s,
                env=self._disk_environment(allocation),
                deadline=deadline,
                sandbox_id=sandbox_id,
            )
            _require_success("create", result)
            await self._verify_guest_clone(
                sandbox_id,
                host_head,
                resolved_workspace,
                deadline,
            )
            allow_network = await self._run(
                "allow_network",
                [
                    "policy",
                    "allow",
                    "network",
                    "--sandbox",
                    sandbox_id,
                    "**",
                ],
                self._command_timeout_s,
                deadline=deadline,
                sandbox_id=sandbox_id,
                public_sandbox_id=sandbox_id,
            )
            _require_success("allow_network", allow_network)
        except (DockerSbxError, asyncio.CancelledError) as error:
            await self._cleanup_after_uncertain_failure(
                sandbox_id,
                error,
                partial_create=True,
            )
        self._sandbox_states[sandbox_id] = _SandboxState.ACTIVE
        if first_successful_create:
            self._trial_deadline = deadline
        self._sandbox_deadlines[sandbox_id] = deadline
        if network_log_supported:
            self._network_log_sandboxes.add(sandbox_id)
        return sandbox_id

    async def _host_head(self, workspace: Path, deadline: float) -> str:
        result = await self._run_command(
            "git",
            "clone_verification",
            [
                "-C",
                str(workspace),
                "rev-parse",
                "--verify",
                "--end-of-options",
                "HEAD^{commit}",
            ],
            self._command_timeout_s,
            env=self._host_git_environment(),
            deadline=deadline,
        )
        _require_success("clone_verification", result)
        return _parse_commit_sha(result.stdout, "host_head_invalid")

    def _require_workspace_unchanged(
        self, workspace: Path, expected_identity: tuple[int, int]
    ) -> None:
        if _workspace_identity(workspace) != expected_identity:
            raise DockerSbxError("clone_verification", "workspace_changed")

    def _host_git_environment(self) -> dict[str, str]:
        environment = self._subprocess_environment.copy()
        for variable in tuple(environment):
            if variable.upper().startswith("GIT_") or variable.upper() == "SSH_ASKPASS":
                environment.pop(variable)
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
            }
        )
        return environment

    async def _verify_guest_clone(
        self,
        sandbox_id: str,
        host_head: str,
        host_workspace: Path,
        deadline: float,
    ) -> None:
        guest_workspace = _parse_guest_path(
            await self._guest_clone_command(sandbox_id, ["pwd"], deadline),
            "guest_pwd_invalid",
        )
        guest_top_level = _parse_guest_path(
            await self._guest_clone_command(
                sandbox_id, ["git", "rev-parse", "--show-toplevel"], deadline
            ),
            "guest_top_level_invalid",
        )
        if guest_top_level != guest_workspace:
            raise DockerSbxError("clone_verification", "guest_workspace_mismatch")
        inside_work_tree = await self._guest_clone_command(
            sandbox_id,
            ["git", "rev-parse", "--is-inside-work-tree"],
            deadline,
        )
        if inside_work_tree not in (b"true\n", b"true\r\n"):
            raise DockerSbxError("clone_verification", "guest_not_work_tree")
        guest_head = _parse_commit_sha(
            await self._guest_clone_command(
                sandbox_id,
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    "HEAD^{commit}",
                ],
                deadline,
            ),
            "guest_head_invalid",
        )
        if guest_head != host_head:
            raise DockerSbxError("clone_verification", "guest_head_mismatch")
        status = await self._guest_clone_command(
            sandbox_id,
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=no",
            ],
            deadline,
        )
        if status:
            diagnostics = await self._collect_clone_diagnostics(
                sandbox_id,
                host_workspace,
                status,
                deadline,
            )
            failure_evidence = self._clone_failure_evidence(
                sandbox_id,
                deadline,
                diagnostics,
            )
            raise DockerSbxError(
                "clone_verification",
                "guest_status_not_clean",
                failure_evidence=failure_evidence,
            )

    async def _collect_clone_diagnostics(
        self,
        sandbox_id: str,
        host_workspace: Path,
        status_output: bytes,
        deadline: float,
    ) -> _CloneDiagnostics:
        status_entries, status_malformed = _parse_git_status_output(status_output)
        if status_malformed or not status_entries:
            return _CloneDiagnostics(
                changes=(),
                host_git_config={},
                guest_git_config={},
                captured_parts=(_bounded_clone_capture(status_output),),
                truncated=len(status_output) > _MAX_CLONE_DIAGNOSTIC_BYTES,
                diagnostic_status="malformed",
            )

        try:
            diff_output = await self._guest_clone_command(
                sandbox_id,
                ["git", "diff", "--raw", "-z", "--no-ext-diff", "HEAD", "--"],
                deadline,
            )
            diff_entries, diff_malformed = _parse_git_diff_output(diff_output)
            changes, merge_malformed = _merge_git_changes(
                status_entries,
                diff_entries,
            )
            if diff_malformed or merge_malformed:
                return _CloneDiagnostics(
                    changes=(),
                    host_git_config={},
                    guest_git_config={},
                    captured_parts=(_bounded_clone_capture(status_output),),
                    truncated=(
                        len(status_output) > _MAX_CLONE_DIAGNOSTIC_BYTES
                        or len(diff_output) > _MAX_CLONE_DIAGNOSTIC_BYTES
                    ),
                    diagnostic_status="malformed",
                )
            host_config, host_config_parts = await self._collect_host_git_config(
                host_workspace,
                deadline,
            )
            guest_config, guest_config_parts = await self._collect_guest_git_config(
                sandbox_id,
                deadline,
            )
        except DockerSbxError as error:
            if error.reason in {
                "process_cleanup_unconfirmed",
                "total_duration_exhausted",
            }:
                raise
            return _CloneDiagnostics(
                changes=(),
                host_git_config={},
                guest_git_config={},
                captured_parts=(_bounded_clone_capture(status_output),),
                truncated=len(status_output) > _MAX_CLONE_DIAGNOSTIC_BYTES,
                diagnostic_status="unavailable",
            )
        except ValueError:
            return _CloneDiagnostics(
                changes=(),
                host_git_config={},
                guest_git_config={},
                captured_parts=(_bounded_clone_capture(status_output),),
                truncated=len(status_output) > _MAX_CLONE_DIAGNOSTIC_BYTES,
                diagnostic_status="malformed",
            )

        return _CloneDiagnostics(
            changes=tuple(changes),
            host_git_config=host_config,
            guest_git_config=guest_config,
            captured_parts=(
                *(_bounded_clone_capture(part) for part in host_config_parts),
                *(_bounded_clone_capture(part) for part in guest_config_parts),
            ),
            truncated=(
                len(status_entries) > _MAX_CLONE_DIAGNOSTIC_ENTRIES
                or len(diff_entries) > _MAX_CLONE_DIAGNOSTIC_ENTRIES
                or len(status_output) > _MAX_CLONE_DIAGNOSTIC_BYTES
                or len(diff_output) > _MAX_CLONE_DIAGNOSTIC_BYTES
            ),
        )

    async def _collect_host_git_config(
        self,
        host_workspace: Path,
        deadline: float,
    ) -> tuple[dict[str, str], tuple[bytes, ...]]:
        values: dict[str, str] = {}
        outputs: list[bytes] = []
        for key in _GIT_CONFIG_KEYS:
            result = await self._run_command(
                "git",
                "clone_verification",
                [
                    "-C",
                    str(host_workspace),
                    "config",
                    "--default",
                    "unset",
                    "--get",
                    key,
                ],
                self._command_timeout_s,
                env=self._host_git_environment(),
                deadline=deadline,
            )
            _require_success("clone_verification", result)
            values[key] = _parse_git_config_value(result.stdout)
            outputs.append(result.stdout)
        return values, tuple(outputs)

    async def _collect_guest_git_config(
        self,
        sandbox_id: str,
        deadline: float,
    ) -> tuple[dict[str, str], tuple[bytes, ...]]:
        values: dict[str, str] = {}
        outputs: list[bytes] = []
        for key in _GIT_CONFIG_KEYS:
            output = await self._guest_clone_command(
                sandbox_id,
                ["git", "config", "--default", "unset", "--get", key],
                deadline,
            )
            values[key] = _parse_git_config_value(output)
            outputs.append(output)
        return values, tuple(outputs)

    def _clone_failure_evidence(
        self,
        sandbox_id: str,
        deadline: float,
        diagnostics: _CloneDiagnostics,
    ) -> SandboxFailureEvidence:
        max_records = min(
            len(diagnostics.changes),
            _MAX_CLONE_DIAGNOSTIC_ENTRIES,
        )
        for record_count in range(max_records, -1, -1):
            selected = diagnostics.changes[:record_count]
            captured_parts = (
                tuple(
                    part
                    for change in selected
                    for part in (change.raw_status, change.raw_diff)
                )
                + diagnostics.captured_parts
            )
            captured = b"".join(captured_parts)
            if len(captured) > _MAX_CLONE_DIAGNOSTIC_BYTES:
                continue
            truncated = diagnostics.truncated or record_count < len(diagnostics.changes)
            details = _clone_diagnostic_details(
                selected,
                diagnostics.host_git_config,
                diagnostics.guest_git_config,
                captured,
                truncated=truncated,
                diagnostic_status=diagnostics.diagnostic_status,
            )
            evidence = self._command_failure_evidence(
                "clone_verification",
                "guest_status_not_clean",
                deadline=deadline,
                deadline_limited=False,
                subprocess_started=True,
                sandbox_id=sandbox_id,
                details=details,
            )
            try:
                serialize_sandbox_failure_evidence(evidence)
            except ValueError:
                continue
            return evidence

        details = _clone_diagnostic_details(
            (),
            {},
            {},
            b"",
            truncated=True,
            diagnostic_status=diagnostics.diagnostic_status,
        )
        return self._command_failure_evidence(
            "clone_verification",
            "guest_status_not_clean",
            deadline=deadline,
            deadline_limited=False,
            subprocess_started=True,
            sandbox_id=sandbox_id,
            details=details,
        )

    async def _guest_clone_command(
        self, sandbox_id: str, argv: list[str], deadline: float
    ) -> bytes:
        result = await self._run(
            "clone_verification",
            ["exec", sandbox_id, "--", *argv],
            self._command_timeout_s,
            deadline=deadline,
            sandbox_id=sandbox_id,
            public_sandbox_id=sandbox_id,
        )
        _require_success("clone_verification", result)
        return result.stdout

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
            public_sandbox_id=sandbox_id,
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
                public_sandbox_id=sandbox_id,
            )
            _require_success("publish_port", published)
            listed = await self._run(
                "publish_port",
                ["ports", sandbox_id, "--json"],
                self._command_timeout_s,
                deadline=self._require_deadline(sandbox_id),
                sandbox_id=sandbox_id,
                public_sandbox_id=sandbox_id,
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
            public_sandbox_id=sandbox_id,
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
                public_sandbox_id=sandbox_id,
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
            sandbox_id=sandbox_id,
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
                failure_evidence=self._sandbox_cleanup_failure_evidence(
                    primary_error,
                    sandbox_id,
                    cleanup_error,
                ),
            ) from primary_error
        if cancellation is not None:
            raise cancellation
        raise primary_error

    def _sandbox_cleanup_failure_evidence(
        self,
        primary_error: DockerSbxError,
        sandbox_id: str,
        cleanup_error: BaseException,
    ) -> SandboxFailureEvidence:
        cleanup_evidence = get_sandbox_failure_evidence(cleanup_error)
        if cleanup_evidence is None:
            return self._command_failure_evidence(
                primary_error.operation,
                "cleanup_unconfirmed",
                deadline=None,
                deadline_limited=False,
                subprocess_started=False,
                sandbox_id=sandbox_id,
            )
        return SandboxFailureEvidence(
            operation=primary_error.operation,
            reason="cleanup_unconfirmed",
            sandbox_id=sandbox_id,
            trial_elapsed_s=cleanup_evidence.trial_elapsed_s,
            trial_remaining_s=cleanup_evidence.trial_remaining_s,
            deadline_limited=cleanup_evidence.deadline_limited,
            subprocess_started=cleanup_evidence.subprocess_started,
        )

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
            (
                "policy_allow_network",
                ["policy", "allow", "network", "--help"],
                ("--sandbox", '"**"'),
            ),
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
        public_sandbox_id: str | None = None,
    ) -> _CommandResult:
        return await self._run_command(
            "sbx",
            operation,
            arguments,
            timeout_s,
            env=env,
            deadline=deadline,
            sandbox_id=sandbox_id,
            public_sandbox_id=public_sandbox_id,
        )

    async def _run_command(
        self,
        executable: str,
        operation: str,
        arguments: list[str],
        timeout_s: float,
        *,
        env: dict[str, str] | None = None,
        deadline: float | None = None,
        sandbox_id: str | None = None,
        public_sandbox_id: str | None = None,
    ) -> _CommandResult:
        process: asyncio.subprocess.Process | None = None
        try:
            effective_timeout_s, deadline_limited = self._effective_timeout(
                operation,
                timeout_s,
                deadline=deadline,
                public_sandbox_id=public_sandbox_id,
                evidence_sandbox_id=sandbox_id,
            )
            async with asyncio.timeout(effective_timeout_s):
                spawn_kwargs: dict[str, Any] = {
                    "stdin": asyncio.subprocess.DEVNULL,
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "env": env if env is not None else self._subprocess_environment,
                }
                process = await asyncio.create_subprocess_exec(
                    executable,
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
            failure_evidence = self._command_failure_evidence(
                operation,
                timeout_reason,
                deadline=deadline,
                deadline_limited=deadline_limited,
                subprocess_started=process is not None,
                sandbox_id=sandbox_id,
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    timeout_reason,
                    process,
                    error,
                    sandbox_id=public_sandbox_id if deadline_limited else None,
                    failure_evidence=failure_evidence,
                )
            raise DockerSbxError(
                operation,
                timeout_reason,
                sandbox_id=public_sandbox_id if deadline_limited else None,
                failure_evidence=failure_evidence,
            ) from error
        except asyncio.CancelledError as error:
            if process is not None:
                await _raise_after_process_cleanup(
                    operation, "cancelled", process, error
                )
            raise
        except FileNotFoundError as error:
            failure_evidence = self._command_failure_evidence(
                operation,
                "executable_unavailable",
                deadline=deadline,
                deadline_limited=deadline_limited,
                subprocess_started=False,
                sandbox_id=sandbox_id,
            )
            raise DockerSbxError(
                operation,
                "executable_unavailable",
                failure_evidence=failure_evidence,
            ) from error
        except OSError as error:
            failure_evidence = self._command_failure_evidence(
                operation,
                "io_error",
                deadline=deadline,
                deadline_limited=deadline_limited,
                subprocess_started=process is not None,
                sandbox_id=sandbox_id,
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    "io_error",
                    process,
                    error,
                    failure_evidence=failure_evidence,
                )
            raise DockerSbxError(
                operation,
                "io_error",
                failure_evidence=failure_evidence,
            ) from error

        now = time.monotonic()
        remaining_s = None if deadline is None else max(0.0, deadline - now)
        elapsed_s = (
            None
            if deadline is None
            else max(0.0, self._policy.total_duration_s - (deadline - now))
        )
        return _CommandResult(
            returncode=returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            _execution_context=_CommandExecutionContext(
                operation=operation,
                sandbox_id=sandbox_id,
                deadline_limited=deadline_limited,
                subprocess_started=True,
                trial_elapsed_s=elapsed_s,
                trial_remaining_s=remaining_s,
            ),
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
        public_sandbox_id: str | None,
        evidence_sandbox_id: str | None,
    ) -> tuple[float, bool]:
        if deadline is None:
            return timeout_s, False
        now = time.monotonic()
        remaining_s = deadline - now
        if remaining_s <= 0:
            raise DockerSbxError(
                operation,
                "total_duration_exhausted",
                sandbox_id=public_sandbox_id,
                failure_evidence=self._command_failure_evidence_at(
                    operation,
                    "total_duration_exhausted",
                    deadline=deadline,
                    deadline_limited=True,
                    subprocess_started=False,
                    sandbox_id=evidence_sandbox_id,
                    now=now,
                ),
            )
        if remaining_s <= timeout_s:
            return remaining_s, True
        return timeout_s, False

    def _command_failure_evidence(
        self,
        operation: str,
        reason: str,
        *,
        deadline: float | None,
        deadline_limited: bool,
        subprocess_started: bool,
        sandbox_id: str | None,
        returncode: int | None = None,
        details: dict[str, FailureEvidenceValue] | None = None,
    ) -> SandboxFailureEvidence:
        return self._command_failure_evidence_at(
            operation,
            reason,
            deadline=deadline,
            deadline_limited=deadline_limited,
            subprocess_started=subprocess_started,
            sandbox_id=sandbox_id,
            returncode=returncode,
            details=details,
            now=time.monotonic(),
        )

    def _command_failure_evidence_at(
        self,
        operation: str,
        reason: str,
        *,
        deadline: float | None,
        deadline_limited: bool,
        subprocess_started: bool,
        sandbox_id: str | None,
        returncode: int | None = None,
        details: dict[str, FailureEvidenceValue] | None = None,
        now: float,
    ) -> SandboxFailureEvidence:
        remaining_s = None if deadline is None else max(0.0, deadline - now)
        elapsed_s = (
            None
            if deadline is None
            else max(0.0, self._policy.total_duration_s - (deadline - now))
        )
        return SandboxFailureEvidence(
            operation=operation,
            reason=reason,
            returncode=returncode,
            sandbox_id=sandbox_id,
            trial_elapsed_s=elapsed_s,
            trial_remaining_s=remaining_s,
            deadline_limited=deadline_limited,
            subprocess_started=subprocess_started,
            details=dict(details or {}),
        )


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    payload_limit = MAX_OUTPUT_BYTES - len(_TRUNCATION_MARKER)
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    head = bytearray()
    tail = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        head_remaining = head_limit - len(head)
        if head_remaining > 0:
            head.extend(chunk[:head_remaining])
        tail_chunk = memoryview(chunk)[min(len(chunk), head_remaining) :]
        if not tail_chunk:
            continue
        if len(tail_chunk) >= tail_limit:
            tail[:] = tail_chunk[-tail_limit:]
            truncated = True
            continue
        excess = len(tail) + len(tail_chunk) - tail_limit
        if excess > 0:
            del tail[:excess]
            truncated = True
        tail.extend(tail_chunk)
    if truncated:
        return bytes(head) + _TRUNCATION_MARKER + bytes(tail)
    return bytes(head) + bytes(tail)


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
    failure_evidence: SandboxFailureEvidence | None = None,
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
        assert failure_evidence is not None
        cleanup_evidence = SandboxFailureEvidence(
            operation=operation,
            reason="process_cleanup_unconfirmed",
            sandbox_id=failure_evidence.sandbox_id,
            trial_elapsed_s=failure_evidence.trial_elapsed_s,
            trial_remaining_s=failure_evidence.trial_remaining_s,
            deadline_limited=failure_evidence.deadline_limited,
            subprocess_started=failure_evidence.subprocess_started,
        )
        provider_error = DockerSbxError(
            operation,
            reason,
            sandbox_id=sandbox_id,
            failure_evidence=failure_evidence,
        )
        raise DockerSbxError(
            operation,
            "process_cleanup_unconfirmed",
            cleanup_error=str(cleanup_error),
            failure_evidence=cleanup_evidence,
        ) from provider_error
    raise DockerSbxError(
        operation,
        reason,
        sandbox_id=sandbox_id,
        failure_evidence=failure_evidence,
    ) from primary_error


def _decode_human_output(output: bytes) -> str:
    decoded = output.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return decoded
    payload_limit = MAX_OUTPUT_BYTES - len(_TRUNCATION_MARKER)
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    head = encoded[:head_limit]
    tail = encoded[-tail_limit:]
    while True:
        try:
            decoded_head = head.decode("utf-8")
            break
        except UnicodeDecodeError:
            head = head[:-1]
    while True:
        try:
            decoded_tail = tail.decode("utf-8")
            break
        except UnicodeDecodeError:
            tail = tail[1:]
    return decoded_head + _TRUNCATION_MARKER.decode("ascii") + decoded_tail


def _require_success(operation: str, result: _CommandResult) -> None:
    if result.returncode != 0:
        context = result._execution_context
        failure_evidence = SandboxFailureEvidence(
            operation=context.operation,
            reason="nonzero_exit",
            returncode=result.returncode,
            sandbox_id=context.sandbox_id,
            trial_elapsed_s=context.trial_elapsed_s,
            trial_remaining_s=context.trial_remaining_s,
            deadline_limited=context.deadline_limited,
            subprocess_started=context.subprocess_started,
        )
        raise DockerSbxError(
            operation,
            "nonzero_exit",
            returncode=result.returncode,
            stderr=_decode_human_output(result.stderr),
            failure_evidence=failure_evidence,
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


def _resolve_workspace(workspace: Path) -> Path:
    candidate = Path(workspace)
    try:
        if _is_link_or_reparse_point(candidate):
            raise DockerSbxError("clone_verification", "workspace_invalid")
        resolved = candidate.resolve(strict=True)
        _workspace_identity(resolved)
    except DockerSbxError:
        raise
    except (OSError, RuntimeError) as error:
        raise DockerSbxError("clone_verification", "workspace_invalid") from error
    return resolved


def _workspace_identity(workspace: Path) -> tuple[int, int]:
    try:
        if _is_link_or_reparse_point(workspace) or not workspace.is_dir():
            raise DockerSbxError("clone_verification", "workspace_invalid")
        status = workspace.stat()
    except DockerSbxError:
        raise
    except OSError as error:
        raise DockerSbxError("clone_verification", "workspace_invalid") from error
    return status.st_dev, status.st_ino


def _is_link_or_reparse_point(path: Path) -> bool:
    status = path.lstat()
    attributes = getattr(status, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _parse_commit_sha(output: bytes, reason: str) -> str:
    if _COMMIT_SHA_LINE.fullmatch(output) is None:
        raise DockerSbxError("clone_verification", reason)
    return output[:40].decode("ascii")


def _parse_guest_path(output: bytes, reason: str) -> str:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise DockerSbxError("clone_verification", reason) from None
    if text.endswith("\r\n"):
        path = text[:-2]
    elif text.endswith("\n"):
        path = text[:-1]
    else:
        raise DockerSbxError("clone_verification", reason)
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise DockerSbxError("clone_verification", reason)
    return path


def _parse_git_status_output(
    output: bytes,
) -> tuple[tuple[_GitStatusEntry, ...], bool]:
    if not output:
        return (), False
    fields = output.split(b"\0")
    if fields[-1] != b"":
        return (), True
    entries: list[_GitStatusEntry] = []
    try:
        index = 0
        while index < len(fields) - 1:
            entry = fields[index]
            index += 1
            if len(entry) < 4 or entry[2:3] != b" ":
                return (), True
            status = entry[:2].decode("ascii")
            if (
                status == "  "
                or status in {"??", "!!"}
                or any(character not in _GIT_STATUS_CODES for character in status)
            ):
                return (), True
            path = _normalize_git_path(entry[3:])
            path2: bytes | None = None
            raw = entry + b"\0"
            if "R" in status or "C" in status:
                if index >= len(fields) - 1:
                    return (), True
                path2 = _normalize_git_path(fields[index])
                index += 1
                raw += fields[index - 1] + b"\0"
            entries.append(
                _GitStatusEntry(
                    porcelain_status=status,
                    path=path,
                    path2=path2,
                    raw=raw,
                )
            )
    except (UnicodeDecodeError, ValueError):
        return (), True
    return tuple(entries), False


def _parse_git_diff_output(
    output: bytes,
) -> tuple[tuple[_GitDiffEntry, ...], bool]:
    if not output:
        return (), False
    fields = output.split(b"\0")
    if fields[-1] != b"":
        return (), True
    entries: list[_GitDiffEntry] = []
    index = 0
    while index < len(fields) - 1:
        header = fields[index]
        index += 1
        match = _GIT_DIFF_HEADER.fullmatch(header)
        if match is None or index >= len(fields) - 1:
            return (), True
        path_bytes = fields[index]
        index += 1
        try:
            path = _normalize_git_path(path_bytes)
            diff_status = _normalize_git_token(match.group("status"))
            old_blob = _normalize_git_token(match.group("old_blob"))
            new_blob = _normalize_git_token(match.group("new_blob"))
        except ValueError:
            return (), True
        path2: bytes | None = None
        raw = header + b"\0" + path_bytes + b"\0"
        if diff_status.startswith(("R", "C")):
            if index >= len(fields) - 1:
                return (), True
            try:
                path2 = _normalize_git_path(fields[index])
            except ValueError:
                return (), True
            index += 1
            raw += fields[index - 1] + b"\0"
        entries.append(
            _GitDiffEntry(
                diff_status=diff_status,
                old_mode=match.group("old_mode").decode("ascii"),
                new_mode=match.group("new_mode").decode("ascii"),
                old_blob=old_blob,
                new_blob=new_blob,
                path=path,
                path2=path2,
                raw=raw,
            )
        )
    return tuple(entries), False


def _merge_git_changes(
    status_entries: tuple[_GitStatusEntry, ...],
    diff_entries: tuple[_GitDiffEntry, ...],
) -> tuple[tuple[_GitChange, ...], bool]:
    if not status_entries or len(status_entries) != len(diff_entries):
        return (), True
    remaining = list(diff_entries)
    changes: list[_GitChange] = []
    for status in status_entries:
        match_index: int | None = None
        for index, diff in enumerate(remaining):
            exact = (status.path, status.path2) == (diff.path, diff.path2)
            reversed_paths = status.path2 is not None and (
                status.path,
                status.path2,
            ) == (diff.path2, diff.path)
            if exact or reversed_paths:
                match_index = index
                break
        if match_index is None:
            return (), True
        diff = remaining.pop(match_index)
        changes.append(
            _GitChange(
                porcelain_status=status.porcelain_status,
                diff_status=diff.diff_status,
                old_mode=diff.old_mode,
                new_mode=diff.new_mode,
                old_blob=diff.old_blob,
                new_blob=diff.new_blob,
                path=status.path,
                path2=status.path2,
                raw_status=status.raw,
                raw_diff=diff.raw,
            )
        )
    return tuple(changes), False


def _normalize_git_path(raw: bytes) -> bytes:
    path = raw.decode("utf-8", errors="backslashreplace")
    windows_path = PureWindowsPath(path)
    if (
        not path
        or PurePosixPath(path).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or path.startswith(("/", "\\"))
        or any(part == ".." for part in path.replace("\\", "/").split("/"))
        or len(path.encode("utf-8")) > _MAX_CLONE_DIAGNOSTIC_VALUE_BYTES
    ):
        raise ValueError("unsafe Git path")
    return raw


def _display_git_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="backslashreplace")


def _normalize_git_token(raw: bytes) -> str:
    token = raw.decode("utf-8", errors="backslashreplace")
    if (
        not token
        or any(character.isspace() for character in token)
        or len(token.encode("utf-8")) > _MAX_CLONE_DIAGNOSTIC_VALUE_BYTES
    ):
        raise ValueError("invalid Git metadata token")
    return token


def _parse_git_config_value(output: bytes) -> str:
    if output.endswith(b"\r\n"):
        value_bytes = output[:-2]
    elif output.endswith(b"\n"):
        value_bytes = output[:-1]
    else:
        raise ValueError("Git config output is not line terminated")
    value = value_bytes.decode("utf-8", errors="backslashreplace")
    if "\r" in value or "\n" in value:
        raise ValueError("Git config output contains multiple lines")
    if len(value.encode("utf-8")) > _MAX_CLONE_DIAGNOSTIC_VALUE_BYTES:
        raise ValueError("Git config output is too large")
    return value


def _bounded_clone_capture(output: bytes) -> bytes:
    return output[:_MAX_CLONE_DIAGNOSTIC_BYTES]


def _clone_diagnostic_details(
    changes: tuple[_GitChange, ...],
    host_git_config: dict[str, str],
    guest_git_config: dict[str, str],
    captured: bytes,
    *,
    truncated: bool,
    diagnostic_status: str | None,
) -> dict[str, FailureEvidenceValue]:
    records: list[FailureEvidenceRecord] = []
    for change in changes:
        record: FailureEvidenceRecord = {
            "porcelain_status": change.porcelain_status,
            "diff_status": change.diff_status,
            "old_mode": change.old_mode,
            "new_mode": change.new_mode,
            "old_blob": change.old_blob,
            "new_blob": change.new_blob,
            "path": _display_git_path(change.path),
        }
        if change.path2 is not None:
            record["path2"] = _display_git_path(change.path2)
        records.append(record)
    details: dict[str, FailureEvidenceValue] = {
        "captured_bytes_sha256": hashlib.sha256(captured).hexdigest(),
        "tracked_changes": records,
        "host_git_config": dict(host_git_config),
        "guest_git_config": dict(guest_git_config),
        "truncated": truncated,
    }
    if diagnostic_status is not None:
        details["diagnostic_status"] = diagnostic_status
    return details


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
        decoded: Any = json.loads(
            text,
            object_pairs_hook=_strict_json_object_from_pairs,
        )
    except (json.JSONDecodeError, _DuplicateJsonKeyError):
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
            or protocol not in ("tcp", "tcp4")
        ):
            raise DockerSbxError("publish_port", "port_mapping_invalid")
        if sandbox_port == container_port:
            if protocol != "tcp4":
                raise DockerSbxError("publish_port", "port_mapping_invalid")
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
        decoded: Any = json.loads(
            text,
            object_pairs_hook=_strict_json_object_from_pairs,
        )
    except (json.JSONDecodeError, _DuplicateJsonKeyError):
        return None
    if not isinstance(decoded, dict) or set(decoded) != _NETWORK_LOG_KEYS:
        return None
    blocked_hosts = decoded["blocked_hosts"]
    allowed_hosts = decoded["allowed_hosts"]
    if (
        not isinstance(blocked_hosts, list)
        or not isinstance(allowed_hosts, list)
        or len(blocked_hosts) + len(allowed_hosts) > MAX_NETWORK_EVENTS
    ):
        return None
    events: list[dict[str, Any]] = []
    for decision, entries, entry_keys in (
        ("blocked", blocked_hosts, _BLOCKED_NETWORK_LOG_ENTRY_KEYS),
        ("allowed", allowed_hosts, _ALLOWED_NETWORK_LOG_ENTRY_KEYS),
    ):
        for item in entries:
            if not isinstance(item, dict) or set(item) != entry_keys:
                return None
            string_fields = tuple(entry_keys - {"count_since"})
            if any(
                not isinstance(item[field], str) or len(item[field]) > 1024
                for field in string_fields
            ):
                return None
            if (
                not item["host"]
                or not item["last_seen"]
                or not item["since"]
                or item["vm_name"] != sandbox_id
                or item["proxy_type"] not in _NETWORK_PROXIES
                or type(item["count_since"]) is not int
                or item["count_since"] < 1
            ):
                return None
            events.append(
                {
                    "sandbox": item["vm_name"],
                    "decision": decision,
                    "host": item["host"],
                    "proxy": item["proxy_type"],
                    "rule": item["rule"],
                    "reason": item["reason"] if decision == "blocked" else "",
                    "last_seen": item["last_seen"],
                    "count": item["count_since"],
                }
            )
    return events


def _strict_json_object_from_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise _DuplicateJsonKeyError
        parsed[key] = value
    return parsed


def _unsupported_network_log(reason: str) -> NetworkLogResult:
    return NetworkLogResult(events=[], supported=False, unsupported_reason=reason)
