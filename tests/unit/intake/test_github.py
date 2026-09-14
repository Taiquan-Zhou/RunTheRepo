import asyncio
from pathlib import Path
from typing import Literal

import pytest

from repotrial.domain.models import RepoRef
from repotrial.intake import github


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"fatal: unable to access: Could not resolve host", "dns"),
        (b"fatal: unable to access: Failed to connect", "transport"),
        (b"fatal: unable to access: Could not resolve proxy: proxy", "dns"),
        (
            b"fatal: unable to access: Recv failure: Connection reset by peer",
            "transport",
        ),
        (
            b"error: RPC failed; curl 56 Recv failure: Connection reset by peer",
            "transport",
        ),
        (b"error: RPC failed; curl 28 Operation timed out", "transport"),
        (b"fatal: unable to access: remote peer closed connection", "transport"),
        (
            b"fatal: SSL certificate problem: unable to get local issuer certificate",
            "unknown",
        ),
        (b"error: RPC failed", "unknown"),
        (b"fatal: curl failed", "unknown"),
        (b"fatal: early EOF", "unknown"),
        (
            b"fatal: unable to access: The requested URL returned error: 503",
            "http",
        ),
        (b"fatal: Authentication failed", "authentication"),
        (b"fatal: couldn't find remote ref pinned", "missing_ref"),
        (b"unrecognized secret text", "unknown"),
    ],
)
def test_git_failure_classification_is_sanitized(stderr: bytes, expected: str) -> None:
    assert github._classify_git_failure(stderr) == expected


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            (
                b"could not resolve host; authentication failed; "
                b"requested URL returned error: 503; couldn't find remote ref; "
                b"failed to connect"
            ),
            "dns",
        ),
        (
            (
                b"authentication failed; requested URL returned error: 503; "
                b"couldn't find remote ref; failed to connect"
            ),
            "authentication",
        ),
        (
            (
                b"requested URL returned error: 503; couldn't find remote ref; "
                b"failed to connect"
            ),
            "http",
        ),
        (
            b"couldn't find remote ref; failed to connect",
            "missing_ref",
        ),
        (b"failed to connect; unrelated token", "transport"),
        (b"unrecognized secret text", "unknown"),
    ],
)
def test_git_failure_classification_obeys_fixed_priority(
    stderr: bytes, expected: str
) -> None:
    assert github._classify_git_failure(stderr) == expected


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self.stdout = _FakeStream([stdout])
        self.stderr = _FakeStream([stderr])
        self.returncode: int | None = None
        self._final_returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_remote_clone_retries_after_run_git_classifies_transient_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "run git retry destination"
    expected_sha = "c" * 40
    clone_calls = 0

    async def fake_create_subprocess_exec(
        *arguments: str, **_kwargs: object
    ) -> _FakeProcess:
        nonlocal clone_calls
        if "clone" in arguments:
            clone_calls += 1
            if clone_calls == 1:
                return _FakeProcess(
                    b"",
                    b"error: RPC failed; curl 56 Recv failure: Connection reset by peer",
                    128,
                )
            return _FakeProcess(b"", b"", 0)
        return _FakeProcess(f"{expected_sha}\n".encode(), b"", 0)

    monkeypatch.setattr(
        github.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(github, "NETWORK_RETRY_DELAY_SECONDS", 0)

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve("https://github.com/Owner/Project", destination)
    )

    assert commit_sha == expected_sha
    assert local_path == destination.resolve()
    assert clone_calls == 2


def test_parse_github_url_canonicalizes_case_and_literal_git_suffix() -> None:
    assert callable(getattr(github, "parse_github_url", None))

    repo = github.parse_github_url("https://GitHub.com/Owner/Project.git")

    assert repo == RepoRef(
        url="https://github.com/Owner/Project",
        owner="Owner",
        repo="Project",
        requested_ref=None,
    )


def test_parse_github_url_preserves_requested_branch_or_tag_exactly() -> None:
    repo = github.parse_github_url(
        "https://github.com/Owner/Project", requested_ref="release/v1"
    )

    assert repo.requested_ref == "release/v1"


