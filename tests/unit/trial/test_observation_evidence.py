import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider
from repotrial.trial import observation_evidence as evidence_module
from repotrial.trial.observation_evidence import (
    ObservationEvidenceError,
    ObservationEvidenceRecorder,
)
from repotrial.trial.observer import (
    ObservationCollectionError,
    ObservationParseError,
    _collect_observation_with_evidence,
)

CONTAINER_ID = "a" * 12
DISCOVERY_ARGV = (
    "docker",
    "compose",
    "-f",
    "compose-secret.yaml",
    "ps",
    "--all",
    "--no-trunc",
    "--orphans=false",
    "--format",
    "json",
)


class _Provider(SandboxProvider):
    def __init__(
        self,
        scripts: Mapping[tuple[str, ...], object],
        network: object | None = None,
    ) -> None:
        self.scripts = dict(scripts)
        self.network = (
            NetworkLogResult(events=[], supported=True, unsupported_reason=None)
            if network is None
            else network
        )
        self.calls: list[tuple[object, ...]] = []

    async def create(self, workspace: Path, name: str) -> str:
        raise AssertionError("observer must not create a sandbox")

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self.calls.append(("exec", sandbox_id, tuple(argv), timeout_s))
        response = self.scripts[tuple(argv)]
        if isinstance(response, BaseException):
            raise response
        return cast(ExecResult, response)

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        raise AssertionError("observer must not publish a port")

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        raise AssertionError("observer must not copy files")

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        self.calls.append(("network_log", sandbox_id))
        if isinstance(self.network, BaseException):
            raise self.network
        return cast(NetworkLogResult, self.network)

    async def destroy(self, sandbox_id: str) -> None:
        raise AssertionError("observer must not destroy the sandbox")


