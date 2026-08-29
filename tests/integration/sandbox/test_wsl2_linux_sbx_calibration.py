import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import pytest

from repotrial.sandbox import docker_sbx
from repotrial.sandbox.docker_sbx import (
    DockerSbxError,
    DockerSbxPolicy,
    DockerSbxProvider,
    calculate_disk_allocation,
)
from repotrial.sandbox.lifecycle import managed_sandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_WSL2_SBX_CALIBRATION") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_WSL2_SBX_CALIBRATION=1",
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "wsl2_compose"
_EXPECTED_FILES = {
    "compose.yaml",
    "www/health.txt",
    "www/index.html",
}
_MARKER = b"repotrial-wsl2-sbx-ok\n"
_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _TrustedFixtureRepository:
    path: Path
    commit: str
    parent_sentinel: str


@dataclass(frozen=True, slots=True)
class _Mount:
    source: str
    filesystem: str
    target: str


def _run_host(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_s: int = 15,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        check=False,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout_s,
    )


def _require_supported_host(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("UNSUPPORTED: WSL2/Linux calibration requires Linux")

    try:
        kernel_release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        pytest.skip("UNSUPPORTED: WSL2 kernel release is unavailable")
    if "microsoft" not in kernel_release.lower() or not os.environ.get("WSL_INTEROP"):
        pytest.skip("UNSUPPORTED: WSL2 host is required")
    if not Path("/dev/kvm").exists():
        pytest.skip("UNSUPPORTED: /dev/kvm is unavailable")
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    if shutil.which("git") is None:
        pytest.skip("UNSUPPORTED: git executable is unavailable")
    if shutil.which("findmnt") is None:
        pytest.skip("UNSUPPORTED: findmnt executable is unavailable")

    for label, path in (("fixture", _FIXTURE_ROOT), ("temporary", tmp_path)):
        result = _run_host(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)],
            timeout_s=5,
        )
        if result.returncode != 0 or result.stdout.decode("utf-8").strip() != "ext4":
            pytest.skip(f"UNSUPPORTED: {label} path must be ext4")


def _create_trusted_fixture_repository(tmp_path: Path) -> _TrustedFixtureRepository:
    sentinel = f"repotrial-parent-sentinel-{uuid.uuid4().hex}"
    (tmp_path / sentinel).write_text(
        "not part of calibration fixture\n", encoding="utf-8"
    )
    repository = tmp_path / "trusted-fixture"
    shutil.copytree(_FIXTURE_ROOT, repository)

    for argv in (
        ["git", "init", "--quiet"],
        ["git", "add", "--all"],
        [
            "git",
            "-c",
            "user.name=RepoTrial Calibration",
            "-c",
            "user.email=calibration@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "trusted fixture",
        ],
    ):
        result = _run_host(argv, cwd=repository)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    files = _run_host(["git", "ls-files", "-z"], cwd=repository)
    assert files.returncode == 0
    committed_files = {
        item for item in files.stdout.decode("utf-8").split("\0") if item
    }
    assert committed_files == _EXPECTED_FILES

    commit = _run_host(["git", "rev-parse", "HEAD"], cwd=repository)
    assert commit.returncode == 0
    commit_sha = commit.stdout.decode("ascii").strip()
    assert len(commit_sha) == 40
    return _TrustedFixtureRepository(repository, commit_sha, sentinel)


def _policy(*, total_duration_s: int) -> DockerSbxPolicy:
    return DockerSbxPolicy(
        cpus=1,
        memory_mb=1024,
        pids_limit=64,
        disk_mb=512,
        total_duration_s=total_duration_s,
    )


async def _exec(
    provider: DockerSbxProvider,
    sandbox_id: str,
    argv: list[str],
    *,
    timeout_s: int = 30,
) -> str:
    result = await provider.exec(sandbox_id, argv, timeout_s=timeout_s)
    assert result.exit_code == 0, (argv, result.stderr, result.stdout)
    return result.stdout


def _parse_mount(output: str) -> _Mount:
    lines = [line for line in output.splitlines() if line]
    assert len(lines) == 1
    fields = lines[0].split(maxsplit=2)
    assert len(fields) == 3
    source, filesystem, target = fields
    assert source and filesystem and target
    return _Mount(source=source, filesystem=filesystem, target=target)


def _parse_df_total(output: str) -> int:
    lines = [line for line in output.splitlines() if line]
    assert len(lines) >= 2
    fields = lines[-1].split()
    assert len(fields) >= 6
    total = int(fields[1])
    assert total > 0
    return total


