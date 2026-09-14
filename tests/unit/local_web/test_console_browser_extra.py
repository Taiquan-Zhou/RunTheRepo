from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, Request, Route, expect, sync_playwright
from test_console_browser import JOB_ID, BrowserRepository, BrowserRunner, live_server

from repotrial.doctor import DoctorCheck, DoctorReport


@pytest.fixture(scope="module")
def browser_extra() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        yield playwright.chromium.launch(headless=True)


def _fill_required(page: Page, sha: str) -> None:
    page.locator("[name=url]").fill("https://github.com/acme/demo")
    page.locator("[name=commit_sha]").fill(sha)
    page.locator("[name=container_port]").fill("8080")


def _capture_job_payloads(page: Page) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []

    def capture(request: Request) -> None:
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            payloads.append(json.loads(request.post_data or "{}"))

    page.on("request", capture)
    return payloads


def _add_progress(route: Route, progress: dict[str, object]) -> None:
    response = route.fetch()
    payload = json.loads(response.text())
    payload["progress"] = progress
    route.fulfill(
        status=response.status,
        headers=response.headers,
        body=json.dumps(payload),
    )


def test_progress_renders_confirmed_current_and_unconfirmed_phases_without_percentages(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    progress = {
        "phase": "checking_application",
        "completed_phases": ["preparing_repository"],
        "latest_event": "application_check_started",
        "latest_operation": "http_check",
        "evidence_age_seconds": 12,
    }
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def status(route: Route) -> None:
            if route.request.method == "GET" and "/report/" not in route.request.url:
                _add_progress(route, progress)
            else:
                route.continue_()

        page.route("**/api/jobs/*", status)
        page.goto(base_url)
        _fill_required(page, "6" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#phase-progress")).to_be_visible(timeout=6000)
        expect(page.locator("#phase-list")).to_contain_text("仓库准备")
        expect(page.locator("#phase-list")).to_contain_text("应用检查")
        expect(page.locator("#phase-list")).to_contain_text("尚未确认")
        expect(page.locator("#phase-list")).to_contain_text("当前阶段")
        expect(page.locator("#latest-evidence")).to_contain_text("12 秒前")
        expect(page.locator("body")).not_to_contain_text("%")
        page.close()


def test_progress_missing_evidence_says_waiting_without_inventing_stage(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "7" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#phase-progress")).to_be_visible(timeout=6000)
        expect(page.locator("#phase-progress")).to_contain_text(
            "阶段证据暂不可用，等待服务端更新"
        )
        expect(page.locator("#phase-list")).to_have_text("")
        expect(page.locator("body")).not_to_contain_text("百分比")
        page.close()


@pytest.mark.parametrize(
    ("terminal", "phase_status", "latest_status"),
    [
        ("failed", "已停止", "阶段证据已停止"),
        ("unknown", "状态待确认", "阶段状态待确认"),
        ("completed", "尚未确认", "终态证据已确认"),
    ],
)
def test_progress_distinguishes_terminal_state_without_inventing_activity(
    browser_extra: Browser, terminal: str, phase_status: str, latest_status: str
) -> None:
    runner = BrowserRunner(terminal=terminal)
    progress = {"phase": "hardening", "completed_phases": ["preparing_repository"]}
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def status(route: Route) -> None:
            if route.request.method == "GET" and "/report/" not in route.request.url:
                _add_progress(route, progress)
            else:
                route.continue_()

        page.route("**/api/jobs/*", status)
        page.goto(base_url)
        _fill_required(page, "a" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#phase-list")).to_contain_text(
            "权限收紧实验 · " + phase_status, timeout=6000
        )
        if terminal == "completed":
            expect(page.locator("#phase-list")).not_to_contain_text("当前阶段")
        expect(page.locator("#latest-evidence")).to_have_text(latest_status)
        expect(page.locator("#latest-evidence")).not_to_contain_text("等待新的证据更新")
        if terminal == "completed":
            expect(page.locator("#task-card .run-events")).to_be_visible()
            expect(page.locator("#phase-progress")).to_be_visible()
            expect(page.locator("#phase-progress-unavailable")).not_to_be_visible()
            confirmed_color = page.locator("#phase-list li.confirmed").evaluate(
                "element => getComputedStyle(element).color"
            )
            pending_color = page.locator(
                "#phase-list li:not(.confirmed):not(.current)"
            ).first.evaluate("element => getComputedStyle(element).color")
            success_color = page.locator("#task-card").evaluate(
                """element => {
                    const token = getComputedStyle(element)
                        .getPropertyValue('--success').trim();
                    const probe = document.createElement('span');
                    probe.style.color = token;
                    element.append(probe);
                    const computed = getComputedStyle(probe).color;
                    probe.remove();
                    return computed;
                }"""
            )
            assert confirmed_color == success_color
            assert confirmed_color != pending_color
        page.close()


def test_progress_age_without_event_has_no_extra_separator(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="unknown")
    progress = {
        "phase": "checking_application",
        "completed_phases": [],
        "evidence_age_seconds": 2,
    }
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def status(route: Route) -> None:
            if route.request.method == "GET" and "/report/" not in route.request.url:
                _add_progress(route, progress)
            else:
                route.continue_()

        page.route("**/api/jobs/*", status)
        page.goto(base_url)
        _fill_required(page, "b" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#latest-evidence")).to_have_text(
            "最近证据：2 秒前", timeout=6000
        )
        expect(page.locator("#latest-evidence")).not_to_contain_text("： ·")
        page.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("git_metadata_fallback", "使用备用方式读取仓库信息"),
        ("git_timeout", "备用仓库信息读取超时"),
        ("git_unavailable", "备用仓库信息读取不可用"),
        ("git_metadata_limit_exceeded", "备用仓库信息读取超过限制"),
        ("archive_limit_exceeded", "仓库文件超过自动识别读取上限"),
        ("git_ls_remote_failed", "无法读取最新提交"),
        ("git_archive_failed", "无法读取仓库文件"),
        ("git_archive_invalid", "仓库归档不可解析"),
        ("git_archive_redirect_rejected", "读取地址跳转被拒绝"),
        ("git_commit_not_found", "提交不存在"),
        ("git_compose_not_found", "未找到 Compose"),
        ("git_compose_selection_required", "请选择 Compose"),
    ],
)
def test_git_metadata_fallback_and_errors_have_specific_messages(
    browser_extra: Browser, code: str, expected: str
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def metadata(route: Route) -> None:
            body = {
                "commit_sha": "a" * 40,
                "commit_url": "https://github.com/acme/demo/commit/" + "a" * 40,
                "compose_candidates": [],
                "selected_compose_path": None,
                "ports": [],
                "warnings": [code] if code == "git_metadata_fallback" else [],
                "errors": [] if code == "git_metadata_fallback" else [code],
            }
            route.fulfill(content_type="application/json", body=json.dumps(body))

        page.route("**/api/repository", metadata)
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("#repository-meta-status")).to_contain_text(
            expected, timeout=4000
        )
        if code == "git_metadata_fallback":
            expect(page.locator("#submit-button")).to_be_enabled()
        page.close()


def test_git_metadata_error_relocalizes_without_exposing_code(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def metadata(route: Route) -> None:
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "commit_sha": "a" * 40,
                        "commit_url": "https://github.com/acme/demo/commit/" + "a" * 40,
                        "compose_candidates": [],
                        "selected_compose_path": None,
                        "ports": [],
                        "warnings": [],
                        "errors": ["git_archive_invalid"],
                    }
                ),
            )

        page.route("**/api/repository", metadata)
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("#repository-meta-status")).to_contain_text(
            "仓库归档不可解析", timeout=4000
        )
        page.locator("#language-select").select_option("en")
        expect(page.locator("#repository-meta-status")).to_contain_text(
            "The repository archive could not be parsed"
        )
        expect(page.locator("#repository-meta-status")).not_to_contain_text(
            "git_archive_invalid"
        )
        page.close()


