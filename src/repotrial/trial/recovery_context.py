from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

_MAX_SOURCE_BYTES = 65_536
_MAX_ENV_KEYS = 32
_MAX_LOG_ENTRIES = 32
_MAX_LOG_KEY_LENGTH = 128
_MAX_LOG_FIELD_LENGTH = 4_096
_MAX_AGGREGATE_LOG_LENGTH = 16_384
_TRUNCATION_MARKER = "\n[TRUNCATED]\n"
_MIN_TRUNCATED_FIELD_LENGTH = len(_TRUNCATION_MARKER) + 2
_MAX_YAML_NODES = 4_096
_MAX_YAML_DEPTH = 128
_PORTABLE_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_COMPOSE_INTERPOLATION = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(?::-|-|:\?|\?)[^}]*)?\}"
)
_ENV_EXAMPLE_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|$)"
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class RecoveryEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs: dict[str, str]


class DeclaredEnvSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    relative_path: str
    sha256: str


class RecoveryRepositoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_env_keys: frozenset[str]
    declarations: tuple[DeclaredEnvSource, ...]


def project_recovery_evidence(logs: Mapping[str, str]) -> RecoveryEvidenceView:
    """Return the bounded planner view of already-sanitized Boot logs."""
    if not isinstance(logs, Mapping):
        raise TypeError("logs must be a mapping")

    string_names = [name for name in logs if isinstance(name, str)]
    ordered_names = [name for name in ("up", "ps", "logs") if name in logs]
    ordered_names.extend(
        sorted(name for name in string_names if name not in {"up", "ps", "logs"})
    )
    projected: dict[str, str] = {}
    remaining = _MAX_AGGREGATE_LOG_LENGTH
    for name in ordered_names:
        if len(projected) >= _MAX_LOG_ENTRIES or remaining <= 0:
            break
        if not isinstance(name, str) or len(name) > _MAX_LOG_KEY_LENGTH:
            continue
        value = logs[name]
        if not isinstance(value, str):
            continue
        separator_length = 1 if projected else 0
        limit = min(_MAX_LOG_FIELD_LENGTH, remaining - separator_length)
        projected_value = _head_tail(value, limit)
        if projected_value is None:
            continue
        projected[name] = projected_value
        remaining -= separator_length + len(projected_value)
    return RecoveryEvidenceView(logs=projected)


def derive_recovery_context(
    workspace: Path,
    compose_path: str,
) -> RecoveryRepositoryContext:
    """Read bounded environment declarations from the already pinned workspace."""
    root = _real_directory(workspace, "workspace")
    compose = _selected_compose_source(root, compose_path)
    sources: list[tuple[Path, str]] = [(compose, compose.relative_to(root).as_posix())]
    env_example = root / ".env.example"
    if _path_exists(env_example):
        sources.append((env_example, ".env.example"))

    declarations: list[DeclaredEnvSource] = []
    for source, relative_path in sources:
        content, sha256 = _read_utf8_source(root, source, relative_path)
        keys = (
            _compose_value_keys(content, relative_path)
            if relative_path != ".env.example"
            else _env_example_keys(content)
        )
        declarations.extend(
            DeclaredEnvSource(key=key, relative_path=relative_path, sha256=sha256)
            for key in keys
            if _PORTABLE_ENV_KEY.fullmatch(key) is not None
        )

    allowed = sorted({declaration.key for declaration in declarations})[:_MAX_ENV_KEYS]
    allowed_keys = frozenset(allowed)
    bounded = [item for item in declarations if item.key in allowed_keys]
    unique = {(item.key, item.relative_path, item.sha256): item for item in bounded}
    ordered = tuple(item for _, item in sorted(unique.items()))
    return RecoveryRepositoryContext(
        allowed_env_keys=allowed_keys,
        declarations=ordered,
    )


def _head_tail(value: str, limit: int) -> str | None:
    if len(value) <= limit:
        return value
    if limit < _MIN_TRUNCATED_FIELD_LENGTH:
        return None
    retained = limit - len(_TRUNCATION_MARKER)
    head_length = (retained + 1) // 2
    tail_length = retained - head_length
    tail = value[-tail_length:] if tail_length else ""
    return f"{value[:head_length]}{_TRUNCATION_MARKER}{tail}"


def _env_example_keys(content: str) -> list[str]:
    keys: list[str] = []
    for line in content.splitlines():
        match = _ENV_EXAMPLE_DECLARATION.match(line)
        if match is not None:
            keys.append(match.group(1))
    return keys


def _selected_compose_source(workspace: Path, compose_path: str) -> Path:
    if (
        not isinstance(compose_path, str)
        or not compose_path
        or "\\" in compose_path
        or _contains_control(compose_path)
    ):
        raise ValueError("compose path must be a safe relative path")
    relative = Path(compose_path)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("compose path must be a safe relative path")
    return workspace / relative


def _real_directory(path: Path, label: str) -> Path:
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"{label} must be an existing real directory") from None
    if not stat.S_ISDIR(path_stat.st_mode) or _is_link(path, path_stat):
        raise ValueError(f"{label} must be an existing real directory")
    return resolved


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError(".env.example could not be read") from None
    return True


def _require_path_identity(
    workspace: Path,
    path: Path,
    expected: tuple[int, int, int],
    label: str,
) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise ValueError(f"{label} must be an existing regular file") from None
    _require_path_inside(workspace, path, label)
    _reject_linked_components(workspace, path.parent)
    if not _is_regular_file(path_stat) or _is_link(path, path_stat):
        raise ValueError(f"{label} must be an existing regular file")
    if _file_identity(path_stat) != expected:
        raise ValueError(f"{label} changed while reading")


