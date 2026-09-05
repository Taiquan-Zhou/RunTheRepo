import asyncio
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from repotrial.sandbox.docker_sbx import DockerSbxPolicy, DockerSbxProvider
from repotrial.sandbox.lifecycle import managed_sandbox
from repotrial.trial.boot import boot_compose
from repotrial.trial.startup_inputs import (
    materialize_startup_input,
    plan_startup_input,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "startup_input"
_BUSYBOX_IMAGE = (
    "busybox:1.36.1@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
_EMPTY_INVENTORY = b"No sandboxes found.\nLaunch one: sbx run claude\n"


def test_startup_input_fixture_is_pinned_bounded_and_unprivileged() -> None:
    compose_path = _FIXTURE_ROOT / "compose.yaml"
    sample_path = _FIXTURE_ROOT / ".env.sample"
    health_path = _FIXTURE_ROOT / "www" / "health.txt"

    compose_text = compose_path.read_text(encoding="utf-8")
    compose = YAML(typ="safe").load(compose_text)
    service = compose["services"]["web"]

    assert service["image"] == _BUSYBOX_IMAGE
    assert service["user"] == "65534:65534"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["env_file"] == [".env"]
    assert service["command"] == [
        "httpd",
        "-f",
        "-p",
        "${LD_HOST_PORT:-8080}",
        "-h",
        "/www",
    ]
    assert service["ports"] == ["8080:8080"]
    assert service["configs"] == [{"source": "health", "target": "/www/health.txt"}]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8080/health.txt",
    ]
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "devices" not in service
    assert "volumes" not in service
    assert "docker.sock" not in compose_text
    assert compose["configs"] == {"health": {"file": "./www/health.txt"}}
    assert sample_path.read_text(encoding="utf-8").splitlines() == [
        "LD_HOST_PORT=9090",
        "ld_preload=./bad.so",
        "APP_OPTIONAL=",
    ]
    assert health_path.read_text(encoding="utf-8") == "ok\n"


def _create_fixture_repository(tmp_path: Path, *, empty_output: bool = False) -> Path:
    repository = tmp_path / "startup-input-repository"
    shutil.copytree(_FIXTURE_ROOT, repository)
    if empty_output:
        (repository / ".env.sample").write_text(
            "LD_HOST_PORT=9090\nld_preload=./bad.so\n", encoding="utf-8"
        )
    for argv in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "startup-input@example.invalid"],
        ["git", "config", "user.name", "RepoTrial Startup Input"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "trusted startup input fixture"],
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
        total_duration_s=300,
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


@pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_REAL_SBX") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_REAL_SBX=1 for startup-input proof",
)
@pytest.mark.parametrize(
    ("empty_output", "expected_output"),
    ((False, b"APP_OPTIONAL=\n"), (True, b"")),
    ids=("non-empty-output", "empty-output"),
)
def test_real_sbx_materializes_startup_input_before_boot(
    tmp_path: Path, empty_output: bool, expected_output: bytes
) -> None:
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    repository = _create_fixture_repository(tmp_path, empty_output=empty_output)
    evidence_path = tmp_path / "startup-input-attempt.jsonl"
    lifecycle_path = tmp_path / "startup-input-lifecycle.jsonl"
    plan = plan_startup_input(repository, "compose.yaml")
    assert plan is not None
    assert plan.output_bytes == expected_output
    assert plan.omitted_control_key_names == ("LD_HOST_PORT", "ld_preload")
    provider = DockerSbxProvider(_policy(), command_timeout_s=60)

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            repository,
            "startup-input-proof",
            lifecycle_artifact=lifecycle_path,
        ) as sandbox_id:
            materialized = await materialize_startup_input(
                provider,
                sandbox_id,
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=evidence_path,
            )
            assert materialized.target_mode == 0o600
            boot = await boot_compose(
                provider,
                sandbox_id,
                "compose.yaml",
                {},
                attempt=1,
                unset_env_keys=plan.all_source_key_names,
                project_directory=".",
            )
            assert boot.verdict.value == "pass"
            echo = await provider.exec(sandbox_id, ["echo", "ok"], timeout_s=30)
            assert echo.exit_code == 0
            assert echo.stdout == "ok\n"

    _assert_empty_inventory()
    try:
        asyncio.run(exercise())
    finally:
        _assert_empty_inventory()

    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    evidence_text = evidence_path.read_text(encoding="utf-8")
    evidence = [json.loads(line) for line in evidence_text.splitlines()]
    assert [row["outcome"] for row in evidence] == ["start", "terminal"]
    assert evidence[-1]["reason"] == "materialized"
    assert "9090" not in evidence_text
    assert "./bad.so" not in evidence_text
    assert "APP_OPTIONAL=" not in evidence_text
    assert "repotrial_empty_payload" not in evidence_text
    assert lifecycle_path.is_file()
