from __future__ import annotations

import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect, sync_playwright

from repotrial.doctor import DoctorCheck, DoctorReport
from repotrial.local_web.app import create_app
from repotrial.local_web.repository import (
    RepositoryInput,
    RepositoryMetadata,
)
from repotrial.local_web.runner import Report, RunnerBusyError
from repotrial.local_web.settings import ModelSettingsStore

JOB_ID = "11111111-1111-4111-8111-111111111111"


class BrowserRunner:
    def __init__(self, *, terminal: str = "completed") -> None:
        self.active = False
        self.terminal = terminal
        self.submissions: list[dict[str, object]] = []
        self.status_calls = 0
        self.failure_stage: str | None = None
        self.error_code: str | None = None
        self.run_id: str | None = None
        self.failure_evidence: dict[str, object] = {}
        self.terminal_elapsed: float | None = None
        self.terminal_stop_reason: str | None = None

    async def submit(self, request: object) -> str:
        if self.active:
            raise RunnerBusyError("a trial is already running")
        self.active = True
        self.submissions.append(asdict(request))
        return JOB_ID

    async def status(self, job_id: str) -> dict[str, object]:
        self.status_calls += 1
        if self.status_calls == 1:
            return {"job_id": job_id, "state": "running", "elapsed_seconds": 1.2}
        result: dict[str, object] = {
            "job_id": job_id,
            "state": self.terminal,
            "elapsed_seconds": (
                self.terminal_elapsed if self.terminal_elapsed is not None else 3.4
            ),
            "exit_code": 0 if self.terminal == "completed" else 2,
            "reports": {
                "json": f"/api/jobs/{job_id}/report/json",
                "html": f"/api/jobs/{job_id}/report/html",
            },
        }
        if self.terminal == "failed":
            result["stop_reason"] = self.terminal_stop_reason or "启动验证未通过"
        elif self.terminal == "completed":
            result["stop_reason"] = "completed"
        elif self.terminal == "unknown":
            result["error"] = "任务状态不可用"
        if self.failure_stage is not None:
            result["failure_stage"] = self.failure_stage
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.failure_evidence:
            result["failure_evidence"] = dict(self.failure_evidence)
        self.active = False
        return result

    async def report(self, job_id: str, kind: str) -> Report | None:
        return Report(
            body=b"<html></html>" if kind == "html" else b'{"ok":true}',
            media_type="text/html" if kind == "html" else "application/json",
        )

    async def shutdown(self) -> None:
        self.active = False


class BrowserServices:
    def ensure_ready(self) -> DoctorReport:
        return DoctorReport(
            checks=(
                DoctorCheck("sbx_daemon", "PASS", True, "running", None),
                DoctorCheck("sbx_diagnose", "PASS", True, "ok", None),
                DoctorCheck("sbx_inventory", "PASS", True, "empty", None),
            )
        )


class BrowserRepository:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    def discover(self, payload: RepositoryInput) -> RepositoryMetadata:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        sha = payload.commit_sha
        return RepositoryMetadata(
            commit_sha=sha,
            commit_url=(f"{payload.url}/commit/{sha}" if sha is not None else None),
            compose_candidates=(),
            selected_compose_path=None,
            ports=(),
            warnings=(),
            errors=() if sha is not None else ("github_unavailable",),
        )


@contextmanager
def live_server(
    runner: BrowserRunner,
    *,
    repository: BrowserRepository | None = None,
    services: object | None = None,
    doctor: Callable[[], DoctorReport] | None = None,
) -> Iterator[str]:
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    doctor = doctor or (
        lambda: DoctorReport(
            checks=(
                DoctorCheck(
                    "docker", "FAIL", True, "Docker 未运行", "启动 Docker 后重试。"
                ),
                DoctorCheck(
                    "pid",
                    "UNSUPPORTED",
                    False,
                    "当前运行时不支持验证",
                    "请查看能力限制。",
                ),
            )
        )
    )
    with tempfile.TemporaryDirectory(prefix="repotrial-browser-") as settings_dir:
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    Path("/tmp/repotrial-browser"),
                    runner=runner,
                    doctor=doctor,
                    repository=repository or BrowserRepository(),
                    services=services or BrowserServices(),
                    settings_store=ModelSettingsStore(
                        Path(settings_dir) / "model-settings.json"
                    ),
                ),
                host="127.0.0.1",
                port=port,
                log_level="error",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=3)
            raise RuntimeError("uvicorn did not start")
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        yield playwright.chromium.launch(headless=True)


