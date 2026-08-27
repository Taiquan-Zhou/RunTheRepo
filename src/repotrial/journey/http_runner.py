"""A bounded, deterministic HTTP runner for declared journeys."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from repotrial.domain.enums import Verdict
from repotrial.domain.models import Journey, JourneyResult, JourneyStep
from repotrial.journey.verifier import evaluate_assertion

_BODY_LIMIT_BYTES = 65_536
_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_ASSERTIONS_PER_STEP = 64
_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:password|token|secret|api[_-]?key)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
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


def _validate_base_url(base_url: str) -> str | None:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


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

    method = step.params["method"]
    path = step.params["path"]
    if not isinstance(method, str) or method not in _ALLOWED_METHODS:
        return "invalid_method", None, None, None, False
    if not isinstance(path, str):
        return "invalid_path", None, None, None, False
    parsed_path = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed_path.scheme
        or parsed_path.netloc
        or "\\" in path
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
    redacted = _PEM_PRIVATE_KEY.sub("<redacted-private-key>", text)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", redacted)
    redacted = _BEARER_TOKEN.sub("Bearer <redacted>", redacted)
    if truncated:
        redacted += "\n[repotrial:truncated]\n"
    return hashlib.sha256(redacted.encode("utf-8")).hexdigest()


async def _read_bounded_body(response: httpx.Response) -> tuple[bytes, bool]:
    body = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
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
    path.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
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

    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_paths: list[str] = []
    passed_steps = 0
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS),
        follow_redirects=False,
    ) as client:
        for index, step in enumerate(journey.steps):
            evidence_path = evidence_dir / f"step-{index:04d}.json"
            validation_error, method, path, json_body, has_json_body = _validate_step(
                step
            )
            if validation_error is not None:
                evidence_paths.append(
                    _write_evidence(
                        evidence_path,
                        _evidence(
                            method=None,
                            path=None,
                            json_body=None,
                            has_json_body=False,
                            status_code=None,
                            truncated=None,
                            body_hash=None,
                            assertion_outcomes=[],
                            failure_category=validation_error,
                        ),
                    )
                )
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{step.step_id}:{validation_error}",
                )

            assert method is not None
            assert path is not None
            try:
                async with client.stream(
                    method, f"{origin}{path}", json=json_body
                ) as response:
                    body, truncated = await _read_bounded_body(response)
            except httpx.HTTPError:
                evidence_paths.append(
                    _write_evidence(
                        evidence_path,
                        _evidence(
                            method=method,
                            path=path,
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
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{step.step_id}:network_error",
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

            evidence_paths.append(
                _write_evidence(
                    evidence_path,
                    _evidence(
                        method=method,
                        path=path,
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
            if failure_category is not None:
                return _failure_result(
                    journey,
                    passed_steps,
                    evidence_paths,
                    f"{step.step_id}:{failure_category}",
                )
            passed_steps += 1

    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.PASS,
        passed_steps=passed_steps,
        total_steps=len(journey.steps),
        evidence_paths=evidence_paths,
    )
