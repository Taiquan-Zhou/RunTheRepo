import asyncio
import hashlib
import traceback
from pathlib import Path
from typing import cast

import pytest

from repotrial.agent import graph as graph_module
from repotrial.compose.compatibility import CompatibilityError
from repotrial.sandbox.base import (
    ExecResult,
    SandboxFailureEvidence,
    attach_sandbox_failure_evidence,
    get_sandbox_failure_evidence,
)
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.compatibility import verify_guest_compatibility_overlay


def _run_verifier(
    provider: FakeSandboxProvider,
    sandbox_id: str,
    relative_path: str,
    expected_sha256: str,
) -> None:
    asyncio.run(
        verify_guest_compatibility_overlay(
            provider,
            sandbox_id,
            relative_path=relative_path,
            expected_sha256=expected_sha256,
        )
    )


def test_guest_compatibility_verifier_uses_bounded_argv_and_exact_digest(
    tmp_path: Path,
) -> None:
    relative_path = ".repotrial-overlays/compatibility.overlay.yaml"
    payload = b"services: {}\n"
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    provider = FakeSandboxProvider(
        scripts={
            ("sha256sum", "--", relative_path): ExecResult(
                exit_code=0,
                stdout=f"{expected_sha256}  {relative_path}\n",
                stderr="",
            )
        }
    )
    sandbox_id = asyncio.run(provider.create(tmp_path, "guest-verify"))

    _run_verifier(provider, sandbox_id, relative_path, expected_sha256)

    assert provider.calls[-1] == (
        "exec",
        sandbox_id,
        ("sha256sum", "--", relative_path),
        30,
    )


@pytest.mark.parametrize(
    ("result", "expected_reason", "expected_branch"),
    [
        (
            ExecResult(exit_code=1, stdout="", stderr="missing"),
            "guest_artifact_missing",
            "command_failed",
        ),
        (
            ExecResult(exit_code=0, stdout="not-a-digest\n", stderr=""),
            "guest_output_malformed",
            "format",
        ),
        (
            ExecResult(exit_code=0, stdout="a" * 64 + "  wrong\n", stderr=""),
            "guest_output_malformed",
            "path",
        ),
        (
            ExecResult(
                exit_code=0,
                stdout="b" * 64 + "  .repotrial-overlays/compatibility.overlay.yaml\n",
                stderr="",
            ),
            "guest_hash_mismatch",
            "hash",
        ),
        (
            ExecResult(exit_code=0, stdout="x" * 2048, stderr=""),
            "guest_output_oversize",
            "output_oversize",
        ),
    ],
)
def test_guest_compatibility_verifier_fails_closed_for_invalid_guest_results(
    tmp_path: Path,
    result: ExecResult,
    expected_reason: str,
    expected_branch: str,
) -> None:
    relative_path = ".repotrial-overlays/compatibility.overlay.yaml"
    expected_sha256 = "a" * 64
    provider = FakeSandboxProvider(scripts={("sha256sum", "--", relative_path): result})
    sandbox_id = asyncio.run(provider.create(tmp_path, "guest-verify"))

    with pytest.raises(
        CompatibilityError, match=f"compatibility planning failed: {expected_reason}"
    ) as raised:
        _run_verifier(provider, sandbox_id, relative_path, expected_sha256)
    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "compatibility"
    assert evidence.reason == expected_reason
    assert evidence.details["check"] == "overlay_digest"
    assert evidence.details["failure_branch"] == expected_branch


def test_guest_verification_evidence_is_bounded_and_persisted_by_template_record(
    tmp_path: Path,
) -> None:
    relative_path = ".repotrial-overlays/compatibility.overlay.yaml"
    provider = FakeSandboxProvider(
        scripts={
            ("sha256sum", "--", relative_path): ExecResult(
                exit_code=0,
                stdout="not-a-digest\n",
                stderr="secret stderr must not persist",
            )
        }
    )
    sandbox_id = asyncio.run(provider.create(tmp_path, "guest-verify"))

    with pytest.raises(CompatibilityError) as raised:
        _run_verifier(provider, sandbox_id, relative_path, "a" * 64)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "compatibility"
    assert evidence.reason == "guest_output_malformed"
    assert evidence.returncode == 0
    assert evidence.details == {
        "check": "overlay_digest",
        "failure_branch": "stderr",
        "stdout_bytes": 13,
        "stdout_newlines": 1,
        "stderr_bytes": 30,
        "stderr_newlines": 0,
    }

    record = graph_module._template_event_record(
        "template_boot", "failed", error=raised.value
    )
    failure = record["failure"]
    assert isinstance(failure, dict)
    assert failure["operation"] == "compatibility"
    assert failure["reason"] == "guest_output_malformed"
    assert failure["details"] == evidence.details
    assert "secret stderr" not in repr(record)
    assert relative_path not in repr(record)


