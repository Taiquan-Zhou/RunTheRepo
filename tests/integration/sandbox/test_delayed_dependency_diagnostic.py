import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from repotrial.sandbox.docker_sbx import DockerSbxPolicy, DockerSbxProvider
from repotrial.sandbox.lifecycle import managed_sandbox

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "delayed_dependency"
_BUSYBOX_IMAGE = (
    "busybox:1.36.1@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)


def test_delayed_dependency_fixture_is_pinned_bounded_and_unprivileged() -> None:
    compose_path = _FIXTURE_ROOT / "compose.yaml"
    health_path = _FIXTURE_ROOT / "www" / "health.txt"

    compose_text = compose_path.read_text(encoding="utf-8")
    compose = YAML(typ="safe").load(compose_text)
    services = compose["services"]
    db = services["db"]
    web = services["web"]

    assert set(services) == {"db", "web"}
    assert db["image"] == _BUSYBOX_IMAGE
    assert db["command"] == [
        "sh",
        "-c",
        "sleep 70; exec httpd -f -p 8081 -h /www",
    ]
    assert db["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8081/health.txt",
    ]
    assert db["healthcheck"]["start_period"] == "90s"
    assert web["image"] == _BUSYBOX_IMAGE
    assert web["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert web["command"] == ["httpd", "-f", "-p", "8080", "-h", "/www"]
    assert web["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8080/health.txt",
    ]
    assert web["ports"] == ["8080:8080"]

    for service in services.values():
        assert service["configs"] == [
            {"source": "health-marker", "target": "/www/health.txt"}
        ]
        assert "volumes" not in service
        assert "privileged" not in service
        assert "network_mode" not in service
    assert compose["configs"] == {"health-marker": {"file": "./www/health.txt"}}
    assert "/var/run/docker.sock" not in compose_text
    assert "secrets:" not in compose_text
    assert health_path.read_text(encoding="utf-8") == "ok\n"


def _create_fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "delayed-dependency-repository"
    shutil.copytree(_FIXTURE_ROOT, repository)
    commands = (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "rg6@example.invalid"],
        ["git", "config", "user.name", "RepoTrial RG6"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "trusted delayed dependency fixture"],
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


def _service_health(stdout: str) -> dict[str, str]:
    health: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        service = row.get("Service")
        value = row.get("Health")
        if isinstance(service, str) and isinstance(value, str):
            health[service] = value.lower()
    return health


def _lifecycle_events(path: Path) -> list[str]:
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


@pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_REAL_SBX") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_REAL_SBX=1 for the RG6 diagnostic",
)
def test_dependency_becomes_healthy_after_sixty_and_before_one_twenty_seconds(
    tmp_path: Path,
) -> None:
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    repository = _create_fixture_repository(tmp_path)
    compose_prefix = ["docker", "compose", "-f", "compose.yaml"]
    current_lifecycle = tmp_path / "current-lifecycle.jsonl"
    expanded_lifecycle = tmp_path / "expanded-lifecycle.jsonl"

    async def exercise() -> dict[str, object]:
        current_provider = DockerSbxProvider(_policy(), command_timeout_s=30)
        observations: list[dict[str, object]] = []
        state_transitions: list[tuple[float, dict[str, str]]] = []
        async with managed_sandbox(
            current_provider,
            repository,
            "rg6-readiness-current",
            lifecycle_artifact=current_lifecycle,
        ) as sandbox_id:
            started = time.monotonic()
            current = await current_provider.exec(
                sandbox_id,
                [*compose_prefix, "up", "-d", "--wait", "--wait-timeout", "60"],
                timeout_s=120,
            )
            current_elapsed_s = time.monotonic() - started
            assert current.exit_code == 1, (current.stdout, current.stderr)
            assert "timeout waiting for dependencies" in current.stderr
            assert 55 <= current_elapsed_s < 90, {
                "elapsed_s": current_elapsed_s,
                "stdout": current.stdout,
                "stderr": current.stderr,
            }

            dependency_healthy_elapsed_s: float | None = None
            while time.monotonic() - started < 120:
                state = await current_provider.exec(
                    sandbox_id,
                    [*compose_prefix, "ps", "--all", "--format", "json"],
                    timeout_s=30,
                )
                elapsed_s = time.monotonic() - started
                health = _service_health(state.stdout)
                state_transitions.append((elapsed_s, health))
                observations.append(
                    {
                        "elapsed_s": elapsed_s,
                        "exit_code": state.exit_code,
                        "health": health,
                    }
                )
                if health.get("db") == "healthy":
                    dependency_healthy_elapsed_s = elapsed_s
                    break
                await asyncio.sleep(2)
            assert dependency_healthy_elapsed_s is not None, observations
            assert 60 < dependency_healthy_elapsed_s < 120
            assert current_elapsed_s < dependency_healthy_elapsed_s
            assert all(observation["exit_code"] == 0 for observation in observations)
            assert any(
                health.get("db") == "starting"
                and elapsed_s < dependency_healthy_elapsed_s
                for elapsed_s, health in state_transitions
            )

        current_record = {
            "current_wait_timeout_s": 60,
            "current_exit_code": current.exit_code,
            "current_elapsed_s": current_elapsed_s,
            "current_stdout_tail": current.stdout[-4096:],
            "current_stderr_tail": current.stderr[-4096:],
            "dependency_healthy_elapsed_s": dependency_healthy_elapsed_s,
            "observations": observations,
        }
        print("RG6_CURRENT_DIAGNOSTIC=" + json.dumps(current_record, sort_keys=True))

        expanded_provider = DockerSbxProvider(_policy(), command_timeout_s=30)
        async with managed_sandbox(
            expanded_provider,
            repository,
            "rg6-readiness-expanded",
            lifecycle_artifact=expanded_lifecycle,
        ) as sandbox_id:
            expanded_started = time.monotonic()
            expanded = await expanded_provider.exec(
                sandbox_id,
                [*compose_prefix, "up", "-d", "--wait", "--wait-timeout", "120"],
                timeout_s=180,
            )
            expanded_elapsed_s = time.monotonic() - expanded_started
            assert expanded.exit_code == 0, (expanded.stdout, expanded.stderr)
            assert 60 < expanded_elapsed_s < 120
            expanded_state = await expanded_provider.exec(
                sandbox_id,
                [*compose_prefix, "ps", "--all", "--format", "json"],
                timeout_s=30,
            )
            assert expanded_state.exit_code == 0
            assert _service_health(expanded_state.stdout) == {
                "db": "healthy",
                "web": "healthy",
            }

        return {
            **current_record,
            "expanded_wait_timeout_s": 120,
            "expanded_exit_code": expanded.exit_code,
            "expanded_elapsed_s": expanded_elapsed_s,
            "expanded_state": _service_health(expanded_state.stdout),
        }

    try:
        result = asyncio.run(exercise())
        assert _lifecycle_events(current_lifecycle) == [
            "create_attempt",
            "create_success",
            "destroy_attempt",
            "destroy_success",
        ]
        assert _lifecycle_events(expanded_lifecycle) == [
            "create_attempt",
            "create_success",
            "destroy_attempt",
            "destroy_success",
        ]
        print("RG6_DIAGNOSTIC=" + json.dumps(result, sort_keys=True))
    finally:
        _assert_empty_inventory()
