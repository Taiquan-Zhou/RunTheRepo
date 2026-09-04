import asyncio
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from ruamel.yaml import YAML

from repotrial.domain.enums import Verdict
from repotrial.intake.github import clone_and_resolve
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.docker_sbx import (
    DockerSbxError,
    DockerSbxPolicy,
    DockerSbxProvider,
)
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.sandbox.lifecycle import managed_sandbox
from repotrial.trial.boot import boot_compose

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "loopback_publication"
_BUSYBOX_IMAGE = (
    "busybox:1.36.1@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
_EMPTY_INVENTORY = b"No sandboxes found.\nLaunch one: sbx run claude\n"
_CHANGEDTECTION_URL = "https://github.com/dgtlmoon/changedetection.io"
_CHANGEDTECTION_SHA = "5d9c7c6da76340597243e8163c4f2439237fa0e8"
_OBSERVATION_KEYS = {"address_class", "port", "status", "error"}
_ERROR_TOKEN = re.compile(r"[a-z0-9_]{1,64}\Z")
_MARKER_BODY = b"repotrial-loopback-marker\n"
_NETWORK_FAILURE_TOKENS = frozenset(
    {
        "connection_aborted_error",
        "connection_refused_error",
        "connection_reset_error",
        "os_error",
        "remote_disconnected",
        "timeout_error",
        "url_error",
    }
)


def test_loopback_publication_fixture_is_pinned_bounded_and_unprivileged() -> None:
    compose_path = _FIXTURE_ROOT / "compose.yaml"
    marker_path = _FIXTURE_ROOT / "marker.txt"

    compose_text = compose_path.read_text(encoding="utf-8")
    compose = YAML(typ="safe").load(compose_text)
    service = compose["services"]["web"]

    assert service["image"] == _BUSYBOX_IMAGE
    assert service["user"] == "65534:65534"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["ports"] == ["127.0.0.1:8080:8080"]
    assert service["command"] == ["httpd", "-f", "-p", "8080", "-h", "/www"]
    assert service["configs"] == [{"source": "marker", "target": "/www/marker.txt"}]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8080/marker.txt",
    ]
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "devices" not in service
    assert "volumes" not in service
    assert "docker.sock" not in compose_text
    assert compose["configs"] == {"marker": {"file": "./marker.txt"}}
    assert marker_path.read_text(encoding="utf-8") == "repotrial-loopback-marker\n"


