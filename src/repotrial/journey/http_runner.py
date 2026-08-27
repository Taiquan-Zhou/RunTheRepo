"""A bounded, deterministic HTTP runner for declared journeys."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from repotrial.domain.enums import Verdict
from repotrial.domain.models import Journey, JourneyResult, JourneyStep
from repotrial.journey.verifier import evaluate_assertion, validate_assertion

_BODY_LIMIT_BYTES = 65_536
_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_ASSERTIONS_PER_STEP = 64
_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_ASSIGNMENT = re.compile(
    r"(?<![^\s{,\[])(?:-\s*)?[\"']?(?P<key>[A-Za-z0-9_-]+)[\"']?\s*"
    r"(?P<separator>[:=])\s*(?P<value>.*?)"
    r"(?=(?:\s+|,\s*|\{\s*|\[\s*(?:\{\s*)?)[\"']?[A-Za-z0-9_-]+[\"']?\s*[:=]|(?:\r?\n)?\Z)"
)


def _failure_result(
    journey: Journey, passed_steps: int, evidence_paths: list[str], reason: str
) -> JourneyResult:
    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.FAIL,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
        failure_reason=reason,
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
    try:
        parsed_path = urlsplit(path)
    except ValueError:
        return "invalid_path", None, None, None, False
    decoded_path = _decoded_path(parsed_path.path)
    if (
        decoded_path is None
        or not path.startswith("/")
        or path.startswith("//")
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.fragment
        or _has_controls_or_backslash(decoded_path)
        or decoded_path.count("/") != parsed_path.path.count("/")
        or "//" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or len(path) > 2_048
    ):
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


def _redact_credential_assignments(text: str) -> str:
    redacted_lines: list[str] = []
    block_key_column: int | None = None
    for line in text.splitlines(keepends=True):
        if block_key_column is not None:
            indentation = len(line) - len(line.lstrip(" \t"))
            if not line.strip() or indentation > block_key_column:
                continue
            block_key_column = None

        matches = list(_ASSIGNMENT.finditer(line))
        if not matches:
            redacted_lines.append(line)
            continue

        cursor = 0
        redacted_line: list[str] = []
        for match in matches:
            redacted_line.append(line[cursor : match.start("value")])
            value = match.group("value")
            if _credential_key(match.group("key")):
                redacted_line.append("<redacted>")
                if not value.strip() or value.lstrip().startswith(("|", ">")):
                    block_key_column = match.start("key")
            else:
                redacted_line.append(value)
            cursor = match.end("value")
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
) -> dict[str, object]:
    return {
        "assertions": assertion_outcomes,
        "failure_category": failure_category,
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

    for index, step in enumerate(journey.steps):
        validation_error, _, preflight_path, _, _ = _validate_step(step)
        if validation_error is not None:
            return _failure_result(
                journey, 0, [], f"{_step_token(index)}:{validation_error}"
            )
        assert preflight_path is not None
        if _request_url(origin, preflight_path) is None:
            return _failure_result(journey, 0, [], f"{_step_token(index)}:invalid_path")

    evidence_dir.mkdir(parents=True, exist_ok=True)
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
            request_url = _request_url(origin, path)
            assert request_url is not None
            try:
                async with client.stream(
                    method, request_url, json=json_body
                ) as response:
                    content_encoding = response.headers.get(
                        "content-encoding", "identity"
                    )
                    if content_encoding.strip().lower() not in {"", "identity"}:
                        try:
                            evidence_paths.append(
                                _write_evidence(
                                    evidence_path,
                                    _evidence(
                                        method=method,
                                        path=_safe_request_target(request_url),
                                        json_body=json_body,
                                        has_json_body=has_json_body,
                                        status_code=response.status_code,
                                        truncated=False,
                                        body_hash=None,
                                        assertion_outcomes=[],
                                        failure_category="unsupported_content_encoding",
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
                            f"{_step_token(index)}:unsupported_content_encoding",
                        )
                    body, truncated = await _read_bounded_body(response)
            except httpx.HTTPError:
                try:
                    evidence_paths.append(
                        _write_evidence(
                            evidence_path,
                            _evidence(
                                method=method,
                                path=_safe_request_target(request_url),
                                json_body=json_body,
                                has_json_body=has_json_body,
                                status_code=None,
                                truncated=None,
                                body_hash=None,
                                assertion_outcomes=[],
                                failure_category="network_error",
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
                    f"{_step_token(index)}:network_error",
                )

            text = body.decode("utf-8", errors="replace")
            outcomes: list[dict[str, object]] = []
            failure_category: str | None = None
            for item in step.assertions:
                passed, category = evaluate_assertion(
                    item, status_code=response.status_code, text=text
                )
                outcomes.append(
                    {
                        "category": category,
                        "outcome": "passed" if passed else "failed",
                    }
                )
                if not passed:
                    failure_category = category
                    break

            try:
                evidence_paths.append(
                    _write_evidence(
                        evidence_path,
                        _evidence(
                            method=method,
                            path=_safe_request_target(request_url),
                            json_body=json_body,
                            has_json_body=has_json_body,
                            status_code=response.status_code,
                            truncated=truncated,
                            body_hash=_redacted_body_hash(body, truncated),
                            assertion_outcomes=outcomes,
                            failure_category=failure_category,
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
            if failure_category is not None:
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{_step_token(index)}:{failure_category}",
                )
            passed_steps += 1

    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.PASS,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
    )
