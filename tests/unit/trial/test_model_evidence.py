import json
import math
from pathlib import Path

import pytest
from pydantic import BaseModel

from repotrial.trial.model_evidence import (
    ModelAttemptEvidenceError,
    ModelAttemptRecorder,
)


class _Proposal(BaseModel):
    answer: str


def test_model_attempt_records_bounded_start_and_success_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline-model-attempt.jsonl"
    recorder = ModelAttemptRecorder(
        path,
        purpose="journey",
        system="system secret-value",
        user="README secret-value",
        schema=_Proposal,
    )

    recorder.finish_success({"answer": "secret-value"}, journey_count=1)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["phase"] for row in rows] == ["start", "terminal"]
    assert rows[0]["purpose"] == "journey"
    assert rows[1]["outcome"] == "success"
    assert rows[1]["journey_count"] == 1
    assert len(rows[0]["system_sha256"]) == 64
    assert len(rows[0]["user_sha256"]) == 64
    assert len(rows[0]["schema_sha256"]) == 64
    assert len(rows[1]["accepted_output_sha256"]) == 64
    assert all(
        math.isfinite(row["elapsed_s"]) and row["elapsed_s"] >= 0 for row in rows
    )
    serialized = path.read_text(encoding="utf-8")
    assert "secret-value" not in serialized


def test_model_attempt_existing_or_link_destination_fails_without_overwrite(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "baseline-model-attempt.jsonl"
    existing.write_text("existing\n", encoding="utf-8")

    with pytest.raises(ModelAttemptEvidenceError, match="already in use"):
        ModelAttemptRecorder(
            existing,
            purpose="journey",
            system="system",
            user="user",
            schema=_Proposal,
        )

    assert existing.read_text(encoding="utf-8") == "existing\n"
