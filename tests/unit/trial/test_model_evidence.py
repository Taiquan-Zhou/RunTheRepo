import json
import math
import os
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from repotrial.trial.model_evidence import (
    ModelAttemptEvidenceError,
    ModelAttemptOutcome,
    ModelAttemptRecorder,
)


class _Proposal(BaseModel):
    answer: str


def test_model_attempt_records_bounded_start_and_success_metadata(
    tmp_path: Path,
) -> None:
    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system secret-value",
        user="README secret-value",
        schema=_Proposal,
    )

    recorder.finish_success({"answer": "secret-value"}, journey_count=1)

    path = recorder.path
    assert path.name == "baseline-model-attempt-0001.jsonl"
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


def test_model_attempt_reentry_uses_a_new_deterministic_slot_without_overwrite(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "baseline-model-attempt-0001.jsonl"
    existing.write_text('{"phase":"start"}\n', encoding="utf-8")

    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )
    recorder.finish_failure("adapter_error")

    assert recorder.path.name == "baseline-model-attempt-0002.jsonl"
    assert existing.read_text(encoding="utf-8") == '{"phase":"start"}\n'


def test_model_attempt_slot_symlink_is_never_followed_or_overwritten(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"outside\n")
    linked_slot = tmp_path / "baseline-model-attempt-0001.jsonl"
    linked_slot.symlink_to(outside)

    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )
    recorder.finish_failure("adapter_error")

    assert recorder.path.name == "baseline-model-attempt-0002.jsonl"
    assert outside.read_bytes() == b"outside\n"


@pytest.mark.parametrize("terminal", ["success", "failure"])
def test_model_attempt_terminal_reuses_claimed_descriptor_without_reopening_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    real_open = os.open
    real_write = os.write
    real_close = os.close
    opened: list[int] = []
    written: list[int] = []
    closed: list[int] = []

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        opened.append(descriptor)
        return descriptor

    def record_write(descriptor: int, payload: bytes) -> int:
        written.append(descriptor)
        return real_write(descriptor, payload)

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr("repotrial.trial.model_evidence.os.open", record_open)
    monkeypatch.setattr("repotrial.trial.model_evidence.os.write", record_write)
    monkeypatch.setattr("repotrial.trial.model_evidence.os.close", record_close)
    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )
    assert len(opened) == 1
    claimed = opened[0]
    os.fstat(claimed)

    def reject_reopen(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        del path, flags, mode
        raise AssertionError("terminal must not reopen the evidence path")

    monkeypatch.setattr("repotrial.trial.model_evidence.os.open", reject_reopen)
    if terminal == "success":
        recorder.finish_success({"answer": "ok"}, journey_count=1)
    else:
        recorder.finish_failure("adapter_error")
    recorder.close()
    recorder.close()

    assert written and set(written) == {claimed}
    assert closed.count(claimed) == 1
    with pytest.raises(OSError):
        os.fstat(claimed)


def test_model_attempt_explicit_close_is_idempotent_and_preserves_start_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        opened.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr("repotrial.trial.model_evidence.os.open", record_open)
    monkeypatch.setattr("repotrial.trial.model_evidence.os.close", record_close)
    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )

    recorder.close()
    recorder.close()

    assert closed.count(opened[0]) == 1
    rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert [row["phase"] for row in rows] == ["start"]


def test_model_attempt_short_writes_are_completed_and_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    real_fsync = os.fsync
    write_sizes: list[int] = []
    fsync_calls = 0

    def short_write(descriptor: int, payload: bytes) -> int:
        chunk = payload[:7]
        write_sizes.append(len(chunk))
        return real_write(descriptor, chunk)

    def record_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(descriptor)

    monkeypatch.setattr("repotrial.trial.model_evidence.os.write", short_write)
    monkeypatch.setattr("repotrial.trial.model_evidence.os.fsync", record_fsync)
    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )

    recorder.finish_failure("adapter_error")

    rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert [row["phase"] for row in rows] == ["start", "terminal"]
    assert len(write_sizes) > 2
    assert fsync_calls == 2


def test_partial_terminal_is_not_reused_as_a_successful_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )
    real_write = os.write
    terminal_calls = 0

    def fail_after_partial(descriptor: int, payload: bytes) -> int:
        nonlocal terminal_calls
        terminal_calls += 1
        if terminal_calls == 1:
            return real_write(descriptor, payload[:5])
        raise OSError("simulated interrupted append")

    monkeypatch.setattr("repotrial.trial.model_evidence.os.write", fail_after_partial)
    with pytest.raises(ModelAttemptEvidenceError, match="append"):
        first.finish_success({"answer": "ok"}, journey_count=1)
    monkeypatch.setattr("repotrial.trial.model_evidence.os.write", real_write)

    second = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )
    second.finish_success({"answer": "ok"}, journey_count=1)

    assert first.path.name.endswith("0001.jsonl")
    assert second.path.name.endswith("0002.jsonl")
    second_rows = [json.loads(line) for line in second.path.read_text().splitlines()]
    assert second_rows[-1]["outcome"] == "success"


def test_model_attempt_outcome_is_runtime_closed(tmp_path: Path) -> None:
    recorder = ModelAttemptRecorder(
        tmp_path,
        purpose="journey",
        system="system",
        user="user",
        schema=_Proposal,
    )

    with pytest.raises(ValueError, match="outcome"):
        recorder.finish_failure(cast(ModelAttemptOutcome, "unknown"))
