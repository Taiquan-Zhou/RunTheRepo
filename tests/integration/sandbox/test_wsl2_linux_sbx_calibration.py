import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
_MAX_HOST_OUTPUT_BYTES = 65_536
_CANONICAL_EMPTY_SBX_INVENTORY = b"No sandboxes found.\n"
_MAJOR_MINOR_PATTERN = re.compile(r"[0-9]+:[0-9]+")
_HOST_CLEANUP_TIMEOUT_S = 5.0
_HOST_READER_NAME_PREFIX = "repotrial-host-reader-"
_HOST_CLOSER_NAME_PREFIX = "repotrial-host-closer-"
_HOST_KILLER_NAME_PREFIX = "repotrial-host-killer-"


@dataclass(frozen=True, slots=True)
class _TrustedFixtureRepository:
    path: Path
    commit: str
    parent_sentinel: str


@dataclass(frozen=True, slots=True)
class _Mount:
    source: str
    filesystem: str
    major_minor: str
    uuid: str
    target: str

    @property
    def identity(self) -> tuple[str, str]:
        return (self.major_minor, self.uuid)


@dataclass(frozen=True, slots=True)
class _HostCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _LoopbackResponse(Protocol):
    def geturl(self) -> str: ...

    def read(self, size: int = -1) -> bytes: ...


class _RejectLoopbackRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        fp: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> Request:
        raise AssertionError("loopback marker request redirected")


_LOOPBACK_OPENER = build_opener(_RejectLoopbackRedirect())


def _run_host(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_s: int = 15,
) -> _HostCommandResult:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    outputs: dict[str, bytes] = {}
    failures: list[Exception] = []
    closing_streams = threading.Event()

    def read_stream(name: str, stream: BinaryIO) -> None:
        try:
            outputs[name] = _read_bounded_host_stream(stream)
        except AssertionError as error:
            failures.append(error)
        except (OSError, ValueError) as error:
            if not closing_streams.is_set():
                failures.append(error)

    readers = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout),
            name=f"{_HOST_READER_NAME_PREFIX}stdout",
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr),
            name=f"{_HOST_READER_NAME_PREFIX}stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        cleanup_deadline = time.monotonic() + _HOST_CLEANUP_TIMEOUT_S
        cleanup_failures = _kill_and_reap_host_process(process, cleanup_deadline)
        closing_streams.set()
        cleanup_failures.extend(
            _close_host_streams((process.stdout, process.stderr), cleanup_deadline)
        )
        cleanup_failures.extend(_join_host_threads(readers, cleanup_deadline))
        if cleanup_failures:
            raise AssertionError(
                f"trusted host timeout cleanup unconfirmed: {'; '.join(cleanup_failures)}"
            ) from error
        raise AssertionError(f"trusted host command timed out: {argv[0]}") from error

    cleanup_deadline = time.monotonic() + _HOST_CLEANUP_TIMEOUT_S
    reader_cleanup_failures = _join_host_threads(readers, cleanup_deadline)
    if reader_cleanup_failures:
        closing_streams.set()
        reader_cleanup_failures.extend(
            _close_host_streams((process.stdout, process.stderr), cleanup_deadline)
        )
        reader_cleanup_failures.extend(_join_host_threads(readers, cleanup_deadline))
        raise AssertionError(
            "trusted host reader cleanup unconfirmed: "
            f"{'; '.join(reader_cleanup_failures)}"
        )
    assert not failures, str(failures[0])
    missing_outputs = {"stdout", "stderr"}.difference(outputs)
    assert not missing_outputs, "trusted host output missing: " + ", ".join(
        sorted(missing_outputs)
    )
    return _HostCommandResult(
        returncode=returncode,
        stdout=outputs["stdout"],
        stderr=outputs["stderr"],
    )


def _read_bounded_host_stream(stream: BinaryIO) -> bytes:
    collected = bytearray()
    exceeded = False
    while chunk := stream.read(8192):
        remaining = _MAX_HOST_OUTPUT_BYTES + 1 - len(collected)
        if remaining > 0:
            collected.extend(chunk[:remaining])
        if len(chunk) > remaining or len(collected) > _MAX_HOST_OUTPUT_BYTES:
            exceeded = True
    assert not exceeded, "trusted host command output exceeded bounded limit"
    return bytes(collected)


def _kill_and_reap_host_process(
    process: subprocess.Popen[bytes], deadline: float
) -> list[str]:
    failures = _run_bounded_host_operation("kill", process.kill, deadline)
    remaining = _remaining_cleanup_time(deadline)
    if remaining <= 0:
        failures.append("reap_deadline_exhausted")
        return failures
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        failures.append("reap_timeout")
    except OSError as error:
        failures.append(f"reap_failed:{type(error).__name__}")
    return failures


def _run_bounded_host_operation(
    operation_name: str, operation: Callable[[], None], deadline: float
) -> list[str]:
    failures: list[str] = []

    def run_operation() -> None:
        try:
            operation()
        except (OSError, RuntimeError, ValueError) as error:
            failures.append(f"{operation_name}_failed:{type(error).__name__}")

    worker = threading.Thread(
        target=run_operation,
        name=f"{_HOST_KILLER_NAME_PREFIX}{operation_name}",
        daemon=True,
    )
    worker.start()
    failures.extend(_join_host_threads([worker], deadline))
    return failures


