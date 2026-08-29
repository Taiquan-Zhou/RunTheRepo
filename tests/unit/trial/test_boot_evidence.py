import json
from pathlib import Path

import pytest

from repotrial.domain.enums import Verdict
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult
from repotrial.trial import boot_evidence


def _session(path: Path, env: dict[str, str]) -> boot_evidence._BootEvidenceSession:
    return boot_evidence._BootEvidenceSession(path, env)


def _write_complete_evidence(path: Path, env: dict[str, str]) -> None:
    session = _session(path, env)
    session.record_command(
        "up",
        ExecResult(exit_code=1, stdout="up-output", stderr="error-" + ("x" * 80_000)),
    )
    session.record_command("ps", ExecResult(exit_code=0, stdout="[]", stderr=""))
    session.record_command("logs", ExecResult(exit_code=0, stdout="logs", stderr=""))
    session.finalize(Verdict.FAIL, {"web": "exited"})


def test_evidence_schema_is_bounded_deterministic_and_redacts_all_runtime_values(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "baseline-boot-attempt.json"
    secret = "postgresql://alice:super-secret@db.invalid/app"
    _write_complete_evidence(evidence_path, {"PUBLIC_DSN": secret})

    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert [item["name"] for item in payload["commands"]] == ["up", "ps", "logs"]
    assert payload["commands"][0]["exit_code"] == 1
    assert payload["commands"][0]["stderr"]["truncated"] is True
    assert payload["final"]["verdict"] == "fail"
    assert len(evidence_path.read_bytes()) <= 262_144
    assert secret not in evidence_path.read_text(encoding="utf-8")
    assert evidence_path.read_bytes().endswith(b"\n")


def test_evidence_redacts_four_byte_values_that_cross_head_tail_boundary(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "baseline-boot-attempt.json"
    secret = "left-💩-right"
    session = _session(evidence_path, {"UNRELATED_VALUE": secret})
    session.record_command(
        "up",
        ExecResult(
            exit_code=1,
            stdout=("a" * 40_000) + secret + ("b" * 40_000),
            stderr="",
        ),
    )
    session.finalize(Verdict.FAIL, {})

    persisted = evidence_path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "left-" not in persisted
    assert "-right" not in persisted


def test_evidence_records_malformed_results_and_exception_class_without_raw_exception(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "baseline-boot-attempt.json"
    session = _session(evidence_path, {"APP_VALUE": "never-persist-this"})
    session.record_exception("up", TypeError("never-persist-this"))
    session.finalize(Verdict.FAIL, {})

    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["commands"] == [{"exception_type": "TypeError", "name": "up"}]
    assert "never-persist-this" not in evidence_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("target_kind", ["existing", "link", "missing_parent"])
def test_evidence_rejects_untrusted_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    evidence_path = tmp_path / "baseline-boot-attempt.json"
    if target_kind == "existing":
        evidence_path.write_text("existing", encoding="utf-8")
    elif target_kind == "link":
        monkeypatch.setattr(boot_evidence, "_is_link", lambda _: True)
    else:
        evidence_path = tmp_path / "missing" / evidence_path.name

    with pytest.raises(boot_evidence.BootEvidenceError):
        _session(evidence_path, {})


def test_recovery_evidence_replaces_redacted_checkpoint_with_rationale_and_disposition(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "baseline-boot-attempt.json"
    _write_complete_evidence(evidence_path, {"APP_VALUE": "not-for-artifact"})

    boot_evidence.record_recovery_evidence(
        evidence_path,
        RecoveryAction(action="wait", params={"seconds": 1}, reason="service warming"),
        disposition="applied",
        stop_reason=None,
    )

    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["recovery"] == {
        "action": "wait",
        "disposition": "applied",
        "reason": "service warming",
        "stop_reason": None,
    }
    assert len(evidence_path.read_bytes()) <= 262_144


def test_failed_replacement_never_writes_unredacted_temporary_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path = tmp_path / "baseline-boot-attempt.json"
    secret = "temporary-secret-value"
    session = _session(evidence_path, {"NON_SENSITIVE_NAME": secret})
    session.record_command("up", ExecResult(exit_code=1, stdout=secret, stderr=""))

    monkeypatch.setattr(
        boot_evidence.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("blocked")),
    )
    with pytest.raises(boot_evidence.BootEvidenceError):
        session.finalize(Verdict.FAIL, {})

    for candidate in tmp_path.iterdir():
        assert secret not in candidate.read_text(encoding="utf-8")
