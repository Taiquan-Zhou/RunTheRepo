from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event, RLock
from typing import Literal, TypedDict
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from repotrial.compose.parser import ComposeParseError, load_compose_bytes
from repotrial.domain.models import RepoRef
from repotrial.intake.github import RepoIntakeError, parse_github_url
from repotrial.local_web.git_metadata import GitMetadataClient

_ROOT = "https://api.github.com"
_SHA = re.compile(r"[0-9a-f]{40}")
_NAMES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")
_LATEST_CACHE_TTL = 5.0
_PINNED_CACHE_TTL = 300.0
_CACHE_MAX_ENTRIES = 64
_CACHE_MAX_BYTES = 8 * 1024 * 1024
_DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
_MAX_RETRY_AFTER = 24 * 60 * 60.0


class PortCandidate(TypedDict):
    service: str
    port: int


@dataclass(frozen=True)
class GithubResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


Fetcher = Callable[[str, Mapping[str, str]], GithubResponse]


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(min_length=1, max_length=2048)
    commit_sha: str | None = Field(default=None, min_length=40, max_length=40)
    compose_path: str | None = Field(default=None, max_length=256)

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        try:
            return parse_github_url(v).url
        except RepoIntakeError:
            raise ValueError(
                "URL must be a GitHub HTTPS owner/repository URL"
            ) from None

    @field_validator("commit_sha")
    @classmethod
    def check_sha(cls, v: str | None) -> str | None:
        if v is not None and _SHA.fullmatch(v) is None:
            raise ValueError("commit_sha must be a full lowercase SHA")
        return v

    @field_validator("compose_path")
    @classmethod
    def check_path(cls, v: str | None) -> str | None:
        p = Path(v) if v is not None else None
        if v is not None and (
            not v
            or "\x00" in v
            or p is None
            or p.is_absolute()
            or "\\" in v
            or any(x in {"", ".", ".."} for x in p.parts)
        ):
            raise ValueError("compose_path must be a safe relative path")
        return v


@dataclass(frozen=True)
class RepositoryMetadata:
    commit_sha: str | None
    commit_url: str | None
    compose_candidates: tuple[str, ...]
    selected_compose_path: str | None
    ports: tuple[PortCandidate, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    retry_after_seconds: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "commit_sha": self.commit_sha,
            "commit_url": self.commit_url,
            "compose_candidates": list(self.compose_candidates),
            "selected_compose_path": self.selected_compose_path,
            "ports": [dict(x) for x in self.ports],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            **(
                {"retry_after_seconds": self.retry_after_seconds}
                if self.retry_after_seconds is not None
                else {}
            ),
        }


class _Failure(RuntimeError):
    def __init__(self, code: str, retry_after_seconds: float | None = None) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


@dataclass(frozen=True)
class _CacheEntry:
    value: object
    expires_at: float
    size: int


