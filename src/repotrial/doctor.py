"""Read-only runtime checks used by ``repotrial doctor``.

The doctor deliberately keeps command output private.  Probes consume only
the bounded, validated facts they need and expose stable check details to the
CLI; captured stdout/stderr is never copied into a report.
"""

from __future__ import annotations

import json
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

type CheckStatus = Literal["PASS", "FAIL", "UNSUPPORTED"]

SUPPORTED_SBX_VERSION = "v0.39.0"
PID_HARD_BOUND_LIMITATION = "pid_hard_bound_unsupported"
DEFAULT_COMMAND_TIMEOUT_S = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class CommandResult:
    """A bounded command result with no unbounded or undecoded output."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    decode_error: bool = False
    launch_error: bool = False


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        """Run an argv command with a bounded timeout and capture."""


class _BoundedBytes:
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._value = bytearray()
        self.limited = False

    def append(self, chunk: bytes) -> None:
        remaining = self._max_bytes - len(self._value)
        if remaining > 0:
            self._value.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.limited = True

    @property
    def value(self) -> bytes:
        return bytes(self._value)


def _decode_bounded(value: bytes) -> tuple[str, bool]:
    try:
        return value.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


class BoundedCommandRunner:
    """Run commands without a shell and retain only bounded UTF-8 output."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float | None = None,
    ) -> CommandResult:
        timeout = self._timeout_s if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, TypeError, ValueError):
            return CommandResult(
                returncode=None,
                stdout="",
                stderr="",
                launch_error=True,
            )

        stdout = _BoundedBytes(self._max_output_bytes)
        stderr = _BoundedBytes(self._max_output_bytes)
        output_stream = process.stdout
        error_stream = process.stderr
        streams = (
            (output_stream, stdout),
            (error_stream, stderr),
        )
        selector = selectors.DefaultSelector()
        for stream, collector in streams:
            if stream is not None:
                try:
                    selector.register(stream, selectors.EVENT_READ, collector)
                except (OSError, ValueError):
                    stream.close()

        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while selector.get_map():
                process_done = process.poll() is not None
                if not process_done:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        self._kill_process_group(process)
                        break
                    wait_s = min(remaining, 0.05)
                else:
                    wait_s = 0

                try:
                    events = selector.select(wait_s)
                except OSError:
                    events = []
                if not events and process_done:
                    break
                for key, _ in events:
                    collector = key.data
                    try:
                        chunk = os.read(key.fd, 4096)
                    except (OSError, ValueError):
                        chunk = b""
                    if chunk:
                        collector.append(chunk)
                    else:
                        try:
                            selector.unregister(key.fileobj)
                        except (KeyError, OSError, ValueError):
                            pass
        finally:
            selector.close()
            for stream, _collector in streams:
                if stream is not None:
                    stream.close()

        try:
            returncode = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            returncode = None
        stdout_text, stdout_decode_error = _decode_bounded(stdout.value)
        stderr_text, stderr_decode_error = _decode_bounded(stderr.value)
        return CommandResult(
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            timed_out=timed_out,
            output_limited=stdout.limited or stderr.limited,
            decode_error=stdout_decode_error or stderr_decode_error,
        )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    blocking: bool
    detail: str
    remediation: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "blocking": self.blocking,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ready(self) -> bool:
        return all(
            check.status == "PASS" or not check.blocking for check in self.checks
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
        }


def render_json(report: DoctorReport) -> str:
    """Render a stable JSON object with no command output or terminal escapes."""

    return json.dumps(report.as_dict(), ensure_ascii=False, separators=(",", ":"))


def render_human(report: DoctorReport) -> str:
    """Render one deterministic status line per check and a final verdict."""

    lines: list[str] = []
    for check in report.checks:
        line = f"{check.status} {check.name}: {check.detail}"
        if check.remediation is not None:
            line += f"; remediation: {check.remediation}"
        lines.append(line)
    lines.append("READY" if report.ready else "NOT_READY")
    return "\n".join(lines) + "\n"


def parse_inventory_output(output: str) -> bool:
    """Accept only the reviewed official empty ``sbx list`` forms."""

    lines = output.splitlines()
    if not lines or lines[0] != "No sandboxes found.":
        return False
    if len(lines) == 1:
        return True
    return len(lines) == 2 and lines[1].startswith("Launch one:")


def _read_pid1() -> str:
    return Path("/proc/1/comm").read_text(encoding="utf-8")


def _kvm_is_accessible() -> bool:
    path = Path("/dev/kvm")
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def _path_is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _is_successful_command(result: CommandResult) -> bool:
    return (
        result.returncode == 0
        and not result.timed_out
        and not result.output_limited
        and not result.decode_error
        and not result.launch_error
    )


_VERSION_RE = re.compile(r"sbx version: (?P<version>v[^ ]+) (?P<commit>[0-9a-f]{40})")
_PLAYWRIGHT_VERSION_RE = re.compile(r"Playwright version: \S+")
_CHROMIUM_RE = re.compile(r"^/[\S]+/chromium-[0-9]+$")
_HEADLESS_RE = re.compile(r"^/[\S]+/chromium_headless_shell-[0-9]+$")
_FFMPEG_RE = re.compile(r"^/[\S]+/ffmpeg-[0-9]+$")


