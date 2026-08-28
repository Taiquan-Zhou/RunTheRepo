import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import TracebackType
from typing import Self

import pytest
from pydantic import BaseModel

from repotrial.models.openai_compat import (
    ModelAdapterError,
    OpenAICompatibleModelAdapter,
)


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
        self._thread = Thread(target=self._server.serve_forever)

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


def _chat_completion(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}]}


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


def test_explicit_json_schema_unsupported_response_uses_one_json_only_fallback() -> (
    None
):
    with _ResponseServer(
        [
            (
                400,
                {"error": {"message": "response_format json_schema unsupported"}},
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
