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


def _supports_fd_anchored_cleanup() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.listdir in os.supports_fd
        and os.stat in os.supports_follow_symlinks
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )


def _assert_normal_clone_cleanup_result(destination: Path) -> None:
    if _supports_fd_anchored_cleanup():
        assert not destination.exists()
    else:
        assert destination.is_dir()


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


def test_clone_and_resolve_checks_out_a_requested_full_sha_detached(
    local_repository: tuple[Path, str, str], tmp_path: Path
) -> None:
    source, first_commit, second_commit = local_repository
    destination = tmp_path / "historical commit destination"

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve(str(source), destination, requested_ref=first_commit)
    )

    assert second_commit != first_commit
    assert commit_sha == first_commit
    assert local_path == destination.resolve()
    assert _git(local_path, "rev-parse", "HEAD^{commit}") == first_commit
    detached = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=local_path,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert detached.returncode == 1


def test_local_full_sha_keeps_filtered_clone_then_checks_out_requested_sha(
    monkeypatch: pytest.MonkeyPatch,
    local_repository: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    source, first_commit, _second_commit = local_repository
    destination = tmp_path / "local exact-sha argv destination"
    invocations: list[tuple[object, ...]] = []

    async def fake_create_subprocess_exec(
        *arguments: object, **keywords: object
    ) -> _FakeProcess:
        invocations.append(arguments)
        if "clone" in arguments:
            _assert_clone_invocation(arguments, keywords, source, destination)
            return _FakeProcess(stdout=_FakeStream(), stderr=_FakeStream())
        if "checkout" in arguments:
            assert arguments == (
                "git",
                "-C",
                str(destination),
                "checkout",
                "--detach",
                first_commit,
            )
            return _FakeProcess(stdout=_FakeStream(), stderr=_FakeStream())
        assert arguments == (
            "git",
            "-C",
            str(destination),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        return _FakeProcess(
            stdout=_FakeStream([f"{first_commit}\n".encode()]),
            stderr=_FakeStream(),
        )

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve(str(source), destination, requested_ref=first_commit)
    )

    assert len(invocations) == 3
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
    _assert_normal_clone_cleanup_result(destination)


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
    _assert_normal_clone_cleanup_result(destination)


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
        self.killed = False
        self.returncode: int | None = None
        self.waited = False
        self.stdout = _FakeStream(block=True)
        self.stderr = _FakeStream(block=True)

    async def wait(self) -> int:
        self.waited = True
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _FakeStream:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        error: OSError | None = None,
        block: bool = False,
    ) -> None:
        self._chunks = list(chunks or [])
        self._error = error
        self._block = block
        self.started = asyncio.Event()
        self.read_calls = 0
        self.requested_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        self.requested_sizes.append(size)
        self.started.set()
        if self._block:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: _FakeStream,
        stderr: _FakeStream,
        returncode: int | None = 0,
        wait_error: OSError | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self._expected_returncode = returncode
        self._wait_error = wait_error
        self.returncode: int | None = None
        self.wait_calls = 0
        self.killed = False

    async def wait(self) -> int:
        self.wait_calls += 1
        if self._wait_error is not None:
            raise self._wait_error
        if self.returncode is None:
            self.returncode = self._expected_returncode
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


def _patch_fake_git_process(
    monkeypatch: pytest.MonkeyPatch, process: _FakeProcess
) -> None:
    async def fake_create_subprocess_exec(
        *_arguments: object, **_keywords: object
    ) -> _FakeProcess:
        return process

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )


def test_git_stderr_reader_is_bounded_and_process_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = b"credential=do-not-persist"
    process = _FakeProcess(
        stdout=_FakeStream(),
        stderr=_FakeStream(
            [secret + b"x" * github._MAX_GIT_OUTPUT_BYTES, b"tail-after-limit"]
        ),
        returncode=128,
    )
    _patch_fake_git_process(monkeypatch, process)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github._run_git("clone"))

    assert raised.value.failure_class == "unknown"
    assert secret.decode() not in str(raised.value)
    assert process.wait_calls >= 1
    assert process.killed is False
    assert process.stderr.read_calls >= 2
    assert all(
        size <= github._GIT_READ_CHUNK_BYTES for size in process.stderr.requested_sizes
    )


def test_git_readers_bound_simultaneous_oversized_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=_FakeStream([b"o" * (github._MAX_GIT_OUTPUT_BYTES + 1)]),
        stderr=_FakeStream([b"e" * (github._MAX_GIT_OUTPUT_BYTES + 1)]),
        returncode=0,
    )
    _patch_fake_git_process(monkeypatch, process)

    stdout = asyncio.run(github._run_git("resolve"))

    assert stdout == b"o" * github._MAX_GIT_OUTPUT_BYTES
    assert process.wait_calls >= 1
    assert process.killed is False


def test_git_reader_timeout_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=_FakeStream(block=True),
        stderr=_FakeStream(block=True),
    )
    _patch_fake_git_process(monkeypatch, process)
    monkeypatch.setattr(github, "COMMAND_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github._run_git("clone"))

    assert raised.value.operation == "clone_timeout"
    assert process.killed is True
    assert process.wait_calls >= 1


def test_git_reader_cancellation_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=_FakeStream(block=True),
        stderr=_FakeStream(block=True),
    )
    _patch_fake_git_process(monkeypatch, process)

    async def cancel_run() -> None:
        task = asyncio.create_task(github._run_git("clone"))
        try:
            await asyncio.wait_for(process.stdout.started.wait(), timeout=0.1)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())

    assert process.killed is True
    assert process.wait_calls >= 1


@pytest.mark.parametrize("failed_stream", ["stdout", "stderr"])
def test_git_reader_oserror_is_local_io_and_reaped(
    monkeypatch: pytest.MonkeyPatch, failed_stream: str
) -> None:
    secret = "SECRET reader failure"
    failure = OSError(secret)
    stdout = _FakeStream(error=failure) if failed_stream == "stdout" else _FakeStream()
    stderr = _FakeStream(error=failure) if failed_stream == "stderr" else _FakeStream()
    process = _FakeProcess(stdout=stdout, stderr=stderr)
    _patch_fake_git_process(monkeypatch, process)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github._run_git("clone"))

    assert raised.value.operation == "clone_io"
    assert raised.value.failure_class == "local_io"
    assert secret not in str(raised.value)
    assert process.killed is True
    assert process.wait_calls >= 1


def test_git_wait_oserror_is_local_io_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=_FakeStream(),
        stderr=_FakeStream(),
        wait_error=OSError("SECRET wait failure"),
    )
    _patch_fake_git_process(monkeypatch, process)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github._run_git("clone"))

    assert raised.value.operation == "clone_io"
    assert raised.value.failure_class == "local_io"
    assert "SECRET" not in str(raised.value)
    assert process.killed is True
    assert process.wait_calls >= 2


def test_git_nonzero_stderr_uses_sanitized_class_not_local_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=_FakeStream(),
        stderr=_FakeStream([b"fatal: unable to access: Failed to connect"]),
        returncode=128,
    )
    _patch_fake_git_process(monkeypatch, process)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github._run_git("clone"))

    assert raised.value.operation == "clone"
    assert raised.value.returncode == 128
    assert raised.value.failure_class == "transport"
    assert process.wait_calls >= 1
    assert process.killed is False


def test_git_spawn_oserror_is_local_io_without_raw_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_spawn_error(*_arguments: object, **_keywords: object) -> None:
        raise OSError("SECRET spawn failure")

    monkeypatch.setattr(github.asyncio, "create_subprocess_exec", raise_spawn_error)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github._run_git("clone"))

    assert raised.value.operation == "clone_io"
    assert raised.value.failure_class == "local_io"
    assert "SECRET" not in str(raised.value)


