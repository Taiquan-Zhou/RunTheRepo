from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Route, expect, sync_playwright
from test_console_browser import BrowserRunner, live_server


@pytest.fixture(scope="module")
def visual_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        yield playwright.chromium.launch(headless=True)


def _fill_required(page, sha: str = "a" * 40) -> None:
    page.locator("[name=url]").fill("https://github.com/acme/demo")
    page.locator("[name=commit_sha]").fill(sha)
    page.locator("[name=container_port]").fill("8080")


def test_idle_is_a_single_workspace_with_product_shell(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        expect(page.locator("h1")).to_have_text("新建检查")
        assert page.locator("#production-sidebar").count() == 1
        assert page.locator(".hero, .hero-art").count() == 0
        expect(page.locator("#config-card")).to_be_visible()
        expect(page.locator("#environment-card")).to_be_visible()
        assert page.locator("body").get_attribute("data-console-state") == "idle"
        header = page.locator(".topbar").bounding_box()
        language = page.locator("#language-select").bounding_box()
        assert header and language and language["y"] < header["y"] + header["height"]
        body_text = page.locator("body").inner_text()
        for stray in (
            "Inspection setup",
            "Environment",
            "Sandbox readiness",
            "Evidence",
        ):
            assert stray not in body_text
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "idle-1440.png"), full_page=True)
        page.close()


def test_idle_config_and_environment_cards_keep_equal_bottoms(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        config = page.locator("#config-card").bounding_box()
        environment = page.locator("#environment-card").bounding_box()
        assert config and environment
        config_bottom = config["y"] + config["height"]
        environment_bottom = environment["y"] + environment["height"]
        assert abs(config_bottom - environment_bottom) <= 1, {
            "config": config,
            "environment": environment,
        }
        page.close()


def test_workspace_surface_and_flow_context_follow_console_state(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        expect(page.locator("#workspace-surface")).to_be_visible()
        expect(page.locator("#workspace-flow")).to_be_visible()
        expect(page.locator("#workspace-eyebrow")).to_have_text("仓库检查工作台")
        expect(page.locator("#workspace-flow")).to_have_attribute(
            "aria-label", "检查模式"
        )
        expect(page.locator("#workspace-flow .flow-step")).to_have_count(3)
        expect(
            page.locator("#workspace-flow .flow-step[aria-current='step']")
        ).to_have_text("仓库")
        page.close()

    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page, sha="b" * 40)
        expect(page.locator("#fact-sha")).to_have_text("bbbbbbbbbbbb…")
        expect(page.locator("#fact-sha")).to_have_attribute("title", "b" * 40)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        expect(
            page.locator("#workspace-flow .flow-step[aria-current='step']")
        ).to_have_text("运行")
        assert "%" not in page.locator("#workspace-flow").inner_text()
        page.close()

    with live_server(BrowserRunner(terminal="completed")) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#html-report")).to_be_visible(timeout=6000)
        expect(
            page.locator("#workspace-flow .flow-step[aria-current='step']")
        ).to_have_text("证据")
        page.close()


def test_workspace_motion_respects_reduced_motion_and_mobile_bounds(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 390, "height": 844})
        page.emulate_media(reduced_motion="reduce")
        page.goto(base_url)
        expect(page.locator("#workspace-surface")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        transition = page.locator("#workspace-surface").evaluate(
            "node => getComputedStyle(node).transitionDuration"
        )
        assert transition in {"0s", "0ms"}
        page.close()


def test_idle_environment_is_compact_and_real(visual_browser: Browser) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        page.get_by_role("button", name="检查环境").click()
        expect(page.locator("#environment-state-badge")).to_have_text("需处理")
        expect(page.locator("#environment-preflight")).to_contain_text("运行前检查")
        expect(page.locator("#environment")).to_contain_text("Docker 未运行")
        expect(page.locator("#environment")).to_have_attribute(
            "data-environment-state", "error"
        )
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "environment-1440.png"), full_page=True)
        assert page.locator("#environment-card").bounding_box()["height"] < 640
        page.close()


def test_workspace_heading_and_environment_badge_translate(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base_url)
        expect(page.locator("#workspace-eyebrow")).to_have_text("仓库检查工作台")
        page.locator("#language-select").select_option("en")
        expect(page.locator("#workspace-eyebrow")).to_have_text(
            "Repository inspection workspace"
        )
        expect(page.locator("#environment-state-badge")).to_have_text("Ready")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.close()


def test_submit_switches_to_run_workspace_and_compact_summary(
    visual_browser: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "running-1440.png"), full_page=True)
        expect(page.locator("#status-title")).to_be_visible()
        assert page.locator("body").get_attribute("data-console-state") in {
            "submitting",
            "running",
            "completed",
        }
        expect(page.locator("#run-summary")).to_contain_text("acme/demo")
        expect(page.locator(".activity")).to_be_visible()
        expect(page.locator("#environment-card")).to_be_hidden()
        expect(page.locator("#new-inspection-button")).to_be_disabled()
        assert page.locator("#config-card").is_hidden()
        page.close()