def test_usage_guide_says_empty_model_pair_disables_model_assistance(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url + "/?view=guide&lang=zh")
        expect(page.locator("body")).to_contain_text("两项都留空表示不启用模型辅助")
        page.goto(base_url + "/?view=guide&lang=en")
        expect(page.locator("body")).to_contain_text(
            "If both endpoint and model name are empty, model assistance is disabled"
        )
        page.close()


def test_total_duration_exhausted_has_bounded_stop_copy_and_preserves_evidence(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "sandbox:exec:total_duration_exhausted"
    runner.run_id = "66666666-6666-4666-8666-666666666666"
    progress = {
        "phase": "hardening",
        "completed_phases": ["preparing_repository", "checking_application"],
        "experiment_index": 2,
        "latest_event": "hardening_started",
        "latest_operation": "sandbox_exec",
        "evidence_age_seconds": 65,
    }
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def status(route: Route) -> None:
            if route.request.method == "GET" and "/report/" not in route.request.url:
                _add_progress(route, progress)
            else:
                route.continue_()

        page.route("**/api/jobs/*", status)
        page.goto(base_url)
        _fill_required(page, "8" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text(
            "达到时间上限，已停止", timeout=6000
        )
        expect(page.locator("#status-desc")).to_have_text(
            "已保留已有结果，部分步骤未完成"
        )
        expect(page.locator("#reason")).to_have_text(
            "本次运行时间预算已用完，系统停止后续步骤并清理环境"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "查看报告中已完成和未完成的检查"
        )
        expect(page.locator("#failure-code")).to_have_text(
            "sandbox:exec:total_duration_exhausted"
        )
        expect(page.locator("#failure-run-id")).to_have_text(
            "66666666-6666-4666-8666-666666666666"
        )
        expect(page.locator("#html-report")).to_be_visible()
        expect(page.locator("#phase-list")).to_contain_text("权限收紧实验（第 2 轮）")
        assert page.locator("#state-dot").evaluate(
            "element => getComputedStyle(element).backgroundColor"
        ) in {"rgb(164, 113, 38)", "#a47126"}
        page.close()


def test_phase_and_budget_stop_copy_relocalize_without_resetting_evidence(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "total_duration_exhausted"
    progress = {
        "phase": "finalizing",
        "completed_phases": ["preparing_repository"],
        "latest_event": "cleanup_started",
        "latest_operation": "cleanup",
        "evidence_age_seconds": 61,
    }
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def status(route: Route) -> None:
            if route.request.method == "GET" and "/report/" not in route.request.url:
                _add_progress(route, progress)
            else:
                route.continue_()

        page.route("**/api/jobs/*", status)
        page.goto(base_url)
        _fill_required(page, "9" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text(
            "达到时间上限，已停止", timeout=6000
        )
        page.locator("#language-select").select_option("en")
        expect(page.locator("#status-title")).to_have_text(
            "Time limit reached; stopped"
        )
        expect(page.locator("#status-desc")).to_have_text(
            "Existing results were retained; some steps were not completed"
        )
        expect(page.locator("#reason")).to_have_text(
            "The run time budget was exhausted; remaining steps were stopped and the environment was cleaned up"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "Review completed and incomplete checks in the report"
        )
        expect(page.locator("#phase-list")).to_contain_text("Finalizing")
        expect(page.locator("#latest-evidence")).to_contain_text("1 min ago")
        expect(page.locator("body")).not_to_contain_text("%")
        page.close()


def test_submission_uses_inferred_journeys_without_basic_page_fields(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        payloads = _capture_job_payloads(page)
        page.goto(base_url)
        assert page.locator("#basic-check-enabled").count() == 0
        assert page.locator("[name=basic_check_path]").count() == 0
        assert page.locator("[name=basic_check_text]").count() == 0
        _fill_required(page, "5" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        assert payloads and "basic_check_path" not in payloads[0]
        assert "basic_check_text" not in payloads[0]
        page.close()


def test_insufficient_coverage_failure_has_actionable_copy(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "insufficient_coverage"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "3" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("功能验证")
        expect(page.locator("#failure-reason")).to_have_text(
            "没有任何通过的功能验证流程，无法继续加固验证"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "未形成有效的功能验证结果；查看报告确认是缺少验证流程还是已有断言未通过，再处理后重试"
        )
        page.close()


def test_experiment_sandbox_failure_explains_environment_creation_in_both_languages(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "experiment:sandbox_failed"
    runner.run_id = "55555555-5555-4555-8555-555555555555"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "4" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("权限收紧实验")
        expect(page.locator("#failure-reason")).to_have_text(
            "权限收紧实验未能创建隔离环境；请查看运行证据，应用启动/首页验证可能已通过"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "检查 Docker Sandbox 实验环境的创建状态和运行证据，再重试。"
        )
        expect(page.locator("#failure-code")).to_have_text("experiment:sandbox_failed")
        expect(page.locator("#failure-run-id")).to_have_text(
            "55555555-5555-4555-8555-555555555555"
        )
        expect(page.locator("#html-report")).to_be_visible()
        page.locator("#language-select").select_option("en")
        expect(page.locator("#failure-stage")).to_have_text(
            "Least-privilege experiment"
        )
        expect(page.locator("#failure-reason")).to_contain_text(
            "could not create its isolated environment"
        )
        expect(page.locator("#failure-next-step")).to_contain_text(
            "Docker Sandbox experiment environment"
        )
        expect(page.locator("#failure-code")).to_have_text("experiment:sandbox_failed")
        expect(page.locator("#failure-run-id")).to_have_text(
            "55555555-5555-4555-8555-555555555555"
        )
        expect(page.locator("#html-report")).to_be_visible()
        page.close()


def test_checkout_timeout_has_specific_bilingual_failure_copy(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "intake:checkout_timeout"
    runner.run_id = "77777777-7777-4777-8777-777777777777"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "1" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("准备仓库版本")
        expect(page.locator("#failure-reason")).to_have_text(
            "准备指定提交的仓库文件时超时，尚未进入应用启动阶段"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "可使用相同提交重试；若持续发生，需要检查 Git 连接和仓库检出日志"
        )
        expect(page.locator("#failure-code")).to_have_text("intake:checkout_timeout")
        expect(page.locator("#failure-run-id")).to_have_text(
            "77777777-7777-4777-8777-777777777777"
        )
        page.locator("#language-select").select_option("en")
        expect(page.locator("#failure-stage")).to_have_text(
            "Preparing repository version"
        )
        expect(page.locator("#failure-reason")).to_have_text(
            "Preparing the specified commit's repository files timed out; the application startup stage was not reached."
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "Retry with the same commit; if it continues, check the Git connection and repository checkout logs."
        )
        expect(page.locator("#failure-code")).to_have_text("intake:checkout_timeout")
        expect(page.locator("#failure-run-id")).to_have_text(
            "77777777-7777-4777-8777-777777777777"
        )
        page.close()


def test_preparation_timeout_explains_network_recovery_without_claiming_retry(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "intake:preparation_timeout"
    runner.run_id = "88888888-8888-4888-8888-888888888888"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "2" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("准备仓库版本")
        expect(page.locator("#failure-reason")).to_have_text(
            "仓库准备超过总时间限制，尚未完成指定版本的下载。"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "请检查运行后端与 GitHub 的连接；网络恢复后可使用相同配置重试。更多说明见使用文档。"
        )
        expect(page.locator("#failure-code")).to_have_text("intake:preparation_timeout")
        expect(page.locator("#failure-run-id")).to_have_text(
            "88888888-8888-4888-8888-888888888888"
        )
        page.locator("#language-select").select_option("en")
        expect(page.locator("#failure-stage")).to_have_text(
            "Preparing repository version"
        )
        expect(page.locator("#failure-reason")).to_have_text(
            "Preparing the repository exceeded the total time limit; the specified version was not fully downloaded."
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "Check the connection between the runtime backend and GitHub; retry with the same configuration after the network recovers. See the user guide for more information."
        )
        expect(page.locator("#failure-code")).to_have_text("intake:preparation_timeout")
        expect(page.locator("#failure-run-id")).to_have_text(
            "88888888-8888-4888-8888-888888888888"
        )
        page.close()


def test_poll_network_failure_retries_without_resubmitting(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        failed = False

        def fail_once(route: Route) -> None:
            nonlocal failed
            if route.request.method == "GET" and not failed:
                failed = True
                route.abort()
            else:
                route.continue_()

        page.route("**/api/jobs/*", fail_once)
        page.goto(base_url)
        _fill_required(page, "c" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        assert failed
        assert len(runner.submissions) == 1
        page.close()


def test_poll_network_failure_preserves_last_progress_and_marks_status_unknown(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    progress = {
        "phase": "checking_application",
        "completed_phases": ["preparing_repository"],
        "latest_event": "application_check_started",
        "evidence_age_seconds": 12,
    }
    interrupted = False
    running_sent = False

    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def interrupt_poll(route: Route) -> None:
            nonlocal interrupted, running_sent
            if route.request.method != "GET" or "/report/" in route.request.url:
                route.continue_()
            elif not running_sent:
                running_sent = True
                _add_progress(route, progress)
            else:
                interrupted = True
                route.abort()

        page.route("**/api/jobs/*", interrupt_poll)
        page.goto(base_url)
        _fill_required(page, "c" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#phase-list")).to_contain_text("应用检查", timeout=6000)
        expect(page.locator("#status-title")).to_have_text("状态未知", timeout=6000)
        assert interrupted
        expect(page.locator("#phase-list")).to_contain_text("应用检查")
        expect(page.locator("#phase-list")).not_to_contain_text("当前阶段")
        page.close()


def test_new_submission_clears_previous_progress(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    first_job_id = "11111111-1111-4111-8111-111111111111"
    second_job_id = "22222222-2222-4222-8222-222222222222"
    first_status_sent = False
    submission_count = 0
    progress = {
        "phase": "checking_application",
        "completed_phases": ["preparing_repository"],
        "latest_event": "application_check_started",
    }

    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def jobs(route: Route) -> None:
            nonlocal first_status_sent, submission_count
            if route.request.method == "POST" and route.request.url.endswith(
                "/api/jobs"
            ):
                submission_count += 1
                response = route.fetch()
                payload = json.loads(response.text())
                payload["job_id"] = (
                    first_job_id if submission_count == 1 else second_job_id
                )
                route.fulfill(
                    status=response.status,
                    headers=response.headers,
                    body=json.dumps(payload),
                )
            elif route.request.method == "GET" and "/report/" not in route.request.url:
                if not first_status_sent:
                    first_status_sent = True
                    _add_progress(route, progress)
                else:
                    route.continue_()
            else:
                route.continue_()

        page.route("**/api/jobs", jobs)
        page.route("**/api/jobs", jobs)
        page.route("**/api/jobs", jobs)
        page.route("**/api/jobs/**", jobs)
        page.goto(base_url)
        _fill_required(page, "d" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#phase-list")).to_contain_text("应用检查", timeout=6000)
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)

        page.get_by_role("button", name="新建检查").click()
        _fill_required(page, "e" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        expect(page.locator("#phase-list")).not_to_contain_text("应用检查")
        expect(page.locator("#phase-progress-unavailable")).to_be_visible()
        page.close()


def test_new_submission_clears_previous_report_links(browser_extra: Browser) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        get_count = 0

        def block_new_job_polls(route: Route) -> None:
            nonlocal get_count
            if route.request.method == "GET":
                get_count += 1
                if get_count >= 3:
                    route.abort()
                    return
            route.continue_()

        page.route("**/api/jobs/*", block_new_job_polls)
        page.goto(base_url)
        _fill_required(page, "d" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#html-report")).to_be_visible(timeout=6000)
        page.get_by_role("button", name="新建检查").click()
        _fill_required(page, "e" * 40)
        page.get_by_role("button", name="开始检查").click()
        assert page.locator("#html-report").is_hidden()
        assert page.locator("#json-report").is_hidden()
        assert len(runner.submissions) == 2
        page.close()


def test_submit_network_failure_locks_form_without_resubmitting(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        failed = False

        def fail_submit(route: Route) -> None:
            nonlocal failed
            if route.request.method == "POST" and not failed:
                failed = True
                route.abort()
            else:
                route.continue_()

        page.route("**/api/jobs", fail_submit)
        page.goto(base_url)
        _fill_required(page, "f" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#form-errors")).to_contain_text("提交结果未知")
        expect(page.locator("#submit-button")).to_be_disabled()
        assert len(runner.submissions) == 0
        page.close()


def test_session_storage_write_failure_does_not_fail_submitted_job(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.add_init_script(
            "Storage.prototype.setItem = function() { throw new Error('denied'); };"
        )
        page.goto(base_url)
        _fill_required(page, "8" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        assert len(runner.submissions) == 1
        assert page.locator("#form-errors").inner_text() == ""
        page.close()


def test_new_submission_unknown_clears_old_job_before_reload(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        post_count = 0

        def fail_second_submit(route: Route) -> None:
            nonlocal post_count
            if route.request.method == "POST":
                post_count += 1
                if post_count == 2:
                    route.abort()
                    return
            route.continue_()

        page.route("**/api/jobs", fail_second_submit)
        page.goto(base_url)
        _fill_required(page, "1" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        expect(page.locator("#html-report")).to_be_visible()
        page.get_by_role("button", name="新建检查").click()
        _fill_required(page, "2" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#form-errors")).to_contain_text("刷新无法恢复")
        page.reload()
        expect(page.locator("#status-title")).to_have_text("等待开始")
        expect(page.locator("#html-report")).to_be_hidden()
        page.close()


@pytest.mark.parametrize("terminal", ["unknown", "bogus"])
def test_unknown_or_malformed_status_is_not_treated_as_success(
    browser_extra: Browser, terminal: str
) -> None:
    runner = BrowserRunner(terminal=terminal)
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "3" * 40)
        page.get_by_role("button", name="开始检查").click()
        if terminal == "unknown":
            expect(page.locator("#status-title")).to_have_text("状态未知", timeout=6000)
            expect(page.locator("#reason")).to_have_text("任务状态不可用")
            expect(page.locator("#submit-button")).to_be_enabled()
            assert (
                page.evaluate("sessionStorage.getItem('repotrial-active-job-id')")
                is None
            )
            calls = runner.status_calls
            page.wait_for_timeout(1200)
            assert runner.status_calls == calls
        else:
            expect(page.locator("#reason")).to_have_text(
                "暂时无法获取状态，当前任务未判定停止；正在重试。",
                timeout=6000,
            )
            expect(page.locator("#submit-button")).to_be_disabled()
            page.wait_for_timeout(1200)
            assert runner.status_calls >= 3
        page.close()


def test_malformed_http_error_relocalizes_after_language_switch(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def malformed_response(route: Route) -> None:
            if route.request.method == "POST":
                route.fulfill(
                    status=502,
                    content_type="text/plain",
                    body="upstream unavailable",
                )
            else:
                route.continue_()

        page.route("**/api/jobs", malformed_response)
        page.goto(base_url)
        _fill_required(page, "a" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#form-errors")).to_have_text("服务返回了无法读取的响应。")
        page.locator("#language-select").select_option("en")
        expect(page.locator("#form-errors")).to_have_text(
            "The service returned an unreadable response."
        )
        page.close()


def test_language_switch_translates_static_help_and_remembers_form(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        page.locator("#language-select").select_option("en")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page.locator("h1")).to_have_text("New inspection")
        expect(page.locator("label[for=url]")).to_have_text("GitHub repository")
        expect(page.locator("[name=commit_sha]")).to_have_attribute(
            "placeholder", "Enter the full 40-character lowercase commit SHA"
        )
        expect(page.locator("#submit-button")).to_have_text("Start check")
        expect(page.locator("#status-title")).to_have_text("Waiting to start")
        expect(page.locator("#status-desc")).to_have_text(
            "Configure the repository to start a check"
        )
        assert page.locator("[role=progressbar]").count() == 0
        assert "progress" not in page.locator("body").inner_text().lower()
        expect(page.locator("[name=url]")).to_have_value("https://github.com/acme/demo")
        page.locator("a[data-guide=sha]").click()
        expect(page.locator("#guide-dialog h2")).to_have_text("Configuration guide")
        expect(page.locator("#guide-sha h3")).to_contain_text("Commit SHA")
        page.reload()
        expect(page.locator("#language-select")).to_have_value("en")
        expect(page.locator("h1")).to_have_text("New inspection")
        page.close()


def test_language_switch_rerenders_doctor_and_status_without_requests(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        doctor_calls = 0

        def count_doctor(route: Route) -> None:
            nonlocal doctor_calls
            if route.request.method == "POST":
                doctor_calls += 1
            route.continue_()

        page.route("**/api/doctor", count_doctor)
        page.goto(base_url)
        page.get_by_role("button", name="检查环境").click()
        expect(
            page.locator("#environment").get_by_text("Docker 未运行")
        ).to_be_visible()
        page.locator("#language-select").select_option("en")
        expect(
            page.locator("#environment").get_by_text("Docker 未运行")
        ).to_be_visible()
        expect(
            page.locator("#environment").get_by_text("Reason: Docker 未运行")
        ).to_be_visible()
        expect(
            page.locator("#environment").get_by_text(
                "Suggested action: 启动 Docker 后重试。"
            )
        ).to_be_visible()
        assert doctor_calls == 1
        _fill_required(page, "9" * 40)
        expect(page.locator("#submit-button")).to_be_disabled()
        page.locator("#language-select").select_option("zh")
        expect(page.locator("#submit-button")).to_be_disabled()
        assert runner.submissions == []
        page.close()


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_task_states_do_not_show_unmeasurable_progress(
    browser_extra: Browser, terminal: str
) -> None:
    runner = BrowserRunner(terminal=terminal)
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "b" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_contain_text(
            "完成" if terminal == "completed" else "失败", timeout=6000
        )
        assert page.locator("[role=progressbar]").count() == 0
        body_text = page.locator("body").inner_text()
        assert "检查进度" not in body_text
        assert "100%" not in body_text
        page.close()


def test_running_task_does_not_show_unknown_progress_placeholder(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="running")
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "c" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        assert page.locator("[role=progressbar]").count() == 0
        assert "进度未知" not in page.locator("body").inner_text()
        page.close()


def test_guest_output_malformed_failure_is_explained_without_blame(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "compatibility:guest_output_malformed"
    runner.run_id = "44444444-4444-4444-8444-444444444444"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "d" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("隔离环境兼容性检查")
        expect(page.locator("#failure-reason")).to_have_text(
            "隔离环境返回的数据不符合工具预期，无法继续检查"
        )
        expect(page.locator("#failure-next-step")).to_contain_text(
            "维护者检查隔离环境的校验记录"
        )
        expect(page.locator("#failure-run-id")).to_have_text(
            "44444444-4444-4444-8444-444444444444"
        )
        page.close()


def test_bare_guest_output_malformed_failure_is_explained_in_both_languages(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "guest_output_malformed"
    runner.run_id = "33333333-3333-4333-8333-333333333333"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "e" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("隔离环境兼容性检查")
        expect(page.locator("#failure-reason")).to_have_text(
            "隔离环境返回的数据不符合工具预期，无法继续检查"
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "请保留运行标识，供维护者检查隔离环境的校验记录。"
        )
        expect(page.locator("#failure-code")).to_have_text("guest_output_malformed")
        expect(page.locator("#failure-run-id")).to_have_text(
            "33333333-3333-4333-8333-333333333333"
        )
        page.locator("#language-select").select_option("en")
        expect(page.locator("#failure-stage")).to_have_text(
            "Isolated environment compatibility check"
        )
        expect(page.locator("#failure-reason")).to_have_text(
            "The isolated environment returned data that does not match the tool's expected format, so the check could not continue."
        )
        expect(page.locator("#failure-next-step")).to_have_text(
            "Keep the run ID so maintainers can inspect the isolated environment validation records."
        )
        expect(page.locator("#failure-code")).to_have_text("guest_output_malformed")
        page.close()


def test_metadata_cooldown_stops_auto_retries_but_manual_fields_submit(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        metadata_calls = 0

        def rate_limited_metadata(route: Route) -> None:
            nonlocal metadata_calls
            metadata_calls += 1
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "commit_sha": None,
                        "commit_url": None,
                        "compose_candidates": [],
                        "selected_compose_path": None,
                        "ports": [],
                        "warnings": [],
                        "errors": ["github_forbidden"],
                        "retry_after_seconds": 3,
                    }
                ),
            )

        page.route("**/api/repository", rate_limited_metadata)
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("#repository-meta-status")).to_contain_text(
            "暂时限制查询", timeout=4000
        )
        page.locator("summary").click()
        page.locator("[name=commit_sha]").fill("e" * 40)
        expect(page.locator("#commit-link")).to_have_attribute(
            "href", "https://github.com/acme/demo/commit/" + "e" * 40
        )
        page.locator("[name=compose_path]").fill("compose.yml")
        page.locator("[name=container_port]").fill("8080")
        page.wait_for_timeout(1200)
        assert metadata_calls == 1
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        assert len(runner.submissions) == 1
        page.close()


@pytest.mark.parametrize("restore_field", ["port", "compose"])
def test_stale_metadata_request_does_not_block_same_key_retry(
    browser_extra: Browser, restore_field: str
) -> None:
    runner = BrowserRunner()
    repository = BrowserRepository(delay=1.0)
    with live_server(runner, repository=repository) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("#repository-meta-status")).to_have_text(
            "正在识别提交、Compose 和端口…", timeout=3000
        )
        if restore_field == "port":
            page.locator("[name=container_port]").fill("8080")
            page.locator("[name=container_port]").fill("")
        else:
            page.locator("summary").click()
            page.locator("[name=compose_path]").fill("compose.yml")
            page.locator("[name=compose_path]").fill("")
        expect(page.locator("#repository-meta-status")).to_have_text(
            "自动识别失败，可继续手动填写。", timeout=5000
        )
        expect(page.locator("#detect-repository")).to_be_enabled()
        assert repository.calls >= 2
        page.close()


def test_repository_metadata_autofills_commit_compose_and_single_port(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        sha = "a" * 40

        def metadata(route: Route) -> None:
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "commit_sha": sha,
                        "commit_url": f"https://github.com/acme/demo/commit/{sha}",
                        "compose_candidates": ["docker-compose.yml"],
                        "selected_compose_path": "docker-compose.yml",
                        "ports": [{"port": 5000, "service": "web"}],
                        "warnings": [],
                    }
                ),
            )

        page.route("**/api/repository", metadata)
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("[name=commit_sha]")).to_have_value(sha, timeout=4000)
        expect(page.locator("#commit-link")).to_have_attribute(
            "href", f"https://github.com/acme/demo/commit/{sha}"
        )
        expect(page.locator("[name=compose_path]")).to_have_value("docker-compose.yml")
        expect(page.locator("[name=container_port]")).to_have_value("5000")
        page.close()


def test_repository_metadata_requires_explicit_choice_for_multiple_ports(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def metadata(route: Route) -> None:
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "commit_sha": "b" * 40,
                        "commit_url": (
                            "https://github.com/acme/demo/commit/" + "b" * 40
                        ),
                        "compose_candidates": [
                            "docker-compose.yml",
                            "deploy/compose.yml",
                        ],
                        "selected_compose_path": "docker-compose.yml",
                        "ports": [
                            {"port": 5000, "service": "web"},
                            {"port": 5432, "service": "db"},
                        ],
                        "warnings": [],
                    }
                ),
            )

        page.route("**/api/repository", metadata)
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("#port-options")).to_be_visible(timeout=4000)
        expect(page.locator("[name=container_port]")).to_have_value("")
        expect(page.locator("#port-options")).to_contain_text("web · 5000")
        expect(page.locator("#port-options")).to_contain_text("db · 5432")
        page.locator("#port-options").select_option("5432")
        expect(page.locator("[name=container_port]")).to_have_value("5432")
        page.locator("summary").click()
        expect(page.locator("#compose-options")).to_be_visible()
        page.close()


def test_changing_compose_rediscovers_port_and_clears_old_candidate(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        calls = 0

        def metadata(route: Route) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                body = {
                    "commit_sha": "b" * 40,
                    "commit_url": "https://github.com/acme/demo/commit/" + "b" * 40,
                    "compose_candidates": ["compose.yml", "deploy/compose.yml"],
                    "selected_compose_path": "compose.yml",
                    "ports": [{"port": 5000, "service": "web"}],
                    "warnings": [],
                    "errors": [],
                }
            else:
                body = {
                    "commit_sha": "b" * 40,
                    "commit_url": "https://github.com/acme/demo/commit/" + "b" * 40,
                    "compose_candidates": ["compose.yml", "deploy/compose.yml"],
                    "selected_compose_path": "deploy/compose.yml",
                    "ports": [{"port": 3000, "service": "web"}],
                    "warnings": [],
                    "errors": [],
                }
            route.fulfill(content_type="application/json", body=json.dumps(body))

        page.route("**/api/repository", metadata)
        page.goto(base_url)
        page.locator("[name=url]").fill("https://github.com/acme/demo")
        expect(page.locator("[name=container_port]")).to_have_value(
            "5000", timeout=4000
        )
        page.locator("summary").click()
        page.locator("#compose-options").select_option("deploy/compose.yml")
        expect(page.locator("[name=container_port]")).to_have_value(
            "3000", timeout=4000
        )
        assert calls >= 2
        page.close()


def test_failed_task_explains_stage_reason_next_step_and_run_id(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "intake:clone_timeout"
    runner.run_id = "22222222-2222-4222-8222-222222222222"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "c" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-details")).to_be_visible()
        expect(page.locator("#failure-stage")).to_contain_text("下载仓库")
        expect(page.locator("#failure-code")).to_contain_text("intake:clone_timeout")
        expect(page.locator("#failure-next-step")).to_contain_text("网络")
        expect(page.locator("#failure-run-id")).to_contain_text(
            "22222222-2222-4222-8222-222222222222"
        )
        page.close()


def test_failure_stage_and_code_are_inferred_from_safe_stop_reason(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="failed")
    runner.terminal_stop_reason = "intake:fetch"
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "f" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#failure-stage")).to_have_text("下载仓库")
        expect(page.locator("#failure-code")).to_have_text("intake:fetch")
        expect(page.locator("#failure-reason")).to_have_text("intake:fetch")
        page.close()


def test_elapsed_clock_repaints_between_status_polls(browser_extra: Browser) -> None:
    runner = BrowserRunner(terminal="completed")
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "d" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#elapsed")).to_contain_text("秒", timeout=3000)
        first = page.locator("#elapsed").inner_text()
        page.wait_for_timeout(1200)
        second = page.locator("#elapsed").inner_text()
        assert second != first or runner.status_calls >= 2
        assert page.evaluate("formatElapsed(61.25)").endswith("1 秒")
        page.close()


def test_terminal_elapsed_reconciles_down_and_stays_frozen_on_language_switch(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner(terminal="completed")
    runner.terminal_elapsed = 0.5
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        _fill_required(page, "e" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("已完成", timeout=6000)
        expect(page.locator("#elapsed")).to_have_text("0 秒")
        page.locator("#language-select").select_option("en")
        expect(page.locator("#elapsed")).to_have_text("0 sec")
        page.wait_for_timeout(500)
        expect(page.locator("#elapsed")).to_have_text("0 sec")
        page.close()


def test_model_settings_are_advanced_and_compose_stays_primary(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        assert page.locator("#model-settings[open]").count() == 0
        assert page.locator("#model-settings #compose_path").count() == 0
        expect(page.locator("#compose_path")).to_be_visible()
        page.locator("#model-settings summary").click()
        page.locator("#model_endpoint").fill("https://api.deepseek.com")
        page.locator("#model_name").fill("deepseek-v4-flash")
        page.locator("#model_api_key").fill("browser-secret")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")
        expect(page.locator("#model_api_key")).to_have_value("")
        page.locator("#clear-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("模型设置已清除")
        expect(page.locator("#clear-model-settings")).to_have_text("清除模型设置")
        expect(page.locator("#model_endpoint")).to_have_value("")
        expect(page.locator("#model_name")).to_have_value("")
        page.locator("#language-select").select_option("en")
        expect(page.locator("#model-settings")).to_contain_text("Model base URL")
        expect(page.locator("#clear-model-settings")).to_have_text(
            "Clear model settings"
        )
        expect(page.locator("#model_api_key")).to_have_attribute(
            "placeholder", "Leave blank to reuse a saved key for this endpoint"
        )
        expect(page.locator("#model_name")).to_have_attribute("role", "combobox")
        expect(page.locator("#model_name")).to_have_attribute(
            "aria-controls", "model-name-options"
        )
        expect(page.locator("#compose_path")).to_be_visible()
        page.locator("[data-guide=model]").click()
        expect(page.locator("#guide-dialog")).to_contain_text(
            "Save or clear the API key here."
        )
        expect(page.locator("#guide-dialog")).not_to_contain_text(
            "REPOTRIAL_MODEL_API_KEY"
        )
        page.close()

        guide_page = browser_extra.new_page()
        guide_page.goto(f"{base_url}?view=guide&lang=en")
        expect(guide_page.locator("#advanced")).to_contain_text("Model settings")
        expect(guide_page.locator("#advanced")).not_to_contain_text("Compose path")
        expect(guide_page.locator("#fields")).to_contain_text("Compose relative path")
        guide_page.goto(f"{base_url}?view=guide&lang=zh")
        expect(guide_page.locator("#advanced")).to_contain_text("模型设置")
        expect(guide_page.locator("#advanced")).not_to_contain_text("Compose 路径")
        expect(guide_page.locator("#fields")).to_contain_text("Compose 相对路径")
        guide_page.close()


def test_model_discovery_selects_or_allows_manual_model_entry(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def discovery(route: Route) -> None:
            assert route.request.method == "POST"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"models": ["qwen", "llama"]}),
            )

        page.route("**/api/settings/model/discover", discovery)
        page.goto(base_url)
        assert page.locator("#model_provider").count() == 0
        page.locator("#model-settings summary").click()
        page.locator("#model_endpoint").fill("https://model.example/v1")
        page.locator("#model_api_key").fill("new-browser-key")
        page.locator("#discover-models").click()
        page.locator("#model-name-toggle").click()
        expect(page.locator("#model-name-options")).to_be_visible()
        expect(page.locator("#model-name-options [role=option]")).to_have_count(2)
        page.screenshot(path="/tmp/repotrial-model-combobox-expanded.png")
        page.locator("#model-name-options [role=option]").first.click()
        page.locator("#model_name").fill("qwen")
        expect(page.locator("#model-name-options [role=option]")).to_have_count(1)
        page.locator("#model_name").press("Escape")
        page.locator("#model-name-toggle").click()
        expect(page.locator("#model-name-options [role=option]")).to_have_count(2)
        page.locator("#model-name-options [role=option]").first.click()
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")

        page.locator("#model_name").fill("manual-model")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")
        page.locator("#language-select").select_option("en")
        expect(page.locator("#model-settings")).to_contain_text("Get models")
        page.close()


def test_model_settings_send_discovery_save_and_disable_payloads(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    captured: list[tuple[str, dict[str, object]]] = []
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def model_api(route: Route) -> None:
            payload = json.loads(route.request.post_data or "{}")
            if route.request.method in {"POST", "PUT", "DELETE"}:
                captured.append((route.request.method, payload))
            if route.request.method == "POST":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": ["qwen"]}),
                )
            elif route.request.method == "PUT":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "endpoint": payload.get("endpoint"),
                            "model_name": payload.get("model_name"),
                            "api_key_configured": bool(payload.get("api_key")),
                        }
                    ),
                )
            else:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "endpoint": "https://api.example/v1",
                            "model_name": "qwen",
                            "api_key_configured": False,
                        }
                    ),
                )

        page.route("**/api/settings/model/discover", model_api)
        page.route("**/api/settings/model", model_api)
        page.route("**/api/settings/model/key", model_api)
        page.goto(base_url)
        page.locator("#model-settings summary").click()
        page.locator("#model_endpoint").fill("https://api.example/v1/")
        page.locator("#model_api_key").fill("secret")
        page.locator("#discover-models").click()
        page.locator("#model-name-toggle").click()
        expect(page.locator("#model-name-options [role=option]")).to_have_count(1)
        page.locator("#model-name-options [role=option]").click()
        expect(page.locator("#model_name")).to_have_value("qwen")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")
        page.locator("#model_endpoint").fill("https://api.example/v1")
        page.locator("#model_name").fill("manual-model")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")
        page.locator("#model_endpoint").fill("")
        page.locator("#model_name").fill("")
        page.locator("#save-model-settings").click()
        expect(page.locator("#model-settings-error")).to_have_text("已保存")
        page.locator("#clear-model-settings").click()
        assert captured[0] == (
            "POST",
            {"endpoint": "https://api.example/v1", "api_key": "secret"},
        )
        puts = [payload for method, payload in captured if method == "PUT"]
        assert puts[0]["provider"] == "custom"
        assert puts[0]["endpoint"] == "https://api.example/v1"
        assert puts[0]["model_name"] == "qwen"
        assert puts[1]["model_name"] == "manual-model"
        assert puts[2] == {
            "provider": None,
            "endpoint": None,
            "model_name": None,
            "api_key": "",
        }
        assert any(method == "DELETE" for method, _ in captured)
        page.close()


def test_model_discovery_discards_stale_response_and_reenables_button(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    pending: list[Route] = []
    calls = 0
    with live_server(runner) as base_url:
        page = browser_extra.new_page()

        def discovery(route: Route) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                pending.append(route)
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"models": ["new-model"]}),
            )

        page.route("**/api/settings/model/discover", discovery)
        page.goto(base_url)
        page.locator("#model-settings summary").click()
        page.locator("#model_endpoint").fill("https://old.example/v1")
        page.dispatch_event("#discover-models", "click")
        expect(page.locator("#discover-models")).to_be_disabled()
        page.locator("#model_endpoint").fill("https://new.example/v1")
        expect(page.locator("#discover-models")).to_be_enabled()
        pending[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": ["stale-model"]}),
        )
        page.wait_for_timeout(100)
        expect(page.locator("#model-name-options [role=option]")).to_have_count(0)
        page.locator("#discover-models").click()
        page.locator("#model-name-toggle").click()
        expect(page.locator("#model-name-options [role=option]")).to_have_count(1)
        expect(page.locator("#model-name-options [role=option]")).to_contain_text(
            "new-model"
        )
        page.close()


def test_stop_waits_for_server_terminal_and_reruns_frozen_request(
    browser_extra: Browser,
) -> None:
    runner = BrowserRunner()
    payloads = _capture_job_payloads
    with live_server(runner) as base_url:
        page = browser_extra.new_page()
        captured = payloads(page)
        status_calls = 0
        rerun_id = "22222222-2222-4222-8222-222222222222"
        snapshot = {
            "url": "https://github.com/acme/demo",
            "commit_sha": "a" * 40,
            "container_port": 8080,
            "compose_path": "compose.yml",
        }

        def jobs(route: Route) -> None:
            nonlocal status_calls
            request = route.request
            if request.method == "POST" and request.url.endswith("/stop"):
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "job_id": JOB_ID,
                            "state": "running",
                            "control": "stopping",
                            "request": snapshot,
                        }
                    ),
                )
                return
            if (
                request.method == "POST"
                and request.url.endswith("/api/jobs")
                and len(captured) > 1
            ):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"job_id": rerun_id}),
                )
                return
            if request.method == "GET" and request.url.endswith(f"/api/jobs/{JOB_ID}"):
                status_calls += 1
                control = (
                    "stopping"
                    if status_calls == 2
                    else "stopped"
                    if status_calls >= 3
                    else None
                )
                body = {"job_id": JOB_ID, "state": "running", "request": snapshot}
                if control:
                    body["control"] = control
                route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )
                return
            if request.method == "GET" and request.url.endswith(
                f"/api/jobs/{rerun_id}"
            ):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {"job_id": rerun_id, "state": "running", "request": snapshot}
                    ),
                )
                return
            route.continue_()

        page.route("**/api/jobs", jobs)
        page.route("**/api/jobs/**", jobs)
        page.goto(base_url)
        page.evaluate(
            """() => {
                const originalFetch = window.fetch.bind(window);
                window.__releaseStop = null;
                window.fetch = (input, init) => {
                    const url = typeof input === 'string' ? input : input.url;
                    if (!url.endsWith('/stop')) return originalFetch(input, init);
                    return new Promise((resolve, reject) => {
                        window.__releaseStop = () => originalFetch(input, init).then(resolve, reject);
                    });
                };
            }"""
        )
        _fill_required(page, "b" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#stop-button")).to_be_enabled(timeout=6000)
        page.locator("#stop-button").click()
        page.wait_for_function("typeof window.__releaseStop === 'function'")
        expect(page.locator("#status-title")).to_have_text("已停止", timeout=6000)
        expect(page.locator("#retry-button")).to_be_enabled()
        page.locator("[name=commit_sha]").fill("c" * 40, force=True)
        page.locator("#retry-button").click()
        page.evaluate("window.__releaseStop()")
        expect(page.locator("#stop-button")).to_be_enabled(timeout=6000)
        assert len(captured) >= 2
        assert captured[1]["commit_sha"] == "a" * 40
        assert "model_api_key" not in captured[1]
        page.close()


@pytest.mark.parametrize("missing", ["sbx_daemon", "sbx_diagnose", "sbx_inventory"])
def test_incomplete_startup_and_manual_reports_remain_blocked(
    browser_extra: Browser, missing: str
) -> None:
    checks = tuple(
        DoctorCheck(name, "PASS", True, "ok", None)
        for name in ("sbx_daemon", "sbx_diagnose", "sbx_inventory")
        if name != missing
    )

    class IncompleteServices:
        def ensure_ready(self) -> DoctorReport:
            return DoctorReport(checks=checks)

    runner = BrowserRunner()
    with live_server(
        runner,
        services=IncompleteServices(),
        doctor=lambda: DoctorReport(checks=checks),
    ) as base_url:
        page = browser_extra.new_page()
        page.goto(base_url)
        expect(page.locator("#environment-state-badge")).to_have_text("需处理")
        expect(page.locator("#submit-button")).to_be_disabled()
        page.get_by_role("button", name="检查环境").click()
        expect(page.locator("#environment-state-badge")).to_have_text("需处理")
        expect(page.locator("#submit-button")).to_be_disabled()
        page.close()
