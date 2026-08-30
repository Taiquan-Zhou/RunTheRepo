import asyncio
import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider
from repotrial.trial import observation_evidence as evidence_module
from repotrial.trial import observer as observer_module
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
        ("audit", "start"),
        ("audit", "success"),
    ]
    assert [row["sequence"] for row in rows] == list(range(1, 13))
    assert {row["phase"] for row in rows[:10:2]} == {"provider_execution"}
    assert {row["reason"] for row in rows[:10:2]} == {"collector_started"}
    assert {row["phase"] for row in rows[1:10:2]} == {"parsing"}
    assert {row["reason"] for row in rows[1:10:2]} == {"collector_succeeded"}
    assert (rows[-2]["phase"], rows[-2]["reason"]) == (
        "serialization",
        "audit_started",
    )
    assert (rows[-1]["phase"], rows[-1]["reason"]) == (
        "atomic_persistence",
        "audit_persisted",
    )
    assert [row["exit_code"] for row in rows] == [
        None,
        0,
        None,
        0,
        None,
        0,
        None,
        0,
        None,
        None,
        None,
        None,
    ]


def test_maximum_container_run_records_bounded_390_event_ledger(
    tmp_path: Path,
) -> None:
    containers = [(f"service-{index}", f"{index + 1:012x}") for index in range(64)]
    discovery = "\n".join(
        json.dumps({"Service": service, "ID": container_id})
        for service, container_id in containers
    )
    scripts: dict[tuple[str, ...], object] = {DISCOVERY_ARGV: _result(discovery)}
    for _, container_id in containers:
        scripts[("docker", "inspect", container_id)] = _result(
            json.dumps([{"Id": container_id, "Config": {"Env": []}}])
        )
        scripts[("docker", "diff", container_id)] = _result()
        scripts[
            (
                "docker",
                "top",
                container_id,
                "-eo",
                "pid=,ppid=,user=,comm=",
            )
        ] = _result()
    evidence = tmp_path / "observer-boundary.jsonl"

    _collect(_Provider(scripts), tmp_path / "observation.json", evidence)

    rows = _rows(evidence)
    assert len(rows) == 390
    assert [row["sequence"] for row in rows] == list(range(1, 391))
    assert Counter(row["operation"] for row in rows) == {
        "discovery": 2,
        "inspect": 128,
        "diff": 128,
        "top": 128,
        "network": 2,
        "audit": 2,
    }


@pytest.mark.parametrize(
    ("failure", "error_type", "message", "reason"),
    [
        (
            "serialization",
            ObservationParseError,
            "audit data is not serializable",
            "audit_serialization_failed",
        ),
        (
            "oversize",
            ObservationCollectionError,
            "audit artifact exceeds size limit",
            "audit_too_large",
        ),
    ],
)
def test_audit_serialization_failures_receive_audit_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    error_type: type[Exception],
    message: str,
    reason: str,
) -> None:
    if failure == "serialization":
        monkeypatch.setattr(
            observer_module,
            "_command_audit",
            lambda argv, stdout, parsed: {"unserializable": object()},
        )
    else:
        monkeypatch.setattr(observer_module, "_MAX_ARTIFACT_BYTES", 1)
    evidence = tmp_path / "observer-boundary.jsonl"

    with pytest.raises(error_type, match=f"^{message}$"):
        _collect(
            _Provider({DISCOVERY_ARGV: _result("")}),
            tmp_path / "observation.json",
            evidence,
        )

    rows = _rows(evidence)
    assert [(row["operation"], row["outcome"]) for row in rows[-2:]] == [
        ("audit", "start"),
        ("audit", "failure"),
    ]
    assert rows[-2]["phase"] == "serialization"
    assert rows[-1]["phase"] == "serialization"
    assert rows[-1]["reason"] == reason


class _ArtifactCollisionProvider(_Provider):
    def __init__(
        self, scripts: Mapping[tuple[str, ...], object], artifact: Path
    ) -> None:
        super().__init__(scripts)
        self._artifact = artifact

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        result = await super().network_log(sandbox_id)
        self._artifact.write_text("sentinel\n", encoding="utf-8")
        return result


