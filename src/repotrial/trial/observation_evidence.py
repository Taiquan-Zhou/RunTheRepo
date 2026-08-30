import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from repotrial.sandbox.base import ExecResult

type ObservationOperation = Literal["discovery", "inspect", "diff", "top", "network"]
type EvidencePhase = Literal[
    "provider_execution",
    "result_validation",
    "parsing",
    "serialization",
    "atomic_persistence",
]
type EvidenceReason = Literal[
    "collector_started",
    "collector_succeeded",
    "provider_exception",
    "nonzero_exit",
    "malformed_exec_result",
    "malformed_network_result",
    "parse_failure",
    "resource_limit_exceeded",
    "network_unsupported",
    "serialization_failed",
    "evidence_too_large",
    "destination_collision",
    "parent_invalid",
    "persistence_failed",
    "close_failed",
]

_SCHEMA_VERSION = 1
_MAX_EVENTS = 388
_MAX_EVENT_BYTES = 4_096
_MAX_LEDGER_BYTES = 1_048_576
_MAX_ARGV_COUNT = 64
_MAX_ARGV_BYTES = 65_536
_MAX_HASHED_OUTPUT_BYTES = 65_536
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_OPERATIONS: frozenset[str] = frozenset(
    {"discovery", "inspect", "diff", "top", "network"}
)
_TERMINAL_TAXONOMY: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("provider_execution", "failure", "provider_exception"),
        ("result_validation", "failure", "nonzero_exit"),
        ("result_validation", "failure", "malformed_exec_result"),
        ("result_validation", "failure", "malformed_network_result"),
        ("parsing", "failure", "parse_failure"),
        ("parsing", "failure", "resource_limit_exceeded"),
        ("parsing", "success", "collector_succeeded"),
        ("parsing", "success", "network_unsupported"),
    }
)
_CLOSE_FAILURE_NOTE = (
    "secondary observation evidence close failed; descriptor ownership is uncertain"
)
_PERSISTENCE_FAILURE_NOTE = (
    "secondary observation evidence persistence failed while preserving primary error"
)


class ObservationEvidenceError(RuntimeError):
    """Private observer evidence could not be safely retained."""

    def __init__(self, phase: EvidencePhase, reason: EvidenceReason) -> None:
        super().__init__("observation evidence could not be retained")
        self.phase = phase
        self.reason = reason


