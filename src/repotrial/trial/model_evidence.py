import hashlib
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

type ModelAttemptOutcome = Literal[
    "success",
    "planner_timeout",
    "transport_error",
    "http_error",
    "response_too_large",
    "structured_response_invalid",
    "policy_rejected",
    "adapter_error",
]
type ModelAttemptPurpose = Literal["journey", "recovery"]

_SCHEMA_VERSION = 1
_MAX_ATTEMPT_SLOTS = 9_999
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_PURPOSE_PREFIXES: dict[ModelAttemptPurpose, str] = {
    "journey": "baseline-model-attempt",
    "recovery": "recovery-model-attempt",
}
_FAILURE_OUTCOMES: frozenset[str] = frozenset(
    {
        "planner_timeout",
        "transport_error",
        "http_error",
        "response_too_large",
        "structured_response_invalid",
        "policy_rejected",
        "adapter_error",
    }
)


class ModelAttemptEvidenceError(RuntimeError):
    """A private model-attempt artifact could not be safely persisted."""


class ModelAttemptRecorder:
    """Append bounded metadata for one model request without raw model data."""

    def __init__(
        self,
        evidence_dir: Path,
        *,
        purpose: ModelAttemptPurpose,
        system: str,
        user: str,
        schema: type[BaseModel],
    ) -> None:
        if purpose not in _PURPOSE_PREFIXES:
            raise ValueError("invalid model attempt purpose")
        if not isinstance(system, str) or not isinstance(user, str):
            raise TypeError("model inputs must be strings")
        schema_bytes = _canonical_schema_bytes(schema)
        self._started_at = time.monotonic()
        self._finished = False
        self._purpose = purpose
        self._path, self._identity = _claim_attempt_slot(
            evidence_dir,
            _PURPOSE_PREFIXES[purpose],
            _serialize(
                {
                    "elapsed_s": 0.0,
                    "phase": "start",
                    "purpose": purpose,
                    "schema_bytes": len(schema_bytes),
                    "schema_sha256": _sha256(schema_bytes),
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": 1,
                    "system_bytes": len(system.encode("utf-8")),
                    "system_sha256": _sha256(system.encode("utf-8")),
                    "user_bytes": len(user.encode("utf-8")),
                    "user_sha256": _sha256(user.encode("utf-8")),
                }
            ),
        )

    @property
    def path(self) -> Path:
        return self._path

    def finish_success(
        self, accepted_output: object, *, journey_count: int | None
    ) -> None:
        if journey_count is not None and (
            type(journey_count) is not int or journey_count < 0
        ):
            raise ValueError("journey_count must be a non-negative integer or None")
        output = _canonical_json_bytes(accepted_output)
        self._finish(
            "success",
            accepted_output_sha256=_sha256(output),
            journey_count=journey_count,
        )

    def finish_failure(self, outcome: ModelAttemptOutcome) -> None:
        if outcome not in _FAILURE_OUTCOMES:
            raise ValueError("invalid model attempt outcome")
        self._finish(outcome, accepted_output_sha256=None, journey_count=None)

    def _finish(
        self,
        outcome: ModelAttemptOutcome,
        *,
        accepted_output_sha256: str | None,
        journey_count: int | None,
    ) -> None:
        if self._finished:
            raise ModelAttemptEvidenceError("model attempt is already finalized")
        elapsed_s = time.monotonic() - self._started_at
        if not math.isfinite(elapsed_s) or elapsed_s < 0:
            raise ModelAttemptEvidenceError("model attempt clock is invalid")
        _append_existing_file(
            self._path,
            self._identity,
            _serialize(
                {
                    "accepted_output_sha256": accepted_output_sha256,
                    "elapsed_s": elapsed_s,
                    "journey_count": journey_count,
                    "outcome": outcome,
                    "phase": "terminal",
                    "purpose": self._purpose,
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": 2,
                }
            ),
        )
        self._finished = True


def _canonical_schema_bytes(schema: type[BaseModel]) -> bytes:
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise TypeError("schema must be a Pydantic model type")
    try:
        return _canonical_json_bytes(schema.model_json_schema())
    except (TypeError, ValueError):
        raise ModelAttemptEvidenceError("model schema cannot be serialized") from None


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ModelAttemptEvidenceError("model evidence cannot be serialized") from None


def _serialize(event: dict[str, object]) -> bytes:
    return _canonical_json_bytes(event) + b"\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _claim_attempt_slot(
    evidence_dir: Path, prefix: str, payload: bytes
) -> tuple[Path, tuple[int, int]]:
    if not isinstance(evidence_dir, Path):
        raise TypeError("model evidence directory must be a Path")
    _validate_parent(evidence_dir)
    for slot in range(1, _MAX_ATTEMPT_SLOTS + 1):
        path = evidence_dir / f"{prefix}-{slot:04d}.jsonl"
        claimed = _try_create_new_file(path, payload)
        if claimed is not None:
            return path, claimed
    raise ModelAttemptEvidenceError("model evidence attempt slots are exhausted")


def _try_create_new_file(path: Path, payload: bytes) -> tuple[int, int] | None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    except OSError as error:
        raise ModelAttemptEvidenceError("could not create model evidence") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ModelAttemptEvidenceError(
                "model evidence target is not a real regular file"
            )
        _write_all(descriptor, payload, "create")
        os.fsync(descriptor)
        return opened.st_dev, opened.st_ino
    except ModelAttemptEvidenceError:
        raise
    except OSError as error:
        raise ModelAttemptEvidenceError("could not create model evidence") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _append_existing_file(
    path: Path, expected_identity: tuple[int, int], payload: bytes
) -> None:
    _validate_parent(path.parent)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (
                opened.st_dev,
                opened.st_ino,
            )
            != expected_identity
        ):
            raise ModelAttemptEvidenceError("model evidence target changed")
        _write_all(descriptor, payload, "append")
        os.fsync(descriptor)
    except ModelAttemptEvidenceError:
        raise
    except OSError as error:
        raise ModelAttemptEvidenceError("could not append model evidence") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes, operation: str) -> None:
    offset = 0
    try:
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write made no progress")
            offset += written
    except OSError as error:
        raise ModelAttemptEvidenceError(
            f"could not {operation} model evidence"
        ) from error


def _validate_parent(path: Path) -> None:
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ModelAttemptEvidenceError(
            "model evidence parent is unavailable"
        ) from error
    if _is_link(path) or not stat.S_ISDIR(path_stat.st_mode) or resolved != path:
        raise ModelAttemptEvidenceError("model evidence parent is not a real directory")


def _is_link(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )
