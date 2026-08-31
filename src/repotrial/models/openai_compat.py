import asyncio
import json
import time
from dataclasses import dataclass
from typing import Final, Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

_REQUEST_TIMEOUT: Final = httpx.Timeout(90.0, connect=3.0)
_OPERATION_TIMEOUT_S: Final = 180.0
_MAX_PROMPT_CHARS: Final = 16_384
_MAX_SCHEMA_BYTES: Final = 65_536
_MAX_RESPONSE_BYTES: Final = 1_048_576
_GENERIC_REQUEST_ERROR_TYPES: Final = (
    "invalid_request_error",
    "bad_request",
    "request_error",
)
_FORBIDDEN_FALLBACK_MARKERS: Final = frozenset(
    {
        "authentication",
        "authorization",
        "unauthorized",
        "forbidden",
        "api_key",
        "api key",
        "invalid_model",
        "model_not_found",
        "insufficient_quota",
        "quota",
        "rate_limit",
        "rate limit",
        "context_length",
        "context limit",
    }
)
_MODEL_ADAPTER_FAILURE_CODES: Final = frozenset(
    {
        "transport_error",
        "http_error",
        "response_too_large",
        "structured_response_invalid",
    }
)


type ModelAdapterFailureCode = Literal[
    "transport_error",
    "http_error",
    "response_too_large",
    "structured_response_invalid",
]

_monotonic = time.monotonic


class ModelAdapterError(RuntimeError):
    """Credential-free failure returned by the OpenAI-compatible boundary."""

    def __init__(self, message: str, *, reason_code: ModelAdapterFailureCode) -> None:
        if reason_code not in _MODEL_ADAPTER_FAILURE_CODES:
            raise ValueError("invalid model adapter reason code")
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class _HttpResponse:
    status_code: int
    body: bytes