def _playwright_chromium_root(output: str) -> Path | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or not _PLAYWRIGHT_VERSION_RE.fullmatch(lines[0]):
        return None
    try:
        browsers_index = lines.index("Browsers:")
    except ValueError:
        return None
    entries: list[str] = []
    for line in lines[browsers_index + 1 :]:
        if line == "References:":
            break
        entries.append(line)
    if not entries:
        return None
    chromium_roots: list[Path] = []
    for entry in entries:
        if _CHROMIUM_RE.fullmatch(entry):
            root = Path(entry)
            if ".." not in root.parts:
                chromium_roots.append(root)
            continue
        if _HEADLESS_RE.fullmatch(entry) or _FFMPEG_RE.fullmatch(entry):
            continue
        return None
    if len(chromium_roots) != 1:
        return None
    return chromium_roots[0]


def _diagnose_is_healthy(output: str) -> int | None:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != "1.0":
        return None
    checks = payload.get("checks")
    summary = payload.get("summary")
    if not isinstance(checks, list) or not checks or not isinstance(summary, dict):
        return None
    required_check_keys = {"name", "status", "message", "detail", "hint"}
    for check in checks:
        if not isinstance(check, dict) or not required_check_keys.issubset(check):
            return None
        if any(not isinstance(check[key], str) for key in required_check_keys):
            return None
        if check["status"] != "pass":
            return None
    required_summary_keys = {"pass", "warn", "fail", "skip"}
    if not required_summary_keys.issubset(summary):
        return None
    summary_values: dict[str, int] = {}
    for key in required_summary_keys:
        value = summary[key]
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        summary_values[key] = value
    if (
        summary_values["pass"] != len(checks)
        or summary_values["warn"] != 0
        or summary_values["fail"] != 0
        or summary_values["skip"] != 0
    ):
        return None
    return len(checks)


