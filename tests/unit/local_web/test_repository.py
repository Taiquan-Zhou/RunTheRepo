from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from repotrial.local_web.app import create_app
from repotrial.local_web.git_metadata import GitMetadata
from repotrial.local_web.repository import (
    GithubRepositoryDiscovery,
    GithubResponse,
    RepositoryInput,
    RepositoryMetadata,
    discover_repository,
)

SHA = "a" * 40


class FakeFetcher:
    def __init__(self, responses: Mapping[str, bytes | tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: Mapping[str, str]) -> GithubResponse:
        self.calls.append((url, dict(params)))
        result = self.responses[url]
        if isinstance(result, tuple):
            status, body = result
        else:
            status, body = 200, result
        return GithubResponse(status_code=status, headers={}, body=body)


def _json(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _tree(*paths: tuple[str, str]) -> bytes:
    return _json(
        {
            "truncated": False,
            "tree": [
                {"path": path, "mode": "100644", "type": "blob", "sha": blob}
                for path, blob in paths
            ],
        }
    )


def test_manual_sha_discovers_selected_compose_and_container_ports() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_url = "https://api.github.com/repos/acme/demo/git/blobs/cccccccccccccccccccccccccccccccccccccccc"
    fetcher = FakeFetcher(
        {
            tree_url: _tree(
                ("README.md", "dddddddddddddddddddddddddddddddddddddddd"),
                ("deploy/compose.yml", "cccccccccccccccccccccccccccccccccccccccc"),
            ),
            blob_url: _json(
                {
                    "encoding": "base64",
                    "content": "c2VydmljZXM6CiAgd2ViOgogICAgcG9ydHM6CiAgICAgIC0gIjgwODA6MzAwMCIK",
                }
            ),
        }
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=fetcher,
    )

    assert result.commit_sha == SHA
    assert result.commit_url == f"https://github.com/acme/demo/commit/{SHA}"
    assert result.compose_candidates == ("deploy/compose.yml",)
    assert result.selected_compose_path == "deploy/compose.yml"
    assert result.ports == ({"service": "web", "port": 3000},)
    assert result.warnings == ()
    assert result.errors == ()


def test_latest_commit_is_resolved_from_public_commits_endpoint() -> None:
    latest = "b" * 40
    commits_url = "https://api.github.com/repos/acme/demo/commits"
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{latest}"
    fetcher = FakeFetcher(
        {
            commits_url: _json([{"sha": latest}]),
            tree_url: _tree(),
        }
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"),
        fetcher=fetcher,
    )

    assert result.commit_sha == latest
    assert result.commit_url == f"https://github.com/acme/demo/commit/{latest}"
    assert fetcher.calls[0][1] == {"per_page": "1"}
    assert "compose_not_found" in result.errors


def test_discovery_reuses_successful_tree_and_blob_responses() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "c" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"
    fetcher = FakeFetcher(
        {
            tree_url: _tree(("compose.yml", blob_sha)),
            blob_url: _blob_response("services:\n  web:\n    expose: [3000]\n"),
        }
    )
    discovery = GithubRepositoryDiscovery(fetcher)
    payload = RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA)

    assert discovery.discover(payload).errors == ()
    assert discovery.discover(payload).errors == ()
    assert [url for url, _ in fetcher.calls] == [tree_url, blob_url]


def test_latest_cache_expires_while_pinned_tree_cache_remains(monkeypatch) -> None:
    latest = "b" * 40
    commits_url = "https://api.github.com/repos/acme/demo/commits"
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{latest}"
    fetcher = FakeFetcher({commits_url: _json([{"sha": latest}]), tree_url: _tree()})
    now = [100.0]
    monkeypatch.setattr("repotrial.local_web.repository.time.monotonic", lambda: now[0])
    discovery = GithubRepositoryDiscovery(fetcher)
    payload = RepositoryInput(url="https://github.com/acme/demo")

    discovery.discover(payload)
    now[0] += 6
    discovery.discover(payload)

    assert [url for url, _ in fetcher.calls].count(commits_url) == 2
    assert [url for url, _ in fetcher.calls].count(tree_url) == 1


def test_pinned_cache_is_keyed_by_endpoint_and_parameters() -> None:
    other_sha = "b" * 40
    first_tree = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    second_tree = f"https://api.github.com/repos/acme/demo/git/trees/{other_sha}"
    fetcher = FakeFetcher({first_tree: _tree(), second_tree: _tree()})
    discovery = GithubRepositoryDiscovery(fetcher)

    discovery.discover(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA)
    )
    discovery.discover(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=other_sha)
    )

    assert [url for url, _ in fetcher.calls] == [first_tree, second_tree]


