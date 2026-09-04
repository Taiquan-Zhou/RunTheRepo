import asyncio
import hashlib
from pathlib import Path

import pytest

from repotrial.compose.compatibility import CompatibilityError
from repotrial.sandbox.base import ExecResult
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
        5,
    )


@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (
            ExecResult(exit_code=1, stdout="", stderr="missing"),
            "guest_artifact_missing",
        ),
        (
            ExecResult(exit_code=0, stdout="not-a-digest\n", stderr=""),
            "guest_output_malformed",
        ),
        (
            ExecResult(exit_code=0, stdout="a" * 64 + "  wrong\n", stderr=""),
            "guest_output_malformed",
        ),
        (
            ExecResult(
                exit_code=0,
                stdout="b" * 64 + "  .repotrial-overlays/compatibility.overlay.yaml\n",
                stderr="",
            ),
            "guest_hash_mismatch",
        ),
        (
            ExecResult(exit_code=0, stdout="x" * 2048, stderr=""),
            "guest_output_oversize",
        ),
    ],
)
def test_guest_compatibility_verifier_fails_closed_for_invalid_guest_results(
    tmp_path: Path, result: ExecResult, expected_reason: str
) -> None:
    relative_path = ".repotrial-overlays/compatibility.overlay.yaml"
    expected_sha256 = "a" * 64
    provider = FakeSandboxProvider(scripts={("sha256sum", "--", relative_path): result})
    sandbox_id = asyncio.run(provider.create(tmp_path, "guest-verify"))

    with pytest.raises(
        CompatibilityError, match=f"compatibility planning failed: {expected_reason}"
    ):
        _run_verifier(provider, sandbox_id, relative_path, expected_sha256)
