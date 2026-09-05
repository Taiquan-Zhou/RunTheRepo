from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from repotrial.doctor import (
    BoundedCommandRunner,
    CommandResult,
    Doctor,
    DoctorCheck,
    DoctorReport,
    parse_inventory_output,
    render_human,
    render_json,
)

_SBX_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _diagnose_json(
    *,
    statuses: Sequence[str] = ("pass",) * 12,
    summary: dict[str, int] | None = None,
) -> str:
    checks = [
        {
            "name": f"check-{index}",
            "status": status,
            "message": "ok",
            "detail": "",
            "hint": "",
        }
        for index, status in enumerate(statuses)
    ]
    payload = {
        "version": "1.0",
        "checks": checks,
        "summary": summary or {"pass": len(checks), "warn": 0, "fail": 0, "skip": 0},
    }
    return json.dumps(payload)


class _StubRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        key = tuple(argv)
        self.calls.append((key, timeout_s))
        return self.responses[key]


def _healthy_doctor(
    runner: _StubRunner | None = None,
    *,
    executable_lookup: Callable[[str], str | None] | None = None,
    path_is_executable: Callable[[Path], bool] | None = None,
) -> Doctor:
    command_runner = runner or _healthy_runner()
    return Doctor(
        command_runner=command_runner,
        system_name=lambda: "Linux",
        pid1_reader=lambda: "systemd\n",
        kvm_checker=lambda: True,
        executable_lookup=executable_lookup or (lambda name: f"/usr/bin/{name}"),
        path_is_executable=path_is_executable
        or (
            lambda path: (
                path.as_posix().endswith("/chrome-linux64/chrome")
                or path.as_posix().endswith(
                    "/chrome-headless-shell-linux64/chrome-headless-shell"
                )
            )
        ),
        playwright_python="python",
    )


def _healthy_runner() -> _StubRunner:
    return _StubRunner(
        {
            ("sbx", "version"): CommandResult(
                returncode=0,
                stdout=f"sbx version: v0.39.0 {_SBX_COMMIT}\n",
                stderr="",
            ),
            ("sbx", "diagnose", "--output", "json"): CommandResult(
                returncode=0,
                stdout=_diagnose_json(),
                stderr="",
            ),
            ("sbx", "list"): CommandResult(
                returncode=0,
                stdout="No sandboxes found.\nLaunch one: sbx run claude\n",
                stderr="",
            ),
            ("python", "-m", "playwright", "install", "--list"): CommandResult(
                returncode=0,
                stdout=(
                    "Playwright version: 1.62.0\n"
                    "  Browsers:\n"
                    "    /tmp/ms-playwright/chromium-1234\n"
                    "    /tmp/ms-playwright/chromium_headless_shell-1234\n"
                    "    /tmp/ms-playwright/ffmpeg-1011\n"
                ),
                stderr="",
            ),
        }
    )


def test_healthy_report_is_ordered_and_pid_limit_is_nonblocking() -> None:
    command_runner = _healthy_runner()

    report = _healthy_doctor(command_runner).run()

    assert [check.name for check in report.checks] == [
        "linux",
        "systemd",
        "kvm",
        "git",
        "sbx",
        "sbx_version",
        "sbx_diagnose",
        "sbx_inventory",
        "playwright_chromium",
        "pid_hard_bound",
    ]
    assert report.ready is True
    assert all(check.status == "PASS" for check in report.checks[:-1])
    assert report.checks[-1] == DoctorCheck(
        name="pid_hard_bound",
        status="UNSUPPORTED",
        blocking=False,
        detail="Docker Sandboxes v0.39.0 does not expose a PID hard bound",
        remediation=None,
    )
    assert {timeout for _argv, timeout in command_runner.calls} == {30.0}


@pytest.mark.parametrize("failure", ["linux", "systemd", "kvm", "git", "sbx"])
def test_local_prerequisite_failures_are_blocking(failure: str) -> None:
    system_name = lambda: "Linux"
    pid1_reader = lambda: "systemd\n"
    kvm_checker = lambda: True
    executable_lookup = lambda name: f"/usr/bin/{name}"
    if failure == "linux":
        system_name = lambda: "Windows"
    elif failure == "systemd":
        pid1_reader = lambda: "init\n"
    elif failure == "kvm":
        kvm_checker = lambda: False
    elif failure == "git":
        executable_lookup = lambda name: None if name == "git" else f"/usr/bin/{name}"
    else:
        executable_lookup = lambda name: None if name == "sbx" else f"/usr/bin/{name}"

    check = next(
        check
        for check in Doctor(
            command_runner=_healthy_runner(),
            system_name=system_name,
            pid1_reader=pid1_reader,
            kvm_checker=kvm_checker,
            executable_lookup=executable_lookup,
            path_is_executable=lambda path: path.as_posix().endswith(
                "/chrome-linux64/chrome"
            ),
            playwright_python="python",
        )
        .run()
        .checks
        if check.name == failure
    )

    assert check.status == "FAIL"
    assert check.blocking is True
    assert check.remediation


def test_version_mismatch_is_blocking_unsupported() -> None:
    runner = _StubRunner(
        {
            ("sbx", "version"): CommandResult(0, "sbx version: v0.38.0 deadbeef\n", ""),
            ("sbx", "diagnose", "--output", "json"): CommandResult(1, "", ""),
            ("sbx", "list"): CommandResult(1, "", ""),
            ("python", "-m", "playwright", "install", "--list"): CommandResult(
                1, "", ""
            ),
        }
    )
    check = next(
        check
        for check in _healthy_doctor(runner).run().checks
        if check.name == "sbx_version"
    )

    assert check.status == "UNSUPPORTED"
    assert check.blocking is True


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(1, "", ""),
        CommandResult(None, "", "", timed_out=True),
        CommandResult(0, "not json", ""),
        CommandResult(0, _diagnose_json(statuses=("warn",) + ("pass",) * 11), ""),
    ],
)
def test_diagnose_failures_are_fail_closed(result: CommandResult) -> None:
    runner = _healthy_runner()
    runner.responses[("sbx", "diagnose", "--output", "json")] = result

    check = next(
        check
        for check in _healthy_doctor(runner).run().checks
        if check.name == "sbx_diagnose"
    )

    assert check.status == "FAIL"
    assert check.blocking is True
    assert check.remediation


def test_diagnose_allows_only_reviewed_binary_version_warning() -> None:
    payload = json.loads(
        _diagnose_json(
            statuses=("warn",) + ("pass",) * 11,
            summary={"pass": 11, "warn": 1, "fail": 0, "skip": 0},
        )
    )
    payload["checks"][0]["name"] = "Binary version"
    runner = _healthy_runner()
    runner.responses[("sbx", "diagnose", "--output", "json")] = CommandResult(
        0, json.dumps(payload), ""
    )

    check = next(
        check
        for check in _healthy_doctor(runner).run().checks
        if check.name == "sbx_diagnose"
    )

    assert check.status == "PASS"
    assert check.blocking is True
    assert "remote version check warning" in check.detail


@pytest.mark.parametrize(
    "output,expected",
    [
        ("No sandboxes found.\n", True),
        ("No sandboxes found.\nLaunch one: sbx run claude\n", True),
        ("sandbox-1\n", False),
        ("No sandboxes found.\nunexpected\n", False),
        ("No sandboxes found.", False),
        ("No sandboxes found.\nLaunch one:\n", False),
        ("No sandboxes found.\nLaunch one: arbitrary\n", False),
        ("No sandboxes found.\r\n", False),
    ],
)
def test_inventory_parser_accepts_only_reviewed_empty_forms(
    output: str, expected: bool
) -> None:
    assert parse_inventory_output(output) is expected


def test_inventory_warning_on_stderr_is_not_an_official_empty_form() -> None:
    runner = _healthy_runner()
    runner.responses[("sbx", "list")] = CommandResult(
        returncode=0,
        stdout="No sandboxes found.\n",
        stderr="warning\n",
    )

    check = next(
        check
        for check in _healthy_doctor(runner).run().checks
        if check.name == "sbx_inventory"
    )

    assert check.status == "FAIL"
    assert check.blocking is True


def test_missing_playwright_executable_fails_without_browser_launch() -> None:
    command_runner = _healthy_runner()
    doctor = _healthy_doctor(command_runner, path_is_executable=lambda _path: False)

    check = next(
        check for check in doctor.run().checks if check.name == "playwright_chromium"
    )

    assert check.status == "FAIL"
    assert check.blocking is True
    assert check.remediation == (
        "Install Chromium and its OS dependencies with "
        "uv run playwright install --with-deps chromium."
    )
    assert not any("sync_playwright" in argv for argv, _timeout in command_runner.calls)


def test_missing_headless_shell_executable_is_blocking() -> None:
    command_runner = _healthy_runner()
    doctor = _healthy_doctor(
        command_runner,
        path_is_executable=lambda path: path.as_posix().endswith(
            "/chrome-linux64/chrome"
        ),
    )

    check = next(
        check for check in doctor.run().checks if check.name == "playwright_chromium"
    )

    assert check.status == "FAIL"
    assert check.blocking is True


def test_command_adapter_uses_argv_timeout_and_bounded_utf8_output() -> None:
    adapter = BoundedCommandRunner(timeout_s=0.2, max_output_bytes=16)

    result = adapter.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('secret-' * 1000)"],
    )

    assert result.returncode == 0
    assert result.output_limited is True
    assert len(result.stdout.encode("utf-8")) <= 16
    assert result.stderr == ""


def test_report_rendering_never_includes_captured_output() -> None:
    secret = "captured-secret-value"
    command_runner = _healthy_runner()
    command_runner.responses[("sbx", "version")] = CommandResult(0, secret, secret)

    report = _healthy_doctor(command_runner).run()

    assert secret not in render_human(report)
    assert secret not in render_json(report)


def test_renderers_have_stable_shapes() -> None:
    report = DoctorReport(checks=(DoctorCheck("example", "PASS", False, "ok", None),))

    assert json.loads(render_json(report)) == {
        "ready": True,
        "checks": [
            {
                "name": "example",
                "status": "PASS",
                "blocking": False,
                "detail": "ok",
                "remediation": None,
            }
        ],
    }
    assert render_human(report).splitlines()[-1] == "READY"


def test_command_adapter_does_not_wait_for_descendant_holding_pipe() -> None:
    """Timeout remains bounded when a child inherits the output pipe."""

    adapter = BoundedCommandRunner(timeout_s=0.05, max_output_bytes=1024)
    started = time.monotonic()
    result = adapter.run(
        [
            sys.executable,
            "-c",
            ("import os, time; pid = os.fork(); time.sleep(2)"),
        ]
    )

    assert result.timed_out is True
    assert time.monotonic() - started < 1.0


def test_command_adapter_keeps_timeout_after_process_closes_pipes() -> None:
    adapter = BoundedCommandRunner(timeout_s=0.05, max_output_bytes=1024)
    started = time.monotonic()

    result = adapter.run(
        [
            sys.executable,
            "-c",
            "import os, time; os.close(1); os.close(2); time.sleep(2)",
        ]
    )

    assert result.timed_out is True
    assert result.returncode is not None
    assert time.monotonic() - started < 1.0
