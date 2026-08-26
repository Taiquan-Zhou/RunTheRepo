"""Deterministic discovery of a root-level Compose file."""

import stat
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


def discover_compose(root: Path) -> Path:
    """Return the one eligible root-level Compose file for ``root``."""
    normalized_root = _normalize_root(root)

    for filename in _STANDARD_COMPOSE_FILENAMES:
        candidate = normalized_root / filename
        candidate_mode = _lstat_mode(candidate)
        if candidate_mode is None:
            continue
        if not stat.S_ISREG(candidate_mode):
            raise ComposeDiscoveryError("invalid_standard_file")
        return _normalize_candidate(normalized_root, candidate)

    fallback_candidates: list[Path] = []
    try:
        direct_children = list(normalized_root.iterdir())
    except OSError:
        raise ComposeDiscoveryError("root_unreadable") from None
    for candidate in direct_children:
        if not _is_fallback_candidate_name(candidate):
            continue
        candidate_mode = _lstat_mode(candidate)
        if candidate_mode is None or not stat.S_ISREG(candidate_mode):
            raise ComposeDiscoveryError("invalid_fallback_file")
        fallback_candidates.append(candidate)

    if not fallback_candidates:
        raise ComposeNotFoundError("not_found")
    if len(fallback_candidates) > 1:
        raise AmbiguousComposeError("ambiguous")
    return _normalize_candidate(normalized_root, fallback_candidates[0])


def _normalize_root(root: Path) -> Path:
    root_mode = _lstat_mode(root)
    if root_mode is None or not stat.S_ISDIR(root_mode):
        raise ComposeDiscoveryError("invalid_root")
    try:
        normalized_root = root.resolve(strict=True)
    except OSError:
        raise ComposeDiscoveryError("invalid_root") from None
    if normalized_root.is_symlink():
        raise ComposeDiscoveryError("invalid_root")
    return normalized_root


def _is_fallback_candidate_name(candidate: Path) -> bool:
    return (
        "compose" in candidate.name.casefold()
        and candidate.suffix.casefold() in _COMPOSE_SUFFIXES
    )


def _lstat_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError:
        raise ComposeDiscoveryError("path_unreadable") from None


def _normalize_candidate(root: Path, candidate: Path) -> Path:
    try:
        normalized_candidate = candidate.resolve(strict=True)
    except OSError:
        raise ComposeDiscoveryError("candidate_unreadable") from None
    if normalized_candidate.parent != root:
        raise ComposeDiscoveryError("candidate_outside_root")
    return normalized_candidate