class Doctor:
    """Build a deterministic, read-only environment report."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        system_name: Callable[[], str] = platform.system,
        pid1_reader: Callable[[], str] = _read_pid1,
        kvm_checker: Callable[[], bool] = _kvm_is_accessible,
        executable_lookup: Callable[[str], str | None] = shutil.which,
        path_is_executable: Callable[[Path], bool] = _path_is_executable,
        playwright_python: str | None = None,
    ) -> None:
        self._command_runner = command_runner or BoundedCommandRunner(
            timeout_s=command_timeout_s
        )
        self._command_timeout_s = command_timeout_s
        self._system_name = system_name
        self._pid1_reader = pid1_reader
        self._kvm_checker = kvm_checker
        self._executable_lookup = executable_lookup
        self._path_is_executable = path_is_executable
        self._playwright_python = playwright_python or sys.executable

    def _run_command(self, argv: Sequence[str]) -> CommandResult:
        try:
            return self._command_runner.run(argv, timeout_s=self._command_timeout_s)
        except (OSError, TimeoutError, TypeError, ValueError):
            return CommandResult(None, "", "", launch_error=True)

    def _check_linux(self) -> DoctorCheck:
        try:
            is_linux = self._system_name() == "Linux"
        except (OSError, RuntimeError):
            is_linux = False
        if is_linux:
            return DoctorCheck("linux", "PASS", True, "Linux runtime detected", None)
        return DoctorCheck(
            "linux",
            "FAIL",
            True,
            "RepoTrial requires a Linux runtime",
            "Run RepoTrial inside the dedicated Linux WSL2 runtime.",
        )

    def _check_systemd(self) -> DoctorCheck:
        try:
            is_systemd = self._pid1_reader().strip() == "systemd"
        except (OSError, UnicodeError, RuntimeError):
            is_systemd = False
        if is_systemd:
            return DoctorCheck("systemd", "PASS", True, "PID 1 is systemd", None)
        return DoctorCheck(
            "systemd",
            "FAIL",
            True,
            "PID 1 is not systemd",
            "Enable systemd in WSL2 and retry the preflight.",
        )

    def _check_kvm(self) -> DoctorCheck:
        try:
            accessible = self._kvm_checker()
        except (OSError, RuntimeError):
            accessible = False
        if accessible:
            return DoctorCheck(
                "kvm", "PASS", True, "/dev/kvm is readable and writable", None
            )
        return DoctorCheck(
            "kvm",
            "FAIL",
            True,
            "/dev/kvm is missing or inaccessible",
            "Enable nested virtualization and expose readable/writable /dev/kvm.",
        )

    def _check_executable(self, name: str, remediation: str) -> DoctorCheck:
        try:
            found = self._executable_lookup(name) is not None
        except (OSError, RuntimeError):
            found = False
        if found:
            return DoctorCheck(name, "PASS", True, f"{name} is available", None)
        return DoctorCheck(
            name,
            "FAIL",
            True,
            f"{name} is not available",
            remediation,
        )

    def _check_sbx_version(self) -> DoctorCheck:
        result = self._run_command(("sbx", "version"))
        if not _is_successful_command(result):
            return DoctorCheck(
                "sbx_version",
                "FAIL",
                True,
                "sbx version could not be verified",
                "Install Docker Sandboxes v0.39.0 and retry the preflight.",
            )
        match = _VERSION_RE.fullmatch(result.stdout.strip())
        if match is not None and match.group("version") == SUPPORTED_SBX_VERSION:
            return DoctorCheck(
                "sbx_version",
                "PASS",
                True,
                "Docker Sandboxes v0.39.0 is supported",
                None,
            )
        return DoctorCheck(
            "sbx_version",
            "UNSUPPORTED",
            True,
            "Docker Sandboxes is not the reviewed v0.39.0 runtime",
            "Install and select Docker Sandboxes v0.39.0.",
        )

    def _check_diagnose(self) -> DoctorCheck:
        result = self._run_command(("sbx", "diagnose", "--output", "json"))
        if not _is_successful_command(result):
            return DoctorCheck(
                "sbx_diagnose",
                "FAIL",
                True,
                "sbx diagnostics did not complete successfully",
                "Run sbx diagnose --output json and resolve the blocking runtime checks.",
            )
        count = _diagnose_is_healthy(result.stdout)
        if count is not None:
            return DoctorCheck(
                "sbx_diagnose",
                "PASS",
                True,
                f"{count} sbx diagnostic checks passed",
                None,
            )
        return DoctorCheck(
            "sbx_diagnose",
            "FAIL",
            True,
            "sbx diagnostics are invalid or contain a non-passing check",
            "Run sbx diagnose --output json and resolve the blocking runtime checks.",
        )

    def _check_inventory(self) -> DoctorCheck:
        result = self._run_command(("sbx", "list"))
        if (
            _is_successful_command(result)
            and not result.stderr
            and parse_inventory_output(result.stdout)
        ):
            return DoctorCheck(
                "sbx_inventory",
                "PASS",
                True,
                "sbx inventory is empty",
                None,
            )
        return DoctorCheck(
            "sbx_inventory",
            "FAIL",
            True,
            "sbx inventory is not the reviewed empty form",
            "Inspect sbx list and resolve any owned sandbox before retrying.",
        )

    def _check_playwright(self) -> DoctorCheck:
        result = self._run_command(
            (
                self._playwright_python,
                "-m",
                "playwright",
                "install",
                "--list",
            )
        )
        root = (
            _playwright_chromium_root(result.stdout)
            if _is_successful_command(result)
            else None
        )
        executable = False
        if root is not None:
            executable = any(
                self._path_is_executable(root / relative)
                for relative in ("chrome-linux64/chrome", "chrome-linux/chrome")
            )
        if executable:
            return DoctorCheck(
                "playwright_chromium",
                "PASS",
                True,
                "Playwright Chromium is installed and executable",
                None,
            )
        return DoctorCheck(
            "playwright_chromium",
            "FAIL",
            True,
            "Playwright Chromium is missing or not executable",
            "Install Chromium and its OS dependencies with "
            "uv run playwright install --with-deps chromium.",
        )

    @staticmethod
    def _check_pid_bound() -> DoctorCheck:
        return DoctorCheck(
            "pid_hard_bound",
            "UNSUPPORTED",
            False,
            "Docker Sandboxes v0.39.0 does not expose a PID hard bound",
            None,
        )

    def run(self) -> DoctorReport:
        checks = (
            self._check_linux(),
            self._check_systemd(),
            self._check_kvm(),
            self._check_executable(
                "git", "Install Git and ensure it is available on PATH."
            ),
            self._check_executable(
                "sbx",
                "Install Docker Sandboxes v0.39.0 and ensure sbx is available on PATH.",
            ),
            self._check_sbx_version(),
            self._check_diagnose(),
            self._check_inventory(),
            self._check_playwright(),
            self._check_pid_bound(),
        )
        return DoctorReport(checks=checks)


def build_doctor_report(
    *,
    command_runner: CommandRunner | None = None,
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    system_name: Callable[[], str] = platform.system,
    pid1_reader: Callable[[], str] = _read_pid1,
    kvm_checker: Callable[[], bool] = _kvm_is_accessible,
    executable_lookup: Callable[[str], str | None] = shutil.which,
    path_is_executable: Callable[[Path], bool] = _path_is_executable,
    playwright_python: str | None = None,
) -> DoctorReport:
    """Construct a report while keeping probe dependencies injectable."""

    return Doctor(
        command_runner=command_runner,
        command_timeout_s=command_timeout_s,
        system_name=system_name,
        pid1_reader=pid1_reader,
        kvm_checker=kvm_checker,
        executable_lookup=executable_lookup,
        path_is_executable=path_is_executable,
        playwright_python=playwright_python,
    ).run()


def run_doctor() -> DoctorReport:
    """Run the default read-only doctor."""

    return Doctor().run()
