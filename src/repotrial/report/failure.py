"""Render sanitized reports for CLI execution failures."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal

from .render import TrialReportPaths

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_TEXT_PATTERN = re.compile(r"[^\x00-\x1f\x7f]{1,512}\Z")
_SAFE_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_KNOWN_EXCEPTION_TYPES = frozenset(
    {"DockerSbxError", "DockerSbxUnsupportedError", "RepoIntakeError"}
)

type CleanupStatus = Literal["not_verified", "failed"]


@dataclass(frozen=True, slots=True)
class FailureReportProjection:
    run_id: str
    repo_url: str
    expected_commit_sha: str | None
    actual_verified_commit_sha: str | None
    compose_path: str | None
    container_port: int | None
    exit_code: int
    stop_reason: str
    exception_type: str
    started_at_utc: str | None
    ended_at_utc: str | None
    monotonic_duration_s: float | None
    cleanup_status: CleanupStatus
    failure_returncode: int | None
    failure_operation: str | None
    failure_reason: str | None

    @classmethod
    def from_evidence(cls, evidence: Mapping[str, object]) -> FailureReportProjection:
        run_id = _safe_text(evidence.get("run_id"), fallback="unknown")
        repo_url = _safe_text(evidence.get("repository_url"), fallback="unknown")
        expected = _safe_sha(evidence.get("expected_sha"))
        actual = _safe_sha(evidence.get("actual_verified_sha"))
        compose_path = _safe_reference(evidence.get("compose_path"))
        container_port = evidence.get("container_port")
        if type(container_port) is not int or not 1 <= container_port <= 65_535:
            container_port = None
        exit_code = evidence.get("exit_code")
        if type(exit_code) is not int:
            exit_code = 4
        exception_type = evidence.get("exception_type")
        if exception_type not in _KNOWN_EXCEPTION_TYPES:
            exception_type = "Exception"
        stop_reason = _safe_stop_reason(evidence.get("stop_reason"))
        failure = evidence.get("failure_evidence")
        failure_returncode: int | None = None
        failure_operation: str | None = None
        failure_reason: str | None = None
        if isinstance(failure, Mapping):
            value = failure.get("returncode")
            if type(value) is int:
                failure_returncode = value
            failure_operation = _safe_token(failure.get("operation"))
            failure_reason = _safe_token(failure.get("reason"))
        cleanup_status: CleanupStatus = (
            "failed" if evidence.get("cleanup_status") == "failed" else "not_verified"
        )
        return cls(
            run_id=run_id,
            repo_url=repo_url,
            expected_commit_sha=expected,
            actual_verified_commit_sha=actual,
            compose_path=compose_path,
            container_port=container_port,
            exit_code=exit_code,
            stop_reason=stop_reason,
            exception_type=exception_type,
            started_at_utc=_optional_text(evidence.get("started_at_utc")),
            ended_at_utc=_optional_text(evidence.get("ended_at_utc")),
            monotonic_duration_s=_optional_float(evidence.get("monotonic_duration_s")),
            cleanup_status=cleanup_status,
            failure_returncode=failure_returncode,
            failure_operation=failure_operation,
            failure_reason=failure_reason,
        )


class FailureReportWriteError(RuntimeError):
    """The exclusive failure-report pair could not be created."""


def render_failure_report(
    projection: FailureReportProjection, output_dir: Path
) -> TrialReportPaths:
    """Create the fixed JSON/HTML pair without overwriting existing files."""
    report = _report(projection)
    json_path = output_dir / "trial-report.json"
    html_path = output_dir / "trial-report.html"
    created: list[Path] = []
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_exclusive(
            json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        created.append(json_path)
        _write_exclusive(html_path, _html(report))
        created.append(html_path)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise FailureReportWriteError("failure report pair unavailable") from error
    return TrialReportPaths(json_path=json_path, html_path=html_path)


def _report(projection: FailureReportProjection) -> dict[str, object]:
    identity: dict[str, object] = {
        "run_id": projection.run_id,
        "repo_url": projection.repo_url,
        "commit_sha": projection.actual_verified_commit_sha,
        "expected_commit_sha": projection.expected_commit_sha,
        "actual_verified_commit_sha": projection.actual_verified_commit_sha,
    }
    if projection.compose_path is not None:
        identity["compose_reference"] = projection.compose_path
    return {
        "report_kind": "execution_failure",
        "completed": False,
        "disclaimer": "Execution failed; this report is not an execution success.",
        "identity": identity,
        "timing": {
            "started_at_utc": projection.started_at_utc,
            "ended_at_utc": projection.ended_at_utc,
            "monotonic_duration_s": projection.monotonic_duration_s,
        },
        "failure": {
            "exception_type": projection.exception_type,
            "exit_code": projection.exit_code,
            "stop_reason": projection.stop_reason,
            "returncode": projection.failure_returncode,
            "operation": projection.failure_operation,
            "reason": projection.failure_reason,
        },
        "cleanup": {"status": projection.cleanup_status},
        "availability": {
            "observations": "unavailable",
            "journeys": "unavailable",
            "hardening": "unavailable",
        },
        "known_limitations": ["pid_hard_bound_unsupported"],
        "evidence": {"references": ["attempt-result.json"]},
    }


def _html(report: dict[str, object]) -> str:
    identity = report["identity"]
    failure = report["failure"]
    timing = report["timing"]
    cleanup = report["cleanup"]
    availability = report["availability"]
    assert isinstance(identity, dict)
    assert isinstance(failure, dict)
    assert isinstance(timing, dict)
    assert isinstance(cleanup, dict)
    assert isinstance(availability, dict)
    rows = "".join(
        f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>"
        for key, value in identity.items()
    )
    failure_rows = "".join(
        f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>"
        for key, value in failure.items()
    )
    timing_rows = "".join(
        f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>"
        for key, value in timing.items()
    )
    availability_rows = "".join(
        f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>"
        for key, value in availability.items()
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>RepoTrial execution failure</title></head><body>"
        "<h1>RepoTrial execution failure</h1>"
        f"<p>{escape(str(report['disclaimer']))}</p>"
        f"<h2>Identity</h2><dl>{rows}</dl>"
        f"<h2>Failure</h2><dl>{failure_rows}</dl>"
        f"<h2>Timing</h2><dl>{timing_rows}</dl>"
        f"<h2>Cleanup</h2><p>{escape(str(cleanup['status']))}</p>"
        f"<h2>Availability</h2><dl>{availability_rows}</dl>"
        "</body></html>\n"
    )


def _write_exclusive(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as output:
        output.write(text)
        output.flush()


def _safe_text(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_TEXT_PATTERN.fullmatch(value):
        return value
    return fallback


def _safe_stop_reason(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return "internal:unknown_failure"
    parts = value.split(":")
    if all(_safe_token(part) is not None for part in parts):
        return value
    return "internal:unknown_failure"


def _optional_text(value: object) -> str | None:
    return _safe_text(value, fallback="") or None


def _safe_sha(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA_PATTERN.fullmatch(value) else None


def _safe_reference(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and _SAFE_REFERENCE_PATTERN.fullmatch(value)
        else None
    )


def _safe_token(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
        for character in value
    ):
        return None
    return value


def _optional_float(value: object) -> float | None:
    if isinstance(value, float) and value >= 0:
        return value
    if type(value) is int and value >= 0:
        return float(value)
    return None
