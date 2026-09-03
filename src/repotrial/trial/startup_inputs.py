"""Deterministic planning for the narrowly supported repository startup input."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from repotrial.compose.parser import canonical_compose_json, load_compose

_MAX_SOURCE_BYTES: Final = 65_536
_MAX_LINE_BYTES: Final = 4_096
_MAX_ASSIGNMENTS: Final = 64
_SYNTHETIC_VALUE: Final = "repotrial-synthetic-value"
_POLICY_ID: Final = "startup-input-v1-option-b"
_SOURCE_NAME: Final = ".env.sample"
_TARGET_NAME: Final = ".env"
_ASSIGNMENT_RE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z")
_PORTABLE_LITERAL_RE: Final = re.compile(r"[A-Za-z0-9._:/@+%,-]+\Z")
_PATH_COMPONENT_RE: Final = re.compile(r"[A-Za-z0-9._:-]+\Z")
_CONTROL_EXACT: Final = frozenset(
    {"PATH", "HOME", "PYTHONHOME", "PYTHONPATH", "XDG_CONFIG_HOME"}
)
_CONTROL_PREFIXES: Final = ("COMPOSE_", "DOCKER_", "DYLD_", "LD_")
_SECRET_COMPONENTS: Final = frozenset({"PASSWORD", "SECRET", "TOKEN"})


class StartupInputUnsupported(RuntimeError):
    """A fail-closed startup-input planning rejection."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class StartupInputPlan:
    """Immutable, host-derived bytes and identity for one startup input."""

    compose_relative_path: str
    compose_config_hash: str
    source_relative_path: str
    target_relative_path: str
    source_sha256: str
    output_sha256: str
    all_source_key_names: tuple[str, ...]
    accepted_key_names: tuple[str, ...]
    synthetic_key_names: tuple[str, ...]
    omitted_control_key_names: tuple[str, ...]
    expected_service_names: tuple[str, ...]
    policy_id: str
    output_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Assignment:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    file_type: int
    size: int
    mtime_ns: int
    ctime_ns: int


def plan_startup_input(workspace: Path, compose_path: str) -> StartupInputPlan | None:
    """Plan a guest-only root ``.env`` for the supported Compose shape."""
    root = _resolve_workspace(workspace)
    compose_file = _resolve_compose_path(root, compose_path)
    compose = load_compose(compose_file)
    target_relative, services = _required_env_target(root, compose)
    if target_relative is None:
        return None

    target = root / Path(*target_relative.split("/"))
    _require_target_absent(target)
    source = target.parent / _SOURCE_NAME
    source_bytes = _read_source(source)
    assignments = _parse_source(source_bytes)

    ordered_assignments = sorted(assignments, key=lambda item: item.key.encode("ascii"))
    output_assignments: list[_Assignment] = []
    synthetic_keys: list[str] = []
    omitted_control_keys: list[str] = []
    for assignment in ordered_assignments:
        if _is_control_key(assignment.key):
            omitted_control_keys.append(assignment.key)
            continue
        value = assignment.value
        if value and _is_secret_looking_key(assignment.key):
            value = _SYNTHETIC_VALUE
            synthetic_keys.append(assignment.key)
        output_assignments.append(_Assignment(assignment.key, value))

    output_bytes = b"".join(
        f"{assignment.key}={assignment.value}\n".encode()
        for assignment in output_assignments
    )
    compose_config_hash = hashlib.sha256(
        canonical_compose_json(compose).encode("utf-8")
    ).hexdigest()
    source_relative_path = _relative_posix(root, source)
    target_relative_path = _relative_posix(root, target)
    expected_services = tuple(sorted(services, key=lambda value: value.encode("utf-8")))
    return StartupInputPlan(
        compose_relative_path=_relative_posix(root, compose_file),
        compose_config_hash=compose_config_hash,
        source_relative_path=source_relative_path,
        target_relative_path=target_relative_path,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        output_sha256=hashlib.sha256(output_bytes).hexdigest(),
        all_source_key_names=tuple(
            assignment.key for assignment in ordered_assignments
        ),
        accepted_key_names=tuple(assignment.key for assignment in output_assignments),
        synthetic_key_names=tuple(synthetic_keys),
        omitted_control_key_names=tuple(omitted_control_keys),
        expected_service_names=expected_services,
        policy_id=_POLICY_ID,
        output_bytes=output_bytes,
    )


