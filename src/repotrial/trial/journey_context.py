from __future__ import annotations

import os
import stat
from pathlib import Path

_MAX_SOURCE_BYTES = 65_536
_MAX_EXCERPT_CHARACTERS = 4_096
_ROOT_README_NAMES = ("README.md", "README")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def derive_journey_readme_excerpt(workspace: Path) -> str:
    """Return a bounded, inert root README excerpt from a pinned workspace."""
    if not _is_real_directory(workspace):
        return ""
    for name in _ROOT_README_NAMES:
        content = _read_bounded_root_file(workspace / name)
        if content is not None:
            return content[:_MAX_EXCERPT_CHARACTERS]
    return ""


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not _is_link(path, metadata)


def _read_bounded_root_file(path: Path) -> str | None:
    try:
        initial = path.lstat()
    except OSError:
        return None
    if not _is_regular_file(path, initial) or initial.st_size > _MAX_SOURCE_BYTES:
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not _same_file(initial, opened) or not _same_file(opened, current):
            return None
        if not _is_regular_file(path, current) or opened.st_size > _MAX_SOURCE_BYTES:
            return None
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = None
            raw = source.read(_MAX_SOURCE_BYTES + 1)
            final_handle = os.fstat(source.fileno())
        final_path = path.lstat()
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        len(raw) > _MAX_SOURCE_BYTES
        or not _same_file(opened, final_handle)
        or not _same_file(final_handle, final_path)
        or not _is_regular_file(path, final_path)
    ):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _is_regular_file(path: Path, metadata: os.stat_result) -> bool:
    return stat.S_ISREG(metadata.st_mode) and not _is_link(path, metadata)


def _is_link(path: Path, metadata: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )
