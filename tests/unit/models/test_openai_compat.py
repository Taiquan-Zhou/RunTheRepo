import asyncio
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Self, cast

import httpx
import pytest
from pydantic import BaseModel, Field

from repotrial.models import openai_compat
from repotrial.models.openai_compat import (
    ModelAdapterError,
    ModelAdapterFailureCode,
    OpenAICompatibleModelAdapter,
)
from repotrial.trial.planner import plan_journeys, propose_recovery


class _Answer(BaseModel):
    answer: str


class _ResponseServer:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.authorization_headers: list[str | None] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                parent.requests.append((self.path, json.loads(self.rfile.read(length))))
                parent.authorization_headers.append(self.headers.get("Authorization"))
                status, body = parent.responses.pop(0)
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}/v1/"
        self._thread = Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._server.server_port}{path}"


def _chat_completion(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}]}


class _ControlledClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_structured_returns_the_requested_pydantic_type_from_json_schema_response() -> (
    None
):
    with _ResponseServer([(200, _chat_completion('{"answer":"ok"}'))]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="ok")
    assert type(result) is _Answer
    assert server.requests[0][0] == "/v1/chat/completions"
    assert server.authorization_headers == [None]
    assert server.requests[0][1]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "_Answer",
            "strict": True,
            "schema": _Answer.model_json_schema(),
        },
    }


def test_request_timeout_preserves_short_connect_and_allows_ninety_second_reads() -> (
    None
):
    assert openai_compat._REQUEST_TIMEOUT == httpx.Timeout(90.0, connect=3.0)


def test_response_after_former_ten_second_read_timeout_is_accepted() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            time.sleep(10.1)
            encoded = json.dumps(_chat_completion('{"answer":"late"}')).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with _temporary_server(Handler) as server:
        adapter = OpenAICompatibleModelAdapter(f"{server}/v1", "local-model")
        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="late")


def test_invalid_model_output_is_retried_once_then_fails_without_response_data() -> (
    None
):
    with _ResponseServer(
        [
            (200, _chat_completion('{"answer":1}')),
            (200, _chat_completion('{"answer":2}')),
        ]
    ) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        with pytest.raises(
            ModelAdapterError, match="invalid structured response"
        ) as error:
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

    assert len(server.requests) == 2
    assert "answer" not in str(error.value)


def test_model_adapter_error_exposes_a_closed_reason_code_without_timeout_inheritance() -> (
    None
):
    error = ModelAdapterError("model request failed", reason_code="http_error")

    assert error.reason_code == "http_error"
    assert not isinstance(error, TimeoutError)

    with pytest.raises(ValueError, match="reason code"):
        ModelAdapterError(
            "model request failed",
            reason_code=cast(ModelAdapterFailureCode, "unknown"),
        )


def test_explicit_json_schema_unsupported_response_uses_one_json_only_fallback() -> (
    None
):
    with _ResponseServer(
        [
            (
                400,
                {
                    "error": {
                        "param": "response_format",
                        "message": "response_format json_schema unsupported",
                    }
                },
            ),
            (200, _chat_completion('{"answer":"fallback"}')),
        ]
    ) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="fallback")
    assert len(server.requests) == 2
    assert "response_format" in server.requests[0][1]
    assert "response_format" not in server.requests[1][1]
    fallback_system = server.requests[1][1]["messages"][0]["content"]
    assert "valid JSON" in fallback_system


def test_generic_response_format_rejection_uses_one_json_only_fallback() -> None:
    with _ResponseServer(
        [
            (
                400,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                        "message": "response_format is unavailable for this request",
                    }
                },
            ),
            (200, _chat_completion('{"answer":"portable"}')),
        ]
    ) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="portable")
    assert len(server.requests) == 2
    assert "response_format" in server.requests[0][1]
    assert "response_format" not in server.requests[1][1]


