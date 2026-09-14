from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from repotrial.local_web.progress import _scandir, project_progress

RUN_ID = "11111111-1111-4111-8111-111111111111"


def _attempt_dir(root: Path, *, purpose: str = "experiment", index: int = 1) -> Path:
    token = hashlib.sha256(RUN_ID.encode()).hexdigest()[:16]
    path = (
        root
        / "artifacts"
        / RUN_ID
        / "evidence"
        / f"{purpose}-{token}-{index:04d}-attempt-01"
    )
    path.mkdir(parents=True)
    (path / ".repotrial-attempt.json").write_text(
        json.dumps({"index": index, "purpose": purpose, "run_token": token}),
        encoding="utf-8",
    )
    return path


def test_missing_evidence_stays_at_earliest_phase(tmp_path: Path) -> None:
    (tmp_path / "artifacts" / RUN_ID).mkdir(parents=True)

    assert project_progress(tmp_path, RUN_ID, now=100.0) == {
        "phase": "preparing_repository",
        "completed_phases": [],
    }


def test_progress_projects_experiment_event_and_age(tmp_path: Path) -> None:
    attempt = _attempt_dir(tmp_path, index=2)
    lifecycle = attempt / "candidate-lifecycle.jsonl"
    lifecycle.write_text(
        '{"event":"create_attempt"}\n'
        '{"event":"provider_failure","failure":{"operation":"exec",'
        '"reason":"total_duration_exhausted"}}\n',
        encoding="utf-8",
    )
    os.utime(lifecycle, (90.0, 90.0))
    os.utime(attempt / ".repotrial-attempt.json", (90.0, 90.0))

    assert project_progress(tmp_path, RUN_ID, now=100.0) == {
        "phase": "hardening",
        "completed_phases": [],
        "experiment_index": 3,
        "latest_event": "provider_failure",
        "latest_operation": "exec",
        "evidence_age_seconds": 10.0,
    }


def test_old_or_mismatched_attempt_is_ignored(tmp_path: Path) -> None:
    attempt = _attempt_dir(tmp_path, index=1)
    marker = json.loads((attempt / ".repotrial-attempt.json").read_text())
    marker["run_token"] = "0" * 16
    (attempt / ".repotrial-attempt.json").write_text(json.dumps(marker))
    (attempt / "candidate-lifecycle.jsonl").write_text(
        '{"event":"provider_failure","failure":{"operation":"exec"}}\n',
        encoding="utf-8",
    )

    assert project_progress(tmp_path, RUN_ID, now=100.0) == {
        "phase": "preparing_repository",
        "completed_phases": [],
    }


def test_partial_json_is_ignored_and_symlinked_attempt_is_rejected(
    tmp_path: Path,
) -> None:
    real = _attempt_dir(tmp_path, index=1)
    (real / "candidate-lifecycle.jsonl").write_text(
        '{"event":"provider_failure"\n', encoding="utf-8"
    )
    symlink = real.parent / "experiment-old-0000-attempt-01"
    symlink.symlink_to(real, target_is_directory=True)

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "hardening"
    assert result.get("latest_event") is None
    assert result.get("latest_operation") is None