def test_concurrent_discovery_calls_share_one_inflight_request() -> None:
    class BlockingFetcher:
        def __init__(self) -> None:
            self.calls = 0
            self.started = Event()
            self.release = Event()

        def __call__(self, url: str, params: Mapping[str, str]) -> GithubResponse:
            self.calls += 1
            self.started.set()
            assert self.release.wait(2)
            return GithubResponse(200, {}, _tree())

    fetcher = BlockingFetcher()
    discovery = GithubRepositoryDiscovery(fetcher)
    payload = RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA)
    results: list[RepositoryMetadata] = []
    threads = [
        Thread(target=lambda: results.append(discovery.discover(payload)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert fetcher.started.wait(2)
    fetcher.release.set()
    for thread in threads:
        thread.join(2)

    assert fetcher.calls == 1
    assert len(results) == 2
    assert all(result.errors == ("compose_not_found",) for result in results)


def test_rate_limit_falls_back_to_git_metadata() -> None:
    class FakeGit:
        def discover(
            self, owner: str, repository: str, **kwargs: object
        ) -> GitMetadata:
            assert owner == "acme"
            assert repository == "demo"
            return GitMetadata(
                SHA,
                ("compose.yml",),
                "compose.yml",
                b"services: {web: {expose: [3000]}}",
            )

    fetcher = HeaderFetcher(GithubResponse(403, {"X-RateLimit-Remaining": "0"}, b"{}"))
    result = GithubRepositoryDiscovery(fetcher, git_client=FakeGit()).discover(
        RepositoryInput(url="https://github.com/acme/demo")
    )

    assert result.commit_sha == SHA
    assert result.ports == ({"service": "web", "port": 3000},)
    assert "git_metadata_fallback" in result.warnings
    assert result.errors == ()
    assert result.retry_after_seconds is None


def test_plain_forbidden_is_not_reported_as_rate_limited() -> None:
    commits_url = "https://api.github.com/repos/acme/demo/commits"
    fetcher = FakeFetcher({commits_url: (403, b'{"message":"private"}')})

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"), fetcher=fetcher
    )

    assert result.errors == ("github_forbidden",)
    assert result.retry_after_seconds is None


def test_forbidden_with_remaining_quota_is_not_rate_limited() -> None:
    fetcher = HeaderFetcher(
        GithubResponse(
            403,
            {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "4102444800"},
            b"{}",
        )
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"), fetcher=fetcher
    )

    assert result.errors == ("github_forbidden",)


def test_rate_limit_response_sets_cooldown_and_retry_after() -> None:
    commits_url = "https://api.github.com/repos/acme/demo/commits"
    fetcher = HeaderFetcher(
        GithubResponse(403, {"X-RateLimit-Remaining": "0", "Retry-After": "9"}, b"{}")
    )
    discovery = GithubRepositoryDiscovery(fetcher)
    payload = RepositoryInput(url="https://github.com/acme/demo")

    first = discovery.discover(payload)
    second = discovery.discover(payload)

    assert first.errors == ("github_rate_limited",)
    assert first.retry_after_seconds is not None
    assert 8 <= first.retry_after_seconds <= 9
    assert second.errors == ("github_rate_limited",)
    assert second.retry_after_seconds is not None
    assert fetcher.calls == 1
    assert commits_url == fetcher.url


def test_remaining_zero_without_retry_header_uses_default_cooldown() -> None:
    fetcher = HeaderFetcher(GithubResponse(403, {"x-ratelimit-remaining": "0"}, b"{}"))

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"), fetcher=fetcher
    )

    assert result.errors == ("github_rate_limited",)
    assert result.retry_after_seconds == 60.0


def test_malformed_rate_limit_headers_use_bounded_default_cooldown() -> None:
    fetcher = HeaderFetcher(
        GithubResponse(
            429,
            {
                "Retry-After": "nan",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "inf",
            },
            b"{}",
        )
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"), fetcher=fetcher
    )

    assert result.errors == ("github_rate_limited",)
    assert result.retry_after_seconds == 60.0


def test_rate_limit_reset_header_extends_429_cooldown(monkeypatch) -> None:
    monkeypatch.setattr("repotrial.local_web.repository.time.time", lambda: 100.0)
    fetcher = HeaderFetcher(
        GithubResponse(
            429,
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "700"},
            b"{}",
        )
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"), fetcher=fetcher
    )

    assert result.errors == ("github_rate_limited",)
    assert result.retry_after_seconds == 600.0