def test_strict_request_consumption_reduces_fallback_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ControlledClock()
    monkeypatch.setattr(openai_compat, "_OPERATION_TIMEOUT_S", 10.0, raising=False)
    monkeypatch.setattr(openai_compat, "_monotonic", clock, raising=False)
    request_timeouts: list[float | None] = []

    async def request(
        payload: dict[str, object], *, timeout: float | None = None
    ) -> openai_compat._HttpResponse:
        del payload
        request_timeouts.append(timeout)
        if len(request_timeouts) == 1:
            clock.advance(3.0)
            return openai_compat._HttpResponse(
                400,
                b'{"error":{"param":"response_format","message":"response_format json_schema unsupported"}}',
            )
        return openai_compat._HttpResponse(
            200, json.dumps(_chat_completion('{"answer":"ok"}')).encode()
        )

    adapter = OpenAICompatibleModelAdapter("http://127.0.0.1", "model")
    monkeypatch.setattr(adapter, "_request", request)

    assert asyncio.run(
        adapter.structured(system="system", user="user", schema=_Answer)
    ) == _Answer(answer="ok")
    assert request_timeouts == [10.0, 7.0]


def test_expired_operation_deadline_does_not_start_fallback_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ControlledClock()
    monkeypatch.setattr(openai_compat, "_OPERATION_TIMEOUT_S", 5.0, raising=False)
    monkeypatch.setattr(openai_compat, "_monotonic", clock, raising=False)
    request_count = 0

    async def request(
        payload: dict[str, object], *, timeout: float | None = None
    ) -> openai_compat._HttpResponse:
        nonlocal request_count
        del payload, timeout
        request_count += 1
        clock.advance(5.0)
        if request_count == 1:
            return openai_compat._HttpResponse(
                400,
                b'{"error":{"param":"response_format","message":"response_format json_schema unsupported"}}',
            )
        return openai_compat._HttpResponse(
            200, json.dumps(_chat_completion('{"answer":"unexpected"}')).encode()
        )

    adapter = OpenAICompatibleModelAdapter("http://127.0.0.1", "model")
    monkeypatch.setattr(adapter, "_request", request)

    with pytest.raises(TimeoutError, match="deadline"):
        asyncio.run(adapter.structured(system="system", user="user", schema=_Answer))

    assert request_count == 1


def test_strict_retry_shares_operation_deadline_and_stops_at_two_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ControlledClock()
    monkeypatch.setattr(openai_compat, "_OPERATION_TIMEOUT_S", 10.0, raising=False)
    monkeypatch.setattr(openai_compat, "_monotonic", clock, raising=False)
    request_timeouts: list[float | None] = []

    async def request(
        payload: dict[str, object], *, timeout: float | None = None
    ) -> openai_compat._HttpResponse:
        del payload
        request_timeouts.append(timeout)
        clock.advance(2.0)
        return openai_compat._HttpResponse(
            200, json.dumps(_chat_completion('{"answer":1}')).encode()
        )

    adapter = OpenAICompatibleModelAdapter("http://127.0.0.1", "model")
    monkeypatch.setattr(adapter, "_request", request)

    with pytest.raises(ModelAdapterError, match="invalid structured response"):
        asyncio.run(adapter.structured(system="system", user="user", schema=_Answer))

    assert request_timeouts == [10.0, 8.0]


@pytest.mark.parametrize(
    "error",
    [
        {
            "type": "invalid_request_error",
            "message": "invalid API key for response_format",
        },
        {"code": "model_not_found", "message": "model cannot use response_format"},
        {"code": "insufficient_quota", "message": "quota blocks response_format"},
        {"type": "rate_limit_error", "message": "rate limit for response_format"},
        {
            "code": "context_length_exceeded",
            "message": "context limit in response_format request",
        },
    ],
)
def test_generic_non_capability_classification_never_falls_back(
    error: dict[str, str],
) -> None:
    with _ResponseServer([(400, {"error": error})]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
    assert len(server.requests) == 1


def test_invalid_portable_fallback_stops_after_two_requests() -> None:
    responses = [
        (
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "response_format unavailable",
                }
            },
        ),
        (200, _chat_completion('{"answer":1}')),
    ]
    with _ResponseServer(responses) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        with pytest.raises(ModelAdapterError, match="invalid structured response"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
    assert len(server.requests) == 2


def test_strict_retry_cannot_turn_into_a_third_fallback_request() -> None:
    responses = [
        (200, _chat_completion('{"answer":1}')),
        (
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "response_format unavailable",
                }
            },
        ),
    ]
    with _ResponseServer(responses) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "code", ["invalid_api_key", "invalid_model", "insufficient_quota"]
)
def test_explicit_non_capability_codes_veto_json_only_fallback(code: str) -> None:
    response = {
        "error": {
            "code": code,
            "param": "response_format",
            "message": "response_format json_schema unsupported",
        }
    }
    with _ResponseServer(
        [(400, response), (200, _chat_completion('{"answer":"no"}'))]
    ) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

    assert len(server.requests) == 1


