import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class NetworkLogResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    supported: bool
    unsupported_reason: str | None = None


type FailureEvidenceScalar = str | int | float | bool | None
type FailureEvidenceRecord = dict[str, FailureEvidenceScalar]
type FailureEvidenceValue = (
    FailureEvidenceScalar
    | list[FailureEvidenceScalar]
    | FailureEvidenceRecord
    | list[FailureEvidenceRecord]
)


_MAX_FAILURE_TOKEN_BYTES = 64
_MAX_FAILURE_DETAIL_KEYS = 16
_MAX_FAILURE_LIST_ITEMS = 32
_MAX_FAILURE_VALUE_BYTES = 512
_MAX_FAILURE_JSON_BYTES = 16_384
_MAX_FAILURE_CAUSE_DEPTH = 8


@dataclass(frozen=True, slots=True)
class SandboxFailureEvidence:
    operation: str
    reason: str
    returncode: int | None = None
    sandbox_id: str | None = None
    trial_elapsed_s: float | None = None
    trial_remaining_s: float | None = None
    deadline_limited: bool | None = None
    subprocess_started: bool | None = None
    details: dict[str, FailureEvidenceValue] = field(default_factory=dict)


class _SandboxFailureEvidenceCarrier(Protocol):
    sandbox_failure_evidence: SandboxFailureEvidence


def attach_sandbox_failure_evidence(
    failure: BaseException,
    evidence: SandboxFailureEvidence,
) -> SandboxFailureEvidence:
    carrier = cast(_SandboxFailureEvidenceCarrier, failure)
    carrier.sandbox_failure_evidence = evidence
    return evidence


def get_sandbox_failure_evidence(
    failure: BaseException,
) -> SandboxFailureEvidence | None:
    evidence = getattr(failure, "sandbox_failure_evidence", None)
    return evidence if isinstance(evidence, SandboxFailureEvidence) else None


def find_sandbox_failure_evidence(
    failure: BaseException,
    *,
    max_depth: int = 8,
) -> SandboxFailureEvidence | None:
    current: BaseException | None = failure
    seen: set[int] = set()
    for _depth in range(min(max_depth, _MAX_FAILURE_CAUSE_DEPTH)):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        evidence = get_sandbox_failure_evidence(current)
        if evidence is not None:
            return evidence
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return None


