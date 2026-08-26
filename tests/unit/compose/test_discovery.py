import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from repotrial.intake.compose_discovery import (
    AmbiguousComposeError,
    ComposeDiscoveryError,
    ComposeNotFoundError,
    discover_compose,
)


@dataclass(frozen=True)
class _StatWithUnavailableInode:
    st_mode: int
    st_ino: int
    st_dev: int
    st_size: int
    st_file_attributes: int


def _with_unavailable_inode(stat_result: os.stat_result) -> _StatWithUnavailableInode:
    return _StatWithUnavailableInode(
        st_mode=stat_result.st_mode,
        st_ino=0,
        st_dev=stat_result.st_dev,
        st_size=stat_result.st_size,
        st_file_attributes=getattr(stat_result, "st_file_attributes", 0) or 0,
    )


def test_discover_compose_uses_standard_name_priority(tmp_path: Path) -> None:
    for filename in (
        "compose.yml",
        "compose.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
    ):
        (tmp_path / filename).write_text("services: {}\n", encoding="utf-8")

    assert discover_compose(tmp_path) == (tmp_path / "compose.yml").resolve()


def test_discover_compose_returns_one_nonstandard_root_level_candidate(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "project-COMPOSE.YAML"
    candidate.write_text("services: {}\n", encoding="utf-8")

    discovered = discover_compose(tmp_path)

    assert discovered == candidate.resolve()
    assert discovered.is_absolute()
    assert discovered.is_relative_to(tmp_path.resolve())


def test_discover_compose_rejects_missing_or_multiple_fallback_candidates(
    tmp_path: Path,
) -> None:
    with pytest.raises(ComposeNotFoundError):
        discover_compose(tmp_path)

    (tmp_path / "frontend-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "backend-compose.yaml").write_text("services: {}\n", encoding="utf-8")

    with pytest.raises(AmbiguousComposeError):
        discover_compose(tmp_path)


def test_discover_compose_rejects_invalid_root_and_never_recurses(
    tmp_path: Path,
) -> None:
    non_directory = tmp_path / "not-a-directory"
    non_directory.write_text("services: {}\n", encoding="utf-8")

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(non_directory)

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "compose.yml").write_text("services: {}\n", encoding="utf-8")

    with pytest.raises(ComposeNotFoundError):
        discover_compose(tmp_path)


def _assert_sanitized_discovery_error(
    error: ComposeDiscoveryError, marker: str, reason: str
) -> None:
    assert str(error) == f"compose discovery failed: {reason}"
    assert marker not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__


def test_discover_compose_sanitizes_nul_root_before_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    untrusted_marker = "untrusted-nul-root-marker"
    traversals: list[str] = []

    def unexpected_resolve(path: Path, *arguments: object, **keywords: object) -> Path:
        traversals.append("resolve")
        raise AssertionError(f"unexpected resolution: {path}")

    def unexpected_iterdir(path: Path) -> object:
        traversals.append("iterdir")
        raise AssertionError(f"unexpected enumeration: {path}")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    monkeypatch.setattr(Path, "iterdir", unexpected_iterdir)

    with pytest.raises(ComposeDiscoveryError) as error:
        discover_compose(Path(f"{untrusted_marker}\0.yml"))

    _assert_sanitized_discovery_error(error.value, untrusted_marker, "path_unreadable")
    assert traversals == []


def test_discover_compose_sanitizes_root_resolution_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untrusted_marker = "root-resolution-marker"
    original_resolve = Path.resolve

    def invalid_root_resolve(
        path: Path, *arguments: object, **keywords: object
    ) -> Path:
        if path == tmp_path:
            raise ValueError(untrusted_marker)
        return original_resolve(path, *arguments, **keywords)

    monkeypatch.setattr(Path, "resolve", invalid_root_resolve)

    with pytest.raises(ComposeDiscoveryError) as error:
        discover_compose(tmp_path)

    _assert_sanitized_discovery_error(error.value, untrusted_marker, "invalid_root")


def test_discover_compose_sanitizes_fallback_enumeration_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untrusted_marker = "fallback-enumeration-marker"
    original_iterdir = Path.iterdir

    def invalid_root_iterdir(path: Path) -> object:
        if path == tmp_path:
            raise ValueError(untrusted_marker)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", invalid_root_iterdir)

    with pytest.raises(ComposeDiscoveryError) as error:
        discover_compose(tmp_path)

    _assert_sanitized_discovery_error(error.value, untrusted_marker, "root_unreadable")


def test_discover_compose_sanitizes_candidate_resolution_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "compose.yml"
    candidate.write_text("services: {}\n", encoding="utf-8")
    untrusted_marker = "candidate-resolution-marker"
    original_resolve = Path.resolve

    def invalid_candidate_resolve(
        path: Path, *arguments: object, **keywords: object
    ) -> Path:
        if path == candidate:
            raise ValueError(untrusted_marker)
        return original_resolve(path, *arguments, **keywords)

    monkeypatch.setattr(Path, "resolve", invalid_candidate_resolve)

    with pytest.raises(ComposeDiscoveryError) as error:
        discover_compose(tmp_path)

    _assert_sanitized_discovery_error(
        error.value, untrusted_marker, "candidate_unreadable"
    )


def test_discover_compose_rejects_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    os.symlink(target, linked_root, target_is_directory=True)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(linked_root)


def test_discover_compose_fails_closed_for_invalid_standard_or_fallback_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yml").mkdir()
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)

    (tmp_path / "compose.yml").rmdir()
    (tmp_path / "compose.yaml").unlink()
    target = tmp_path / "target.yml"
    target.write_text("services: {}\n", encoding="utf-8")
    os.symlink(target, tmp_path / "project-compose.yml")

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)


