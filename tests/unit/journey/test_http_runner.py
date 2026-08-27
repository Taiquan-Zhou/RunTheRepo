import asyncio
import gzip
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
from repotrial.journey.http_runner import _redacted_body_hash, run_http_journey


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

    original = step("invalid", "GET", "/x", [])
    invalid_step = mutate(original)
    result = run(journey(invalid_step), tmp_path, httpx.MockTransport(handler))

    assert result.verdict is Verdict.FAIL
    assert result.failure_reason == f"step-0000:{expected_reason}"
    assert invoked is False


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
        journey(step("origin", "GET", "/health?probe=1", [])),
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
        journey(step("target", "GET", path, [])), tmp_path, httpx.MockTransport(handler)
    )

    assert result.failure_reason == "step-0000:invalid_path"
    assert invoked is False


def test_evidence_uses_a_secret_safe_canonical_target(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedStream([b""]))

    result = run(
        journey(step("query", "GET", "/items?token=raw-secret&page=2", [])),
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
        journey(step("encoded", "GET", path, [])),
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
        journey(step("network", "GET", "/x?access%5Ftoken=alpha&mode=ok", [])),
        tmp_path,
        httpx.MockTransport(fail),
    )
    evidence = json.loads(Path(result.evidence_paths[0]).read_text(encoding="utf-8"))
    assert evidence["request"]["path"] == "/x?access%5Ftoken=<redacted>&mode=ok"


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
        journey(step("compressed", "GET", "/compressed", [])),
        tmp_path,
        httpx.MockTransport(handler),
    )

    assert result.failure_reason == "step-0000:unsupported_content_encoding"
    assert observed_headers == ["identity"]
    assert [Path(path).name for path in result.evidence_paths] == ["step-0000.json"]


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
            journey(step("redact", "GET", "/redact", [])),
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
        journey(step("collision", "GET", "/collision", [])),
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
        journey(step("path", "GET", path, [])), tmp_path, httpx.MockTransport(handler)
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


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        return None


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
    hostile = step("../../secret", "GET", "/safe", [])
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
        journey(step("timeout", "GET", "/slow", [])),
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