def serialize_sandbox_failure_evidence(
    evidence: SandboxFailureEvidence,
) -> dict[str, object]:
    def bounded_text(value: str, limit: int) -> str:
        if len(value.encode("utf-8")) > limit:
            raise ValueError("failure evidence text exceeds limit")
        return value

    def list_shape_error() -> ValueError:
        return ValueError("failure evidence list shape is invalid")

    def scalar(value: FailureEvidenceScalar) -> FailureEvidenceScalar:
        normalized: FailureEvidenceScalar
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("failure evidence float must be finite")
            normalized = round(value, 6)
        elif isinstance(value, str):
            return bounded_text(value, _MAX_FAILURE_VALUE_BYTES)
        elif value is None or type(value) in {bool, int}:
            normalized = value
        else:
            raise ValueError("failure evidence scalar is invalid")
        try:
            encoded = json.dumps(
                normalized, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("failure evidence scalar is invalid") from error
        if len(encoded) > _MAX_FAILURE_VALUE_BYTES:
            raise ValueError("failure evidence scalar exceeds limit")
        return normalized

    if len(evidence.details) > _MAX_FAILURE_DETAIL_KEYS:
        raise ValueError("failure evidence has too many details")
    details: dict[str, object] = {}
    for key, value in evidence.details.items():
        safe_key = bounded_text(key, _MAX_FAILURE_TOKEN_BYTES)
        if isinstance(value, list):
            if len(value) > _MAX_FAILURE_LIST_ITEMS:
                raise ValueError("failure evidence list exceeds limit")
            records_expected = bool(value) and isinstance(value[0], dict)
            if records_expected:
                records: list[dict[str, FailureEvidenceScalar]] = []
                for item in value:
                    if not isinstance(item, dict):
                        raise list_shape_error()
                    if len(item) > _MAX_FAILURE_DETAIL_KEYS:
                        raise ValueError("failure evidence record exceeds limit")
                    records.append(
                        {
                            bounded_text(item_key, _MAX_FAILURE_TOKEN_BYTES): scalar(
                                item_value
                            )
                            for item_key, item_value in item.items()
                        }
                    )
                details[safe_key] = records
            else:
                scalars: list[FailureEvidenceScalar] = []
                for item in value:
                    if isinstance(item, dict):
                        raise list_shape_error()
                    scalars.append(scalar(item))
                details[safe_key] = scalars
        elif isinstance(value, dict):
            if len(value) > _MAX_FAILURE_DETAIL_KEYS:
                raise ValueError("failure evidence mapping exceeds limit")
            details[safe_key] = {
                bounded_text(item_key, _MAX_FAILURE_TOKEN_BYTES): scalar(item_value)
                for item_key, item_value in value.items()
            }
        else:
            details[safe_key] = scalar(value)

    projected: dict[str, object] = {
        "operation": bounded_text(evidence.operation, _MAX_FAILURE_TOKEN_BYTES),
        "reason": bounded_text(evidence.reason, _MAX_FAILURE_TOKEN_BYTES),
    }
    for key in (
        "returncode",
        "sandbox_id",
        "trial_elapsed_s",
        "trial_remaining_s",
        "deadline_limited",
        "subprocess_started",
    ):
        value = getattr(evidence, key)
        if value is not None:
            projected[key] = scalar(value)
    if details:
        projected["details"] = details
    encoded = json.dumps(
        projected, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(encoded) > _MAX_FAILURE_JSON_BYTES:
        raise ValueError("failure evidence JSON exceeds limit")
    return projected


@dataclass(frozen=True, slots=True)
class PartialCreateCleanupContext:
    """Owned sandbox identity and failures from an unconfirmed create cleanup."""

    sandbox_id: str
    create_failure: BaseException
    cleanup_failure: BaseException


class _PartialCreateCleanupCarrier(Protocol):
    partial_create_cleanup: PartialCreateCleanupContext


def attach_partial_create_cleanup_context(
    create_failure: BaseException,
    sandbox_id: str,
    cleanup_failure: BaseException,
) -> PartialCreateCleanupContext:
    context = PartialCreateCleanupContext(
        sandbox_id=sandbox_id,
        create_failure=create_failure,
        cleanup_failure=cleanup_failure,
    )
    carrier = cast(_PartialCreateCleanupCarrier, create_failure)
    carrier.partial_create_cleanup = context
    return context


def get_partial_create_cleanup_context(
    create_failure: BaseException,
) -> PartialCreateCleanupContext | None:
    context = getattr(create_failure, "partial_create_cleanup", None)
    if (
        isinstance(context, PartialCreateCleanupContext)
        and context.create_failure is create_failure
    ):
        return context
    return None


_RUNTIME_TEMPLATE_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z"
)
_RUNTIME_TEMPLATE_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_RUNTIME_TEMPLATE_IMAGE_ID_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
_RUNTIME_TEMPLATE_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RUNTIME_TEMPLATE_SANDBOX_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z"
)
_MAX_RUNTIME_TEMPLATE_USES = 128


def _runtime_template_text(
    value: object,
    *,
    field: str,
    pattern: re.Pattern[str],
    max_bytes: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"runtime template {field} must be a string")
    normalized = value.strip()
    if not normalized or any(
        ord(character) < 32 or ord(character) == 127 or character.isspace()
        for character in normalized
    ):
        raise ValueError(f"runtime template {field} is invalid")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"runtime template {field} is too large")
    if pattern.fullmatch(normalized) is None:
        raise ValueError(f"runtime template {field} is invalid")
    return normalized


