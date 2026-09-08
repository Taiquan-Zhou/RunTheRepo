import asyncio
import gzip
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from repotrial.domain.enums import Verdict
from repotrial.domain.models import (
    Journey,
    JourneyAssertion,
    JourneyResult,
    JourneyStep,
)
from repotrial.journey.http_runner import (
    _canonical_json_hash,
    _redacted_body_hash,
    run_http_journey,
)


def run(
    journey: Journey,
    evidence_dir: Path,
    transport: httpx.AsyncBaseTransport,
    base_url: str = "https://fixture.test",
) -> JourneyResult:
    if isinstance(transport, httpx.MockTransport):
        fixture_handler = transport.handler

        async def raw_fixture_handler(request: httpx.Request) -> httpx.Response:
            response = fixture_handler(request)
            if inspect.isawaitable(response):
                response = await response
            if not isinstance(response.stream, ChunkedStream):
                response = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    stream=ChunkedStream([response.content]),
                    request=request,
                )
            return response

        transport = httpx.MockTransport(raw_fixture_handler)
    return asyncio.run(
        run_http_journey(
            journey,
            base_url=base_url,
            evidence_dir=evidence_dir,
            transport=transport,
        )
    )


def step(
    step_id: str,
    method: str,
    path: str,
    assertions: list[JourneyAssertion],
    json_body: object | None = None,
) -> JourneyStep:
    params: dict[str, object] = {"method": method, "path": path}
    if json_body is not None:
        params["json"] = json_body
    return JourneyStep(
        step_id=step_id,
        tool="http",
        action="request",
        params=params,
        assertions=assertions,
    )


def assertion(kind: str, target: str, expected: object) -> JourneyAssertion:
    return JourneyAssertion(kind=kind, target=target, expected=expected)


def journey(*steps: JourneyStep) -> Journey:
    return Journey(journey_id="journey", name="Journey", steps=list(steps))


def test_health_status_assertion_passes_and_writes_canonical_evidence(
    tmp_path: Path,
) -> None:
    result = run(
        journey(
            step(
                "health",
                "GET",
                "/health",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200, text="ok")),
    )

    assert result.verdict is Verdict.PASS
    assert result.passed_steps == 1
    assert result.total_steps == 1
    assert result.failure_reason is None
    evidence_path = Path(result.evidence_paths[0])
    assert evidence_path.name == "step-0000.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["request"] == {
        "body_sha256": None,
        "method": "GET",
        "path": "/health",
    }
    assert evidence["response"]["status_code"] == 200