def test_baseline_completion_requires_boot_and_all_planned_steps(
    tmp_path: Path,
) -> None:
    token = hashlib.sha256(RUN_ID.encode()).hexdigest()[:16]
    attempt = _attempt_dir(tmp_path, purpose="baseline", index=1)
    lifecycle = attempt / "baseline-lifecycle.jsonl"
    lifecycle.write_text('{"event":"create_success"}\n', encoding="utf-8")
    (attempt / "baseline-boot-attempt.json").write_text(
        json.dumps({"final": {"verdict": "pass"}}), encoding="utf-8"
    )
    step = attempt / "baseline-journey-0000"
    step.mkdir()
    (step / "step-0000.json").write_text(
        json.dumps(
            {
                "assertions": [{"outcome": "passed"}],
                "failure_category": None,
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "artifacts" / RUN_ID / "evidence" / f"run-{token}"
    plan.mkdir()
    (plan / "baseline-journeys.json").write_text(
        json.dumps({"journeys": [{"steps": [{"step_id": "get"}]}]}),
        encoding="utf-8",
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "collecting_observations"
    assert result["completed_phases"] == [
        "preparing_repository",
        "preparing_environment",
        "starting_application",
        "checking_application",
    ]


def test_failed_terminal_evidence_keeps_hardening_phase(tmp_path: Path) -> None:
    attempt = _attempt_dir(tmp_path, index=1)
    (attempt / "candidate-lifecycle.jsonl").write_text(
        '{"event":"destroy_success"}\n', encoding="utf-8"
    )
    run_path = tmp_path / "artifacts" / RUN_ID
    (run_path / "attempt-result.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "terminal_outcome": "trial_failed",
                "exit_code": 3,
                "stop_reason": "experiment:total_duration_exhausted",
            }
        ),
        encoding="utf-8",
    )
    (run_path / "report").mkdir()
    (run_path / "report" / "trial-report.json").write_text(
        json.dumps({"identity": {"run_id": RUN_ID}}), encoding="utf-8"
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "hardening"
    assert result["completed_phases"] == []
    assert result["experiment_index"] == 2


def test_matching_completed_terminal_evidence_marks_finalizing(
    tmp_path: Path,
) -> None:
    _attempt_dir(tmp_path, purpose="baseline", index=1)
    run_path = tmp_path / "artifacts" / RUN_ID
    (run_path / "attempt-result.json").write_text(
        json.dumps({"run_id": RUN_ID, "terminal_outcome": "completed", "exit_code": 0}),
        encoding="utf-8",
    )
    (run_path / "report").mkdir()
    (run_path / "report" / "trial-report.json").write_text(
        json.dumps({"identity": {"run_id": RUN_ID}}), encoding="utf-8"
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "finalizing"
    assert result["completed_phases"] == ["finalizing"]


def test_symlinked_artifact_ancestor_is_ignored(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    (actual / "artifacts" / RUN_ID / "evidence").mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    assert project_progress(linked, RUN_ID, now=100.0) == {
        "phase": "preparing_repository",
        "completed_phases": [],
    }


def test_create_attempt_without_success_stays_in_environment_preparation(
    tmp_path: Path,
) -> None:
    attempt = _attempt_dir(tmp_path, purpose="baseline", index=1)
    (attempt / "baseline-lifecycle.jsonl").write_text(
        '{"event":"create_attempt"}\n', encoding="utf-8"
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "preparing_environment"
    assert result["completed_phases"] == []


def test_create_success_without_boot_enters_starting_phase(tmp_path: Path) -> None:
    attempt = _attempt_dir(tmp_path, purpose="baseline", index=1)
    (attempt / "baseline-lifecycle.jsonl").write_text(
        '{"event":"create_success"}\n', encoding="utf-8"
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "starting_application"
    assert result["completed_phases"] == ["preparing_environment"]


def test_warmup_create_success_stays_in_environment_preparation(
    tmp_path: Path,
) -> None:
    attempt = _attempt_dir(tmp_path, purpose="baseline", index=1)
    (attempt / "warmup-lifecycle.jsonl").write_text(
        '{"event":"create_attempt"}\n'
        '{"event":"create_success","sandbox_id":"warmup"}\n'
        '{"event":"destroy_success","sandbox_id":"warmup"}\n',
        encoding="utf-8",
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)

    assert result["phase"] == "preparing_environment"
    assert result["completed_phases"] == []


def test_real_named_symlink_attempt_is_rejected(tmp_path: Path) -> None:
    token = hashlib.sha256(RUN_ID.encode()).hexdigest()[:16]
    evidence = tmp_path / "artifacts" / RUN_ID / "evidence"
    evidence.mkdir(parents=True)
    target = tmp_path / "outside-attempt"
    target.mkdir()
    (target / ".repotrial-attempt.json").write_text(
        json.dumps({"index": 0, "purpose": "experiment", "run_token": token}),
        encoding="utf-8",
    )
    link = evidence / f"experiment-{token}-0000-attempt-01"
    link.symlink_to(target, target_is_directory=True)

    assert project_progress(tmp_path, RUN_ID, now=100.0) == {
        "phase": "preparing_repository",
        "completed_phases": [],
    }


def test_artifacts_ancestor_symlink_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    (actual / "artifacts" / RUN_ID / "evidence").mkdir(parents=True)
    (tmp_path / "artifacts").symlink_to(actual / "artifacts", target_is_directory=True)

    assert project_progress(tmp_path, RUN_ID, now=100.0) == {
        "phase": "preparing_repository",
        "completed_phases": [],
    }


def test_oversized_nonregular_and_invalid_json_evidence_is_ignored(
    tmp_path: Path,
) -> None:
    attempt = _attempt_dir(tmp_path, index=1)
    (attempt / "candidate-lifecycle.jsonl").write_bytes(b"x" * (256 * 1024 + 1))
    (attempt / "candidate-observation.json").mkdir()
    (attempt / "candidate-bad.json").write_text("{", encoding="utf-8")

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "hardening"
    assert result.get("latest_event") is None
    assert result.get("latest_operation") is None


def test_scan_is_bounded_before_late_attempt(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for index in range(100):
        (evidence / f"entry-{index:03d}").mkdir()

    assert len(_scandir(evidence)) == 64


def test_failed_boot_and_incomplete_multi_journey_do_not_complete(
    tmp_path: Path,
) -> None:
    token = hashlib.sha256(RUN_ID.encode()).hexdigest()[:16]
    attempt = _attempt_dir(tmp_path, purpose="baseline", index=1)
    (attempt / "baseline-lifecycle.jsonl").write_text(
        '{"event":"create_success"}\n', encoding="utf-8"
    )
    (attempt / "baseline-boot-attempt.json").write_text(
        json.dumps({"final": {"verdict": "fail"}}), encoding="utf-8"
    )
    for journey_index in range(2):
        journey = attempt / f"baseline-journey-{journey_index:04d}"
        journey.mkdir()
        if journey_index == 0:
            (journey / "step-0000.json").write_text(
                json.dumps(
                    {
                        "assertions": [{"outcome": "passed"}],
                        "failure_category": None,
                    }
                ),
                encoding="utf-8",
            )
    plan = tmp_path / "artifacts" / RUN_ID / "evidence" / f"run-{token}"
    plan.mkdir()
    (plan / "baseline-journeys.json").write_text(
        json.dumps(
            {
                "journeys": [
                    {"steps": [{"step_id": "one"}]},
                    {"steps": [{"step_id": "two"}]},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "starting_application"
    assert result["completed_phases"] == [
        "preparing_repository",
        "preparing_environment",
    ]


def test_failed_step_does_not_complete_multi_journey_plan(tmp_path: Path) -> None:
    token = hashlib.sha256(RUN_ID.encode()).hexdigest()[:16]
    attempt = _attempt_dir(tmp_path, purpose="baseline", index=1)
    (attempt / "baseline-boot-attempt.json").write_text(
        json.dumps({"final": {"verdict": "pass"}}), encoding="utf-8"
    )
    for journey_index, outcome in enumerate(("passed", "failed")):
        journey = attempt / f"baseline-journey-{journey_index:04d}"
        journey.mkdir()
        (journey / "step-0000.json").write_text(
            json.dumps(
                {
                    "assertions": [{"outcome": outcome}],
                    "failure_category": None if outcome == "passed" else "assertion",
                }
            ),
            encoding="utf-8",
        )
    plan = tmp_path / "artifacts" / RUN_ID / "evidence" / f"run-{token}"
    plan.mkdir()
    (plan / "baseline-journeys.json").write_text(
        json.dumps(
            {
                "journeys": [
                    {"steps": [{"step_id": "one"}]},
                    {"steps": [{"step_id": "two"}]},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = project_progress(tmp_path, RUN_ID, now=100.0)
    assert result["phase"] == "checking_application"
    assert result["completed_phases"] == [
        "preparing_repository",
        "starting_application",
    ]
