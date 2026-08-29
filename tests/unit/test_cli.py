from pathlib import Path

import pytest
from typer import Typer
from typer.testing import CliRunner

from repotrial import cli
from repotrial.cli import create_app
from repotrial.config import create_run_layout
from repotrial.domain.enums import Verdict
from repotrial.domain.models import JourneyResult, RunState
from repotrial.run_outcome import TerminalOutcome, classify_terminal_outcome
from repotrial.sandbox.base import SandboxProvider

FIXED_RUN_ID = "11111111-1111-4111-8111-111111111111"


def fixed_run_id() -> str:
    return FIXED_RUN_ID


def make_app(artifacts_root: Path) -> Typer:
    return create_app(artifacts_root=artifacts_root, run_id_generator=fixed_run_id)


def test_doctor_reports_a_healthy_result(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(make_app(artifacts_root), ["doctor"])

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert not artifacts_root.exists()


def test_dry_run_inspect_creates_the_required_run_layout(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    artifact_path = artifacts_root / FIXED_RUN_ID

    result = CliRunner().invoke(
        make_app(artifacts_root),
        ["inspect", "--dry-run", "https://github.com/a/b"],
    )

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"run_id={FIXED_RUN_ID}",
        f"artifact_path={artifact_path}",
    ]
    run_directories = list(artifact_path.iterdir())
    assert {path.name for path in run_directories} == {
        "evidence",
        "experiments",
        "report",
    }
    assert all(path.is_dir() for path in run_directories)


def test_dry_run_accepts_case_insensitive_github_hostname_and_git_suffix(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root),
        ["inspect", "--dry-run", "https://GitHub.com/a/b.git"],
    )

    assert result.exit_code == 0
    assert (artifacts_root / FIXED_RUN_ID).is_dir()


@pytest.mark.parametrize(
    "url",
    [
        "github.com/a/b",
        "https://github.com/a",
        "http://github.com/a/b",
        "https://github.com:443/a/b",
        "https://github.com:/a/b",
        "https://user@github.com/a/b",
        "https://github.com/a/b/",
        "https://github.com/a/b/c",
        "https://github.com/a/b?branch=main",
        "https://github.com/a/b#readme",
        "https://github.com/a/b?",
        "https://github.com/a/b#",
        "https://github.com/a/b ",
        "https://github.com/a/b\n",
        "https://github.com/a/b|invalid",
        "https://github.com/a/b%zz",
        "https://github.com/./repo",
        "https://github.com/owner/..",
        "https://github.com/%2e/repo",
        "https://github.com/owner/%2e%2e",
        "https://github.com/owner%2Frepo/project",
        "https://github.com/owner/repo%2Fsub",
        "https://github.com/owner%5Crepo/project",
        "https://github.com/owner/repo%5Csub",
    ],
)
def test_malformed_inspect_url_creates_no_artifacts(tmp_path: Path, url: str) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(make_app(artifacts_root), ["inspect", "--dry-run", url])

    assert result.exit_code != 0
    assert "run_id=" not in result.stdout
    assert not artifacts_root.exists()


def test_non_github_inspect_url_creates_no_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root),
        ["inspect", "--dry-run", "https://example.com/a/b"],
    )

    assert result.exit_code != 0
    assert not artifacts_root.exists()


def test_inspect_without_dry_run_creates_no_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root), ["inspect", "https://github.com/a/b"]
    )

    assert result.exit_code != 0
    assert not artifacts_root.exists()


def test_docker_sbx_inspect_requires_a_full_lowercase_commit_sha_before_artifacts(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root),
        ["inspect", "--provider", "docker-sbx", "https://github.com/a/b"],
    )

    assert result.exit_code != 0
    assert "--commit-sha" in result.output
    assert not artifacts_root.exists()


def test_cli_docker_sbx_default_keeps_a_1024_mb_memory_bound() -> None:
    assert cli._DEFAULT_DOCKER_SBX_POLICY.memory_mb == 1024


@pytest.mark.parametrize("commit_sha", ["A" * 40, "a" * 39, "g" * 40])
def test_dry_run_rejects_invalid_supplied_commit_sha_before_artifacts(
    tmp_path: Path, commit_sha: str
) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root),
        [
            "inspect",
            "--dry-run",
            "--commit-sha",
            commit_sha,
            "https://github.com/a/b",
        ],
    )

    assert result.exit_code != 0
    assert not artifacts_root.exists()


def test_invalid_container_port_fails_before_provider_or_artifact_use(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"

    def unexpected_provider(_name: str) -> SandboxProvider:
        raise AssertionError("invalid port reached provider construction")

    app = create_app(
        artifacts_root=artifacts_root,
        run_id_generator=fixed_run_id,
        provider_factory=unexpected_provider,
    )
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "--provider",
            "fake",
            "--container-port",
            "0",
            "https://github.com/a/b",
        ],
    )

    assert result.exit_code != 0
    assert not artifacts_root.exists()


def test_run_layout_rejects_a_colliding_run_id(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"

    run_id, run_path = create_run_layout(artifacts_root, fixed_run_id)

    assert run_id == FIXED_RUN_ID
    assert run_path == artifacts_root / FIXED_RUN_ID
    with pytest.raises(FileExistsError):
        create_run_layout(artifacts_root, fixed_run_id)


def test_shared_terminal_classifier_preserves_success_exit_semantics() -> None:
    run = RunState(
        run_id="run-1",
        repo_url="https://github.com/a/b",
        stop_reason="no_more_mutations",
        baseline_journey_results=[
            JourneyResult(
                journey_id="journey-1",
                verdict=Verdict.PASS,
                passed_steps=1,
                total_steps=1,
            )
        ],
    )

    result = classify_terminal_outcome(run)

    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.exit_code == 0