def _require_path_inside(workspace: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        raise ValueError(f"{label} must be inside workspace") from None
    if ".." in relative.parts:
        raise ValueError(f"{label} must be inside workspace")


def _reject_linked_components(workspace: Path, target: Path) -> None:
    current = workspace
    for component in target.relative_to(workspace).parts:
        current /= component
        try:
            current_stat = current.lstat()
        except OSError:
            raise ValueError("trusted path component does not exist") from None
        if _is_link(current, current_stat):
            raise ValueError("trusted path contains a link")


def _read_utf8_source(
    workspace: Path, source: Path, label: str
) -> tuple[str, str]:
    try:
        source_stat = source.lstat()
    except OSError:
        raise ValueError(f"{label} could not be read") from None
    _require_path_inside(workspace, source, label)
    _reject_linked_components(workspace, source.parent)
    if not _is_regular_file(source_stat) or _is_link(source, source_stat):
        raise ValueError(f"{label} must be an existing regular file")
    source_identity = _file_identity(source_stat)
    try:
        with source.open("rb") as source_file:
            handle_stat = os.fstat(source_file.fileno())
            if not _is_regular_file(handle_stat) or _file_identity(handle_stat) != source_identity:
                raise ValueError(f"{label} changed while reading")
            _require_path_identity(workspace, source, source_identity, label)
            if handle_stat.st_size > _MAX_SOURCE_BYTES:
                raise ValueError(f"{label} exceeds the source size limit")
            source_bytes = source_file.read(_MAX_SOURCE_BYTES + 1)
            if len(source_bytes) > _MAX_SOURCE_BYTES:
                raise ValueError(f"{label} exceeds the source size limit")
            final_handle_stat = os.fstat(source_file.fileno())
            if (
                not _is_regular_file(final_handle_stat)
                or _file_identity(final_handle_stat) != source_identity
                or final_handle_stat.st_size > _MAX_SOURCE_BYTES
            ):
                raise ValueError(f"{label} changed while reading")
            _require_path_identity(workspace, source, source_identity, label)
    except OSError:
        raise ValueError(f"{label} could not be read") from None
    try:
        content = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} must be UTF-8") from None
    return content, hashlib.sha256(source_bytes).hexdigest()


def _compose_value_keys(content: str, label: str) -> list[str]:
    values = _yaml_scalar_values(content, label)
    return [key for value in values for key in _interpolation_keys(value)]


def _yaml_scalar_values(content: str, label: str) -> list[str]:
    yaml = YAML(typ="rt", pure=True)
    yaml.allow_duplicate_keys = False
    values: list[str] = []
    collections: list[list[object]] = []
    document_count = 0
    node_count = 0
    try:
        for event in yaml.parse(content):
            if isinstance(event, DocumentStartEvent):
                document_count += 1
                if document_count > 1:
                    raise ValueError(f"{label} must contain one YAML document")
            if isinstance(event, (AliasEvent, MappingStartEvent, ScalarEvent, SequenceStartEvent)):
                node_count += 1
                if node_count > _MAX_YAML_NODES:
                    raise ValueError(f"{label} exceeds YAML resource limit")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                _begin_collection_value(collections, label)
                if len(collections) >= _MAX_YAML_DEPTH:
                    raise ValueError(f"{label} exceeds YAML resource limit")
                collections.append(["mapping" if isinstance(event, MappingStartEvent) else "sequence", True])
            elif isinstance(event, ScalarEvent):
                if _scalar_is_mapping_key(collections):
                    collections[-1][1] = False
                else:
                    values.append(event.value)
                    _complete_collection_value(collections)
            elif isinstance(event, AliasEvent):
                if _scalar_is_mapping_key(collections):
                    raise ValueError(f"{label} contains an unsupported YAML key")
                _complete_collection_value(collections)
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                if not collections:
                    raise ValueError(f"{label} must be valid YAML")
                collections.pop()
    except (OverflowError, RecursionError, UnicodeError, YAMLError):
        raise ValueError(f"{label} must be valid YAML") from None
    if document_count != 1 or collections:
        raise ValueError(f"{label} must be valid YAML")
    return values


def _begin_collection_value(collections: list[list[object]], label: str) -> None:
    if not collections:
        return
    if _scalar_is_mapping_key(collections):
        raise ValueError(f"{label} contains an unsupported YAML key")
    _complete_collection_value(collections)


def _scalar_is_mapping_key(collections: list[list[object]]) -> bool:
    return bool(collections and collections[-1][0] == "mapping" and collections[-1][1])


def _complete_collection_value(collections: list[list[object]]) -> None:
    if collections and collections[-1][0] == "mapping":
        collections[-1][1] = True


def _interpolation_keys(value: str) -> list[str]:
    keys: list[str] = []
    position = 0
    while position < len(value):
        if value[position] != "$":
            position += 1
            continue
        if position + 1 < len(value) and value[position + 1] == "$":
            position += 2
            continue
        match = _COMPOSE_INTERPOLATION.match(value, position)
        if match is None:
            position += 1
            continue
        keys.append(match.group(1))
        position = match.end()


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int]:
    if stat_result.st_ino == 0:
        raise ValueError("source identity is invalid")
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat.S_IFMT(stat_result.st_mode),
    )


def _is_regular_file(stat_result: os.stat_result) -> bool:
    return stat.S_ISREG(stat_result.st_mode) and not (
        _REPARSE_POINT
        and getattr(stat_result, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _is_link(path: Path, path_stat: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)
