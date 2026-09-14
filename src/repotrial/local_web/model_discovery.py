"""Bounded discovery for OpenAI-compatible model lists."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

_MODEL_TIMEOUT_S = 8.0
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_MODELS = 256
_MAX_MODEL_ID_CHARS = 256


class ModelDiscoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return {
            "invalid_endpoint": "model endpoint is invalid",
            "invalid_api_key": "api key is invalid",
            "redirect": "model service redirect refused",
            "unauthorized": "model service rejected the request",
            "not_found": "model service models endpoint was not found",
            "upstream_error": "model service returned an error",
            "timeout": "model service timed out",
            "response_too_large": "model service response was too large",
            "malformed": "model service returned an invalid model list",
            "transport": "model service could not be reached",
        }.get(self.code, "model discovery failed")


def discover_models(
    endpoint: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    return asyncio.run(_discover_models(endpoint, api_key, transport=transport))


async def _discover_models(
    endpoint: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[str, ...]:
    url = _models_url(endpoint)
    validate_api_key(api_key)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with asyncio.timeout(_MODEL_TIMEOUT_S):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=3.0, read=1.0, write=3.0, pool=3.0),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        raise ModelDiscoveryError("redirect")
                    if response.status_code == 401:
                        raise ModelDiscoveryError("unauthorized")
                    if response.status_code == 404:
                        raise ModelDiscoveryError("not_found")
                    if response.status_code != 200:
                        raise ModelDiscoveryError("upstream_error")
                    _check_content_encoding(response.headers.get("content-encoding"))
                    _check_content_length(response.headers.get("content-length"))
                    body = await _read_response(response.aiter_bytes(chunk_size=8192))
    except ModelDiscoveryError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise ModelDiscoveryError("timeout") from None
    except httpx.HTTPError:
        raise ModelDiscoveryError("transport") from None
    return _parse_models(body)


def validate_api_key(api_key: str | None) -> None:
    if api_key is None:
        return
    if (
        not isinstance(api_key, str)
        or len(api_key) > 4096
        or "\x00" in api_key
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in api_key)
    ):
        raise ModelDiscoveryError("invalid_api_key")


def _models_url(endpoint: str) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ModelDiscoveryError("invalid_endpoint")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ModelDiscoveryError("invalid_endpoint") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or (port is not None and not 1 <= port <= 65535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\x00" in endpoint
    ):
        raise ModelDiscoveryError("invalid_endpoint")
    return endpoint.rstrip("/") + "/models"


def _check_content_encoding(value: str | None) -> None:
    if value is not None and value.strip().lower() != "identity":
        raise ModelDiscoveryError("malformed")


def _check_content_length(value: str | None) -> None:
    if value is None:
        return
    try:
        size = int(value)
    except ValueError:
        raise ModelDiscoveryError("malformed") from None
    if size < 0 or size > _MAX_RESPONSE_BYTES:
        raise ModelDiscoveryError("response_too_large")


async def _read_response(chunks: AsyncIterator[bytes]) -> bytes:
    body = bytearray()
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ModelDiscoveryError("response_too_large")
    return bytes(body)


def _parse_models(body: bytes) -> tuple[str, ...]:
    try:
        payload: Any = json.loads(body)
    except (RecursionError, UnicodeDecodeError, ValueError):
        raise ModelDiscoveryError("malformed") from None
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "list"
        or not isinstance(payload.get("data"), list)
        or len(payload["data"]) > _MAX_MODELS
    ):
        raise ModelDiscoveryError("malformed")
    result: list[str] = []
    seen: set[str] = set()
    for item in payload["data"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or len(item["id"]) > _MAX_MODEL_ID_CHARS
        ):
            raise ModelDiscoveryError("malformed")
        model_id = item["id"]
        if model_id not in seen:
            seen.add(model_id)
            result.append(model_id)
    return tuple(result)
