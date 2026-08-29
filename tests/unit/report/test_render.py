import importlib
import json
from pathlib import Path

import pytest

from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    Journey,
    JourneyResult,
    JourneyStep,
    Mutation,
    ObservationSnapshot,
    RiskFinding,
    RunState,
)


def _journey(journey_id: str) -> Journey:
    return Journey(
        journey_id=journey_id,
        name=f"{journey_id} journey",
        steps=[
            JourneyStep(
                step_id=f"{journey_id}-step", tool="http", action="get", params={}
            )
        ],
    )


def _result(journey_id: str, verdict: Verdict = Verdict.PASS) -> JourneyResult:
    return JourneyResult(
        journey_id=journey_id,
        verdict=verdict,
        passed_steps=1 if verdict is Verdict.PASS else 0,
        total_steps=1,
        evidence_paths=[f"evidence/{journey_id}.json"],
    )


def _state(*, hostile: bool = False, untested: bool = False) -> RunState:
    journeys = [_journey(f"journey-{number}") for number in range(1, 5)]
    results = [_result(journey.journey_id) for journey in journeys]
    if untested:
        results = [
            _result("journey-1", Verdict.PASS),
            _result("journey-2", Verdict.UNSUPPORTED),
            _result("journey-3", Verdict.FAIL),
        ]
    before = ObservationSnapshot(
        inspect={"baseline": "observed"},
        unsupported_collectors=["network_runtime", "ebpf"],
    )
    keep = ExperimentRecord(
        experiment_id="keep-read-only",
        parent_config_hash="baseline-hash",
        candidate_config_hash="keep-hash",
        mutation=Mutation(
            mutation_id="read-only",
            type=MutationType.SET_READ_ONLY,
            service="web",
            params={
                "note": "<script>alert('unsafe')</script>" if hostile else "verified"
            },
        ),
        boot=Verdict.PASS,
        journeys=results,
        before=before,
        after=ObservationSnapshot(
            unsupported_collectors=["network_runtime", "proc_events"]
        ),
        verdict=ExperimentVerdict.KEEP,
        reason="baseline_pass_journeys_preserved",
    )
    rollback = ExperimentRecord(
        experiment_id="rollback-caps",
        parent_config_hash="keep-hash",
        candidate_config_hash="rollback-hash",
        mutation=Mutation(
            mutation_id="drop-caps",
            type=MutationType.DROP_ALL_CAPS,
            service="web",
        ),
        boot=Verdict.FAIL,
        journeys=[],
        before=ObservationSnapshot(unsupported_collectors=["ebpf"]),
        after=ObservationSnapshot(unsupported_collectors=["proc_events"]),
        verdict=ExperimentVerdict.ROLLBACK,
        reason="boot_regression",
    )
    return RunState(
        run_id="run-001",
        repo_url=(
            "https://example.invalid/<script>repo</script>"
            if hostile
            else "https://example.invalid/repo"
        ),
        commit_sha="a" * 40,
        compose_path="artifacts/accepted-compose.yaml",
        baseline_config_hash="baseline-hash",
        current_config_hash="keep-hash",
        risk_findings=[
            RiskFinding(
                finding_id="privileged-web",
                kind="privileged",
                service="web",
                severity=90,
                evidence={"privileged": True},
            )
        ],
        journeys=journeys,
        baseline_journey_results=results,
        baseline_observation=before,
        experiments=[keep, rollback],
        artifacts=[
            "artifacts/first.overlay.yaml",
            "artifacts/second.overlay.yml",
            "artifacts/nonexistent-sentinel.json",
            "artifacts/accepted-compose.yaml",
        ],
        stop_reason="no_remaining_mutations",
    )


def _render(state: RunState, output_dir: Path):
    render_module = importlib.import_module("repotrial.report.render")
    return render_module.render_trial_report(state, output_dir)