def _assert_clone_invocation(
    arguments: tuple[object, ...],
    keywords: dict[str, object],
    source: Path | str,
    destination: Path,
    *,
    requires_http_1_1: bool = False,
) -> None:
    expected_arguments: list[object] = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
    ]
    if requires_http_1_1:
        expected_arguments.extend(("-c", "http.version=HTTP/1.1"))
    expected_arguments.extend(
        (
            "clone",
            "--filter=blob:none",
            "--",
            str(source.resolve()) if isinstance(source, Path) else source,
            str(destination),
        )
    )
    assert arguments == tuple(expected_arguments)
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


def test_remote_clone_uses_explicit_http_1_1_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = "https://github.com/Owner/Project"
    destination = tmp_path / "remote destination"
    expected_sha = "a" * 40

    async def fake_create_subprocess_exec(
        *arguments: object, **keywords: object
    ) -> _FakeProcess:
        if "clone" in arguments:
            _assert_clone_invocation(
                arguments, keywords, source, destination, requires_http_1_1=True
            )
            return _FakeProcess(stdout=_FakeStream(), stderr=_FakeStream())
        assert "rev-parse" in arguments
        return _FakeProcess(
            stdout=_FakeStream([f"{expected_sha}\n".encode()]),
            stderr=_FakeStream(),
        )

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    commit_sha, local_path = asyncio.run(github.clone_and_resolve(source, destination))

    assert commit_sha == expected_sha
    assert local_path == destination.resolve()


def test_remote_full_sha_fetches_only_exact_commit_in_one_pack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = "https://github.com/Owner/Project"
    destination = tmp_path / "remote exact-sha destination"
    expected_sha = "a" * 40
    invocations: list[tuple[object, ...]] = []

    async def fake_create_subprocess_exec(
        *arguments: object, **_keywords: object
    ) -> _FakeProcess:
        invocations.append(arguments)
        stdout = (
            _FakeStream([f"{expected_sha}\n".encode()])
            if "rev-parse" in arguments
            else _FakeStream()
        )
        return _FakeProcess(stdout=stdout, stderr=_FakeStream())

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve(source, destination, requested_ref=expected_sha)
    )

    common = (
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "http.version=HTTP/1.1",
        "-C",
        str(destination),
    )
    assert invocations == [
        (*common, "init"),
        (
            *common,
            "fetch",
            "--depth=1",
            "--no-tags",
            "--",
            source,
            expected_sha,
        ),
        (*common, "checkout", "--detach", "FETCH_HEAD"),
        (
            "git",
            "-C",
            str(destination),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ),
    ]
    assert all("--filter=blob:none" not in command for command in invocations)
    assert commit_sha == expected_sha
    assert local_path == destination.resolve()


def test_remote_full_sha_mismatch_fails_closed_and_cleans_owned_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = "https://github.com/Owner/Project"
    destination = tmp_path / "remote mismatched exact-sha destination"
    requested_sha = "a" * 40
    resolved_sha = "b" * 40
    invocations: list[tuple[object, ...]] = []

    async def fake_create_subprocess_exec(
        *arguments: object, **_keywords: object
    ) -> _FakeProcess:
        invocations.append(arguments)
        stdout = (
            _FakeStream([f"{resolved_sha}\n".encode()])
            if "rev-parse" in arguments
            else _FakeStream()
        )
        return _FakeProcess(stdout=stdout, stderr=_FakeStream())

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(
            github.clone_and_resolve(source, destination, requested_ref=requested_sha)
        )

    assert raised.value.operation == "commit_sha_mismatch"
    assert len(invocations) == 4
    assert "init" in invocations[0]
    assert "fetch" in invocations[1]
    assert "checkout" in invocations[2]
    assert "rev-parse" in invocations[3]
    _assert_normal_clone_cleanup_result(destination)


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
    assert process.waited is True
    _assert_normal_clone_cleanup_result(destination)


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
        while not process.stdout.started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    asyncio.run(cancel_clone())

    assert process.killed is True
    assert process.waited is True
    _assert_normal_clone_cleanup_result(destination)


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


