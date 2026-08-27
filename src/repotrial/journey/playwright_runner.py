"""A bounded, deterministic Playwright runner for declared browser journeys."""

from pathlib import Path
from typing import Literal, cast
from unicodedata import category
from urllib.parse import urlsplit

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from repotrial.domain.enums import Verdict
from repotrial.domain.models import Journey, JourneyResult, JourneyStep
from repotrial.journey.http_runner import (
    _decoded_path,
    _has_controls_or_backslash,
    _same_origin,
    _validate_base_url,
)

_MAX_STEPS = 64
_MAX_STRING_LENGTH = 4_096
_MAX_PATH_LENGTH = 2_048
_TIMEOUT_MS = 5_000
AllowedRole = Literal[
    "button", "link", "checkbox", "radio", "menuitem", "option", "tab"
]
_ALLOWED_ROLES: frozenset[AllowedRole] = frozenset(
    {"button", "link", "checkbox", "radio", "menuitem", "option", "tab"}
)


def _step_token(index: int) -> str:
    return f"step-{index:04d}"


def _failure_result(
    journey: Journey,
    passed_steps: int,
    evidence_paths: list[str],
    reason: str,
) -> JourneyResult:
    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.FAIL,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
        failure_reason=reason,
    )


def _valid_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_STRING_LENGTH
        and not any(category(character).startswith("C") for character in value)
    )


def _valid_path(path: object) -> bool:
    if (
        not isinstance(path, str)
        or len(path) > _MAX_PATH_LENGTH
        or _has_controls_or_backslash(path)
        or not path.startswith("/")
        or path.startswith("//")
    ):
        return False
    try:
        parsed = urlsplit(path)
    except ValueError:
        return False
    decoded = _decoded_path(parsed.path)
    return not (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or decoded is None
        or _has_controls_or_backslash(decoded)
        or decoded.count("/") != parsed.path.count("/")
        or "//" in decoded
        or any(segment in {".", ".."} for segment in decoded.split("/"))
    )


def _validate_step(step: JourneyStep) -> str | None:
    if step.tool != "browser":
        return "invalid_tool"
    if step.assertions:
        return "invalid_assertions"
    params = step.params
    if step.action == "goto":
        return (
            None
            if set(params) == {"path"} and _valid_path(params["path"])
            else "invalid_params"
        )
    if step.action == "fill_by_label":
        if set(params) != {"label", "value"}:
            return "invalid_params"
        return (
            None
            if _valid_text(params["label"]) and _valid_text(params["value"])
            else "invalid_params"
        )
    if step.action == "click_by_role":
        if set(params) != {"role", "name"}:
            return "invalid_params"
        return (
            None
            if isinstance(params["role"], str)
            and params["role"] in _ALLOWED_ROLES
            and _valid_text(params["name"])
            else "invalid_params"
        )
    if step.action == "assert_text_visible":
        return (
            None
            if set(params) == {"text"} and _valid_text(params["text"])
            else "invalid_params"
        )
    return "invalid_action"


def _request_url(origin: httpx.URL, path: str) -> httpx.URL | None:
    try:
        candidate = origin.join(path)
    except httpx.InvalidURL:
        return None
    return candidate if _same_origin(origin, candidate) else None


def _page_is_on_origin(page: Page, origin: httpx.URL) -> bool:
    try:
        current = httpx.URL(page.url)
    except httpx.InvalidURL:
        return False
    return _same_origin(origin, current)


def _redirect_is_same_origin(
    origin: httpx.URL, source: httpx.URL, location: str | None
) -> bool:
    if not location:
        return False
    try:
        target = source.join(location)
    except httpx.InvalidURL:
        return False
    return _same_origin(origin, target)


def _artifact_targets(evidence_dir: Path, steps: int) -> list[Path]:
    return [
        evidence_dir / "trace.zip",
        *(evidence_dir / f"{_step_token(index)}-failure.png" for index in range(steps)),
    ]


def _write_exclusive(path: Path, content: bytes) -> str:
    with path.open("xb") as artifact:
        artifact.write(content)
    return str(path)


async def _close_context(context: BrowserContext | None) -> None:
    if context is not None:
        try:
            await context.close()
        except PlaywrightError:
            pass


async def _close_browser(browser: Browser | None) -> None:
    if browser is not None:
        try:
            await browser.close()
        except PlaywrightError:
            pass


async def _capture_failure(page: Page, path: Path) -> str | None:
    try:
        image = await page.screenshot(full_page=True)
        return _write_exclusive(path, image)
    except (OSError, PlaywrightError):
        return None