class ObservationEvidenceRecorder:
    """Append bounded collector-boundary metadata to one claimed JSONL file."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("observation evidence path must be a Path")
        _validate_parent(path.parent)
        _reject_existing_destination(path)
        self._path = path
        self._descriptor: int | None = _create_destination(path)
        self._sequence = 0
        self._written_bytes = 0
        self._active_operation: ObservationOperation | None = None
        self._last_close_error: ObservationEvidenceError | None = None

    @property
    def path(self) -> Path:
        return self._path

    def record_start(self, operation: ObservationOperation, argv: list[str]) -> None:
        _validate_operation(operation)
        if self._active_operation is not None:
            raise ValueError("collector evidence already has an active operation")
        event = self._base_event(operation, argv)
        event.update(
            {
                "exception_class": None,
                "outcome": "start",
                "phase": "provider_execution",
                "reason": "collector_started",
                "stderr": None,
                "stdout": None,
            }
        )
        self._append(event)
        self._active_operation = operation

    def record_terminal(
        self,
        operation: ObservationOperation,
        argv: list[str],
        *,
        phase: EvidencePhase,
        outcome: Literal["success", "failure"],
        reason: EvidenceReason,
        result: ExecResult | None = None,
        exception: BaseException | None = None,
    ) -> None:
        _validate_operation(operation)
        _validate_terminal_taxonomy(phase, outcome, reason)
        if self._active_operation != operation:
            raise ValueError(
                "collector evidence terminal operation does not match start"
            )
        event = self._base_event(operation, argv)
        if isinstance(result, ExecResult):
            stdout = _output_metadata(result.stdout)
            stderr = _output_metadata(result.stderr)
        else:
            stdout = None
            stderr = None
        event.update(
            {
                "exception_class": _exception_class(exception),
                "outcome": outcome,
                "phase": phase,
                "reason": reason,
                "stderr": stderr,
                "stdout": stdout,
            }
        )
        self._append(event)
        self._active_operation = None

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError as error:
            close_error = ObservationEvidenceError("atomic_persistence", "close_failed")
            self._last_close_error = close_error
            raise close_error from error
        self._descriptor = None
        self._last_close_error = None

    def close_preserving_primary(self, primary: BaseException) -> None:
        if self._descriptor is None or primary is self._last_close_error:
            return
        if self._last_close_error is not None:
            _add_note(primary, _CLOSE_FAILURE_NOTE)
            return
        try:
            self.close()
        except ObservationEvidenceError:
            _add_note(primary, _CLOSE_FAILURE_NOTE)

    def record_terminal_preserving_primary(
        self,
        operation: ObservationOperation,
        argv: list[str],
        *,
        phase: EvidencePhase,
        reason: EvidenceReason,
        result: ExecResult | None,
        primary: BaseException,
    ) -> None:
        try:
            self.record_terminal(
                operation,
                argv,
                phase=phase,
                outcome="failure",
                reason=reason,
                result=result,
                exception=primary if reason == "provider_exception" else None,
            )
        except ObservationEvidenceError:
            _add_note(primary, _PERSISTENCE_FAILURE_NOTE)

    def _base_event(
        self, operation: ObservationOperation, argv: list[str]
    ) -> dict[str, object]:
        argv_count, argv_sha256 = _argv_metadata(argv)
        return {
            "argv_count": argv_count,
            "argv_sha256": argv_sha256,
            "operation": operation,
            "schema_version": _SCHEMA_VERSION,
            "sequence": self._sequence + 1,
        }

    def _append(self, event: dict[str, object]) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
        if self._sequence >= _MAX_EVENTS:
            raise ObservationEvidenceError("serialization", "evidence_too_large")
        payload = _serialize_event(event)
        if self._written_bytes + len(payload) > _MAX_LEDGER_BYTES:
            raise ObservationEvidenceError("serialization", "evidence_too_large")
        primary: ObservationEvidenceError | None = None
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except OSError as error:
            primary = ObservationEvidenceError(
                "atomic_persistence", "persistence_failed"
            )
            raise primary from error
        finally:
            if primary is not None:
                self._close_preserving_failure(primary)
        self._sequence += 1
        self._written_bytes += len(payload)

    def _close_preserving_failure(self, primary: BaseException) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError:
            _add_note(primary, _CLOSE_FAILURE_NOTE)
            return
        self._descriptor = None


def _validate_operation(operation: object) -> None:
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError("invalid observation evidence operation")


def _validate_terminal_taxonomy(phase: object, outcome: object, reason: object) -> None:
    if not isinstance(phase, str) or phase not in {
        "provider_execution",
        "result_validation",
        "parsing",
        "serialization",
        "atomic_persistence",
    }:
        raise ValueError("invalid observation evidence phase")
    if not isinstance(outcome, str) or outcome not in {"success", "failure"}:
        raise ValueError("invalid observation evidence outcome")
    if (
        not isinstance(reason, str)
        or (phase, outcome, reason) not in _TERMINAL_TAXONOMY
    ):
        raise ValueError("invalid observation evidence reason")


def _argv_metadata(argv: object) -> tuple[int, str]:
    if (
        type(argv) is not list
        or len(argv) > _MAX_ARGV_COUNT
        or any(not isinstance(item, str) for item in argv)
    ):
        raise ValueError("invalid observation evidence argv")
    try:
        serialized = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ObservationEvidenceError(
            "serialization", "serialization_failed"
        ) from None
    if len(serialized) > _MAX_ARGV_BYTES:
        raise ObservationEvidenceError("serialization", "evidence_too_large")
    return len(argv), hashlib.sha256(serialized).hexdigest()


def _output_metadata(value: str) -> dict[str, object]:
    encoded_bytes = 0
    hashed_bytes = 0
    digest = hashlib.sha256()
    for offset in range(0, len(value), 4_096):
        encoded = value[offset : offset + 4_096].encode("utf-8", errors="replace")
        encoded_bytes += len(encoded)
        remaining = _MAX_HASHED_OUTPUT_BYTES - hashed_bytes
        if remaining > 0:
            accepted = encoded[:remaining]
            digest.update(accepted)
            hashed_bytes += len(accepted)
    return {
        "encoded_bytes": encoded_bytes,
        "hashed_bytes": hashed_bytes,
        "sha256": digest.hexdigest(),
        "truncated": hashed_bytes < encoded_bytes,
    }


def _exception_class(exception: BaseException | None) -> str | None:
    if exception is None:
        return None
    name = type(exception).__name__
    return name if _EXCEPTION_CLASS.fullmatch(name) is not None else "Exception"


def _serialize_event(event: dict[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(
                event,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ObservationEvidenceError(
            "serialization", "serialization_failed"
        ) from None
    if len(payload) > _MAX_EVENT_BYTES:
        raise ObservationEvidenceError("serialization", "evidence_too_large")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write made no progress")
        offset += written


def _validate_parent(path: Path) -> None:
    try:
        identity = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "parent_invalid"
        ) from error
    if not stat.S_ISDIR(identity.st_mode) or _is_link(identity) or resolved != path:
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")


def _reject_existing_destination(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error
    raise ObservationEvidenceError("atomic_persistence", "destination_collision")


def _create_destination(path: Path) -> int:
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "destination_collision"
        ) from error
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
    except BaseException as error:
        try:
            os.close(descriptor)
        except OSError:
            _add_note(error, _CLOSE_FAILURE_NOTE)
        raise
    return descriptor


def _is_link(identity: object) -> bool:
    mode = getattr(identity, "st_mode", 0)
    attributes = getattr(identity, "st_file_attributes", 0)
    return stat.S_ISLNK(mode) or bool(_REPARSE_POINT and attributes & _REPARSE_POINT)


def _add_note(primary: BaseException, note: str) -> None:
    if note not in getattr(primary, "__notes__", ()):
        primary.add_note(note)