def _result(stdout: str = "", *, exit_code: int = 0, stderr: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _scripts(*, discovery: object | None = None) -> dict[tuple[str, ...], object]:
    discovery_result = (
        _result(json.dumps({"Service": "api", "ID": CONTAINER_ID}))
        if discovery is None
        else discovery
    )
    return {
        DISCOVERY_ARGV: discovery_result,
        ("docker", "inspect", CONTAINER_ID): _result(
            json.dumps(
                [
                    {
                        "Id": CONTAINER_ID,
                        "Config": {"Env": ["TOKEN=credential-secret"]},
                    }
                ]
            )
        ),
        ("docker", "diff", CONTAINER_ID): _result("A /credential-secret\n"),
        (
            "docker",
            "top",
            CONTAINER_ID,
            "-eo",
            "pid=,ppid=,user=,comm=",
        ): _result("1 0 root credential-secret\n"),
    }


def _collect(provider: SandboxProvider, artifact: Path, evidence: Path) -> object:
    return asyncio.run(
        _collect_observation_with_evidence(
            provider,
            "sandbox-secret-id",
            "compose-secret.yaml",
            artifact,
            evidence_path=evidence,
        )
    )


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def _terminal(path: Path) -> dict[str, object]:
    return _rows(path)[-1]


def test_success_records_two_deterministic_events_per_collector(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"

    _collect(_Provider(_scripts()), tmp_path / "observation.json", evidence)

    rows = _rows(evidence)
    assert [(row["operation"], row["outcome"]) for row in rows] == [
        ("discovery", "start"),
        ("discovery", "success"),
        ("inspect", "start"),
        ("inspect", "success"),
        ("diff", "start"),
        ("diff", "success"),
        ("top", "start"),
        ("top", "success"),
        ("network", "start"),
        ("network", "success"),
    ]
    assert [row["sequence"] for row in rows] == list(range(1, 11))
    assert {row["phase"] for row in rows[::2]} == {"provider_execution"}
    assert {row["reason"] for row in rows[::2]} == {"collector_started"}
    assert {row["phase"] for row in rows[1::2]} == {"parsing"}
    assert {row["reason"] for row in rows[1::2]} == {"collector_succeeded"}


class _SentinelProviderError(RuntimeError):
    pass


@pytest.mark.parametrize("boundary", ["exec", "network"])
def test_provider_exception_is_recorded_safely_and_propagated_unchanged(
    tmp_path: Path, boundary: str
) -> None:
    failure = _SentinelProviderError("credential-secret")
    provider = (
        _Provider({DISCOVERY_ARGV: failure})
        if boundary == "exec"
        else _Provider({DISCOVERY_ARGV: _result("")}, failure)
    )
    evidence = tmp_path / "observer-boundary.jsonl"

    with pytest.raises(_SentinelProviderError) as raised:
        _collect(provider, tmp_path / "observation.json", evidence)

    assert raised.value is failure
    operation = "discovery" if boundary == "exec" else "network"
    operation_rows = [row for row in _rows(evidence) if row["operation"] == operation]
    assert [row["outcome"] for row in operation_rows] == ["start", "failure"]
    terminal = _terminal(evidence)
    assert terminal["phase"] == "provider_execution"
    assert terminal["outcome"] == "failure"
    assert terminal["reason"] == "provider_exception"
    assert terminal["exception_class"] == "_SentinelProviderError"
    assert "credential-secret" not in evidence.read_text("utf-8")


@pytest.mark.parametrize(
    ("discovery", "error", "phase", "reason"),
    [
        (
            _result("stdout-secret", exit_code=17, stderr="stderr-secret"),
            ObservationCollectionError,
            "result_validation",
            "nonzero_exit",
        ),
        (
            object(),
            ObservationParseError,
            "result_validation",
            "malformed_exec_result",
        ),
        (
            _result("not-json-secret"),
            ObservationParseError,
            "parsing",
            "parse_failure",
        ),
        (
            _result("x" * 1_048_577),
            ObservationParseError,
            "parsing",
            "resource_limit_exceeded",
        ),
    ],
)
def test_exec_failure_taxonomy_is_closed_and_stable(
    tmp_path: Path,
    discovery: object,
    error: type[Exception],
    phase: str,
    reason: str,
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"

    with pytest.raises(error):
        _collect(
            _Provider(_scripts(discovery=discovery)),
            tmp_path / "observation.json",
            evidence,
        )

    terminal = _terminal(evidence)
    assert [row["outcome"] for row in _rows(evidence)] == ["start", "failure"]
    assert terminal["phase"] == phase
    assert terminal["outcome"] == "failure"
    assert terminal["reason"] == reason


def test_network_unsupported_is_a_successful_observed_terminal(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"
    provider = _Provider(
        {DISCOVERY_ARGV: _result("")},
        NetworkLogResult(
            events=[], supported=False, unsupported_reason="runtime unavailable"
        ),
    )

    snapshot = _collect(provider, tmp_path / "observation.json", evidence)

    assert snapshot.unsupported_collectors == ["network_runtime"]
    terminal = _terminal(evidence)
    assert terminal["operation"] == "network"
    assert terminal["phase"] == "parsing"
    assert terminal["outcome"] == "success"
    assert terminal["reason"] == "network_unsupported"


def test_malformed_network_result_is_a_result_validation_failure(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"

    with pytest.raises(ObservationParseError):
        _collect(
            _Provider({DISCOVERY_ARGV: _result("")}, object()),
            tmp_path / "observation.json",
            evidence,
        )

    terminal = _terminal(evidence)
    assert terminal["operation"] == "network"
    assert terminal["phase"] == "result_validation"
    assert terminal["outcome"] == "failure"
    assert terminal["reason"] == "malformed_network_result"


def test_metadata_contains_only_bounded_hashes_lengths_and_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_module, "_MAX_HASHED_OUTPUT_BYTES", 8)
    raw_stdout = "stdout-secret"
    raw_stderr = "stderr-secret"
    evidence = tmp_path / "observer-boundary.jsonl"
    provider = _Provider(
        {DISCOVERY_ARGV: _result(raw_stdout, exit_code=1, stderr=raw_stderr)}
    )

    with pytest.raises(ObservationCollectionError):
        _collect(provider, tmp_path / "observation.json", evidence)

    terminal = _terminal(evidence)
    stdout = cast(dict[str, object], terminal["stdout"])
    stderr = cast(dict[str, object], terminal["stderr"])
    assert stdout == {
        "encoded_bytes": len(raw_stdout.encode()),
        "hashed_bytes": 8,
        "sha256": hashlib.sha256(raw_stdout.encode()[:8]).hexdigest(),
        "truncated": True,
    }
    assert stderr == {
        "encoded_bytes": len(raw_stderr.encode()),
        "hashed_bytes": 8,
        "sha256": hashlib.sha256(raw_stderr.encode()[:8]).hexdigest(),
        "truncated": True,
    }
    assert terminal["argv_count"] == len(DISCOVERY_ARGV)
    assert (
        terminal["argv_sha256"]
        == hashlib.sha256(
            json.dumps(
                list(DISCOVERY_ARGV),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    serialized = evidence.read_text("utf-8")
    for raw in (
        raw_stdout,
        raw_stderr,
        "compose-secret.yaml",
        "sandbox-secret-id",
        CONTAINER_ID,
        "credential-secret",
    ):
        assert raw not in serialized


def test_destination_collision_fails_before_provider_work_without_overwrite(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"
    evidence.write_text("sentinel\n", encoding="utf-8")
    provider = _Provider(_scripts())

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, tmp_path / "observation.json", evidence)

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "destination_collision"
    assert evidence.read_text("utf-8") == "sentinel\n"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("destination", "reason"),
    [("audit", "destination_collision"), ("other_parent", "parent_invalid")],
)
def test_diagnostic_destination_must_be_distinct_inside_audit_attempt_directory(
    tmp_path: Path, destination: str, reason: str
) -> None:
    artifact = tmp_path / "observation.json"
    if destination == "audit":
        evidence = artifact
    else:
        other = tmp_path / "other"
        other.mkdir()
        evidence = other / "observer-boundary.jsonl"
    provider = _Provider(_scripts())

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, artifact, evidence)

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == reason
    assert provider.calls == []
    assert not evidence.exists()


def test_destination_creation_race_is_rejected_without_overwrite_or_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"
    provider = _Provider(_scripts())
    real_open = os.open
    real_close = os.close
    raced = False

    def racing_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        nonlocal raced
        if not raced:
            raced = True
            decoy = real_open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.write(decoy, b"sentinel\n")
            real_close(decoy)
        return real_open(path, flags, mode)

    monkeypatch.setattr(evidence_module.os, "open", racing_open)

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, tmp_path / "observation.json", evidence)

    assert raised.value.reason == "destination_collision"
    assert evidence.read_bytes() == b"sentinel\n"
    assert provider.calls == []


def test_terminal_destination_link_or_reparse_is_rejected_before_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"
    provider = _Provider(_scripts())
    real_lstat = Path.lstat

    def reparse_lstat(path: Path) -> object:
        if path == evidence:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    monkeypatch.setattr(evidence_module, "_REPARSE_POINT", 0x400)

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, tmp_path / "observation.json", evidence)

    assert raised.value.reason == "destination_collision"
    assert provider.calls == []


def test_symlink_destination_is_never_followed_or_overwritten(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    evidence = tmp_path / "observer-boundary.jsonl"
    evidence.symlink_to(outside)
    provider = _Provider(_scripts())

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, tmp_path / "observation.json", evidence)

    assert raised.value.reason == "destination_collision"
    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert provider.calls == []


def test_event_serialization_and_size_fail_closed_with_stable_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")
    monkeypatch.setattr(evidence_module, "_MAX_EVENT_BYTES", 1)

    with pytest.raises(ObservationEvidenceError) as oversized:
        recorder.record_start("discovery", list(DISCOVERY_ARGV))

    assert oversized.value.phase == "serialization"
    assert oversized.value.reason == "evidence_too_large"
    recorder.close()

    second = ObservationEvidenceRecorder(tmp_path / "second.jsonl")

    def fail_json(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise TypeError("raw serialization secret")

    monkeypatch.setattr(evidence_module, "_MAX_EVENT_BYTES", 4_096)
    monkeypatch.setattr(evidence_module.json, "dumps", fail_json)
    with pytest.raises(ObservationEvidenceError) as serialization:
        second.record_start("discovery", list(DISCOVERY_ARGV))

    assert serialization.value.phase == "serialization"
    assert serialization.value.reason == "serialization_failed"
    assert "secret" not in str(serialization.value)
    second.close()


def test_short_writes_are_completed_fsynced_and_never_reopen_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_write = os.write
    real_fsync = os.fsync
    opens = 0
    writes = 0
    fsyncs = 0
    opened_flags: list[int] = []

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        nonlocal opens
        opens += 1
        opened_flags.append(flags)
        return real_open(path, flags, mode)

    def short_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        return real_write(descriptor, payload[:7])

    def record_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    monkeypatch.setattr(evidence_module.os, "write", short_write)
    monkeypatch.setattr(evidence_module.os, "fsync", record_fsync)
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")
    recorder.record_start("discovery", list(DISCOVERY_ARGV))
    recorder.record_terminal(
        "discovery",
        list(DISCOVERY_ARGV),
        phase="parsing",
        outcome="success",
        reason="collector_succeeded",
        result=_result("ok", stderr="warning"),
    )
    recorder.close()

    assert opens == 1
    assert opened_flags[0] & os.O_APPEND
    assert opened_flags[0] & os.O_CREAT
    assert opened_flags[0] & os.O_EXCL
    if nofollow := getattr(os, "O_NOFOLLOW", 0):
        assert opened_flags[0] & nofollow
    assert writes > 2
    assert fsyncs == 2


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_persistence_failure_closes_descriptor_and_reports_atomic_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")

    def fail(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise OSError("raw persistence secret")

    monkeypatch.setattr(
        evidence_module.os, "write" if failure == "write" else "fsync", fail
    )

    with pytest.raises(ObservationEvidenceError) as raised:
        recorder.record_start("discovery", list(DISCOVERY_ARGV))

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "persistence_failed"
    assert "secret" not in str(raised.value)
    recorder.close()


def test_cancellation_and_secondary_close_failure_preserve_same_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = asyncio.CancelledError("raw cancellation secret")
    provider = _Provider({DISCOVERY_ARGV: cancellation})
    real_open = os.open
    real_close = os.close
    opened: list[int] = []

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        opened.append(descriptor)
        return descriptor

    def fail_close(descriptor: int) -> None:
        del descriptor
        raise OSError("raw close secret")

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    monkeypatch.setattr(evidence_module.os, "close", fail_close)
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            _collect(
                provider,
                tmp_path / "observation.json",
                tmp_path / "observer-boundary.jsonl",
            )
    finally:
        monkeypatch.setattr(evidence_module.os, "close", real_close)
        for descriptor in opened:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            real_close(descriptor)

    assert raised.value is cancellation
    assert len(getattr(raised.value, "__notes__", ())) == 1


def test_provider_exception_survives_terminal_persistence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = _SentinelProviderError("credential-secret")
    provider = _Provider({DISCOVERY_ARGV: failure})
    real_write = os.write
    writes = 0

    def fail_terminal_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, payload)
        raise OSError("raw persistence secret")

    monkeypatch.setattr(evidence_module.os, "write", fail_terminal_write)

    with pytest.raises(_SentinelProviderError) as raised:
        _collect(
            provider,
            tmp_path / "observation.json",
            tmp_path / "observer-boundary.jsonl",
        )

    assert raised.value is failure
    assert getattr(raised.value, "__notes__", ()) == [
        "secondary observation evidence persistence failed while preserving primary error"
    ]


def test_close_failure_retains_descriptor_for_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_close = os.close
    opened: list[int] = []

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        opened.append(descriptor)
        return descriptor

    def fail_close(descriptor: int) -> None:
        del descriptor
        raise OSError("raw close secret")

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")
    monkeypatch.setattr(evidence_module.os, "close", fail_close)

    with pytest.raises(ObservationEvidenceError) as raised:
        recorder.close()

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "close_failed"
    assert "secret" not in str(raised.value)
    os.fstat(opened[0])

    monkeypatch.setattr(evidence_module.os, "close", real_close)
    recorder.close()
    recorder.close()
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_runtime_taxonomy_rejects_unknown_values(tmp_path: Path) -> None:
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")

    with pytest.raises(ValueError, match="operation"):
        recorder.record_start(cast(object, "unknown"), [])
    with pytest.raises(ValueError, match="operation"):
        recorder.record_start(cast(object, []), [])

    recorder.record_start("discovery", list(DISCOVERY_ARGV))
    with pytest.raises(ValueError, match="phase"):
        recorder.record_terminal(
            "discovery",
            list(DISCOVERY_ARGV),
            phase=cast(object, "unknown"),
            outcome="failure",
            reason="parse_failure",
        )
    recorder.close()
