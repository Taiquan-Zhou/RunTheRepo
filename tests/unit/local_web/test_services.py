from __future__ import annotations

from collections.abc import Sequence

import pytest

from repotrial.doctor import CommandResult, DoctorCheck, DoctorReport
from repotrial.local_web.services import RequiredServices


class StubRunner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        self.calls.append((tuple(argv), timeout_s))
        return self.responses.pop(0)


def _doctor() -> DoctorReport:
    return DoctorReport(
        checks=(
            DoctorCheck("sbx_diagnose", "PASS", True, "ok", None),
            DoctorCheck("sbx_inventory", "PASS", True, "ok", None),
        )
    )


def test_already_running_does_not_restart_daemon() -> None:
    runner = StubRunner([CommandResult(0, '{"status":"running"}', "")])

    report = RequiredServices(command_runner=runner, doctor=_doctor).ensure_ready()

    assert report.ready is True
    assert [check.name for check in report.checks] == [
        "sbx_daemon",
        "sbx_diagnose",
        "sbx_inventory",
    ]
    assert runner.calls == [
        (("sbx", "daemon", "status", "--json"), 10.0),
    ]


def test_stopped_daemon_is_started_and_rechecked() -> None:
    runner = StubRunner(
        [
            CommandResult(0, '{"status":"stopped"}', ""),
            CommandResult(0, "", ""),
            CommandResult(0, '{"status":"running","socket":"private"}', ""),
        ]
    )

    report = RequiredServices(command_runner=runner, doctor=_doctor).ensure_ready()

    assert report.ready is True
    assert runner.calls == [
        (("sbx", "daemon", "status", "--json"), 10.0),
        (("sbx", "daemon", "start", "--detach"), 10.0),
        (("sbx", "daemon", "status", "--json"), 10.0),
    ]
    assert all(
        secret not in report.as_dict().__repr__() for secret in ("private", "socket")
    )


def test_start_failure_is_blocking_and_does_not_restart() -> None:
    runner = StubRunner(
        [
            CommandResult(0, '{"status":"stopped"}', ""),
            CommandResult(7, "", "daemon failed"),
        ]
    )

    report = RequiredServices(command_runner=runner, doctor=_doctor).ensure_ready()

    assert report.ready is False
    assert report.checks[-1].status == "FAIL"
    assert "daemon failed" not in report.checks[-1].detail
    assert runner.calls[-1][0] == ("sbx", "daemon", "start", "--detach")


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(0, "[]", ""),
        CommandResult(0, "not-json", ""),
        CommandResult(0, '{"status":"starting"}', ""),
        CommandResult(None, "", "", timed_out=True),
    ],
)
def test_invalid_status_is_blocking(result: CommandResult) -> None:
    runner = StubRunner([result])

    report = RequiredServices(command_runner=runner, doctor=_doctor).ensure_ready()

    assert report.ready is False
    assert report.checks[0].name == "sbx_daemon"
    assert report.checks[0].status == "FAIL"


def test_no_shell_and_timeout_is_bounded() -> None:
    runner = StubRunner([CommandResult(0, '{"status":"running"}', "")])

    RequiredServices(command_runner=runner, doctor=_doctor).ensure_ready()

    assert all(timeout == 10.0 for _argv, timeout in runner.calls)
    assert all(
        "|" not in arg and "&&" not in arg for argv, _ in runner.calls for arg in argv
    )


def test_doctor_exception_is_blocking() -> None:
    runner = StubRunner([CommandResult(0, '{"status":"running"}', "")])

    def raising_doctor() -> DoctorReport:
        raise OSError("private failure")

    report = RequiredServices(
        command_runner=runner, doctor=raising_doctor
    ).ensure_ready()

    assert report.ready is False
    assert any(check.status == "FAIL" and check.blocking for check in report.checks)
    assert "private failure" not in report.as_dict().__repr__()


@pytest.mark.parametrize(
    "doctor_report",
    [
        DoctorReport(checks=()),
        DoctorReport(checks=(DoctorCheck("sbx_inventory", "PASS", True, "ok", None),)),
    ],
)
def test_empty_or_incomplete_doctor_is_blocking(
    doctor_report: DoctorReport,
) -> None:
    runner = StubRunner([CommandResult(0, '{"status":"running"}', "")])

    report = RequiredServices(
        command_runner=runner, doctor=lambda: doctor_report
    ).ensure_ready()

    assert report.ready is False
    assert any(
        check.name == "sbx_doctor" and check.status == "FAIL" for check in report.checks
    )
