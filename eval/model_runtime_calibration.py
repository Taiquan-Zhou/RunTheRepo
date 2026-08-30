"""Reproducible, sanitized calibration for the frozen local model runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Protocol
from unicodedata import category
from urllib.parse import urlsplit, urlunsplit

import httpx

from repotrial.domain.models import Journey
from repotrial.models.base import ModelAdapter, RecoveryAction
from repotrial.models.openai_compat import OpenAICompatibleModelAdapter, _HttpResponse
from repotrial.trial.planner import plan_journeys, propose_recovery

DEFAULT_ENDPOINT = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen3:4b-instruct"
DEFAULT_RUNTIME_VERSION = "0.33.2"
DEFAULT_MODEL_DIGEST = (
    "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
)
OUTER_DEADLINE_SECONDS = 95.0
RECOVERY_EVIDENCE = "application exited with status 1 after dependency handshake failed"
JOURNEY_EVIDENCE = (
    "Define exactly one HTTP Journey DSL item. Use journey_id health, name Health "
    "check, and exactly one step with step_id request-health, tool http, action "
    "request, params method GET and path /health, plus exactly one status_code "
    "assertion targeting response.status with integer expected 200."
)
_MAX_NATIVE_RESPONSE_BYTES = 1_048_576
_NATIVE_TIMEOUT = httpx.Timeout(120.0, connect=3.0)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]{0,255}\Z")
_SAFE_PUBLIC_STOP_REASONS = frozenset(
    {
        "invalid recovery evidence",
        "model adapter error",
        "model timeout",
        "no recovery action",
        "too many errors",
        "unsafe proposal",
    }
)
_METRIC_FIELD_ALLOWLIST = frozenset(
    {
        "schema_version",
        "event",
        "schema",
        "run",
        "runtime_version",
        "model",
        "digest",
        "status",
        "statuses",
        "requests",
        "fallback",
        "validation",
        "public_stop_reason",
        "journey_count",
        "elapsed_seconds",
        "recovery_validation",
        "journey_validation",
        "resident",
        "outer_deadline_seconds",
    }
)

type MetricRecord = dict[str, object]
type EmitMetric = Callable[[MetricRecord], None]


class CalibrationError(RuntimeError):
    """A sanitized calibration failure suitable for a JSONL status code."""


class TracedAdapter(ModelAdapter, Protocol):
    trace: RequestTrace


class NativeApi(Protocol):
    async def get_json(self, path: str) -> NativeResponse: ...

    async def post_json(
        self, path: str, payload: dict[str, object]
    ) -> NativeResponse: ...


@dataclass(frozen=True)
class CalibrationConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    runtime_version: str = DEFAULT_RUNTIME_VERSION
    model_digest: str = DEFAULT_MODEL_DIGEST
    outer_deadline_seconds: float = OUTER_DEADLINE_SECONDS

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/v1"
        ):
            raise ValueError("endpoint must be a credential-free Ollama /v1 URL")
        if _MODEL_NAME.fullmatch(self.model) is None:
            raise ValueError("model must be a bounded credential-free identifier")
        if not _safe_identifier(self.runtime_version):
            raise ValueError("runtime version must be a bounded identifier")
        if _DIGEST.fullmatch(self.model_digest) is None:
            raise ValueError("model digest must be 64 lowercase hexadecimal characters")
        if self.outer_deadline_seconds != OUTER_DEADLINE_SECONDS:
            raise ValueError(
                "outer deadline must preserve the frozen 95 second contract"
            )

    @property
    def native_base_url(self) -> str:
        parsed = urlsplit(self.endpoint)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


@dataclass
class RequestTrace:
    modes: list[str] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class NativeResponse:
    status_code: int
    data: object


@dataclass(frozen=True)
class InvalidProbeSession:
    recovery_adapter: TracedAdapter
    journey_adapter: TracedAdapter
    request_count: Callable[[], int]


type AdapterFactory = Callable[[str, str], TracedAdapter]
type InvalidSessionFactory = Callable[
    [str, str], AbstractContextManager[InvalidProbeSession]
]
type ProposeRecovery = Callable[
    [dict[str, str], str, set[str], int, ModelAdapter | None],
    Awaitable[RecoveryAction],
]
type PlanJourneys = Callable[[Path, str, ModelAdapter | None], Awaitable[list[Journey]]]


@dataclass(frozen=True)
class CalibrationDependencies:
    native_api: NativeApi
    adapter_factory: AdapterFactory
    invalid_session_factory: InvalidSessionFactory
    propose_recovery: ProposeRecovery
    plan_journeys: PlanJourneys
    monotonic: Callable[[], float]


class TracingOpenAICompatibleModelAdapter(OpenAICompatibleModelAdapter):
    """Record transport shape while retaining the production adapter implementation."""

    def __init__(self, endpoint: str, model_name: str) -> None:
        super().__init__(endpoint, model_name)
        self.trace = RequestTrace()

    async def _request(self, payload: dict[str, object]) -> _HttpResponse:
        self.trace.modes.append(
            "json_schema" if "response_format" in payload else "json_only"
        )
        response = await super()._request(payload)
        self.trace.statuses.append(response.status_code)
        return response


class NativeOllamaApi:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    async def get_json(self, path: str) -> NativeResponse:
        return await self._request("GET", path, None)

    async def post_json(self, path: str, payload: dict[str, object]) -> NativeResponse:
        return await self._request("POST", path, payload)

    async def _request(
        self, method: str, path: str, payload: dict[str, object] | None
    ) -> NativeResponse:
        try:
            async with httpx.AsyncClient(
                timeout=_NATIVE_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                if payload is None:
                    response = await client.request(method, f"{self._base_url}{path}")
                else:
                    response = await client.request(
                        method, f"{self._base_url}{path}", json=payload
                    )
        except httpx.HTTPError:
            raise CalibrationError("native_api_transport_error") from None
        if len(response.content) > _MAX_NATIVE_RESPONSE_BYTES:
            raise CalibrationError("native_api_response_too_large")
        try:
            data: object = response.json()
        except json.JSONDecodeError:
            raise CalibrationError("native_api_invalid_json") from None
        return NativeResponse(response.status_code, data)


class _InvalidResponseServer(AbstractContextManager[InvalidProbeSession]):
    def __init__(self, model: str) -> None:
        self._model = model
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._requests = 0

    def __enter__(self) -> InvalidProbeSession:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                owner._requests += 1
                content = _invalid_content(self)
                envelope = {
                    "id": "sanitized-invalid-probe",
                    "object": "chat.completion",
                    "model": owner._model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(content, separators=(",", ":")),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                body = json.dumps(envelope, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        endpoint = f"http://127.0.0.1:{port}/v1"
        return InvalidProbeSession(
            recovery_adapter=TracingOpenAICompatibleModelAdapter(endpoint, self._model),
            journey_adapter=TracingOpenAICompatibleModelAdapter(endpoint, self._model),
            request_count=lambda: self._requests,
        )

    def __exit__(self, *args: object) -> None:
        del args
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _invalid_content(request: BaseHTTPRequestHandler) -> dict[str, object]:
    try:
        declared_length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        declared_length = 0
    if not 0 <= declared_length <= _MAX_NATIVE_RESPONSE_BYTES:
        return {"invalid": True}
    try:
        payload: object = json.loads(request.rfile.read(declared_length))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"invalid": True}
    if _schema_name(payload) == "RecoveryAction":
        return {"action": "run", "params": {}, "reason": "invalid action"}
    return {
        "journeys": [
            {
                "journey_id": "invalid",
                "name": "Invalid method",
                "steps": [
                    {
                        "step_id": "invalid-method",
                        "tool": "http",
                        "action": "request",
                        "params": {"method": "PUT", "path": "/health"},
                        "assertions": [
                            {
                                "kind": "status_code",
                                "target": "response.status",
                                "expected": 200,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _schema_name(payload: object) -> str | None:
    mapping = _mapping(payload)
    response_format = _mapping(mapping.get("response_format")) if mapping else None
    json_schema = (
        _mapping(response_format.get("json_schema")) if response_format else None
    )
    name = json_schema.get("name") if json_schema else None
    return name if isinstance(name, str) else None


async def run_calibration(
    config: CalibrationConfig,
    dependencies: CalibrationDependencies,
    emit: EmitMetric,
) -> None:
    await _verify_metadata(config, dependencies.native_api, emit)
    await _native_action(
        dependencies.native_api,
        dependencies.monotonic,
        emit,
        "prewarm",
        {
            "model": config.model,
            "prompt": "",
            "stream": False,
            "keep_alive": -1,
        },
    )
    for run in range(1, 4):
        await _recovery_probe(config, dependencies, emit, run)
    for run in range(1, 4):
        await _journey_probe(config, dependencies, emit, "warm_journey", run)
    await _invalid_probe(config, dependencies, emit)
    await _native_action(
        dependencies.native_api,
        dependencies.monotonic,
        emit,
        "unload",
        {"model": config.model, "stream": False, "keep_alive": 0},
    )
    await _verify_nonresident(config, dependencies.native_api, emit)
    await _journey_probe(config, dependencies, emit, "cold_journey", 1)
    _emit(
        emit,
        event="summary",
        validation="passed",
        outer_deadline_seconds=config.outer_deadline_seconds,
    )


async def _verify_metadata(
    config: CalibrationConfig, native_api: NativeApi, emit: EmitMetric
) -> None:
    version_response = await native_api.get_json("/api/version")
    tags_response = await native_api.get_json("/api/tags")
    if not _successful(version_response.status_code) or not _successful(
        tags_response.status_code
    ):
        raise CalibrationError("metadata_http_error")
    version_data = _mapping(version_response.data)
    tags_data = _mapping(tags_response.data)
    version = version_data.get("version") if version_data else None
    digest = _tag_digest(tags_data, config.model)
    if version != config.runtime_version or digest != config.model_digest:
        raise CalibrationError("metadata_mismatch")
    _emit(
        emit,
        event="metadata",
        runtime_version=config.runtime_version,
        model=config.model,
        digest=config.model_digest,
        status=version_response.status_code,
        validation="matched",
    )


async def _native_action(
    native_api: NativeApi,
    monotonic: Callable[[], float],
    emit: EmitMetric,
    event: str,
    payload: dict[str, object],
) -> None:
    started = monotonic()
    response = await native_api.post_json("/api/generate", payload)
    elapsed = monotonic() - started
    if not _successful(response.status_code):
        raise CalibrationError(f"{event}_http_error")
    _emit(
        emit,
        event=event,
        status=response.status_code,
        elapsed_seconds=round(elapsed, 6),
        validation="passed",
    )


async def _recovery_probe(
    config: CalibrationConfig,
    dependencies: CalibrationDependencies,
    emit: EmitMetric,
    run: int,
) -> None:
    adapter = dependencies.adapter_factory(config.endpoint, config.model)
    started = dependencies.monotonic()
    result = await dependencies.propose_recovery(
        {"logs": RECOVERY_EVIDENCE}, "", set(), 0, adapter
    )
    elapsed = dependencies.monotonic() - started
    if not isinstance(result, RecoveryAction):
        raise CalibrationError("recovery_public_contract_error")
    fallback = _validated_trace(adapter.trace)
    validation, stop_reason = _recovery_validation(result)
    metric: MetricRecord = {
        "event": "warm_recovery",
        "schema": "RecoveryAction",
        "run": run,
        "statuses": list(adapter.trace.statuses),
        "requests": len(adapter.trace.modes),
        "fallback": fallback,
        "validation": validation,
        "elapsed_seconds": round(elapsed, 6),
    }
    if stop_reason is not None:
        metric["public_stop_reason"] = stop_reason
    _emit(emit, **metric)
    if validation not in {
        "schema_valid_policy_valid",
        "schema_valid_policy_rejected",
    }:
        raise CalibrationError("recovery_schema_probe_failed")


async def _journey_probe(
    config: CalibrationConfig,
    dependencies: CalibrationDependencies,
    emit: EmitMetric,
    event: str,
    run: int,
) -> None:
    adapter = dependencies.adapter_factory(config.endpoint, config.model)
    started = dependencies.monotonic()
    with tempfile.TemporaryDirectory() as directory:
        result = await dependencies.plan_journeys(
            Path(directory), JOURNEY_EVIDENCE, adapter
        )
    elapsed = dependencies.monotonic() - started
    if not isinstance(result, list) or not all(
        isinstance(journey, Journey) for journey in result
    ):
        raise CalibrationError("journey_public_contract_error")
    fallback = _validated_trace(adapter.trace)
    validation = "schema_valid_policy_valid" if result else "policy_rejected"
    _emit(
        emit,
        event=event,
        schema="Journey",
        run=run,
        statuses=list(adapter.trace.statuses),
        requests=len(adapter.trace.modes),
        fallback=fallback,
        validation=validation,
        journey_count=len(result),
        elapsed_seconds=round(elapsed, 6),
    )
    if not result:
        raise CalibrationError("journey_policy_rejected")
    if event == "cold_journey" and elapsed >= config.outer_deadline_seconds:
        raise CalibrationError("cold_journey_deadline_exceeded")


async def _invalid_probe(
    config: CalibrationConfig,
    dependencies: CalibrationDependencies,
    emit: EmitMetric,
) -> None:
    with dependencies.invalid_session_factory(config.endpoint, config.model) as session:
        recovery = await dependencies.propose_recovery(
            {"logs": RECOVERY_EVIDENCE}, "", set(), 0, session.recovery_adapter
        )
        with tempfile.TemporaryDirectory() as directory:
            journeys = await dependencies.plan_journeys(
                Path(directory), JOURNEY_EVIDENCE, session.journey_adapter
            )
        requests = session.request_count()
        traces = session.recovery_adapter.trace, session.journey_adapter.trace
        statuses = traces[0].statuses + traces[1].statuses
        modes = traces[0].modes + traces[1].modes
    recovery_closed = (
        isinstance(recovery, RecoveryAction)
        and recovery.action == "stop"
        and recovery.reason == "unsafe proposal"
    )
    journey_closed = isinstance(journeys, list) and journeys == []
    fallback = any(mode == "json_only" for mode in modes)
    _emit(
        emit,
        event="intentionally_invalid",
        schema="RecoveryAction+Journey",
        statuses=list(statuses),
        requests=requests,
        fallback=fallback,
        recovery_validation="fail_closed" if recovery_closed else "unexpected_accept",
        journey_validation="fail_closed" if journey_closed else "unexpected_accept",
    )
    if (
        not recovery_closed
        or not journey_closed
        or requests != 2
        or modes != ["json_schema", "json_schema"]
        or statuses != [200, 200]
    ):
        raise CalibrationError("invalid_response_accepted")


async def _verify_nonresident(
    config: CalibrationConfig, native_api: NativeApi, emit: EmitMetric
) -> None:
    response = await native_api.get_json("/api/ps")
    if not _successful(response.status_code):
        raise CalibrationError("residency_http_error")
    resident = _model_is_resident(response.data, config.model)
    _emit(
        emit,
        event="residency",
        status=response.status_code,
        resident=resident,
        validation="nonresident" if not resident else "resident",
    )
    if resident:
        raise CalibrationError("model_still_resident")


def _validated_trace(trace: RequestTrace) -> bool:
    if (
        not trace.modes
        or len(trace.modes) != len(trace.statuses)
        or any(status != 200 for status in trace.statuses)
    ):
        raise CalibrationError("model_transport_failed")
    fallback = any(mode == "json_only" for mode in trace.modes)
    if any(mode not in {"json_schema", "json_only"} for mode in trace.modes):
        raise CalibrationError("unknown_model_request_mode")
    return fallback


def _recovery_validation(result: RecoveryAction) -> tuple[str, str | None]:
    if result.action != "stop":
        return "schema_valid_policy_valid", None
    public_reason = (
        result.reason
        if result.reason in _SAFE_PUBLIC_STOP_REASONS
        else "model_supplied_redacted"
    )
    if result.reason == "unsafe proposal":
        return "schema_valid_policy_rejected", public_reason
    if result.reason in _SAFE_PUBLIC_STOP_REASONS:
        return "public_planner_failure", public_reason
    return "schema_valid_policy_valid", public_reason


def _tag_digest(tags: dict[str, object] | None, model: str) -> str | None:
    models = tags.get("models") if tags else None
    if type(models) is not list:
        return None
    for item in models:
        item_mapping = _mapping(item)
        if item_mapping and item_mapping.get("name") == model:
            digest = item_mapping.get("digest")
            return digest if isinstance(digest, str) else None
    return None


def _model_is_resident(data: object, model: str) -> bool:
    mapping = _mapping(data)
    models = mapping.get("models") if mapping else None
    if type(models) is not list:
        raise CalibrationError("residency_invalid_payload")
    return any(
        (item_mapping := _mapping(item)) is not None
        and item_mapping.get("name") == model
        for item in models
    )


def _mapping(value: object) -> dict[str, object] | None:
    if type(value) is not dict or not all(isinstance(key, str) for key in value):
        return None
    return value


def _safe_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and not any(category(character).startswith("C") for character in value)
    )


def _successful(status_code: int) -> bool:
    return 200 <= status_code < 300


def _emit(emit: EmitMetric, **fields: object) -> None:
    metric: MetricRecord = {"schema_version": 1, **fields}
    if not set(metric) <= _METRIC_FIELD_ALLOWLIST:
        raise CalibrationError("metric_field_not_allowed")
    emit(metric)


def _jsonl_emitter(metric: MetricRecord) -> None:
    print(json.dumps(metric, sort_keys=True, separators=(",", ":")), flush=True)


def _default_dependencies(config: CalibrationConfig) -> CalibrationDependencies:
    return CalibrationDependencies(
        native_api=NativeOllamaApi(config.native_base_url),
        adapter_factory=TracingOpenAICompatibleModelAdapter,
        invalid_session_factory=lambda endpoint, model: _InvalidResponseServer(model),
        propose_recovery=propose_recovery,
        plan_journeys=plan_journeys,
        monotonic=time.perf_counter,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the frozen Ollama model-runtime calibration."
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--runtime-version", default=DEFAULT_RUNTIME_VERSION)
    parser.add_argument("--model-digest", default=DEFAULT_MODEL_DIGEST)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = CalibrationConfig(
            endpoint=args.endpoint,
            model=args.model,
            runtime_version=args.runtime_version,
            model_digest=args.model_digest,
        )
    except ValueError:
        _jsonl_emitter(
            {"schema_version": 1, "event": "failure", "validation": "config_invalid"}
        )
        return 2
    try:
        asyncio.run(
            run_calibration(config, _default_dependencies(config), _jsonl_emitter)
        )
    except CalibrationError as error:
        _jsonl_emitter(
            {"schema_version": 1, "event": "failure", "validation": str(error)}
        )
        return 1
    except (OSError, RuntimeError, TypeError, UnicodeError):
        _jsonl_emitter(
            {"schema_version": 1, "event": "failure", "validation": "internal_error"}
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
