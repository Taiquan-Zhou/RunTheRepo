from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from dataclasses import replace
from pathlib import Path

import pytest

from repotrial.local_web.runner import CliRequest, CliRunner, _read_owned_file

RUN_ID = "11111111-1111-4111-8111-111111111111"


class FakeStream:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if not self.content:
            return b""
        chunk, self.content = self.content[:size], self.content[size:]
        return chunk


class FakeProcess:
    def __init__(
        self, *, returncode: int = 0, wait_event: asyncio.Event | None = None
    ) -> None:
        self.stdout = FakeStream(f"run_id={RUN_ID}\n".encode() + b"x" * 100_000)
        self.stderr = FakeStream(b"secret-looking stderr\n" + b"y" * 100_000)
        self.returncode: int | None = None
        self._final_returncode = returncode
        self.wait_event = wait_event
        self.signals: list[int] = []
        self.waited = False

    async def wait(self) -> int:
        if self.wait_event is not None:
            await self.wait_event.wait()
        self.returncode = self._final_returncode
        self.waited = True
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        if self.wait_event is not None:
            self.wait_event.set()


def request() -> CliRequest:
    return CliRequest(
        url="https://github.com/acme/demo",
        commit_sha="c" * 40,
        container_port=4321,
        compose_path="deploy/compose.yml",
        model_endpoint="https://model.example/v1",
        model_name="model-x",
    )


def write_terminal_artifacts(
    root: Path,
    *,
    matching: bool = True,
    exit_code: int = 0,
    terminal_outcome: str = "completed",
) -> None:
    run_path = root / "artifacts" / RUN_ID
    (run_path / "report").mkdir(parents=True)
    evidence = {
        "run_id": RUN_ID,
        "repository_url": "https://github.com/acme/demo",
        "expected_sha": "c" * 40 if matching else "d" * 40,
        "actual_verified_sha": "c" * 40,
        "container_port": 4321,
        "compose_path": "deploy/compose.yml",
        "exit_code": exit_code,
        "terminal_outcome": terminal_outcome,
        "stop_reason": "completed",
    }
    report = {
        "identity": {
            "run_id": RUN_ID,
            "repo_url": "https://github.com/acme/demo",
            "commit_sha": "c" * 40,
        }
    }
    (run_path / "attempt-result.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )
    (run_path / "report" / "trial-report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    (run_path / "report" / "trial-report.html").write_text(
        "<html></html>", encoding="utf-8"
    )


def write_early_failure_evidence(
    root: Path,
    *,
    actual_verified_sha: str | None = None,
    with_report: bool = False,
    report_commit_sha: str | None = None,
    failure_evidence: dict[str, object] | None = None,
) -> None:
    run_path = root / "artifacts" / RUN_ID
    run_path.mkdir(parents=True)
    evidence: dict[str, object] = {
        "run_id": RUN_ID,
        "repository_url": "https://github.com/acme/demo",
        "expected_sha": "c" * 40,
        "actual_verified_sha": actual_verified_sha,
        "container_port": 4321,
        "compose_path": "deploy/compose.yml",
        "exit_code": 4,
        "terminal_outcome": "exception",
        "stop_reason": "intake:clone_timeout",
    }
    if failure_evidence is not None:
        evidence["failure_evidence"] = failure_evidence
    (run_path / "attempt-result.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )
    if with_report:
        report_dir = run_path / "report"
        report_dir.mkdir()
        report = {
            "report_kind": "execution_failure",
            "completed": False,
            "identity": {
                "run_id": RUN_ID,
                "repo_url": "https://github.com/acme/demo",
                "commit_sha": report_commit_sha,
                "expected_commit_sha": "c" * 40,
                "actual_verified_commit_sha": actual_verified_sha,
            },
        }
        (report_dir / "trial-report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        (report_dir / "trial-report.html").write_text(
            "<html>failure</html>", encoding="utf-8"
        )


def test_cli_request_preserves_exact_arguments(tmp_path: Path) -> None:
    runner = CliRunner(tmp_path, executable="/venv/bin/python")
    assert runner.argv(request()) == [
        "/venv/bin/python",
        "-I",
        "-m",
        "repotrial",
        "inspect",
        "https://github.com/acme/demo",
        "--provider",
        "docker-sbx",
        "--commit-sha",
        "c" * 40,
        "--container-port",
        "4321",
        "--compose-path",
        "deploy/compose.yml",
        "--model-endpoint",
        "https://model.example/v1",
        "--model-name",
        "model-x",
    ]


def test_submit_projects_only_matching_terminal_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path)
    process = FakeProcess()
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*argv: object, **kwargs: object) -> FakeProcess:
        calls.append(argv)
        assert kwargs["cwd"] == tmp_path.resolve()
        assert "shell" not in kwargs
        assert kwargs["start_new_session"] is True
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path, max_output_bytes=128)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "completed"
    assert status["stop_reason"] == "completed"
    assert status["reports"]
    assert calls[0][0:5] == tuple(runner.argv(request())[:5])
    assert max(process.stdout.read_sizes) <= 4096


