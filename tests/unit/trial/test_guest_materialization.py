import asyncio
import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest

from repotrial.compose.compatibility import CompatibilityError
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.compatibility import (
    materialize_guest_compatibility_overlay,
    materialize_guest_experiment_overlay,
    verify_guest_experiment_overlay,
)

_RELATIVE_PATH = ".repotrial-overlays/compatibility.overlay.yaml"
_EXPERIMENT_RELATIVE_PATH = ".repotrial-overlays/experiment.overlay.yaml"


class RecordingProvider(SandboxProvider):
    def __init__(self, result: ExecResult | None = None) -> None:
        self.result = result or ExecResult(
            exit_code=0,
            stdout=(
                f"root=/workspace\npath={_RELATIVE_PATH}\nmode=600\nsha256={'a' * 64}\n"
            ),
            stderr="",
        )
        self.calls: list[tuple[str, object, ...]] = []

    async def create(self, workspace: Path, name: str) -> str:
        del workspace, name
        raise AssertionError("materializer must not create a sandbox")

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self.calls.append((sandbox_id, tuple(argv), timeout_s))
        return self.result

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        del sandbox_id, container_port
        raise AssertionError("materializer must not publish a port")

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        del sandbox_id, remote_path, local_path
        raise AssertionError("materializer must not copy files")

    async def network_log(self, sandbox_id: str):
        del sandbox_id
        raise AssertionError("materializer must not collect network logs")

    async def destroy(self, sandbox_id: str) -> None:
        del sandbox_id
        raise AssertionError("materializer must not destroy a sandbox")


class LocalAdapterProvider(RecordingProvider):
    def __init__(self, cwd: Path) -> None:
        super().__init__()
        self.cwd = cwd

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self.calls.append((sandbox_id, tuple(argv), timeout_s))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def _materialize(
    provider: SandboxProvider,
    artifact: Path,
    evidence: Path,
    expected_sha256: str,
) -> None:
    asyncio.run(
        materialize_guest_compatibility_overlay(
            provider,
            "sandbox-1",
            host_artifact_path=artifact,
            relative_path=_RELATIVE_PATH,
            expected_sha256=expected_sha256,
            evidence_path=evidence,
        )
    )