@pytest.mark.parametrize("failure", ["collision", "write"])
def test_audit_persistence_failures_receive_audit_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    artifact = tmp_path / "observation.json"
    evidence = tmp_path / "observer-boundary.jsonl"
    provider: SandboxProvider = _Provider({DISCOVERY_ARGV: _result("")})
    if failure == "collision":
        provider = _ArtifactCollisionProvider({DISCOVERY_ARGV: _result("")}, artifact)
        expected_message = "artifact target is already in use"
        expected_reason = "audit_destination_collision"
    else:
        real_open = Path.open

        def fail_artifact_open(
            path: Path, mode: str = "r", *args: object, **kwargs: object
        ) -> object:
            if path == artifact:
                raise OSError("raw audit write secret")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_artifact_open)
        expected_message = "audit artifact could not be written"
        expected_reason = "audit_persistence_failed"

    with pytest.raises(ObservationCollectionError, match=f"^{expected_message}$"):
        _collect(provider, artifact, evidence)

    terminal = _terminal(evidence)
    assert terminal["operation"] == "audit"
    assert terminal["phase"] == "atomic_persistence"
    assert terminal["outcome"] == "failure"
    assert terminal["reason"] == expected_reason


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
    ("discovery", "error", "phase", "reason", "exit_code"),
    [
        (
            _result("stdout-secret", exit_code=17, stderr="stderr-secret"),
            ObservationCollectionError,
            "result_validation",
            "nonzero_exit",
            17,
        ),
        (
            object(),
            ObservationParseError,
            "result_validation",
            "malformed_exec_result",
            None,
        ),
        (
            ExecResult.model_construct(
                exit_code=True, stdout="stdout-secret", stderr="stderr-secret"
            ),
            ObservationParseError,
            "result_validation",
            "malformed_exec_result",
            None,
        ),
        (
            _result("not-json-secret"),
            ObservationParseError,
            "parsing",
            "parse_failure",
            0,
        ),
        (
            _result("x" * 1_048_577),
            ObservationParseError,
            "parsing",
            "resource_limit_exceeded",
            0,
        ),
    ],
)
def test_exec_failure_taxonomy_is_closed_and_stable(
    tmp_path: Path,
    discovery: object,
    error: type[Exception],
    phase: str,
    reason: str,
    exit_code: int | None,
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
    assert terminal["exit_code"] == exit_code


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
    terminal = [row for row in _rows(evidence) if row["operation"] == "network"][-1]
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

    def racing_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        is_destination = (dir_fd is not None and Path(path) == Path(evidence.name)) or (
            dir_fd is None and Path(path) == evidence
        )
        if is_destination and not raced:
            raced = True
            decoy_flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            )
            decoy = (
                real_open(path, decoy_flags, 0o600)
                if dir_fd is None
                else real_open(path, decoy_flags, 0o600, dir_fd=dir_fd)
            )
            os.write(decoy, b"sentinel\n")
            real_close(decoy)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    if os.name == "nt":
        real_windows_open = evidence_module._open_windows_destination

        def racing_windows_open(name: str, parent_handle: int) -> int:
            nonlocal raced
            if not raced:
                raced = True
                evidence.write_bytes(b"sentinel\n")
            return real_windows_open(name, parent_handle)

        monkeypatch.setattr(
            evidence_module, "_open_windows_destination", racing_windows_open
        )
    else:
        monkeypatch.setattr(evidence_module.os, "open", racing_open)

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, tmp_path / "observation.json", evidence)

    assert raised.value.reason == "destination_collision"
    assert evidence.read_bytes() == b"sentinel\n"
    assert provider.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory descriptors required")
def test_parent_symlink_swap_cannot_create_escaped_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    displaced = tmp_path / "displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    artifact = attempt / "observation.json"
    evidence = attempt / "observer-boundary.jsonl"
    provider = _Provider(_scripts())
    real_open = os.open
    swapped = False

    def swap_parent_before_destination_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        is_destination = (dir_fd is not None and Path(path) == Path(evidence.name)) or (
            dir_fd is None and Path(path) == evidence
        )
        if is_destination and not swapped:
            swapped = True
            attempt.rename(displaced)
            attempt.symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(evidence_module.os, "open", swap_parent_before_destination_open)

    with pytest.raises(ObservationEvidenceError) as raised:
        _collect(provider, artifact, evidence)

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "parent_invalid"
    assert not (outside / evidence.name).exists()
    assert provider.calls == []