def test_console_uses_real_state_and_keeps_help_operable(browser: Browser) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser.new_page()
        page.goto(base_url)
        assert page.locator("h1").inner_text() == "新建检查"
        assert page.locator("#production-sidebar").count() == 1
        assert page.locator("#demo-run").count() == 0
        page.locator("a[data-guide=sha]").click()
        expect(page.locator("dialog")).to_be_visible()
        page.get_by_role("button", name="关闭").click()
        page.get_by_role("button", name="检查环境").click()
        expect(
            page.locator("#environment").get_by_text("Docker 未运行")
        ).to_be_visible()
        expect(
            page.locator("#environment").get_by_text("启动 Docker 后重试。")
        ).to_be_visible()
        expect(
            page.locator("#environment").get_by_text("当前运行时不支持验证")
        ).to_be_visible()
        page.close()


def test_usage_guide_opens_in_new_tab_and_preserves_console(
    browser: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser.new_page()
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        guide_link = page.locator("#usage-guide")
        expect(guide_link).to_have_attribute("href", "/?view=guide&lang=zh")
        with page.expect_popup() as popup_info:
            guide_link.click()
        guide = popup_info.value
        guide.wait_for_load_state("domcontentloaded")
        expect(guide.locator("h1")).to_contain_text("使用文档")
        expect(guide.locator("body")).to_contain_text("8080:3000")
        assert guide.locator("#trial-form").count() == 0
        assert guide.locator("script").count() == 0
        assert (
            page.locator("[name=url]").input_value() == "https://github.com/acme/demo"
        )
        assert runner.submissions == []
        page.locator("#language-select").select_option("en")
        expect(guide_link).to_have_attribute("href", "/?view=guide&lang=en")
        guide.close()
        page.close()


def test_console_submits_once_polls_server_and_exposes_validated_report_links(
    browser: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser.new_page()
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        page.locator("[name=commit_sha]").fill("a" * 40)
        page.locator("[name=container_port]").fill("8080")
        page.get_by_role("button", name="开始检查").click()
        page.locator("#status-title").wait_for(state="visible")
        page.wait_for_function(
            "document.querySelector('#status-title').textContent.includes('已完成')"
        )
        assert runner.submissions and len(runner.submissions) == 1
        assert page.locator("[role=progressbar]").count() == 0
        assert "100%" not in page.locator("body").inner_text()
        assert (
            page.locator("#html-report").get_attribute("href")
            == f"/api/jobs/{JOB_ID}/report/html"
        )
        assert (
            page.locator("#json-report").get_attribute("href")
            == f"/api/jobs/{JOB_ID}/report/json"
        )
        assert (
            page.evaluate("sessionStorage.getItem('repotrial-active-job-id')") == JOB_ID
        )
        page.reload()
        page.wait_for_function(
            "document.querySelector('#status-title').textContent.includes('已完成')"
        )
        assert len(runner.submissions) == 1
        page.close()


def test_console_renders_field_errors_and_failed_report(browser: Browser) -> None:
    runner = BrowserRunner(terminal="failed")
    with live_server(runner) as base_url:
        page = browser.new_page()
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        page.locator("[name=commit_sha]").fill("b" * 40)
        page.locator("[name=container_port]").fill("8080")
        page.locator("summary").click()
        page.locator("[name=model_endpoint]").fill("https://model.example/v1")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_contain_text("model_name")
        assert not runner.submissions
        page.locator("[name=model_name]").fill("model-x")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")
        page.get_by_role("button", name="开始检查").click()
        page.wait_for_function(
            "document.querySelector('#status-title').textContent.includes('失败')"
        )
        assert page.locator("#reason").inner_text() == "启动验证未通过"
        assert page.locator("[role=progressbar]").count() == 0
        assert "进度" not in page.locator("body").inner_text()
        assert (
            page.locator("#html-report").get_attribute("href")
            == f"/api/jobs/{JOB_ID}/report/html"
        )
        page.close()