@pytest.mark.skipif(
    not _supports_fd_anchored_cleanup(), reason="safe fd-anchored cleanup unavailable"
)
def test_clone_cleanup_preserves_replacement_rebound_after_final_identity_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "destination"
    claim = github._claim_destination(destination)
    moved_owned_destination = tmp_path / "moved owned destination"
    sentinel = destination / "replacement-sentinel.txt"
    original_lstat = Path.lstat
    rebound = False

    def rebind_after_identity_check(self: Path) -> os.stat_result:
        nonlocal rebound
        identity = original_lstat(self)
        if self == destination and not rebound:
            rebound = True
            destination.rename(moved_owned_destination)
            destination.mkdir()
            sentinel.write_text("do not delete", encoding="utf-8")
        return identity

    monkeypatch.setattr(Path, "lstat", rebind_after_identity_check)

    try:
        github._remove_owned_destination(claim)
    finally:
        github._close_directory_fd(claim.directory_fd)

    assert rebound is True
    assert sentinel.read_text(encoding="utf-8") == "do not delete"


@pytest.mark.skipif(
    not _supports_fd_anchored_cleanup(), reason="safe fd-anchored cleanup unavailable"
)
def test_clone_cleanup_removes_owned_nested_content_before_root_path_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "destination"
    claim = github._claim_destination(destination)
    nested = destination / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "owned.txt").write_text("remove me", encoding="utf-8")
    original_lstat = Path.lstat

    def require_empty_root_before_identity_check(self: Path) -> os.stat_result:
        if self == destination:
            assert not any(destination.iterdir())
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", require_empty_root_before_identity_check)

    try:
        github._remove_owned_destination(claim)
    finally:
        github._close_directory_fd(claim.directory_fd)

    assert not destination.exists()


@pytest.mark.skipif(
    not _supports_fd_anchored_cleanup(), reason="safe fd-anchored cleanup unavailable"
)
def test_clone_cleanup_unlinks_child_symlink_without_following_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "destination"
    claim = github._claim_destination(destination)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("preserve me", encoding="utf-8")
    (destination / "external-link").symlink_to(external, target_is_directory=True)

    def forbid_path_recursive_cleanup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup used path-recursive root deletion")

    monkeypatch.setattr(shutil, "rmtree", forbid_path_recursive_cleanup)

    try:
        github._remove_owned_destination(claim)
    finally:
        github._close_directory_fd(claim.directory_fd)

    assert sentinel.read_text(encoding="utf-8") == "preserve me"
    assert not destination.exists()


def test_clone_cleanup_preserves_nonempty_claim_when_fd_operations_are_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "destination"
    claim = github._claim_destination(destination)
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("preserve me", encoding="utf-8")
    monkeypatch.setattr(github.os, "supports_dir_fd", set())
    monkeypatch.setattr(github.os, "supports_fd", set())
    monkeypatch.setattr(github.os, "supports_follow_symlinks", set())

    try:
        github._remove_owned_destination(claim)
    finally:
        github._close_directory_fd(claim.directory_fd)

    assert sentinel.read_text(encoding="utf-8") == "preserve me"