class GithubRepositoryDiscovery:
    def __init__(
        self,
        fetcher: Fetcher | None = None,
        *,
        git_client: GitMetadataClient | None = None,
    ) -> None:
        self.fetcher = fetcher or _HttpxFetcher()
        self._git_client = (
            git_client
            if git_client is not None
            else (GitMetadataClient() if fetcher is None else None)
        )
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], _CacheEntry] = {}
        self._cache_bytes = 0
        self._cache_lock = RLock()
        self._inflight: dict[tuple[str, tuple[tuple[str, str], ...]], Event] = {}
        self._cooldown_until = 0.0

    def discover(self, payload: RepositoryInput) -> RepositoryMetadata:
        repo = parse_github_url(payload.url)
        start = time.monotonic()
        w: list[str] = []
        sha = payload.commit_sha
        if sha is None:
            try:
                sha = self._latest(repo.owner, repo.repo, start)
            except _Failure as e:
                if e.code in {
                    "github_rate_limited",
                    "github_timeout",
                    "github_unavailable",
                }:
                    return self._git_fallback(
                        payload, repo, None, e.retry_after_seconds, e.code
                    )
                return _meta(
                    None, None, (), None, (), w, [e.code], e.retry_after_seconds
                )
        assert sha is not None
        url = f"{repo.url}/commit/{sha}"
        try:
            tree, trunc, long_compose_path = self._tree(
                repo.owner, repo.repo, sha, start
            )
        except _Failure as e:
            if e.code in {
                "github_rate_limited",
                "github_timeout",
                "github_unavailable",
            }:
                return self._git_fallback(
                    payload, repo, sha, e.retry_after_seconds, e.code
                )
            return _meta(sha, url, (), None, (), w, [e.code], e.retry_after_seconds)
        if trunc:
            w.append("tree_truncated")
        if long_compose_path:
            w.append("compose_path_too_long")
        all_files: dict[str, str] = {}
        for item in tree:
            path = item.get("path")
            blob_sha = item.get("sha")
            if isinstance(path, str) and isinstance(blob_sha, str):
                all_files[path] = blob_sha
        entries = _entries(tree)
        if len(entries) > 64:
            entries = entries[:64]
            w.append("compose_candidates_limited")
        candidates = tuple(x[0] for x in entries)
        selected = _select(payload.compose_path, entries)
        if (
            selected is None
            and payload.compose_path is not None
            and payload.compose_path in all_files
        ):
            selected = payload.compose_path
        if payload.compose_path is not None and selected is None:
            return _meta(sha, url, candidates, None, (), w, ["compose_not_found"])
        if selected is not None and selected not in candidates:
            candidates = (*candidates, selected)
        if selected is None:
            if not entries:
                return _meta(sha, url, candidates, None, (), w, ["compose_not_found"])
            w.append("compose_selection_required")
            return _meta(sha, url, candidates, None, (), w, [])
        try:
            source = self._blob(repo.owner, repo.repo, all_files[selected], start)
            compose = load_compose_bytes(source)
        except _Failure as e:
            if e.code in {
                "github_rate_limited",
                "github_timeout",
                "github_unavailable",
            }:
                return self._git_fallback(
                    payload, repo, sha, e.retry_after_seconds, e.code
                )
            return _meta(
                sha, url, candidates, selected, (), w, [e.code], e.retry_after_seconds
            )
        except ComposeParseError:
            return _meta(sha, url, candidates, selected, (), w, ["compose_invalid"])
        ports, pw = _ports(compose)
        w.extend(pw)
        if len(ports) > 1:
            w.append("multiple_port_candidates")
        if not ports:
            w.append("no_port_candidates")
        return _meta(sha, url, candidates, selected, ports, w, [])

    def _git_fallback(
        self,
        payload: RepositoryInput,
        repo: RepoRef,
        known_sha: str | None,
        retry_after_seconds: float | None,
        failure_code: str = "github_rate_limited",
    ) -> RepositoryMetadata:
        git_client = self._git_client
        if git_client is None:
            return _meta(
                known_sha,
                _commit_url(repo, known_sha),
                (),
                None,
                (),
                [],
                [failure_code],
                retry_after_seconds,
            )
        result = git_client.discover(
            repo.owner,
            repo.repo,
            requested_sha=known_sha,
            compose_path=payload.compose_path,
        )
        warnings = ["git_metadata_fallback", *result.warnings]
        sha = result.commit_sha or known_sha
        url = _commit_url(repo, sha)
        candidates = result.compose_candidates
        selected = result.selected_compose_path
        if result.compose_bytes is None:
            return _meta(
                sha, url, candidates, selected, (), warnings, list(result.errors)
            )
        try:
            compose = load_compose_bytes(result.compose_bytes)
        except ComposeParseError:
            return _meta(
                sha, url, candidates, selected, (), warnings, ["compose_invalid"]
            )
        ports, port_warnings = _ports(compose)
        warnings.extend(port_warnings)
        if len(ports) > 1:
            warnings.append("multiple_port_candidates")
        if not ports:
            warnings.append("no_port_candidates")
        return _meta(sha, url, candidates, selected, ports, warnings, [])

    def _latest(self, owner: str, repository: str, start: float) -> str:
        body = self._json(
            self._endpoint(owner, repository, "commits"),
            {"per_page": "1"},
            start,
            "latest",
        )
        if not isinstance(body, list) or not body or not isinstance(body[0], dict):
            raise _Failure("commit_not_found")
        sha = body[0].get("sha")
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            raise _Failure("github_invalid_response")
        return sha

    def _tree(
        self, owner: str, repository: str, sha: str, start: float
    ) -> tuple[list[dict[str, object]], bool, bool]:
        body = self._json(
            self._endpoint(owner, repository, f"git/trees/{quote(sha, safe='')}"),
            {"recursive": "1"},
            start,
            "tree",
        )
        if not isinstance(body, dict) or not isinstance(body.get("tree"), list):
            raise _Failure("github_invalid_response")
        out: list[dict[str, object]] = []
        long_compose_path = False
        for x in body["tree"][:10000]:
            if (
                isinstance(x, dict)
                and isinstance(x.get("path"), str)
                and len(x["path"]) > 256
                and _is_compose_candidate(x["path"])
            ):
                long_compose_path = True
            if (
                isinstance(x, dict)
                and x.get("type") == "blob"
                and x.get("mode") in {"100644", "100755"}
                and isinstance(x.get("path"), str)
                and isinstance(x.get("sha"), str)
                and _safe(x["path"])
                and _SHA.fullmatch(x["sha"])
            ):
                out.append({"path": x["path"], "sha": x["sha"]})
        return (
            out,
            body.get("truncated") is True or len(body["tree"]) > 10000,
            long_compose_path,
        )

    def _blob(self, owner: str, repository: str, sha: str, start: float) -> bytes:
        body = self._json(
            self._endpoint(owner, repository, f"git/blobs/{quote(sha, safe='')}"),
            {},
            start,
            "blob",
        )
        if (
            not isinstance(body, dict)
            or body.get("encoding") != "base64"
            or not isinstance(body.get("content"), str)
        ):
            raise _Failure("github_invalid_response")
        try:
            data = base64.b64decode("".join(body["content"].split()), validate=True)
        except (ValueError, binascii.Error):
            raise _Failure("github_invalid_response") from None
        if len(data) > 4 * 1024 * 1024:
            raise _Failure("compose_too_large")
        return data

    def _json(
        self,
        url: str,
        params: Mapping[str, str],
        start: float,
        op: Literal["latest", "tree", "blob"],
    ) -> object:
        key = (url, tuple(sorted(params.items())))
        while True:
            remaining = 30.0 - (time.monotonic() - start)
            if remaining <= 0:
                raise _Failure("github_timeout")
            with self._cache_lock:
                cached = self._cache.get(key)
                now = time.monotonic()
                if cached is not None:
                    if cached.expires_at > now:
                        return cached.value
                    self._remove_cache_entry(key)
                if self._cooldown_until > now:
                    raise _Failure(
                        "github_rate_limited",
                        max(0.0, self._cooldown_until - now),
                    )
                pending = self._inflight.get(key)
                if pending is None:
                    pending = Event()
                    self._inflight[key] = pending
                    break
            if not pending.wait(remaining):
                raise _Failure("github_timeout")

        try:
            try:
                resp = self.fetcher(url, params)
            except _Failure:
                raise
            except (httpx.TimeoutException, TimeoutError):
                raise _Failure("github_timeout") from None
            except (httpx.HTTPError, OSError):
                raise _Failure("github_unavailable") from None
            if resp.status_code == 429:
                delay = _rate_limit_delay(resp.headers)
                if delay is None:
                    delay = _DEFAULT_RATE_LIMIT_COOLDOWN
                self._set_cooldown(delay)
                raise _Failure("github_rate_limited", delay)
            if resp.status_code == 403:
                rate_delay = _rate_limit_delay(resp.headers)
                if rate_delay is not None:
                    self._set_cooldown(rate_delay)
                    raise _Failure("github_rate_limited", rate_delay)
                raise _Failure("github_forbidden")
            if resp.status_code == 404:
                raise _Failure(
                    "commit_not_found" if op == "latest" else "github_not_found"
                )
            if resp.status_code in {301, 302, 307, 308}:
                raise _Failure("github_redirect_rejected")
            if resp.status_code < 200 or resp.status_code >= 300:
                raise _Failure("github_unavailable")
            try:
                value = json.loads(resp.body)
            except (ValueError, UnicodeDecodeError):
                raise _Failure("github_invalid_response") from None
            self._cache_response(key, value, len(resp.body), op)
            return value
        finally:
            with self._cache_lock:
                event = self._inflight.pop(key, None)
                if event is not None:
                    event.set()

    def _cache_response(
        self,
        key: tuple[str, tuple[tuple[str, str], ...]],
        value: object,
        size: int,
        op: Literal["latest", "tree", "blob"],
    ) -> None:
        if size > _CACHE_MAX_BYTES:
            return
        entry = _CacheEntry(
            value,
            time.monotonic()
            + (_LATEST_CACHE_TTL if op == "latest" else _PINNED_CACHE_TTL),
            size,
        )
        with self._cache_lock:
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= previous.size
            while self._cache and (
                len(self._cache) >= _CACHE_MAX_ENTRIES
                or self._cache_bytes + size > _CACHE_MAX_BYTES
            ):
                removed = self._cache.pop(next(iter(self._cache)))
                self._cache_bytes -= removed.size
            self._cache[key] = entry
            self._cache_bytes += size

    def _remove_cache_entry(self, key: tuple[str, tuple[tuple[str, str], ...]]) -> None:
        removed = self._cache.pop(key, None)
        if removed is not None:
            self._cache_bytes -= removed.size

    def _set_cooldown(self, delay: float) -> None:
        with self._cache_lock:
            self._cooldown_until = max(
                self._cooldown_until, time.monotonic() + min(delay, _MAX_RETRY_AFTER)
            )

    @staticmethod
    def _endpoint(owner: str, repository: str, suffix: str) -> str:
        return f"{_ROOT}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/{suffix}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value.strip() if isinstance(value, str) else None
    return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = _header(headers, "retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    if seconds < 0:
        return 0.0
    return min(seconds, _MAX_RETRY_AFTER)


def _rate_limit_delay(headers: Mapping[str, str]) -> float | None:
    retry_after = _retry_after(headers)
    remaining = _header(headers, "x-ratelimit-remaining")
    reset = _header(headers, "x-ratelimit-reset")
    reset_delay: float | None = None
    if reset is not None:
        try:
            reset_delay = float(reset) - time.time()
        except ValueError:
            reset_delay = None
        if reset_delay is not None and not math.isfinite(reset_delay):
            reset_delay = None
        if reset_delay is not None:
            reset_delay = min(max(0.0, reset_delay), _MAX_RETRY_AFTER)
    if remaining == "0":
        if retry_after is not None:
            return retry_after
        if reset_delay is not None:
            return reset_delay
        return _DEFAULT_RATE_LIMIT_COOLDOWN
    if retry_after is not None:
        return retry_after
    return None


def discover_repository(
    p: RepositoryInput, *, fetcher: Fetcher | None = None
) -> RepositoryMetadata:
    return GithubRepositoryDiscovery(fetcher).discover(p)


def _commit_url(repo: RepoRef, sha: str | None) -> str | None:
    if sha is None:
        return None
    return f"{repo.url}/commit/{sha}"


class _HttpxFetcher:
    def __call__(self, url: str, params: Mapping[str, str]) -> GithubResponse:
        with (
            httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(8, connect=8),
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "RepoTrial-local-console",
                },
                limits=httpx.Limits(max_connections=2),
            ) as client,
            client.stream("GET", url, params=params) as r,
        ):
            chunks: list[bytes] = []
            size = 0
            started = time.monotonic()
            for c in r.iter_bytes():
                if time.monotonic() - started >= 30:
                    raise _Failure("github_timeout")
                size += len(c)
                if size > 8 * 1024 * 1024:
                    raise _Failure("github_response_too_large")
                chunks.append(c)
            return GithubResponse(r.status_code, dict(r.headers), b"".join(chunks))