def _create_fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "loopback-publication-repository"
    shutil.copytree(_FIXTURE_ROOT, repository)
    git_environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_") and name.upper() != "SSH_ASKPASS"
    }
    git_environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    for arguments in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "rg3@example.invalid"],
        ["git", "config", "user.name", "RepoTrial RG3"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "trusted loopback fixture"],
    ):
        argv = [
            arguments[0],
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *arguments[1:],
        ]
        subprocess.run(
            argv,
            cwd=repository,
            check=True,
            env=git_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    return repository


def _policy() -> DockerSbxPolicy:
    return DockerSbxPolicy(
        cpus=1,
        memory_mb=1024,
        pids_limit=64,
        disk_mb=8192,
        total_duration_s=900,
    )


def _assert_empty_inventory() -> None:
    result = subprocess.run(
        ["sbx", "list"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stderr == b""
    assert result.stdout == _EMPTY_INVENTORY


def _bounded_error(error: BaseException) -> str:
    token = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return token if _ERROR_TOKEN.fullmatch(token) else "exception"


def _exec_observation(
    address_class: str, port: int, result: ExecResult
) -> dict[str, object]:
    return {
        "address_class": address_class,
        "port": port,
        "status": "pass" if result.exit_code == 0 else "fail",
        "error": None if result.exit_code == 0 else "nonzero_exit",
    }


def _error_observation(
    address_class: str, port: int, error: BaseException
) -> dict[str, object]:
    return {
        "address_class": address_class,
        "port": port,
        "status": "error",
        "error": _bounded_error(error),
    }


async def _run_exec_probe(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    address_class: str,
    port: int,
    argv: list[str],
    expected_stdout: str | None = None,
) -> dict[str, object]:
    try:
        result = await provider.exec(sandbox_id, argv, timeout_s=30)
    except DockerSbxError as error:
        return _error_observation(address_class, port, error)
    observation = _exec_observation(address_class, port, result)
    if (
        result.exit_code == 0
        and expected_stdout is not None
        and result.stdout != expected_stdout
    ):
        observation["status"] = "fail"
        observation["error"] = "marker_mismatch"
    return observation


def _provider_error_token(error: DockerSbxError) -> str:
    reason = error.reason
    token = f"provider_{reason}"
    return token if _ERROR_TOKEN.fullmatch(token) else "provider_error"


def _network_error_token(error: OSError) -> str:
    reason: object = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused_error"
    if isinstance(reason, http.client.RemoteDisconnected):
        return "remote_disconnected"
    if isinstance(reason, ConnectionResetError):
        return "connection_reset_error"
    if isinstance(reason, ConnectionAbortedError):
        return "connection_aborted_error"
    if isinstance(reason, TimeoutError):
        return "timeout_error"
    return "url_error" if isinstance(error, URLError) else "os_error"


def _network_error_observation(
    address_class: str, port: int, error: OSError
) -> dict[str, object]:
    return {
        "address_class": address_class,
        "port": port,
        "status": "error",
        "error": _network_error_token(error),
    }


async def _run_provider_probe(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    port: int,
    request_path: str,
    expected_body: bytes | None,
) -> dict[str, object]:
    try:
        host_port = await provider.publish_port(sandbox_id, port)
    except DockerSbxError as error:
        return {
            "address_class": "provider_published_loopback",
            "port": port,
            "status": "error",
            "error": _provider_error_token(error),
        }
    if type(host_port) is not int or not 1 <= host_port <= 65_535:
        return {
            "address_class": "provider_published_loopback",
            "port": port,
            "status": "error",
            "error": "provider_port_invalid",
        }

    try:
        body = await asyncio.to_thread(
            _request_published_port, host_port, request_path, expected_body
        )
    except OSError as error:
        return _network_error_observation("provider_published_loopback", port, error)
    if expected_body is not None and body != expected_body:
        return {
            "address_class": "provider_published_loopback",
            "port": port,
            "status": "fail",
            "error": "marker_mismatch",
        }
    return {
        "address_class": "provider_published_loopback",
        "port": port,
        "status": "pass",
        "error": None,
    }


async def _run_three_probes(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    compose_path: str,
    service_name: str,
    port: int,
    request_path: str,
    expected_body: bytes | None = None,
) -> list[dict[str, object]]:
    container_id = await provider.exec(
        sandbox_id,
        ["docker", "compose", "-f", compose_path, "ps", "-q", service_name],
        timeout_s=30,
    )
    normalized_id = container_id.stdout.strip().lower()
    if container_id.exit_code == 0 and re.fullmatch(r"[0-9a-f]{12,64}", normalized_id):
        wget_output = "-" if expected_body is not None else "/dev/null"
        container_argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            f"container:{normalized_id}",
            _BUSYBOX_IMAGE,
            "wget",
            "-q",
            "-T",
            "10",
            "-O",
            wget_output,
            f"http://127.0.0.1:{port}{request_path}",
        ]
        container_observation = await _run_exec_probe(
            provider,
            sandbox_id,
            address_class="target_container_loopback",
            port=port,
            argv=container_argv,
            expected_stdout=(
                expected_body.decode("ascii") if expected_body is not None else None
            ),
        )
    else:
        container_observation = {
            "address_class": "target_container_loopback",
            "port": port,
            "status": "fail",
            "error": "container_id_unavailable",
        }

    guest_observation = await _run_exec_probe(
        provider,
        sandbox_id,
        address_class="sandbox_guest_loopback",
        port=port,
        argv=[
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            _BUSYBOX_IMAGE,
            "wget",
            "-q",
            "-T",
            "10",
            "-O",
            "-" if expected_body is not None else "/dev/null",
            f"http://127.0.0.1:{port}{request_path}",
        ],
        expected_stdout=(
            expected_body.decode("ascii") if expected_body is not None else None
        ),
    )

    provider_observation = await _run_provider_probe(
        provider,
        sandbox_id,
        port=port,
        request_path=request_path,
        expected_body=expected_body,
    )

    return [container_observation, guest_observation, provider_observation]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _request_published_port(
    host_port: int, request_path: str, expected_body: bytes | None
) -> bytes:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler()
    )
    read_limit = 1 if expected_body is None else len(expected_body) + 1
    try:
        response = opener.open(
            f"http://127.0.0.1:{host_port}{request_path}", timeout=10
        )
    except HTTPError as error:
        if not 300 <= error.code < 400:
            raise OSError("unexpected_http_status") from None
        try:
            return error.read(read_limit)
        finally:
            error.close()
    with response:
        if not 200 <= response.status < 400:
            raise OSError("unexpected_http_status")
        return response.read(read_limit)


def _write_observations(
    path: Path, observations: list[dict[str, object]], port: int
) -> None:
    _assert_bounded_observations(observations, port)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(observations, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def _assert_bounded_observations(
    observations: list[dict[str, object]], port: int
) -> None:
    assert [row["address_class"] for row in observations] == [
        "target_container_loopback",
        "sandbox_guest_loopback",
        "provider_published_loopback",
    ]
    for row in observations:
        assert set(row) == _OBSERVATION_KEYS
        assert row["port"] == port
        assert row["status"] in {"pass", "fail", "error"}
        error = row["error"]
        assert error is None or (
            isinstance(error, str) and _ERROR_TOKEN.fullmatch(error) is not None
        )


def _publication_topology_signature(observations: list[dict[str, object]]) -> bool:
    if len(observations) != 3:
        return False
    return (
        observations[0]["status"] == "pass"
        and observations[1]["status"] == "pass"
        and observations[2]["status"] == "error"
        and observations[2]["error"] in _NETWORK_FAILURE_TOKENS
    )


class _DeadlinePublishProvider(FakeSandboxProvider):
    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        raise DockerSbxError("publish_port", "total_duration_exhausted")


class _InvalidPortProvider(FakeSandboxProvider):
    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        return 0


def test_provider_deadline_failure_is_not_a_loopback_topology_signature() -> None:
    observation = asyncio.run(
        _run_provider_probe(
            _DeadlinePublishProvider(),
            "sandbox-1",
            port=8080,
            request_path="/marker.txt",
            expected_body=b"repotrial-loopback-marker\n",
        )
    )
    observations = [
        {
            "address_class": "target_container_loopback",
            "port": 8080,
            "status": "pass",
            "error": None,
        },
        {
            "address_class": "sandbox_guest_loopback",
            "port": 8080,
            "status": "pass",
            "error": None,
        },
        observation,
    ]

    assert observation == {
        "address_class": "provider_published_loopback",
        "port": 8080,
        "status": "error",
        "error": "provider_total_duration_exhausted",
    }
    assert _publication_topology_signature(observations) is False


def test_invalid_provider_port_is_not_requested_or_classified_as_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = False

    def unexpected_request(
        host_port: int, request_path: str, expected_body: bytes | None
    ) -> bytes:
        nonlocal requested
        requested = True
        raise AssertionError("invalid provider port must not be requested")

    monkeypatch.setattr(
        sys.modules[__name__], "_request_published_port", unexpected_request
    )
    observation = asyncio.run(
        _run_provider_probe(
            _InvalidPortProvider(),
            "sandbox-1",
            port=8080,
            request_path="/",
            expected_body=None,
        )
    )

    assert requested is False
    assert observation["error"] == "provider_port_invalid"
    assert (
        _publication_topology_signature(
            [
                {"status": "pass"},
                {"status": "pass"},
                observation,
            ]
        )
        is False
    )


def test_network_error_tokens_are_explicit_and_match_the_signature() -> None:
    assert (
        _network_error_token(URLError(ConnectionRefusedError()))
        == "connection_refused_error"
    )
    assert _network_error_token(TimeoutError()) == "timeout_error"
    assert (
        _network_error_token(http.client.RemoteDisconnected()) == "remote_disconnected"
    )


def test_redirect_handler_never_follows_untrusted_location() -> None:
    request = urllib.request.Request("http://127.0.0.1:12345/")

    assert (
        _NoRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {"Location": "http://169.254.169.254/latest/meta-data/"},
            "http://169.254.169.254/latest/meta-data/",
        )
        is None
    )


def test_redirect_response_is_reachable_without_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect = HTTPError(
        "http://127.0.0.1:12345/",
        302,
        "Found",
        {"Location": "http://169.254.169.254/latest/meta-data/"},
        BytesIO(b"redirect body"),
    )

    class _RedirectingOpener:
        def open(self, request: str, timeout: int) -> object:
            raise redirect

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: _RedirectingOpener(),
    )

    assert _request_published_port(12345, "/", None) == b"r"


