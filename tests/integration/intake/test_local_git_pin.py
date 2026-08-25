import asyncio
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from repotrial.intake import github

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="unsupported: Git executable is required for local clone integration tests",
)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def local_repository(tmp_path: Path) -> Iterator[tuple[Path, str, str]]:
    source = tmp_path / "source repository"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "RepoTrial test")
    _git(source, "config", "user.email", "repotrial@example.test")

    (source / "README.md").write_text("first\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "first commit")
    first_commit = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "stable", first_commit)
    _git(source, "tag", "v1.0.0", first_commit)

    (source / "README.md").write_text("second\n", encoding="utf-8")
    _git(source, "commit", "-am", "second commit")
    second_commit = _git(source, "rev-parse", "HEAD")

    yield source, first_commit, second_commit


def test_clone_and_resolve_pins_local_default_head(
    local_repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    source, _first_commit, second_commit = local_repository
    destination = tmp_path / "destination with spaces"

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve(str(source), destination)
    )

    assert commit_sha == second_commit
    assert len(commit_sha) == 40
    assert all(character in "0123456789abcdef" for character in commit_sha)
    assert local_path == destination.resolve()
    assert _git(local_path, "rev-parse", "HEAD") == second_commit


@pytest.mark.parametrize("requested_ref", ["stable", "v1.0.0"])
def test_clone_and_resolve_pins_requested_branch_or_tag(
    local_repository: tuple[Path, str, str], tmp_path: Path, requested_ref: str
) -> None:
    source, first_commit, _second_commit = local_repository
    destination = tmp_path / f"destination-{requested_ref}"

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve(str(source), destination, requested_ref=requested_ref)
    )

    assert commit_sha == first_commit
    assert local_path == destination.resolve()


