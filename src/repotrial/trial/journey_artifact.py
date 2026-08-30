import hashlib
import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from repotrial.domain.models import Journey

_SCHEMA_VERSION = 1
_MAX_ARTIFACT_BYTES = 262_144
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class JourneyArtifactError(RuntimeError):
    """The private canonical baseline Journey artifact is unsafe or mismatched."""


def write_baseline_journeys(path: Path, journeys: Sequence[Journey]) -> str:
    """Persist the exact baseline Journey payload once and return its SHA-256."""
    payload = _canonical_payload(journeys)
    payload_hash = _sha256(payload)
    document = (
        _canonical_json_bytes(
            {
                "journeys": json.loads(payload),
                "payload_sha256": payload_hash,
                "schema_version": _SCHEMA_VERSION,
            }
        )
        + b"\n"
    )
    if len(document) > _MAX_ARTIFACT_BYTES:
        raise JourneyArtifactError("baseline journey artifact exceeds byte budget")
    _create_new_file(path, document)
    return payload_hash


def verify_baseline_journeys(path: Path, journeys: Sequence[Journey]) -> str:
    """Fail closed unless in-memory Journeys exactly match the persisted payload."""
    document = _read_document(path)
    expected_payload = _canonical_payload(journeys)
    expected_hash = _sha256(expected_payload)
    if document["payload_sha256"] != expected_hash:
        raise JourneyArtifactError("baseline journey artifact hash mismatch")
    stored_payload = _canonical_json_bytes(document["journeys"])
    if _sha256(stored_payload) != expected_hash:
        raise JourneyArtifactError("baseline journey artifact hash mismatch")
    return expected_hash


def _canonical_payload(journeys: Sequence[Journey]) -> bytes:
    if isinstance(journeys, (str, bytes)):
        raise TypeError("journeys must be a Journey sequence")
    items: list[dict[str, object]] = []
    for journey in journeys:
        if not isinstance(journey, Journey):
            raise TypeError("journeys must contain Journey values")
        items.append(journey.model_dump(mode="json"))
    return _canonical_json_bytes(items)


def _read_document(path: Path) -> dict[str, object]:
    _validate_existing_file(path)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise JourneyArtifactError(
            "could not read baseline journey artifact"
        ) from error
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise JourneyArtifactError("baseline journey artifact exceeds byte budget")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JourneyArtifactError("baseline journey artifact is invalid") from None
    if (
        type(document) is not dict
        or set(document) != {"schema_version", "payload_sha256", "journeys"}
        or document["schema_version"] != _SCHEMA_VERSION
        or not isinstance(document["payload_sha256"], str)
        or len(document["payload_sha256"]) != 64
        or type(document["journeys"]) is not list
    ):
        raise JourneyArtifactError("baseline journey artifact is invalid")
    return document


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise JourneyArtifactError(
            "baseline journey payload cannot be serialized"
        ) from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _create_new_file(path: Path, payload: bytes) -> None:
    if not isinstance(path, Path):
        raise TypeError("baseline journey artifact path must be a Path")
    _validate_parent(path.parent)
    if path.exists() or path.is_symlink():
        raise JourneyArtifactError("baseline journey artifact is already in use")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
    except OSError as error:
        raise JourneyArtifactError(
            "baseline journey artifact is already in use"
        ) from error
    _validate_existing_file(path)


def _validate_existing_file(path: Path) -> None:
    if not isinstance(path, Path):
        raise TypeError("baseline journey artifact path must be a Path")
    _validate_parent(path.parent)
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise JourneyArtifactError(
            "baseline journey artifact is unavailable"
        ) from error
    if _is_link(path) or not stat.S_ISREG(path_stat.st_mode):
        raise JourneyArtifactError(
            "baseline journey artifact is not a real regular file"
        )


def _validate_parent(path: Path) -> None:
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise JourneyArtifactError(
            "baseline journey artifact parent is unavailable"
        ) from error
    if _is_link(path) or not stat.S_ISDIR(path_stat.st_mode) or resolved != path:
        raise JourneyArtifactError(
            "baseline journey artifact parent is not a real directory"
        )


def _is_link(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )
