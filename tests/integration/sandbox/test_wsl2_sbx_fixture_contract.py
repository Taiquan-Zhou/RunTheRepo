import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from ruamel.yaml import YAML

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "wsl2_compose"
_CALIBRATION_TEST_PATH = Path(__file__).with_name("test_wsl2_linux_sbx_calibration.py")


@pytest.fixture(scope="module")
def calibration_module() -> ModuleType:
    module_name = "_repotrial_wsl2_sbx_calibration_contract"
    specification = importlib.util.spec_from_file_location(
        module_name, _CALIBRATION_TEST_PATH
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def test_wsl2_compose_fixture_has_pinned_minimal_web_service() -> None:
    """Detect a mutable or privileged calibration fixture before runtime use."""
    compose_path = _FIXTURE_ROOT / "compose.yaml"
    fixture_text = compose_path.read_text(encoding="utf-8")
    compose = YAML(typ="safe").load(fixture_text)
    service = compose["services"]["web"]

    assert service["image"] == (
        "busybox:1.36.1@sha256:"
        "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
    )
    assert service["command"] == ["httpd", "-f", "-p", "8080", "-h", "/www"]
    assert service["volumes"] == ["./www:/www:ro"]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8080/health.txt",
    ]
    assert "ports" not in service
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "/var/run/docker.sock" not in fixture_text
    assert (_FIXTURE_ROOT / "www" / "index.html").read_text(encoding="utf-8") == (
        "repotrial-wsl2-sbx-ok\n"
    )
    assert (_FIXTURE_ROOT / "www" / "health.txt").read_text(encoding="utf-8") == "ok\n"


def test_calibration_workload_spawn_guard_only_matches_owned_sleep(
    calibration_module: ModuleType,
) -> None:
    assert calibration_module._matches_workload_spawn(
        ("sbx", "exec", "owned", "--", "sleep", "120"),
        sandbox_id="owned",
        workload=("sleep", "120"),
    )
    assert not calibration_module._matches_workload_spawn(
        ("sbx", "exec", "--help"),
        sandbox_id="owned",
        workload=("sleep", "120"),
    )
    assert not calibration_module._matches_workload_spawn(
        ("sbx", "exec", "other", "--", "sleep", "120"),
        sandbox_id="owned",
        workload=("sleep", "120"),
    )


def test_calibration_mount_parser_binds_requested_path_to_real_identity(
    calibration_module: ModuleType,
) -> None:
    mount = calibration_module._parse_mount(
        "/dev/vdb ext4 8:16 fixture-uuid /workspace",
        "/workspace",
    )
    assert mount.identity == ("8:16", "fixture-uuid")
    with pytest.raises(AssertionError):
        calibration_module._parse_mount(
            "/dev/vdb ext4 8:16 fixture-uuid /", "/workspace"
        )


def test_calibration_network_evidence_requires_a_new_or_advanced_event(
    calibration_module: ModuleType,
) -> None:
    event = {
        "sandbox": "owned",
        "decision": "blocked",
        "host": "169.254.169.254",
        "proxy": "network",
        "rule": "169.254.169.254/32",
        "reason": "deny",
        "last_seen": "2026-08-30T00:00:00Z",
        "count": 1,
    }
    expected_evidence = {"169.254.169.254", "169.254.169.254/32"}
    assert not calibration_module._has_fresh_blocked_evidence(
        [event], [event], expected_evidence
    )
    assert calibration_module._has_fresh_blocked_evidence(
        [event], [{**event, "count": 2}], expected_evidence
    )
    assert calibration_module._has_fresh_blocked_evidence(
        [], [event], expected_evidence
    )