def test_empty_journey_fails_before_network_or_evidence_side_effects(
    tmp_path: Path,
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    evidence_dir = tmp_path / "evidence"
    result = run(journey(), evidence_dir, httpx.MockTransport(handler))

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "journey:empty_steps"
    assert result.evidence_paths == []
    assert not evidence_dir.exists()
    assert invoked is False


def test_assertion_free_http_journey_fails_before_network_or_evidence_side_effects(
    tmp_path: Path,
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(500)

    evidence_dir = tmp_path / "evidence"
    result = run(
        journey(step("unchecked", "GET", "/unchecked", [])),
        evidence_dir,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "journey:missing_assertions"
    assert result.evidence_paths == []
    assert not evidence_dir.exists()
    assert invoked is False


def test_evidence_directory_initialization_failure_is_structured_and_skips_request(
    tmp_path: Path,
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    evidence_path = tmp_path / "evidence-file"
    evidence_path.write_text("not a directory", encoding="utf-8")
    result = run(
        journey(
            step(
                "health",
                "GET",
                "/health",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        evidence_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "journey:evidence_failure"
    assert result.evidence_failure_reason == "journey:evidence_initialization_failure"
    assert result.evidence_paths == []
    assert invoked is False


def test_crud_requests_support_json_path_and_text_assertions(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"item": {"id": "item-1"}})
        if request.method == "GET":
            return httpx.Response(200, text="item-1 created")
        return httpx.Response(204)

    result = run(
        journey(
            step(
                "create",
                "POST",
                "/items",
                [
                    assertion("status_code", "response.status", 201),
                    assertion("json_path_equals", "item.id", "item-1"),
                ],
                {"name": "first"},
            ),
            step(
                "read",
                "GET",
                "/items/item-1",
                [assertion("text_contains", "response.text", "created")],
            ),
            step(
                "delete",
                "DELETE",
                "/items/item-1",
                [assertion("status_code", "response.status", 204)],
            ),
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )
    assert result.verdict is Verdict.PASS
    assert result.passed_steps == 3
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/items"),
        ("GET", "/items/item-1"),
        ("DELETE", "/items/item-1"),
    ]


def test_failed_assertion_stops_later_steps_and_counts_only_fully_asserted_steps(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    result = run(
        journey(
            step(
                "first",
                "GET",
                "/first",
                [assertion("status_code", "response.status", 200)],
            ),
            step(
                "later",
                "GET",
                "/later",
                [assertion("status_code", "response.status", 200)],
            ),
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )
    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:assertion:status_code"
    assert result.passed_steps == 0
    assert result.total_steps == 2
    assert [request.url.path for request in requests] == ["/first"]
    assert len(result.evidence_paths) == 1


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (lambda value: value.model_copy(update={"tool": "shell"}), "invalid_tool"),
        (lambda value: value.model_copy(update={"action": "get"}), "invalid_action"),
        (
            lambda value: value.model_copy(
                update={"params": {"method": "PUT", "path": "/x"}}
            ),
            "invalid_method",
        ),
        (
            lambda value: value.model_copy(
                update={"params": {"method": "GET", "path": "/x", "headers": {}}}
            ),
            "invalid_params",
        ),
    ],
)
def test_invalid_execution_capabilities_fail_closed_without_transport(
    tmp_path: Path, mutate: Callable[[JourneyStep], JourneyStep], expected_reason: str
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    original = step(
        "invalid", "GET", "/x", [assertion("status_code", "response.status", 200)]
    )
    invalid_step = mutate(original)
    result = run(journey(invalid_step), tmp_path, httpx.MockTransport(handler))

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == f"step-0000:{expected_reason}"
    assert invoked is False


def test_capture_bearer_declaration_is_admitted_by_http_preflight(
    tmp_path: Path,
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200, json={"token": "abc"})

    result = run(
        journey(
            step(
                "login",
                "POST",
                "/login",
                [assertion("status_code", "response.status", 200)],
            ).model_copy(
                update={
                    "params": {
                        "method": "POST",
                        "path": "/login",
                        "auth": {"capture_bearer": "token"},
                    }
                }
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert invoked is True


def test_bearer_capture_is_reused_only_on_explicit_authenticated_steps(
    tmp_path: Path,
) -> None:
    token = "fixture.token+1/="
    requests: list[httpx.Request] = []

    def auth_params(
        method: str, path: str, *, capture: bool = False
    ) -> dict[str, object]:
        return {
            "method": method,
            "path": path,
            "auth": {"capture_bearer": "token"} if capture else {"use_bearer": True},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": token})
        if request.url.path == "/items" and request.method == "POST":
            assert request.headers.get("authorization") == f"Bearer {token}"
            return httpx.Response(201, json={"id": "item-1", "echo": token})
        if request.url.path == "/items/item-1" and request.method == "GET":
            assert request.headers.get("authorization") == f"Bearer {token}"
            return httpx.Response(200, json={"id": "item-1"})
        if request.url.path == "/items/item-1" and request.method == "DELETE":
            assert request.headers.get("authorization") == f"Bearer {token}"
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    login = step(
        "login",
        "POST",
        "/login",
        [
            assertion("status_code", "response.status", 200),
            assertion("json_path_equals", "token", token),
        ],
    ).model_copy(update={"params": auth_params("POST", "/login", capture=True)})
    create = step(
        "create",
        "POST",
        "/items",
        [assertion("status_code", "response.status", 201)],
        {"name": "first", "token": token},
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/items",
                "json": {"name": "first", "token": token},
                "auth": {"use_bearer": True},
            }
        }
    )
    read = step(
        "read",
        "GET",
        "/items/item-1",
        [assertion("json_path_equals", "id", "item-1")],
    ).model_copy(update={"params": auth_params("GET", "/items/item-1")})
    delete = step(
        "delete",
        "DELETE",
        "/items/item-1",
        [assertion("status_code", "response.status", 204)],
    ).model_copy(update={"params": auth_params("DELETE", "/items/item-1")})

    result = run(
        journey(login, create, read, delete),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/login"),
        ("POST", "/items"),
        ("GET", "/items/item-1"),
        ("DELETE", "/items/item-1"),
    ]
    evidence = "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json")
    )
    assert token not in evidence


def test_runtime_token_redaction_handles_json_unicode_escapes() -> None:
    raw_token = b'{"opaque":"alpha.token"}'
    escaped_token = b'{"opaque":"alpha\\u002etoken"}'

    assert _redacted_body_hash(raw_token, False, "alpha.token") == _redacted_body_hash(
        escaped_token, False, "alpha.token"
    )


def test_same_origin_redirect_retains_explicit_bearer_header(
    tmp_path: Path,
) -> None:
    token = "abc.token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": token})
        assert request.headers.get("authorization") == f"Bearer {token}"
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="final")

    login = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    redirect = step(
        "redirect",
        "GET",
        "/redirect",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "GET",
                "path": "/redirect",
                "auth": {"use_bearer": True},
            }
        }
    )

    result = run(journey(login, redirect), tmp_path, httpx.MockTransport(handler))

    assert result.verdict is Verdict.PASS


def test_plain_request_keeps_legacy_canonical_request_body_hash(
    tmp_path: Path,
) -> None:
    body = {"password": "plain-secret", "name": "item"}

    result = run(
        journey(
            step(
                "create",
                "POST",
                "/items",
                [assertion("status_code", "response.status", 201)],
                body,
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(201)),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["request"]["body_sha256"] == _canonical_json_hash(body)


@pytest.mark.parametrize(
    "payload",
    [{}, {"token": 123}, {"token": "bad token"}],
)
def test_invalid_captured_token_fails_before_downstream_request(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(200, json=payload)
        return httpx.Response(200)

    login = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    read = step(
        "read",
        "GET",
        "/items",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {"method": "GET", "path": "/items", "auth": {"use_bearer": True}}
        }
    )

    result = run(journey(login, read), tmp_path, httpx.MockTransport(handler))

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:auth_failure"
    assert [request.url.path for request in requests] == ["/login"]


def test_failed_capture_assertion_does_not_enable_auth(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"token": "abc"})

    login = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    read = step(
        "read",
        "GET",
        "/items",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {"method": "GET", "path": "/items", "auth": {"use_bearer": True}}
        }
    )

    result = run(journey(login, read), tmp_path, httpx.MockTransport(handler))

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:assertion:status_code"
    assert [request.url.path for request in requests] == ["/login"]


def test_use_before_capture_fails_preflight_without_network(tmp_path: Path) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    result = run(
        journey(
            step(
                "read",
                "GET",
                "/items",
                [assertion("status_code", "response.status", 200)],
            ).model_copy(
                update={
                    "params": {
                        "method": "GET",
                        "path": "/items",
                        "auth": {"use_bearer": True},
                    }
                }
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:auth_failure"
    assert invoked is False


def test_truncated_capture_fails_before_downstream_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    body = b'{"token":"abc"}' + b"x" * 70_000

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(200, stream=ChunkedStream([body]))
        return httpx.Response(200)

    login = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    read = step(
        "read",
        "GET",
        "/items",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {"method": "GET", "path": "/items", "auth": {"use_bearer": True}}
        }
    )

    result = run(journey(login, read), tmp_path, httpx.MockTransport(handler))

    assert result.failure_reason == "step-0000:auth_failure"
    assert [request.url.path for request in requests] == ["/login"]


def test_captured_token_is_not_reused_between_journey_invocations(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"token": "abc"})

    capture = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    first = run(journey(capture), tmp_path / "first", httpx.MockTransport(handler))
    use = step(
        "read",
        "GET",
        "/items",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {"method": "GET", "path": "/items", "auth": {"use_bearer": True}}
        }
    )
    second = run(journey(use), tmp_path / "second", httpx.MockTransport(handler))

    assert first.verdict is Verdict.PASS
    assert second.failure_reason == "step-0000:auth_failure"
    assert [request.url.path for request in requests] == ["/login"]


def test_captured_token_is_not_sent_on_steps_without_use_bearer(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "abc"})
        assert request.headers.get("authorization") is None
        return httpx.Response(200)

    capture = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    plain = step(
        "plain", "GET", "/plain", [assertion("status_code", "response.status", 200)]
    )

    result = run(journey(capture, plain), tmp_path, httpx.MockTransport(handler))

    assert result.verdict is Verdict.PASS


def test_cross_origin_redirect_does_not_receive_bearer_or_get_a_second_request(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "abc"})
        assert request.headers.get("authorization") == "Bearer abc"
        return httpx.Response(302, headers={"location": "https://attacker.test/final"})

    capture = step(
        "login",
        "POST",
        "/login",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "POST",
                "path": "/login",
                "auth": {"capture_bearer": "token"},
            }
        }
    )
    redirect = step(
        "redirect",
        "GET",
        "/redirect",
        [assertion("status_code", "response.status", 200)],
    ).model_copy(
        update={
            "params": {
                "method": "GET",
                "path": "/redirect",
                "auth": {"use_bearer": True},
            }
        }
    )

    result = run(journey(capture, redirect), tmp_path, httpx.MockTransport(handler))

    assert result.failure_reason == "step-0001:redirect:origin_change"
    assert [request.url.path for request in requests] == ["/login", "/redirect"]


