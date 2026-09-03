import json
from pathlib import Path
from typing import Self

import pytest
from typer import Typer
from typer.testing import CliRunner

from repotrial import cli
from repotrial.cli import create_app
from repotrial.config import create_run_layout
from repotrial.domain.enums import Verdict
from repotrial.domain.models import JourneyResult, RunState
from repotrial.intake import github
from repotrial.intake.github import RepoIntakeError
from repotrial.run_outcome import TerminalOutcome, classify_terminal_outcome
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import DockerSbxPolicy
from repotrial.sandbox.fake import FakeSandboxProvider

FIXED_RUN_ID = "11111111-1111-4111-8111-111111111111"


class _CredentialBearingStream:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.read_calls = 0
        self.requested_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        self.requested_sizes.append(size)
        if not self._content:
            return b""
        if size < 0:
            size = len(self._content)
        content, self._content = self._content[:size], self._content[size:]
        return content


class _CredentialBearingProcess:
    def __init__(self, stderr: bytes) -> None:
        self.stdout = _CredentialBearingStream(b"")
        self.stderr = _CredentialBearingStream(stderr)
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 128
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


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


def test_cli_docker_sbx_default_uses_supported_cpu_and_keeps_memory_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_policies: list[DockerSbxPolicy] = []
    provider = FakeSandboxProvider()

    def recording_provider(policy: DockerSbxPolicy) -> SandboxProvider:
        captured_policies.append(policy)
        return provider

    monkeypatch.setattr(cli, "DockerSbxProvider", recording_provider)

    assert cli._make_provider("docker-sbx", None) is provider
    assert len(captured_policies) == 1
    policy = captured_policies[0]
    assert policy.cpus == 1
    assert isinstance(policy.cpus, int)
    assert policy.memory_mb == 1024
    assert policy.pids_limit == 64
    assert policy.disk_mb == 8192
    assert policy.total_duration_s == 900


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


def _make_intake_failure_app(
    artifacts_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: BaseException,
    run_id: str = "run-fixed",
):
    async def failing_pinner(
        _url: str, _destination: Path, _requested_ref: str | None
    ) -> object:
        raise error

    monkeypatch.setattr(cli, "pin_repository", failing_pinner)
    return create_app(
        artifacts_root=artifacts_root,
        run_id_generator=lambda: run_id,
        provider_factory=lambda _name: FakeSandboxProvider(),
    )


def _invoke_intake_failure(app: Typer, *, commit_sha: str = "a" * 40):
    return CliRunner().invoke(
        app,
        [
            "inspect",
            "--provider",
            "fake",
            "--commit-sha",
            commit_sha,
            "https://github.com/owner/repository",
        ],
    )


def test_exact_sha_intake_failure_writes_private_sanitized_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    app = _make_intake_failure_app(
        artifacts_root,
        monkeypatch,
        error=RepoIntakeError("clone", 128, failure_class="dns"),
    )

    result = _invoke_intake_failure(app)

    assert result.exit_code == 4
    run_path = artifacts_root / "run-fixed"
    intake_failure = run_path / "intake-failure.json"
    assert json.loads(intake_failure.read_text(encoding="utf-8")) == {
        "failure_class": "dns",
        "operation": "clone",
        "returncode": 128,
        "run_id": "run-fixed",
        "schema_version": 1,
    }
    assert "raw" not in intake_failure.read_text(encoding="utf-8")
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert attempt["private_intake_evidence_status"] == "written"
    assert attempt["stop_reason"] == "intake:clone"
    assert attempt["exit_code"] == 4


def test_git_credential_stderr_is_redacted_across_cli_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    secret = "credential=do-not-persist-this-token"
    process = _CredentialBearingProcess(
        f"fatal: Authentication failed for {secret}\n".encode()
        + b"x" * (github._MAX_GIT_OUTPUT_BYTES + 1)
    )
    observed_exception_text: list[str] = []

    async def fake_create_subprocess_exec(
        *_arguments: object, **_keywords: object
    ) -> _CredentialBearingProcess:
        return process

    async def failing_pinner(
        _url: str, _destination: Path, _requested_ref: str | None
    ) -> object:
        try:
            await github._run_git("clone")
        except RepoIntakeError as error:
            observed_exception_text.append(str(error))
            raise
        raise AssertionError("fake Git process unexpectedly succeeded")

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(cli, "pin_repository", failing_pinner)
    app = create_app(
        artifacts_root=artifacts_root,
        run_id_generator=lambda: "run-fixed",
        provider_factory=lambda _name: FakeSandboxProvider(),
    )

    result = _invoke_intake_failure(app)

    run_path = artifacts_root / "run-fixed"
    intake_text = (run_path / "intake-failure.json").read_text(encoding="utf-8")
    attempt_text = (run_path / "attempt-result.json").read_text(encoding="utf-8")
    assert observed_exception_text == [
        "repository intake failed: clone (returncode=128)"
    ]
    for output in (
        intake_text,
        attempt_text,
        result.stdout,
        result.stderr,
        result.output,
    ):
        assert secret not in output
    assert json.loads(intake_text)["failure_class"] == "authentication"
    assert json.loads(attempt_text)["stop_reason"] == "intake:clone"
    assert process.stderr.read_calls > 1
    assert all(
        size <= github._GIT_READ_CHUNK_BYTES for size in process.stderr.requested_sizes
    )
    assert result.exit_code == 4


