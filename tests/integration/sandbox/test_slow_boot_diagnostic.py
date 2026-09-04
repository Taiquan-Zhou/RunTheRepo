import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from repotrial.domain.enums import Verdict
from repotrial.sandbox.base import ExecResult
from repotrial.sandbox.docker_sbx import (
    MAX_OUTPUT_BYTES,
    DockerSbxPolicy,
    DockerSbxProvider,
)
from repotrial.sandbox.lifecycle import managed_sandbox
from repotrial.trial import boot as boot_module
from repotrial.trial.boot import BootResult, boot_compose

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "slow_boot"
_BUSYBOX_IMAGE = (
    "busybox:1.36.1@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)


def test_slow_boot_fixture_is_pinned_bounded_and_unprivileged() -> None:
    compose_path = _FIXTURE_ROOT / "compose.yaml"
    dockerfile_path = _FIXTURE_ROOT / "Dockerfile"
    health_path = _FIXTURE_ROOT / "health.txt"

    compose_text = compose_path.read_text(encoding="utf-8")
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    compose = YAML(typ="safe").load(compose_text)
    service = compose["services"]["web"]

    assert service["build"] == {"context": "."}
    assert service["command"] == ["httpd", "-f", "-p", "8080", "-h", "/www"]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8080/health.txt",
    ]
    assert service["ports"] == ["8080:8080"]
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "volumes" not in service
    assert "/var/run/docker.sock" not in compose_text

    assert dockerfile_text.splitlines() == [
        f"FROM {_BUSYBOX_IMAGE}",
        "RUN sleep 250",
        "COPY health.txt /www/health.txt",
    ]
    assert health_path.read_text(encoding="utf-8") == "ok\n"


def _create_fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "slow-boot-repository"
    shutil.copytree(_FIXTURE_ROOT, repository)
    commands = (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "rg4@example.invalid"],
        ["git", "config", "user.name", "RepoTrial RG4"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "trusted slow boot fixture"],
    )
    for argv in commands:
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
    assert result.stdout == b"No sandboxes found.\nLaunch one: sbx run claude\n"


def _lifecycle_events(path: Path) -> list[str]:
    return [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_REAL_SBX") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_REAL_SBX=1 for the slow boot contract",
)
def test_slow_boot_completes_with_extended_compose_up_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    repository = _create_fixture_repository(tmp_path)
    lifecycle_path = tmp_path / "slow-boot-lifecycle.jsonl"
    provider_exec_calls: list[tuple[tuple[str, ...], int]] = []
    original_provider_exec = DockerSbxProvider.exec

    async def capture_provider_exec(
        self: DockerSbxProvider,
        sandbox_id: str,
        argv: list[str],
        timeout_s: int = 60,
    ) -> ExecResult:
        provider_exec_calls.append((tuple(argv), timeout_s))
        return await original_provider_exec(self, sandbox_id, argv, timeout_s)

    monkeypatch.setattr(DockerSbxProvider, "exec", capture_provider_exec)

    async def exercise() -> tuple[BootResult, float]:
        provider = DockerSbxProvider(_policy(), command_timeout_s=30)
        async with managed_sandbox(
            provider,
            repository,
            "rg4-slow-boot",
            lifecycle_artifact=lifecycle_path,
        ) as sandbox_id:
            started = time.monotonic()
            result = await boot_compose(
                provider,
                sandbox_id,
                "compose.yaml",
                {},
                1,
            )
            return result, time.monotonic() - started

    try:
        result, elapsed_s = asyncio.run(exercise())
        assert boot_module._COMPOSE_UP_TIMEOUT_S == 600
        assert provider_exec_calls[0] == (
            (
                "docker",
                "compose",
                "-f",
                "compose.yaml",
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "60",
            ),
            boot_module._COMPOSE_UP_TIMEOUT_S,
        )
        assert [timeout for _argv, timeout in provider_exec_calls] == [600, 30, 30]
        assert result.verdict is Verdict.PASS
        assert result.service_states == {"web": "running/healthy"}
        assert set(result.logs) == {"up", "ps", "logs"}
        assert all(
            len(output.encode("utf-8")) <= MAX_OUTPUT_BYTES
            for output in result.logs.values()
        )
        assert 250 <= elapsed_s < boot_module._COMPOSE_UP_TIMEOUT_S
        assert _lifecycle_events(lifecycle_path) == [
            "create_attempt",
            "create_success",
            "destroy_attempt",
            "destroy_success",
        ]
    finally:
        _assert_empty_inventory()