def test_invalid_observation_is_rejected_before_evidence_file_creation(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "invalid.json"
    invalid = [
        {
            "address_class": "target_container_loopback",
            "port": 8080,
            "status": "pass",
            "error": None,
            "unexpected": "must-not-persist",
        }
    ]

    with pytest.raises(AssertionError):
        _write_observations(evidence_path, invalid, 8080)

    assert not evidence_path.exists()


@pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_REAL_SBX") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_REAL_SBX=1 for the RG3 fixture",
)
def test_real_sbx_observes_loopback_publication_boundary(tmp_path: Path) -> None:
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    repository = _create_fixture_repository(tmp_path)
    evidence_path = tmp_path / "loopback-observations.json"
    lifecycle_path = tmp_path / "loopback-lifecycle.jsonl"
    provider = DockerSbxProvider(_policy(), command_timeout_s=60)

    async def exercise() -> list[dict[str, object]]:
        async with managed_sandbox(
            provider,
            repository,
            "rg3-loopback-fixture",
            lifecycle_artifact=lifecycle_path,
        ) as sandbox_id:
            boot = await boot_compose(provider, sandbox_id, "compose.yaml", {}, 1)
            assert boot.verdict is Verdict.PASS
            return await _run_three_probes(
                provider,
                sandbox_id,
                compose_path="compose.yaml",
                service_name="web",
                port=8080,
                request_path="/marker.txt",
                expected_body=_MARKER_BODY,
            )

    _assert_empty_inventory()
    try:
        observations = asyncio.run(exercise())
        _write_observations(evidence_path, observations, 8080)
    finally:
        _assert_empty_inventory()

    _assert_bounded_observations(observations, 8080)
    assert observations[0]["status"] == "pass"
    assert observations[1]["status"] == "pass"
    assert _publication_topology_signature(observations)
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert lifecycle_path.is_file()


@pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_RG3_TARGET") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_RG3_TARGET=1 for changedetection.io",
)
def test_changedetection_loopback_publication_boundary(tmp_path: Path) -> None:
    if os.environ.get("REPOTRIAL_RUN_REAL_SBX") != "1":
        pytest.skip("UNSUPPORTED: target diagnostic also requires real SBX opt-in")
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    checkout = tmp_path / "changedetection-repository"
    evidence_path = tmp_path / "changedetection-loopback-observations.json"
    lifecycle_path = tmp_path / "changedetection-loopback-lifecycle.jsonl"
    provider = DockerSbxProvider(_policy(), command_timeout_s=120)

    async def exercise() -> tuple[str, list[dict[str, object]]]:
        actual_sha, repository = await clone_and_resolve(
            _CHANGEDTECTION_URL,
            checkout,
            requested_ref=_CHANGEDTECTION_SHA,
        )
        async with managed_sandbox(
            provider,
            repository,
            "rg3-changedetection",
            lifecycle_artifact=lifecycle_path,
        ) as sandbox_id:
            boot = await boot_compose(provider, sandbox_id, "docker-compose.yml", {}, 1)
            assert boot.verdict is Verdict.PASS
            observations = await _run_three_probes(
                provider,
                sandbox_id,
                compose_path="docker-compose.yml",
                service_name="changedetection",
                port=5000,
                request_path="/",
            )
        return actual_sha, observations

    _assert_empty_inventory()
    try:
        actual_sha, observations = asyncio.run(exercise())
        _write_observations(evidence_path, observations, 5000)
    finally:
        _assert_empty_inventory()

    assert actual_sha == _CHANGEDTECTION_SHA
    _assert_bounded_observations(observations, 5000)
    assert _publication_topology_signature(observations)
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert lifecycle_path.is_file()