def _parse_mem_total_kib(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            assert len(fields) == 3 and fields[2] == "kB"
            return int(fields[1])
    raise AssertionError("MemTotal is missing")


def _parse_compose_ps(output: str) -> list[dict[str, object]]:
    decoded = json.loads(output)
    assert isinstance(decoded, list) and decoded
    assert all(isinstance(item, dict) for item in decoded)
    return decoded


async def _assert_guest_clone_contract(
    provider: DockerSbxProvider,
    sandbox_id: str,
    fixture: _TrustedFixtureRepository,
) -> str:
    guest_workspace = (await _exec(provider, sandbox_id, ["pwd"])).strip()
    assert guest_workspace.startswith("/")
    assert (
        await _exec(provider, sandbox_id, ["git", "rev-parse", "HEAD"])
    ).strip() == fixture.commit
    assert (
        await _exec(provider, sandbox_id, ["git", "rev-parse", "--show-toplevel"])
    ).strip() == guest_workspace
    files = await _exec(provider, sandbox_id, ["git", "ls-files", "-z"])
    assert {item for item in files.split("\0") if item} == _EXPECTED_FILES
    assert (
        await _exec(provider, sandbox_id, ["git", "status", "--porcelain"])
    ).strip() == ""
    assert (
        await _exec(
            provider,
            sandbox_id,
            ["find", ".", "-xdev", "-name", fixture.parent_sentinel, "-print"],
        )
    ).strip() == ""
    return guest_workspace


async def _assert_resources(
    provider: DockerSbxProvider,
    sandbox_id: str,
    guest_workspace: str,
    policy: DockerSbxPolicy,
) -> None:
    assert (
        await _exec(provider, sandbox_id, ["getconf", "_NPROCESSORS_ONLN"])
    ).strip() == "1"
    mem_total_kib = _parse_mem_total_kib(
        await _exec(provider, sandbox_id, ["cat", "/proc/meminfo"])
    )
    assert 0 < mem_total_kib <= 1024 * 1024

    allocation = calculate_disk_allocation(policy.disk_mb)
    filesystem_checks = (
        ("/", allocation.root_mb),
        ("/var/lib/docker", allocation.docker_mb),
        (guest_workspace, allocation.workspace_mb),
    )
    mounts: list[_Mount] = []
    for path, size_mb in filesystem_checks:
        mounts.append(
            _parse_mount(
                await _exec(
                    provider,
                    sandbox_id,
                    ["findmnt", "-n", "-o", "SOURCE,FSTYPE,TARGET", "--target", path],
                )
            )
        )
        total_bytes = _parse_df_total(
            await _exec(provider, sandbox_id, ["df", "-B1", "-P", path])
        )
        assert total_bytes <= size_mb * _MIB
    assert len({mount.source for mount in mounts}) == len(mounts)


async def _assert_network_policy(provider: DockerSbxProvider, sandbox_id: str) -> None:
    public_body = await _exec(
        provider,
        sandbox_id,
        ["wget", "-q", "-O", "-", "http://example.com/"],
        timeout_s=15,
    )
    assert "Example Domain" in public_body

    probes = {
        "http://169.254.169.254/latest/meta-data/": {
            "169.254.169.254",
            "169.254.169.254/32",
            "169.254.0.0/16",
        },
        "http://10.255.255.1:81/": {"10.255.255.1", "10.0.0.0/8"},
        "http://host.docker.internal:80/": {"host.docker.internal"},
    }
    for probe in probes:
        result = await provider.exec(
            sandbox_id,
            ["wget", "-q", "-O", "-", probe],
            timeout_s=10,
        )
        assert result.exit_code != 0, (probe, result.stdout, result.stderr)

    network_log = await provider.network_log(sandbox_id)
    if not network_log.supported:
        pytest.skip(
            f"UNSUPPORTED: network_log is unavailable: {network_log.unsupported_reason}"
        )
    for probe, expected_evidence in probes.items():
        assert any(
            event["decision"] == "blocked"
            and any(
                token in " ".join((event["host"], event["rule"], event["reason"]))
                for token in expected_evidence
            )
            for event in network_log.events
        ), probe


async def _assert_published_marker(
    provider: DockerSbxProvider,
    sandbox_id: str,
    guest_workspace: str,
    destination: Path,
) -> None:
    host_port = await provider.publish_port(sandbox_id, 8080)
    assert type(host_port) is int and 1 <= host_port <= 65_535
    assert await asyncio.to_thread(_read_loopback_marker, host_port) == _MARKER
    assert not destination.exists()
    await provider.copy(
        sandbox_id,
        f"{guest_workspace}/www/index.html",
        destination,
    )
    assert destination.read_bytes() == _MARKER


def _read_loopback_marker(host_port: int) -> bytes:
    with urlopen(f"http://127.0.0.1:{host_port}", timeout=10) as response:
        return response.read()


def _assert_empty_sbx_inventory() -> None:
    result = _run_host(["sbx", "list"], timeout_s=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    inventory = result.stdout.decode("utf-8").strip()
    assert inventory in {"", "No sandboxes found", "No sandboxes found."}, inventory


async def _warm_image_cache(
    fixture: _TrustedFixtureRepository,
    tmp_path: Path,
) -> None:
    provider = DockerSbxProvider(_policy(total_duration_s=120), command_timeout_s=30)
    async with managed_sandbox(
        provider,
        fixture.path,
        "wsl2-cache",
        lifecycle_artifact=tmp_path / "cache-lifecycle.jsonl",
    ) as sandbox_id:
        await _exec(
            provider,
            sandbox_id,
            ["docker", "compose", "pull"],
            timeout_s=60,
        )


def test_wsl2_linux_sbx_primary_calibration(tmp_path: Path) -> None:
    _require_supported_host(tmp_path)
    fixture = _create_trusted_fixture_repository(tmp_path)
    policy = _policy(total_duration_s=180)

    async def exercise() -> None:
        provider = DockerSbxProvider(policy, command_timeout_s=30)
        async with managed_sandbox(
            provider,
            fixture.path,
            "wsl2-primary",
            lifecycle_artifact=tmp_path / "primary-lifecycle.jsonl",
        ) as sandbox_id:
            guest_workspace = await _assert_guest_clone_contract(
                provider, sandbox_id, fixture
            )
            await _exec(
                provider,
                sandbox_id,
                ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "60"],
                timeout_s=90,
            )
            services = _parse_compose_ps(
                await _exec(
                    provider,
                    sandbox_id,
                    ["docker", "compose", "ps", "--format", "json"],
                )
            )
            assert any(service.get("Service") == "web" for service in services)
            await _assert_resources(provider, sandbox_id, guest_workspace, policy)
            await _assert_network_policy(provider, sandbox_id)
            await _assert_published_marker(
                provider,
                sandbox_id,
                guest_workspace,
                tmp_path / "copied-index.html",
            )

    try:
        asyncio.run(exercise())
    finally:
        _assert_empty_sbx_inventory()


def test_wsl2_linux_sbx_timeout_calibration(tmp_path: Path) -> None:
    _require_supported_host(tmp_path)
    fixture = _create_trusted_fixture_repository(tmp_path)

    async def exercise() -> None:
        await _warm_image_cache(fixture, tmp_path)
        provider = DockerSbxProvider(_policy(total_duration_s=45), command_timeout_s=30)
        async with managed_sandbox(
            provider,
            fixture.path,
            "wsl2-timeout",
            lifecycle_artifact=tmp_path / "timeout-lifecycle.jsonl",
        ) as sandbox_id:
            with pytest.raises(DockerSbxError) as timed_out:
                await provider.exec(sandbox_id, ["sleep", "120"], timeout_s=120)
            assert timed_out.value.operation == "exec"
            assert timed_out.value.reason == "total_duration_exhausted"
            with pytest.raises(DockerSbxError) as expired:
                await provider.exec(sandbox_id, ["true"], timeout_s=1)
            assert expired.value.operation == "exec"
            assert expired.value.reason == "total_duration_exhausted"

    try:
        asyncio.run(exercise())
    finally:
        _assert_empty_sbx_inventory()


def test_wsl2_linux_sbx_cancellation_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_supported_host(tmp_path)
    fixture = _create_trusted_fixture_repository(tmp_path)
    exec_spawned = asyncio.Event()
    original_create_subprocess_exec = docker_sbx.asyncio.create_subprocess_exec

    async def record_real_sbx_exec(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        if args[:2] == ("sbx", "exec"):
            exec_spawned.set()
        return await original_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(
        docker_sbx.asyncio,
        "create_subprocess_exec",
        record_real_sbx_exec,
    )

    async def exercise() -> None:
        provider = DockerSbxProvider(
            _policy(total_duration_s=120), command_timeout_s=30
        )
        async with managed_sandbox(
            provider,
            fixture.path,
            "wsl2-cancellation",
            lifecycle_artifact=tmp_path / "cancellation-lifecycle.jsonl",
        ) as sandbox_id:
            workload = asyncio.create_task(
                provider.exec(sandbox_id, ["sleep", "120"], timeout_s=120)
            )
            await asyncio.wait_for(exec_spawned.wait(), timeout=30)
            workload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await workload

    try:
        asyncio.run(exercise())
    finally:
        _assert_empty_sbx_inventory()