def test_intake_failure_artifact_collision_is_secondary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    existing_text = "existing private evidence"

    async def failing_pinner(
        _url: str, destination: Path, _requested_ref: str | None
    ) -> object:
        (destination.parent / "intake-failure.json").write_text(
            existing_text, encoding="utf-8"
        )
        raise RepoIntakeError("clone", 128, failure_class="dns")

    monkeypatch.setattr(cli, "pin_repository", failing_pinner)
    app = create_app(
        artifacts_root=artifacts_root,
        run_id_generator=lambda: "run-fixed",
        provider_factory=lambda _name: FakeSandboxProvider(),
    )

    result = _invoke_intake_failure(app)

    assert result.exit_code == 4
    run_path = artifacts_root / "run-fixed"
    assert (run_path / "intake-failure.json").read_text(encoding="utf-8") == (
        existing_text
    )
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert attempt["private_intake_evidence_status"] == "collision"
    assert attempt["stop_reason"] == "intake:clone"
    assert attempt["exit_code"] == 4


class _FlushFailingFile:
    name = "intake-failure.json"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def write(self, _value: str) -> int:
        return 0

    def flush(self) -> None:
        raise OSError("SECRET flush failure")

    def fileno(self) -> int:
        return 0


@pytest.mark.parametrize(
    "failure_mode", ["open", "write", "partial_write", "flush", "fsync"]
)
def test_intake_failure_evidence_writer_failure_is_secondary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    app = _make_intake_failure_app(
        artifacts_root,
        monkeypatch,
        error=RepoIntakeError("clone", 128, failure_class="dns"),
    )

    if failure_mode == "open":
        original_open = Path.open

        def fail_intake_open(path: Path, *args: object, **kwargs: object) -> object:
            if path.name == "intake-failure.json":
                raise OSError("SECRET open failure")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_intake_open)
    elif failure_mode == "write":
        original_dump = cli.json.dump

        def fail_intake_dump(
            value: object, stream: object, *args: object, **kwargs: object
        ) -> object:
            if Path(str(getattr(stream, "name", ""))).name == "intake-failure.json":
                raise OSError("SECRET write failure")
            return original_dump(value, stream, *args, **kwargs)

        monkeypatch.setattr(cli.json, "dump", fail_intake_dump)
    elif failure_mode == "partial_write":

        def partial_intake_dump(
            _value: object, stream: object, *args: object, **kwargs: object
        ) -> object:
            del args, kwargs
            stream.write('{"partial":"SECRET partial write')
            raise OSError("SECRET partial write failure")

        monkeypatch.setattr(cli.json, "dump", partial_intake_dump)
    elif failure_mode == "fsync":

        def fail_intake_fsync(_file_descriptor: int) -> None:
            raise OSError("SECRET fsync failure")

        monkeypatch.setattr(cli.os, "fsync", fail_intake_fsync)
    else:
        original_open = Path.open

        def flush_failure_open(path: Path, *args: object, **kwargs: object) -> object:
            if path.name == "intake-failure.json":
                return _FlushFailingFile()
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", flush_failure_open)

    result = _invoke_intake_failure(app)

    assert result.exit_code == 4
    run_path = artifacts_root / "run-fixed"
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert attempt["private_intake_evidence_status"] == "write_failed"
    assert attempt["stop_reason"] == "intake:clone"
    assert attempt["exit_code"] == 4
    assert not (run_path / "intake-failure.json").exists()


def test_intake_failure_partial_cleanup_error_is_secondary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    app = _make_intake_failure_app(
        artifacts_root,
        monkeypatch,
        error=RepoIntakeError("clone", 128, failure_class="dns"),
    )

    def partial_intake_dump(
        _value: object, stream: object, *args: object, **kwargs: object
    ) -> object:
        del args, kwargs
        stream.write('{"partial":"incomplete evidence')
        raise OSError("SECRET partial write failure")

    original_unlink = Path.unlink

    def fail_intake_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "intake-failure.json":
            raise OSError("SECRET unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(cli.json, "dump", partial_intake_dump)
    monkeypatch.setattr(Path, "unlink", fail_intake_unlink)

    result = _invoke_intake_failure(app)

    assert result.exit_code == 4
    run_path = artifacts_root / "run-fixed"
    intake_failure = run_path / "intake-failure.json"
    assert intake_failure.exists()
    assert intake_failure.read_text(encoding="utf-8") == (
        '{"partial":"incomplete evidence'
    )
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert attempt["private_intake_evidence_status"] == "write_failed"
    assert attempt["stop_reason"] == "intake:clone"
    assert attempt["exit_code"] == 4


def test_ordinary_exception_does_not_create_private_intake_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    app = _make_intake_failure_app(
        artifacts_root,
        monkeypatch,
        error=ValueError("SECRET ordinary failure"),
    )

    result = _invoke_intake_failure(app)

    assert result.exit_code == 4
    run_path = artifacts_root / "run-fixed"
    assert not (run_path / "intake-failure.json").exists()
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert "private_intake_evidence_status" not in attempt
    assert attempt["stop_reason"] == "internal:valueerror"
    assert attempt["exit_code"] == 4


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
