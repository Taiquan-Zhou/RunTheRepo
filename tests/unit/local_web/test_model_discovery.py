from __future__ import annotations

import httpx
import pytest

from repotrial.local_web.model_discovery import (
    ModelDiscoveryError,
    discover_models,
)


def test_discovery_requests_explicit_models_path_with_supplied_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "qwen"}, {"id": "deepseek-chat"}],
            },
        )

    models = discover_models(
        "https://model.example/v1/",
        "new-secret",
        transport=httpx.MockTransport(handler),
    )

    assert models == ("qwen", "deepseek-chat")
    assert len(requests) == 1
    assert str(requests[0].url) == "https://model.example/v1/models"
    assert requests[0].headers["authorization"] == "Bearer new-secret"
    assert requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("status", "error_code"),
    [(401, "unauthorized"), (404, "not_found"), (500, "upstream_error")],
)
def test_discovery_maps_upstream_status_without_body(
    status: int, error_code: str
) -> None:
    secret = "response-secret"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=secret.encode())

    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "request-secret",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == error_code
    assert secret not in str(raised.value)
    assert "request-secret" not in str(raised.value)


def test_discovery_rejects_redirect_without_following_or_leaking_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/models"})

    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "request-secret",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == "redirect"
    assert len(requests) == 1
    assert "evil.example" not in str(raised.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"object":"list","data":[{"name":"missing-id"}]}',
        b'{"object":"object","data":[]}',
        b'{"object":"list","data":"wrong"}',
    ],
)
def test_discovery_rejects_malformed_model_lists(body: bytes) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == "malformed"


def test_discovery_rejects_oversized_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (64 * 1024 + 1))

    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == "response_too_large"


def test_discovery_maps_timeout_without_upstream_details() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream-timeout")

    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == "timeout"
    assert "upstream-timeout" not in str(raised.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://model.example/v1?key=secret",
        "https://user:secret@model.example/v1",
        "https://[broken",
        "https://:443/v1",
    ],
)
def test_discovery_rejects_unsafe_base_url(endpoint: str) -> None:
    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            endpoint,
            "request-secret",
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )

    assert raised.value.code == "invalid_endpoint"
    assert "request-secret" not in str(raised.value)


@pytest.mark.parametrize("api_key", ["bad\rkey", "bad\nkey", "密钥", "bad\x00key"])
def test_discovery_rejects_header_unsafe_api_keys(api_key: str) -> None:
    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            api_key,
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )

    assert raised.value.code == "invalid_api_key"
    assert api_key not in str(raised.value)


def test_discovery_enforces_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from repotrial.local_web import model_discovery

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.02)
                yield b"x"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowStream())

    monkeypatch.setattr(model_discovery, "_MODEL_TIMEOUT_S", 0.05)
    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == "timeout"


def test_discovery_rejects_compressed_response_before_reading_body() -> None:
    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("compressed body must not be read")
            yield b"unreachable"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=ExplodingStream(),
        )

    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "",
            transport=httpx.MockTransport(handler),
        )

    assert raised.value.code == "malformed"


def test_discovery_rejects_deeply_nested_json() -> None:
    body = ("[" * 10000 + "]" * 10000).encode()
    with pytest.raises(ModelDiscoveryError) as raised:
        discover_models(
            "https://model.example/v1",
            "",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
        )

    assert raised.value.code == "malformed"
