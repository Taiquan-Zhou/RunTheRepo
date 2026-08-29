import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repotrial.domain.enums import Verdict
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult

_SCHEMA_VERSION = 1
_MAX_ARTIFACT_BYTES = 262_144
_MAX_STREAM_BYTES = 32_000
_MAX_COMMANDS = 3
_TRUNCATION_MARKER = "\n...[truncated]"
_REDACTION = "[REDACTED]"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_TEMP_PREFIX = ".baseline-boot-evidence-"


class BootEvidenceError(RuntimeError):
    pass


class _StreamEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str
    truncated: bool


class _CommandEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["up", "ps", "logs"]
    exit_code: int
    stdout: _StreamEvidence
    stderr: _StreamEvidence


class _CommandExceptionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["up", "ps", "logs"]
    exception_type: str


class _FinalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: str
    service_states: dict[str, str]


class _RecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: str
    disposition: Literal["applied", "stopped", "unsupported"]
    reason: str
    stop_reason: str | None


class _EvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    attempt: int = Field(ge=1)
    compose_path: str
    commands: list[_CommandEvidence | _CommandExceptionEvidence] = Field(
        default_factory=list, max_length=_MAX_COMMANDS
    )
    final: _FinalEvidence | None = None
    recovery: _RecoveryEvidence | None = None


class _BootEvidenceSession:
    def __init__(
        self,
        path: Path,
        env: dict[str, str],
        *,
        attempt: int,
        compose_path: str,
    ) -> None:
        _validate_new_target(path)
        _validate_attempt(attempt)
        _validate_compose_path(compose_path)
        self._path = path
        self._redactions = tuple(
            sorted(
                {value for value in env.values() if isinstance(value, str) and value},
                key=lambda value: (-len(value), value),
            )
        )
        self._document = _EvidenceDocument(
            attempt=attempt,
            compose_path=_redact_text(compose_path, self._redactions),
        )
        self._created = False

    def record_command(
        self, name: Literal["up", "ps", "logs"], result: ExecResult
    ) -> None:
        _validate_command_name(name)
        if not isinstance(result, ExecResult):
            raise TypeError("boot command returned a malformed result")
        if (
            type(result.exit_code) is not int
            or not isinstance(result.stdout, str)
            or not isinstance(result.stderr, str)
        ):
            raise TypeError("boot command returned a malformed result")
        self._append(
            _CommandEvidence(
                name=name,
                exit_code=result.exit_code,
                stdout=_stream_evidence(result.stdout, self._redactions),
                stderr=_stream_evidence(result.stderr, self._redactions),
            )
        )

    def record_exception(
        self, name: Literal["up", "ps", "logs"], error: BaseException
    ) -> None:
        _validate_command_name(name)
        self._append(
            _CommandExceptionEvidence(name=name, exception_type=type(error).__name__)
        )

    def finalize(self, verdict: Verdict, service_states: dict[str, str]) -> None:
        if not isinstance(verdict, Verdict):
            raise TypeError("verdict must be a Verdict")
        if not isinstance(service_states, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in service_states.items()
        ):
            raise TypeError("service_states must be a string mapping")
        self._document.final = _FinalEvidence(
            verdict=verdict.value,
            service_states=dict(sorted(service_states.items())),
        )
        self._persist()

    def _append(self, command: _CommandEvidence | _CommandExceptionEvidence) -> None:
        if self._document.final is not None:
            raise BootEvidenceError("boot evidence is already finalized")
        if any(existing.name == command.name for existing in self._document.commands):
            raise BootEvidenceError("boot command evidence is already recorded")
        self._document.commands.append(command)
        self._persist()

    def _persist(self) -> None:
        serialized = _serialize(self._document)
        if not self._created:
            _exclusive_create(self._path, serialized)
            self._created = True
            return
        _atomic_replace(self._path, serialized)


def record_recovery_evidence(
    path: Path,
    action: RecoveryAction,
    *,
    disposition: Literal["applied", "stopped", "unsupported"],
    stop_reason: str | None,
) -> None:
    if not isinstance(action, RecoveryAction):
        raise TypeError("action must be a RecoveryAction")
    if disposition not in {"applied", "stopped", "unsupported"}:
        raise ValueError("invalid recovery disposition")
    if stop_reason is not None and not isinstance(stop_reason, str):
        raise TypeError("stop_reason must be a string or None")
    document = _read_document(path)
    document.recovery = _RecoveryEvidence(
        action=action.action,
        disposition=disposition,
        reason=action.reason,
        stop_reason=stop_reason,
    )
    _atomic_replace(path, _serialize(document))


