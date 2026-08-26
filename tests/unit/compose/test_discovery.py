import os
from pathlib import Path

import pytest

from repotrial.intake.compose_discovery import (
    AmbiguousComposeError,
    ComposeDiscoveryError,
    ComposeNotFoundError,
    discover_compose,
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