def test_cached_pinned_data_is_available_during_rate_limit_cooldown() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    commits_url = "https://api.github.com/repos/acme/demo/commits"

    class CooldownFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, url: str, params: Mapping[str, str]) -> GithubResponse:
            self.calls.append(url)
            if url == tree_url:
                return GithubResponse(200, {}, _tree())
            return GithubResponse(429, {"Retry-After": "30"}, b"{}")

    fetcher = CooldownFetcher()
    discovery = GithubRepositoryDiscovery(fetcher)
    pinned = RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA)

    assert discovery.discover(pinned).errors == ("compose_not_found",)
    assert discovery.discover(
        RepositoryInput(url="https://github.com/acme/demo")
    ).errors == ("github_rate_limited",)
    assert discovery.discover(pinned).errors == ("compose_not_found",)

    assert fetcher.calls == [tree_url, commits_url]


def test_timeout_is_reported_as_github_timeout() -> None:
    def timeout_fetcher(url: str, params: Mapping[str, str]) -> GithubResponse:
        raise TimeoutError

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"), fetcher=timeout_fetcher
    )

    assert result.errors == ("github_timeout",)


def test_unavailable_without_git_client_keeps_original_failure_code() -> None:
    def unavailable_fetcher(url: str, params: Mapping[str, str]) -> GithubResponse:
        raise OSError("network unavailable")

    result = GithubRepositoryDiscovery(unavailable_fetcher).discover(
        RepositoryInput(url="https://github.com/acme/demo")
    )

    assert result.errors == ("github_unavailable",)


class HeaderFetcher:
    def __init__(self, response: GithubResponse) -> None:
        self.response = response
        self.calls = 0
        self.url = ""

    def __call__(self, url: str, params: Mapping[str, str]) -> GithubResponse:
        self.calls += 1
        self.url = url
        return self.response


def test_multiple_compose_files_and_ports_require_explicit_selection() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    one_url = "https://api.github.com/repos/acme/demo/git/blobs/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    two_url = "https://api.github.com/repos/acme/demo/git/blobs/ffffffffffffffffffffffffffffffffffffffff"
    fetcher = FakeFetcher(
        {
            tree_url: _tree(
                ("compose.yml", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"),
                (
                    "deploy/docker-compose.yaml",
                    "ffffffffffffffffffffffffffffffffffffffff",
                ),
            ),
            one_url: _json(
                {
                    "encoding": "base64",
                    "content": "c2VydmljZXM6IHt3ZWI6IHtwb3J0czogWyIzMDAwIiwgIjgwODA6ODA4MCJdfX0=",
                }
            ),
            two_url: _json(
                {
                    "encoding": "base64",
                    "content": "c2VydmljZXM6IHt3ZWI6IHtwb3J0czogWyIzMDAwIl19fQ==",
                }
            ),
        }
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=fetcher,
    )

    assert result.compose_candidates == ("compose.yml", "deploy/docker-compose.yaml")
    assert result.selected_compose_path == "compose.yml"
    assert result.ports == (
        {"service": "web", "port": 3000},
        {"service": "web", "port": 8080},
    )
    assert "multiple_port_candidates" in result.warnings


def test_explicit_compose_path_is_required_when_root_candidates_are_ambiguous() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    fetcher = FakeFetcher(
        {
            tree_url: _tree(
                ("compose.yml", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"),
                ("docker-compose.yml", "ffffffffffffffffffffffffffffffffffffffff"),
            ),
            "https://api.github.com/repos/acme/demo/git/blobs/ffffffffffffffffffffffffffffffffffffffff": _json(
                {
                    "encoding": "base64",
                    "content": "c2VydmljZXM6IHt3ZWI6IHtwb3J0czogWyIzMDAwIl19fQ==",
                }
            ),
        }
    )

    result = discover_repository(
        RepositoryInput(
            url="https://github.com/acme/demo",
            commit_sha=SHA,
            compose_path="docker-compose.yml",
        ),
        fetcher=fetcher,
    )

    assert result.selected_compose_path == "docker-compose.yml"
    assert result.errors == ()


def test_forbidden_is_exposed_as_safe_error_without_upstream_text() -> None:
    commits_url = "https://api.github.com/repos/acme/demo/commits"
    fetcher = FakeFetcher({commits_url: (403, b'{"message":"private details"}')})

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"),
        fetcher=fetcher,
    )

    assert result.commit_sha is None
    assert result.errors == ("github_forbidden",)
    assert "private details" not in repr(result)


class FakeRepository:
    def discover(self, payload: RepositoryInput) -> RepositoryMetadata:
        return RepositoryMetadata(
            commit_sha=SHA,
            commit_url=f"https://github.com/acme/demo/commit/{SHA}",
            compose_candidates=("compose.yml",),
            selected_compose_path="compose.yml",
            ports=({"service": "web", "port": 3000},),
            warnings=(),
            errors=(),
        )


def test_repository_route_uses_same_origin_csrf_and_returns_stable_contract(
    tmp_path: Path,
) -> None:
    repository = FakeRepository()
    with TestClient(create_app(tmp_path, repository=repository)) as client:
        page = client.get("/", headers={"host": "127.0.0.1"})
        token = page.text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
        payload = {"url": "https://github.com/acme/demo", "commit_sha": SHA}

        denied = client.post(
            "/api/repository",
            json=payload,
            headers={"host": "127.0.0.1"},
        )
        assert denied.status_code == 403

        response = client.post(
            "/api/repository",
            json=payload,
            headers={"host": "127.0.0.1", "x-csrf-token": token},
        )
        assert response.status_code == 200
        assert response.json() == {
            "commit_sha": SHA,
            "commit_url": f"https://github.com/acme/demo/commit/{SHA}",
            "compose_candidates": ["compose.yml"],
            "selected_compose_path": "compose.yml",
            "ports": [{"service": "web", "port": 3000}],
            "warnings": [],
            "errors": [],
        }


def test_explicit_safe_yaml_path_can_be_selected_even_without_compose_name() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "9" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"
    fetcher = FakeFetcher(
        {
            tree_url: _tree(("deploy/application.yml", blob_sha)),
            blob_url: _json(
                {
                    "encoding": "base64",
                    "content": "c2VydmljZXM6IHt3ZWI6IHtwb3J0czogWyIzMDAwIl19fQ==",
                }
            ),
        }
    )

    result = discover_repository(
        RepositoryInput(
            url="https://github.com/acme/demo",
            commit_sha=SHA,
            compose_path="deploy/application.yml",
        ),
        fetcher=fetcher,
    )

    assert result.selected_compose_path == "deploy/application.yml"
    assert result.ports == ({"service": "web", "port": 3000},)
    assert result.errors == ()


def _blob_response(source: str) -> bytes:
    return _json(
        {
            "encoding": "base64",
            "content": base64.b64encode(source.encode()).decode(),
        }
    )


def test_long_compose_candidate_is_skipped_with_safe_warning() -> None:
    long_path = "deploy/" + ("x" * 250) + "/compose.yml"
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    fetcher = FakeFetcher({tree_url: _tree((long_path, "a" * 40))})

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=fetcher,
    )

    assert result.compose_candidates == ()
    assert "compose_path_too_long" in result.warnings
    assert result.errors == ("compose_not_found",)