def _stream_evidence(text: str, redactions: tuple[str, ...]) -> _StreamEvidence:
    redacted = _redact_text(text, redactions)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= _MAX_STREAM_BYTES:
        return _StreamEvidence(text=redacted, truncated=False)
    payload_limit = _MAX_STREAM_BYTES - len(_TRUNCATION_MARKER.encode("ascii"))
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    head = encoded[:head_limit]
    tail = encoded[-tail_limit:]
    return _StreamEvidence(
        text=_decode_head(head) + _TRUNCATION_MARKER + _decode_tail(tail),
        truncated=True,
    )


def _redact_text(text: str, redactions: tuple[str, ...]) -> str:
    redacted = text
    for value in redactions:
        redacted = redacted.replace(value, _REDACTION)
    return redacted


def _decode_head(value: bytes) -> str:
    while True:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            value = value[:-1]


def _decode_tail(value: bytes) -> str:
    while True:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            value = value[1:]


def _serialize(document: _EvidenceDocument) -> bytes:
    # Three commands with two 32,000-byte views leave more than 70 KiB for JSON.
    if _MAX_ARTIFACT_BYTES - (2 * _MAX_COMMANDS * _MAX_STREAM_BYTES) <= 0:
        raise AssertionError("boot evidence metadata budget is exhausted")
    serialized = (
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > _MAX_ARTIFACT_BYTES:
        raise BootEvidenceError("boot evidence exceeds byte budget")
    return serialized


def _exclusive_create(path: Path, serialized: bytes) -> None:
    _validate_new_target(path)
    try:
        with path.open("xb") as output:
            output.write(serialized)
    except OSError as error:
        raise BootEvidenceError("could not create boot evidence") from error
    _verify_bytes(path, serialized)


def _atomic_replace(path: Path, serialized: bytes) -> None:
    _validate_existing_target(path)
    temporary = path.parent / f"{_TEMP_PREFIX}{os.getpid()}-{id(serialized)}.tmp"
    try:
        with temporary.open("xb") as output:
            output.write(serialized)
        _verify_bytes(temporary, serialized)
        _validate_existing_target(path)
        os.replace(temporary, path)
    except OSError as error:
        raise BootEvidenceError("could not replace boot evidence") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    _verify_bytes(path, serialized)


def _read_document(path: Path) -> _EvidenceDocument:
    _validate_existing_target(path)
    try:
        serialized = path.read_bytes()
    except OSError as error:
        raise BootEvidenceError("could not read boot evidence") from error
    if len(serialized) > _MAX_ARTIFACT_BYTES:
        raise BootEvidenceError("boot evidence exceeds byte budget")
    try:
        return _EvidenceDocument.model_validate_json(serialized, strict=True)
    except (ValidationError, ValueError) as error:
        raise BootEvidenceError("boot evidence schema is invalid") from error


def _validate_new_target(path: Path) -> None:
    if not isinstance(path, Path):
        raise TypeError("evidence path must be a Path")
    if path.exists() or path.is_symlink():
        raise BootEvidenceError("boot evidence target is already in use")
    _validate_parent(path.parent)


def _validate_existing_target(path: Path) -> None:
    if not isinstance(path, Path):
        raise TypeError("evidence path must be a Path")
    _validate_parent(path.parent)
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise BootEvidenceError("boot evidence target is unavailable") from error
    if _is_link(path) or not stat.S_ISREG(path_stat.st_mode):
        raise BootEvidenceError("boot evidence target is not a real regular file")


def _validate_parent(parent: Path) -> None:
    try:
        parent_stat = parent.lstat()
        resolved = parent.resolve(strict=True)
    except OSError as error:
        raise BootEvidenceError("boot evidence parent does not exist") from error
    if _is_link(parent) or not stat.S_ISDIR(parent_stat.st_mode) or resolved != parent:
        raise BootEvidenceError("boot evidence parent is not a real directory")


def _verify_bytes(path: Path, expected: bytes) -> None:
    _validate_existing_target(path)
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise BootEvidenceError("could not verify boot evidence") from error
    if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
        raise BootEvidenceError("boot evidence replacement verification failed")


def _validate_command_name(name: str) -> None:
    if name not in {"up", "ps", "logs"}:
        raise ValueError("invalid boot command name")


def _validate_attempt(attempt: object) -> None:
    if type(attempt) is not int:
        raise TypeError("attempt must be an integer")
    if attempt < 1:
        raise ValueError("attempt must be positive")


def _validate_compose_path(compose_path: object) -> None:
    if not isinstance(compose_path, str):
        raise TypeError("compose_path must be a string")
    if "\0" in compose_path:
        raise ValueError("compose_path must not contain NUL")


def _is_link(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        _REPARSE_POINT and path_stat.st_file_attributes & _REPARSE_POINT
    )