def test_completed_report_is_primary_and_terminal_actions_are_visible(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner(terminal="completed")) as base_url:
        page = visual_browser.new_page(viewport={"width": 1920, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#html-report")).to_be_visible(timeout=6000)
        expect(page.locator("h1")).to_have_text("检查结果")
        expect(page.locator("#html-report")).to_contain_text("HTML")
        expect(page.locator("#json-report")).to_be_visible()
        expect(page.locator("#retry-button")).to_be_visible()
        expect(page.locator("#new-inspection-button")).to_be_visible()
        expect(page.locator("#retry-button")).to_have_class("btn secondary")
        page.locator("#language-select").select_option("en")
        expect(page.locator("h1")).to_have_text("Inspection result")
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "completed-en-1920.png"), full_page=True)
        run_box = page.locator("#task-card").bounding_box()
        report_box = page.locator("#report-card").bounding_box()
        assert (
            run_box
            and report_box
            and report_box["width"] > run_box["width"] * 1.7
            and abs(report_box["y"] - run_box["y"]) <= 1
        )
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "completed-1920.png"), full_page=True)
        page.close()


def test_failed_terminal_cards_do_not_stretch_to_peer_height(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner(terminal="failed")) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)

        report = page.locator("#report-card").bounding_box()
        task = page.locator("#task-card").bounding_box()
        actions = page.locator("#report-card .report-actions").bounding_box()
        assert report and task and actions
        assert abs(report["y"] - task["y"]) <= 1
        report_content_gap = (
            report["y"] + report["height"] - (actions["y"] + actions["height"])
        )
        assert report_content_gap < 80, report
        page.close()


def test_transient_unknown_and_submission_unknown_keep_workspace_locked(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})

        def abort_status(route: Route) -> None:
            if route.request.method == "GET":
                route.abort()
            else:
                route.continue_()

        page.route("**/api/jobs/*", abort_status)
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("状态未知", timeout=6000)
        expect(page.locator("#new-inspection-button")).to_be_disabled()
        expect(page.locator("#retry-button")).to_be_disabled()
        page.close()

    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})

        def missing_job_id(route: Route) -> None:
            if route.request.method == "POST":
                route.fulfill(status=200, content_type="application/json", body="{}")
            else:
                route.continue_()

        page.route("**/api/jobs", missing_job_id)
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("提交结果未知", timeout=6000)
        expect(page.locator("#intro-title")).to_have_text("无法确认提交状态")
        expect(page.locator("#new-inspection-button")).to_be_disabled()
        expect(page.locator("#retry-button")).to_be_disabled()
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(output / "submission-unknown-1440.png"), full_page=True
        )
        page.close()


def test_duplicate_programmatic_submit_keeps_running_state(
    visual_browser: Browser,
) -> None:
    runner = BrowserRunner()
    with live_server(runner) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        before = page.locator("#run-summary").inner_text()
        page.locator("#trial-form").evaluate("form => form.requestSubmit()")
        expect(page.locator("body")).to_have_attribute("data-console-state", "running")
        assert page.locator("#run-summary").inner_text() == before
        assert len(runner.submissions) == 1
        page.close()


def test_restored_unknown_summary_relocalizes_after_active_job_is_cleared(
    visual_browser: Browser,
) -> None:
    runner = BrowserRunner(terminal="unknown")
    with live_server(runner) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script(
            "sessionStorage.setItem('repotrial-active-job-id', "
            "'11111111-1111-4111-8111-111111111111');"
        )
        page.goto(base_url)
        expect(page.locator("#status-title")).to_have_text("状态未知", timeout=6000)
        expect(page.locator("#run-summary")).to_contain_text("正在恢复已保存的检查")
        page.locator("#language-select").select_option("en")
        expect(page.locator("#run-summary")).to_contain_text(
            "Repository details unavailable after refresh"
        )
        page.close()


def test_failed_and_unknown_keep_distinct_actions(visual_browser: Browser) -> None:
    for terminal, expected, intro in (
        ("failed", "检查失败", "检查未完成"),
        ("unknown", "状态未知", "状态不可用"),
    ):
        with live_server(BrowserRunner(terminal=terminal)) as base_url:
            page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(base_url)
            _fill_required(page, "b" * 40)
            page.get_by_role("button", name="开始检查").click()
            expect(page.locator("#status-title")).to_contain_text(
                expected, timeout=6000
            )
            expect(page.locator("h1")).to_have_text(intro)
            expect(page.locator("#retry-button")).to_be_visible()
            expect(page.locator("#new-inspection-button")).to_be_visible()
            if terminal == "unknown":
                expect(page.locator("#report-card")).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            output = Path("/tmp/repotrial-console-refinement-v2")
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f"{terminal}-1440.png"), full_page=True)
            page.close()