def test_port_candidates_skip_udp_variables_ranges_and_boolean_values() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "8" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"
    compose = """services:
  web:
    ports:
      - true
      - 3000/udp
      - '${PORT}:3000'
      - '8000-8001:8000-8001'
    expose:
      - 3000
"""
    fetcher = FakeFetcher(
        {tree_url: _tree(("compose.yml", blob_sha)), blob_url: _blob_response(compose)}
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=fetcher,
    )

    assert result.ports == ({"service": "web", "port": 3000},)
    assert "port_invalid" in result.warnings
    assert "port_unresolved" in result.warnings
    assert "port_range_unresolved" in result.warnings
    assert "multiple_port_candidates" not in result.warnings


def test_very_long_numeric_port_returns_safe_warning() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "7" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"
    compose = 'services:\n  web:\n    expose: ["' + ("9" * 5000) + '"]\n'
    fetcher = FakeFetcher(
        {tree_url: _tree(("compose.yml", blob_sha)), blob_url: _blob_response(compose)}
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=fetcher,
    )

    assert result.ports == ()
    assert "port_invalid" in result.warnings


def test_symlink_compose_entry_is_not_read() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    fetcher = FakeFetcher(
        {
            tree_url: _json(
                {
                    "truncated": False,
                    "tree": [
                        {
                            "path": "compose.yml",
                            "mode": "120000",
                            "type": "blob",
                            "sha": "6" * 40,
                        }
                    ],
                }
            )
        }
    )

    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=fetcher,
    )

    assert result.compose_candidates == ()
    assert result.errors == ("compose_not_found",)


