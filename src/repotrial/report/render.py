"""Render bounded trial reports from the already-recorded run state."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

from repotrial.domain.models import ObservationSnapshot, RunState

_TEMPLATE_NAME = "report.html.j2"
_HARDENED_OVERLAY_UNAVAILABLE_REASON = (
    "A single cumulative hardened overlay is not proven by the current RunState."
)


@dataclass(frozen=True, slots=True)
class TrialReportPaths:
    """Fixed output paths created by :func:`render_trial_report`."""

    json_path: Path
    html_path: Path


def render_trial_report(state: RunState, output_dir: Path) -> TrialReportPaths:
    """Write deterministic JSON and HTML reports without dereferencing artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report = _project(state)
    json_path = output_dir / "trial-report.json"
    html_path = output_dir / "trial-report.html"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    html_path.write_text(
        _template_environment().get_template(_TEMPLATE_NAME).render(report=report),
        encoding="utf-8",
    )
    return TrialReportPaths(json_path=json_path, html_path=html_path)


def _project(state: RunState) -> dict[str, object]:
    state = _validated_state(state)
    coverage = _coverage(state)
    return {
        "disclaimer": (
            "This tested-journey/workload-conditioned hardened candidate is not a "
            "security proof."
        ),
        "identity": {
            "run_id": state.run_id,
            "repo_url": state.repo_url,
            "commit_sha": state.commit_sha,
            "baseline_config_hash": state.baseline_config_hash,
            "current_config_hash": state.current_config_hash,
            "current_compose_reference": state.compose_path,
        },
        "coverage": coverage,
        "stop_reason": state.stop_reason,
        "recovery_env_keys": list(state.recovery_env_keys),
        "baseline_risk_findings": [
            finding.model_dump(mode="json") for finding in state.risk_findings
        ],
        "baseline_observation": _snapshot(state.baseline_observation),
        "experiments": [
            {
                "experiment_id": record.experiment_id,
                "parent_config_hash": record.parent_config_hash,
                "candidate_config_hash": record.candidate_config_hash,
                "mutation": record.mutation.model_dump(mode="json"),
                "boot": record.boot.value,
                "journeys": [
                    result.model_dump(mode="json") for result in record.journeys
                ],
                "before": _snapshot(record.before),
                "after": _snapshot(record.after),
                "verdict": record.verdict.value,
                "reason": record.reason,
            }
            for record in state.experiments
        ],
        "unsupported_collectors": _unsupported_collectors(state),
        "artifacts": {
            "references": list(state.artifacts),
            "experiment_overlays": [
                reference
                for reference in state.artifacts
                if reference != state.compose_path
                and reference != state.compatibility_overlay_path
                and reference.lower().endswith((".overlay.yaml", ".overlay.yml"))
            ],
            "compatibility_overlay": {
                "reference": state.compatibility_overlay_path,
                "sha256": state.compatibility_overlay_sha256,
            },
            "accepted_configuration": {
                "compose_reference": state.compose_path,
                "config_hash": state.current_config_hash,
            },
            "hardened_overlay": {
                "status": "unavailable",
                "reason": _HARDENED_OVERLAY_UNAVAILABLE_REASON,
            },
        },
    }


def _validated_state(state: RunState) -> RunState:
    try:
        return RunState.model_validate(state)
    except ValidationError:
        raise ValueError("invalid run state") from None


def _coverage(state: RunState) -> dict[str, object]:
    results_by_id = {
        result.journey_id: result for result in state.baseline_journey_results
    }
    journeys = []
    for journey in state.journeys:
        result = results_by_id.get(journey.journey_id)
        journeys.append(
            {
                "journey_id": journey.journey_id,
                "name": journey.name,
                "classification": "untested" if result is None else result.verdict.name,
            }
        )
    completed = sum(item["classification"] != "untested" for item in journeys)
    return {
        "summary": f"{completed}/{len(journeys)} journeys",
        "journeys": journeys,
    }


def _snapshot(snapshot: ObservationSnapshot | None) -> dict[str, object] | None:
    return None if snapshot is None else snapshot.model_dump(mode="json")


def _unsupported_collectors(state: RunState) -> list[dict[str, object]]:
    collectors: dict[str, list[str]] = {}
    for location, snapshot in _snapshots(state):
        if snapshot is None:
            continue
        for collector in snapshot.unsupported_collectors:
            locations = collectors.setdefault(collector, [])
            if location not in locations:
                locations.append(location)
    return [
        {"collector": collector, "observed_in": locations}
        for collector, locations in collectors.items()
    ]


def _snapshots(state: RunState) -> Iterable[tuple[str, ObservationSnapshot | None]]:
    yield "baseline", state.baseline_observation
    for record in state.experiments:
        yield f"experiment:{record.experiment_id}:before", record.before
        yield f"experiment:{record.experiment_id}:after", record.after


def _template_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=True,
        keep_trailing_newline=True,
    )
