from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_sdist_excludes_local_state_and_keeps_project_sources(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    for filename in ("pyproject.toml", "README.md"):
        shutil.copy2(REPOSITORY_ROOT / filename, project_root / filename)
    for directory in ("src", "tests"):
        shutil.copytree(REPOSITORY_ROOT / directory, project_root / directory)

    excluded_paths = (
        ".git",
        ".env",
        ".env.example",
        ".coverage",
        ".coverage.worker",
        ".github/workflows/ci.yml",
        ".superpowers/sdd/review.md",
        ".venv/sentinel",
        ".idea/workspace.xml",
        ".mypy_cache/sentinel",
        ".pytest_cache/sentinel",
        ".ruff_cache/sentinel",
        ".vscode/settings.json",
        "artifacts/sentinel",
        "AGENTS.md",
        "build/sentinel",
        "coverage.xml",
        "dist/sentinel",
        "docs/project/spec.docx",
        "htmlcov/index.html",
        "repotrial.egg-info/sentinel",
        "src/repotrial.egg-info/PKG-INFO",
        "src/repotrial/.env",
        "src/repotrial/debug.log",
        "tests/.coverage",
        "tests/.idea/workspace.xml",
        "uv.lock",
    )
    for relative_path in excluded_paths:
        sentinel = project_root / relative_path
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("must not ship\n", encoding="utf-8")

    output_dir = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(output_dir)],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    [archive_path] = output_dir.glob("*.tar.gz")

    with tarfile.open(archive_path, "r:gz") as archive:
        members = {
            PurePosixPath(*PurePosixPath(member.name).parts[1:])
            for member in archive.getmembers()
        }

    excluded_members = {PurePosixPath(path) for path in excluded_paths}
    assert excluded_members.isdisjoint(members), excluded_members & members
    expected_members = {
        PurePosixPath("README.md"),
        PurePosixPath("pyproject.toml"),
        PurePosixPath("PKG-INFO"),
        PurePosixPath("src/repotrial/report/templates/report.html.j2"),
        *(
            PurePosixPath(path.relative_to(project_root).as_posix())
            for source_root in (project_root / "src", project_root / "tests")
            for path in source_root.rglob("*.py")
        ),
    }
    assert expected_members <= members
