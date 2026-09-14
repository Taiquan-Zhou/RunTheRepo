from __future__ import annotations

import gzip
import os
import re
import selectors
import signal
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from threading import Event, RLock
from typing import BinaryIO, Protocol, cast

import httpx

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SAFE_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_COMPOSE_NAMES = (
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)
_MAX_PATH = 256
_MAX_FILES = 10000
_MAX_COMPRESSED = 32 * 1024 * 1024
_MAX_DECOMPRESSED = 128 * 1024 * 1024
_MAX_COMPOSE = 4 * 1024 * 1024
_MAX_OUTPUT = 64 * 1024
_MAX_TOTAL_SECONDS = 60.0
_MAX_GIT_SECONDS = 30.0
_CACHE_LATEST_TTL = 5.0
_CACHE_PINNED_TTL = 300.0
_CACHE_MAX_ENTRIES = 16
_CACHE_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class GitCommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int


class GitCommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit: int,
    ) -> GitCommandResult: ...


ArchiveFetcher = Callable[[str], Iterable[bytes]]


@dataclass(frozen=True)
class GitMetadata:
    commit_sha: str | None
    compose_candidates: tuple[str, ...]
    selected_compose_path: str | None
    compose_bytes: bytes | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CacheEntry:
    value: GitMetadata
    expires_at: float
    size: int


class _GitLimitError(RuntimeError):
    pass