def test_materializes_bounded_host_artifact_with_controlled_argv_and_identity_evidence(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "compatibility.overlay.yaml"
    payload = b"services: {}\n"
    artifact.write_bytes(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    provider = RecordingProvider(
        ExecResult(
            exit_code=0,
            stdout=(
                "root=/workspace\n"
                f"path={_RELATIVE_PATH}\n"
                "mode=600\n"
                f"sha256={expected_sha256}\n"
            ),
            stderr="",
        )
    )
    evidence = tmp_path / "compatibility-materialization.jsonl"

    _materialize(provider, artifact, evidence, expected_sha256)

    assert len(provider.calls) == 1
    sandbox_id, argv, timeout_s = provider.calls[0]
    assert sandbox_id == "sandbox-1"
    assert argv[:4] == ("sh", "-eu", "-c", argv[3])
    assert argv[4] == "repotrial-compatibility-overlay"
    assert argv[5:7] == (_RELATIVE_PATH, expected_sha256)
    assert argv[7] == "c2VydmljZXM6IHt9Cg=="
    assert timeout_s == 30

    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert [row["outcome"] for row in rows] == ["start", "terminal"]
    assert all(row["adapter_sha256"] for row in rows)
    assert all(row["artifact_relative_path"] == _RELATIVE_PATH for row in rows)
    assert all(row["artifact_sha256"] == expected_sha256 for row in rows)
    assert rows[1]["reason"] == "materialized"
    assert "c2VydmljZXM6" not in evidence.read_text()
    assert "services: {}" not in evidence.read_text()


def test_experiment_materializer_uses_fixed_guest_path_and_purpose(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "candidate.overlay.yaml"
    payload = b"services: {}\n"
    artifact.write_bytes(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    provider = RecordingProvider(
        ExecResult(
            exit_code=0,
            stdout=(
                "root=/workspace\n"
                f"path={_EXPERIMENT_RELATIVE_PATH}\n"
                "mode=600\n"
                f"sha256={expected_sha256}\n"
            ),
            stderr="",
        )
    )
    evidence = tmp_path / "experiment-materialization.jsonl"

    asyncio.run(
        materialize_guest_experiment_overlay(
            provider,
            "sandbox-1",
            host_artifact_path=artifact,
            expected_sha256=expected_sha256,
            evidence_path=evidence,
        )
    )

    _, argv, timeout_s = provider.calls[0]
    assert argv[4] == "repotrial-experiment-overlay"
    assert argv[5] == _EXPERIMENT_RELATIVE_PATH
    assert timeout_s == 30
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert all(row["purpose"] == "experiment_overlay_materialization" for row in rows)
    assert all(
        row["artifact_relative_path"] == _EXPERIMENT_RELATIVE_PATH for row in rows
    )


def test_experiment_verifier_cannot_be_redirected_to_compatibility_path() -> None:
    expected_sha256 = "a" * 64
    provider = FakeSandboxProvider(
        scripts={
            ("sha256sum", "--", _EXPERIMENT_RELATIVE_PATH): ExecResult(
                exit_code=0,
                stdout=f"{expected_sha256}  {_EXPERIMENT_RELATIVE_PATH}\n",
                stderr="",
            )
        }
    )
    sandbox_id = asyncio.run(provider.create(Path("/tmp"), "guest-verify"))

    asyncio.run(
        verify_guest_experiment_overlay(
            provider, sandbox_id, expected_sha256=expected_sha256
        )
    )

    assert provider.calls[-1] == (
        "exec",
        sandbox_id,
        ("sha256sum", "--", _EXPERIMENT_RELATIVE_PATH),
        30,
    )


def test_host_hash_mismatch_fails_before_provider_transport(tmp_path: Path) -> None:
    artifact = tmp_path / "compatibility.overlay.yaml"
    artifact.write_bytes(b"different\n")
    provider = RecordingProvider()
    evidence = tmp_path / "compatibility-materialization.jsonl"

    with pytest.raises(CompatibilityError, match="artifact_hash_mismatch"):
        _materialize(provider, artifact, evidence, "a" * 64)

    assert provider.calls == []
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert rows[-1]["reason"] == "artifact_hash_mismatch"


@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (
            ExecResult(exit_code=0, stdout="malformed\n", stderr=""),
            "guest_output_malformed",
        ),
        (
            ExecResult(exit_code=1, stdout="", stderr="provider failure"),
            "guest_materialization_failed",
        ),
        (
            ExecResult(exit_code=0, stdout="x" * 2048, stderr=""),
            "guest_output_oversize",
        ),
    ],
)
def test_guest_materialization_fails_closed_and_records_terminal_reason(
    tmp_path: Path, result: ExecResult, expected_reason: str
) -> None:
    artifact = tmp_path / "compatibility.overlay.yaml"
    payload = b"services: {}\n"
    artifact.write_bytes(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    provider = RecordingProvider(result)
    evidence = tmp_path / "compatibility-materialization.jsonl"

    with pytest.raises(CompatibilityError, match=expected_reason):
        _materialize(provider, artifact, evidence, expected_sha256)

    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert rows[-1]["outcome"] == "terminal"
    assert rows[-1]["reason"] == expected_reason


def test_oversize_host_artifact_fails_without_transport(tmp_path: Path) -> None:
    artifact = tmp_path / "compatibility.overlay.yaml"
    artifact.write_bytes(b"x" * (1_048_576 + 1))
    provider = RecordingProvider()
    evidence = tmp_path / "compatibility-materialization.jsonl"

    with pytest.raises(CompatibilityError, match="artifact_oversize"):
        _materialize(provider, artifact, evidence, "a" * 64)

    assert provider.calls == []


def test_local_adapter_materializes_exact_bytes_with_mode_and_hash(
    tmp_path: Path,
) -> None:
    guest_root = tmp_path / "guest"
    guest_root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(guest_root)], check=True)
    artifact = tmp_path / "host.overlay.yaml"
    payload = b"services:\n  web:\n    ports: []\n"
    artifact.write_bytes(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    evidence = tmp_path / "compatibility-materialization.jsonl"
    provider = LocalAdapterProvider(guest_root)

    _materialize(provider, artifact, evidence, expected_sha256)

    target = guest_root / _RELATIVE_PATH
    assert target.read_bytes() == payload
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha256
    assert evidence.read_text().count("services:") == 0
    assert json.loads(evidence.read_text().splitlines()[-1])["reason"] == "materialized"


def test_local_adapter_no_clobber_preserves_existing_guest_file(
    tmp_path: Path,
) -> None:
    guest_root = tmp_path / "guest"
    guest_root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(guest_root)], check=True)
    target = guest_root / _RELATIVE_PATH
    target.parent.mkdir()
    existing = b"existing guest bytes\n"
    target.write_bytes(existing)
    artifact = tmp_path / "host.overlay.yaml"
    payload = b"services: {}\n"
    artifact.write_bytes(payload)
    evidence = tmp_path / "compatibility-materialization.jsonl"
    provider = LocalAdapterProvider(guest_root)

    with pytest.raises(CompatibilityError, match="guest_materialization_failed"):
        _materialize(provider, artifact, evidence, hashlib.sha256(payload).hexdigest())

    assert target.read_bytes() == existing
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert rows[-1]["reason"] == "guest_materialization_failed"


@pytest.mark.parametrize("root_kind", ["non_git", "nested_git"])
def test_local_adapter_rejects_non_git_or_non_root_working_directory(
    tmp_path: Path, root_kind: str
) -> None:
    guest_root = tmp_path / "guest"
    guest_root.mkdir()
    if root_kind == "nested_git":
        subprocess.run(["git", "init", "--quiet", str(guest_root)], check=True)
        cwd = guest_root / "nested"
        cwd.mkdir()
    else:
        cwd = guest_root
    artifact = tmp_path / "host.overlay.yaml"
    payload = b"services: {}\n"
    artifact.write_bytes(payload)
    evidence = tmp_path / "compatibility-materialization.jsonl"
    provider = LocalAdapterProvider(cwd)

    with pytest.raises(CompatibilityError, match="guest_materialization_failed"):
        _materialize(provider, artifact, evidence, hashlib.sha256(payload).hexdigest())

    assert not (guest_root / _RELATIVE_PATH).exists()


def test_materializer_keeps_fixed_relative_path_validation(tmp_path: Path) -> None:
    artifact = tmp_path / "host.overlay.yaml"
    payload = b"services: {}\n"
    artifact.write_bytes(payload)
    evidence = tmp_path / "compatibility-materialization.jsonl"
    provider = RecordingProvider()

    with pytest.raises(CompatibilityError, match="artifact_path_invalid"):
        asyncio.run(
            materialize_guest_compatibility_overlay(
                provider,
                "sandbox-1",
                host_artifact_path=artifact,
                relative_path="compatibility.overlay.yaml",
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                evidence_path=evidence,
            )
        )

    assert provider.calls == []