def _resolve_workspace(workspace: Path) -> Path:
    try:
        root = workspace.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise StartupInputUnsupported("workspace_invalid") from None
    if not root.is_dir():
        raise StartupInputUnsupported("workspace_invalid")
    return root


def _resolve_compose_path(root: Path, compose_path: str) -> Path:
    if (
        not compose_path
        or "\\" in compose_path
        or any(ord(character) < 32 for character in compose_path)
    ):
        raise StartupInputUnsupported("compose_path_invalid")
    candidate = Path(compose_path)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise StartupInputUnsupported("compose_path_invalid")
    try:
        unresolved = root / candidate
        current = root
        for index, component in enumerate(candidate.parts):
            current /= component
            metadata = current.lstat()
            is_final = index == len(candidate.parts) - 1
            if _has_reparse_point(metadata) or stat.S_ISLNK(metadata.st_mode):
                raise StartupInputUnsupported("compose_path_invalid")
            if is_final:
                if not stat.S_ISREG(metadata.st_mode):
                    raise StartupInputUnsupported("compose_path_invalid")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise StartupInputUnsupported("compose_path_invalid")
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(root)
    except StartupInputUnsupported:
        raise
    except (OSError, RuntimeError, ValueError):
        raise StartupInputUnsupported("compose_path_invalid") from None
    return resolved


def _required_env_target(
    root: Path, compose: Mapping[str, object]
) -> tuple[str | None, set[str]]:
    services = compose.get("services")
    if not isinstance(services, Mapping) or not services:
        return None, set()

    all_services: set[str] = set()
    target_candidates: set[Path] = set()
    for service_name, service in services.items():
        if not isinstance(service_name, str) or not isinstance(service, Mapping):
            return None, set()
        all_services.add(service_name)
        if "env_file" not in service:
            continue
        entries = _env_file_entries(service["env_file"])
        if entries is None:
            return None, set()
        parsed_entries = [_parse_env_file_entry(entry) for entry in entries]
        if any(entry is None for entry in parsed_entries):
            return None, set()
        valid_entries = [entry for entry in parsed_entries if entry is not None]
        required_paths = [path for path, required in valid_entries if required]
        if not required_paths:
            continue
        if len(required_paths) != 1:
            return None, set()
        raw_target = required_paths[0]
        target = _resolve_env_target(root, raw_target)
        if target is None:
            return None, set()
        try:
            target.relative_to(root)
        except ValueError:
            raise StartupInputUnsupported("target_escape") from None
        if target != root / _TARGET_NAME:
            return None, set()
        target_candidates.add(target)

    if len(target_candidates) != 1:
        return None, set()
    return _TARGET_NAME, all_services


def _env_file_entries(value: object) -> list[object] | None:
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, list):
        return list(value)
    return None


def _parse_env_file_entry(value: object) -> tuple[str, bool] | None:
    if isinstance(value, str):
        return value, True
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    required = value.get("required", True)
    if not isinstance(path, str) or not isinstance(required, bool):
        return None
    return path, required


def _resolve_env_target(compose_parent: Path, raw_target: str) -> Path | None:
    candidate = Path(raw_target)
    if candidate.is_absolute():
        raise StartupInputUnsupported("target_escape")
    if not raw_target or "\\" in raw_target or "\x00" in raw_target:
        return None
    try:
        return (compose_parent / candidate).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _require_target_absent(target: Path) -> None:
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise StartupInputUnsupported("target_preexisting") from None
    raise StartupInputUnsupported("target_preexisting")


def _read_source(path: Path) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        raise StartupInputUnsupported("source_missing") from None
    except OSError:
        raise StartupInputUnsupported("source_invalid") from None
    if _is_linked_or_nonregular(path_stat):
        raise StartupInputUnsupported("source_linked")
    expected_identity = _identity(path_stat)
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except FileNotFoundError:
        raise StartupInputUnsupported("source_missing") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StartupInputUnsupported("source_linked") from None
        raise StartupInputUnsupported("source_invalid") from None
    try:
        initial_stat = os.fstat(descriptor)
        identity = _regular_identity(initial_stat)
        if identity != expected_identity:
            raise StartupInputUnsupported("source_changed")
        source = os.read(descriptor, _MAX_SOURCE_BYTES + 1)
        final_stat = os.fstat(descriptor)
        if _regular_identity(final_stat) != identity:
            raise StartupInputUnsupported("source_changed")
        _require_path_identity(path, identity)
        if initial_stat.st_size > _MAX_SOURCE_BYTES or len(source) > _MAX_SOURCE_BYTES:
            raise StartupInputUnsupported("source_too_large")
        if final_stat.st_size != len(source):
            raise StartupInputUnsupported("source_changed")
        return source
    except StartupInputUnsupported:
        raise
    except FileNotFoundError:
        raise StartupInputUnsupported("source_changed") from None
    except OSError:
        raise StartupInputUnsupported("source_invalid") from None
    finally:
        os.close(descriptor)