def test_renderer_writes_complete_stable_json_and_html_snapshot(tmp_path: Path) -> None:
    paths = _render(_state(), tmp_path)

    report = json.loads(paths.json_path.read_text(encoding="utf-8"))
    html = paths.html_path.read_text(encoding="utf-8")

    assert paths.json_path == tmp_path / "trial-report.json"
    assert paths.html_path == tmp_path / "trial-report.html"
    assert report["identity"] == {
        "baseline_config_hash": "baseline-hash",
        "commit_sha": "a" * 40,
        "current_compose_reference": "artifacts/accepted-compose.yaml",
        "current_config_hash": "keep-hash",
        "repo_url": "https://example.invalid/repo",
        "run_id": "run-001",
    }
    assert report["coverage"]["summary"] == "4/4 journeys"
    assert report["stop_reason"] == "no_remaining_mutations"
    assert [item["classification"] for item in report["coverage"]["journeys"]] == [
        "PASS",
        "PASS",
        "PASS",
        "PASS",
    ]
    assert report["baseline_risk_findings"][0]["finding_id"] == "privileged-web"
    assert report["baseline_observation"]["inspect"] == {"baseline": "observed"}
    assert [item["experiment_id"] for item in report["experiments"]] == [
        "keep-read-only",
        "rollback-caps",
    ]
    assert [item["verdict"] for item in report["experiments"]] == ["keep", "rollback"]
    assert [
        (item["reason"], [journey["journey_id"] for journey in item["journeys"]])
        for item in report["experiments"]
    ] == [
        (
            "baseline_pass_journeys_preserved",
            ["journey-1", "journey-2", "journey-3", "journey-4"],
        ),
        ("boot_regression", []),
    ]
    assert "4/4 journeys" in html
    assert "KEEP" in html
    assert "ROLLBACK" in html
    assert "not a security proof" in html


def test_renderer_keeps_untested_and_unsupported_coverage_distinct(
    tmp_path: Path,
) -> None:
    report = json.loads(
        _render(_state(untested=True), tmp_path).json_path.read_text(encoding="utf-8")
    )

    assert [item["classification"] for item in report["coverage"]["journeys"]] == [
        "PASS",
        "UNSUPPORTED",
        "FAIL",
        "untested",
    ]
    assert report["coverage"]["summary"] == "3/4 journeys"


def test_renderer_preserves_collector_provenance_and_artifact_references_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_read(*args: object, **kwargs: object) -> str:
        raise AssertionError("renderer must not dereference artifact references")

    monkeypatch.setattr(Path, "read_text", fail_if_read)

    paths = _render(_state(), tmp_path)
    report = json.loads(paths.json_path.read_bytes().decode("utf-8"))

    assert report["unsupported_collectors"] == [
        {
            "collector": "network_runtime",
            "observed_in": [
                "baseline",
                "experiment:keep-read-only:before",
                "experiment:keep-read-only:after",
            ],
        },
        {
            "collector": "ebpf",
            "observed_in": [
                "baseline",
                "experiment:keep-read-only:before",
                "experiment:rollback-caps:before",
            ],
        },
        {
            "collector": "proc_events",
            "observed_in": [
                "experiment:keep-read-only:after",
                "experiment:rollback-caps:after",
            ],
        },
    ]
    assert report["artifacts"]["references"] == _state().artifacts
    assert report["artifacts"]["experiment_overlays"] == [
        "artifacts/first.overlay.yaml",
        "artifacts/second.overlay.yml",
    ]


def test_renderer_selects_only_established_overlay_artifact_suffixes(
    tmp_path: Path,
) -> None:
    state = _state()
    state.compose_path = "artifacts/current.compose.yaml"
    state.artifacts = [
        "artifacts/first.overlay.yaml",
        "artifacts/older-accepted.compose.yaml",
        "artifacts/second.overlay.yml",
        "artifacts/current.compose.yaml",
    ]

    report = json.loads(_render(state, tmp_path).json_path.read_text(encoding="utf-8"))

    assert report["artifacts"]["experiment_overlays"] == [
        "artifacts/first.overlay.yaml",
        "artifacts/second.overlay.yml",
    ]
    assert report["artifacts"]["references"] == state.artifacts


def test_renderer_escapes_hostile_html_and_marks_hardened_overlay_unavailable(
    tmp_path: Path,
) -> None:
    paths = _render(_state(hostile=True), tmp_path)
    report = json.loads(paths.json_path.read_text(encoding="utf-8"))
    html = paths.html_path.read_text(encoding="utf-8")

    assert "<script>repo</script>" not in html
    assert "&lt;script&gt;repo&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(&#39;unsafe&#39;)&lt;/script&gt;" in html
    assert report["artifacts"]["accepted_configuration"] == {
        "compose_reference": "artifacts/accepted-compose.yaml",
        "config_hash": "keep-hash",
    }
    assert report["artifacts"]["hardened_overlay"] == {
        "reason": "A single cumulative hardened overlay is not proven by the current RunState.",
        "status": "unavailable",
    }