def _normalize_runtime_template_repository(value: object) -> str:
    repository = _runtime_template_text(
        value,
        field="repository",
        pattern=_RUNTIME_TEMPLATE_REPOSITORY_PATTERN,
        max_bytes=512,
    ).lower()
    parts = repository.split("/")
    if any(not part for part in parts):
        raise ValueError("runtime template repository is invalid")
    if parts[0] in {"docker.io", "index.docker.io"}:
        parts[0] = "docker.io"
        if len(parts) == 2:
            parts.insert(1, "library")
    elif len(parts) == 1:
        parts = ["docker.io", "library", parts[0]]
    elif "." not in parts[0] and ":" not in parts[0] and parts[0] != "localhost":
        parts.insert(0, "docker.io")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class RuntimeTemplateIdentity:
    """The bounded provider identity of one owned SBX runtime template."""

    repository: str
    tag: str
    image_id: str
    image_identity_sha256: str

    def __post_init__(self) -> None:
        repository = _normalize_runtime_template_repository(self.repository)
        tag = _runtime_template_text(
            self.tag,
            field="tag",
            pattern=_RUNTIME_TEMPLATE_TAG_PATTERN,
            max_bytes=128,
        )
        image_id = _runtime_template_text(
            self.image_id,
            field="image_id",
            pattern=_RUNTIME_TEMPLATE_IMAGE_ID_PATTERN,
            max_bytes=12,
        )
        image_identity_sha256 = _runtime_template_text(
            self.image_identity_sha256,
            field="image_identity_sha256",
            pattern=_RUNTIME_TEMPLATE_HASH_PATTERN,
            max_bytes=64,
        )
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "tag", tag)
        object.__setattr__(self, "image_id", image_id)
        object.__setattr__(self, "image_identity_sha256", image_identity_sha256)

    def as_public_record(self) -> dict[str, str]:
        """Return only bounded, non-sensitive identity fields."""

        return {
            "repository": self.repository,
            "tag": self.tag,
            "image_id": self.image_id,
            "image_identity_sha256": self.image_identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class RuntimeTemplateUse:
    """One successful sandbox create that consumed a runtime template."""

    sandbox_id: str
    identity: RuntimeTemplateIdentity

    def __post_init__(self) -> None:
        sandbox_id = _runtime_template_text(
            self.sandbox_id,
            field="sandbox_id",
            pattern=_RUNTIME_TEMPLATE_SANDBOX_ID_PATTERN,
            max_bytes=128,
        )
        if not isinstance(self.identity, RuntimeTemplateIdentity):
            raise TypeError("runtime template use identity is invalid")
        object.__setattr__(self, "sandbox_id", sandbox_id)

    def as_public_record(self) -> dict[str, str]:
        return {
            "sandbox_id": self.sandbox_id,
            **self.identity.as_public_record(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeTemplateAudit:
    """Immutable bounded audit state for one provider invocation."""

    identity: RuntimeTemplateIdentity | None = None
    uses: tuple[RuntimeTemplateUse, ...] = ()
    removal_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.identity is not None and not isinstance(
            self.identity, RuntimeTemplateIdentity
        ):
            raise TypeError("runtime template audit identity is invalid")
        if type(self.uses) is not tuple:
            raise TypeError("runtime template audit uses must be a tuple")
        if len(self.uses) > _MAX_RUNTIME_TEMPLATE_USES:
            raise ValueError("runtime template audit uses exceed the maximum size")
        for use in self.uses:
            if not isinstance(use, RuntimeTemplateUse):
                raise TypeError("runtime template audit use is invalid")
            if self.identity is None or use.identity != self.identity:
                raise ValueError("runtime template audit use identity mismatch")
        if type(self.removal_confirmed) is not bool:
            raise TypeError("runtime template audit removal_confirmed is invalid")

    def with_use(self, sandbox_id: str) -> "RuntimeTemplateAudit":
        """Return a new audit containing one successful template create."""

        if self.identity is None:
            raise ValueError("runtime template use has no identity")
        if self.removal_confirmed:
            raise ValueError("runtime template was already finalized")
        if len(self.uses) >= _MAX_RUNTIME_TEMPLATE_USES:
            raise ValueError("runtime template audit uses exceed the maximum size")
        return RuntimeTemplateAudit(
            identity=self.identity,
            uses=(*self.uses, RuntimeTemplateUse(sandbox_id, self.identity)),
            removal_confirmed=False,
        )

    def as_public_record(self) -> dict[str, object]:
        """Return bounded identity/use/removal fields for graph evidence."""

        return {
            "template_identity": (
                None if self.identity is None else self.identity.as_public_record()
            ),
            "template_uses": [use.as_public_record() for use in self.uses],
            "removal_confirmed": self.removal_confirmed,
        }


class SandboxProvider(ABC):
    @abstractmethod
    async def create(self, workspace: Path, name: str) -> str: ...

    @abstractmethod
    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult: ...

    @abstractmethod
    async def publish_port(self, sandbox_id: str, container_port: int) -> int: ...

    @abstractmethod
    async def copy(
        self, sandbox_id: str, remote_path: str, local_path: Path
    ) -> None: ...

    @abstractmethod
    async def network_log(self, sandbox_id: str) -> NetworkLogResult: ...

    @abstractmethod
    async def destroy(self, sandbox_id: str) -> None:
        """Retry-safe cleanup for owned IDs; unknown or unowned IDs fail closed."""
        ...

    @property
    def supports_runtime_templates(self) -> bool:
        return False

    async def activate_runtime_template(
        self, sandbox_id: str, image_identity_sha256: str
    ) -> None:
        del sandbox_id, image_identity_sha256
        raise RuntimeError("runtime templates are unsupported")

    def expected_image_identity_sha256(self) -> str | None:
        return None

    def runtime_template_audit(self) -> RuntimeTemplateAudit:
        """Return the immutable audit snapshot for this invocation."""

        if self.supports_runtime_templates:
            raise RuntimeError("runtime template audit is unsupported")
        return RuntimeTemplateAudit(removal_confirmed=True)

    async def begin_invocation(self) -> None:
        """Mark the start of one graph invocation for provider-owned state."""
        if self.supports_runtime_templates:
            raise RuntimeError("runtime template invocation is unsupported")

    async def finalize_runtime_template(self) -> None:
        if self.supports_runtime_templates:
            raise RuntimeError("runtime template finalization is unsupported")
