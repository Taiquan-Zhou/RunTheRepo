from __future__ import annotations

import io
import os
import tarfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Self

import pytest

import repotrial.local_web.git_metadata as gm
from repotrial.local_web.git_metadata import GitCommandResult, GitMetadataClient

SHA = "a" * 40


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit: int,
    ) -> GitCommandResult:
        command = tuple(argv)
        self.calls.append(command)
        if "ls-remote" in command:
            return GitCommandResult(
                f"ref: refs/heads/master\tHEAD\n{SHA}\tHEAD\n".encode(),
                b"",
                0,
            )
        return GitCommandResult(b"", b"", 0)


def _archive(*members: tuple[str, bytes | None, str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "compose.yml"
            else:
                assert content is not None
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _client(
    archive_bytes: bytes,
    *,
    git: FakeGit | None = None,
) -> tuple[GitMetadataClient, FakeGit]:
    fake_git = git or FakeGit()

    def archive_fetcher(url: str):
        return iter((archive_bytes,))

    return GitMetadataClient(fake_git, archive_fetcher=archive_fetcher), fake_git


def test_git_metadata_reads_head_and_bounded_archive() -> None:
    compose = b"services:\n  web:\n    expose: [3000]\n"
    client, git = _client(
        _archive(
            (f"changedetection.io-{SHA}/README.md", b"readme", "file"),
            (f"changedetection.io-{SHA}/compose.yml", compose, "file"),
            (f"changedetection.io-{SHA}/linked-compose.yml", None, "symlink"),
        )
    )

    result = client.discover("dgtlmoon", "changedetection.io")

    assert result.commit_sha == SHA
    assert result.compose_candidates == ("compose.yml",)
    assert result.selected_compose_path == "compose.yml"
    assert result.compose_bytes == compose
    assert any("ls-remote" in call for call in git.calls)
    assert all("http.version=HTTP/1.1" in call for call in git.calls)
    assert all("clone" not in call and "checkout" not in call for call in git.calls)


def test_git_metadata_uses_exact_sha_without_head_lookup() -> None:
    client, git = _client(
        _archive((f"demo-{SHA}/compose.yml", b"services: {}\n", "file"))
    )

    result = client.discover("acme", "demo", requested_sha=SHA)

    assert result.commit_sha == SHA
    assert not any("ls-remote" in call for call in git.calls)


def test_git_metadata_caches_fixed_sha_archive() -> None:
    client, git = _client(
        _archive((f"demo-{SHA}/compose.yml", b"services: {}\n", "file"))
    )

    first = client.discover("acme", "demo", requested_sha=SHA)
    second = client.discover("acme", "demo", requested_sha=SHA)

    assert first == second
    assert len(git.calls) == 0


def test_git_metadata_rejects_unsafe_compose_path_without_network() -> None:
    client, git = _client(b"")

    result = client.discover(
        "acme", "demo", requested_sha=SHA, compose_path="../compose.yml"
    )

    assert result.errors == ("git_invalid_compose_path",)
    assert git.calls == []


def test_git_metadata_reports_archive_limit() -> None:
    client, _ = _client(b"x" * (32 * 1024 * 1024 + 1))

    result = client.discover("acme", "demo", requested_sha=SHA)

    assert result.errors == ("git_metadata_limit_exceeded",)
    assert result.commit_sha == SHA


def test_invalid_inputs_are_rejected_before_runner() -> None:
    client, git = _client(b"")
    assert client.discover("../owner", "demo").errors == ("git_invalid_repository",)
    assert client.discover("acme", "demo", requested_sha="bad").errors == (
        "git_invalid_commit",
    )
    assert git.calls == []


def test_multiple_root_compose_files_require_selection() -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo(f"demo-{SHA}/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name in ("compose.yml", "docker-compose.yml"):
            content = b"services: {}\n"
            info = tarfile.TarInfo(f"demo-{SHA}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    client, _ = _client(output.getvalue())
    result = client.discover("acme", "demo", requested_sha=SHA)
    assert result.compose_candidates == ("compose.yml", "docker-compose.yml")
    assert result.selected_compose_path is None
    assert result.warnings == ("git_compose_selection_required",)


def test_custom_compose_path_can_select_nonstandard_yaml() -> None:
    client, _ = _client(
        _archive((f"demo-{SHA}/deploy/application.yml", b"services: {}\n", "file"))
    )
    result = client.discover(
        "acme",
        "demo",
        requested_sha=SHA,
        compose_path="deploy/application.yml",
    )
    assert result.selected_compose_path == "deploy/application.yml"
    assert result.compose_bytes == b"services: {}\n"


def test_missing_explicit_compose_path_is_not_found_even_with_other_candidates() -> (
    None
):
    client, _ = _client(
        _archive((f"demo-{SHA}/compose.yml", b"services: {}\n", "file"))
    )

    result = client.discover(
        "acme",
        "demo",
        requested_sha=SHA,
        compose_path="deploy/compose.yml",
    )

    assert result.selected_compose_path is None
    assert result.errors == ("git_compose_not_found",)


@pytest.mark.parametrize(
    "member_name",
    ("demo-" + SHA + "/../compose.yml", "/demo-" + SHA + "/compose.yml"),
)
def test_archive_rejects_unsafe_member_path(member_name: str) -> None:
    client, _ = _client(_archive((member_name, b"services: {}\n", "file")))
    result = client.discover("acme", "demo", requested_sha=SHA)
    assert result.errors == ("git_archive_invalid",)


def test_archive_enforces_decompressed_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "_MAX_DECOMPRESSED", 32)
    client, _ = _client(_archive((f"demo-{SHA}/README.md", b"x" * 128, "file")))
    result = client.discover("acme", "demo", requested_sha=SHA)
    assert result.errors == ("git_metadata_limit_exceeded",)


def test_git_runner_reads_bounded_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nprintf out\nprintf err >&2\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    result = gm._SubprocessGitRunner()(
        ("ls-remote",),
        timeout_seconds=2,
        output_limit=100,
    )
    assert result.stdout == b"out"
    assert result.stderr == b"err"


def test_git_runner_kills_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nsleep 2\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    processes = []
    real_popen = gm.subprocess.Popen

    def tracked_popen(*args: object, **kwargs: object):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(gm.subprocess, "Popen", tracked_popen)
    with pytest.raises(gm._GitLimitError, match="git_timeout"):
        gm._SubprocessGitRunner()(
            ("ls-remote",),
            timeout_seconds=0.05,
            output_limit=100,
        )
    assert processes[0].stdout is not None and processes[0].stdout.closed
    assert processes[0].stderr is not None and processes[0].stderr.closed


def test_git_runner_rejects_output_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nhead -c 10000 /dev/zero\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    with pytest.raises(gm._GitLimitError, match="git_metadata_limit_exceeded"):
        gm._SubprocessGitRunner()(
            ("ls-remote",),
            timeout_seconds=2,
            output_limit=100,
        )


def test_git_environment_strips_user_git_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/secret/config")
    monkeypatch.setenv("GIT_TOKEN", "secret")
    monkeypatch.setenv("SSH_ASKPASS", "/secret/askpass")
    environment = gm._git_environment()
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_TOKEN" not in environment
    assert "SSH_ASKPASS" not in environment


class _FakeHttpResponse:
    def __init__(self, status_code: int, chunks: object = ()) -> None:
        self.status_code = status_code
        self._chunks = chunks
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def iter_raw(self):
        if isinstance(self._chunks, BaseException):
            raise self._chunks
        return iter(self._chunks)


class _FakeHttpClient:
    def __init__(self, response: _FakeHttpResponse, **kwargs: object) -> None:
        self.response = response
        self.kwargs = kwargs
        self.closed = False

    def stream(self, method: str, url: str) -> _FakeHttpResponse:
        assert method == "GET"
        assert url.startswith("https://codeload.github.com/")
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("status", "error"),
    (
        (301, "git_archive_redirect_rejected"),
        (404, "git_commit_not_found"),
        (500, "git_archive_failed"),
    ),
)
def test_http_archive_fetcher_maps_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int, error: str
) -> None:
    response = _FakeHttpResponse(status)
    client = _FakeHttpClient(response)
    monkeypatch.setattr(gm.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(gm._ArchiveFailure, match=error):
        list(
            gm._http_archive_fetcher(
                "https://codeload.github.com/acme/demo/tar.gz/" + SHA
            )
        )
    assert client.closed


def test_http_archive_fetcher_streams_raw_bytes_and_rejects_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeHttpResponse(200, (b"a", b"b"))
    client = _FakeHttpClient(response)

    def make_client(**kwargs: object) -> _FakeHttpClient:
        client.kwargs.update(kwargs)
        return client

    monkeypatch.setattr(gm.httpx, "Client", make_client)
    assert list(
        gm._http_archive_fetcher("https://codeload.github.com/acme/demo/tar.gz/" + SHA)
    ) == [b"a", b"b"]
    assert client.kwargs["follow_redirects"] is False
    assert client.kwargs["headers"]["Accept-Encoding"] == "identity"

    error_response = _FakeHttpResponse(200, gm.httpx.ReadTimeout("slow"))
    error_client = _FakeHttpClient(error_response)
    monkeypatch.setattr(gm.httpx, "Client", lambda **kwargs: error_client)
    with pytest.raises(gm._ArchiveFailure, match="git_timeout"):
        list(
            gm._http_archive_fetcher(
                "https://codeload.github.com/acme/demo/tar.gz/" + SHA
            )
        )


def test_git_metadata_singleflight_shares_one_archive_read() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def archive_fetcher(url: str):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(2)
        return iter((_archive((f"demo-{SHA}/compose.yml", b"services: {}\n", "file")),))

    client = GitMetadataClient(FakeGit(), archive_fetcher=archive_fetcher)
    results: list[object] = []
    first = threading.Thread(
        target=lambda: results.append(
            client.discover("acme", "demo", requested_sha=SHA)
        )
    )
    second = threading.Thread(
        target=lambda: results.append(
            client.discover("acme", "demo", requested_sha=SHA)
        )
    )
    first.start()
    assert started.wait(2)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert calls == 1
    assert len(results) == 2
    assert all(
        result.selected_compose_path == "compose.yml"
        for result in results
        if isinstance(result, gm.GitMetadata)
    )


def test_latest_archive_failure_preserves_resolved_sha() -> None:
    client, _ = _client(b"not-a-tar")
    result = client.discover("acme", "demo")
    assert result.commit_sha == SHA
    assert result.errors == ("git_archive_invalid",)


def test_duplicate_regular_archive_path_is_rejected() -> None:
    archive = _archive(
        (f"demo-{SHA}/compose.yml", b"services: {}\n", "file"),
        (f"demo-{SHA}/compose.yml", b"services: {web: {}}\n", "file"),
    )
    client, _ = _client(archive)
    result = client.discover("acme", "demo", requested_sha=SHA)
    assert result.errors == ("git_archive_invalid",)


def test_archive_closes_upstream_iterator_on_failure() -> None:
    closed = False

    def archive_fetcher(url: str):
        nonlocal closed
        try:
            yield b"not-a-gzip-stream"
        finally:
            closed = True

    client = GitMetadataClient(FakeGit(), archive_fetcher=archive_fetcher)
    result = client.discover("acme", "demo", requested_sha=SHA)
    assert result.errors == ("git_archive_invalid",)
    assert closed