def test_model_key_is_only_passed_to_trusted_cli_environment(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path)
    process = FakeProcess()
    captured: dict[str, object] = {}

    async def fake_exec(*argv: object, **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    request_with_key = replace(request(), model_api_key="secret")
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request_with_key)
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert "secret" not in repr(captured["argv"])
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["REPOTRIAL_MODEL_API_KEY"] == "secret"
    assert "secret" not in repr(status)
    assert status["request"] == {
        "url": request().url,
        "commit_sha": request().commit_sha,
        "container_port": request().container_port,
        "compose_path": request().compose_path,
    }
    assert "model_endpoint" not in status["request"]
    assert "model_name" not in status["request"]


def test_mismatched_terminal_identity_cannot_succeed(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path, matching=False)
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    assert asyncio.run(exercise())["state"] == "failed"


def test_early_failure_exposes_validated_diagnostics_without_verified_sha(
    monkeypatch, tmp_path: Path
) -> None:
    write_early_failure_evidence(
        tmp_path,
        with_report=True,
        failure_evidence={
            "operation": "clone",
            "reason": "timeout",
            "returncode": 124,
        },
    )
    process = FakeProcess(returncode=4)

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "failed"
    assert status["stop_reason"] == "intake:clone_timeout"
    assert status["failure_evidence"] == {
        "operation": "clone",
        "reason": "timeout",
        "returncode": 124,
    }
    assert "reports" not in status


@pytest.mark.parametrize("report_mutation", ["missing", "corrupt"])
def test_completed_evidence_without_valid_report_does_not_project_success_reason(
    monkeypatch, tmp_path: Path, report_mutation: str
) -> None:
    write_terminal_artifacts(tmp_path)
    report_dir = tmp_path / "artifacts" / RUN_ID / "report"
    if report_mutation == "missing":
        (report_dir / "trial-report.json").unlink()
    else:
        (report_dir / "trial-report.json").write_text("not-json", encoding="utf-8")
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "failed"
    assert status["error"] == "terminal evidence unavailable"
    assert "stop_reason" not in status


def test_report_requires_verified_evidence_sha_even_when_failure_report_identity_matches(
    monkeypatch, tmp_path: Path
) -> None:
    write_early_failure_evidence(
        tmp_path,
        with_report=True,
        report_commit_sha="c" * 40,
    )
    process = FakeProcess(returncode=4)

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "failed"
    assert status["stop_reason"] == "intake:clone_timeout"
    assert "reports" not in status


def test_early_failure_diagnostics_do_not_require_report_files(
    monkeypatch, tmp_path: Path
) -> None:
    write_early_failure_evidence(
        tmp_path,
        failure_evidence={"operation": "clone", "reason": "timeout"},
    )
    process = FakeProcess(returncode=4)

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "failed"
    assert status["stop_reason"] == "intake:clone_timeout"
    assert status["failure_evidence"] == {
        "operation": "clone",
        "reason": "timeout",
    }
    assert "reports" not in status


def test_failure_diagnostics_reject_mismatched_verified_sha(
    monkeypatch, tmp_path: Path
) -> None:
    write_early_failure_evidence(tmp_path, actual_verified_sha="d" * 40)
    process = FakeProcess(returncode=4)

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "failed"
    assert "stop_reason" not in status
    assert "failure_stage" not in status
    assert "failure_evidence" not in status


def test_mismatched_identity_does_not_publish_reports(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path, matching=False)
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> tuple[str, dict[str, object]]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return job_id, await runner.status(job_id)

    job_id, status = asyncio.run(exercise())
    assert "reports" not in status
    assert asyncio.run(runner.report(job_id, "json")) is None


def test_nonzero_cli_with_valid_failed_evidence_keeps_reports(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path, exit_code=2, terminal_outcome="exception")
    process = FakeProcess(returncode=2)

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "failed"
    assert status["reports"]


def test_report_rejects_parent_directory_symlink_after_projection(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path)
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def project() -> str:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return job_id

    job_id = asyncio.run(project())
    report_dir = tmp_path / "artifacts" / RUN_ID / "report"
    moved = tmp_path / "moved-report"
    report_dir.rename(moved)
    report_dir.symlink_to(moved, target_is_directory=True)
    assert asyncio.run(runner.report(job_id, "json")) is None