def test_calibration_network_evidence_aggregates_duplicates_and_rejects_regression(
    calibration_module: ModuleType,
) -> None:
    def event(count: int, last_seen: str) -> dict[str, object]:
        return {
            "sandbox": "owned",
            "decision": "blocked",
            "host": "169.254.169.254",
            "proxy": "network",
            "rule": "169.254.169.254/32",
            "reason": "deny",
            "last_seen": last_seen,
            "count": count,
        }

    expected_evidence = {"169.254.169.254", "169.254.169.254/32"}
    highest = event(10, "2026-08-30T10:00:00Z")
    lowest = event(1, "2026-08-30T01:00:00Z")
    stale_after = event(2, "2026-08-30T02:00:00Z")
    assert not calibration_module._has_fresh_blocked_evidence(
        [highest, lowest], [stale_after], expected_evidence
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [lowest, highest], [stale_after], expected_evidence
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [event(1, "2026-08-30T10:00:00Z")],
        [event(1, "2026-08-30T09:00:00Z")],
        expected_evidence,
    )
    assert calibration_module._has_fresh_blocked_evidence(
        [event(1, "2026-08-30T10:00:00Z")],
        [event(2, "2026-08-30T10:00:00Z")],
        expected_evidence,
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [event(1, "2026-08-30T10:00:00Z")],
        [event(2, "not-a-timestamp")],
        expected_evidence,
    )


def test_calibration_inventory_parser_only_accepts_canonical_empty_output(
    calibration_module: ModuleType,
) -> None:
    assert (
        calibration_module._parse_empty_sbx_inventory(b"No sandboxes found.\n", b"")
        is None
    )
    for stdout, stderr in (
        (b"", b""),
        (b"No sandboxes found\n", b""),
        (b"No sandboxes found.\n", b"warning\n"),
    ):
        with pytest.raises(AssertionError):
            calibration_module._parse_empty_sbx_inventory(stdout, stderr)


def test_calibration_host_stream_reader_rejects_unbounded_output(
    calibration_module: ModuleType,
) -> None:
    assert calibration_module._read_bounded_host_stream(io.BytesIO(b"ok")) == b"ok"
    with pytest.raises(AssertionError):
        calibration_module._read_bounded_host_stream(
            io.BytesIO(b"x" * (calibration_module._MAX_HOST_OUTPUT_BYTES + 1))
        )


def test_calibration_host_timeout_is_bounded_when_reader_pipe_never_closes() -> None:
    script = f"""
import importlib.util
import subprocess
import sys
import threading

specification = importlib.util.spec_from_file_location("calibration", {str(_CALIBRATION_TEST_PATH)!r})
assert specification is not None and specification.loader is not None
module = importlib.util.module_from_spec(specification)
sys.modules["calibration"] = module
specification.loader.exec_module(module)
module._HOST_CLEANUP_TIMEOUT_S = 0.05

class InheritedPipe:
    def __init__(self):
        self.release = threading.Event()
        self.close_called = False

    def read(self, size):
        self.release.wait()
        return b""

    def close(self):
        self.close_called = True

class Process:
    def __init__(self):
        self.stdout = InheritedPipe()
        self.stderr = InheritedPipe()
        self.killed = False
        self.reaped = False

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        self.reaped = True
        return 137

    def kill(self):
        self.killed = True

process = Process()
module.subprocess.Popen = lambda *args, **kwargs: process
try:
    module._run_host(["fake"], timeout_s=0.01)
except AssertionError as error:
    assert "cleanup unconfirmed" in str(error)
else:
    raise AssertionError("expected bounded timeout cleanup failure")
assert process.killed and process.reaped
assert process.stdout.close_called and process.stderr.close_called
readers = [thread for thread in threading.enumerate() if thread.name.startswith("repotrial-host-reader")]
assert readers and all(thread.daemon for thread in readers)
print("bounded-timeout")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout.strip() == b"bounded-timeout"


def test_calibration_loopback_reader_reads_only_marker_plus_one_byte(
    calibration_module: ModuleType,
) -> None:
    class Response:
        def __init__(self, body: bytes, url: str) -> None:
            self.body = body
            self.url = url
            self.read_sizes: list[int] = []

        def geturl(self) -> str:
            return self.url

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return self.body[:size]

    url = "http://127.0.0.1:12345"
    response = Response(b"repotrial-wsl2-sbx-ok\n", url)
    assert (
        calibration_module._read_exact_loopback_response(response, url) == response.body
    )
    assert response.read_sizes == [len(response.body) + 1]
    with pytest.raises(AssertionError):
        calibration_module._read_exact_loopback_response(
            Response(response.body + b"x", url), url
        )