@pytest.mark.parametrize(
    ("url", "requested_ref"),
    [
        ("http://github.com/owner/repo", None),
        ("https://user@github.com/owner/repo", None),
        ("https://github.com:443/owner/repo", None),
        ("https://github.com/owner/repo/", None),
        ("https://github.com/owner//repo", None),
        ("https://github.com/owner/repo?ref=main", None),
        ("https://github.com/owner/repo#readme", None),
        ("https://github.com/owner/repo?", None),
        ("https://github.com/owner/repo#", None),
        ("https://github.com/owner/repo%2Egit", None),
        ("https://github.com/owner/repo with space", None),
        ("https://github.com/owner/repo\n", None),
        ("https://example.com/owner/repo", None),
        ("https://github.com/owner/repo", ""),
        ("https://github.com/owner/repo", "main\nnext"),
    ],
)
def test_parse_github_url_rejects_ambiguous_or_unsafe_input(
    url: str, requested_ref: str | None
) -> None:
    with pytest.raises(github.RepoIntakeError):
        github.parse_github_url(url, requested_ref)


def test_pin_repository_rejects_invalid_url_before_destination_creation(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "clone destination"

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(
            github.pin_repository("https://example.com/owner/repo", destination)
        )

    assert not destination.exists()


def test_remote_transient_failure_retries_once_after_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "remote retry destination"
    expected_sha = "a" * 40
    attempts: list[tuple[str, bool]] = []

    async def fake_run_git(operation: str, *_arguments: str) -> bytes:
        attempts.append((operation, destination.exists()))
        if len(attempts) == 1:
            raise github.RepoIntakeError("clone", failure_class="transport")
        if operation == "resolve":
            return f"{expected_sha}\n".encode()
        return b""

    monkeypatch.setattr(github, "_run_git", fake_run_git)
    monkeypatch.setattr(github, "NETWORK_RETRY_DELAY_SECONDS", 0)

    commit_sha, local_path = asyncio.run(
        github.clone_and_resolve("https://github.com/Owner/Project", destination)
    )

    assert commit_sha == expected_sha
    assert local_path == destination.resolve()
    assert [operation for operation, _exists in attempts] == [
        "clone",
        "clone",
        "resolve",
    ]
    assert attempts[0][1] is True
    assert attempts[1][1] is True


@pytest.mark.parametrize(
    "error",
    [
        github.RepoIntakeError("clone", failure_class="authentication"),
        github.RepoIntakeError("clone", failure_class="http"),
        github.RepoIntakeError("clone", failure_class="missing_ref"),
        github.RepoIntakeError("clone", failure_class="local_io"),
        github.RepoIntakeError("clone", failure_class="unknown"),
    ],
)
def test_remote_non_transient_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: github.RepoIntakeError,
) -> None:
    destination = tmp_path / "remote no retry destination"
    calls = 0

    async def fail_run_git(*_arguments: str) -> bytes:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(github, "_run_git", fail_run_git)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert raised.value is error
    assert calls == 1


def test_remote_command_timeout_retries_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "remote timeout retry destination"
    expected_sha = "b" * 40
    clone_calls = 0

    async def fake_run_git(operation: str, *_arguments: str) -> bytes:
        nonlocal clone_calls
        if operation == "clone":
            clone_calls += 1
            if clone_calls == 1:
                raise github.RepoIntakeError(
                    "clone_timeout", process_cleanup_succeeded=True
                )
            return b""
        return f"{expected_sha}\n".encode()

    monkeypatch.setattr(github, "_run_git", fake_run_git)
    monkeypatch.setattr(github, "NETWORK_RETRY_DELAY_SECONDS", 0)

    commit_sha, _local_path = asyncio.run(
        github.clone_and_resolve("https://github.com/Owner/Project", destination)
    )

    assert commit_sha == expected_sha
    assert clone_calls == 2