class _ArchiveFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GitMetadataClient:
    def __init__(
        self,
        runner: GitCommandRunner | None = None,
        *,
        archive_fetcher: ArchiveFetcher | None = None,
    ) -> None:
        self._runner = runner or _SubprocessGitRunner()
        self._archive_fetcher = archive_fetcher or _http_archive_fetcher
        self._cache: dict[tuple[str, str, str, str], _CacheEntry] = {}
        self._cache_bytes = 0
        self._lock = RLock()
        self._inflight: dict[tuple[str, str, str, str], Event] = {}
        self._latest: dict[tuple[str, str], tuple[float, str]] = {}

    def discover(
        self,
        owner: str,
        repository: str,
        *,
        requested_sha: str | None = None,
        compose_path: str | None = None,
    ) -> GitMetadata:
        if not _SAFE_PART.fullmatch(owner) or not _SAFE_PART.fullmatch(repository):
            return _failure(None, "git_invalid_repository")
        if requested_sha is not None and _FULL_SHA.fullmatch(requested_sha) is None:
            return _failure(None, "git_invalid_commit")
        if compose_path is not None and not _safe_path(compose_path):
            return _failure(requested_sha, "git_invalid_compose_path")
        key = (owner, repository, requested_sha or "", compose_path or "")
        leader, event = self._claim(key)
        if not leader:
            if not event.wait(_MAX_TOTAL_SECONDS + 1):
                return _failure(requested_sha, "git_timeout")
            return self._cached(key) or _failure(requested_sha, "git_timeout")
        deadline = time.monotonic() + _MAX_TOTAL_SECONDS
        try:
            try:
                result = self._discover_uncached(
                    owner, repository, requested_sha, compose_path, deadline
                )
            except _GitLimitError as error:
                result = _failure(requested_sha, str(error))
            except _ArchiveFailure as error:
                result = _failure(requested_sha, error.code)
            self._store(
                key,
                result,
                _CACHE_LATEST_TTL if requested_sha is None else _CACHE_PINNED_TTL,
            )
            return result
        finally:
            with self._lock:
                pending = self._inflight.pop(key, None)
                if pending is not None:
                    pending.set()

    def _discover_uncached(
        self,
        owner: str,
        repository: str,
        requested_sha: str | None,
        compose_path: str | None,
        deadline: float,
    ) -> GitMetadata:
        sha = requested_sha or self._resolve_head(owner, repository, deadline)
        if sha is None:
            return _failure(None, "git_unavailable")
        if time.monotonic() >= deadline:
            raise _GitLimitError("git_timeout")
        chunks = self._archive_fetcher(
            f"https://codeload.github.com/{owner}/{repository}/tar.gz/{sha}"
        )
        try:
            files, contents = _read_archive(chunks, compose_path, deadline)
        except _ArchiveFailure as error:
            return _failure(sha, error.code)
        candidates = _compose_candidates(files)
        selected = _select_compose(compose_path, candidates, files)
        candidate_paths = tuple(path for path, _ in candidates)
        if selected is not None and selected not in candidate_paths:
            candidate_paths = (*candidate_paths, selected)
        if selected is None:
            if candidates:
                return GitMetadata(
                    sha,
                    candidate_paths,
                    None,
                    None,
                    warnings=("git_compose_selection_required",),
                )
            return GitMetadata(
                sha, candidate_paths, None, None, errors=("git_compose_not_found",)
            )
        data = contents.get(selected)
        return (
            GitMetadata(sha, candidate_paths, selected, data)
            if data is not None
            else _failure(sha, "git_compose_not_found")
        )

    def _resolve_head(self, owner: str, repository: str, deadline: float) -> str | None:
        key = (owner, repository)
        with self._lock:
            cached = self._latest.get(key)
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
        remaining = min(_MAX_GIT_SECONDS, deadline - time.monotonic())
        if remaining <= 0:
            raise _GitLimitError("git_timeout")
        result = self._run(
            (
                "ls-remote",
                "--symref",
                "--",
                f"https://github.com/{owner}/{repository}.git",
                "HEAD",
            ),
            remaining,
        )
        if result is None or result.returncode != 0:
            return None
        sha = _parse_head(result.stdout)
        if sha is not None:
            with self._lock:
                self._latest[key] = (time.monotonic() + _CACHE_LATEST_TTL, sha)
        return sha

    def _run(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> GitCommandResult | None:
        try:
            return self._runner(
                _git_argv(argv),
                timeout_seconds=timeout_seconds,
                output_limit=_MAX_OUTPUT,
            )
        except _GitLimitError:
            raise
        except (OSError, subprocess.SubprocessError):
            return None

    def _claim(self, key: tuple[str, str, str, str]) -> tuple[bool, Event]:
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > time.monotonic():
                ready = Event()
                ready.set()
                return False, ready
            if cached is not None:
                self._remove(key)
            pending = self._inflight.get(key)
            if pending is not None:
                return False, pending
            pending = Event()
            self._inflight[key] = pending
            return True, pending

    def _cached(self, key: tuple[str, str, str, str]) -> GitMetadata | None:
        with self._lock:
            entry = self._cache.get(key)
            return (
                entry.value
                if entry is not None and entry.expires_at > time.monotonic()
                else None
            )

    def _store(
        self, key: tuple[str, str, str, str], value: GitMetadata, ttl: float
    ) -> None:
        size = len(value.compose_bytes or b"") + sum(
            len(path) for path in value.compose_candidates
        )
        if size > _CACHE_MAX_BYTES:
            return
        with self._lock:
            old = self._cache.pop(key, None)
            if old is not None:
                self._cache_bytes -= old.size
            while self._cache and (
                len(self._cache) >= _CACHE_MAX_ENTRIES
                or self._cache_bytes + size > _CACHE_MAX_BYTES
            ):
                self._remove(next(iter(self._cache)))
            self._cache[key] = _CacheEntry(value, time.monotonic() + ttl, size)
            self._cache_bytes += size

    def _remove(self, key: tuple[str, str, str, str]) -> None:
        old = self._cache.pop(key, None)
        if old is not None:
            self._cache_bytes -= old.size


class _SubprocessGitRunner:
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit: int,
    ) -> GitCommandResult:
        if any("\x00" in value for value in argv):
            raise OSError("invalid git argument")
        process = subprocess.Popen(
            ("git", *argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
        started = time.monotonic()
        try:
            while selector.get_map():
                if time.monotonic() - started >= timeout_seconds:
                    raise _GitLimitError("git_timeout")
                for key, _ in selector.select(0.1):
                    stream = cast(BinaryIO, key.fileobj)
                    data = os.read(stream.fileno(), 8192)
                    if not data:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    buffers[stream].extend(data)
                    if sum(len(value) for value in buffers.values()) > output_limit:
                        raise _GitLimitError("git_metadata_limit_exceeded")
            return GitCommandResult(
                bytes(buffers[process.stdout]),
                bytes(buffers[process.stderr]),
                process.wait(timeout=2),
            )
        except (_GitLimitError, OSError, subprocess.TimeoutExpired):
            _kill_process_group(process)
            raise
        finally:
            selector.close()
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _git_argv(argv: Sequence[str]) -> tuple[str, ...]:
    return (
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "http.version=HTTP/1.1",
        "-c",
        "protocol.version=2",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "core.hooksPath=/dev/null",
        *argv,
    )


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_") and key.upper() != "SSH_ASKPASS"
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_ALLOW_PROTOCOL": "https",
        }
    )
    return environment


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _http_archive_fetcher(url: str) -> Iterable[bytes]:
    client = httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(8, connect=8),
        headers={
            "User-Agent": "RepoTrial-local-console",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with client.stream("GET", url) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                raise _ArchiveFailure("git_archive_redirect_rejected")
            if response.status_code == 404:
                raise _ArchiveFailure("git_commit_not_found")
            if response.status_code < 200 or response.status_code >= 300:
                raise _ArchiveFailure("git_archive_failed")
            try:
                yield from response.iter_raw()
            except (httpx.HTTPError, TimeoutError):
                raise _ArchiveFailure("git_timeout") from None
    except httpx.HTTPError:
        raise _ArchiveFailure("git_unavailable") from None
    finally:
        client.close()


class _BoundedGzipReader:
    def __init__(self, chunks: Iterable[bytes], deadline: float) -> None:
        self._chunks = iter(chunks)
        self._deadline = deadline
        self._compressed = 0
        self._pending = b""

    def read_compressed(self, size: int) -> bytes:
        while len(self._pending) < size:
            if time.monotonic() >= self._deadline:
                raise _ArchiveFailure("git_timeout")
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            self._compressed += len(chunk)
            if self._compressed > _MAX_COMPRESSED:
                raise _ArchiveFailure("git_metadata_limit_exceeded")
            self._pending += chunk
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = 64 * 1024
        return self.read_compressed(size)


class _BoundedDecompressedReader:
    def __init__(self, gzip_reader: gzip.GzipFile, deadline: float) -> None:
        self._gzip_reader = gzip_reader
        self._deadline = deadline
        self._decompressed = 0

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() >= self._deadline:
            raise _ArchiveFailure("git_timeout")
        data = self._gzip_reader.read(64 * 1024 if size < 0 else min(size, 64 * 1024))
        self._decompressed += len(data)
        if self._decompressed > _MAX_DECOMPRESSED:
            raise _ArchiveFailure("git_metadata_limit_exceeded")
        return data


def _read_archive(
    chunks: Iterable[bytes],
    requested_path: str | None,
    deadline: float,
) -> tuple[dict[str, str], dict[str, bytes]]:
    compressed = _BoundedGzipReader(chunks, deadline)
    gzip_reader = gzip.GzipFile(fileobj=cast(BinaryIO, compressed), mode="rb")
    tar_reader = _BoundedDecompressedReader(gzip_reader, deadline)
    files: dict[str, str] = {}
    contents: dict[str, bytes] = {}
    count = 0
    root_prefix: str | None = None
    seen_paths: set[str] = set()
    try:
        with tarfile.open(fileobj=cast(BinaryIO, tar_reader), mode="r|") as archive:
            for member in archive:
                count += 1
                if count > _MAX_FILES or member.size < 0:
                    raise _ArchiveFailure("git_metadata_limit_exceeded")
                relative = _relative_archive_path(member.name, root_prefix)
                if relative is None:
                    if root_prefix is None:
                        parts = _archive_parts(member.name)
                        if parts:
                            root_prefix = parts[0]
                            continue
                    raise _ArchiveFailure("git_archive_invalid")
                root_prefix, path = relative
                if not member.isreg():
                    continue
                if path in seen_paths:
                    raise _ArchiveFailure("git_archive_invalid")
                seen_paths.add(path)
                if not _is_compose_candidate(path) and path != requested_path:
                    continue
                if member.size > _MAX_COMPOSE:
                    raise _ArchiveFailure("git_metadata_limit_exceeded")
                source = archive.extractfile(member)
                if source is None:
                    raise _ArchiveFailure("git_archive_invalid")
                data = source.read(_MAX_COMPOSE + 1)
                if len(data) > _MAX_COMPOSE:
                    raise _ArchiveFailure("git_metadata_limit_exceeded")
                contents[path] = data
                files[path] = "archive"
    except (gzip.BadGzipFile, EOFError, tarfile.TarError):
        raise _ArchiveFailure("git_archive_invalid") from None
    finally:
        gzip_reader.close()
        close = getattr(chunks, "close", None)
        if callable(close):
            close()
    return files, contents


def _relative_archive_path(
    name: str, root_prefix: str | None
) -> tuple[str, str] | None:
    parts = _archive_parts(name)
    if len(parts) < 2:
        return None
    if root_prefix is not None and parts[0] != root_prefix:
        raise _ArchiveFailure("git_archive_invalid")
    relative = "/".join(parts[1:])
    if not _safe_path(relative):
        raise _ArchiveFailure("git_archive_invalid")
    return parts[0], relative


def _archive_parts(name: str) -> tuple[str, ...]:
    if (
        not name
        or name.startswith(chr(47))
        or chr(92) in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise _ArchiveFailure("git_archive_invalid")
    name = name.removesuffix("/")
    parts = tuple(name.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _ArchiveFailure("git_archive_invalid")
    return parts


def _parse_head(output: bytes) -> str | None:
    for line in output.splitlines():
        fields = line.decode("ascii", "ignore").split("\t")
        if len(fields) == 2 and fields[1] == "HEAD" and _FULL_SHA.fullmatch(fields[0]):
            return fields[0]
    return None


def _compose_candidates(files: Mapping[str, str]) -> list[tuple[str, str]]:
    candidates = [
        (path, value) for path, value in files.items() if _is_compose_candidate(path)
    ]
    return sorted(
        candidates,
        key=lambda item: (
            0 if "/" not in item[0] else 1,
            _standard_rank(item[0]),
            item[0],
        ),
    )


def _select_compose(
    requested: str | None,
    candidates: list[tuple[str, str]],
    files: Mapping[str, str],
) -> str | None:
    if requested is not None:
        return requested if requested in files else None
    root = [path for path, _ in candidates if "/" not in path]
    if len(root) == 1:
        return root[0]
    if not root and len(candidates) == 1:
        return candidates[0][0]
    return None


def _is_compose_candidate(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].casefold()
    return name in _COMPOSE_NAMES or (
        "compose" in name and name.endswith((".yml", ".yaml"))
    )


def _standard_rank(path: str) -> int:
    name = path.rsplit("/", 1)[-1].casefold()
    return _COMPOSE_NAMES.index(name) if name in _COMPOSE_NAMES else len(_COMPOSE_NAMES)


def _safe_path(path: str) -> bool:
    return bool(
        path
        and len(path) <= _MAX_PATH
        and not path.startswith("/")
        and chr(92) not in path
        and chr(0) not in path
        and not any(ord(char) < 32 or ord(char) == 127 for char in path)
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _failure(commit_sha: str | None, code: str) -> GitMetadata:
    return GitMetadata(commit_sha, (), None, None, errors=(code,))