def test_discover_compose_rejects_root_replaced_after_initial_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    attacker_root = tmp_path / "attacker-root"
    attacker_root.mkdir()
    (attacker_root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    original_lstat = Path.lstat
    swapped = False

    def lstat_then_swap(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == root and not swapped:
            swapped = True
            (root / "compose.yml").unlink()
            root.rmdir()
            os.symlink(attacker_root, root, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_then_swap)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(root)


def test_discover_compose_rejects_candidate_replaced_after_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "compose.yml"
    candidate.write_text("services: {}\n", encoding="utf-8")
    attacker_candidate = tmp_path / "attacker.yml"
    attacker_candidate.write_text("services: {}\n", encoding="utf-8")
    original_lstat = Path.lstat
    swapped = False

    def lstat_then_swap(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == candidate and not swapped:
            swapped = True
            candidate.unlink()
            os.symlink(attacker_candidate, candidate)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_then_swap)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)


def test_discover_compose_rejects_root_replaced_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "compose.yml"
    candidate.write_text("services: {}\n", encoding="utf-8")
    attacker_root = tmp_path / "attacker-root"
    attacker_root.mkdir()
    os.link(candidate, attacker_root / "compose.yml")
    original_lstat = Path.lstat
    candidate_lstat_calls = 0

    def lstat_then_swap_root(path: Path) -> os.stat_result:
        nonlocal candidate_lstat_calls
        result = original_lstat(path)
        if path == candidate:
            candidate_lstat_calls += 1
            if candidate_lstat_calls == 3:
                candidate.unlink()
                root.rmdir()
                os.symlink(attacker_root, root, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_then_swap_root)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(root)


def test_discover_compose_rejects_candidate_replaced_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "compose.yml"
    candidate.write_text("services: {}\n", encoding="utf-8")
    replacement = tmp_path / "replacement.yml"
    replacement.write_text("services: {app: {image: attacker}}\n", encoding="utf-8")
    original_lstat = Path.lstat
    candidate_lstat_calls = 0

    def lstat_then_replace(path: Path) -> os.stat_result:
        nonlocal candidate_lstat_calls
        result = original_lstat(path)
        if path == candidate:
            candidate_lstat_calls += 1
            if candidate_lstat_calls == 3:
                candidate.unlink()
                replacement.replace(candidate)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_then_replace)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)


def test_discover_compose_rejects_fallback_replaced_by_different_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "project-compose.yml"
    candidate.write_text("services: {}\n", encoding="utf-8")
    replacement = tmp_path / "replacement.yml"
    replacement.write_text("services: {app: {image: attacker}}\n", encoding="utf-8")
    original_lstat = Path.lstat
    swapped = False

    def lstat_then_replace(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == candidate and not swapped:
            swapped = True
            candidate.unlink()
            replacement.replace(candidate)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_then_replace)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)


def test_discover_compose_rejects_static_root_without_usable_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    original_lstat = Path.lstat

    def lstat_with_unavailable_inode(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path == root:
            return _with_unavailable_inode(result)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_unavailable_inode)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(root)


def test_discover_compose_rejects_zero_inode_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "compose.yml").write_text("services: {origin: {}}\n", encoding="utf-8")
    attacker_root = tmp_path / "attacker-root"
    attacker_root.mkdir()
    (attacker_root / "compose.yml").write_text(
        "services: {attacker: {}}\n", encoding="utf-8"
    )
    original_lstat = Path.lstat
    swapped = False

    def lstat_with_unavailable_inode(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == root:
            if not swapped:
                swapped = True
                root.replace(tmp_path / "original-root")
                attacker_root.replace(root)
            return _with_unavailable_inode(result)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_unavailable_inode)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(root)


def test_discover_compose_rejects_zero_inode_standard_candidate_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "compose.yml"
    candidate.write_text("services: {origin: {}}\n", encoding="utf-8")
    replacement = tmp_path / "replacement.yml"
    replacement.write_text("services: {attacker: {}}\n", encoding="utf-8")
    original_lstat = Path.lstat
    swapped = False

    def lstat_with_unavailable_inode(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == candidate:
            if not swapped:
                swapped = True
                candidate.unlink()
                replacement.replace(candidate)
            return _with_unavailable_inode(result)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_unavailable_inode)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)


def test_discover_compose_rejects_zero_inode_fallback_candidate_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "project-compose.yml"
    candidate.write_text("services: {origin: {}}\n", encoding="utf-8")
    replacement = tmp_path / "replacement.yml"
    replacement.write_text("services: {attacker: {}}\n", encoding="utf-8")
    original_lstat = Path.lstat
    swapped = False

    def lstat_with_unavailable_inode(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == candidate:
            if not swapped:
                swapped = True
                candidate.unlink()
                replacement.replace(candidate)
            return _with_unavailable_inode(result)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_unavailable_inode)

    with pytest.raises(ComposeDiscoveryError):
        discover_compose(tmp_path)
