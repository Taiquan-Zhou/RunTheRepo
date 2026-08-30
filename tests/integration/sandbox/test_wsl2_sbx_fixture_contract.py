import importlib.util
import io
import json
import subprocess
import sys
import threading
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


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "http://169.254.169.254/latest/meta-data/",
    ],
    ids=["public", "denied"],
)
def test_calibration_network_probe_argv_uses_sbx_shell_curl(
    calibration_module: ModuleType,
    url: str,
) -> None:
    argv = calibration_module._shell_curl_argv(url)

    assert argv == [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        url,
    ]
    assert argv.count(url) == 1
    assert not {"sh", "bash", "-c", "&&", ";", "|"}.intersection(argv)


def test_calibration_compose_ps_parser_normalizes_single_service_object(
    calibration_module: ModuleType,
) -> None:
    service = {"Service": "web", "State": "running"}

    assert calibration_module._parse_compose_ps(json.dumps(service)) == [service]


def test_calibration_compose_ps_parser_preserves_non_empty_service_array(
    calibration_module: ModuleType,
) -> None:
    services = [{"Service": "web", "State": "running"}]

    assert calibration_module._parse_compose_ps(json.dumps(services)) == services


@pytest.mark.parametrize(
    "payload",
    [[], {}, 0, "web", [{"Service": "web"}, "invalid"]],
    ids=["empty-list", "empty-object", "scalar-number", "scalar-string", "mixed-list"],
)
def test_calibration_compose_ps_parser_rejects_invalid_shapes(
    calibration_module: ModuleType, payload: object
) -> None:
    with pytest.raises(AssertionError):
        calibration_module._parse_compose_ps(json.dumps(payload))


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


def test_calibration_mount_parser_accepts_absent_uuid(
    calibration_module: ModuleType,
) -> None:
    mount = calibration_module._parse_mount(
        "overlay overlay 0:28 /",
        "/",
    )

    assert mount.source == "overlay"
    assert mount.filesystem == "overlay"
    assert mount.identity == ("0:28", "")
    assert mount.target == "/"


@pytest.mark.parametrize(
    ("output", "requested_path"),
    [
        ("", "/workspace"),
        ("/dev/vdb ext4 8:16", "/workspace"),
        ("/dev/vdb ext4 8:16 fixture-uuid /workspace extra", "/workspace extra"),
        ("/dev/vdb ext4 8:16x /workspace", "/workspace"),
        ("/dev/vdb ext4 8:16 /", "/workspace"),
        ("/dev/vdb ext4 8:16 /workspace\n/dev/vdc ext4 8:32 /data", "/workspace"),
        ("   ", "/workspace"),
    ],
    ids=[
        "empty",
        "too-few-fields",
        "too-many-fields",
        "invalid-major-minor",
        "mismatched-target",
        "multiple-lines",
        "whitespace-only",
    ],
)
def test_calibration_mount_parser_rejects_malformed_output(
    calibration_module: ModuleType,
    output: str,
    requested_path: str,
) -> None:
    with pytest.raises(AssertionError):
        calibration_module._parse_mount(output, requested_path)


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


def test_calibration_network_evidence_requires_monotonic_unpoisoned_progress(
    calibration_module: ModuleType,
) -> None:
    def event(
        count: object,
        last_seen: object,
        *,
        rule: str = "169.254.169.254/32",
    ) -> dict[str, object]:
        return {
            "sandbox": "owned",
            "decision": "blocked",
            "host": "169.254.169.254",
            "proxy": "network",
            "rule": rule,
            "reason": "deny",
            "last_seen": last_seen,
            "count": count,
        }

    expected_evidence = {"169.254.169.254", "169.254.169.254/32"}
    baseline = event(10, "2026-08-30T10:00:00Z")
    assert not calibration_module._has_fresh_blocked_evidence(
        [baseline], [event(11, "2026-08-30T09:00:00Z")], expected_evidence
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [baseline], [event(9, "2026-08-30T11:00:00Z")], expected_evidence
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [event(10, "2026-08-30T10:00:00Z")],
        [event(10, "2026-08-30T12:00:00+02:00")],
        expected_evidence,
    )
    assert calibration_module._has_fresh_blocked_evidence(
        [baseline], [event(11, "2026-08-30T11:00:00Z")], expected_evidence
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [baseline, event("invalid", "2026-08-30T12:00:00Z")],
        [event(11, "2026-08-30T11:00:00Z")],
        expected_evidence,
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [baseline, event(12, "invalid-timestamp")],
        [event(11, "2026-08-30T11:00:00Z")],
        expected_evidence,
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [baseline, {**event(12, "2026-08-30T12:00:00Z"), "proxy": 7}],
        [event(11, "2026-08-30T11:00:00Z")],
        expected_evidence,
    )
    assert not calibration_module._has_fresh_blocked_evidence(
        [baseline],
        [event(11, "2026-08-30T11:00:00Z"), event("invalid", "invalid")],
        expected_evidence,
    )
    assert calibration_module._has_fresh_blocked_evidence(
        [baseline],
        [event(1, "2026-08-30T01:00:00Z", rule="new-rule")],
        expected_evidence,
    )


