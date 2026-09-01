import json
import math
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
    for _depth in range(max_depth):
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
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("failure evidence float must be finite")
            return round(value, 6)
        if isinstance(value, str):
            return bounded_text(value, _MAX_FAILURE_VALUE_BYTES)
        if value is None or type(value) in {bool, int}:
            return value
        raise ValueError("failure evidence scalar is invalid")

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
