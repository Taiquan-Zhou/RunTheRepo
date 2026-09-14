"""Bounded startup checks for services required by the local console."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from repotrial.doctor import (
    BoundedCommandRunner,
    CommandResult,
    CommandRunner,
    DoctorCheck,
    DoctorReport,
)

_DAEMON_TIMEOUT_S = 10.0
_STATUS_ARGV = ("sbx", "daemon", "status", "--json")
_START_ARGV = ("sbx", "daemon", "start", "--detach")
REQUIRED_SERVICE_CHECKS = frozenset({"sbx_daemon", "sbx_diagnose", "sbx_inventory"})
_REQUIRED_DOCTOR_CHECKS = REQUIRED_SERVICE_CHECKS - {"sbx_daemon"}


def verified_ready(report: DoctorReport) -> bool:
    """Return readiness only when every required service check is present and passes."""

    return (
        REQUIRED_SERVICE_CHECKS.issubset({check.name for check in report.checks})
        and report.ready
    )


def verified_report_dict(report: DoctorReport) -> dict[str, object]:
    """Project a report with the authoritative readiness verdict for the console."""

    result = report.as_dict()
    result["ready"] = verified_ready(report)
    return result


def render_verified_json(report: DoctorReport) -> str:
    """Render the authoritative console report without exposing command output."""

    return json.dumps(
        verified_report_dict(report), ensure_ascii=False, separators=(",", ":")
    )


class RequiredServices:
    """Ensure the local SBX daemon is running before accepting jobs."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        doctor: Callable[[], DoctorReport] | None = None,
        command_timeout_s: float = _DAEMON_TIMEOUT_S,
    ) -> None:
        if command_timeout_s <= 0:
            raise ValueError("command_timeout_s must be positive")
        self._command_runner = command_runner or BoundedCommandRunner(
            timeout_s=command_timeout_s
        )
        self._doctor = doctor
        self._command_timeout_s = command_timeout_s

    def ensure_ready(self) -> DoctorReport:
        first = self._run(_STATUS_ARGV)
        status = _daemon_status(first)
        if status == "running":
            return self._with_doctor(_daemon_pass("SBX daemon is running"))
        if status != "stopped":
            return _daemon_failure(
                "SBX daemon status could not be verified",
                "Run sbx daemon status --json and resolve the local SBX service before retrying.",
            )

        started = self._run(_START_ARGV)
        if not _successful(started):
            return _daemon_failure(
                "SBX daemon could not be started",
                "Run sbx daemon start --detach and resolve the local SBX service before retrying.",
            )
        verified = self._run(_STATUS_ARGV)
        if _daemon_status(verified) != "running":
            return _daemon_failure(
                "SBX daemon did not become ready after startup",
                "Run sbx daemon status --json and resolve the local SBX service before retrying.",
            )
        return self._with_doctor(_daemon_pass("SBX daemon was started and is running"))

    def _run(self, argv: Sequence[str]) -> CommandResult:
        try:
            return self._command_runner.run(
                argv,
                timeout_s=self._command_timeout_s,
            )
        except (OSError, TimeoutError, TypeError, ValueError):
            return CommandResult(None, "", "", launch_error=True)

    def _with_doctor(self, daemon_check: DoctorCheck) -> DoctorReport:
        if self._doctor is None:
            return DoctorReport(checks=(daemon_check,))
        try:
            report = self._doctor()
            checks = tuple(
                check for check in report.checks if check.name != "sbx_daemon"
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return _doctor_failure(daemon_check, "Required environment checks failed")
        names = {check.name for check in checks}
        if not _REQUIRED_DOCTOR_CHECKS.issubset(names):
            return _doctor_failure(
                daemon_check, "Required environment checks were incomplete"
            )
        return DoctorReport(checks=(daemon_check, *checks))


def _doctor_failure(daemon_check: DoctorCheck, detail: str) -> DoctorReport:
    return DoctorReport(
        checks=(
            daemon_check,
            DoctorCheck(
                "sbx_doctor",
                "FAIL",
                True,
                detail,
                "Run the environment check again and resolve the missing required checks.",
            ),
        )
    )


def _successful(result: CommandResult) -> bool:
    return (
        result.returncode == 0
        and not result.timed_out
        and not result.output_limited
        and not result.decode_error
        and not result.launch_error
    )


def _daemon_status(result: CommandResult) -> str | None:
    if not _successful(result):
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or type(payload.get("status")) is not str:
        return None
    status = payload["status"]
    return status if status in {"running", "stopped"} else None


def _daemon_pass(detail: str) -> DoctorCheck:
    return DoctorCheck("sbx_daemon", "PASS", True, detail, None)


def _daemon_failure(detail: str, remediation: str) -> DoctorReport:
    return DoctorReport(
        checks=(DoctorCheck("sbx_daemon", "FAIL", True, detail, remediation),)
    )