def test_guest_verification_suppresses_raw_provider_exception_with_existing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative_path = ".repotrial-overlays/compatibility.overlay.yaml"
    provider = FakeSandboxProvider()

    async def fail_exec(
        _sandbox_id: str, _argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        error = RuntimeError("guest secret output")
        attach_sandbox_failure_evidence(
            error,
            SandboxFailureEvidence(operation="sandbox", reason="exec_failed"),
        )
        raise error

    monkeypatch.setattr(provider, "exec", fail_exec)
    sandbox_id = asyncio.run(provider.create(tmp_path, "guest-verify"))

    with pytest.raises(
        CompatibilityError, match="compatibility planning failed: guest_verify_failed"
    ) as raised:
        _run_verifier(provider, sandbox_id, relative_path, "a" * 64)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.operation == "compatibility"
    assert evidence.reason == "guest_verify_failed"
    assert evidence.details["failure_branch"] == "command_failed"
    assert evidence.details["stdout_bytes"] is None
    assert evidence.details["stdout_newlines"] is None
    assert evidence.details["stderr_bytes"] is None
    assert evidence.details["stderr_newlines"] is None
    assert "guest secret output" not in "".join(
        traceback.format_exception(raised.value)
    )


@pytest.mark.parametrize(
    ("result", "expected_branch", "expected_reason"),
    [
        (cast(ExecResult, object()), "result_type", "guest_output_malformed"),
        (
            ExecResult.model_construct(exit_code="not-an-int", stdout="", stderr=""),
            "exit_field",
            "guest_output_malformed",
        ),
        (
            ExecResult.model_construct(exit_code=0, stdout=object(), stderr=""),
            "output_oversize",
            "guest_output_oversize",
        ),
        (
            ExecResult.model_construct(exit_code=0, stdout="", stderr=object()),
            "output_oversize",
            "guest_output_oversize",
        ),
    ],
)
def test_guest_verification_evidence_handles_untrusted_result_shapes(
    tmp_path: Path,
    result: ExecResult,
    expected_branch: str,
    expected_reason: str,
) -> None:
    relative_path = ".repotrial-overlays/compatibility.overlay.yaml"
    provider = FakeSandboxProvider(
        scripts={
            ("sha256sum", "--", relative_path): result,
        }
    )
    sandbox_id = asyncio.run(provider.create(tmp_path, "guest-verify"))

    with pytest.raises(CompatibilityError) as raised:
        _run_verifier(provider, sandbox_id, relative_path, "a" * 64)

    evidence = get_sandbox_failure_evidence(raised.value)
    assert evidence is not None
    assert evidence.reason == expected_reason
    assert evidence.details["failure_branch"] == expected_branch
    if expected_branch == "result_type":
        assert evidence.details["stdout_bytes"] is None
        assert evidence.details["stdout_newlines"] is None
        assert evidence.details["stderr_bytes"] is None
        assert evidence.details["stderr_newlines"] is None
    elif expected_branch == "exit_field":
        assert evidence.details["stdout_bytes"] == 0
        assert evidence.details["stdout_newlines"] == 0
        assert evidence.details["stderr_bytes"] == 0
        assert evidence.details["stderr_newlines"] == 0
    elif result.stdout is not None and not isinstance(result.stdout, str):
        assert evidence.details["stdout_bytes"] is None
        assert evidence.details["stdout_newlines"] is None
    else:
        assert evidence.details["stderr_bytes"] is None
        assert evidence.details["stderr_newlines"] is None