def _close_host_streams(streams: tuple[BinaryIO, ...], deadline: float) -> list[str]:
    failures: list[str] = []

    def close_stream(stream: BinaryIO) -> None:
        try:
            stream.close()
        except (OSError, ValueError) as error:
            failures.append(f"pipe_close_failed:{type(error).__name__}")

    closers = [
        threading.Thread(
            target=close_stream,
            args=(stream,),
            name=f"{_HOST_CLOSER_NAME_PREFIX}{index}",
            daemon=True,
        )
        for index, stream in enumerate(streams)
    ]
    for closer in closers:
        closer.start()
    failures.extend(_join_host_threads(closers, deadline))
    return failures


def _join_host_threads(threads: list[threading.Thread], deadline: float) -> list[str]:
    for thread in threads:
        thread.join(timeout=_remaining_cleanup_time(deadline))
    return [
        f"thread_not_terminated:{thread.name}"
        for thread in threads
        if thread.is_alive()
    ]


def _remaining_cleanup_time(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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


def _parse_mount(output: str, requested_path: str) -> _Mount:
    lines = [line for line in output.splitlines() if line]
    assert len(lines) == 1
    fields = lines[0].split(maxsplit=4)
    assert len(fields) == 5
    source, filesystem, major_minor, filesystem_uuid, target = fields
    assert source and filesystem and _MAJOR_MINOR_PATTERN.fullmatch(major_minor)
    assert filesystem_uuid and target == requested_path
    return _Mount(
        source=source,
        filesystem=filesystem,
        major_minor=major_minor,
        uuid=filesystem_uuid,
        target=target,
    )


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
    await _exec(
        provider,
        sandbox_id,
        ["test", "!", "-e", f"../{fixture.parent_sentinel}"],
    )
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
                    [
                        "findmnt",
                        "-n",
                        "-o",
                        "SOURCE,FSTYPE,MAJ:MIN,UUID,TARGET",
                        "--target",
                        path,
                    ],
                ),
                path,
            )
        )
        total_bytes = _parse_df_total(
            await _exec(provider, sandbox_id, ["df", "-B1", "-P", path])
        )
        assert total_bytes <= size_mb * _MIB
    assert len({mount.identity for mount in mounts}) == len(mounts)


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
    for probe, expected_evidence in probes.items():
        before = await _observed_network_events(provider, sandbox_id)
        result = await provider.exec(
            sandbox_id,
            ["wget", "-q", "-O", "-", probe],
            timeout_s=10,
        )
        assert result.exit_code != 0, (probe, result.stdout, result.stderr)
        after = await _observed_network_events(provider, sandbox_id)
        assert _has_fresh_blocked_evidence(before, after, expected_evidence), probe


async def _observed_network_events(
    provider: DockerSbxProvider, sandbox_id: str
) -> list[dict[str, object]]:
    network_log = await provider.network_log(sandbox_id)
    if not network_log.supported:
        pytest.skip(
            f"UNSUPPORTED: network_log is unavailable: {network_log.unsupported_reason}"
        )
    return [dict(event) for event in network_log.events]


def _has_fresh_blocked_evidence(
    before: list[dict[str, object]],
    after: list[dict[str, object]],
    expected_evidence: set[str],
) -> bool:
    before_events, before_poisoned, before_globally_poisoned = (
        _aggregate_blocked_evidence(before, expected_evidence)
    )
    after_events, after_poisoned, after_globally_poisoned = _aggregate_blocked_evidence(
        after, expected_evidence
    )
    if before_globally_poisoned or after_globally_poisoned:
        return False
    for key, (count, last_seen) in after_events.items():
        if key in before_poisoned or key in after_poisoned:
            continue
        prior = before_events.get(key)
        if prior is None:
            return True
        prior_count, prior_last_seen = prior
        if (
            count >= prior_count
            and last_seen >= prior_last_seen
            and (count > prior_count or last_seen > prior_last_seen)
        ):
            return True
    return False


def _aggregate_blocked_evidence(
    events: list[dict[str, object]], expected_evidence: set[str]
) -> tuple[
    dict[tuple[str, str, str, str], tuple[int, datetime]],
    set[tuple[str, str, str, str]],
    bool,
]:
    aggregated: dict[tuple[str, str, str, str], tuple[int, datetime]] = {}
    poisoned: set[tuple[str, str, str, str]] = set()
    globally_poisoned = False
    for event in events:
        key = _blocked_evidence_key(event, expected_evidence)
        observed = _blocked_evidence_observation(event, expected_evidence)
        if observed is None:
            if key is None:
                globally_poisoned = (
                    globally_poisoned
                    or _is_malformed_expected_evidence(event, expected_evidence)
                )
            else:
                poisoned.add(key)
            continue
        _, count, last_seen = observed
        prior = aggregated.get(key)
        if prior is None:
            aggregated[key] = (count, last_seen)
        else:
            aggregated[key] = (max(prior[0], count), max(prior[1], last_seen))
    return aggregated, poisoned, globally_poisoned