def test_auth_failure_does_not_trigger_fallback_or_leak_the_api_key() -> None:
    api_key = "test-secret-key"
    with _ResponseServer([(401, {"error": {"message": "bad credentials"}})]) as server:
        adapter = OpenAICompatibleModelAdapter(
            server.endpoint, "local-model", api_key=api_key
        )

        with pytest.raises(ModelAdapterError, match="model request failed") as error:
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

    assert len(server.requests) == 1
    assert server.authorization_headers == [f"Bearer {api_key}"]
    assert api_key not in str(error.value)


def test_endpoint_credentials_are_rejected_before_any_request() -> None:
    with pytest.raises(ValueError, match="endpoint must not include credentials"):
        OpenAICompatibleModelAdapter("http://user:pass@127.0.0.1:8000/v1", "model")


@pytest.mark.parametrize(
    ("status", "error", "label"),
    [
        (
            400,
            {
                "type": "authentication_error",
                "message": "response_format json_schema unsupported",
            },
            "authentication",
        ),
        (
            422,
            {
                "code": "model_not_found",
                "message": "response_format json_schema unsupported",
            },
            "model",
        ),
        (
            400,
            {
                "type": "rate_limit_error",
                "message": "response_format json_schema unsupported",
            },
            "rate limit",
        ),
        (
            500,
            {"message": "response_format json_schema unsupported"},
            "server",
        ),
    ],
)
def test_forbidden_error_classes_never_trigger_json_only_fallback(
    status: int, error: dict[str, str], label: str
) -> None:
    with _ResponseServer([(status, {"error": error})]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

    assert len(server.requests) == 1, label


@pytest.mark.parametrize("status", [400, 422])
def test_non_capability_near_miss_does_not_trigger_fallback(status: int) -> None:
    response = {
        "error": {
            "param": "model",
            "message": "response_format json_schema unsupported",
        }
    }
    with _ResponseServer([(status, response)]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("endpoint_path", "request_path"),
    [
        ("/v1", "/v1/chat/completions"),
        ("/v1/", "/v1/chat/completions"),
        ("/local/openai/v1/", "/local/openai/v1/chat/completions"),
    ],
)
def test_endpoint_normalization_preserves_the_base_path(
    endpoint_path: str, request_path: str
) -> None:
    with _ResponseServer([(200, _chat_completion('{"answer":"ok"}'))]) as server:
        adapter = OpenAICompatibleModelAdapter(server.url(endpoint_path), "local-model")

        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="ok")
    assert [request[0] for request in server.requests] == [request_path]


