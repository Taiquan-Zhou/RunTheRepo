"""Fail-closed Docker Sandboxes provider."""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO, NoReturn

from .base import (
    ExecResult,
    FailureEvidenceRecord,
    FailureEvidenceValue,
    NetworkLogResult,
    RuntimeTemplateAudit,
    RuntimeTemplateIdentity,
    SandboxFailureEvidence,
    SandboxProvider,
    _normalize_runtime_template_repository,
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
_RUNTIME_TEMPLATE_TAG = re.compile(r"repotrial-runtime:[0-9a-f]{32}\Z")
_IMAGE_ID = re.compile(r"[0-9a-f]{12}\Z")
_RUNTIME_TEMPLATE_REPOSITORY = "docker.io/library/repotrial-runtime"
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
_PROCESS_CLEANUP_UNCONFIRMED_NOTE_PREFIX = "process cleanup unconfirmed:"
_ANONYMOUS_CONFIG_RETAINED_NOTE = (
    "anonymous Docker config retained because process cleanup is unconfirmed"
)
_ANONYMOUS_CONFIG_CLEANUP_FAILED_NOTE = "anonymous Docker config cleanup failed"
_IMAGE_BUNDLE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,511}\Z")
_IMAGE_BUNDLE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE_BUNDLE_CHUNK_BYTES = 8192

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