def test_calibration_inventory_parser_only_accepts_canonical_empty_output(
    calibration_module: ModuleType,
) -> None:
    assert (
        calibration_module._parse_empty_sbx_inventory(
            b"No sandboxes found.\nLaunch one: sbx run claude\n", b""
        )
        is None
    )
    for stdout, stderr in (
        (b"No sandboxes found.\n", b""),
        (b"", b""),
        (b"No sandboxes found\n", b""),
        (b"sandbox-123 running\n", b""),
        (b"No sandboxes found.\nLaunch one: sbx run claude\nextra\n", b""),
        (b"No sandboxes found.\nLaunch one: sbx run codex\n", b""),
        (b"No sandboxes found.\nLaunch one: sbx run claude", b""),
        (
            b"No sandboxes found.\nLaunch one: sbx run claude\n",
            b"warning\n",
        ),
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
helpers = [thread for thread in threading.enumerate() if thread.name.startswith("repotrial-host-")]
assert helpers and all(thread.daemon for thread in helpers)
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


def test_calibration_host_timeout_bounds_blocking_kill() -> None:
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

class Pipe:
    def __init__(self):
        self.release = threading.Event()
        self.close_called = False

    def read(self, size):
        self.release.wait()
        return b""

    def close(self):
        self.close_called = True
        self.release.set()

class Process:
    def __init__(self):
        self.stdout = Pipe()
        self.stderr = Pipe()
        self.kill_entered = threading.Event()

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(["fake"], timeout)

    def kill(self):
        self.kill_entered.set()
        threading.Event().wait()

process = Process()
module.subprocess.Popen = lambda *args, **kwargs: process
try:
    module._run_host(["fake"], timeout_s=0.01)
except AssertionError as error:
    assert "cleanup unconfirmed" in str(error)
else:
    raise AssertionError("expected bounded blocking-kill failure")
assert process.kill_entered.is_set()
assert not process.stdout.close_called and not process.stderr.close_called
helpers = [thread for thread in threading.enumerate() if thread.name.startswith("repotrial-host-")]
assert helpers and all(thread.daemon for thread in helpers)
print("bounded-kill")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout.strip() == b"bounded-kill"


def test_calibration_host_timeout_bounds_blocking_reap() -> None:
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

class Pipe:
    def __init__(self):
        self.close_called = False

    def read(self, size):
        threading.Event().wait()
        return b""

    def close(self):
        self.close_called = True

class Process:
    def __init__(self):
        self.stdout = Pipe()
        self.stderr = Pipe()
        self.killed = False
        self.reap_entered = threading.Event()

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        self.reap_entered.set()
        threading.Event().wait()

    def kill(self):
        self.killed = True

process = Process()
module.subprocess.Popen = lambda *args, **kwargs: process
try:
    module._run_host(["fake"], timeout_s=0.01)
except AssertionError as error:
    assert "cleanup unconfirmed" in str(error)
else:
    raise AssertionError("expected bounded blocking-reap failure")
assert process.killed and process.reap_entered.is_set()
assert not process.stdout.close_called and not process.stderr.close_called
helpers = [thread for thread in threading.enumerate() if thread.name.startswith("repotrial-host-")]
assert helpers and all(thread.daemon for thread in helpers)
print("bounded-reap")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout.strip() == b"bounded-reap"


def test_calibration_host_reaper_baseexception_propagates_exact_object(
    calibration_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReaperFailure(BaseException):
        pass

    expected = ReaperFailure("reaper failed")

    class Pipe:
        def read(self, size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Process:
        def __init__(self) -> None:
            self.stdout = Pipe()
            self.stderr = Pipe()
            self.wait_calls = 0
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            raise expected

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr(
        calibration_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    try:
        calibration_module._run_host(["fake"], timeout_s=0.01)
    except ReaperFailure as error:
        assert error is expected
    else:
        raise AssertionError("_run_host returned success after reaper failure")

    assert process.killed
    assert process.wait_calls == 2


@pytest.mark.parametrize("failure_site", ["reader", "killer", "closer"])
def test_calibration_host_helper_baseexceptions_propagate(failure_site: str) -> None:
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

failure_site = {failure_site!r}

class HelperFailure(BaseException):
    pass

expected = HelperFailure(failure_site)

class EmptyPipe:
    def read(self, size):
        return b""

    def close(self):
        return None

class FailingReaderPipe(EmptyPipe):
    def read(self, size):
        raise expected

class BlockingPipe:
    def __init__(self, fail_close=False):
        self.release = threading.Event()
        self.fail_close = fail_close

    def read(self, size):
        self.release.wait()
        return b""

    def close(self):
        self.release.set()
        if self.fail_close:
            raise expected

class Process:
    def __init__(self):
        self.killed = False
        if failure_site == "reader":
            self.stdout = FailingReaderPipe()
            self.stderr = EmptyPipe()
        elif failure_site == "closer":
            self.stdout = BlockingPipe(fail_close=True)
            self.stderr = BlockingPipe()
        else:
            self.stdout = EmptyPipe()
            self.stderr = EmptyPipe()

    def wait(self, timeout=None):
        if failure_site == "reader":
            return 23
        if failure_site == "killer" or not self.killed:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        return 137

    def kill(self):
        if failure_site == "killer":
            raise expected
        self.killed = True

process = Process()
module.subprocess.Popen = lambda *args, **kwargs: process
try:
    module._run_host(["fake"], timeout_s=0.01)
except HelperFailure as error:
    assert error is expected
else:
    raise AssertionError(f"expected {{failure_site}} BaseException propagation")
assert not [
    thread
    for thread in threading.enumerate()
    if thread.name.startswith("repotrial-host-") and not thread.daemon
]
print(f"propagated-{{failure_site}}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout.strip() == f"propagated-{failure_site}".encode()


def test_calibration_host_non_timeout_wait_error_runs_bounded_cleanup() -> None:
    script = f"""
import importlib.util
import sys
import threading

specification = importlib.util.spec_from_file_location("calibration", {str(_CALIBRATION_TEST_PATH)!r})
assert specification is not None and specification.loader is not None
module = importlib.util.module_from_spec(specification)
sys.modules["calibration"] = module
specification.loader.exec_module(module)
module._HOST_CLEANUP_TIMEOUT_S = 0.05

class WaitFailure(BaseException):
    pass

expected = WaitFailure("wait failed")

class Pipe:
    def __init__(self):
        self.release = threading.Event()
        self.close_called = False

    def read(self, size):
        self.release.wait()
        return b""

    def close(self):
        self.close_called = True
        self.release.set()

class Process:
    def __init__(self):
        self.stdout = Pipe()
        self.stderr = Pipe()
        self.wait_calls = 0
        self.killed = False
        self.reaped = False

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise expected
        self.reaped = True
        return 137

    def kill(self):
        self.killed = True

process = Process()
module.subprocess.Popen = lambda *args, **kwargs: process
try:
    module._run_host(["fake"], timeout_s=0.01)
except WaitFailure as error:
    assert error is expected
else:
    raise AssertionError("expected original wait BaseException")
assert process.killed and process.reaped
assert process.stdout.close_called and process.stderr.close_called
assert not [
    thread
    for thread in threading.enumerate()
    if thread.name.startswith("repotrial-host-") and not thread.daemon
]
print("wait-error-cleaned")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout.strip() == b"wait-error-cleaned"


def test_calibration_host_timeout_bounds_blocking_pipe_close() -> None:
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

class Pipe:
    def read(self, size):
        threading.Event().wait()
        return b""

    def close(self):
        threading.Event().wait()

class Process:
    def __init__(self):
        self.stdout = Pipe()
        self.stderr = Pipe()
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
    raise AssertionError("expected bounded blocking-close failure")
assert process.killed and process.reaped
helpers = [thread for thread in threading.enumerate() if thread.name.startswith("repotrial-host-")]
assert helpers and all(thread.daemon for thread in helpers)
print("bounded-close")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout.strip() == b"bounded-close"


def test_calibration_host_normal_exit_preserves_complete_output(
    calibration_module: ModuleType,
) -> None:
    result = calibration_module._run_host(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(b'complete-stdout'); "
                "sys.stderr.buffer.write(b'complete-stderr'); "
                "sys.exit(7)"
            ),
        ],
        timeout_s=1,
    )
    assert result.returncode == 7
    assert result.stdout == b"complete-stdout"
    assert result.stderr == b"complete-stderr"
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("repotrial-host-") and not thread.daemon
    ]


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