def _regular_identity(stat_result: os.stat_result) -> _FileIdentity:
    if _is_linked_or_nonregular(stat_result):
        raise StartupInputUnsupported("source_invalid")
    return _identity(stat_result)


def _identity(stat_result: os.stat_result) -> _FileIdentity:
    if stat_result.st_ino == 0:
        raise StartupInputUnsupported("source_invalid")
    return _FileIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        file_type=stat.S_IFMT(stat_result.st_mode),
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        ctime_ns=stat_result.st_ctime_ns,
    )


def _is_linked_or_nonregular(stat_result: os.stat_result) -> bool:
    return not stat.S_ISREG(stat_result.st_mode) or _has_reparse_point(stat_result)


def _has_reparse_point(stat_result: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _require_path_identity(path: Path, expected: _FileIdentity) -> None:
    try:
        current = path.lstat()
    except OSError:
        raise StartupInputUnsupported("source_changed") from None
    if _is_linked_or_nonregular(current):
        raise StartupInputUnsupported("source_changed")
    actual = _identity(current)
    if actual != expected:
        raise StartupInputUnsupported("source_changed")


def _parse_source(source: bytes) -> list[_Assignment]:
    for index, byte in enumerate(source):
        if byte == 13 and (index + 1 == len(source) or source[index + 1] != 10):
            raise StartupInputUnsupported("unsupported_syntax")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise StartupInputUnsupported("invalid_utf8") from None
    for character in text:
        if unicodedata.category(character) == "Cc" and character not in "\r\n":
            raise StartupInputUnsupported("unsupported_syntax")

    assignments: list[_Assignment] = []
    seen_keys: set[str] = set()
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
            raise StartupInputUnsupported("source_too_large")
        if not line or line.strip(" ") == "":
            continue
        if line.lstrip(" ").startswith("#"):
            continue
        match = _ASSIGNMENT_RE.fullmatch(line)
        if match is None:
            raise StartupInputUnsupported("unsupported_syntax")
        key, value = match.groups()
        if key in seen_keys:
            raise StartupInputUnsupported("duplicate_key")
        _validate_value(value)
        seen_keys.add(key)
        assignments.append(_Assignment(key, value))
        if len(assignments) > _MAX_ASSIGNMENTS:
            raise StartupInputUnsupported("too_many_assignments")
    return assignments


def _validate_value(value: str) -> None:
    if value == "":
        return
    if not value.isascii() or not _PORTABLE_LITERAL_RE.fullmatch(value):
        raise StartupInputUnsupported("unsupported_syntax")
    if value.startswith(("/", "../")) or value == "..":
        raise StartupInputUnsupported("unsupported_syntax")
    if value.startswith("./"):
        _validate_relative_path(value)
    elif "/../" in value or value.endswith("/.."):
        raise StartupInputUnsupported("unsupported_syntax")


def _validate_relative_path(value: str) -> None:
    depth = 0
    for component in value[2:].split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            depth -= 1
            if depth < 0:
                raise StartupInputUnsupported("unsupported_syntax")
            continue
        if _PATH_COMPONENT_RE.fullmatch(component) is None:
            raise StartupInputUnsupported("unsupported_syntax")
        depth += 1


def _is_control_key(key: str) -> bool:
    normalized = key.upper()
    return normalized in _CONTROL_EXACT or normalized.startswith(_CONTROL_PREFIXES)


def _is_secret_looking_key(key: str) -> bool:
    components = key.upper().split("_")
    if _SECRET_COMPONENTS.intersection(components):
        return True
    return any(
        components[index : index + 2] == ["API", "KEY"]
        for index in range(len(components) - 1)
    )


def _relative_posix(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise StartupInputUnsupported("target_escape") from None
    return relative.as_posix()