def test_clone_failure_preserves_empty_root_when_fd_cleanup_is_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"

    async def fail_clone(*_args: object) -> bytes:
        raise github.RepoIntakeError("clone")

    def forbid_path_recursive_cleanup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup used path-recursive root deletion")

    monkeypatch.setattr(github.os, "supports_dir_fd", set())
    monkeypatch.setattr(github.os, "supports_fd", set())
    monkeypatch.setattr(github.os, "supports_follow_symlinks", set())
    monkeypatch.setattr(github, "_run_git", fail_clone)
    monkeypatch.setattr(shutil, "rmtree", forbid_path_recursive_cleanup)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert raised.value.operation == "clone"
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_clone_claim_failure_removes_empty_root_when_fd_cleanup_is_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    original_open = os.open

    def fail_destination_open(
        path: str | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        if Path(path) == destination:
            raise OSError("SECRET directory-open failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(github.os, "supports_dir_fd", set())
    monkeypatch.setattr(github.os, "supports_fd", set())
    monkeypatch.setattr(github.os, "supports_follow_symlinks", set())
    monkeypatch.setattr(github.os, "open", fail_destination_open)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert raised.value.operation == "destination_claim"
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fd behavior")
@pytest.mark.parametrize(
    "failure_stage", ["open", "set_inheritable", "fstat", "second_lstat"]
)
def test_clone_claim_failure_removes_the_directory_it_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    original_open = os.open
    original_lstat = Path.lstat
    original_fstat = os.fstat
    destination_open_calls = 0
    destination_lstat_calls = 0
    fstat_calls = 0

    monkeypatch.setattr(github.os, "supports_dir_fd", set())
    monkeypatch.setattr(github.os, "supports_fd", set())
    monkeypatch.setattr(github.os, "supports_follow_symlinks", set())

    def fail_first_destination_open(
        path: str | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal destination_open_calls
        if Path(path) == destination:
            destination_open_calls += 1
            if destination_open_calls == 1:
                raise OSError("SECRET directory-open failure")
        return original_open(path, flags, *args, **kwargs)

    def fail_to_make_directory_fd_non_inheritable(
        _directory_fd: int, _inheritable: bool
    ) -> None:
        raise OSError("SECRET set-inheritable failure")

    def fail_first_fstat(directory_fd: int) -> os.stat_result:
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls == 1:
            raise OSError("SECRET fstat failure")
        return original_fstat(directory_fd)

    def fail_second_destination_lstat(self: Path) -> os.stat_result:
        nonlocal destination_lstat_calls
        if self == destination:
            destination_lstat_calls += 1
            if destination_lstat_calls == 2:
                raise OSError("SECRET second-lstat failure")
        return original_lstat(self)

    if failure_stage == "open":
        monkeypatch.setattr(github.os, "open", fail_first_destination_open)
    elif failure_stage == "set_inheritable":
        monkeypatch.setattr(
            github.os, "set_inheritable", fail_to_make_directory_fd_non_inheritable
        )
    elif failure_stage == "fstat":
        monkeypatch.setattr(github.os, "fstat", fail_first_fstat)
    else:
        monkeypatch.setattr(Path, "lstat", fail_second_destination_lstat)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert raised.value.operation == "destination_claim"
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert not destination.exists()


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

    _assert_normal_clone_cleanup_result(destination)


class _CommunicateOSErrorProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__(
            stdout=_FakeStream(error=OSError("SECRET communicate failure")),
            stderr=_FakeStream(),
        )


class _CommunicateFileNotFoundErrorProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__(
            stdout=_FakeStream(
                error=FileNotFoundError("SECRET communicate file-not-found failure")
            ),
            stderr=_FakeStream(),
        )


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
    _assert_normal_clone_cleanup_result(destination)


def test_clone_sanitizes_communicate_file_not_found_and_cleans_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    process = _CommunicateFileNotFoundErrorProcess()

    async def fake_create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> _CommunicateFileNotFoundErrorProcess:
        return process

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(github.clone_and_resolve(str(source), destination))

    assert str(raised.value) == "repository intake failed: clone_io"
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert process.killed is True
    assert process.wait_calls >= 1
    _assert_normal_clone_cleanup_result(destination)


class _KillOSErrorProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStream(block=True)
        self.stderr = _FakeStream(block=True)
        self.communicate_started = self.stdout.started
        self.returncode: int | None = None

    async def wait(self) -> int:
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
    _assert_normal_clone_cleanup_result(destination)


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

    _assert_normal_clone_cleanup_result(destination)


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific drive path form")
def test_clone_source_accepts_an_existing_windows_drive_path_with_double_slash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    windows_style_source = str(source).replace(":\\", "://", 1)

    assert github._normalize_clone_source(windows_style_source) == str(source.resolve())
