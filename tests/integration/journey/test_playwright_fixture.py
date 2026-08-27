"""Real Chromium integration coverage for the constrained browser journey DSL."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from repotrial.domain.enums import Verdict
from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.journey.playwright_runner import run_playwright_journey

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "app"
PROJECT_ROOT = Path(__file__).parents[3]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _fixture_server(tmp_path: Path) -> Iterator[str]:
    port = _free_port()
    environment = os.environ.copy()
    environment.update(
        {
            "APP_REQUIRED_TOKEN": "browser-test-token",
            "REPOTRIAL_FIXTURE_DATA_PATH": str(tmp_path / "items.json"),
            "REPOTRIAL_FIXTURE_TMP_PATH": str(tmp_path / "repotrial.tmp"),
            "REPOTRIAL_FIXTURE_PROC_STATUS": str(tmp_path / "status"),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--app-dir",
            str(FIXTURE_DIR),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(process.stdout.read() if process.stdout is not None else "")
            try:
                with urlopen(f"{base_url}/health", timeout=0.1) as response:
                    if response.read() == b'{"status":"ok"}':
                        break
            except OSError:
                pass
            time.sleep(0.02)
        else:
            pytest.fail("fixture did not become ready")
        yield base_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def _journey(*steps: JourneyStep, journey_id: str = "browser-journey") -> Journey:
    return Journey(
        journey_id=journey_id, name="Browser fixture journey", steps=list(steps)
    )


def _step(
    action: str, params: dict[str, object], *, step_id: str = "step"
) -> JourneyStep:
    return JourneyStep(step_id=step_id, tool="browser", action=action, params=params)


def _run(journey: Journey, base_url: str, evidence_dir: Path):
    return asyncio.run(
        run_playwright_journey(journey, base_url=base_url, evidence_dir=evidence_dir)
    )


@contextmanager
def _redirect_server(destination: str, request_count: list[int]) -> Iterator[str]:
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_count[0] += 1
            self.send_response(302)
            self.send_header("Location", destination)
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@contextmanager
def _counting_server(request_count: list[int]) -> Iterator[str]:
    class CountingHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_count[0] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>foreign</title>")

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@contextmanager
def _blocking_server(
    request_started: threading.Event, release: threading.Event
) -> Iterator[str]:
    class BlockingHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_started.set()
            if not release.wait(timeout=3):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!doctype html><button>ready</button>")

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BlockingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_replays_create_delete_flow_and_writes_readable_trace(tmp_path: Path) -> None:
    """Catches a runner that skips a browser action or fails to preserve trace evidence."""
    journey = _journey(
        _step("goto", {"path": "/"}, step_id="hostile / id"),
        _step("fill_by_label", {"label": "Item name", "value": "browser item"}),
        _step("click_by_role", {"role": "button", "name": "Create item"}),
        _step("click_by_role", {"role": "button", "name": "Delete browser item"}),
        _step("assert_text_visible", {"text": "Create item"}),
    )
    original = journey.model_dump(mode="json")

    with _fixture_server(tmp_path) as base_url:
        result = _run(journey, base_url, tmp_path / "evidence")

    trace = tmp_path / "evidence" / "trace.zip"
    assert result.verdict is Verdict.PASS
    assert (result.passed_steps, result.total_steps) == (5, 5)
    assert result.evidence_paths == [str(trace)]
    assert journey.model_dump(mode="json") == original
    assert json.loads((tmp_path / "items.json").read_text(encoding="utf-8")) == []
    assert trace.is_file() and trace.stat().st_size > 0
    with zipfile.ZipFile(trace) as archive:
        assert archive.namelist()


def test_missing_text_stops_at_failure_with_trace_and_indexed_png(
    tmp_path: Path,
) -> None:
    """Catches a runner that treats a missing visible assertion as success or leaks IDs."""
    journey = _journey(
        _step("goto", {"path": "/"}, step_id="first"),
        _step("assert_text_visible", {"text": "not on this page"}, step_id="evil-id"),
        journey_id="../../hostile",
    )
    with _fixture_server(tmp_path) as base_url:
        result = _run(journey, base_url, tmp_path / "evidence")

    trace = tmp_path / "evidence" / "trace.zip"
    screenshot = tmp_path / "evidence" / "step-0001-failure.png"
    assert result.verdict is Verdict.FAIL
    assert (result.passed_steps, result.total_steps) == (1, 2)
    assert result.failure_reason == "step-0001:assertion_failure"
    assert result.evidence_paths == [str(trace), str(screenshot)]
    assert trace.read_bytes()[:2] == b"PK"
    assert screenshot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "evil-id" not in result.failure_reason


@pytest.mark.parametrize(
    "journey",
    [
        _journey(
            JourneyStep(step_id="bad", tool="http", action="goto", params={"path": "/"})
        ),
        _journey(_step("evaluate", {"path": "/"})),
        _journey(_step("goto", {"path": "/", "extra": True})),
        _journey(_step("goto", {"path": "https://example.invalid/"})),
        _journey(_step("goto", {"path": "//example.invalid/"})),
        _journey(_step("goto", {"path": "/%2e%2e/secret"})),
        _journey(_step("click_by_role", {"role": "dialog", "name": "x"})),
        _journey(_step("fill_by_label", {"label": "name", "value": "x" * 4097})),
        _journey(_step("fill_by_label", {"label": "bad\u0085label", "value": "x"})),
        _journey(
            _step("goto", {"path": "/"}),
            _step(
                "goto",
                {"path": "/"},
                step_id="late",
            ),
        ),
    ],
    ids=(
        "tool",
        "action",
        "params",
        "absolute-path",
        "scheme-relative-path",
        "ambiguous-path",
        "role",
        "bound",
        "unicode-control",
        "later-step",
    ),
)
def test_invalid_journeys_fail_preflight_without_artifacts(
    tmp_path: Path, journey: Journey
) -> None:
    """Catches validation that launches Chromium or performs partial journeys first."""
    if journey.steps[-1].step_id == "late":
        journey.steps[-1].assertions = [
            JourneyAssertion(kind="status_code", target="response.status", expected=200)
        ]
    evidence_dir = tmp_path / "evidence"
    result = _run(journey, "http://127.0.0.1:1", evidence_dir)

    assert result.verdict is Verdict.FAIL
    assert result.passed_steps == 0
    assert result.evidence_paths == []
    assert result.failure_reason is not None and result.failure_reason.startswith(
        "step-"
    )
    assert not evidence_dir.exists()


def test_rejects_invalid_base_url_before_browser_or_artifacts(tmp_path: Path) -> None:
    """Catches base URL parsing that admits paths, credentials, or query authority."""
    journey = _journey(_step("goto", {"path": "/"}))
    for bad_base_url in (
        "ftp://127.0.0.1/",
        "http://user@127.0.0.1/",
        "http://127.0.0.1/base",
        "http://127.0.0.1/?token=secret",
        "http://127.0.0.1/#fragment",
    ):
        evidence_dir = tmp_path / str(abs(hash(bad_base_url)))
        result = _run(journey, bad_base_url, evidence_dir)
        assert result.failure_reason == "journey:invalid_base_url"
        assert not evidence_dir.exists()


def test_existing_trace_is_never_overwritten(tmp_path: Path) -> None:
    """Catches evidence creation that silently replaces an existing target artifact."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    sentinel = evidence_dir / "trace.zip"
    sentinel.write_bytes(b"do not overwrite")
    journey = _journey(_step("goto", {"path": "/"}))
    with _fixture_server(tmp_path) as base_url:
        result = _run(journey, base_url, evidence_dir)

    assert result.verdict is Verdict.FAIL
    assert result.passed_steps == 0
    assert result.failure_reason == "journey:trace_failure"
    assert sentinel.read_bytes() == b"do not overwrite"