def test_chunked_response_over_limit_stops_before_the_full_body_is_consumed() -> None:
    chunk = b"x" * 65_536
    chunks = (openai_compat._MAX_RESPONSE_BYTES // len(chunk)) + 512
    sent_chunks = 0
    request_seen = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal sent_chunks
            request_seen.set()
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(chunks):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                    self.wfile.flush()
                    sent_chunks += 1
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with _temporary_server(Handler) as server:
        adapter = OpenAICompatibleModelAdapter(f"{server}/v1", "local-model")

        with pytest.raises(ModelAdapterError, match="response exceeds size limit"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

    assert request_seen.is_set()
    assert sent_chunks < chunks


def test_transport_failure_makes_exactly_one_request_attempt() -> None:
    attempts = _RequestAttempts()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            attempts.add()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with _temporary_server(Handler) as server:
        adapter = OpenAICompatibleModelAdapter(f"{server}/v1", "model")

        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )

        assert attempts.value == 1


def test_timeout_does_not_trigger_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = _RequestAttempts()
    release_server = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            attempts.add()
            release_server.wait()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    monkeypatch.setattr(openai_compat, "_REQUEST_TIMEOUT", httpx.Timeout(0.01))
    with _temporary_server(Handler) as server:
        adapter = OpenAICompatibleModelAdapter(f"{server}/v1", "model")

        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
        release_server.set()
        assert attempts.value == 1


def test_adapter_failure_uses_existing_planner_and_recovery_timeout_boundary(
    tmp_path: Path,
) -> None:
    with _ResponseServer([(500, {"error": {"message": "server failure"}})]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        journeys = asyncio.run(plan_journeys(tmp_path, "", adapter))

    with _ResponseServer([(500, {"error": {"message": "server failure"}})]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        recovery = asyncio.run(
            propose_recovery(
                {"logs": "unrecognized startup failure"}, "", set(), 0, adapter
            )
        )

    assert journeys == []
    assert recovery.action == "stop"
    assert recovery.reason == "model timeout"


def test_oversized_prompts_and_schema_are_rejected_before_network() -> None:
    class _OversizedSchema(BaseModel):
        answer: str = Field(description="x" * (openai_compat._MAX_SCHEMA_BYTES + 1))

    with _ResponseServer([]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        with pytest.raises(ValueError, match="prompt exceeds size limit"):
            asyncio.run(
                adapter.structured(
                    system="x" * (openai_compat._MAX_PROMPT_CHARS + 1),
                    user="user",
                    schema=_Answer,
                )
            )
        with pytest.raises(ValueError, match="prompt exceeds size limit"):
            asyncio.run(
                adapter.structured(
                    system="system",
                    user="x" * (openai_compat._MAX_PROMPT_CHARS + 1),
                    schema=_Answer,
                )
            )
        with pytest.raises(ValueError, match="schema exceeds size limit"):
            asyncio.run(
                adapter.structured(
                    system="system", user="user", schema=_OversizedSchema
                )
            )

    assert server.requests == []


def test_redirect_response_is_not_followed() -> None:
    target_attempts = _RequestAttempts()

    class TargetHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            target_attempts.add()
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with _temporary_server(TargetHandler) as target:
        source_attempts = _RequestAttempts()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                source_attempts.add()
                self.send_response(307)
                self.send_header("Location", f"{target}/redirect-target")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        with _temporary_server(RedirectHandler) as source:
            adapter = OpenAICompatibleModelAdapter(f"{source}/v1", "local-model")
            with pytest.raises(ModelAdapterError, match="model request failed"):
                asyncio.run(
                    adapter.structured(system="system", user="user", schema=_Answer)
                )

    assert source_attempts.value == 1
    assert target_attempts.value == 0


def test_proxy_environment_is_ignored_when_trust_env_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_attempts = _RequestAttempts()

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            proxy_attempts.add()
            self.send_response(502)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with (
        _temporary_server(ProxyHandler) as proxy,
        _ResponseServer([(200, _chat_completion('{"answer":"ok"}'))]) as target,
    ):
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        adapter = OpenAICompatibleModelAdapter(target.endpoint, "local-model")
        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="ok")
    assert len(target.requests) == 1
    assert proxy_attempts.value == 0


def test_cancelling_real_adapter_request_propagates_and_stops_after_one_attempt() -> (
    None
):
    attempts = _RequestAttempts()
    request_started = Event()
    release_server = Event()
    request_finished = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            attempts.add()
            request_started.set()
            release_server.wait()
            request_finished.set()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    async def cancel_request(endpoint: str) -> None:
        adapter = OpenAICompatibleModelAdapter(f"{endpoint}/v1", "local-model")
        task = asyncio.create_task(
            adapter.structured(system="system", user="user", schema=_Answer)
        )
        await asyncio.wait_for(asyncio.to_thread(request_started.wait), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with _temporary_server(Handler) as server:
        try:
            asyncio.run(cancel_request(server))
        finally:
            release_server.set()
            assert request_finished.wait(1)

    assert attempts.value == 1


class _RequestAttempts:
    def __init__(self) -> None:
        self._count = 0
        self._lock = Lock()

    def add(self) -> None:
        with self._lock:
            self._count += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._count


class _TemporaryServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    def __enter__(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()


def _temporary_server(handler: type[BaseHTTPRequestHandler]) -> _TemporaryServer:
    return _TemporaryServer(handler)