def test_windows_parent_guard_fault_fails_closed_before_destination_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened_destinations: list[Path] = []
    real_open = os.open

    def fail_directory_handle(path: Path) -> int:
        del path
        raise OSError("raw Windows handle secret")

    def record_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        opened_destinations.append(Path(path))
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(evidence_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        evidence_module, "_open_windows_directory_handle", fail_directory_handle
    )
    monkeypatch.setattr(evidence_module.os, "open", record_open)

    with pytest.raises(ObservationEvidenceError) as raised:
        ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "parent_invalid"
    assert opened_destinations == []


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handle required")
def test_windows_parent_handle_relative_create_cannot_escape_replaced_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    displaced = tmp_path / "displaced"
    evidence = attempt / "observer-boundary.jsonl"
    real_open_destination = evidence_module._open_windows_destination
    swapped = False

    def swap_parent_before_relative_create(name: str, parent_handle: int) -> int:
        nonlocal swapped
        attempt.rename(displaced)
        attempt.mkdir()
        swapped = True
        return real_open_destination(name, parent_handle)

    monkeypatch.setattr(
        evidence_module,
        "_open_windows_destination",
        swap_parent_before_relative_create,
    )

    with pytest.raises(ObservationEvidenceError) as raised:
        ObservationEvidenceRecorder(evidence)

    assert raised.value.reason == "parent_invalid"
    assert swapped
    assert attempt.is_dir()
    assert not evidence.exists()
    assert (displaced / evidence.name).is_file()


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
    try:
        evidence.symlink_to(outside)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
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
    windows_opens = 0
    real_windows_open = evidence_module._open_windows_destination

    def record_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal opens
        opens += 1
        opened_flags.append(flags)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def short_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        return real_write(descriptor, payload[:7])

    def record_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(descriptor)

    def record_windows_open(name: str, parent_handle: int) -> int:
        nonlocal windows_opens
        windows_opens += 1
        return real_windows_open(name, parent_handle)

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    monkeypatch.setattr(evidence_module.os, "write", short_write)
    monkeypatch.setattr(evidence_module.os, "fsync", record_fsync)
    if os.name == "nt":
        monkeypatch.setattr(
            evidence_module, "_open_windows_destination", record_windows_open
        )
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

    if os.name == "nt":
        assert opens == 0
        assert windows_opens == 1
    else:
        assert opens == 2
        destination_flags = opened_flags[-1]
        assert destination_flags & os.O_APPEND
        assert destination_flags & os.O_CREAT
        assert destination_flags & os.O_EXCL
        if nofollow := getattr(os, "O_NOFOLLOW", 0):
            assert destination_flags & nofollow
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


def test_partial_write_failure_retains_incomplete_final_line_and_never_reuses_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "observer-boundary.jsonl"
    recorder = ObservationEvidenceRecorder(evidence)
    recorder.record_start("discovery", list(DISCOVERY_ARGV))
    first_line = evidence.read_bytes()
    real_write = os.write
    writes = 0

    def partial_then_fail(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, payload[:7])
        raise OSError("raw persistence secret")

    monkeypatch.setattr(evidence_module.os, "write", partial_then_fail)

    with pytest.raises(ObservationEvidenceError) as raised:
        recorder.record_terminal(
            "discovery",
            list(DISCOVERY_ARGV),
            phase="parsing",
            outcome="success",
            reason="collector_succeeded",
            result=_result("ok"),
        )

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "persistence_failed"
    retained = evidence.read_bytes()
    assert retained.startswith(first_line)
    assert first_line.endswith(b"\n")
    assert not retained.endswith(b"\n")
    complete, incomplete = retained.split(b"\n", maxsplit=1)
    assert json.loads(complete)["outcome"] == "start"
    with pytest.raises(json.JSONDecodeError):
        json.loads(incomplete)
    with pytest.raises(ObservationEvidenceError) as collision:
        ObservationEvidenceRecorder(evidence)
    assert collision.value.reason == "destination_collision"