def test_preflight_rejects_a_later_invalid_assertion_before_an_earlier_post(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    result = run(
        journey(
            step("create", "POST", "/items", [], {"name": "must-not-send"}),
            step(
                "invalid-later",
                "GET",
                "/items",
                [assertion("xpath", "response.body", "unsupported")],
            ),
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0001:invalid_assertion"
    assert requests == []
    assert result.evidence_paths == []


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:password@fixture.test",
        "https://fixture.test/api",
        "https://fixture.test?query=1",
        "https://fixture.test\\@attacker.test",
        "https://fixture.test%5c@attacker.test",
        "https://[::1",
    ],
)
def test_invalid_trusted_origins_fail_before_transport(
    tmp_path: Path, base_url: str
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    result = run(
        journey(step("origin", "GET", "/health", [])),
        tmp_path,
        httpx.MockTransport(handler),
        base_url,
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "journey:invalid_base_url"
    assert invoked is False


def test_request_url_preserves_the_frozen_origin_scheme_host_and_effective_port(
    tmp_path: Path,
) -> None:
    observed: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url)
        return httpx.Response(200)

    result = run(
        journey(
            step(
                "origin",
                "GET",
                "/health?probe=1",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
        "https://fixture.test:443",
    )

    assert result.verdict is Verdict.PASS
    assert [
        (url.scheme, url.host, url.port or 443, url.raw_path) for url in observed
    ] == [("https", "fixture.test", 443, b"/health?probe=1")]


@pytest.mark.parametrize(
    "path", ["/safe#fragment", "/a/../b", "/%2e%2e/private", "/bad\\path"]
)
def test_ambiguous_relative_targets_fail_before_transport(
    tmp_path: Path, path: str
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    result = run(
        journey(
            step(
                "target",
                "GET",
                path,
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:invalid_path"
    assert invoked is False


def test_evidence_uses_a_secret_safe_canonical_target(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedStream([b""]))

    result = run(
        journey(
            step(
                "query",
                "GET",
                "/items?token=raw-secret&page=2",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["request"]["path"] == "/items?token=<redacted>&page=2"


@pytest.mark.parametrize(
    "path", ["/safe%0Aevil", "/a%2fb", "/%252e%252e/private", "/bad%ZZ"]
)
def test_encoded_target_ambiguities_fail_before_transport(
    tmp_path: Path, path: str
) -> None:
    result = run(
        journey(
            step(
                "encoded",
                "GET",
                path,
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    assert result.failure_reason == "step-0000:invalid_path"


def test_query_credential_keys_are_decoded_and_redacted_in_network_evidence(
    tmp_path: Path,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("x", request=request)

    result = run(
        journey(
            step(
                "network",
                "GET",
                "/x?access%5Ftoken=alpha&mode=ok",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(fail),
    )
    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["request"]["path"] == "/x?access%5Ftoken=<redacted>&mode=ok"


def test_credential_key_grammar_uses_only_terminal_credential_semantics() -> None:
    alpha = b"password=alpha secret=beta db_password=gamma access_token=delta client_secret=epsilon api_key=zeta apikey=eta private_key=theta ssh_private_key=iota\n"
    beta = b"password=one secret=two db_password=three access_token=four client_secret=five api_key=six apikey=seven private_key=eight ssh_private_key=nine\n"

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(b"token_type=bearer\n", False) != _redacted_body_hash(
        b"token_type=mac\n", False
    )
    assert _redacted_body_hash(b"password_policy=long\n", False) != _redacted_body_hash(
        b"password_policy=short\n", False
    )


def test_valid_json_body_redaction_recurses_through_nested_values() -> None:
    alpha = json.dumps(
        {
            "items": [
                {"client_secret": "alpha", "status": "ready"},
                {"nested": {"apikey": "beta"}},
            ]
        }
    ).encode()
    beta = json.dumps(
        {
            "items": [
                {"client_secret": "gamma", "status": "ready"},
                {"nested": {"apikey": "delta"}},
            ]
        }
    ).encode()
    changed_field = json.dumps(
        {
            "items": [
                {"client_secret": "gamma", "status": "changed"},
                {"nested": {"apikey": "delta"}},
            ]
        }
    ).encode()

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(changed_field, False)


def test_runner_writes_evidence_for_deep_legal_json_without_escaping(
    tmp_path: Path,
) -> None:
    body = b"[" * 1_100 + b"0" + b"]" * 1_100
    result = run(
        journey(
            step(
                "deep-json",
                "GET",
                "/deep",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=ChunkedStream([body]))
        ),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert result.verdict is Verdict.PASS
    assert evidence["response"]["body_sha256"]


def test_runner_writes_evidence_for_legal_long_json_integer_without_escaping(
    tmp_path: Path,
) -> None:
    body = b'{"value":' + b"9" * 5_000 + b"}"
    result = run(
        journey(
            step(
                "long-json",
                "GET",
                "/long",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=ChunkedStream([body]))
        ),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert result.verdict is Verdict.PASS
    assert evidence["response"]["body_sha256"]


def test_malformed_json_fallback_redacts_quoted_credentials_and_keeps_fields() -> None:
    assert _redacted_body_hash(
        b'{"password":"alpha","next":', False
    ) == _redacted_body_hash(b'{"password":"beta","next":', False)

    alpha = b'{"password":"alpha","status":"ready","next":'
    beta = b'{"password":"beta","status":"ready","next":'
    changed_field = b'{"password":"beta","status":"changed","next":'

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(changed_field, False)


def test_truncated_structured_response_redacts_credential_substitutions(
    tmp_path: Path,
) -> None:
    def body(secret: str) -> bytes:
        return (
            f'{{"password":"{secret}","status":"ready","payload":"'.encode()
            + b"x" * 70_000
        )

    hashes: list[str] = []
    for secret in ("alpha", "bravo"):
        result = run(
            journey(
                step(
                    "truncated-json",
                    "GET",
                    "/truncated",
                    [assertion("status_code", "response.status", 200)],
                )
            ),
            tmp_path / secret,
            httpx.MockTransport(
                lambda request, value=secret: httpx.Response(
                    200, stream=ChunkedStream([body(value)])
                )
            ),
        )
        evidence = json.loads(
            Path(result.evidence_paths[0]).read_text(encoding="utf-8")
        )
        assert evidence["response"]["truncated"] is True
        hashes.append(evidence["response"]["body_sha256"])

    assert hashes[0] == hashes[1]


def test_malformed_embedded_list_object_redacts_quoted_credentials() -> None:
    alpha = b'prefix {"items":[{"client_secret":"alpha","status":"ready"}],"next":'
    beta = b'prefix {"items":[{"client_secret":"beta","status":"ready"}],"next":'
    changed_field = (
        b'prefix {"items":[{"client_secret":"beta","status":"changed"}],"next":'
    )

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(changed_field, False)


def test_yaml_sequence_credential_block_preserves_same_mapping_sibling() -> None:
    alpha = b"- password: |\n    alpha\n    omega\n  status: ready\n"
    beta = b"- password: |\n    beta\n  status: ready\n"
    changed_sibling = b"- password: |\n    beta\n  status: changed\n"

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(
        changed_sibling, False
    )


def test_plain_text_multiple_assignments_redact_each_credential_independently() -> None:
    alpha = b"password=alpha token=beta status=ok\n"
    beta = b"password=gamma token=delta status=ok\n"
    changed_status = b"password=gamma token=delta status=changed\n"

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(
        changed_status, False
    )


def test_quoted_credential_value_keeps_assignment_like_whitespace_text_secret() -> None:
    alpha = b'password="alpha status=one" status=ready\n'
    beta = b'password="beta status=two" status=ready\n'
    changed_sibling = b'password="beta status=two" status=changed\n'

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(
        changed_sibling, False
    )


def test_quoted_credential_value_keeps_assignment_like_comma_text_secret() -> None:
    alpha = b'password="alpha, status=one" status=ready\n'
    beta = b'password="beta, status=two" status=ready\n'
    changed_sibling = b'password="beta, status=two" status=changed\n'

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(
        changed_sibling, False
    )


def test_quoted_credential_value_consumes_escaped_quotes() -> None:
    alpha = b'password="alpha\\" status=one" status=ready\n'
    beta = b'password="bravo\\" status=two" status=ready\n'
    changed_sibling = b'password="bravo\\" status=two" status=changed\n'

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(
        changed_sibling, False
    )


def test_truncated_quoted_credential_consumes_assignment_like_content(
    tmp_path: Path,
) -> None:
    def body(secret: str, status: str) -> bytes:
        return (
            f'password="{secret} status={status}, status:two '.encode() + b"x" * 70_000
        )

    hashes: list[str] = []
    for secret, status in (("alpha", "one"), ("bravo", "two")):
        result = run(
            journey(
                step(
                    "truncated-quoted",
                    "GET",
                    "/truncated-quoted",
                    [assertion("status_code", "response.status", 200)],
                )
            ),
            tmp_path / status,
            httpx.MockTransport(
                lambda request, value=secret, inner_status=status: httpx.Response(
                    200, stream=ChunkedStream([body(value, inner_status)])
                )
            ),
        )
        evidence = json.loads(
            Path(result.evidence_paths[0]).read_text(encoding="utf-8")
        )
        assert evidence["response"]["truncated"] is True
        hashes.append(evidence["response"]["body_sha256"])

    assert hashes[0] == hashes[1]


def test_network_evidence_redacts_apikey_and_preserves_noncredential_query_values(
    tmp_path: Path,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("x", request=request)

    result = run(
        journey(
            step(
                "network",
                "GET",
                "/x?apikey=alpha&client%5Fsecret=beta&token_type=bearer&password_policy=long",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(fail),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["request"]["path"] == (
        "/x?apikey=<redacted>&client%5Fsecret=<redacted>&token_type=bearer&password_policy=long"
    )


def test_body_redaction_uses_credential_key_grammar_for_nested_yaml() -> None:
    assert _redacted_body_hash(
        b"  - SSH_PRIVATE_KEY: |\n      alpha\nnext: keep\n", False
    ) == _redacted_body_hash(b"  - SSH_PRIVATE_KEY: |\n      beta\nnext: keep\n", False)


def test_compressed_response_fails_closed_and_requests_identity_encoding(
    tmp_path: Path,
) -> None:
    observed_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_headers.append(request.headers.get("accept-encoding"))
        return httpx.Response(
            200,
            content=gzip.compress(b"needle" * 20_000),
            headers={"content-encoding": "gzip"},
        )

    result = run(
        journey(
            step(
                "compressed",
                "GET",
                "/compressed",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:unsupported_content_encoding"
    assert observed_headers == ["identity"]
    assert [Path(path).name for path in result.evidence_paths] == ["step-0000.json"]


def test_body_redaction_uses_credential_key_grammar_for_assignments() -> None:
    alpha = b"db_password = alpha beta\nclient_secret:\n  one\n  two\nnext: ordinary\n"
    beta = b"db_password = gamma delta\nclient_secret:\n  three\n  four\n  five\nnext: ordinary\n"
    changed_sibling = b"db_password = gamma delta\nclient_secret:\n  three\n  four\n  five\nnext: changed\n"

    assert _redacted_body_hash(alpha, False) == _redacted_body_hash(beta, False)
    assert _redacted_body_hash(beta, False) != _redacted_body_hash(
        changed_sibling, False
    )


def test_redacted_hash_consumes_truncated_pem_and_multiline_credentials(
    tmp_path: Path,
) -> None:
    def body(secret: str) -> bytes:
        prefix = (
            f"password =\n  {secret}\n-----BEGIN PRIVATE KEY-----\n{secret}\n".encode()
        )
        return prefix + b"x" * (65_536 - len(prefix))

    hashes: list[str] = []
    for secret in ("first-secret", "second-secret"):

        async def handler(
            request: httpx.Request, value: str = secret
        ) -> httpx.Response:
            return httpx.Response(200, stream=ChunkedStream([body(value)]))

        result = run(
            journey(
                step(
                    "redact",
                    "GET",
                    "/redact",
                    [assertion("status_code", "response.status", 200)],
                )
            ),
            tmp_path / secret,
            httpx.MockTransport(handler),
        )
        evidence = json.loads(
            Path(result.evidence_paths[0]).read_text(encoding="utf-8")
        )
        hashes.append(evidence["response"]["body_sha256"])
    assert hashes[0] == hashes[1]


def test_redacted_hash_consumes_yaml_literal_and_folded_credential_blocks() -> None:
    literal_alpha = b"password: |\n  alpha\n  omega\nnext: ordinary\n"
    literal_beta = b"password: |\n  beta\n  omega\nnext: ordinary\n"
    folded_alpha = b"token: >\n  alpha\n  omega\nnext: ordinary\n"
    folded_beta = b"token: >\n  beta\n  omega\nnext: ordinary\n"

    assert _redacted_body_hash(literal_alpha, False) == _redacted_body_hash(
        literal_beta, False
    )
    assert _redacted_body_hash(folded_alpha, False) == _redacted_body_hash(
        folded_beta, False
    )
    assert _redacted_body_hash(literal_alpha, False) != _redacted_body_hash(
        b"password: |\n  alpha\n  omega\nnext: changed\n", False
    )


def test_evidence_collision_fails_without_replacing_existing_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "step-0000.json"
    artifact.write_text("sentinel", encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedStream([b""]))

    result = run(
        journey(
            step(
                "collision",
                "GET",
                "/collision",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:evidence_write_error"
    assert artifact.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize(
    "path", ["https://attacker.test/x", "//attacker.test/x", "items"]
)
def test_non_origin_relative_paths_fail_before_transport(
    tmp_path: Path, path: str
) -> None:
    invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        invoked = True
        return httpx.Response(200)

    result = run(
        journey(
            step(
                "path",
                "GET",
                path,
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:invalid_path"
    assert invoked is False


@pytest.mark.parametrize(
    ("response", "target", "expected", "reason"),
    [
        (httpx.Response(200, text="not json"), "value", "x", "malformed_json"),
        (
            httpx.Response(200, json={"value": {}}),
            "value.missing",
            "x",
            "json_path_missing",
        ),
    ],
)
def test_json_path_errors_are_stable_failures(
    tmp_path: Path, response: httpx.Response, target: str, expected: object, reason: str
) -> None:
    result = run(
        journey(
            step(
                "json",
                "GET",
                "/json",
                [assertion("json_path_equals", target, expected)],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: response),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == f"step-0000:{reason}"


def test_json_integer_exceeding_conversion_limit_is_malformed_json(
    tmp_path: Path,
) -> None:
    body = b'{"value":' + b"9" * 5_000 + b"}"
    result = run(
        journey(
            step(
                "bounded-integer",
                "GET",
                "/value",
                [assertion("json_path_equals", "value", 0)],
            )
        ),
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=ChunkedStream([body]))
        ),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:malformed_json"


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (True, 1),
        ({"enabled": True}, {"enabled": 1}),
        ([True, {"count": 1}], [1, {"count": True}]),
    ],
)
def test_json_path_equals_preserves_json_types_recursively(
    tmp_path: Path, actual: object, expected: object
) -> None:
    result = run(
        journey(
            step(
                "typed-json",
                "GET",
                "/typed-json",
                [assertion("json_path_equals", "value", expected)],
            )
        ),
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"value": actual})
        ),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:assertion:json_path_equals"


@pytest.mark.parametrize(
    ("kind", "target", "expected", "reason"),
    [
        ("xpath", "response.body", "x", "invalid_assertion"),
        ("json_path_equals", "items[*]", "x", "invalid_json_path"),
    ],
)
def test_unsupported_or_malformed_assertions_fail_closed(
    tmp_path: Path, kind: str, target: str, expected: object, reason: str
) -> None:
    result = run(
        journey(step("assertion", "GET", "/x", [assertion(kind, target, expected)])),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200, json={"items": ["x"]})),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == f"step-0000:{reason}"


def test_redirect_response_is_not_followed_even_cross_origin(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "https://attacker.test/exfiltrate"}
        )

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 302)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert [str(request.url) for request in requests] == [
        "https://fixture.test/redirect"
    ]


def test_valid_relative_same_origin_redirect_is_followed_for_final_assertions(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [
                    assertion("status_code", "response.status", 200),
                    assertion("text_contains", "response.text", "final"),
                ],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert [str(request.url) for request in requests] == [
        "https://fixture.test/redirect",
        "https://fixture.test/final",
    ]


def test_followed_redirect_adds_only_a_bounded_target_hash_to_evidence(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final?token=secret"})
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["redirects"] == [
        {
            "status_code": 302,
            "target_sha256": hashlib.sha256(b"/final?token=<redacted>").hexdigest(),
        }
    ]
    assert "secret" not in json.dumps(evidence)
    assert "location" not in json.dumps(evidence).lower()


def test_redirect_location_with_space_is_rejected_before_url_normalization(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/has space"})
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:redirect:invalid_location"
    assert [str(request.url) for request in requests] == [
        "https://fixture.test/redirect"
    ]


def test_redirect_location_with_non_ascii_raw_byte_is_rejected_before_url_normalization(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers=[(b"location", b"/x\x85y")],
            )
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:redirect:invalid_location"
    assert [str(request.url) for request in requests] == [
        "https://fixture.test/redirect"
    ]


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        (None, "missing_location"),
        ("duplicate", "duplicate_location"),
        ("https://fixture.test:444/final", "origin_change"),
        ("http://fixture.test/final", "origin_change"),
        ("https://user:password@fixture.test/final", "credential_location"),
        ("/final#fragment", "fragment"),
        ("/final#", "fragment"),
        ("//fixture.test/final", "network_path"),
        ("https://attacker.test/final", "origin_change"),
        ("", "malformed_location"),
        ("/bad%0apath", "invalid_target"),
        ("/final?bad=%ZZ", "invalid_target"),
        ("/a/../final", "invalid_target"),
        ("/%252e%252e/final", "invalid_target"),
        ("/a%2fb", "invalid_target"),
        ("/bad%ZZ", "invalid_target"),
        ("/bad%5cpath", "invalid_location"),
        ("/bad\\path", "invalid_location"),
    ],
)
def test_unsafe_redirect_locations_fail_closed_without_following(
    tmp_path: Path, location: str | None, reason: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers: list[tuple[str, str]] = []
        if location is None:
            return httpx.Response(302, headers=headers)
        if location == "duplicate":
            headers = [("location", "/one"), ("location", "/two")]
        else:
            headers = [("location", location)]
        return httpx.Response(302, headers=headers)

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == f"step-0000:redirect:{reason}"
    assert [str(request.url) for request in requests] == [
        "https://fixture.test/redirect"
    ]


def test_redirect_cycle_is_rejected_without_a_fourth_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "/redirect"})

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:redirect:cycle"
    assert len(requests) == 1


def test_redirect_target_over_2048_characters_is_rejected(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    location = "/" + "x" * 2_048

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:redirect:invalid_target"
    assert len(requests) == 1


def test_redirects_are_limited_to_three_hops(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    targets = {"/one": "/two", "/two": "/three", "/three": "/four", "/four": "/five"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": targets[request.url.path]})

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/one",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:redirect:max_hops"
    assert [request.url.path for request in requests] == [
        "/one",
        "/two",
        "/three",
        "/four",
    ]


def test_explicit_redirect_status_assertion_does_not_follow_same_origin_location(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, text="redirect", headers={"location": "/final"})

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [
                    assertion("status_code", "response.status", 302),
                    assertion("text_contains", "response.text", "redirect"),
                ],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
def test_all_supported_get_redirect_statuses_are_followed(
    tmp_path: Path, status_code: int
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(status_code, headers={"location": "/final"})
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert [request.url.path for request in requests] == ["/redirect", "/final"]


def test_unsupported_redirect_status_is_evaluated_without_following(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(300, headers={"location": "/final"})

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 300)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert len(requests) == 1


def test_post_redirect_is_not_followed_and_keeps_original_body(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, text="redirect", headers={"location": "/final"})

    result = run(
        journey(
            step(
                "redirect",
                "POST",
                "/redirect",
                [assertion("status_code", "response.status", 302)],
                {"name": "original"},
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {"name": "original"}


def test_followed_get_redirect_drops_cookie_authorization_and_body(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={
                    "location": "/final",
                    "set-cookie": "session=secret",
                },
            )
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
                {"name": "original"},
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert len(requests) == 2
    assert requests[0].content
    assert requests[1].content == b""
    assert requests[1].headers.get("cookie") is None
    assert requests[1].headers.get("authorization") is None


def test_intermediate_redirect_body_is_not_read_and_stream_is_closed(
    tmp_path: Path,
) -> None:
    intermediate = ChunkedStream([b"must-not-be-read"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"location": "/final"},
                stream=intermediate,
            )
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.PASS
    assert intermediate.yielded == 0
    assert intermediate.closed is True


def test_redirect_chain_uses_one_step_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.journey.http_runner as http_runner_module

    monkeypatch.setattr(http_runner_module, "_REQUEST_TIMEOUT_SECONDS", 0.05)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0.04)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="final")

    result = run(
        journey(
            step(
                "redirect",
                "GET",
                "/redirect",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:network_error"
    assert len(requests) == 2


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_response_body_is_capped_during_streaming_before_extra_bytes_can_match(
    tmp_path: Path,
) -> None:
    stream = ChunkedStream([b"a" * 65_536, b" secret-needle", b"unread"])
    result = run(
        journey(
            step(
                "large",
                "GET",
                "/large",
                [assertion("text_contains", "response.text", "secret-needle")],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:assertion:text_contains"
    assert stream.yielded == 2
    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["response"]["truncated"] is True


def test_evidence_paths_are_safe_and_deterministic_for_hostile_ids(
    tmp_path: Path,
) -> None:
    hostile = step(
        "../../secret",
        "GET",
        "/safe",
        [assertion("status_code", "response.status", 200)],
    )
    result = run(
        journey(hostile),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    assert [Path(path).name for path in result.evidence_paths] == ["step-0000.json"]
    assert list(tmp_path.iterdir()) == [tmp_path / "step-0000.json"]


def test_evidence_redacts_and_hashes_request_and_response_secrets(
    tmp_path: Path,
) -> None:
    secret = "top-secret-token"
    pem = "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
    response_text = f"password={secret} Authorization: Bearer {secret}\n{pem}"
    result = run(
        journey(
            step(
                "secrets",
                "POST",
                "/secrets",
                [assertion("status_code", "response.status", 200)],
                {"password": secret},
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200, text=response_text)),
    )

    artifact = Path(result.evidence_paths[0]).read_text(encoding="utf-8")
    evidence = json.loads(artifact)
    assert secret not in artifact
    assert "private-material" not in artifact
    assert evidence["request"]["body_sha256"]
    assert evidence["response"]["body_sha256"]


def test_explicit_json_null_has_a_canonical_request_hash(tmp_path: Path) -> None:
    result = run(
        journey(
            JourneyStep(
                step_id="null-body",
                tool="http",
                action="request",
                params={"method": "POST", "path": "/null", "json": None},
                assertions=[assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert (
        evidence["request"]["body_sha256"]
        == "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    )


def test_too_many_assertions_fail_before_writing_unbounded_evidence(
    tmp_path: Path,
) -> None:
    result = run(
        journey(
            step(
                "many-assertions",
                "GET",
                "/many",
                [assertion("status_code", "response.status", 200) for _ in range(65)],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:invalid_assertions"


def test_network_timeout_returns_failure_without_retry(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    result = run(
        journey(
            step(
                "timeout",
                "GET",
                "/slow",
                [assertion("status_code", "response.status", 200)],
            )
        ),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == "step-0000:network_error"
    assert attempts == 1


def test_failure_token_does_not_include_an_unbounded_or_secret_step_id(
    tmp_path: Path,
) -> None:
    result = run(
        journey(
            step(
                "step-id-with-a-secret-value-and-unbounded-context",
                "GET",
                "/failure",
                [assertion("status_code", "response.status", 201)],
            )
        ),
        tmp_path,
        httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    assert result.failure_reason == "step-0000:assertion:status_code"


def test_input_journey_is_not_mutated(tmp_path: Path) -> None:
    original = journey(step("immutable", "POST", "/items", [], {"name": "unchanged"}))
    before = original.model_dump(mode="json")

    run(original, tmp_path, httpx.MockTransport(lambda request: httpx.Response(200)))

    assert original.model_dump(mode="json") == before
