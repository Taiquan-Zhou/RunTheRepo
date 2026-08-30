import json
from pathlib import Path

import pytest

from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.trial.journey_artifact import (
    JourneyArtifactError,
    verify_baseline_journeys,
    write_baseline_journeys,
)


def _journeys() -> list[Journey]:
    return [
        Journey(
            journey_id="health",
            name="Health",
            steps=[
                JourneyStep(
                    step_id="get-health",
                    tool="http",
                    action="request",
                    params={"method": "GET", "path": "/health"},
                    assertions=[
                        JourneyAssertion(
                            kind="status_code",
                            target="response.status",
                            expected=200,
                        )
                    ],
                )
            ],
        )
    ]


def test_baseline_journey_artifact_preserves_full_normalized_payload_and_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline-journeys.json"
    journeys = _journeys()

    payload_hash = write_baseline_journeys(path, journeys)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["payload_sha256"] == payload_hash
    assert document["journeys"][0]["steps"][0]["params"] == {
        "method": "GET",
        "path": "/health",
    }
    assert document["journeys"][0]["steps"][0]["assertions"][0] == {
        "expected": 200,
        "kind": "status_code",
        "target": "response.status",
    }
    verify_baseline_journeys(path, journeys)


def test_baseline_journey_artifact_refuses_overwrite_and_hash_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline-journeys.json"
    journeys = _journeys()
    write_baseline_journeys(path, journeys)

    with pytest.raises(JourneyArtifactError, match="already in use"):
        write_baseline_journeys(path, journeys)

    changed = _journeys()
    changed[0].steps[0].params["path"] = "/different"
    with pytest.raises(JourneyArtifactError, match="hash mismatch"):
        verify_baseline_journeys(path, changed)
