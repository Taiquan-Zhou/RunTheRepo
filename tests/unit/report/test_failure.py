import json
from pathlib import Path

import pytest

from repotrial.report.failure import (
    FailureReportProjection,
    FailureReportWriteError,
    render_failure_report,
)


def _projection() -> FailureReportProjection:
    return FailureReportProjection.from_evidence(
        {
            "run_id": "run-fixed",
            "repository_url": "https://github.com/owner/repository",
            "expected_sha": "a" * 40,
            "actual_verified_sha": "a" * 40,
            "compose_path": "workspace/compose.yaml",
            "container_port": 8080,
            "exit_code": 4,
            "exception_type": "DockerSbxError",
            "stop_reason": "sandbox:exec:timeout",
            "started_at_utc": "2026-09-08T00:00:00+00:00",
            "ended_at_utc": "2026-09-08T00:00:01+00:00",
            "monotonic_duration_s": 1.0,
            "failure_evidence": {
                "operation": "exec",
                "reason": "timeout",
                "returncode": 124,
                "details": {"secret": "must not be public"},
            },
        }
    )


def test_failure_report_projects_only_bounded_failure_metadata_and_escapes_html(
    tmp_path: Path,
) -> None:
    projection = FailureReportProjection.from_evidence(
        {
            "run_id": "run-fixed",
            "repository_url": "https://github.com/owner/<script>alert(1)</script>",
            "expected_sha": "a" * 40,
            "actual_verified_sha": "a" * 40,
            "stop_reason": "sandbox:exec:timeout",
            "exit_code": 4,
            "exception_type": "SecretException",
            "failure_evidence": {
                "operation": "exec",
                "reason": "timeout",
                "returncode": 124,
                "details": {"secret": "do-not-publish"},
            },
            "unknown_field": "do-not-publish",
        }
    )

    paths = render_failure_report(projection, tmp_path / "report")
    report_text = paths.json_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    html = paths.html_path.read_text(encoding="utf-8")

    assert report["report_kind"] == "execution_failure"
    assert report["completed"] is False
    assert report["identity"]["commit_sha"] == "a" * 40
    assert report["identity"]["expected_commit_sha"] == "a" * 40
    assert report["failure"]["exception_type"] == "Exception"
    assert "unknown_field" not in report_text
    assert "do-not-publish" not in report_text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert report["availability"] == {
        "observations": "unavailable",
        "journeys": "unavailable",
        "hardening": "unavailable",
    }
    assert report["cleanup"] == {"status": "not_verified"}


def test_failure_report_collision_preserves_existing_file_and_pair_is_not_partial(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    html_path = report_dir / "trial-report.html"
    html_path.write_text("existing", encoding="utf-8")

    with pytest.raises(FailureReportWriteError):
        render_failure_report(_projection(), report_dir)

    assert html_path.read_text(encoding="utf-8") == "existing"
    assert not (report_dir / "trial-report.json").exists()
