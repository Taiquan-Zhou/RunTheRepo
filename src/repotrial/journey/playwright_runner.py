"""A bounded fixture-only Playwright runner for declared browser journeys."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Literal, cast
from unicodedata import category
from urllib.parse import urlsplit

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    Route,
    ViewportSize,
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
_CLEANUP_TIMEOUT_SECONDS = 2
_MAX_TRACE_BYTES = 8 * 1024 * 1024
_MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_ARTIFACT_BYTES = _MAX_TRACE_BYTES + _MAX_SCREENSHOT_BYTES
_VIEWPORT: ViewportSize = {"width": 1280, "height": 720}
CleanupOutcome = Literal["success", "cancelled", "failure"]
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
    evidence_failure_reason: str | None = None,
) -> JourneyResult:
    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.FAIL,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
        failure_reason=reason,
        evidence_failure_reason=evidence_failure_reason,
    )


def _unsupported_result(journey: Journey) -> JourneyResult:
    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.UNSUPPORTED,
        passed_steps=0,
        total_steps=len(journey.steps),
        failure_reason="isolated browser execution unavailable",
    )


def _valid_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_STRING_LENGTH
        and not any(category(character).startswith("C") for character in value)
    )


def _valid_target(value: object) -> bool:
    return _valid_text(value) and isinstance(value, str) and value.isascii()


def _valid_path(path: object) -> bool:
    if (
        not isinstance(path, str)
        or len(path) > _MAX_PATH_LENGTH
        or not path.isascii()
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
            if _valid_target(params["label"]) and _valid_text(params["value"])
            else "invalid_params"
        )
    if step.action == "click_by_role":
        if set(params) != {"role", "name"}:
            return "invalid_params"
        return (
            None
            if isinstance(params["role"], str)
            and params["role"] in _ALLOWED_ROLES
            and _valid_target(params["name"])
            else "invalid_params"
        )
    if step.action == "assert_text_visible":
        return (
            None
            if set(params) == {"text"} and _valid_target(params["text"])
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


async def _close_context(context: BrowserContext | None) -> CleanupOutcome:
    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return "cancelled"
        except (TimeoutError, PlaywrightError):
            return "failure"
    return "success"


async def _close_browser(browser: Browser | None) -> CleanupOutcome:
    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return "cancelled"
        except (TimeoutError, PlaywrightError):
            return "failure"
    return "success"


def _publish_staged_artifact(
    staged_path: Path,
    target: Path,
    *,
    maximum_size: int,
    total_size: int,
    maximum_total_size: int,
) -> tuple[str | None, int]:
    try:
        size = staged_path.stat().st_size
        if size > maximum_size or total_size + size > maximum_total_size:
            return None, 0
        return _write_exclusive(target, staged_path.read_bytes()), size
    except OSError:
        return None, 0


async def _capture_failure(page: Page, path: Path) -> str | None:
    try:
        image = await page.screenshot(full_page=False)
        if len(image) > _MAX_SCREENSHOT_BYTES:
            return None
        return _write_exclusive(path, image)
    except (OSError, PlaywrightError):
        return None


async def _abort_route_safely(route: Route) -> None:
    try:
        await route.abort()
    except PlaywrightError:
        return


async def run_playwright_journey(
    journey: Journey,
    *,
    base_url: str,
    evidence_dir: Path,
) -> JourneyResult:
    """Refuse browser execution until a sandbox-backed runner is available."""
    del base_url, evidence_dir
    return _unsupported_result(journey)


async def _run_trusted_fixture_playwright_journey(
    journey: Journey,
    *,
    base_url: str,
    evidence_dir: Path,
) -> JourneyResult:
    """Replay a Journey only when trusted test control flow selects this helper."""
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
        evidence_reason = (
            "journey:trace_failure"
            if collision.name == "trace.zip"
            else "journey:screenshot_failure"
        )
        return _failure_result(
            journey,
            0,
            [],
            "journey:evidence_failure",
            evidence_reason,
        )
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=".playwright-", dir=evidence_dir))
    except OSError:
        return _failure_result(
            journey, 0, [], "journey:evidence_failure", "journey:staging_failure"
        )

    browser: Browser | None = None
    context: BrowserContext | None = None
    trace_started = False
    evidence_paths: list[str] = []
    evidence_size = 0
    evidence_failure_reason: str | None = None
    runtime_reason: str | None = None
    failed_index: int | None = None
    passed_steps = 0
    blocked_cross_origin = False
    cancelled = False
    page: Page | None = None

    def record_evidence_failure(reason: str) -> None:
        nonlocal evidence_failure_reason
        if evidence_failure_reason is None:
            evidence_failure_reason = reason

    async def capture_and_publish_failure(page: Page, index: int) -> None:
        nonlocal evidence_size
        staged_path = await _capture_failure(
            page, staging_dir / f"{_step_token(index)}-failure.png"
        )
        if staged_path is None:
            record_evidence_failure("journey:screenshot_failure")
            return
        published, size = _publish_staged_artifact(
            Path(staged_path),
            evidence_dir / f"{_step_token(index)}-failure.png",
            maximum_size=_MAX_SCREENSHOT_BYTES,
            total_size=evidence_size,
            maximum_total_size=_MAX_TOTAL_ARTIFACT_BYTES,
        )
        if published is None:
            record_evidence_failure("journey:screenshot_failure")
            return
        evidence_size += size
        evidence_paths.append(published)

    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(
                    service_workers="block", viewport=_VIEWPORT
                )
                context.set_default_timeout(_TIMEOUT_MS)
                context.set_default_navigation_timeout(_TIMEOUT_MS)

                async def route_request(route: Route) -> None:
                    nonlocal blocked_cross_origin
                    try:
                        candidate = httpx.URL(route.request.url)
                    except httpx.InvalidURL:
                        await _abort_route_safely(route)
                        return
                    if candidate.scheme in {"http", "https"} and not _same_origin(
                        origin, candidate
                    ):
                        blocked_cross_origin = True
                        await _abort_route_safely(route)
                        return
                    if candidate.scheme not in {"http", "https"}:
                        await route.continue_()
                        return
                    try:
                        response = await route.fetch(max_redirects=0, max_retries=0)
                    except PlaywrightError:
                        await _abort_route_safely(route)
                        return
                    if 300 <= response.status < 400:
                        if not _redirect_is_same_origin(
                            origin, candidate, response.headers.get("location")
                        ):
                            blocked_cross_origin = True
                        await _abort_route_safely(route)
                        return
                    try:
                        await route.fulfill(response=response)
                    except PlaywrightError:
                        return

                await context.route("**/*", route_request)
                await context.tracing.start(
                    screenshots=True, snapshots=True, sources=False
                )
                trace_started = True
                page = await context.new_page()
                unexpected_popup = False
                unexpected_download = False

                def note_request(request: Request) -> None:
                    nonlocal unexpected_popup
                    if not request.is_navigation_request():
                        return
                    try:
                        is_primary_page = request.frame.page == page
                    except PlaywrightError:
                        is_primary_page = False
                    if not is_primary_page:
                        unexpected_popup = True

                def note_popup(_popup: Page) -> None:
                    nonlocal unexpected_popup
                    unexpected_popup = True

                def note_download(_download: object) -> None:
                    nonlocal unexpected_download
                    unexpected_download = True

                page.on("popup", note_popup)
                page.on("download", note_download)
                context.on("request", note_request)

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
                        else:
                            text = step.params["text"]
                            assert isinstance(text, str)
                            await page.get_by_text(text, exact=True).wait_for(
                                state="visible"
                            )
                        if (
                            unexpected_popup
                            or len(context.pages) > 1
                            or unexpected_download
                        ):
                            runtime_reason = (
                                "unexpected_popup"
                                if unexpected_popup or len(context.pages) > 1
                                else "unexpected_download"
                            )
                        elif step.action in {
                            "goto",
                            "click_by_role",
                        } and not _page_is_on_origin(page, origin):
                            runtime_reason = (
                                "cross_origin_request"
                                if blocked_cross_origin
                                else "cross_origin_navigation"
                            )
                        if runtime_reason is not None:
                            failed_index = index
                            await capture_and_publish_failure(page, index)
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
                        await capture_and_publish_failure(page, index)
                        break
                    passed_steps += 1
            except PlaywrightError:
                runtime_reason = (
                    "browser_unavailable" if browser is None else "action_failure"
                )
            finally:
                if context is not None and trace_started:
                    try:
                        await asyncio.wait_for(
                            context.tracing.stop(path=str(staging_dir / "trace.zip")),
                            timeout=_CLEANUP_TIMEOUT_SECONDS,
                        )
                        published, size = _publish_staged_artifact(
                            staging_dir / "trace.zip",
                            evidence_dir / "trace.zip",
                            maximum_size=_MAX_TRACE_BYTES,
                            total_size=evidence_size,
                            maximum_total_size=_MAX_TOTAL_ARTIFACT_BYTES,
                        )
                        if published is None:
                            record_evidence_failure("journey:trace_failure")
                        else:
                            evidence_size += size
                            evidence_paths.insert(0, published)
                    except asyncio.CancelledError:
                        cancelled = True
                    except (TimeoutError, OSError, PlaywrightError):
                        record_evidence_failure("journey:trace_failure")
                context_cleanup = await _close_context(context)
                if context_cleanup == "cancelled":
                    cancelled = True
                elif context_cleanup == "failure":
                    record_evidence_failure("journey:context_close_failure")
                browser_cleanup = await _close_browser(browser)
                if browser_cleanup == "cancelled":
                    cancelled = True
                elif browser_cleanup == "failure":
                    record_evidence_failure("journey:browser_close_failure")
    except PlaywrightError:
        runtime_reason = "browser_unavailable"
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    if cancelled:
        raise asyncio.CancelledError
    if runtime_reason is not None:
        reason = (
            f"{_step_token(failed_index)}:{runtime_reason}"
            if failed_index is not None
            else f"journey:{runtime_reason}"
        )
        return _failure_result(
            journey, passed_steps, evidence_paths, reason, evidence_failure_reason
        )
    if evidence_failure_reason is not None:
        return _failure_result(
            journey,
            passed_steps,
            evidence_paths,
            "journey:evidence_failure",
            evidence_failure_reason,
        )
    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.PASS,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
    )
