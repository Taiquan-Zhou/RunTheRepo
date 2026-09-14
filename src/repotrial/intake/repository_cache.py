"""Bounded immutable Git object cache for exact repository pins."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from repotrial.domain.models import PinnedRepo
from repotrial.intake.github import (
    RepoIntakeError,
    _claim_destination,
    _close_directory_fd,
    _DestinationClaim,
    _git_environment,
    _kill_and_reap,
    _normalize_destination,
    _remove_owned_destination,
    parse_github_url,
    pin_repository,
)

_fcntl: ModuleType | None = None
if os.name == "posix":
    try:
        _fcntl = importlib.import_module("fcntl")
    except ImportError:
        pass

_FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_CACHE_SCHEMA_VERSION = 1
_MAX_ENTRIES = 16
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_ENTRY_BYTES = 128 * 1024 * 1024
_CACHE_TIMEOUT_SECONDS = 30
_SCAN_YIELD_EVERY = 64
_MAX_SCAN_ENTRIES = 1_000_000
_MANIFEST_BUDGET = 4096
_SOURCE_ENTRY_BUDGET = 256
_GIT_SECURITY_CONFIG = (
    "-c",
    "credential.helper=",
    "-c",
    "core.askPass=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.http.allow=never",
    "-c",
    "protocol.https.allow=never",
    "-c",
    "protocol.ssh.allow=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.file.allow=always",
)


type RepositoryPinner = Callable[[str, Path, str | None], Awaitable[PinnedRepo]]


class _CacheGitError(RuntimeError):
    pass


class RepositoryCache:
    """Store and restore only complete objects for an exact GitHub commit."""

    def __init__(self, cache_root: Path | None) -> None:
        self._root = None if cache_root is None else Path(cache_root)

    async def pin(
        self,
        url: str,
        destination: Path,
        requested_ref: str | None,
        pinner: RepositoryPinner = pin_repository,
    ) -> PinnedRepo:
        expected_sha = (
            requested_ref
            if requested_ref is not None and _FULL_COMMIT_SHA.fullmatch(requested_ref)
            else None
        )
        if expected_sha is not None:
            restored = await self.restore(url, expected_sha, destination)
            if restored is not None:
                return restored

        pinned = await pinner(url, destination, requested_ref)
        if expected_sha is not None and pinned.commit_sha == expected_sha:
            await self.store(pinned)
        return pinned

    async def restore(
        self, url: str, expected_sha: str, destination: Path
    ) -> PinnedRepo | None:
        try:
            async with asyncio.timeout(_CACHE_TIMEOUT_SECONDS):
                return await self._restore(url, expected_sha, destination)
        except TimeoutError:
            return None

    async def _restore(
        self, url: str, expected_sha: str, destination: Path
    ) -> PinnedRepo | None:
        if not _FULL_COMMIT_SHA.fullmatch(expected_sha):
            return None
        canonical = _canonical_url(url)
        if canonical is None or not self._usable_root():
            return None
        key = _cache_key(canonical, expected_sha)
        assert self._root is not None
        entry = self._root / "entries" / key
        try:
            with self._try_lock() as locked:
                if not locked or not await _valid_entry(
                    entry, canonical, expected_sha, key
                ):
                    return None
                destination_path = _normalize_destination(destination)
                claim = _claim_destination(destination_path)
                try:
                    bare_args = (*_GIT_SECURITY_CONFIG, "-C", str(entry / "repo.git"))
                    await _run_cache_git(
                        "fsck",
                        *bare_args,
                        "fsck",
                        "--full",
                        "--no-reflogs",
                        "--no-dangling",
                    )
                    missing = await _run_cache_git(
                        "missing",
                        *bare_args,
                        "rev-list",
                        "--objects",
                        "--missing=print",
                        expected_sha,
                    )
                    if any(
                        line.startswith("?")
                        for line in missing.decode(
                            "utf-8", errors="replace"
                        ).splitlines()
                    ):
                        raise _CacheGitError("cache has missing objects")
                    await _run_cache_git(
                        "clone",
                        *_GIT_SECURITY_CONFIG,
                        "clone",
                        "--no-local",
                        "--no-hardlinks",
                        "--no-tags",
                        "--no-checkout",
                        "--",
                        str(entry / "repo.git"),
                        str(destination_path),
                    )
                    repository_args = (
                        *_GIT_SECURITY_CONFIG,
                        "-C",
                        str(destination_path),
                    )
                    await _run_cache_git(
                        "checkout",
                        *repository_args,
                        "checkout",
                        "--detach",
                        expected_sha,
                    )
                    await _run_cache_git(
                        "origin",
                        *repository_args,
                        "remote",
                        "set-url",
                        "origin",
                        canonical,
                    )
                    head = (
                        (
                            await _run_cache_git(
                                "resolve",
                                *repository_args,
                                "rev-parse",
                                "--verify",
                                "HEAD^{commit}",
                            )
                        )
                        .decode("ascii", errors="replace")
                        .strip()
                        .lower()
                    )
                    origin = (
                        (
                            await _run_cache_git(
                                "origin",
                                *repository_args,
                                "remote",
                                "get-url",
                                "origin",
                            )
                        )
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
                    if head != expected_sha or origin != canonical:
                        raise _CacheGitError("restored repository identity mismatch")
                    if not await _workspace_git_is_complete(destination_path):
                        raise _CacheGitError("restored repository is incomplete")
                    return PinnedRepo(
                        repo=parse_github_url(canonical, expected_sha),
                        commit_sha=head,
                        local_path=destination_path,
                    )
                except asyncio.CancelledError:
                    _remove_owned_destination(claim)
                    raise
                except (OSError, _CacheGitError):
                    _remove_owned_destination(claim)
                    return None
                finally:
                    _close_directory_fd(claim.directory_fd)
        except (OSError, RepoCacheError, RepoIntakeError):
            return None

    async def store(self, pinned: PinnedRepo) -> bool:
        try:
            async with asyncio.timeout(_CACHE_TIMEOUT_SECONDS):
                return await self._store(pinned)
        except TimeoutError:
            return False

    async def _store(self, pinned: PinnedRepo) -> bool:
        expected_sha = pinned.commit_sha.lower()
        if not _FULL_COMMIT_SHA.fullmatch(expected_sha):
            return False
        canonical = _canonical_url(pinned.repo.url)
        if canonical is None or not self._usable_root():
            return False
        source = Path(pinned.local_path)
        try:
            source = source.resolve(strict=True)
        except OSError:
            return False
        source_git_size = await _source_cache_budget(source)
        if source_git_size is None:
            return False
        key = _cache_key(canonical, expected_sha)
        assert self._root is not None
        entry = self._root / "entries" / key
        try:
            with self._try_lock() as locked:
                if not locked:
                    return False
                if await _valid_entry(entry, canonical, expected_sha, key):
                    return True
                shallow_claim = await _is_owned_shallow_entry(
                    entry, canonical, expected_sha, key
                )
                if shallow_claim is not None and not _evict_shallow_entry(
                    shallow_claim
                ):
                    return False
                entries = [
                    child
                    for child in (self._root / "entries").iterdir()
                    if child.is_dir() and not child.is_symlink()
                ]
                if len(entries) >= _MAX_ENTRIES:
                    return False
                retained_sizes: list[int] = []
                retained_total = 0
                for child in entries:
                    child_size = await _async_tree_size(
                        child,
                        limit=_MAX_TOTAL_BYTES - retained_total,
                    )
                    retained_sizes.append(child_size)
                    retained_total += child_size
                if (
                    source_git_size + _MANIFEST_BUDGET > _MAX_ENTRY_BYTES
                    or retained_total + source_git_size + _MANIFEST_BUDGET
                    > _MAX_TOTAL_BYTES
                ):
                    return False
                temp_path = self._root / "entries" / f".tmp-{uuid.uuid4().hex}"
                claim = _claim_destination(temp_path)
                published = False
                try:
                    bare = temp_path / "repo.git"
                    source_args = (*_GIT_SECURITY_CONFIG, "-C", str(source))
                    source_head = (
                        (
                            await _run_cache_git(
                                "source_head",
                                *source_args,
                                "rev-parse",
                                "--verify",
                                "HEAD^{commit}",
                            )
                        )
                        .decode("ascii", errors="replace")
                        .strip()
                        .lower()
                    )
                    if source_head != expected_sha:
                        raise _CacheGitError("source commit mismatch")
                    await _run_cache_git(
                        "init", *_GIT_SECURITY_CONFIG, "init", "--bare", str(bare)
                    )
                    bare_args = (*_GIT_SECURITY_CONFIG, "-C", str(bare))
                    await _run_cache_git(
                        "fetch",
                        *bare_args,
                        "fetch",
                        "--no-tags",
                        "--no-filter",
                        "--",
                        str(source),
                        expected_sha,
                    )
                    await _run_cache_git(
                        "ref",
                        *bare_args,
                        "update-ref",
                        "refs/heads/repotrial-head",
                        expected_sha,
                    )
                    await _run_cache_git(
                        "head",
                        *bare_args,
                        "symbolic-ref",
                        "HEAD",
                        "refs/heads/repotrial-head",
                    )
                    await _run_cache_git(
                        "origin",
                        *bare_args,
                        "config",
                        "remote.origin.url",
                        canonical,
                    )
                    await _run_cache_git(
                        "fsck",
                        *bare_args,
                        "fsck",
                        "--full",
                        "--no-reflogs",
                        "--no-dangling",
                    )
                    head = (
                        (
                            await _run_cache_git(
                                "resolve",
                                *bare_args,
                                "rev-parse",
                                "--verify",
                                "HEAD^{commit}",
                            )
                        )
                        .decode("ascii", errors="replace")
                        .strip()
                        .lower()
                    )
                    if head != expected_sha or not await _bare_git_is_complete(bare):
                        raise _CacheGitError("cache objects are incomplete")
                    missing = await _run_cache_git(
                        "missing",
                        *bare_args,
                        "rev-list",
                        "--objects",
                        "--missing=print",
                        expected_sha,
                    )
                    if any(
                        line.startswith("?")
                        for line in missing.decode(
                            "utf-8", errors="replace"
                        ).splitlines()
                    ):
                        raise _CacheGitError("cache has missing objects")
                    size = await _async_tree_size(bare, limit=_MAX_ENTRY_BYTES)
                    manifest = {
                        "schema_version": _CACHE_SCHEMA_VERSION,
                        "cache_key": key,
                        "canonical_url": canonical,
                        "commit_sha": expected_sha,
                        "entry_size_bytes": size,
                    }
                    (temp_path / "manifest.json").write_text(
                        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="utf-8",
                    )
                    if size > _MAX_ENTRY_BYTES:
                        return False
                    candidate_size = await _async_tree_size(
                        temp_path, limit=_MAX_ENTRY_BYTES
                    )
                    total = sum(retained_sizes)
                    if total + candidate_size > _MAX_TOTAL_BYTES:
                        return False
                    os.replace(temp_path, entry)
                    published = True
                    return True
                except asyncio.CancelledError:
                    raise
                except (OSError, _CacheGitError):
                    return False
                finally:
                    if not published:
                        _remove_owned_destination(claim)
                    _close_directory_fd(claim.directory_fd)
        except (OSError, RepoCacheError, RepoIntakeError):
            return False

    def _usable_root(self) -> bool:
        if self._root is None or os.name != "posix" or _fcntl is None:
            return False
        try:
            if self._root.exists():
                identity = self._root.lstat()
                if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
                    return False
            else:
                self._root.mkdir(parents=True, mode=0o700)
            os.chmod(self._root, 0o700)
            entries = self._root / "entries"
            if entries.exists():
                identity = entries.lstat()
                if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
                    return False
            else:
                entries.mkdir(mode=0o700)
            os.chmod(entries, 0o700)
            return True
        except OSError:
            return False

    @contextmanager
    def _try_lock(self) -> Iterator[bool]:
        assert self._root is not None
        lock_path = self._root / ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                assert _fcntl is not None
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            try:
                assert _fcntl is not None
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            finally:
                os.close(fd)


class RepoCacheError(RuntimeError):
    pass


def _canonical_url(url: str) -> str | None:
    try:
        parsed = parse_github_url(url)
    except (RepoIntakeError, ValueError):
        return None
    return parsed.url


def _cache_key(canonical_url: str, commit_sha: str) -> str:
    return hashlib.sha256((canonical_url + commit_sha).encode("utf-8")).hexdigest()


async def _valid_entry(entry: Path, url: str, sha: str, key: str) -> bool:
    try:
        if entry.is_symlink() or not entry.is_dir():
            return False
        manifest_path = entry / "manifest.json"
        repo_path = entry / "repo.git"
        if (
            manifest_path.is_symlink()
            or repo_path.is_symlink()
            or not repo_path.is_dir()
        ):
            return False
        manifest_identity = manifest_path.lstat()
        if (
            not stat.S_ISREG(manifest_identity.st_mode)
            or manifest_identity.st_size > 4096
        ):
            return False
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        canonical_manifest = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if manifest_bytes != canonical_manifest:
            return False
        if (
            set(manifest)
            != {
                "schema_version",
                "cache_key",
                "canonical_url",
                "commit_sha",
                "entry_size_bytes",
            }
            or manifest["schema_version"] != _CACHE_SCHEMA_VERSION
            or manifest["cache_key"] != key
            or manifest["canonical_url"] != url
            or manifest["commit_sha"] != sha
            or not isinstance(manifest["entry_size_bytes"], int)
        ):
            return False
        if (
            await _async_tree_size(repo_path, limit=_MAX_ENTRY_BYTES)
            != manifest["entry_size_bytes"]
            or manifest["entry_size_bytes"] > _MAX_ENTRY_BYTES
        ):
            return False
        return await _bare_git_is_complete(repo_path)
    except (OSError, ValueError, TypeError, KeyError, RepoCacheError):
        return False


async def _is_owned_shallow_entry(
    entry: Path, url: str, sha: str, key: str
) -> _DestinationClaim | None:
    claim = _claim_existing_directory(entry)
    if claim is None:
        return None
    try:
        manifest_path = entry / "manifest.json"
        repo_path = entry / "repo.git"
        shallow_path = repo_path / "shallow"
        if (
            manifest_path.is_symlink()
            or repo_path.is_symlink()
            or not repo_path.is_dir()
            or shallow_path.is_symlink()
            or not shallow_path.is_file()
        ):
            _close_directory_fd(claim.directory_fd)
            return None
        manifest_bytes = _read_bounded_regular_file(manifest_path, 4096)
        if manifest_bytes is None:
            _close_directory_fd(claim.directory_fd)
            return None
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not (
            manifest
            == {
                "schema_version": _CACHE_SCHEMA_VERSION,
                "cache_key": key,
                "canonical_url": url,
                "commit_sha": sha,
                "entry_size_bytes": manifest["entry_size_bytes"],
            }
            and isinstance(manifest["entry_size_bytes"], int)
            and manifest["entry_size_bytes"] <= _MAX_ENTRY_BYTES
        ):
            _close_directory_fd(claim.directory_fd)
            return None
        return claim
    except (OSError, ValueError, TypeError, KeyError):
        _close_directory_fd(claim.directory_fd)
        return None


def _claim_existing_directory(entry: Path) -> _DestinationClaim | None:
    try:
        identity = entry.lstat()
        if entry.is_symlink() or not stat.S_ISDIR(identity.st_mode):
            return None
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = os.open(entry, flags)
        opened = os.fstat(directory_fd)
        if (
            opened.st_dev != identity.st_dev
            or opened.st_ino != identity.st_ino
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(identity.st_mode)
        ):
            _close_directory_fd(directory_fd)
            return None
        return _DestinationClaim(
            path=entry,
            device=opened.st_dev,
            inode=opened.st_ino,
            file_type=stat.S_IFMT(opened.st_mode),
            directory_fd=directory_fd,
        )
    except OSError:
        return None


def _evict_shallow_entry(claim: _DestinationClaim) -> bool:
    try:
        return _remove_owned_destination(claim)
    finally:
        _close_directory_fd(claim.directory_fd)


def _read_bounded_regular_file(path: Path, limit: int) -> bytes | None:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            return None
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size > limit
            ):
                return None
            data = os.read(descriptor, limit + 1)
            return data if len(data) <= limit else None
        finally:
            os.close(descriptor)
    except OSError:
        return None


async def _source_cache_budget(source: Path) -> int | None:
    git_dir = source / ".git"
    if (
        not git_dir.is_dir()
        or git_dir.is_symlink()
        or (git_dir / "shallow").exists()
        or (git_dir / "shallow").is_symlink()
    ):
        return None
    try:
        await _async_tree_size(git_dir, limit=_MAX_ENTRY_BYTES)
        return await _async_tree_size(
            source,
            limit=_MAX_ENTRY_BYTES,
            multiplier=2,
            entry_budget=_SOURCE_ENTRY_BUDGET,
            ignore_symlinks=True,
        )
    except (OSError, RepoCacheError):
        return None


async def _async_tree_size(
    root: Path,
    *,
    limit: int | None = None,
    multiplier: int = 1,
    entry_budget: int = 0,
    ignore_symlinks: bool = False,
) -> int:
    total = 0
    scanned = 0
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_SCAN_ENTRIES:
                    raise RepoCacheError("cache scan entry limit")
                if scanned % _SCAN_YIELD_EVERY == 0:
                    await asyncio.sleep(0)
                identity = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(identity.st_mode):
                    if ignore_symlinks:
                        continue
                    raise RepoCacheError("unsupported cache entry")
                if stat.S_ISSOCK(identity.st_mode):
                    raise RepoCacheError("unsupported cache entry")
                if stat.S_ISDIR(identity.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(identity.st_mode):
                    total += identity.st_size * multiplier + entry_budget
                    if limit is not None and total > limit:
                        raise RepoCacheError("cache size limit")
                else:
                    raise RepoCacheError("unsupported cache entry")
    return total


async def _bare_git_is_complete(repo: Path) -> bool:
    try:
        await _async_tree_size(repo, limit=_MAX_ENTRY_BYTES)
    except (OSError, RepoCacheError):
        return False
    if (repo / "objects" / "info" / "alternates").exists() or (
        repo / "shallow"
    ).exists():
        return False
    try:
        config = (repo / "config").read_text(encoding="utf-8")
    except OSError:
        return False
    normalized = config.casefold()
    return not (
        "promisor" in normalized
        or "partialclone" in normalized
        or "alternates" in normalized
    )


async def _workspace_git_is_complete(workspace: Path) -> bool:
    git_dir = workspace / ".git"
    if not git_dir.is_dir():
        return False
    if (git_dir / "objects" / "info" / "alternates").exists() or (
        git_dir / "shallow"
    ).exists():
        return False
    try:
        await _async_tree_size(git_dir, limit=_MAX_ENTRY_BYTES)
        config = (git_dir / "config").read_text(encoding="utf-8")
    except (OSError, RepoCacheError):
        return False
    normalized = config.casefold()
    return (
        "promisor" not in normalized
        and "partialclone" not in normalized
        and "alternates" not in normalized
    )


_MAX_CACHE_OUTPUT_BYTES = 65_536


async def _read_cache_stream(stream: asyncio.StreamReader) -> bytes:
    retained = bytearray()
    while True:
        chunk = await stream.read(8_192)
        if not chunk:
            return bytes(retained)
        remaining = _MAX_CACHE_OUTPUT_BYTES - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])


async def _collect_cache_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes, int]:
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    if stdout_stream is None or stderr_stream is None:
        raise OSError("cache git subprocess pipes are unavailable")
    stdout_task = asyncio.create_task(_read_cache_stream(stdout_stream))
    stderr_task = asyncio.create_task(_read_cache_stream(stderr_stream))
    try:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        returncode = await process.wait()
        return stdout, stderr, returncode
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


async def _run_cache_git(operation: str, *arguments: str) -> bytes:
    process: asyncio.subprocess.Process | None = None
    try:
        environment = _git_environment()
        async with asyncio.timeout(_CACHE_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                "git",
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            stdout, _stderr, returncode = await _collect_cache_output(process)
    except asyncio.CancelledError:
        if process is not None:
            await _kill_and_reap(process)
        raise
    except TimeoutError:
        if process is not None:
            await _kill_and_reap(process)
        raise RepoCacheError(f"{operation}_timeout") from None
    except OSError:
        if process is not None:
            await _kill_and_reap(process)
        raise RepoCacheError(f"{operation}_io") from None
    if process is None or returncode != 0:
        raise _CacheGitError(operation)
    return stdout
