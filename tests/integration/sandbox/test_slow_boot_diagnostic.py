import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from repotrial.sandbox import docker_sbx
from repotrial.sandbox.base import ExecResult, get_sandbox_failure_evidence
from repotrial.sandbox.docker_sbx import (
    MAX_OUTPUT_BYTES,
    DockerSbxError,
    DockerSbxPolicy,
    DockerSbxProvider,
)
from repotrial.sandbox.lifecycle import managed_sandbox
from repotrial.trial import boot as boot_module
from repotrial.trial.boot import boot_compose

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


@pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_REAL_SBX") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_REAL_SBX=1 for the RG4 diagnostic",
)
def test_slow_boot_timeout_is_bounded_and_larger_budget_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    repository = _create_fixture_repository(tmp_path)
    original_spawn = docker_sbx.asyncio.create_subprocess_exec
    original_provider_exec = DockerSbxProvider.exec
    compose_up_processes: list[
        tuple[asyncio.subprocess.Process, tuple[object, ...]]
    ] = []
    provider_exec_calls: list[tuple[tuple[str, ...], int]] = []

    async def capture_compose_up_process(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        arguments = tuple(args)
        if (
            len(arguments) > 7
            and arguments[0] == "sbx"
            and arguments[1] == "exec"
            and "docker" in arguments
            and "compose" in arguments
            and "up" in arguments
        ):
            compose_up_processes.append((process, arguments))
        return process

    async def capture_provider_exec(
        self: DockerSbxProvider,
        sandbox_id: str,
        argv: list[str],
        timeout_s: int = 60,
    ) -> ExecResult:
        provider_exec_calls.append((tuple(argv), timeout_s))
        return await original_provider_exec(self, sandbox_id, argv, timeout_s)

    monkeypatch.setattr(
        docker_sbx.asyncio,
        "create_subprocess_exec",
        capture_compose_up_process,
    )
    monkeypatch.setattr(DockerSbxProvider, "exec", capture_provider_exec)

    async def exercise() -> None:
        timeout_record: dict[str, object] = {}
        current_provider = DockerSbxProvider(_policy(), command_timeout_s=30)
        async with managed_sandbox(
            current_provider,
            repository,
            "rg4-slow-current",
            lifecycle_artifact=tmp_path / "current-lifecycle.jsonl",
        ) as sandbox_id:
            operation_started = time.monotonic()
            with pytest.raises(DockerSbxError) as raised:
                await boot_compose(
                    current_provider,
                    sandbox_id,
                    "compose.yaml",
                    {},
                    1,
                )
            operation_elapsed_s = time.monotonic() - operation_started
            error = raised.value
            evidence = get_sandbox_failure_evidence(error)
            assert error.operation == "exec"
            assert error.reason == "timeout"
            assert evidence is not None
            assert evidence.operation == "exec"
            assert evidence.reason == "timeout"
            assert evidence.deadline_limited is False
            assert evidence.subprocess_started is True
            assert evidence.trial_elapsed_s is not None
            assert evidence.trial_remaining_s is not None
            assert 0 < evidence.trial_remaining_s < 900
            assert boot_module._COMPOSE_UP_TIMEOUT_S == 240
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
            assert 235 <= operation_elapsed_s < 270
            assert error.stderr == ""
            assert not hasattr(error, "stdout")
            assert compose_up_processes
            timed_out_process, timed_out_host_argv = compose_up_processes[-1]
            assert timed_out_process.returncode is not None
            assert timed_out_process.returncode < 0

            compose_state = await current_provider.exec(
                sandbox_id,
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.yaml",
                    "ps",
                    "--all",
                    "--format",
                    "json",
                ],
                timeout_s=30,
            )
            assert compose_state.exit_code == 0
            timeout_record = {
                "operation": evidence.operation,
                "reason": evidence.reason,
                "deadline_limited": evidence.deadline_limited,
                "subprocess_started": evidence.subprocess_started,
                "trial_elapsed_s": evidence.trial_elapsed_s,
                "trial_remaining_s": evidence.trial_remaining_s,
                "operation_elapsed_s": operation_elapsed_s,
                "provider_timeout_s": provider_exec_calls[0][1],
                "host_argv": [str(argument) for argument in timed_out_host_argv],
                "host_child_returncode": timed_out_process.returncode,
                "stderr_tail": error.stderr,
                "stdout_status": "not_exposed_by_timeout_exception",
                "compose_state_tail": compose_state.stdout[-4096:],
            }

        expanded_provider = DockerSbxProvider(_policy(), command_timeout_s=30)
        async with managed_sandbox(
            expanded_provider,
            repository,
            "rg4-slow-expanded",
            lifecycle_artifact=tmp_path / "expanded-lifecycle.jsonl",
        ) as sandbox_id:
            expanded_started = time.monotonic()
            result = await expanded_provider.exec(
                sandbox_id,
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.yaml",
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                ],
                timeout_s=300,
            )
            expanded_elapsed_s = time.monotonic() - expanded_started
            assert result.exit_code == 0, (result.stdout, result.stderr)
            assert 250 <= expanded_elapsed_s < 300
            assert len(result.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES
            assert len(result.stderr.encode("utf-8")) <= MAX_OUTPUT_BYTES
            state = await expanded_provider.exec(
                sandbox_id,
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.yaml",
                    "ps",
                    "--all",
                    "--format",
                    "json",
                ],
                timeout_s=30,
            )
            assert state.exit_code == 0
            assert '"Service":"web"' in state.stdout
            assert '"State":"running"' in state.stdout
            assert '"Health":"healthy"' in state.stdout

        current_lifecycle = (tmp_path / "current-lifecycle.jsonl").read_text(
            encoding="utf-8"
        )
        expanded_lifecycle = (tmp_path / "expanded-lifecycle.jsonl").read_text(
            encoding="utf-8"
        )
        assert [
            json.loads(line)["event"] for line in current_lifecycle.splitlines()
        ] == ["create_attempt", "create_success", "destroy_attempt", "destroy_success"]
        assert [
            json.loads(line)["event"] for line in expanded_lifecycle.splitlines()
        ] == ["create_attempt", "create_success", "destroy_attempt", "destroy_success"]

        print(
            "RG4_DIAGNOSTIC="
            + json.dumps(
                {
                    "current_limit_s": boot_module._COMPOSE_UP_TIMEOUT_S,
                    "expanded_limit_s": 300,
                    "expanded_elapsed_s": expanded_elapsed_s,
                    "timeout": timeout_record,
                    "current_lifecycle": current_lifecycle.splitlines(),
                    "expanded_lifecycle": expanded_lifecycle.splitlines(),
                    "expanded_state_tail": state.stdout[-4096:],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    try:
        asyncio.run(exercise())
    finally:
        _assert_empty_inventory()
