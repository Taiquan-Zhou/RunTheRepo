"""A bounded, deterministic HTTP runner for declared journeys."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from repotrial.domain.enums import Verdict
from repotrial.domain.models import (
    Journey,
    JourneyAssertion,
    JourneyResult,
    JourneyStep,
)
from repotrial.journey.verifier import evaluate_assertion, validate_assertion

_BODY_LIMIT_BYTES = 65_536
_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_ASSERTIONS_PER_STEP = 64
_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)


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


def _step_token(index: int) -> str:
    return f"step-{index:04d}"


def _has_controls_or_backslash(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value) or (
        "\\" in value or "%5c" in value.lower()
    )


def _credential_key(value: str) -> bool:
    normalized = unquote(value).lower().replace("-", "_")
    parts = [part for part in normalized.split("_") if part]
    if not parts:
        return False
    if parts[-1] in {"password", "token", "secret", "apikey"}:
        return True
    return len(parts) >= 2 and tuple(parts[-2:]) in {
        ("api", "key"),
        ("private", "key"),
    }


def _decoded_path(value: str) -> str | None:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return None
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    return None


def _effective_port(url: httpx.URL) -> int:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else 80


def _validate_base_url(base_url: str) -> httpx.URL | None:
    if _has_controls_or_backslash(base_url):
        return None
    try:
        parsed = urlsplit(base_url)
        explicit_port = parsed.port
        origin = httpx.URL(base_url)
    except (ValueError, httpx.InvalidURL):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or origin.username
        or origin.password
        or origin.scheme != parsed.scheme
        or origin.host != parsed.hostname
        or (explicit_port is not None and _effective_port(origin) != explicit_port)
    ):
        return None
    return origin.copy_with(path="/", query=None, fragment=None)


def _same_origin(left: httpx.URL, right: httpx.URL) -> bool:
    return (
        left.scheme == right.scheme
        and left.host == right.host
        and _effective_port(left) == _effective_port(right)
    )


def _request_url(origin: httpx.URL, path: str) -> httpx.URL | None:
    try:
        request_url = origin.join(path)
    except httpx.InvalidURL:
        return None
    if not _same_origin(origin, request_url):
        return None
    return request_url


def _validate_request_target(target: str) -> bool:
    if _has_controls_or_backslash(target):
        return False
    try:
        parsed_target = urlsplit(target)
    except ValueError:
        return False
    decoded_path = _decoded_path(parsed_target.path)
    return not (
        decoded_path is None
        or not target.startswith("/")
        or target.startswith("//")
        or parsed_target.scheme
        or parsed_target.netloc
        or parsed_target.fragment
        or _has_controls_or_backslash(decoded_path)
        or decoded_path.count("/") != parsed_target.path.count("/")
        or "//" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or len(target) > 2_048
    )


def _validate_redirect_reference_path(path: str) -> bool:
    decoded_path = _decoded_path(path)
    return not (
        decoded_path is None
        or _has_controls_or_backslash(decoded_path)
        or decoded_path.count("/") != path.count("/")
        or "//" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    )


def _redirect_target(
    response: httpx.Response,
    *,
    current_url: httpx.URL,
    origin: httpx.URL,
) -> tuple[httpx.URL | None, str | None]:
    locations = response.headers.get_list("location")
    if not locations:
        return None, "missing_location"
    if len(locations) != 1:
        return None, "duplicate_location"

    location = locations[0]
    if not location or location != location.strip():
        return None, "malformed_location"
    if _has_controls_or_backslash(location):
        return None, "invalid_location"
    if location.startswith("//"):
        return None, "network_path"

    try:
        parsed_location = urlsplit(location)
    except ValueError:
        return None, "malformed_location"
    if "#" in location or parsed_location.fragment:
        return None, "fragment"
    if parsed_location.username is not None or parsed_location.password is not None:
        return None, "credential_location"
    if parsed_location.scheme and not parsed_location.netloc:
        return None, "malformed_location"
    if parsed_location.scheme not in {"", "http", "https"}:
        return None, "origin_change"
    if not _validate_redirect_reference_path(parsed_location.path):
        return None, "invalid_target"

    try:
        target = current_url.join(location)
    except (ValueError, httpx.InvalidURL):
        return None, "malformed_location"
    if target.fragment:
        return None, "fragment"
    if target.username or target.password:
        return None, "credential_location"
    try:
        same_origin = _same_origin(origin, target)
    except (ValueError, httpx.InvalidURL):
        return None, "malformed_location"
    if not same_origin:
        return None, "origin_change"
    try:
        request_target = target.raw_path.decode("ascii")
    except UnicodeDecodeError:
        return None, "malformed_location"
    if not _validate_request_target(request_target):
        return None, "invalid_target"
    return target, None


def _redirect_target_identity(url: httpx.URL) -> tuple[str, str, int, bytes]:
    return (url.scheme, url.host, _effective_port(url), url.raw_path)


def _redirect_target_hash(url: httpx.URL) -> str:
    return hashlib.sha256(_safe_request_target(url).encode("utf-8")).hexdigest()


def _expects_redirect_status(
    assertions: list[JourneyAssertion], status_code: int
) -> bool:
    return any(
        getattr(assertion, "kind", None) == "status_code"
        and getattr(assertion, "target", None) == "response.status"
        and getattr(assertion, "expected", None) == status_code
        for assertion in assertions
    )


def _validate_step(
    step: JourneyStep,
) -> tuple[str | None, str | None, str | None, object | None, bool]:
    if step.tool != "http":
        return "invalid_tool", None, None, None, False
    if step.action != "request":
        return "invalid_action", None, None, None, False
    if set(step.params) - {"method", "path", "json"} or not {"method", "path"} <= set(
        step.params
    ):
        return "invalid_params", None, None, None, False
    if len(step.assertions) > _MAX_ASSERTIONS_PER_STEP:
        return "invalid_assertions", None, None, None, False
    for item in step.assertions:
        assertion_error = validate_assertion(item)
        if assertion_error is not None:
            return assertion_error, None, None, None, False

    method = step.params["method"]
    path = step.params["path"]
    if not isinstance(method, str) or method not in _ALLOWED_METHODS:
        return "invalid_method", None, None, None, False
    if not isinstance(path, str):
        return "invalid_path", None, None, None, False
    if _has_controls_or_backslash(path):
        return "invalid_path", None, None, None, False
    if not _validate_request_target(path):
        return "invalid_path", None, None, None, False

    has_json_body = "json" in step.params
    json_body = step.params.get("json")
    if has_json_body:
        try:
            json.dumps(
                json_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        except (TypeError, ValueError):
            return "invalid_params", None, None, None, False
    return None, method, path, json_body, has_json_body


def _canonical_json_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted_body_hash(body: bytes, truncated: bool) -> str:
    text = body.decode("utf-8", errors="replace")
    redacted = _redact_json_body(text)
    if redacted is None:
        redacted = _redact_credential_assignments(text)
    redacted = _PEM_PRIVATE_KEY.sub("<redacted-private-key>", redacted)
    redacted = _BEARER_TOKEN.sub("Bearer <redacted>", redacted)
    if truncated:
        redacted += "\n[repotrial:truncated]\n"
    return hashlib.sha256(redacted.encode("utf-8")).hexdigest()


def _redact_json_body(text: str) -> str | None:
    try:
        value: object = json.loads(text)
        redacted = _redact_json_value(value)
        return json.dumps(
            redacted,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except json.JSONDecodeError:
        return None
    except (ValueError, RecursionError):
        return None


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for key, nested_value in value.items():
            redacted[key] = (
                "<redacted>"
                if isinstance(key, str) and _credential_key(key)
                else _redact_json_value(nested_value)
            )
        return redacted
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _line_content_end(line: str) -> int:
    end = len(line)
    while end and line[end - 1] in "\r\n":
        end -= 1
    return end


def _quoted_end(line: str, start: int, end: int) -> tuple[int, bool]:
    quote = line[start]
    index = start + 1
    while index < end:
        if line[index] == "\\":
            index += 2
        elif line[index] == quote:
            return index + 1, True
        else:
            index += 1
    return end, False


def _assignment_start(line: str, start: int, end: int) -> tuple[str, int, int] | None:
    if start and not (line[start - 1].isspace() or line[start - 1] in "{,["):
        return None

    index = start
    if line[index : index + 1] == "-" and index + 1 < end and line[index + 1].isspace():
        index += 1
        while index < end and line[index].isspace():
            index += 1
    key_start = index
    if index >= end:
        return None
    if line[index] in "\"'":
        key_end, closed = _quoted_end(line, index, end)
        if not closed:
            return None
        key = line[index + 1 : key_end - 1]
        index = key_end
    else:
        while index < end and (line[index].isalnum() or line[index] in "_-"):
            index += 1
        key = line[key_start:index]
    if not key:
        return None
    while index < end and line[index] in " \t":
        index += 1
    if index >= end or line[index] not in ":=":
        return None
    index += 1
    while index < end and line[index] in " \t":
        index += 1
    return key, key_start, index


def _assignment_value_end(line: str, start: int, end: int) -> int:
    if start >= end:
        return end
    if line[start] in "\"'":
        quoted_end, _ = _quoted_end(line, start, end)
        return quoted_end

    index = start
    while index < end:
        if index > start and _assignment_start(line, index, end) is not None:
            boundary = index
            while boundary > start and line[boundary - 1] in " \t":
                boundary -= 1
            if boundary > start and line[boundary - 1] == ",":
                boundary -= 1
            return boundary
        if line[index] in "\"'":
            index, _ = _quoted_end(line, index, end)
        else:
            index += 1
    return end


def _redact_credential_assignments(text: str) -> str:
    redacted_lines: list[str] = []
    block_key_column: int | None = None
    for line in text.splitlines(keepends=True):
        if block_key_column is not None:
            indentation = len(line) - len(line.lstrip(" \t"))
            if not line.strip() or indentation > block_key_column:
                continue
            block_key_column = None

        end = _line_content_end(line)
        cursor = 0
        redacted_line: list[str] = []
        index = 0
        while index < end:
            assignment = _assignment_start(line, index, end)
            if assignment is None:
                if line[index] in "\"'":
                    index, _ = _quoted_end(line, index, end)
                else:
                    index += 1
                continue

            key, key_column, value_start = assignment
            if _credential_key(key):
                value_end = _assignment_value_end(line, value_start, end)
                redacted_line.append(line[cursor:value_start])
                redacted_line.append("<redacted>")
                if not line[value_start:value_end].strip() or line[
                    value_start:value_end
                ].lstrip().startswith(("|", ">")):
                    block_key_column = key_column
                cursor = value_end
                index = value_end
            else:
                index = value_start
        redacted_line.append(line[cursor:])
        redacted_lines.append("".join(redacted_line))
    return "".join(redacted_lines)


def _safe_request_target(url: httpx.URL) -> str:
    target = url.raw_path.decode("ascii")
    path, marker, query = target.partition("?")
    if not marker:
        return target
    safe_parts: list[str] = []
    for part in query.split("&"):
        key, equals, value = part.partition("=")
        safe_parts.append(
            key + equals + ("<redacted>" if equals and _credential_key(key) else value)
        )
    return path + marker + "&".join(safe_parts)


async def _read_bounded_body(response: httpx.Response) -> tuple[bytes, bool]:
    body = bytearray()
    truncated = False
    async for chunk in response.aiter_raw():
        remaining = _BODY_LIMIT_BYTES - len(body)
        if remaining == 0:
            truncated = True
            break
        if len(chunk) > remaining:
            body.extend(chunk[:remaining])
            truncated = True
            break
        body.extend(chunk)
    return bytes(body), truncated


def _write_evidence(path: Path, evidence: dict[str, object]) -> str:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(
            json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        )
    return str(path)


def _evidence(
    *,
    method: str | None,
    path: str | None,
    json_body: object | None,
    has_json_body: bool,
    status_code: int | None,
    truncated: bool | None,
    body_hash: str | None,
    assertion_outcomes: list[dict[str, object]],
    failure_category: str | None,
    redirects: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "assertions": assertion_outcomes,
        "failure_category": failure_category,
        "redirects": list(redirects or []),
        "request": {
            "body_sha256": _canonical_json_hash(json_body) if has_json_body else None,
            "method": method,
            "path": path,
        },
        "response": {
            "body_sha256": body_hash,
            "status_code": status_code,
            "truncated": truncated,
        },
    }


async def run_http_journey(
    journey: Journey,
    *,
    base_url: str,
    evidence_dir: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> JourneyResult:
    """Replay one declared HTTP journey against one trusted origin."""
    origin = _validate_base_url(base_url)
    if origin is None:
        return _failure_result(journey, 0, [], "journey:invalid_base_url")
    if not journey.steps:
        return _failure_result(journey, 0, [], "journey:empty_steps")
    if not any(step.assertions for step in journey.steps):
        return _failure_result(journey, 0, [], "journey:missing_assertions")

    for index, step in enumerate(journey.steps):
        validation_error, _, preflight_path, _, _ = _validate_step(step)
        if validation_error is not None:
            return _failure_result(
                journey, 0, [], f"{_step_token(index)}:{validation_error}"
            )
        assert preflight_path is not None
        if _request_url(origin, preflight_path) is None:
            return _failure_result(journey, 0, [], f"{_step_token(index)}:invalid_path")

    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _failure_result(
            journey,
            0,
            [],
            "journey:evidence_failure",
            "journey:evidence_initialization_failure",
        )
    evidence_paths: list[str] = []
    passed_steps = 0
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS),
        follow_redirects=False,
        headers={"Accept-Encoding": "identity"},
        trust_env=False,
    ) as client:
        for index, step in enumerate(journey.steps):
            evidence_path = evidence_dir / f"step-{index:04d}.json"
            validation_error, method, path, json_body, has_json_body = _validate_step(
                step
            )
            assert validation_error is None

            assert method is not None
            assert path is not None
            initial_url = _request_url(origin, path)
            assert initial_url is not None
            current_url = initial_url
            visited_urls = {_redirect_target_identity(initial_url)}
            redirects: list[dict[str, object]] = []
            response_status: int | None = None
            body: bytes | None = None
            truncated: bool | None = None
            failure_category: str | None = None
            request_body = json_body
            try:
                async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
                    while True:
                        response_status = None
                        next_url: httpx.URL | None = None
                        try:
                            async with client.stream(
                                method, current_url, json=request_body
                            ) as response:
                                response_status = response.status_code
                                should_follow = (
                                    method == "GET"
                                    and response.status_code in _REDIRECT_STATUSES
                                    and not _expects_redirect_status(
                                        step.assertions, response.status_code
                                    )
                                )
                                if should_follow:
                                    if len(redirects) >= _MAX_REDIRECTS:
                                        failure_category = "redirect:max_hops"
                                    else:
                                        next_url, redirect_error = _redirect_target(
                                            response,
                                            current_url=current_url,
                                            origin=origin,
                                        )
                                        redirects.append(
                                            {
                                                "status_code": response.status_code,
                                                "target_sha256": (
                                                    _redirect_target_hash(next_url)
                                                    if next_url is not None
                                                    else None
                                                ),
                                            }
                                        )
                                        if redirect_error is not None:
                                            failure_category = (
                                                f"redirect:{redirect_error}"
                                            )
                                        elif next_url is None:
                                            failure_category = "redirect:invalid_target"
                                        elif (
                                            _redirect_target_identity(next_url)
                                            in visited_urls
                                        ):
                                            failure_category = "redirect:cycle"
                                if failure_category is None and next_url is None:
                                    content_encoding = response.headers.get(
                                        "content-encoding", "identity"
                                    )
                                    if content_encoding.strip().lower() not in {
                                        "",
                                        "identity",
                                    }:
                                        failure_category = (
                                            "unsupported_content_encoding"
                                        )
                                    else:
                                        body, truncated = await _read_bounded_body(
                                            response
                                        )
                        except httpx.HTTPError:
                            failure_category = "network_error"
                        finally:
                            client.cookies.clear()

                        if failure_category is not None:
                            break
                        if next_url is None:
                            break
                        visited_urls.add(_redirect_target_identity(next_url))
                        current_url = next_url
                        request_body = None
            except TimeoutError:
                response_status = None
                body = None
                truncated = None
                failure_category = "network_error"

            if body is None:
                step_failure_category = failure_category or "network_error"
                try:
                    evidence_paths.append(
                        _write_evidence(
                            evidence_path,
                            _evidence(
                                method=method,
                                path=_safe_request_target(initial_url),
                                json_body=json_body,
                                has_json_body=has_json_body,
                                status_code=response_status,
                                truncated=(
                                    False
                                    if step_failure_category
                                    == "unsupported_content_encoding"
                                    else None
                                ),
                                body_hash=None,
                                assertion_outcomes=[],
                                failure_category=step_failure_category,
                                redirects=redirects,
                            ),
                        )
                    )
                except OSError:
                    return _failure_result(
                        journey,
                        passed_steps,
                        evidence_paths,
                        f"{_step_token(index)}:evidence_write_error",
                    )
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{_step_token(index)}:{step_failure_category}",
                )

            assert response_status is not None
            assert truncated is not None
            text = body.decode("utf-8", errors="replace")
            outcomes: list[dict[str, object]] = []
            assertion_failure_category: str | None = None
            for item in step.assertions:
                passed, category = evaluate_assertion(
                    item, status_code=response_status, text=text
                )
                outcomes.append(
                    {
                        "category": category,
                        "outcome": "passed" if passed else "failed",
                    }
                )
                if not passed:
                    assertion_failure_category = category
                    break

            try:
                evidence_paths.append(
                    _write_evidence(
                        evidence_path,
                        _evidence(
                            method=method,
                            path=_safe_request_target(initial_url),
                            json_body=json_body,
                            has_json_body=has_json_body,
                            status_code=response_status,
                            truncated=truncated,
                            body_hash=_redacted_body_hash(body, truncated),
                            assertion_outcomes=outcomes,
                            failure_category=assertion_failure_category,
                            redirects=redirects,
                        ),
                    )
                )
            except OSError:
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{_step_token(index)}:evidence_write_error",
                )
            if assertion_failure_category is not None:
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{_step_token(index)}:{assertion_failure_category}",
                )
            passed_steps += 1

    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.PASS,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
    )
