from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

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