def _entries(tree: list[dict[str, object]]) -> list[tuple[str, str]]:
    out = []
    for x in tree:
        path, sha = x["path"], x["sha"]
        assert isinstance(path, str) and isinstance(sha, str)
        if _is_compose_candidate(path):
            out.append((path, sha))
    return sorted(
        out,
        key=lambda x: (
            0 if "/" not in x[0] else 1,
            _NAMES.index(x[0].rsplit("/", 1)[-1].casefold())
            if x[0].rsplit("/", 1)[-1].casefold() in _NAMES
            else 4,
            x[0],
        ),
    )


def _is_compose_candidate(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].casefold()
    return name in _NAMES or ("compose" in name and name.endswith((".yml", ".yaml")))


def _select(requested: str | None, entries: list[tuple[str, str]]) -> str | None:
    if requested is not None:
        return requested if requested in {x[0] for x in entries} else None
    root = [x[0] for x in entries if "/" not in x[0]]
    if len(root) == 1:
        return root[0]
    if not root and len(entries) == 1:
        return entries[0][0]
    return None


def _ports(
    compose: Mapping[str, object],
) -> tuple[tuple[PortCandidate, ...], list[str]]:
    services = compose.get("services")
    if not isinstance(services, Mapping):
        return (), ["compose_services_unavailable"]
    found: set[tuple[str, int]] = set()
    warnings: list[str] = []
    for service, definition in services.items():
        if not isinstance(service, str) or not isinstance(definition, Mapping):
            continue
        for field in ("ports", "expose"):
            vals = definition.get(field)
            if not isinstance(vals, list):
                continue
            for val in vals:
                port, warning = _port(val)
                if warning:
                    warnings.append(warning)
                if port is not None:
                    found.add((service, port))
    return tuple({"service": s, "port": p} for s, p in sorted(found)), list(
        _unique(warnings)
    )