def test_focus_and_labels_remain_accessible(visual_browser: Browser) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base_url)
        assert (
            page.locator(
                "label[for=url], label[for=commit_sha], label[for=container_port]"
            ).count()
            == 3
        )
        page.keyboard.press("Tab")
        expect(page.locator(":focus")).to_have_count(1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        output = Path("/tmp/repotrial-console-refinement-v2")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "390.png"), full_page=True)
        page.close()


def test_v4_product_shell_has_real_navigation_and_fact_strip(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        expect(page.locator("#production-sidebar")).to_be_visible()
        expect(page.locator("#fact-strip .fact")).to_have_count(4)
        expect(page.locator("#config-card")).to_be_visible()
        expect(page.locator("#environment-card")).to_be_visible()
        assert page.locator(".review-bar, #review-note, #save-note").count() == 0
        page.close()


def test_v4_shell_uses_localized_nav_repository_context_and_state_columns(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        expect(page.locator("#production-sidebar nav")).to_contain_text("新建检查")
        expect(page.locator("#production-sidebar nav")).not_to_contain_text("nav.new")
        expect(page.locator(".topbar .logo")).to_have_count(0)
        expect(page.locator("#header-repository")).to_be_visible()
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        assert page.locator("#task-card .run-events").count() == 1
        assert page.locator("#task-card .run-primary").count() == 1
        page.close()

    with live_server(BrowserRunner(terminal="failed")) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        assert (
            page.locator("#report-card").bounding_box()["width"]
            > page.locator("#task-card").bounding_box()["width"] * 1.7
        )
        page.close()


def test_state_navigation_and_fact_statuses_follow_real_values(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        nav = page.locator("#production-sidebar nav a")
        expect(nav.nth(0)).to_have_attribute("aria-current", "page")
        _fill_required(page, sha="c" * 40)
        states = page.locator("#fact-strip .fact-state")
        expect(states.nth(0)).to_have_text("已填写")
        expect(states.nth(1)).to_have_text("已填写")
        expect(states.nth(2)).to_have_text("已填写")
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        expect(nav.nth(1)).to_have_attribute("aria-current", "page")
        expect(nav.nth(0)).not_to_have_attribute("aria-current", "page")
        page.close()

    with live_server(BrowserRunner(terminal="failed")) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#production-sidebar nav a").nth(2)).to_have_attribute(
            "aria-current", "page"
        )
        page.close()


def test_run_layout_siblings_and_terminal_height(visual_browser: Browser) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        assert page.locator("#task-card > .run-layout > .run-primary").count() == 1
        assert page.locator("#task-card > .run-layout > #run-events").count() == 1
        left = page.locator("#task-card .run-primary").bounding_box()
        right = page.locator("#task-card .run-events").bounding_box()
        assert left and right and abs(left["y"] - right["y"]) <= 1
        page.close()
    with live_server(BrowserRunner(terminal="failed")) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("检查失败", timeout=6000)
        expect(page.locator("#task-card .run-events")).to_be_visible()
        expect(page.locator("#phase-progress")).to_be_visible()
        expect(page.locator("#phase-progress-unavailable")).to_be_visible()
        assert page.evaluate("document.body.scrollHeight < 1400")
        page.close()


def test_mobile_running_events_stack_below_primary(visual_browser: Browser) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base_url)
        _fill_required(page)
        page.get_by_role("button", name="开始检查").click()
        expect(page.locator("#status-title")).to_have_text("运行中", timeout=6000)
        primary = page.locator("#task-card .run-primary").bounding_box()
        events = page.locator("#task-card .run-events").bounding_box()
        task = page.locator("#task-card").bounding_box()
        assert primary and events and task
        content_width = page.locator("#task-card").evaluate(
            "node => node.clientWidth - parseFloat(getComputedStyle(node).paddingLeft) - parseFloat(getComputedStyle(node).paddingRight)"
        )
        assert abs(primary["width"] - content_width) < 4
        assert abs(events["width"] - content_width) < 4
        assert events["y"] >= primary["y"] + primary["height"] - 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.close()


def test_initial_english_localizes_sidebar_and_empty_facts(
    visual_browser: Browser,
) -> None:
    with live_server(BrowserRunner()) as base_url:
        page = visual_browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url)
        page.evaluate("localStorage.setItem('repotrial-language', 'en')")
        page.reload()
        expect(page.locator(".side-label")).to_have_text("Inspection navigation")
        expect(page.locator(".side-help span")).to_have_text("Help")
        assert "lang=en" in (page.locator("#sidebar-guide").get_attribute("href") or "")
        expect(page.locator("#fact-sha")).to_have_text("Not entered")
        expect(page.locator("#fact-sha")).to_have_attribute("title", "Not entered")
        expect(page.locator("#fact-sha")).to_have_attribute("aria-label", "Not entered")
        page.close()
