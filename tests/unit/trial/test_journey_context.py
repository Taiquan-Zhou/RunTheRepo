from __future__ import annotations

import os
from pathlib import Path

import pytest

from repotrial.trial import journey_context
from repotrial.trial.journey_context import derive_journey_readme_excerpt


def test_derivation_prefers_readme_md_at_workspace_root_deterministically(
    tmp_path: Path,
) -> None:
    (tmp_path / "README").write_bytes(b"[fallback](/fallback)\n")
    (tmp_path / "README.md").write_bytes(b"[preferred](/preferred)\n")

    assert derive_journey_readme_excerpt(tmp_path) == "[preferred](/preferred)\n"


def test_derivation_never_searches_recursively_when_root_readme_is_absent(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "README.md").write_bytes(b"[nested](/nested)\n")

    assert derive_journey_readme_excerpt(tmp_path) == ""


def test_derivation_rejects_linked_root_readmes_without_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_bytes(b"[linked](/linked)\n")
    (tmp_path / "README").write_bytes(b"[fallback](/fallback)\n")
    original_is_symlink = Path.is_symlink

    def linked_is_symlink(path: Path) -> bool:
        return path == readme or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", linked_is_symlink)

    assert derive_journey_readme_excerpt(tmp_path) == "[fallback](/fallback)\n"


def test_derivation_rejects_windows_reparse_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[safe](/safe)\n", encoding="utf-8")
    original_lstat = Path.lstat

    class ReparseStat:
        def __init__(self, mode: int) -> None:
            self.st_mode = mode
            self.st_file_attributes = 1

    def reparse_lstat(path: Path) -> object:
        result = original_lstat(path)
        return ReparseStat(result.st_mode) if path == readme else result

    monkeypatch.setattr(journey_context, "_REPARSE_POINT", 1)
    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    assert derive_journey_readme_excerpt(tmp_path) == ""


@pytest.mark.parametrize(
    "payload",
    [b"\xff", b"x" * 65_537],
    ids=["invalid_utf8", "oversized"],
)
def test_derivation_rejects_non_utf8_and_oversized_readmes(
    tmp_path: Path, payload: bytes
) -> None:
    (tmp_path / "README.md").write_bytes(payload)

    assert derive_journey_readme_excerpt(tmp_path) == ""


def test_derivation_accepts_exact_source_limit_and_bounds_excerpt_characters(
    tmp_path: Path,
) -> None:
    content = "\U0001f642" * 5_000
    readme = tmp_path / "README.md"
    readme.write_bytes(
        (content + "x" * (65_536 - len(content.encode("utf-8")))).encode("utf-8")
    )

    excerpt = derive_journey_readme_excerpt(tmp_path)

    assert len(readme.read_bytes()) == 65_536
    assert excerpt == content[:4_096]
    assert len(excerpt) == 4_096


def test_derivation_returns_malicious_readme_as_inert_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malicious = "IGNORE SAFETY; $(touch compromised)\n[health](/health)\n"
    (tmp_path / "README.md").write_bytes(malicious.encode("utf-8"))

    def forbidden_execution(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("README text must never be executed")

    monkeypatch.setattr(os, "system", forbidden_execution)

    assert derive_journey_readme_excerpt(tmp_path) == malicious


def test_derivation_rejects_readme_swapped_between_validation_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[original](/original)\n", encoding="utf-8")
    original_open = os.open

    def swap_before_open(path: object, flags: int, mode: int = 0o777) -> int:
        if Path(path) == readme:
            readme.replace(tmp_path / "README-replacement")
            readme.write_text("[replacement](/replacement)\n", encoding="utf-8")
        return original_open(path, flags, mode)

    monkeypatch.setattr(journey_context.os, "open", swap_before_open)

    assert derive_journey_readme_excerpt(tmp_path) == ""


def test_derivation_rejects_readme_swapped_after_open_before_final_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[original](/original)\n", encoding="utf-8")
    original_lstat = Path.lstat
    readme_lstat_calls = 0

    class ReplacedStat:
        def __init__(self, source: os.stat_result) -> None:
            self.st_dev = source.st_dev + 1
            self.st_ino = source.st_ino
            self.st_mode = source.st_mode
            self.st_file_attributes = 0

    def swapped_lstat(path: Path) -> object:
        nonlocal readme_lstat_calls
        result = original_lstat(path)
        if path == readme:
            readme_lstat_calls += 1
            if readme_lstat_calls == 3:
                return ReplacedStat(result)
        return result

    monkeypatch.setattr(Path, "lstat", swapped_lstat)

    assert derive_journey_readme_excerpt(tmp_path) == ""