@pytest.mark.parametrize(
    "payload",
    (
        {"url": "http://github.com/acme/demo"},
        {"url": "https://github.com/acme/demo", "commit_sha": "BAD"},
        {"url": "https://github.com/acme/demo", "compose_path": "../compose.yml"},
    ),
)
def test_repository_input_rejects_unsafe_values(payload: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        RepositoryInput.model_validate(payload)


@pytest.mark.parametrize(
    ("body", "error"),
    (
        (_json({}), "commit_not_found"),
        (_json([{}]), "github_invalid_response"),
        (_json([{"sha": "bad"}]), "github_invalid_response"),
    ),
)
def test_latest_response_shape_is_sanitized(body: bytes, error: str) -> None:
    url = "https://api.github.com/repos/acme/demo/commits"
    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo"),
        fetcher=FakeFetcher({url: body}),
    )
    assert result.errors == (error,)


def test_tree_invalid_response_is_sanitized() -> None:
    url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=FakeFetcher({url: _json({"tree": "bad"})}),
    )
    assert result.errors == ("github_invalid_response",)


@pytest.mark.parametrize(
    "body",
    (
        _json({"encoding": "utf8", "content": "services: {}"}),
        _json({"encoding": "base64", "content": "not-base64"}),
    ),
)
def test_blob_invalid_content_is_sanitized(body: bytes) -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "e" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"
    result = discover_repository(
        RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA),
        fetcher=FakeFetcher(
            {tree_url: _tree(("compose.yml", blob_sha)), blob_url: body}
        ),
    )
    assert result.errors == ("github_invalid_response",)


def test_tree_rate_limit_uses_known_sha_git_fallback() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"

    class FakeGit:
        def discover(
            self, owner: str, repository: str, **kwargs: object
        ) -> GitMetadata:
            return GitMetadata(
                SHA,
                ("compose.yml",),
                "compose.yml",
                b"services: {web: {expose: [8080]}}",
            )

    result = GithubRepositoryDiscovery(
        HeaderFetcher(GithubResponse(403, {"X-RateLimit-Remaining": "0"}, b"{}")),
        git_client=FakeGit(),
    ).discover(RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA))
    assert result.ports == ({"service": "web", "port": 8080},)
    assert result.errors == ()
    assert "git_metadata_fallback" in result.warnings
    assert tree_url


def test_blob_rate_limit_uses_fallback_without_rest_retry() -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "f" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"

    class FailingBlobFetcher:
        def __call__(self, url: str, params: Mapping[str, str]) -> GithubResponse:
            if url == tree_url:
                return GithubResponse(200, {}, _tree(("compose.yml", blob_sha)))
            assert url == blob_url
            return GithubResponse(403, {"X-RateLimit-Remaining": "0"}, b"{}")

    class FakeGit:
        def discover(
            self, owner: str, repository: str, **kwargs: object
        ) -> GitMetadata:
            return GitMetadata(
                SHA,
                ("compose.yml",),
                "compose.yml",
                b"services: {web: {expose: [5000]}}",
            )

    result = GithubRepositoryDiscovery(
        FailingBlobFetcher(), git_client=FakeGit()
    ).discover(RepositoryInput(url="https://github.com/acme/demo", commit_sha=SHA))
    assert result.ports == ({"service": "web", "port": 5000},)
    assert result.retry_after_seconds is None
    assert result.errors == ()


@pytest.mark.parametrize(
    ("stage", "failure"),
    (
        ("latest", TimeoutError),
        ("tree", OSError),
        ("blob", TimeoutError),
    ),
)
def test_timeout_or_unavailable_uses_git_fallback_at_each_github_stage(
    stage: str, failure: type[Exception]
) -> None:
    tree_url = f"https://api.github.com/repos/acme/demo/git/trees/{SHA}"
    blob_sha = "f" * 40
    blob_url = f"https://api.github.com/repos/acme/demo/git/blobs/{blob_sha}"

    def fetcher(url: str, params: Mapping[str, str]) -> GithubResponse:
        if stage == "latest" or (stage == "tree" and url == tree_url):
            raise failure()
        if url == tree_url:
            return GithubResponse(200, {}, _tree(("compose.yml", blob_sha)))
        if url == blob_url:
            raise failure()
        raise AssertionError(url)

    class FakeGit:
        def discover(
            self, owner: str, repository: str, **kwargs: object
        ) -> GitMetadata:
            return GitMetadata(
                SHA,
                ("compose.yml",),
                "compose.yml",
                b"services: {web: {expose: [8080]}}",
            )

    payload = RepositoryInput(
        url="https://github.com/acme/demo",
        commit_sha=None if stage == "latest" else SHA,
    )
    result = GithubRepositoryDiscovery(fetcher, git_client=FakeGit()).discover(payload)
    assert result.commit_sha == SHA
    assert result.ports == ({"service": "web", "port": 8080},)
    assert "git_metadata_fallback" in result.warnings
    assert result.errors == ()
