from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_MAX_SOURCE_BYTES = 65_536
_MAX_ENV_KEYS = 32
_MAX_LOG_ENTRIES = 32
_MAX_LOG_KEY_LENGTH = 128
_MAX_LOG_FIELD_LENGTH = 4_096
_MAX_AGGREGATE_LOG_LENGTH = 16_384
_TRUNCATION_MARKER = "\n[TRUNCATED]\n"
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

    ordered_names = [name for name in ("up", "ps", "logs") if name in logs]
    ordered_names.extend(
        sorted(name for name in logs if name not in {"up", "ps", "logs"})
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
        limit = min(_MAX_LOG_FIELD_LENGTH, remaining)
        projected[name] = _head_tail(value, limit)
        remaining -= len(projected[name])
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
    if env_example.exists() or env_example.is_symlink():
        sources.append(
            (_regular_file(env_example, root, ".env.example"), ".env.example")
        )

    declarations: list[DeclaredEnvSource] = []
    for source, relative_path in sources:
        content, sha256 = _read_utf8_source(source, relative_path)
        keys = (
            _COMPOSE_INTERPOLATION.findall(content)
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


def _head_tail(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:limit]
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
    return _regular_file(workspace / relative, workspace, "compose")


def _real_directory(path: Path, label: str) -> Path:
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"{label} must be an existing real directory") from None
    if not stat.S_ISDIR(path_stat.st_mode) or _is_link(path, path_stat):
        raise ValueError(f"{label} must be an existing real directory")
    return resolved


def _regular_file(path: Path, workspace: Path, label: str) -> Path:
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        raise ValueError(f"{label} must be inside workspace") from None
    if ".." in relative.parts:
        raise ValueError(f"{label} must be inside workspace")
    _reject_linked_components(workspace, path.parent)
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"{label} must be an existing regular file") from None
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or _is_link(path, path_stat)
        or not resolved.is_relative_to(workspace)
    ):
        raise ValueError(f"{label} must be an existing regular file")
    return resolved


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


def _read_utf8_source(source: Path, label: str) -> tuple[str, str]:
    try:
        source_bytes = source.read_bytes()
    except OSError:
        raise ValueError(f"{label} could not be read") from None
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise ValueError(f"{label} exceeds the source size limit")
    try:
        content = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} must be UTF-8") from None
    return content, hashlib.sha256(source_bytes).hexdigest()


def _is_link(path: Path, path_stat: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)
