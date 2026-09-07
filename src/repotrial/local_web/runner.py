"""Bounded, single-flight subprocess boundary for the local console."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_RUN_ID = re.compile(r"^run_id=([0-9a-fA-F-]{36})$", re.MULTILINE)
_SAFE_JOB_ID = re.compile(r"\A[0-9a-fA-F-]{36}\Z")
_MAX_HISTORY = 32
_MAX_CAPTURE = 64 * 1024
_MAX_EVIDENCE = 256 * 1024
_MAX_REPORT = 16 * 1024 * 1024
ReportKind = Literal["json", "html"]


@dataclass(frozen=True, slots=True)
class CliRequest:
    url: str
    commit_sha: str
    container_port: int
    compose_path: str | None = None
    model_endpoint: str | None = None
    model_name: str | None = None


@dataclass(frozen=True, slots=True)
class Report:
    body: bytes
    media_type: str


@dataclass(slots=True)
class _Job:
    job_id: str
    request: CliRequest | None = None
    started: float = field(default_factory=time.monotonic)
    state: Literal["running", "completed", "failed", "unknown"] = "running"
    process: asyncio.subprocess.Process | None = None
    run_id: str | None = None
    returncode: int | None = None
    finished_elapsed: float | None = None
    shutdown_requested: bool = False
    stop_reason: str | None = None
    error: str | None = None
    report_paths: dict[ReportKind, Path] = field(default_factory=dict)


class RunnerBusyError(RuntimeError):
    """Raised when the one permitted local invocation is already running."""


class CliRunner:
    """Run the installed RepoTrial CLI without exposing shell or raw output."""

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str | None = None,
        max_output_bytes: int = _MAX_CAPTURE,
    ) -> None:
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.workspace = workspace.resolve()
        self.executable = executable or sys.executable
        self.max_output_bytes = max_output_bytes
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._active: _Job | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self._active is not None

    def argv(self, request: CliRequest) -> list[str]:
        argv = [
            self.executable,
            "-I",
            "-m",
            "repotrial",
            "inspect",
            request.url,
            "--provider",
            "docker-sbx",
            "--commit-sha",
            request.commit_sha,
            "--container-port",
            str(request.container_port),
        ]
        optional = (
            ("--compose-path", request.compose_path),
            ("--model-endpoint", request.model_endpoint),
            ("--model-name", request.model_name),
        )
        for flag, value in optional:
            if value is not None:
                argv.extend((flag, value))
        return argv

    async def submit(self, request: CliRequest) -> str:
        if self._active is not None:
            raise RunnerBusyError("a trial is already running")
        job_id = _new_job_id()
        job = _Job(job_id=job_id, request=request)
        self._jobs[job_id] = job
        self._active = job
        self._trim_history()
        self._task = asyncio.create_task(self._run(job, request))
        return job_id

    async def status(self, job_id: str) -> dict[str, object]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "state": "unknown", "elapsed_seconds": 0.0}
        result: dict[str, object] = {
            "job_id": job.job_id,
            "state": job.state,
            "elapsed_seconds": round(
                job.finished_elapsed
                if job.finished_elapsed is not None
                else max(0.0, time.monotonic() - job.started),
                3,
            ),
        }
        if job.returncode is not None:
            result["exit_code"] = job.returncode
        if job.run_id is not None:
            result["run_id"] = job.run_id
        if job.stop_reason is not None:
            result["stop_reason"] = job.stop_reason
        if job.error is not None:
            result["error"] = job.error
        if job.state != "running" and job.report_paths:
            result["reports"] = {
                "json": f"/api/jobs/{job.job_id}/report/json",
                "html": f"/api/jobs/{job.job_id}/report/html",
            }
        return result

    async def report(self, job_id: str, kind: ReportKind) -> Report | None:
        job = self._jobs.get(job_id)
        if job is None or job.state == "running" or not job.report_paths:
            return None
        path = job.report_paths.get(kind)
        if path is None:
            return None
        body = _read_owned_file(self.workspace / "artifacts", path, _MAX_REPORT)
        if body is None:
            return None
        return Report(
            body=body,
            media_type="application/json" if kind == "json" else "text/html",
        )

    async def shutdown(self) -> None:
        job = self._active
        task = self._task
        if job is None or task is None:
            return
        process = job.process
        job.shutdown_requested = True
        if process is not None and process.returncode is None:
            try:
                process.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass
        await task
        self._task = None
        self._active = None

    async def _run(self, job: _Job, request: CliRequest) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv(request),
                cwd=self.workspace,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            job.process = process
            if job.shutdown_requested and process.returncode is None:
                try:
                    process.send_signal(signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass
            stdout_task = asyncio.create_task(self._drain(process.stdout))
            stderr_task = asyncio.create_task(self._drain(process.stderr))
            await process.wait()
            stdout_run_id, _ = await stdout_task
            await stderr_task
            job.returncode = process.returncode
            if stdout_run_id:
                job.run_id = stdout_run_id
            self._project_terminal_evidence(job)
            if job.state == "running":
                job.state = "failed"
                if not job.report_paths:
                    job.error = "terminal evidence unavailable"
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            job.state = "unknown"
            job.error = "CLI process could not be started or state is unavailable"
        finally:
            job.finished_elapsed = max(0.0, time.monotonic() - job.started)
            if self._active is job:
                self._active = None

    async def _drain(
        self, stream: asyncio.StreamReader | None
    ) -> tuple[str | None, bool]:
        if stream is None:
            return None, False
        captured = bytearray()
        limited = False
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = self.max_output_bytes - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                limited = True
        text = bytes(captured).decode("utf-8", errors="ignore")
        match = _RUN_ID.search(text)
        return (match.group(1) if match else None), limited

    def _project_terminal_evidence(self, job: _Job) -> None:
        run_id = job.run_id
        if run_id is None or not _SAFE_JOB_ID.fullmatch(run_id):
            return
        run_path = self.workspace / "artifacts" / run_id
        evidence_path = run_path / "attempt-result.json"
        report_dir = run_path / "report"
        json_path = report_dir / "trial-report.json"
        html_path = report_dir / "trial-report.html"
        root = self.workspace / "artifacts"
        evidence_bytes = _read_owned_file(root, evidence_path, _MAX_EVIDENCE)
        report_bytes = _read_owned_file(root, json_path, _MAX_REPORT)
        html_bytes = _read_owned_file(root, html_path, _MAX_REPORT)
        if evidence_bytes is None or report_bytes is None or html_bytes is None:
            return
        try:
            evidence = json.loads(evidence_bytes.decode("utf-8"))
            report_json = json.loads(report_bytes.decode("utf-8"))
        except (UnicodeError, ValueError):
            return
        if not isinstance(evidence, dict) or evidence.get("run_id") != run_id:
            return
        if not isinstance(report_json, dict):
            return
        report_identity = report_json.get("identity")
        if not isinstance(report_identity, dict):
            return
        request = job.request
        identity_matches = (
            request is not None
            and evidence.get("repository_url") == request.url
            and evidence.get("expected_sha") == request.commit_sha
            and evidence.get("actual_verified_sha") == request.commit_sha
            and evidence.get("container_port") == request.container_port
            and (
                request.compose_path is None
                or evidence.get("compose_path") == request.compose_path
            )
            and report_identity.get("run_id") == run_id
            and report_identity.get("repo_url") == request.url
            and report_identity.get("commit_sha") == request.commit_sha
        )
        if not identity_matches:
            return
        job.report_paths = {"json": json_path, "html": html_path}
        stop_reason = evidence.get("stop_reason")
        if isinstance(stop_reason, str) and len(stop_reason) <= 512:
            job.stop_reason = stop_reason
        if (
            job.returncode == 0
            and evidence.get("exit_code") == 0
            and evidence.get("terminal_outcome") == "completed"
        ):
            job.state = "completed"

    def _trim_history(self) -> None:
        while len(self._jobs) > _MAX_HISTORY:
            job_id, job = next(iter(self._jobs.items()))
            if job is self._active:
                self._jobs.move_to_end(job_id)
                continue
            self._jobs.pop(job_id)


def _new_job_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _read_owned_file(root: Path, path: Path, max_bytes: int) -> bytes | None:
    """Read a fixed artifact through a root dirfd without following symlinks."""
    if max_bytes <= 0:
        return None
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    descriptors: list[int] = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            descriptors.append(current)
        leaf = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current
        )
        descriptors.append(leaf)
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode):
            return None
        result = bytearray()
        while len(result) <= max_bytes:
            chunk = os.read(leaf, min(65_536, max_bytes + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > max_bytes:
                return None
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
    return None
