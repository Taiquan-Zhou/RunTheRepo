import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from repotrial.domain.models import PinnedRepo, RepoRef
from repotrial.intake import repository_cache
from repotrial.intake.repository_cache import RepositoryCache


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


@pytest.fixture
def source_repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "RepoTrial")
    _git(source, "config", "user.email", "repotrial@example.test")
    (source / "README.md").write_text("one\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "one")
    sha = _git(source, "rev-parse", "HEAD")
    return source, sha


@pytest.fixture
def two_commit_repository(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "two-commit-source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "RepoTrial")
    _git(source, "config", "user.email", "repotrial@example.test")
    (source / "README.md").write_text("one\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "one")
    first_sha = _git(source, "rev-parse", "HEAD")
    (source / "README.md").write_text("two\n", encoding="utf-8")
    _git(source, "commit", "-am", "two")
    second_sha = _git(source, "rev-parse", "HEAD")
    return source, first_sha, second_sha


def _pinned(source: Path, sha: str) -> PinnedRepo:
    return PinnedRepo(
        repo=RepoRef(
            url="https://github.com/example/project",
            owner="example",
            repo="project",
            requested_ref=sha,
        ),
        commit_sha=sha,
        local_path=source,
    )


def test_store_then_restore_is_clean_and_does_not_reuse_source(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache = RepositoryCache(tmp_path / "cache")

    assert asyncio.run(cache.store(_pinned(source, sha)))

    (source / "README.md").write_text("mutated\n", encoding="utf-8")
    restored = asyncio.run(
        cache.restore("https://github.com/example/project", sha, tmp_path / "workspace")
    )

    assert restored is not None
    assert restored.commit_sha == sha
    assert restored.local_path == (tmp_path / "workspace").resolve()
    assert (restored.local_path / "README.md").read_text(encoding="utf-8") == "one\n"
    assert _git(restored.local_path, "rev-parse", "HEAD") == sha
    assert _git(restored.local_path, "remote", "get-url", "origin") == (
        "https://github.com/example/project"
    )
    assert not (
        restored.local_path / ".git" / "objects" / "info" / "alternates"
    ).exists()


def test_restore_keeps_parent_objects_and_is_not_shallow(
    two_commit_repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    source, first_sha, second_sha = two_commit_repository
    cache = RepositoryCache(tmp_path / "cache")

    assert asyncio.run(cache.store(_pinned(source, second_sha)))
    restored = asyncio.run(
        cache.restore(
            "https://github.com/example/project", second_sha, tmp_path / "workspace"
        )
    )

    assert restored is not None
    assert _git(restored.local_path, "rev-parse", "--is-shallow-repository") == "false"
    assert _git(restored.local_path, "cat-file", "-e", f"{first_sha}^{{commit}}") == ""
    assert _git(restored.local_path, "rev-list", "--count", "HEAD") == "2"


def test_source_shallow_repository_is_not_cached(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    (source / ".git" / "shallow").write_text(f"{sha}\n", encoding="ascii")
    cache_root = tmp_path / "cache"

    assert not asyncio.run(RepositoryCache(cache_root).store(_pinned(source, sha)))
    assert not list((cache_root / "entries").glob("*"))


def test_fifo_manifest_is_rejected_without_blocking(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache = RepositoryCache(cache_root)
    assert asyncio.run(cache.store(_pinned(source, sha)))
    entry = next((cache_root / "entries").iterdir())
    manifest = entry / "manifest.json"
    manifest.unlink()
    os.mkfifo(manifest)

    restored = asyncio.run(
        asyncio.wait_for(
            cache.restore("https://github.com/example/project", sha, tmp_path / "fifo"),
            timeout=1,
        )
    )

    assert restored is None


def test_shallow_entry_replacement_race_keeps_replaced_directory(
    source_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache = RepositoryCache(cache_root)
    assert asyncio.run(cache.store(_pinned(source, sha)))
    entry = next((cache_root / "entries").iterdir())
    (entry / "repo.git" / "shallow").write_text(f"{sha}\n", encoding="ascii")
    claim = asyncio.run(
        repository_cache._is_owned_shallow_entry(
            entry,
            "https://github.com/example/project",
            sha,
            repository_cache._cache_key("https://github.com/example/project", sha),
        )
    )
    assert claim is not None
    replacement = entry.with_name("replacement-after-validation")
    entry.rename(replacement)
    entry.mkdir()
    (entry / "sentinel").write_text("keep", encoding="ascii")

    assert not repository_cache._evict_shallow_entry(claim)
    assert (entry / "sentinel").read_text(encoding="ascii") == "keep"
    repository_cache._close_directory_fd(claim.directory_fd)


def test_shallow_cache_entry_is_miss_and_can_be_replaced(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache = RepositoryCache(cache_root)
    assert asyncio.run(cache.store(_pinned(source, sha)))
    entry = next((cache_root / "entries").iterdir())
    (entry / "repo.git" / "shallow").write_text(f"{sha}\n", encoding="ascii")

    assert (
        asyncio.run(
            cache.restore("https://github.com/example/project", sha, tmp_path / "miss")
        )
        is None
    )
    assert asyncio.run(cache.store(_pinned(source, sha)))
    assert not (entry / "repo.git" / "shallow").exists()


def test_cached_pinner_hit_does_not_call_network_pinner(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache = RepositoryCache(tmp_path / "cache")
    assert asyncio.run(cache.store(_pinned(source, sha)))
    called = False

    async def network_pinner(
        _url: str, _destination: Path, _requested_ref: str | None
    ) -> PinnedRepo:
        nonlocal called
        called = True
        raise AssertionError("network pinner must not run on cache hit")

    restored = asyncio.run(
        cache.pin(
            "https://github.com/example/project",
            tmp_path / "workspace",
            sha,
            network_pinner,
        )
    )

    assert restored.commit_sha == sha
    assert not called


def test_bad_manifest_is_cache_miss_and_does_not_follow_external_symlink(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache = RepositoryCache(cache_root)
    assert asyncio.run(cache.store(_pinned(source, sha)))
    entry = next((cache_root / "entries").iterdir())
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("safe\n", encoding="utf-8")
    (entry / "manifest.json").write_text("{}\n", encoding="utf-8")
    (entry / "repo.git").rename(entry / "repo.git.real")
    (entry / "repo.git").symlink_to(sentinel)

    restored = asyncio.run(
        cache.restore("https://github.com/example/project", sha, tmp_path / "workspace")
    )

    assert restored is None
    assert sentinel.read_text(encoding="utf-8") == "safe\n"


def test_store_ignores_worktree_symlinks_and_only_caches_git_objects(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    target = tmp_path / "outside"
    target.write_text("outside\n", encoding="utf-8")
    (source / "linked").symlink_to(target)
    cache = RepositoryCache(tmp_path / "cache")

    assert asyncio.run(cache.store(_pinned(source, sha)))
    assert target.read_text(encoding="utf-8") == "outside\n"


def test_capacity_limit_skips_publish_without_affecting_source_pin(
    source_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = source_repository
    monkeypatch.setattr(repository_cache, "_MAX_ENTRIES", 0)
    cache = RepositoryCache(tmp_path / "cache")

    assert not asyncio.run(cache.store(_pinned(source, sha)))
    assert not list((tmp_path / "cache" / "entries").glob("*"))


def test_different_sha_is_a_cache_miss_and_calls_pinner(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, first_sha = source_repository
    cache = RepositoryCache(tmp_path / "cache")
    assert asyncio.run(cache.store(_pinned(source, first_sha)))
    (source / "README.md").write_text("two\n", encoding="utf-8")
    _git(source, "commit", "-am", "two")
    second_sha = _git(source, "rev-parse", "HEAD")
    called = False

    async def pinner(
        _url: str, _destination: Path, requested_ref: str | None
    ) -> PinnedRepo:
        nonlocal called
        called = True
        assert requested_ref == second_sha
        return _pinned(source, second_sha)

    result = asyncio.run(
        cache.pin(
            "https://github.com/example/project",
            tmp_path / "workspace",
            second_sha,
            pinner,
        )
    )

    assert result.commit_sha == second_sha
    assert called


def test_write_failure_does_not_change_successful_pin(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache_root.write_text("not a directory", encoding="utf-8")
    cache = RepositoryCache(cache_root)
    called = False

    async def pinner(
        _url: str, _destination: Path, requested_ref: str | None
    ) -> PinnedRepo:
        nonlocal called
        called = True
        return _pinned(source, sha)

    result = asyncio.run(
        cache.pin(
            "https://github.com/example/project",
            tmp_path / "workspace",
            sha,
            pinner,
        )
    )

    assert result.commit_sha == sha
    assert called


def test_corrupt_cached_object_is_rejected(
    source_repository: tuple[Path, str], tmp_path: Path
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache = RepositoryCache(cache_root)
    assert asyncio.run(cache.store(_pinned(source, sha)))
    entry = next((cache_root / "entries").iterdir())
    object_files = [
        path for path in (entry / "repo.git" / "objects").glob("??/*") if path.is_file()
    ]
    assert object_files
    object_files[0].unlink()

    assert (
        asyncio.run(
            cache.restore(
                "https://github.com/example/project", sha, tmp_path / "workspace"
            )
        )
        is None
    )


def test_cancelled_store_cleans_only_its_temporary_entry(
    source_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    cache = RepositoryCache(cache_root)
    entered = asyncio.Event()

    async def blocked_git(_operation: str, *_arguments: str) -> bytes:
        entered.set()
        await asyncio.sleep(60)
        return b""

    monkeypatch.setattr(repository_cache, "_run_cache_git", blocked_git)

    async def run() -> None:
        task = asyncio.create_task(cache.store(_pinned(source, sha)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    entries = cache_root / "entries"
    assert not list(entries.glob(".tmp-*"))


def test_temp_claim_failure_does_not_break_successful_pin(
    source_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = source_repository
    cache = RepositoryCache(tmp_path / "cache")
    original_claim = repository_cache._claim_destination

    def fail_temp_claim(path: Path):
        if path.name.startswith(".tmp-"):
            raise repository_cache.RepoIntakeError("destination_create")
        return original_claim(path)

    monkeypatch.setattr(repository_cache, "_claim_destination", fail_temp_claim)
    called = False

    async def pinner(
        _url: str, _destination: Path, _requested_ref: str | None
    ) -> PinnedRepo:
        nonlocal called
        called = True
        return _pinned(source, sha)

    result = asyncio.run(
        cache.pin(
            "https://github.com/example/project",
            tmp_path / "workspace",
            sha,
            pinner,
        )
    )

    assert result.commit_sha == sha
    assert called


def test_oversized_source_is_rejected_before_fetch(
    source_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = source_repository
    cache = RepositoryCache(tmp_path / "cache")
    calls: list[str] = []

    async def fake_git(operation: str, *_arguments: str) -> bytes:
        calls.append(operation)
        return (sha + "\n").encode()

    monkeypatch.setattr(repository_cache, "_run_cache_git", fake_git)
    monkeypatch.setattr(repository_cache, "_MAX_ENTRY_BYTES", 1)

    assert not asyncio.run(cache.store(_pinned(source, sha)))
    assert "fetch" not in calls


def test_stale_temporary_entries_count_toward_total_capacity(
    source_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = source_repository
    cache_root = tmp_path / "cache"
    entries = cache_root / "entries"
    entries.mkdir(parents=True)
    stale = entries / ".tmp-stale"
    stale.mkdir()
    (stale / "payload").write_bytes(b"x" * 32)
    cache = RepositoryCache(cache_root)
    calls: list[str] = []

    async def fake_git(operation: str, *_arguments: str) -> bytes:
        calls.append(operation)
        return (sha + "\n").encode()

    monkeypatch.setattr(repository_cache, "_run_cache_git", fake_git)
    source_budget = asyncio.run(repository_cache._source_cache_budget(source))
    assert source_budget is not None
    monkeypatch.setattr(
        repository_cache,
        "_MAX_TOTAL_BYTES",
        source_budget + repository_cache._MANIFEST_BUDGET + 1,
    )

    assert not asyncio.run(cache.store(_pinned(source, sha)))
    assert "fetch" not in calls
    assert stale.exists()


def test_capacity_scan_yields_to_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(128):
        (root / f"file-{index}").write_bytes(b"x")
    monkeypatch.setattr(repository_cache, "_SCAN_YIELD_EVERY", 1)

    async def run() -> None:
        task = asyncio.create_task(repository_cache._async_tree_size(root))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