def test_clone_and_resolve_removes_owned_destination_after_missing_ref(
    local_repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    source, _first_commit, _second_commit = local_repository
    destination = tmp_path / "missing reference destination"

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(
            github.clone_and_resolve(str(source), destination, "does-not-exist")
        )

    assert "does-not-exist" not in str(raised.value)
    assert str(source) not in str(raised.value)
    assert not destination.exists()


def test_clone_and_resolve_preserves_preexisting_destination(
    local_repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    source, _first_commit, _second_commit = local_repository
    destination = tmp_path / "existing destination"
    destination.mkdir()
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("preserve me", encoding="utf-8")

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert sentinel.read_text(encoding="utf-8") == "preserve me"


def test_clone_and_resolve_removes_owned_destination_after_non_git_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "not a repository"
    source.mkdir()
    destination = tmp_path / "destination"

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert str(source) not in str(raised.value)
    assert not destination.exists()


@pytest.mark.parametrize(
    "source",
    [
        "file:///tmp/repository",
        "git@github.com:owner/repository.git",
        "ssh://github.com/owner/repository.git",
        "git://github.com/owner/repository.git",
        "ext::echo unsafe",
        "https://user@github.com/owner/repository",
        "https://github.com/owner/repository?ref=main",
        "https://github.com/owner/repository?",
        "https://github.com/owner/repository#",
    ],
)
def test_clone_and_resolve_rejects_unsafe_transport_before_creating_destination(
    monkeypatch: pytest.MonkeyPatch, source: str, tmp_path: Path
) -> None:
    destination = tmp_path / "destination"

    async def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsafe source reached subprocess creation")

    monkeypatch.setattr(github.asyncio, "create_subprocess_exec", unexpected_spawn)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert not destination.exists()


def test_pin_repository_builds_a_canonical_pinned_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "pinned repository"

    async def fake_clone_and_resolve(
        source: str, dest: Path, requested_ref: str | None = None
    ) -> tuple[str, Path]:
        assert source == "https://github.com/Owner/Project"
        assert dest == destination
        assert requested_ref == "release/v1"
        return "a" * 40, destination.resolve()

    monkeypatch.setattr(github, "clone_and_resolve", fake_clone_and_resolve)

    pinned = asyncio.run(
        github.pin_repository(
            "https://GitHub.com/Owner/Project.git",
            destination,
            requested_ref="release/v1",
        )
    )

    assert pinned.repo.url == "https://github.com/Owner/Project"
    assert pinned.repo.owner == "Owner"
    assert pinned.repo.repo == "Project"
    assert pinned.repo.requested_ref == "release/v1"
    assert pinned.commit_sha == "a" * 40
    assert pinned.local_path == destination.resolve()


class _HangingProcess:
    def __init__(self) -> None:
        self.communicate_calls = 0
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self.killed:
            return b"", b""
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _assert_clone_invocation(
    arguments: tuple[object, ...],
    keywords: dict[str, object],
    source: Path,
    destination: Path,
) -> None:
    assert arguments == (
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "clone",
        "--filter=blob:none",
        "--",
        str(source.resolve()),
        str(destination),
    )
    assert keywords["stdin"] == asyncio.subprocess.DEVNULL
    assert keywords["stdout"] == asyncio.subprocess.PIPE
    assert keywords["stderr"] == asyncio.subprocess.PIPE
    environment = keywords["env"]
    assert isinstance(environment, dict)
    assert "GIT_DIR" not in environment
    assert "GIT_ASKPASS" not in environment
    assert "SSH_ASKPASS" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"


def test_clone_timeout_kills_and_reaps_direct_child(
    monkeypatch: pytest.MonkeyPatch,
    local_repository: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    source, _first_commit, _second_commit = local_repository
    destination = tmp_path / "timeout destination"
    process = _HangingProcess()

    async def fake_create_subprocess_exec(
        *arguments: object, **keywords: object
    ) -> _HangingProcess:
        _assert_clone_invocation(arguments, keywords, source, destination)
        return process

    monkeypatch.setattr(github, "COMMAND_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setenv("GIT_DIR", "host-git-dir")
    monkeypatch.setenv("GIT_ASKPASS", "host-git-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "host-ssh-askpass")
    monkeypatch.setenv("GCM_INTERACTIVE", "allow")
    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert process.killed is True
    assert process.communicate_calls == 2
    assert not destination.exists()


def test_clone_cancellation_kills_and_reaps_direct_child(
    monkeypatch: pytest.MonkeyPatch,
    local_repository: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    source, _first_commit, _second_commit = local_repository
    destination = tmp_path / "cancelled destination"
    process = _HangingProcess()

    async def fake_create_subprocess_exec(
        *arguments: object, **keywords: object
    ) -> _HangingProcess:
        _assert_clone_invocation(arguments, keywords, source, destination)
        return process

    monkeypatch.setenv("GIT_DIR", "host-git-dir")
    monkeypatch.setenv("GIT_ASKPASS", "host-git-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "host-ssh-askpass")
    monkeypatch.setenv("GCM_INTERACTIVE", "allow")

    async def cancel_clone() -> None:
        task = asyncio.create_task(github.clone_and_resolve(str(source), destination))
        while process.communicate_calls == 0:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    asyncio.run(cancel_clone())

    assert process.killed is True
    assert process.communicate_calls == 2
    assert not destination.exists()


def test_clone_does_not_follow_a_final_component_symlink_inserted_during_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    attacker_target = tmp_path / "attacker target"
    original_resolve = Path.resolve
    original_mkdir = Path.mkdir

    def insert_symlink_during_resolve(self: Path, strict: bool = False) -> Path:
        if (
            self == destination
            and not destination.exists()
            and not destination.is_symlink()
        ):
            destination.symlink_to(attacker_target, target_is_directory=True)
        return original_resolve(self, strict=strict)

    def insert_symlink_during_claim(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if (
            self == destination
            and not destination.exists()
            and not destination.is_symlink()
        ):
            destination.symlink_to(attacker_target, target_is_directory=True)
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    async def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("claim race reached subprocess creation")

    monkeypatch.setattr(Path, "resolve", insert_symlink_during_resolve)
    monkeypatch.setattr(Path, "mkdir", insert_symlink_during_claim)
    monkeypatch.setattr(github.asyncio, "create_subprocess_exec", unexpected_spawn)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert destination.is_symlink()
    assert not attacker_target.exists()


def test_clone_cleanup_preserves_a_replacement_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    sentinel = destination / "replacement-sentinel.txt"

    async def replace_destination_then_fail(*_args: object) -> bytes:
        shutil.rmtree(destination)
        destination.mkdir()
        sentinel.write_text("do not delete", encoding="utf-8")
        raise github.RepoIntakeError("clone")

    monkeypatch.setattr(github, "_run_git", replace_destination_then_fail)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert sentinel.read_text(encoding="utf-8") == "do not delete"


def test_clone_sanitizes_destination_parent_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    original_resolve = Path.resolve

    def raise_for_destination_parent(self: Path, strict: bool = False) -> Path:
        if self == destination.parent:
            raise OSError("SECRET destination parent")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", raise_for_destination_parent)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert "SECRET" not in str(raised.value)
    assert raised.value.__suppress_context__ is True


def test_clone_times_out_when_subprocess_spawn_hangs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"

    async def hung_spawn(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    async def assert_internal_timeout() -> None:
        with pytest.raises(github.RepoIntakeError):
            await asyncio.wait_for(
                github.clone_and_resolve(str(source), destination), timeout=0.1
            )

    monkeypatch.setattr(github, "COMMAND_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(github.asyncio, "create_subprocess_exec", hung_spawn)

    asyncio.run(assert_internal_timeout())

    assert not destination.exists()


class _CommunicateOSErrorProcess:
    def __init__(self) -> None:
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        raise OSError("SECRET communicate failure")

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_clone_sanitizes_communicate_oserror_and_cleans_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    process = _CommunicateOSErrorProcess()

    async def fake_create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> _CommunicateOSErrorProcess:
        return process

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert "SECRET" not in str(raised.value)
    assert process.killed is True
    assert not destination.exists()


class _KillOSErrorProcess:
    def __init__(self) -> None:
        self.communicate_started = asyncio.Event()
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def kill(self) -> None:
        raise OSError("SECRET kill failure")


def test_clone_timeout_is_not_masked_by_kill_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    process = _KillOSErrorProcess()

    async def fake_create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> _KillOSErrorProcess:
        return process

    monkeypatch.setattr(github, "COMMAND_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(github, "REAP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert "SECRET" not in str(raised.value)
    assert not destination.exists()


def test_clone_cancellation_is_not_masked_by_kill_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    process = _KillOSErrorProcess()

    async def fake_create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> _KillOSErrorProcess:
        return process

    async def cancel_clone() -> None:
        task = asyncio.create_task(github.clone_and_resolve(str(source), destination))
        await process.communicate_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(github, "REAP_TIMEOUT_SECONDS", 0.001)

    asyncio.run(cancel_clone())

    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific drive path form")
def test_clone_source_accepts_an_existing_windows_drive_path_with_double_slash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    windows_style_source = str(source).replace(":\\", "://", 1)

    assert github._normalize_clone_source(windows_style_source) == str(source.resolve())
