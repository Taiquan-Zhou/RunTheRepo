"""Deterministic discovery of a root-level Compose file."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_STANDARD_COMPOSE_FILENAMES = (
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)
_COMPOSE_SUFFIXES = frozenset({".yml", ".yaml"})


class ComposeDiscoveryError(RuntimeError):
    """A sanitized Compose-discovery failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"compose discovery failed: {reason}")


class ComposeNotFoundError(ComposeDiscoveryError):
    """Raised when a root contains no eligible Compose file."""


class AmbiguousComposeError(ComposeDiscoveryError):
    """Raised when more than one nonstandard Compose candidate exists."""


@dataclass(frozen=True)
class _PathIdentity:
    device: int
    inode: int
    file_type: int


def discover_compose(root: Path) -> Path:
    """Return the one eligible root-level Compose file for ``root``."""
    normalized_root, root_identity = _normalize_root(root)

    for filename in _STANDARD_COMPOSE_FILENAMES:
        _require_root_identity(root, normalized_root, root_identity)
        candidate = normalized_root / filename
        candidate_stat = _lstat(candidate)
        _require_root_identity(root, normalized_root, root_identity)
        if candidate_stat is None:
            continue
        if not _is_regular_file(candidate_stat):
            raise ComposeDiscoveryError("invalid_standard_file")
        return _normalize_candidate(
            root,
            normalized_root,
            root_identity,
            candidate,
            _identity(candidate_stat),
        )

    fallback_candidates: list[tuple[Path, _PathIdentity]] = []
    _require_root_identity(root, normalized_root, root_identity)
    try:
        direct_children = list(normalized_root.iterdir())
    except OSError:
        raise ComposeDiscoveryError("root_unreadable") from None
    _require_root_identity(root, normalized_root, root_identity)
    for candidate in direct_children:
        if not _is_fallback_candidate_name(candidate):
            continue
        candidate_stat = _lstat(candidate)
        _require_root_identity(root, normalized_root, root_identity)
        if candidate_stat is None or not _is_regular_file(candidate_stat):
            raise ComposeDiscoveryError("invalid_fallback_file")
        fallback_candidates.append((candidate, _identity(candidate_stat)))

    _require_root_identity(root, normalized_root, root_identity)
    if not fallback_candidates:
        raise ComposeNotFoundError("not_found")
    if len(fallback_candidates) > 1:
        raise AmbiguousComposeError("ambiguous")
    candidate, candidate_identity = fallback_candidates[0]
    return _normalize_candidate(
        root,
        normalized_root,
        root_identity,
        candidate,
        candidate_identity,
    )


def _normalize_root(root: Path) -> tuple[Path, _PathIdentity]:
    root_stat = _lstat(root)
    if root_stat is None or not _is_directory(root_stat):
        raise ComposeDiscoveryError("invalid_root")
    root_identity = _identity(root_stat)
    try:
        normalized_root = root.resolve(strict=True)
    except OSError:
        raise ComposeDiscoveryError("invalid_root") from None
    _require_root_identity(root, normalized_root, root_identity)
    return normalized_root, root_identity


def _is_fallback_candidate_name(candidate: Path) -> bool:
    return (
        "compose" in candidate.name.casefold()
        and candidate.suffix.casefold() in _COMPOSE_SUFFIXES
    )


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ComposeDiscoveryError("path_unreadable") from None


def _normalize_candidate(
    root: Path,
    normalized_root: Path,
    root_identity: _PathIdentity,
    candidate: Path,
    candidate_identity: _PathIdentity,
) -> Path:
    _require_root_identity(root, normalized_root, root_identity)
    try:
        normalized_candidate = candidate.resolve(strict=True)
    except OSError:
        raise ComposeDiscoveryError("candidate_unreadable") from None
    _require_root_identity(root, normalized_root, root_identity)
    _require_file_identity(candidate, candidate_identity)
    _require_file_identity(normalized_candidate, candidate_identity)
    if normalized_candidate.parent != normalized_root:
        raise ComposeDiscoveryError("candidate_outside_root")
    _require_root_identity(root, normalized_root, root_identity)
    _require_file_identity(candidate, candidate_identity)
    _require_file_identity(normalized_candidate, candidate_identity)
    return normalized_candidate


def _require_root_identity(
    root: Path, normalized_root: Path, expected: _PathIdentity
) -> None:
    _require_directory_identity(root, expected)
    _require_directory_identity(normalized_root, expected)


def _require_directory_identity(path: Path, expected: _PathIdentity) -> None:
    current = _lstat(path)
    if current is None or not _is_directory(current) or _identity(current) != expected:
        raise ComposeDiscoveryError("invalid_root")


def _require_file_identity(path: Path, expected: _PathIdentity) -> None:
    current = _lstat(path)
    if (
        current is None
        or not _is_regular_file(current)
        or _identity(current) != expected
    ):
        raise ComposeDiscoveryError("invalid_candidate")


def _identity(stat_result: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        file_type=stat.S_IFMT(stat_result.st_mode),
    )


def _is_directory(stat_result: os.stat_result) -> bool:
    return stat.S_ISDIR(stat_result.st_mode) and not _is_reparse_point(stat_result)


def _is_regular_file(stat_result: os.stat_result) -> bool:
    return stat.S_ISREG(stat_result.st_mode) and not _is_reparse_point(stat_result)


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(reparse_flag and file_attributes & reparse_flag)