def test_persistence_close_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")
    close_calls = 0

    def fail_write(descriptor: int, payload: bytes) -> int:
        del descriptor, payload
        raise OSError("raw write secret")

    def uncertain_close(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        del descriptor
        raise OSError("raw close secret")

    monkeypatch.setattr(evidence_module.os, "write", fail_write)
    monkeypatch.setattr(evidence_module.os, "close", uncertain_close)

    with pytest.raises(ObservationEvidenceError) as raised:
        recorder.record_start("discovery", list(DISCOVERY_ARGV))

    assert raised.value.reason == "persistence_failed"
    assert getattr(raised.value, "__notes__", ()) == [
        "secondary observation evidence close failed; descriptor ownership is uncertain"
    ]
    recorder.close()
    assert close_calls == 1


def test_constructor_cleanup_never_retries_uncertain_destination_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_fstat = os.fstat
    real_close = os.close
    real_handle_to_descriptor = evidence_module._windows_handle_to_descriptor
    destination_descriptor: int | None = None
    close_calls = 0

    def record_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal destination_descriptor
        descriptor = (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )
        if flags & os.O_CREAT:
            destination_descriptor = descriptor
        return descriptor

    def invalidate_destination(descriptor: int) -> object:
        if descriptor == destination_descriptor:
            return SimpleNamespace(st_mode=stat.S_IFDIR)
        return real_fstat(descriptor)

    def record_handle_to_descriptor(handle: int) -> int:
        nonlocal destination_descriptor
        descriptor = real_handle_to_descriptor(handle)
        destination_descriptor = descriptor
        return descriptor

    def uncertain_destination_close(descriptor: int) -> None:
        nonlocal close_calls
        if descriptor == destination_descriptor:
            close_calls += 1
            raise OSError("raw close secret")
        real_close(descriptor)

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    monkeypatch.setattr(evidence_module.os, "fstat", invalidate_destination)
    monkeypatch.setattr(evidence_module.os, "close", uncertain_destination_close)
    if os.name == "nt":
        monkeypatch.setattr(
            evidence_module,
            "_windows_handle_to_descriptor",
            record_handle_to_descriptor,
        )
    try:
        with pytest.raises(ObservationEvidenceError) as raised:
            ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")
    finally:
        if destination_descriptor is not None:
            monkeypatch.setattr(evidence_module.os, "close", real_close)
            try:
                real_fstat(destination_descriptor)
            except OSError:
                pass
            else:
                real_close(destination_descriptor)

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "persistence_failed"
    assert getattr(raised.value, "__notes__", ()) == [
        "secondary observation evidence close failed; descriptor ownership is uncertain"
    ]
    assert close_calls == 1


def test_cancellation_and_secondary_close_failure_preserve_same_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = asyncio.CancelledError("raw cancellation secret")
    provider = _Provider({DISCOVERY_ARGV: cancellation})
    real_open = os.open
    real_close = os.close
    real_handle_to_descriptor = evidence_module._windows_handle_to_descriptor
    opened: list[int] = []
    destination_descriptors: set[int] = set()

    def record_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )
        opened.append(descriptor)
        if flags & os.O_CREAT:
            destination_descriptors.add(descriptor)
        return descriptor

    def fail_close(descriptor: int) -> None:
        if descriptor in destination_descriptors:
            raise OSError("raw close secret")
        real_close(descriptor)

    def record_handle_to_descriptor(handle: int) -> int:
        descriptor = real_handle_to_descriptor(handle)
        opened.append(descriptor)
        destination_descriptors.add(descriptor)
        return descriptor

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    monkeypatch.setattr(evidence_module.os, "close", fail_close)
    if os.name == "nt":
        monkeypatch.setattr(
            evidence_module,
            "_windows_handle_to_descriptor",
            record_handle_to_descriptor,
        )
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


def test_close_failure_relinquishes_descriptor_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_close = os.close
    real_handle_to_descriptor = evidence_module._windows_handle_to_descriptor
    opened: list[int] = []

    def record_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )
        opened.append(descriptor)
        return descriptor

    close_calls = 0

    def fail_close(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        del descriptor
        raise OSError("raw close secret")

    def record_handle_to_descriptor(handle: int) -> int:
        descriptor = real_handle_to_descriptor(handle)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(evidence_module.os, "open", record_open)
    if os.name == "nt":
        monkeypatch.setattr(
            evidence_module,
            "_windows_handle_to_descriptor",
            record_handle_to_descriptor,
        )
    recorder = ObservationEvidenceRecorder(tmp_path / "observer-boundary.jsonl")
    monkeypatch.setattr(evidence_module.os, "close", fail_close)

    with pytest.raises(ObservationEvidenceError) as raised:
        recorder.close()

    assert raised.value.phase == "atomic_persistence"
    assert raised.value.reason == "close_failed"
    assert "secret" not in str(raised.value)
    recorder.close()
    recorder.close()
    assert close_calls == 1

    monkeypatch.setattr(evidence_module.os, "close", real_close)
    for descriptor in opened:
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        real_close(descriptor)


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