@dataclass(frozen=True, slots=True)
class _RuntimeTemplate:
    repository: str
    tag: str
    image_id: str


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
        self._stopped_sandboxes: set[str] = set()
        self._runtime_template_tag: str | None = None
        self._runtime_template_image_id: str | None = None
        self._runtime_template_expected_identity: str | None = None
        self._runtime_template_pending_tag: str | None = None
        self._runtime_template_activation_used = False
        self._runtime_template_finalization_confirmed = True
        self._runtime_template_identity: RuntimeTemplateIdentity | None = None
        self._runtime_template_audit = RuntimeTemplateAudit(removal_confirmed=True)
        self._runtime_image_bundle_path: Path | None = None
        self._runtime_image_bundle_file: BinaryIO | None = None
        self._runtime_image_bundle_sha256: str | None = None
        self._runtime_image_bundle_size: int | None = None
        self._runtime_image_bundle_stage_used = False

    @property
    def supports_runtime_templates(self) -> bool:
        return True

    def expected_image_identity_sha256(self) -> str | None:
        return self._runtime_template_expected_identity

    def runtime_template_audit(self) -> RuntimeTemplateAudit:
        return self._runtime_template_audit

    async def begin_invocation(self) -> None:
        if not self._runtime_template_finalization_confirmed:
            raise RuntimeError("runtime template finalization is not confirmed")
        if any(
            value is not None
            for value in (
                self._runtime_template_tag,
                self._runtime_template_image_id,
                self._runtime_template_expected_identity,
                self._runtime_template_pending_tag,
                self._runtime_template_identity,
            )
        ):
            raise RuntimeError("runtime template cleanup is not confirmed")
        if any(
            value is not None
            for value in (
                self._runtime_image_bundle_path,
                self._runtime_image_bundle_file,
                self._runtime_image_bundle_sha256,
                self._runtime_image_bundle_size,
            )
        ):
            raise RuntimeError("runtime image bundle cleanup is not confirmed")
        if (
            self._runtime_template_audit.bundle_sha256 is not None
            or self._runtime_template_audit.bundle_size is not None
        ):
            raise RuntimeError("runtime image bundle cleanup is not confirmed")
        if (
            self._runtime_template_audit.identity is not None
            and not self._runtime_template_audit.removal_confirmed
        ):
            raise RuntimeError("runtime template cleanup is not confirmed")
        if any(
            state is not _SandboxState.CLEANED
            for state in self._sandbox_states.values()
        ):
            raise RuntimeError("sandbox cleanup is not confirmed")
        if self._sandbox_deadlines:
            raise RuntimeError("sandbox deadline cleanup is not confirmed")
        if self._network_log_sandboxes or self._stopped_sandboxes:
            raise RuntimeError("sandbox cleanup is not confirmed")
        self._runtime_template_activation_used = False
        self._trial_deadline = None
        self._runtime_template_finalization_confirmed = False
        self._runtime_template_identity = None
        self._runtime_template_audit = RuntimeTemplateAudit()
        self._runtime_image_bundle_stage_used = False

    async def stage_runtime_image_bundle(
        self,
        sandbox_id: str,
        image_references: tuple[str, ...],
        image_ids: tuple[str, ...],
    ) -> None:
        self._require_active(sandbox_id)
        if sandbox_id in self._stopped_sandboxes:
            raise RuntimeError(f"sandbox is stopped: {sandbox_id}")
        if self._runtime_image_bundle_stage_used:
            raise DockerSbxError("image_bundle_stage", "image_bundle_already_staged")
        self._runtime_image_bundle_stage_used = True
        references, ids = _validate_image_bundle_inputs(image_references, image_ids)
        if any(
            value is not None
            for value in (
                self._runtime_image_bundle_path,
                self._runtime_image_bundle_file,
                self._runtime_image_bundle_sha256,
                self._runtime_image_bundle_size,
            )
        ):
            raise DockerSbxError("image_bundle_stage", "image_bundle_already_staged")
        path: Path | None = None
        bundle: BinaryIO | None = None
        try:
            try:
                fd, raw_path = tempfile.mkstemp(prefix="repotrial-image-bundle-")
            except OSError as error:
                evidence = self._command_failure_evidence(
                    "image_bundle_stage",
                    "image_bundle_io_error",
                    deadline=self._require_deadline(sandbox_id),
                    deadline_limited=False,
                    subprocess_started=False,
                    sandbox_id=sandbox_id,
                )
                raise DockerSbxError(
                    "image_bundle_stage",
                    "image_bundle_io_error",
                    sandbox_id=sandbox_id,
                    failure_evidence=evidence,
                ) from error
            path = Path(raw_path)
            os.chmod(path, 0o600)
            bundle = os.fdopen(fd, "w+b", buffering=0)
            self._runtime_image_bundle_path = path
            self._runtime_image_bundle_file = bundle
            max_bytes = calculate_disk_allocation(self._policy.disk_mb).docker_mb * (
                1024 * 1024
            )
            digest, size = await self._stream_image_bundle_export(
                sandbox_id,
                references,
                ids,
                bundle,
                max_bytes=max_bytes,
            )
            if size == 0:
                raise DockerSbxError("image_bundle_stage", "image_bundle_empty")
            bundle.flush()
            if not _same_open_file(path, bundle):
                raise DockerSbxError("image_bundle_stage", "image_bundle_replaced")
            bundle.seek(0)
            self._runtime_image_bundle_sha256 = digest
            self._runtime_image_bundle_size = size
            audit = self._runtime_template_audit
            self._runtime_template_audit = RuntimeTemplateAudit(
                identity=audit.identity,
                uses=audit.uses,
                removal_confirmed=False,
                bundle_sha256=digest,
                bundle_size=size,
            )
        except (DockerSbxError, asyncio.CancelledError, OSError):
            await self._discard_runtime_image_bundle()
            raise

    async def _stream_image_bundle_export(
        self,
        sandbox_id: str,
        references: tuple[str, ...],
        ids: tuple[str, ...],
        bundle: BinaryIO,
        *,
        max_bytes: int,
    ) -> tuple[str, int]:
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        deadline = self._require_deadline(sandbox_id)
        operation = "image_bundle_stage"
        try:
            effective_timeout_s, deadline_limited = self._effective_timeout(
                operation,
                self._command_timeout_s,
                deadline=deadline,
                public_sandbox_id=sandbox_id,
                evidence_sandbox_id=sandbox_id,
            )
            async with asyncio.timeout(effective_timeout_s):
                process = await asyncio.create_subprocess_exec(
                    "sbx",
                    "exec",
                    sandbox_id,
                    "--",
                    "docker",
                    "image",
                    "save",
                    *references,
                    *ids,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._sandbox_command_environment(),
                )
                if process.stdout is None or process.stderr is None:
                    raise OSError("sbx pipes unavailable")
                stderr_task = asyncio.create_task(
                    _read_bounded_with_overflow(process.stderr)
                )
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = await process.stdout.read(_IMAGE_BUNDLE_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        evidence = self._command_failure_evidence(
                            operation,
                            "image_bundle_oversize",
                            deadline=deadline,
                            deadline_limited=deadline_limited,
                            subprocess_started=True,
                            sandbox_id=sandbox_id,
                            details={"max_bytes": max_bytes},
                        )
                        raise DockerSbxError(
                            operation,
                            "image_bundle_oversize",
                            sandbox_id=sandbox_id,
                            failure_evidence=evidence,
                        )
                    bundle.write(chunk)
                    digest.update(chunk)
                returncode = await process.wait()
                stderr, stderr_overflow = await stderr_task
                stderr_task = None
                if stderr_overflow:
                    evidence = self._command_failure_evidence(
                        operation,
                        "image_bundle_stderr_overflow",
                        deadline=deadline,
                        deadline_limited=deadline_limited,
                        subprocess_started=True,
                        sandbox_id=sandbox_id,
                    )
                    raise DockerSbxError(
                        operation,
                        "image_bundle_stderr_overflow",
                        sandbox_id=sandbox_id,
                        failure_evidence=evidence,
                    )
                result = _CommandResult(
                    returncode=returncode,
                    stdout=b"",
                    stderr=stderr,
                    _execution_context=_CommandExecutionContext(
                        operation=operation,
                        sandbox_id=sandbox_id,
                        deadline_limited=deadline_limited,
                        subprocess_started=True,
                        trial_elapsed_s=None,
                        trial_remaining_s=None,
                    ),
                )
                _require_success(operation, result)
                return digest.hexdigest(), size
        except TimeoutError as error:
            evidence = self._command_failure_evidence(
                operation,
                "total_duration_exhausted" if deadline_limited else "timeout",
                deadline=deadline,
                deadline_limited=deadline_limited,
                subprocess_started=process is not None,
                sandbox_id=sandbox_id,
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    evidence.reason,
                    process,
                    error,
                    sandbox_id=sandbox_id,
                    failure_evidence=evidence,
                )
            raise DockerSbxError(
                operation, evidence.reason, failure_evidence=evidence
            ) from error
        except asyncio.CancelledError as error:
            if process is not None:
                attach_sandbox_failure_evidence(
                    error,
                    self._command_failure_evidence(
                        operation,
                        "cancelled",
                        deadline=deadline,
                        deadline_limited=False,
                        subprocess_started=True,
                        sandbox_id=sandbox_id,
                    ),
                )
                await _raise_after_process_cleanup(
                    operation, "cancelled", process, error
                )
            raise
        except DockerSbxError as error:
            if process is not None:
                error_evidence = get_sandbox_failure_evidence(error)
                if error_evidence is None:
                    error_evidence = self._command_failure_evidence(
                        operation,
                        error.reason,
                        deadline=deadline,
                        deadline_limited=False,
                        subprocess_started=True,
                        sandbox_id=sandbox_id,
                    )
                await _raise_after_process_cleanup(
                    operation,
                    error.reason,
                    process,
                    error,
                    sandbox_id=sandbox_id,
                    failure_evidence=error_evidence,
                )
            raise
        except OSError as error:
            evidence = self._command_failure_evidence(
                operation,
                "io_error",
                deadline=deadline,
                deadline_limited=False,
                subprocess_started=process is not None,
                sandbox_id=sandbox_id,
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    "io_error",
                    process,
                    error,
                    sandbox_id=sandbox_id,
                    failure_evidence=evidence,
                )
            raise DockerSbxError(
                operation, "io_error", failure_evidence=evidence
            ) from error
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass

    async def activate_runtime_template(
        self, sandbox_id: str, image_identity_sha256: str
    ) -> None:
        if self._runtime_template_activation_used:
            raise RuntimeError("runtime template activation already invoked")
        self._runtime_template_activation_used = True
        self._require_active(sandbox_id)
        if (
            not isinstance(image_identity_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", image_identity_sha256) is None
        ):
            raise ValueError("image_identity_sha256 must be 64 lowercase hex digits")
        if self._runtime_template_tag is not None:
            raise RuntimeError("runtime template is already active")
        deadline = self._require_deadline(sandbox_id)
        tag = f"repotrial-runtime:{uuid.uuid4().hex}"
        before = await self._list_runtime_templates(deadline)
        if any(_runtime_template_matches(item, tag) for item in before):
            raise DockerSbxError("template_save", "template_tag_collision")

        await self._stop_runtime_sandbox(sandbox_id, deadline)
        self._runtime_template_pending_tag = tag
        save_attempted = False
        try:
            save_attempted = True
            result = await self._run(
                "template_save",
                ["template", "save", sandbox_id, tag],
                self._command_timeout_s,
                deadline=deadline,
            )
            _require_success("template_save", result)
            after = await self._list_runtime_templates(deadline)
            matches = [item for item in after if _runtime_template_matches(item, tag)]
            if len(matches) != 1:
                raise DockerSbxError("template_save", "template_identity_invalid")
            try:
                identity = RuntimeTemplateIdentity(
                    repository=matches[0].repository,
                    tag=matches[0].tag,
                    image_id=matches[0].image_id,
                    image_identity_sha256=image_identity_sha256,
                )
            except (TypeError, ValueError):
                raise DockerSbxError(
                    "template_save", "template_identity_invalid"
                ) from None
            self._runtime_template_tag = tag
            self._runtime_template_image_id = identity.image_id
            self._runtime_template_expected_identity = image_identity_sha256
            self._runtime_template_identity = identity
            self._runtime_template_audit = RuntimeTemplateAudit(
                identity=identity,
                bundle_sha256=self._runtime_image_bundle_sha256,
                bundle_size=self._runtime_image_bundle_size,
            )
        except (DockerSbxError, asyncio.CancelledError) as primary_error:
            if save_attempted:
                try:
                    await self._cleanup_runtime_template(tag)
                except (DockerSbxError, asyncio.CancelledError) as cleanup_error:
                    primary_error.add_note("runtime template cleanup unconfirmed")
                    primary_error.__context__ = cleanup_error
            raise

    async def finalize_runtime_template(self) -> None:
        cleanup_errors: list[BaseException] = []
        try:
            await self._finalize_runtime_template_only()
        except (DockerSbxError, asyncio.CancelledError) as error:
            cleanup_errors.append(error)
        try:
            await self._cleanup_runtime_image_bundle()
        except (DockerSbxError, asyncio.CancelledError) as error:
            cleanup_errors.append(error)
        if not cleanup_errors:
            return
        self._runtime_template_finalization_confirmed = False
        self._mark_runtime_cleanup_unconfirmed()
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        primary, secondary = cleanup_errors
        if isinstance(primary, asyncio.CancelledError):
            primary.add_note(f"runtime bundle cleanup failed: {secondary}")
            raise primary
        assert isinstance(primary, DockerSbxError)
        primary.add_note(f"runtime image bundle cleanup failed: {secondary}")
        raise DockerSbxError(
            primary.operation,
            "dual_cleanup_failed",
            sandbox_id=primary.sandbox_id,
            cleanup_error=f"{primary}; {secondary}",
            failure_evidence=get_sandbox_failure_evidence(primary),
        ) from primary

    async def _finalize_runtime_template_only(self) -> None:
        active_identity = self._runtime_template_identity
        audit_identity = self._runtime_template_audit.identity
        if active_identity is not None and audit_identity != active_identity:
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        identity = active_identity or audit_identity
        tag = self._runtime_template_tag or self._runtime_template_pending_tag
        if tag is None:
            if active_identity is not None or (
                audit_identity is not None
                and not self._runtime_template_audit.removal_confirmed
            ):
                raise DockerSbxError("template_finalize", "template_identity_invalid")
            self._runtime_template_audit = RuntimeTemplateAudit(
                identity=identity,
                uses=self._runtime_template_audit.uses,
                removal_confirmed=True,
                bundle_sha256=self._runtime_image_bundle_sha256,
                bundle_size=self._runtime_image_bundle_size,
            )
            self._runtime_template_finalization_confirmed = True
            return
        if (
            active_identity is None
            and audit_identity is None
            and self._runtime_template_tag is None
            and self._runtime_template_image_id is None
            and self._runtime_template_expected_identity is None
            and not self._runtime_template_audit.uses
            and _RUNTIME_TEMPLATE_TAG.fullmatch(tag) is not None
        ):
            await self._finalize_pending_runtime_template(tag)
            return
        if active_identity is None or audit_identity != active_identity:
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        if self._runtime_template_audit.removal_confirmed:
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        if identity is None or not _runtime_template_identity_matches_tag(
            identity, tag
        ):
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        if (
            self._runtime_template_image_id != identity.image_id
            or self._runtime_template_expected_identity
            != identity.image_identity_sha256
        ):
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        remaining = await self._list_runtime_templates(deadline=None)
        matches = [item for item in remaining if _runtime_template_matches(item, tag)]
        if len(matches) > 1:
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        if matches:
            if (
                matches[0].repository.strip().lower() != identity.repository
                or matches[0].tag != identity.tag
                or matches[0].image_id != identity.image_id
            ):
                raise DockerSbxError("template_finalize", "template_identity_changed")
            expected_image_id = self._runtime_template_image_id
            if (
                expected_image_id is not None
                and matches[0].image_id != expected_image_id
            ):
                raise DockerSbxError("template_finalize", "template_identity_changed")
            await self._remove_runtime_template(tag, deadline=None)
            remaining = await self._list_runtime_templates(deadline=None)
            if any(_runtime_template_matches(item, tag) for item in remaining):
                raise DockerSbxError("template_finalize", "template_still_present")
        if identity is not None:
            self._runtime_template_audit = RuntimeTemplateAudit(
                identity=identity,
                uses=self._runtime_template_audit.uses,
                removal_confirmed=True,
            )
        self._runtime_template_tag = None
        self._runtime_template_image_id = None
        self._runtime_template_expected_identity = None
        self._runtime_template_pending_tag = None
        self._runtime_template_identity = None
        self._runtime_template_finalization_confirmed = True

    async def _cleanup_runtime_image_bundle(self) -> None:
        path = self._runtime_image_bundle_path
        bundle = self._runtime_image_bundle_file
        if path is None and bundle is None:
            if (
                self._runtime_image_bundle_sha256 is not None
                or self._runtime_image_bundle_size is not None
            ):
                raise DockerSbxError(
                    "template_finalize", "image_bundle_identity_invalid"
                )
            return
        if path is None or bundle is None:
            raise DockerSbxError("template_finalize", "image_bundle_state_invalid")
        try:
            if not _same_open_file(path, bundle):
                raise DockerSbxError("template_finalize", "image_bundle_replaced")
            bundle.close()
            self._runtime_image_bundle_file = None
            path.unlink()
            if path.exists():
                raise DockerSbxError("template_finalize", "image_bundle_still_present")
        except DockerSbxError:
            raise
        except (OSError, ValueError) as error:
            raise DockerSbxError(
                "template_finalize", "image_bundle_cleanup_failed"
            ) from error
        self._runtime_image_bundle_path = None
        self._runtime_image_bundle_sha256 = None
        self._runtime_image_bundle_size = None
        audit = self._runtime_template_audit
        self._runtime_template_audit = RuntimeTemplateAudit(
            identity=audit.identity,
            uses=audit.uses,
            removal_confirmed=audit.removal_confirmed,
        )

    async def _discard_runtime_image_bundle(self) -> None:
        path = self._runtime_image_bundle_path
        bundle = self._runtime_image_bundle_file
        if bundle is not None:
            if path is None or not _same_open_file(path, bundle):
                raise DockerSbxError("image_bundle_stage", "image_bundle_replaced")
            try:
                bundle.close()
            except OSError as error:
                raise DockerSbxError(
                    "image_bundle_stage", "image_bundle_cleanup_failed"
                ) from error
        self._runtime_image_bundle_file = None
        if path is not None:
            try:
                if path.exists():
                    path.unlink()
                    if path.exists():
                        raise DockerSbxError(
                            "image_bundle_stage", "image_bundle_still_present"
                        )
            except DockerSbxError:
                raise
            except OSError as error:
                raise DockerSbxError(
                    "image_bundle_stage", "image_bundle_cleanup_failed"
                ) from error
        self._runtime_image_bundle_path = None
        self._runtime_image_bundle_sha256 = None
        self._runtime_image_bundle_size = None
        audit = self._runtime_template_audit
        self._runtime_template_audit = RuntimeTemplateAudit(
            identity=audit.identity,
            uses=audit.uses,
            removal_confirmed=audit.removal_confirmed,
        )

    def _mark_runtime_cleanup_unconfirmed(self) -> None:
        audit = self._runtime_template_audit
        self._runtime_template_audit = RuntimeTemplateAudit(
            identity=audit.identity,
            uses=audit.uses,
            removal_confirmed=False,
            bundle_sha256=self._runtime_image_bundle_sha256,
            bundle_size=self._runtime_image_bundle_size,
        )

    async def _import_runtime_image_bundle(
        self, sandbox_id: str, deadline: float
    ) -> None:
        path = self._runtime_image_bundle_path
        bundle = self._runtime_image_bundle_file
        expected_hash = self._runtime_image_bundle_sha256
        expected_size = self._runtime_image_bundle_size
        if (
            path is None
            or bundle is None
            or expected_hash is None
            or expected_size is None
        ):
            raise DockerSbxError("image_bundle_import", "image_bundle_state_invalid")
        if not _same_open_file(path, bundle):
            raise DockerSbxError("image_bundle_import", "image_bundle_replaced")
        try:
            bundle.seek(0)
        except (OSError, ValueError) as error:
            raise DockerSbxError(
                "image_bundle_import", "image_bundle_io_error"
            ) from error
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        stdout_task: asyncio.Task[None] | None = None
        operation = "image_bundle_import"
        try:
            effective_timeout_s, deadline_limited = self._effective_timeout(
                operation,
                self._command_timeout_s,
                deadline=deadline,
                public_sandbox_id=sandbox_id,
                evidence_sandbox_id=sandbox_id,
            )
            async with asyncio.timeout(effective_timeout_s):
                process = await asyncio.create_subprocess_exec(
                    "sbx",
                    "exec",
                    sandbox_id,
                    "--",
                    "docker",
                    "image",
                    "load",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._sandbox_command_environment(),
                )
                if (
                    process.stdin is None
                    or process.stdout is None
                    or process.stderr is None
                ):
                    raise OSError("sbx pipes unavailable")
                stderr_task = asyncio.create_task(
                    _read_bounded_with_overflow(process.stderr)
                )
                stdout_task = asyncio.create_task(_discard_stream(process.stdout))
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = bundle.read(_IMAGE_BUNDLE_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size:
                        raise DockerSbxError(operation, "image_bundle_changed")
                    digest.update(chunk)
                    process.stdin.write(chunk)
                    await process.stdin.drain()
                process.stdin.close()
                wait_closed = getattr(process.stdin, "wait_closed", None)
                if wait_closed is not None:
                    await wait_closed()
                returncode = await process.wait()
                stderr, stderr_overflow = await stderr_task
                stderr_task = None
                if stdout_task is not None:
                    await stdout_task
                    stdout_task = None
                if stderr_overflow:
                    raise DockerSbxError(operation, "image_bundle_stderr_overflow")
                if size != expected_size or digest.hexdigest() != expected_hash:
                    raise DockerSbxError(operation, "image_bundle_changed")
                result = _CommandResult(
                    returncode=returncode,
                    stdout=b"",
                    stderr=stderr,
                    _execution_context=_CommandExecutionContext(
                        operation=operation,
                        sandbox_id=sandbox_id,
                        deadline_limited=deadline_limited,
                        subprocess_started=True,
                        trial_elapsed_s=None,
                        trial_remaining_s=None,
                    ),
                )
                _require_success(operation, result)
        except TimeoutError as error:
            evidence = self._command_failure_evidence(
                operation,
                "total_duration_exhausted" if deadline_limited else "timeout",
                deadline=deadline,
                deadline_limited=deadline_limited,
                subprocess_started=process is not None,
                sandbox_id=sandbox_id,
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    evidence.reason,
                    process,
                    error,
                    sandbox_id=sandbox_id,
                    failure_evidence=evidence,
                )
            raise DockerSbxError(
                operation, evidence.reason, failure_evidence=evidence
            ) from error
        except asyncio.CancelledError as error:
            if process is not None:
                attach_sandbox_failure_evidence(
                    error,
                    self._command_failure_evidence(
                        operation,
                        "cancelled",
                        deadline=deadline,
                        deadline_limited=False,
                        subprocess_started=True,
                        sandbox_id=sandbox_id,
                    ),
                )
                await _raise_after_process_cleanup(
                    operation, "cancelled", process, error
                )
            raise
        except DockerSbxError as error:
            if process is not None:
                error_evidence = get_sandbox_failure_evidence(error)
                if error_evidence is None:
                    error_evidence = self._command_failure_evidence(
                        operation,
                        error.reason,
                        deadline=deadline,
                        deadline_limited=False,
                        subprocess_started=True,
                        sandbox_id=sandbox_id,
                    )
                await _raise_after_process_cleanup(
                    operation,
                    error.reason,
                    process,
                    error,
                    sandbox_id=sandbox_id,
                    failure_evidence=error_evidence,
                )
            raise
        except (OSError, ValueError) as error:
            evidence = self._command_failure_evidence(
                operation,
                "image_bundle_io_error",
                deadline=deadline,
                deadline_limited=False,
                subprocess_started=process is not None,
                sandbox_id=sandbox_id,
            )
            if process is not None:
                await _raise_after_process_cleanup(
                    operation,
                    "image_bundle_io_error",
                    process,
                    error,
                    sandbox_id=sandbox_id,
                    failure_evidence=evidence,
                )
            raise DockerSbxError(
                operation, "image_bundle_io_error", failure_evidence=evidence
            ) from error
        finally:
            for task in (stderr_task, stdout_task):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def _finalize_pending_runtime_template(self, tag: str) -> None:
        remaining = await self._list_runtime_templates(deadline=None)
        matches = [item for item in remaining if _runtime_template_matches(item, tag)]
        if len(matches) > 1:
            raise DockerSbxError("template_finalize", "template_identity_invalid")
        if matches:
            await self._remove_runtime_template(tag, deadline=None)
        remaining = await self._list_runtime_templates(deadline=None)
        if any(_runtime_template_matches(item, tag) for item in remaining):
            raise DockerSbxError("template_finalize", "template_still_present")
        self._runtime_template_pending_tag = None
        self._runtime_template_audit = RuntimeTemplateAudit(removal_confirmed=True)
        self._runtime_template_finalization_confirmed = True

    async def _cleanup_runtime_template(self, tag: str) -> None:
        await self._remove_runtime_template(tag, deadline=None)
        remaining = await self._list_runtime_templates(deadline=None)
        if any(_runtime_template_matches(item, tag) for item in remaining):
            raise DockerSbxError("template_cleanup", "template_still_present")
        if self._runtime_template_pending_tag == tag:
            self._runtime_template_pending_tag = None

    async def _remove_runtime_template(
        self, reference: str, *, deadline: float | None
    ) -> None:
        result = await self._run(
            "template_rm",
            ["template", "rm", reference],
            self._command_timeout_s,
            deadline=deadline,
        )
        _require_success("template_rm", result)

    async def _list_runtime_templates(
        self, deadline: float | None
    ) -> tuple[_RuntimeTemplate, ...]:
        result = await self._run(
            "template_ls",
            ["template", "ls", "--json"],
            self._command_timeout_s,
            deadline=deadline,
        )
        _require_success("template_ls", result)
        return _parse_runtime_templates(result.stdout)

    async def _stop_runtime_sandbox(self, sandbox_id: str, deadline: float) -> None:
        self._stopped_sandboxes.add(sandbox_id)
        result = await self._run(
            "stop",
            ["stop", sandbox_id],
            self._command_timeout_s,
            deadline=deadline,
            sandbox_id=sandbox_id,
            public_sandbox_id=sandbox_id,
        )
        _require_success("stop", result)

    async def create(self, workspace: Path, name: str) -> str:
        template_tag = self._runtime_template_tag
        template_identity = self._runtime_template_identity
        audit_identity = self._runtime_template_audit.identity
        if (
            any(
                value is not None
                for value in (
                    template_tag,
                    self._runtime_template_image_id,
                    self._runtime_template_expected_identity,
                    self._runtime_template_pending_tag,
                    template_identity,
                )
            )
            or (
                audit_identity is not None
                and not self._runtime_template_audit.removal_confirmed
            )
        ) and (
            template_identity is None
            or template_tag is None
            or not _runtime_template_identity_matches_tag(
                template_identity, template_tag
            )
            or self._runtime_template_expected_identity
            != template_identity.image_identity_sha256
            or self._runtime_template_audit.identity != template_identity
            or self._runtime_template_audit.removal_confirmed
            or len(self._runtime_template_audit.uses) >= 128
        ):
            raise DockerSbxError("create", "template_identity_invalid")
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
        try:
            docker_config = Path(tempfile.mkdtemp(prefix="repotrial-docker-config-"))
        except OSError as error:
            raise DockerSbxError(
                "create",
                "anonymous_config_io",
                failure_evidence=self._command_failure_evidence(
                    "create",
                    "anonymous_config_io",
                    deadline=deadline,
                    deadline_limited=False,
                    subprocess_started=False,
                    sandbox_id=None,
                ),
            ) from error
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
        if template_tag is not None:
            arguments.extend(("--template", template_tag))
        arguments.extend(("shell", str(resolved_workspace)))
        try:
            create_error: DockerSbxError | asyncio.CancelledError | None = None
            try:
                create_environment = self._disk_environment(allocation)
                create_environment.pop("DOCKER_AUTH_CONFIG", None)
                create_environment.pop("REGISTRY_AUTH_FILE", None)
                create_environment["DOCKER_CONFIG"] = str(docker_config)
                result = await self._run(
                    "create",
                    arguments,
                    self._command_timeout_s,
                    env=create_environment,
                    deadline=deadline,
                    sandbox_id=sandbox_id,
                )
                _require_success("create", result)
            except (DockerSbxError, asyncio.CancelledError) as error:
                create_error = error
                raise
            finally:
                if create_error is not None and _process_cleanup_unconfirmed(
                    create_error
                ):
                    create_error.add_note(_ANONYMOUS_CONFIG_RETAINED_NOTE)
                else:
                    try:
                        shutil.rmtree(docker_config)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        if create_error is not None:
                            create_error.add_note(_ANONYMOUS_CONFIG_CLEANUP_FAILED_NOTE)
                        else:
                            raise DockerSbxError(
                                "create",
                                "anonymous_config_cleanup",
                                sandbox_id=sandbox_id,
                                failure_evidence=self._command_failure_evidence(
                                    "create",
                                    "anonymous_config_cleanup",
                                    deadline=deadline,
                                    deadline_limited=False,
                                    subprocess_started=True,
                                    sandbox_id=sandbox_id,
                                ),
                            ) from error
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
            if (
                template_identity is not None
                and self._runtime_image_bundle_path is not None
            ):
                await self._import_runtime_image_bundle(sandbox_id, deadline)
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
        if template_identity is not None:
            self._runtime_template_audit = self._runtime_template_audit.with_use(
                sandbox_id
            )
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

    def _sandbox_command_environment(self) -> dict[str, str]:
        environment = self._subprocess_environment.copy()
        for variable in ("DOCKER_AUTH_CONFIG", "REGISTRY_AUTH_FILE"):
            environment.pop(variable, None)
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
        if sandbox_id in self._stopped_sandboxes:
            raise RuntimeError(f"sandbox is stopped: {sandbox_id}")
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
        if result.returncode != 0 and not _is_exact_sandbox_absence(result, sandbox_id):
            _require_success(operation, result)
        self._sandbox_states[sandbox_id] = _SandboxState.CLEANED
        self._sandbox_deadlines.pop(sandbox_id, None)
        self._network_log_sandboxes.discard(sandbox_id)
        self._stopped_sandboxes.discard(sandbox_id)

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
            ("create", ["create", "--help"], (*_CREATE_FLAGS, "--template")),
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
            (
                "template_save",
                ["template", "save", "--help"],
                ("SANDBOX", "TAG"),
            ),
            ("template_ls", ["template", "ls", "--help"], ("--json",)),
            ("template_rm", ["template", "rm", "--help"], ("TAG|ID",)),
            ("stop", ["stop", "--help"], ("SANDBOX",)),
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
    result, _overflow = await _read_bounded_with_overflow(stream)
    return result


async def _read_bounded_with_overflow(
    stream: asyncio.StreamReader,
) -> tuple[bytes, bool]:
    payload_limit = MAX_OUTPUT_BYTES - len(_TRUNCATION_MARKER)
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    head = bytearray()
    tail = bytearray()
    truncated = False
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
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
        return bytes(head) + _TRUNCATION_MARKER + bytes(tail), total > MAX_OUTPUT_BYTES
    return bytes(head) + bytes(tail), total > MAX_OUTPUT_BYTES


async def _discard_stream(stream: asyncio.StreamReader) -> None:
    while await stream.read(_IMAGE_BUNDLE_CHUNK_BYTES):
        pass


def _same_open_file(path: Path, file: BinaryIO) -> bool:
    try:
        path_stat = path.stat()
        fd_stat = os.fstat(file.fileno())
    except (OSError, ValueError):
        return False
    return (
        stat.S_ISREG(path_stat.st_mode)
        and stat.S_ISREG(fd_stat.st_mode)
        and path_stat.st_dev == fd_stat.st_dev
        and path_stat.st_ino == fd_stat.st_ino
    )


def _validate_image_bundle_inputs(
    references: tuple[str, ...], ids: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(references, tuple) or not isinstance(ids, tuple):
        raise DockerSbxError("image_bundle_stage", "image_bundle_identity_invalid")
    if not references or not ids:
        raise DockerSbxError("image_bundle_stage", "image_bundle_empty")
    normalized_references: list[str] = []
    for reference in references:
        if not isinstance(reference, str):
            raise DockerSbxError("image_bundle_stage", "image_bundle_reference_invalid")
        normalized = reference.strip()
        if (
            normalized != reference
            or _IMAGE_BUNDLE_REFERENCE.fullmatch(normalized) is None
            or normalized.startswith("-")
        ):
            raise DockerSbxError("image_bundle_stage", "image_bundle_reference_invalid")
        normalized_references.append(normalized)
    normalized_ids: list[str] = []
    for image_id in ids:
        if (
            not isinstance(image_id, str)
            or _IMAGE_BUNDLE_ID.fullmatch(image_id) is None
        ):
            raise DockerSbxError("image_bundle_stage", "image_bundle_id_invalid")
        normalized_ids.append(image_id)
    references_tuple = tuple(normalized_references)
    ids_tuple = tuple(normalized_ids)
    if references_tuple != tuple(sorted(references_tuple)):
        raise DockerSbxError("image_bundle_stage", "image_bundle_references_unsorted")
    if len(set(references_tuple)) != len(references_tuple):
        raise DockerSbxError("image_bundle_stage", "image_bundle_references_duplicate")
    if ids_tuple != tuple(sorted(ids_tuple)):
        raise DockerSbxError("image_bundle_stage", "image_bundle_ids_unsorted")
    if len(set(ids_tuple)) != len(ids_tuple):
        raise DockerSbxError("image_bundle_stage", "image_bundle_ids_duplicate")
    return references_tuple, ids_tuple


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


def _is_exact_sandbox_absence(result: _CommandResult, sandbox_id: str) -> bool:
    if result.stdout != b"":
        return False
    exact_error = (
        f"Error: sandbox \x27{sandbox_id}\x27 not found "
        "(run \x27sbx ls\x27 to see your sandboxes)"
    ).encode()
    warning = (
        b"WARN: could not acquire docker hub refresh lock, proceeding without "
        b"cross-process lock: context deadline exceeded"
    )
    allowed_stderr = (
        exact_error,
        exact_error + b"\n",
        warning + b"\n" + exact_error,
        warning + b"\n" + exact_error + b"\n",
    )
    return result.stderr in allowed_stderr


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


def _process_cleanup_unconfirmed(error: BaseException) -> bool:
    if isinstance(error, DockerSbxError):
        return error.reason == "process_cleanup_unconfirmed"
    return any(
        note.startswith(_PROCESS_CLEANUP_UNCONFIRMED_NOTE_PREFIX)
        for note in getattr(error, "__notes__", ())
    )


def _parse_runtime_templates(output: bytes) -> tuple[_RuntimeTemplate, ...]:
    try:
        text = output.decode("utf-8")
        decoded: Any = json.loads(
            text,
            object_pairs_hook=_strict_json_object_from_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKeyError):
        raise DockerSbxError("template_ls", "template_list_invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != {"images"}:
        raise DockerSbxError("template_ls", "template_list_invalid")
    images = decoded["images"]
    if not isinstance(images, list) or len(images) > 128:
        raise DockerSbxError("template_ls", "template_list_invalid")
    templates: list[_RuntimeTemplate] = []
    for item in images:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "repository",
            "tag",
            "flavor",
            "created_at",
            "size",
        }:
            raise DockerSbxError("template_ls", "template_list_invalid")
        image_id = item["id"]
        repository = item["repository"]
        tag = item["tag"]
        flavor = item["flavor"]
        created_at = item["created_at"]
        size = item["size"]
        try:
            normalized_repository = _normalize_runtime_template_repository(repository)
            normalized_tag = tag.strip() if isinstance(tag, str) else tag
        except (TypeError, ValueError):
            raise DockerSbxError("template_ls", "template_list_invalid") from None
        if (
            not isinstance(image_id, str)
            or _IMAGE_ID.fullmatch(image_id) is None
            or not isinstance(tag, str)
            or not isinstance(normalized_tag, str)
            or not normalized_tag
            or any(
                ord(character) < 32 or ord(character) == 127 or character.isspace()
                for character in normalized_tag
            )
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z", normalized_tag)
            is None
            or len(normalized_tag.encode("utf-8")) > 128
            or not isinstance(flavor, str)
            or len(flavor.encode("utf-8")) > 128
            or not isinstance(created_at, str)
            or created_at == ""
            or len(created_at.encode("utf-8")) > 128
            or type(size) is not int
            or size < 0
            or size > 2**63 - 1
        ):
            raise DockerSbxError("template_ls", "template_list_invalid")
        templates.append(
            _RuntimeTemplate(
                repository=normalized_repository,
                tag=normalized_tag,
                image_id=image_id,
            )
        )
    return tuple(templates)


def _runtime_template_matches(template: _RuntimeTemplate, tag: str) -> bool:
    if _RUNTIME_TEMPLATE_TAG.fullmatch(tag) is None:
        return False
    return (
        template.repository == _RUNTIME_TEMPLATE_REPOSITORY
        and template.tag == tag.split(":", 1)[1]
    )


def _runtime_template_identity_matches_tag(
    identity: RuntimeTemplateIdentity, tag: str
) -> bool:
    if _RUNTIME_TEMPLATE_TAG.fullmatch(tag) is None:
        return False
    return (
        identity.repository == _RUNTIME_TEMPLATE_REPOSITORY
        and identity.tag == tag.split(":", 1)[1]
    )


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