@pytest.mark.parametrize("failure_class", ["dns", "transport"])
def test_remote_persistent_transient_failure_has_exactly_two_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_class: Literal["dns", "transport"],
) -> None:
    destination = tmp_path / f"persistent {failure_class} destination"
    calls = 0

    async def fail_run_git(*_arguments: str) -> bytes:
        nonlocal calls
        calls += 1
        raise github.RepoIntakeError("clone", failure_class=failure_class)

    monkeypatch.setattr(github, "_run_git", fail_run_git)
    monkeypatch.setattr(github, "NETWORK_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert raised.value.failure_class == failure_class
    assert calls == 2


def test_remote_timeout_without_cleanup_evidence_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "timeout without cleanup evidence destination"
    calls = 0

    async def fail_run_git(*_arguments: str) -> bytes:
        nonlocal calls
        calls += 1
        raise github.RepoIntakeError("clone_timeout")

    monkeypatch.setattr(github, "_run_git", fail_run_git)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert calls == 1


@pytest.mark.parametrize("failed_operation", ["fetch", "checkout"])
def test_remote_exact_sha_transient_failure_restarts_full_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_operation: str,
) -> None:
    destination = tmp_path / f"exact sha {failed_operation} destination"
    requested_sha = "c" * 40
    operations: list[str] = []

    async def fail_run_git(operation: str, *_arguments: str) -> bytes:
        operations.append(operation)
        if operation == failed_operation:
            raise github.RepoIntakeError(operation, failure_class="transport")
        return b""

    monkeypatch.setattr(github, "_run_git", fail_run_git)
    monkeypatch.setattr(github, "NETWORK_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(
            github.clone_and_resolve(
                "https://github.com/Owner/Project",
                destination,
                requested_ref=requested_sha,
            )
        )

    expected_round = ["clone", "fetch"]
    if failed_operation == "checkout":
        expected_round.append("checkout")
    assert operations == [*expected_round, *expected_round]
    assert operations.count("clone") == 2


def test_remote_preparation_budget_is_shared_across_attempt_and_delay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "shared preparation budget destination"
    clone_calls = 0
    expected_sha = "d" * 40

    async def fake_run_git(operation: str, *_arguments: str) -> bytes:
        nonlocal clone_calls
        if operation == "clone":
            clone_calls += 1
            await asyncio.sleep(0.03)
            if clone_calls == 1:
                raise github.RepoIntakeError("clone", failure_class="transport")
            return b""
        return f"{expected_sha}\n".encode()

    monkeypatch.setattr(github, "_run_git", fake_run_git)
    monkeypatch.setattr(github, "NETWORK_PREPARATION_TIMEOUT_SECONDS", 0.07)
    monkeypatch.setattr(github, "NETWORK_RETRY_DELAY_SECONDS", 0.03)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert raised.value.operation == "preparation_timeout"
    assert clone_calls == 2
    assert not destination.exists()


def test_remote_retry_stops_when_process_cleanup_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "remote process cleanup destination"
    calls = 0

    async def fail_run_git(*_arguments: str) -> bytes:
        nonlocal calls
        calls += 1
        raise github.RepoIntakeError("clone_timeout", process_cleanup_succeeded=False)

    monkeypatch.setattr(github, "_run_git", fail_run_git)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert calls == 1


def test_remote_retry_stops_when_owned_destination_cleanup_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "remote destination cleanup destination"
    calls = 0

    async def fail_run_git(*_arguments: str) -> bytes:
        nonlocal calls
        calls += 1
        raise github.RepoIntakeError("clone", failure_class="transport")

    def refuse_cleanup(*_arguments: object, **_keywords: object) -> bool:
        return False

    monkeypatch.setattr(github, "_run_git", fail_run_git)
    monkeypatch.setattr(github, "_remove_owned_destination", refuse_cleanup)

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert calls == 1
    assert destination.exists()


def test_remote_preparation_budget_expires_with_fixed_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "remote budget destination"
    started = asyncio.Event()

    async def hang_run_git(*_arguments: str) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return b""

    monkeypatch.setattr(github, "_run_git", hang_run_git)
    monkeypatch.setattr(github, "NETWORK_PREPARATION_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(github.RepoIntakeError) as raised:
        asyncio.run(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )

    assert started.is_set()
    assert raised.value.operation == "preparation_timeout"
    assert not destination.exists()


def test_remote_preparation_cancellation_propagates_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "remote cancellation destination"
    started = asyncio.Event()

    async def hang_run_git(*_arguments: str) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return b""

    monkeypatch.setattr(github, "_run_git", hang_run_git)

    async def cancel() -> None:
        task = asyncio.create_task(
            github.clone_and_resolve("https://github.com/Owner/Project", destination)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())

    assert not destination.exists()