async def run_playwright_journey(
    journey: Journey,
    *,
    base_url: str,
    evidence_dir: Path,
) -> JourneyResult:
    """Replay one frozen browser journey against exactly one trusted origin."""
    origin = _validate_base_url(base_url)
    if origin is None:
        return _failure_result(journey, 0, [], "journey:invalid_base_url")
    if len(journey.steps) > _MAX_STEPS:
        return _failure_result(journey, 0, [], "journey:too_many_steps")
    for index, step in enumerate(journey.steps):
        validation_error = _validate_step(step)
        if validation_error is not None:
            return _failure_result(
                journey, 0, [], f"{_step_token(index)}:{validation_error}"
            )
        if step.action == "goto":
            path = step.params["path"]
            assert isinstance(path, str)
            if _request_url(origin, path) is None:
                return _failure_result(
                    journey, 0, [], f"{_step_token(index)}:invalid_params"
                )

    targets = _artifact_targets(evidence_dir, len(journey.steps))
    collision = next(
        (target for target in targets if target.exists() or target.is_symlink()), None
    )
    if collision is not None:
        reason = (
            "journey:trace_failure"
            if collision.name == "trace.zip"
            else "journey:evidence_failure"
        )
        return _failure_result(journey, 0, [], reason)
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _failure_result(journey, 0, [], "journey:evidence_failure")

    browser: Browser | None = None
    context: BrowserContext | None = None
    trace_started = False
    trace_error = False
    evidence_paths: list[str] = []
    runtime_reason: str | None = None
    failed_index: int | None = None
    screenshot_path: str | None = None
    passed_steps = 0
    blocked_cross_origin = False

    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(service_workers="block")
                context.set_default_timeout(_TIMEOUT_MS)
                context.set_default_navigation_timeout(_TIMEOUT_MS)

                async def route_request(route: Route) -> None:
                    nonlocal blocked_cross_origin
                    request_url = route.request.url
                    try:
                        candidate = httpx.URL(request_url)
                    except httpx.InvalidURL:
                        await route.abort()
                        return
                    if candidate.scheme in {"http", "https"} and not _same_origin(
                        origin, candidate
                    ):
                        blocked_cross_origin = True
                        await route.abort()
                        return
                    if candidate.scheme not in {"http", "https"}:
                        await route.continue_()
                        return
                    try:
                        response = await route.fetch(max_redirects=0, max_retries=0)
                    except PlaywrightError:
                        await route.abort()
                        return
                    if 300 <= response.status < 400:
                        if not _redirect_is_same_origin(
                            origin, candidate, response.headers.get("location")
                        ):
                            blocked_cross_origin = True
                        await route.abort()
                        return
                    await route.fulfill(response=response)

                await context.route("**/*", route_request)
                await context.tracing.start(
                    screenshots=True,
                    snapshots=True,
                    sources=False,
                )
                trace_started = True
                page = await context.new_page()

                for index, step in enumerate(journey.steps):
                    try:
                        if step.action == "goto":
                            path = step.params["path"]
                            assert isinstance(path, str)
                            target = _request_url(origin, path)
                            assert target is not None
                            await page.goto(str(target))
                        elif step.action == "fill_by_label":
                            label = step.params["label"]
                            value = step.params["value"]
                            assert isinstance(label, str) and isinstance(value, str)
                            await page.get_by_label(label, exact=True).fill(value)
                        elif step.action == "click_by_role":
                            role = step.params["role"]
                            name = step.params["name"]
                            assert isinstance(role, str) and isinstance(name, str)
                            await page.get_by_role(
                                cast(AllowedRole, role), name=name, exact=True
                            ).click()
                            await page.wait_for_load_state("networkidle")
                        else:
                            text = step.params["text"]
                            assert isinstance(text, str)
                            await page.get_by_text(text, exact=True).wait_for(
                                state="visible"
                            )
                        if step.action in {
                            "goto",
                            "click_by_role",
                        } and not _page_is_on_origin(page, origin):
                            runtime_reason = (
                                "cross_origin_request"
                                if blocked_cross_origin
                                else "cross_origin_navigation"
                            )
                            failed_index = index
                            screenshot_path = await _capture_failure(
                                page, evidence_dir / f"{_step_token(index)}-failure.png"
                            )
                            if screenshot_path is None:
                                runtime_reason = "evidence_failure"
                            break
                    except (PlaywrightError, PlaywrightTimeoutError, httpx.InvalidURL):
                        runtime_reason = (
                            "cross_origin_request"
                            if blocked_cross_origin
                            else (
                                "navigation_failure"
                                if step.action == "goto"
                                else "assertion_failure"
                                if step.action == "assert_text_visible"
                                else "action_failure"
                            )
                        )
                        failed_index = index
                        screenshot_path = await _capture_failure(
                            page, evidence_dir / f"{_step_token(index)}-failure.png"
                        )
                        if screenshot_path is None:
                            runtime_reason = "evidence_failure"
                        break
                    passed_steps += 1
            except PlaywrightError:
                runtime_reason = (
                    "browser_unavailable" if browser is None else "action_failure"
                )
            finally:
                if context is not None and trace_started:
                    try:
                        await context.tracing.stop(path=str(evidence_dir / "trace.zip"))
                        evidence_paths.append(str(evidence_dir / "trace.zip"))
                    except (OSError, PlaywrightError):
                        trace_error = True
                await _close_context(context)
                await _close_browser(browser)
    except PlaywrightError:
        runtime_reason = "browser_unavailable"

    if screenshot_path is not None:
        evidence_paths.append(screenshot_path)
    if trace_error:
        return _failure_result(
            journey, passed_steps, evidence_paths, "journey:trace_failure"
        )
    if runtime_reason is None:
        return JourneyResult(
            journey_id=journey.journey_id,
            verdict=Verdict.PASS,
            passed_steps=passed_steps,
            total_steps=len(journey.steps),
            evidence_paths=evidence_paths,
        )
    return _failure_result(
        journey,
        passed_steps,
        evidence_paths,
        (
            f"{_step_token(failed_index)}:{runtime_reason}"
            if failed_index is not None
            else f"journey:{runtime_reason}"
        ),
    )