def _port(value: object) -> tuple[int | None, str | None]:
    if isinstance(value, Mapping):
        if value.get("protocol", "tcp") != "tcp":
            return None, None
        value = value.get("target")
    if isinstance(value, bool):
        return None, "port_invalid"
    if isinstance(value, int):
        return (value, None) if 1 <= value <= 65535 else (None, "port_invalid")
    if not isinstance(value, str):
        return None, "port_invalid"
    if "$" + "{" in value:
        return None, "port_unresolved"
    value, _, protocol = value.partition("/")
    if protocol and protocol.casefold() != "tcp":
        return None, None
    target = value.rsplit(":", 1)[-1]
    if "-" in target:
        return None, "port_range_unresolved"
    if not target.isascii() or not target.isdecimal() or len(target) > 5:
        return None, "port_invalid"
    port = int(target)
    return (port, None) if 1 <= port <= 65535 else (None, "port_invalid")


def _safe(path: str) -> bool:
    return bool(
        path
        and len(path) <= 256
        and not path.startswith("/")
        and "\\" not in path
        and "\x00" not in path
        and all(x not in {"", ".", ".."} for x in path.split("/"))
    )


def _unique(x: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x))


def _meta(
    sha: str | None,
    url: str | None,
    candidates: tuple[str, ...],
    selected: str | None,
    ports: tuple[PortCandidate, ...],
    w: list[str],
    e: list[str],
    retry_after_seconds: float | None = None,
) -> RepositoryMetadata:
    return RepositoryMetadata(
        sha,
        url,
        candidates,
        selected,
        ports,
        _unique(w),
        _unique(e),
        retry_after_seconds,
    )