def _blocked_evidence_observation(
    event: dict[str, object], expected_evidence: set[str]
) -> tuple[tuple[str, str, str, str], int, datetime] | None:
    key = _blocked_evidence_key(event, expected_evidence)
    if key is None:
        return None
    last_seen = event.get("last_seen")
    count = event.get("count")
    if not isinstance(last_seen, str) or type(count) is not int or count < 1:
        return None
    timestamp = _parse_network_timestamp(last_seen)
    if timestamp is None:
        return None
    return (key, count, timestamp)


def _blocked_evidence_key(
    event: dict[str, object], expected_evidence: set[str]
) -> tuple[str, str, str, str] | None:
    if event.get("decision") != "blocked":
        return None
    host = event.get("host")
    rule = event.get("rule")
    reason = event.get("reason")
    proxy = event.get("proxy")
    if not all(isinstance(value, str) for value in (host, rule, reason, proxy)):
        return None
    assert isinstance(host, str)
    assert isinstance(rule, str)
    assert isinstance(reason, str)
    assert isinstance(proxy, str)
    if not any(token in f"{host} {rule} {reason}" for token in expected_evidence):
        return None
    return (host, rule, reason, proxy)


def _is_malformed_expected_evidence(
    event: dict[str, object], expected_evidence: set[str]
) -> bool:
    if event.get("decision") != "blocked":
        return False
    evidence_fields = (event.get("host"), event.get("rule"), event.get("reason"))
    return any(
        isinstance(field, str) and token in field
        for field in evidence_fields
        for token in expected_evidence
    )


def _parse_network_timestamp(value: str) -> datetime | None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    return timestamp


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
    url = f"http://127.0.0.1:{host_port}"
    with _LOOPBACK_OPENER.open(url, timeout=10) as response:
        return _read_exact_loopback_response(response, url)


def _read_exact_loopback_response(
    response: _LoopbackResponse, expected_url: str
) -> bytes:
    assert response.geturl() == expected_url
    body = response.read(len(_MARKER) + 1)
    assert body == _MARKER
    return body


def _assert_empty_sbx_inventory() -> None:
    result = _run_host(["sbx", "list"], timeout_s=15)
    assert result.returncode == 0
    _parse_empty_sbx_inventory(result.stdout, result.stderr)


def _parse_empty_sbx_inventory(stdout: bytes, stderr: bytes) -> None:
    assert stderr == b""
    assert stdout == _CANONICAL_EMPTY_SBX_INVENTORY


def _matches_workload_spawn(
    arguments: tuple[object, ...],
    *,
    sandbox_id: str,
    workload: tuple[str, ...],
) -> bool:
    return arguments == ("sbx", "exec", sandbox_id, "--", *workload)


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


def test_wsl2_linux_sbx_timeout_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_supported_host(tmp_path)
    fixture = _create_trusted_fixture_repository(tmp_path)
    original_create_subprocess_exec = docker_sbx.asyncio.create_subprocess_exec
    second_workload_spawns = 0
    guarded_arguments: tuple[object, ...] | None = None

    async def reject_expired_workload_spawn(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        nonlocal second_workload_spawns
        if guarded_arguments is not None and tuple(args) == guarded_arguments:
            second_workload_spawns += 1
            raise AssertionError("expired workload reached sbx exec spawn")
        return await original_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(
        docker_sbx.asyncio,
        "create_subprocess_exec",
        reject_expired_workload_spawn,
    )

    async def exercise() -> None:
        nonlocal guarded_arguments
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
            guarded_arguments = ("sbx", "exec", sandbox_id, "--", "true")
            spawn_count_before = second_workload_spawns
            with pytest.raises(DockerSbxError) as expired:
                await provider.exec(sandbox_id, ["true"], timeout_s=1)
            assert expired.value.operation == "exec"
            assert expired.value.reason == "total_duration_exhausted"
            assert second_workload_spawns == spawn_count_before

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
    owned_sandbox_id: str | None = None

    async def record_real_sbx_exec(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        process = await original_create_subprocess_exec(*args, **kwargs)
        if owned_sandbox_id is not None and _matches_workload_spawn(
            tuple(args),
            sandbox_id=owned_sandbox_id,
            workload=("sleep", "120"),
        ):
            exec_spawned.set()
        return process

    monkeypatch.setattr(
        docker_sbx.asyncio,
        "create_subprocess_exec",
        record_real_sbx_exec,
    )

    async def exercise() -> None:
        nonlocal owned_sandbox_id
        provider = DockerSbxProvider(
            _policy(total_duration_s=120), command_timeout_s=30
        )
        async with managed_sandbox(
            provider,
            fixture.path,
            "wsl2-cancellation",
            lifecycle_artifact=tmp_path / "cancellation-lifecycle.jsonl",
        ) as sandbox_id:
            owned_sandbox_id = sandbox_id
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