class OpenAICompatibleModelAdapter:
    def __init__(
        self, endpoint: str, model_name: str, *, api_key: str | None = None
    ) -> None:
        self._endpoint = _normalize_endpoint(endpoint)
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model name must be a non-empty string")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("API key must be a string")
        self._model_name = model_name
        self._api_key = api_key

    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT:
        operation_deadline = _monotonic() + _OPERATION_TIMEOUT_S
        bounded_system = _bounded_prompt(system)
        bounded_user = _bounded_prompt(user)
        schema_json = _bounded_schema(schema)
        first_payload = _json_schema_payload(
            self._model_name, bounded_system, bounded_user, schema, schema_json
        )
        first_response = await self._request_within_deadline(
            first_payload, operation_deadline
        )

        if _explicitly_unsupported_schema(first_response):
            fallback_payload = _json_only_payload(
                self._model_name,
                bounded_system,
                bounded_user,
                schema_json,
            )
            return _validated_result(
                schema,
                await self._successful_response(fallback_payload, operation_deadline),
            )

        if not _is_success(first_response.status_code):
            raise ModelAdapterError("model request failed", reason_code="http_error")
        try:
            return _validated_result(schema, first_response.body)
        except ModelAdapterError:
            return _validated_result(
                schema,
                await self._successful_response(first_payload, operation_deadline),
            )

    async def _successful_response(
        self, payload: dict[str, object], operation_deadline: float
    ) -> bytes:
        response = await self._request_within_deadline(payload, operation_deadline)
        if not _is_success(response.status_code):
            raise ModelAdapterError("model request failed", reason_code="http_error")
        return response.body

    async def _request_within_deadline(
        self, payload: dict[str, object], operation_deadline: float
    ) -> _HttpResponse:
        remaining = operation_deadline - _monotonic()
        if remaining <= 0:
            raise TimeoutError("model operation deadline exceeded")
        try:
            response = await asyncio.wait_for(
                self._request(payload, timeout=_request_timeout_budget(remaining)),
                timeout=remaining,
            )
        except TimeoutError:
            raise TimeoutError("model operation deadline exceeded") from None
        if operation_deadline - _monotonic() <= 0:
            raise TimeoutError("model operation deadline exceeded")
        return response

    async def _request(
        self, payload: dict[str, object], *, timeout: float | None = None
    ) -> _HttpResponse:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request_timeout = _bounded_http_timeout(timeout)
        try:
            async with (
                httpx.AsyncClient(
                    timeout=request_timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    f"{self._endpoint}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response,
            ):
                declared_length = response.headers.get("Content-Length")
                if _declared_response_is_too_large(declared_length):
                    raise ModelAdapterError(
                        "model response exceeds size limit",
                        reason_code="response_too_large",
                    )
                body = await _read_bounded_response(response)
                return _HttpResponse(response.status_code, body)
        except httpx.HTTPError:
            raise ModelAdapterError(
                "model request failed", reason_code="transport_error"
            ) from None


def _request_timeout_budget(remaining: float) -> float:
    read_timeout = _REQUEST_TIMEOUT.read
    if read_timeout is None:
        return remaining
    return min(read_timeout, remaining)


def _bounded_http_timeout(budget: float | None) -> httpx.Timeout:
    if budget is None:
        return _REQUEST_TIMEOUT

    def bounded(value: float | None) -> float | None:
        return None if value is None else min(value, budget)

    return httpx.Timeout(
        connect=bounded(_REQUEST_TIMEOUT.connect),
        read=bounded(_REQUEST_TIMEOUT.read),
        write=bounded(_REQUEST_TIMEOUT.write),
        pool=bounded(_REQUEST_TIMEOUT.pool),
    )


def _normalize_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("endpoint must be a non-empty HTTP URL")
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise ValueError("endpoint must be a valid HTTP URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must be a valid HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not include a query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _bounded_prompt(value: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_PROMPT_CHARS:
        raise ValueError("model prompt exceeds size limit")
    return value


def _bounded_schema(schema: type[BaseModel]) -> object:
    try:
        encoded = json.dumps(
            schema.model_json_schema(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("model schema is not JSON serializable") from None
    if len(encoded) > _MAX_SCHEMA_BYTES:
        raise ValueError("model schema exceeds size limit")
    return json.loads(encoded)


def _json_schema_payload(
    model_name: str,
    system: str,
    user: str,
    schema: type[BaseModel],
    schema_json: object,
) -> dict[str, object]:
    return {
        "model": model_name,
        "messages": _messages(system, user),
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": schema_json,
            },
        },
    }


def _json_only_payload(
    model_name: str, system: str, user: str, schema_json: object
) -> dict[str, object]:
    schema_text = json.dumps(schema_json, separators=(",", ":"), ensure_ascii=False)
    json_only_system = (
        f"{system}\n\nReturn only a valid JSON object that matches this schema:\n"
        f"{schema_text}"
    )
    return {
        "model": model_name,
        "messages": _messages(json_only_system, user),
    }


def _messages(system: str, user: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def _read_bounded_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ModelAdapterError(
                "model response exceeds size limit", reason_code="response_too_large"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _declared_response_is_too_large(content_length: str | None) -> bool:
    if content_length is None:
        return False
    try:
        return int(content_length) > _MAX_RESPONSE_BYTES
    except ValueError:
        return True


def _explicitly_unsupported_schema(response: _HttpResponse) -> bool:
    if response.status_code not in {400, 422}:
        return False
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    error = _error_details(payload)
    if error is None:
        return False
    message, error_type, error_code, error_param = error
    return _is_response_format_capability_rejection(
        message, error_type, error_code, error_param
    )


def _error_details(
    payload: object,
) -> tuple[str, str | None, str | None, str | None] | None:
    if type(payload) is not dict:
        return None
    error = payload.get("error")
    if type(error) is not dict:
        return None
    message = error.get("message")
    if not isinstance(message, str):
        return None
    return (
        message,
        _optional_error_field(error, "type"),
        _optional_error_field(error, "code"),
        _optional_error_field(error, "param"),
    )


def _optional_error_field(error: dict[object, object], name: str) -> str | None:
    value = error.get(name)
    return value if isinstance(value, str) else None


def _is_response_format_capability_rejection(
    message: str,
    error_type: str | None,
    error_code: str | None,
    error_param: str | None,
) -> bool:
    normalized = tuple(
        value.lower()
        for value in (message, error_type, error_code, error_param)
        if value is not None
    )
    if any(
        marker in value
        for value in normalized
        for marker in _FORBIDDEN_FALLBACK_MARKERS
    ):
        return False
    if "response_format" not in message.lower():
        return False
    if not _is_generic_or_schema_capability(error_type):
        return False
    if not _is_generic_or_schema_capability(error_code):
        return False
    return error_param is None or _is_schema_capability_discriminator(error_param)


def _is_generic_or_schema_capability(value: str | None) -> bool:
    return (
        value is None
        or value.lower() in _GENERIC_REQUEST_ERROR_TYPES
        or _is_schema_capability_discriminator(value)
    )


def _is_schema_capability_discriminator(value: str | None) -> bool:
    if value is None:
        return False
    lowered = value.lower()
    return "response_format" in lowered or "json_schema" in lowered


def _is_success(status_code: int) -> bool:
    return 200 <= status_code < 300


def _validated_result[ResultModelT: BaseModel](
    schema: type[ResultModelT], response_body: bytes
) -> ResultModelT:
    try:
        envelope: object = json.loads(response_body)
        content = _chat_completion_content(envelope)
        return schema.model_validate_json(content)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        raise ModelAdapterError(
            "invalid structured response", reason_code="structured_response_invalid"
        ) from None


def _chat_completion_content(response: object) -> str:
    if type(response) is not dict:
        raise TypeError("invalid chat completion response")
    choices = response.get("choices")
    if type(choices) is not list or not choices:
        raise ValueError("invalid chat completion response")
    first_choice = choices[0]
    if type(first_choice) is not dict:
        raise ValueError("invalid chat completion response")
    message = first_choice.get("message")
    if type(message) is not dict:
        raise ValueError("invalid chat completion response")
    content = message.get("content")
    if not isinstance(content, str):
        raise TypeError("invalid chat completion response")
    return content