def test_existing_failure_screenshot_is_never_overwritten(tmp_path: Path) -> None:
    """Catches a runner that overwrites a later failure screenshot artifact."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    sentinel = evidence_dir / "step-0001-failure.png"
    sentinel.write_bytes(b"do not overwrite")
    journey = _journey(
        _step("goto", {"path": "/"}),
        _step("assert_text_visible", {"text": "missing"}),
    )
    with _fixture_server(tmp_path) as base_url:
        result = _run(journey, base_url, evidence_dir)

    assert result.verdict is Verdict.FAIL
    assert result.passed_steps == 0
    assert result.failure_reason == "journey:evidence_failure"
    assert sentinel.read_bytes() == b"do not overwrite"


def test_cross_origin_redirect_is_blocked_without_retrying_or_contacting_target(
    tmp_path: Path,
) -> None:
    """Catches network authority expansion or implicit retry after a redirect."""
    origin_requests = [0]
    foreign_requests = [0]
    with (
        _counting_server(foreign_requests) as foreign_url,
        _redirect_server(foreign_url, origin_requests) as base_url,
    ):
        result = _run(
            _journey(_step("goto", {"path": "/"})),
            base_url,
            tmp_path / "evidence",
        )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason in {
        "step-0000:cross_origin_request",
        "step-0000:cross_origin_navigation",
    }
    assert origin_requests == [1]
    assert foreign_requests == [0]


def test_cancellation_propagates_after_browser_cleanup(tmp_path: Path) -> None:
    """Catches cancellation being translated into a false result or leaked browser session."""

    async def cancel_run(base_url: str) -> None:
        task = asyncio.create_task(
            run_playwright_journey(
                _journey(_step("goto", {"path": "/"})),
                base_url=base_url,
                evidence_dir=tmp_path / "evidence",
            )
        )
        assert await asyncio.to_thread(request_started.wait, 3)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        result = await run_playwright_journey(
            _journey(_step("goto", {"path": "/"})),
            base_url=base_url,
            evidence_dir=tmp_path / "after-cancel",
        )
        assert result.verdict is Verdict.PASS

    request_started = threading.Event()
    release = threading.Event()
    with _blocking_server(request_started, release) as base_url:
        asyncio.run(cancel_run(base_url))