def test_report_over_limit_is_not_published(monkeypatch, tmp_path: Path) -> None:
    write_terminal_artifacts(tmp_path)
    (tmp_path / "artifacts" / RUN_ID / "report" / "trial-report.html").write_bytes(
        b"x" * (16 * 1024 * 1024 + 1)
    )
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    assert "reports" not in asyncio.run(exercise())


def test_spawn_error_is_unknown_without_raw_error_output(
    monkeypatch, tmp_path: Path
) -> None:
    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        raise OSError("private detail")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    status = asyncio.run(exercise())
    assert status["state"] == "unknown"
    assert "private detail" not in str(status)


def test_owned_artifact_open_is_nonblocking_before_type_check(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    opened_flags: list[int] = []
    real_open = os.open

    def fake_open(
        path: str | bytes | os.PathLike[str], flags: int, **kwargs: object
    ) -> int:
        opened_flags.append(flags)
        if path == root:
            return real_open(path, flags, **kwargs)
        raise OSError("stop after observing flags")

    monkeypatch.setattr("repotrial.local_web.runner.os.open", fake_open)
    assert _read_owned_file(root, root / "fifo", 128) is None
    assert opened_flags
    assert any(flags & os.O_NONBLOCK for flags in opened_flags)


def test_shutdown_sends_sigint_and_waits_for_owned_process(
    monkeypatch, tmp_path: Path
) -> None:
    process = FakeProcess(wait_event=asyncio.Event())

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> None:
        await runner.submit(request())
        while runner._active is not None and runner._active.process is None:
            await asyncio.sleep(0)
        await runner.shutdown()

    asyncio.run(exercise())
    assert process.signals
    assert process.waited


def test_shutdown_during_process_creation_signals_new_process(
    monkeypatch, tmp_path: Path
) -> None:
    creation_started = asyncio.Event()
    release_creation = asyncio.Event()
    process = FakeProcess(wait_event=asyncio.Event())

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        creation_started.set()
        await release_creation.wait()
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> None:
        await runner.submit(request())
        await creation_started.wait()
        shutdown_task = asyncio.create_task(runner.shutdown())
        await asyncio.sleep(0)
        assert runner._active is not None
        assert runner._active.shutdown_requested
        release_creation.set()
        await shutdown_task

    asyncio.run(exercise())
    assert process.signals
    assert process.waited


class GatedRunIdStream:
    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.sent = False

    async def read(self, _size: int = -1) -> bytes:
        if not self.sent:
            self.sent = True
            return f"run_id={RUN_ID}\n".encode()
        await self.released.wait()
        return b""


class GatedProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(wait_event=None)
        self.stdout = GatedRunIdStream()
        self.stderr = FakeStream(b"")
        self.released = self.stdout.released

    async def wait(self) -> int:
        await self.released.wait()
        self.returncode = self._final_returncode
        self.waited = True
        return self.returncode


def test_status_projects_stage_before_subprocess_finishes(
    monkeypatch, tmp_path: Path
) -> None:
    token = hashlib.sha256(RUN_ID.encode()).hexdigest()[:16]
    attempt = (
        tmp_path
        / "artifacts"
        / RUN_ID
        / "evidence"
        / f"baseline-{token}-0001-attempt-01"
    )
    attempt.mkdir(parents=True)
    (attempt / ".repotrial-attempt.json").write_text(
        json.dumps({"index": 1, "purpose": "baseline", "run_token": token}),
        encoding="utf-8",
    )
    (attempt / "baseline-lifecycle.jsonl").write_text(
        '{"event":"create_attempt"}\n', encoding="utf-8"
    )
    process = GatedProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> GatedProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        job_id = await runner.submit(request())
        for _ in range(50):
            status = await runner.status(job_id)
            if "run_id" in status:
                break
            await asyncio.sleep(0.01)
        early = await runner.status(job_id)
        assert runner._task is not None
        process.released.set()
        await runner._task
        return early, await runner.status(job_id)

    early, terminal = asyncio.run(exercise())
    assert early["run_id"] == RUN_ID
    assert early["progress"]["phase"] == "preparing_environment"
    assert early["progress"]["completed_phases"] == []
    assert terminal["run_id"] == RUN_ID


def test_stdout_run_id_is_strict_and_first_match_wins(
    monkeypatch, tmp_path: Path
) -> None:
    process = FakeProcess()
    process.stdout = FakeStream(
        b"run_id=not-a-uuid\n"
        + f"run_id={RUN_ID}\n".encode()
        + b"run_id=22222222-2222-4222-8222-222222222222\n"
    )

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    assert asyncio.run(exercise())["run_id"] == RUN_ID


class FragmentedStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def read(self, _size: int = -1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


def test_fragmented_run_id_line_is_bound_once(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    process.stdout = FragmentedStream(
        [
            b"run_id=11111111-1111-",
            b"4111-8111-111111111111\n",
            b"run_id=22222222-2222-4222-8222-222222222222\n",
        ]
    )

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id)

    assert asyncio.run(exercise())["run_id"] == RUN_ID


def test_completed_large_report_remains_available_to_runner(
    monkeypatch, tmp_path: Path
) -> None:
    write_terminal_artifacts(tmp_path)
    report_path = tmp_path / "artifacts" / RUN_ID / "report" / "trial-report.json"
    report_path.write_text(
        json.dumps(
            {
                "identity": {
                    "run_id": RUN_ID,
                    "repo_url": "https://github.com/acme/demo",
                    "commit_sha": "c" * 40,
                },
                "padding": "x" * 425_787,
            }
        ),
        encoding="utf-8",
    )
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> tuple[dict[str, object], object]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        return await runner.status(job_id), await runner.report(job_id, "json")

    status, report = asyncio.run(exercise())
    assert status["state"] == "completed"
    assert status["progress"]["phase"] == "finalizing"
    assert status["reports"]
    assert report is not None
    assert len(report.body) > 425_000


def test_runner_rejects_nonpositive_capture_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_output_bytes"):
        CliRunner(tmp_path, max_output_bytes=0)


def test_unknown_status_and_report_are_safe(tmp_path: Path) -> None:
    runner = CliRunner(tmp_path)
    assert runner.active is False
    status = asyncio.run(runner.status("missing"))
    assert status == {"job_id": "missing", "state": "unknown", "elapsed_seconds": 0.0}
    assert asyncio.run(runner.report("missing", "json")) is None


from repotrial.local_web.runner import (
    JobNotActiveError,
    JobNotFoundError,
    RunnerBusyError,
)


def test_stop_returns_stopping_then_stopped_after_owned_process_reaped(
    monkeypatch, tmp_path: Path
) -> None:
    process = FakeProcess(wait_event=asyncio.Event())

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        job_id = await runner.submit(request())
        while runner._active is not None and runner._active.process is None:
            await asyncio.sleep(0)
        stopping = await runner.stop(job_id)
        repeated = await runner.stop(job_id)
        assert repeated["control"] == "stopping"
        assert runner.active is True
        assert process.signals
        assert process.signals[-1] == signal.SIGINT
        assert runner._task is not None
        await runner._task
        return stopping, await runner.status(job_id)

    stopping, stopped = asyncio.run(exercise())
    assert stopping["control"] == "stopping"
    assert stopped["control"] == "stopped"
    assert stopped["job_id"] == stopping["job_id"]


def test_stop_then_shutdown_sends_sigint_only_once(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess(wait_event=asyncio.Event())

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> None:
        job_id = await runner.submit(request())
        while runner._active is not None and runner._active.process is None:
            await asyncio.sleep(0)
        await runner.stop(job_id)
        await runner.shutdown()

    asyncio.run(exercise())
    assert process.signals == [signal.SIGINT]


def test_stop_before_process_launch_is_forwarded_and_keeps_single_flight(
    monkeypatch, tmp_path: Path
) -> None:
    creation_started = asyncio.Event()
    release_creation = asyncio.Event()
    process = FakeProcess(wait_event=asyncio.Event())

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        creation_started.set()
        await release_creation.wait()
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> dict[str, object]:
        job_id = await runner.submit(request())
        await creation_started.wait()
        stopping = await runner.stop(job_id)
        assert runner.active is True
        with pytest.raises(RunnerBusyError):
            await runner.submit(request())
        release_creation.set()
        assert runner._task is not None
        await runner._task
        assert process.signals
        return stopping

    stopping = asyncio.run(exercise())
    assert stopping["control"] == "stopping"


def test_stop_is_idempotent_while_stopping_and_rejected_after_terminal(
    monkeypatch, tmp_path: Path
) -> None:
    process = FakeProcess()

    async def fake_exec(*_argv: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    runner = CliRunner(tmp_path)

    async def exercise() -> tuple[str, dict[str, object]]:
        job_id = await runner.submit(request())
        assert runner._task is not None
        await runner._task
        with pytest.raises(JobNotActiveError):
            await runner.stop(job_id)
        with pytest.raises(JobNotFoundError):
            await runner.stop("22222222-2222-4222-8222-222222222222")
        return job_id, await runner.status(job_id)

    job_id, status = asyncio.run(exercise())
    assert status["job_id"] == job_id
