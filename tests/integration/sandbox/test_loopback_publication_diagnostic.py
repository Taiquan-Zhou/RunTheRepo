import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

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
    for argv in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "rg3@example.invalid"],
        ["git", "config", "user.name", "RepoTrial RG3"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "trusted loopback fixture"],
    ):
        subprocess.run(
            argv,
            cwd=repository,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
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
) -> dict[str, object]:
    try:
        result = await provider.exec(sandbox_id, argv, timeout_s=30)
    except DockerSbxError as error:
        return _error_observation(address_class, port, error)
    return _exec_observation(address_class, port, result)


async def _run_three_probes(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    compose_path: str,
    service_name: str,
    port: int,
    request_path: str,
) -> list[dict[str, object]]:
    container_id = await provider.exec(
        sandbox_id,
        ["docker", "compose", "-f", compose_path, "ps", "-q", service_name],
        timeout_s=30,
    )
    normalized_id = container_id.stdout.strip().lower()
    if container_id.exit_code == 0 and re.fullmatch(r"[0-9a-f]{12,64}", normalized_id):
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
            "/dev/null",
            f"http://127.0.0.1:{port}{request_path}",
        ]
        container_observation = await _run_exec_probe(
            provider,
            sandbox_id,
            address_class="target_container_loopback",
            port=port,
            argv=container_argv,
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
            "/dev/null",
            f"http://127.0.0.1:{port}{request_path}",
        ],
    )

    try:
        host_port = await provider.publish_port(sandbox_id, port)
        await asyncio.to_thread(_request_published_port, host_port, request_path)
    except (DockerSbxError, OSError) as error:
        provider_observation = _error_observation(
            "provider_published_loopback", port, error
        )
    else:
        provider_observation = {
            "address_class": "provider_published_loopback",
            "port": port,
            "status": "pass",
            "error": None,
        }

    return [container_observation, guest_observation, provider_observation]


def _request_published_port(host_port: int, request_path: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(
        f"http://127.0.0.1:{host_port}{request_path}", timeout=10
    ) as response:
        if not 200 <= response.status < 400:
            raise OSError("unexpected_http_status")
        response.read(1)


def _write_observations(path: Path, observations: list[dict[str, object]]) -> None:
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
            )

    _assert_empty_inventory()
    try:
        observations = asyncio.run(exercise())
        _write_observations(evidence_path, observations)
    finally:
        _assert_empty_inventory()

    _assert_bounded_observations(observations, 8080)
    assert observations[0]["status"] == "pass"
    assert observations[1]["status"] == "pass"
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
        _write_observations(evidence_path, observations)
    finally:
        _assert_empty_inventory()

    assert actual_sha == _CHANGEDTECTION_SHA
    _assert_bounded_observations(observations, 5000)
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert lifecycle_path.is_file()
