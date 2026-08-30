import json
import os
from pathlib import Path

import pytest

from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.trial.journey_artifact import (
    JourneyArtifactError,
    verify_baseline_journeys,
    write_baseline_journeys,
    write_or_verify_baseline_journeys,
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


def test_baseline_journey_write_or_verify_accepts_identical_reentry_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline-journeys.json"
    journeys = _journeys()

    first_hash = write_or_verify_baseline_journeys(path, journeys)
    second_hash = write_or_verify_baseline_journeys(path, _journeys())

    assert first_hash == second_hash
    changed = _journeys()
    changed[0].name = "Changed"
    with pytest.raises(JourneyArtifactError, match="hash mismatch"):
        write_or_verify_baseline_journeys(path, changed)


def test_baseline_journey_write_or_verify_rejects_incomplete_reentry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline-journeys.json"
    incomplete = b'{"journeys":['
    path.write_bytes(incomplete)

    with pytest.raises(JourneyArtifactError, match="invalid"):
        write_or_verify_baseline_journeys(path, _journeys())

    assert path.read_bytes() == incomplete


def test_baseline_journey_publication_is_atomic_and_never_replaces_racing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "baseline-journeys.json"
    real_link = os.link

    def racing_link(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        Path(destination).write_bytes(b"racing-writer")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("repotrial.trial.journey_artifact.os.link", racing_link)

    with pytest.raises(JourneyArtifactError, match="already in use"):
        write_baseline_journeys(path, _journeys())

    assert path.read_bytes() == b"racing-writer"


def test_baseline_journey_short_writes_are_completed_and_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    real_fsync = os.fsync
    writes = 0
    fsyncs = 0

    def short_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        return real_write(descriptor, payload[:11])

    def record_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr("repotrial.trial.journey_artifact.os.write", short_write)
    monkeypatch.setattr("repotrial.trial.journey_artifact.os.fsync", record_fsync)

    write_baseline_journeys(tmp_path / "baseline-journeys.json", _journeys())

    assert writes > 1
    assert fsyncs == 1


def test_baseline_journey_read_validates_the_open_descriptor_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "baseline-journeys.json"
    write_baseline_journeys(path, _journeys())
    decoy = tmp_path / "decoy.json"
    decoy.write_bytes(path.read_bytes())
    real_open = os.open

    def open_decoy(
        path_value: str | bytes | Path, flags: int, mode: int = 0o777
    ) -> int:
        if flags & os.O_RDONLY == os.O_RDONLY and not flags & os.O_WRONLY:
            return real_open(decoy, flags, mode)
        return real_open(path_value, flags, mode)

    monkeypatch.setattr("repotrial.trial.journey_artifact.os.open", open_decoy)

    with pytest.raises(JourneyArtifactError, match="target changed"):
        verify_baseline_journeys(path, _journeys())


def test_baseline_journey_read_is_bounded_to_limit_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repotrial.trial import journey_artifact as artifact_module

    path = tmp_path / "baseline-journeys.json"
    path.write_bytes(b"x" * (artifact_module._MAX_ARTIFACT_BYTES + 10_000))
    real_read = os.read
    requested: list[int] = []

    def bounded_read(descriptor: int, count: int) -> bytes:
        requested.append(count)
        return real_read(descriptor, count)

    monkeypatch.setattr("repotrial.trial.journey_artifact.os.read", bounded_read)

    with pytest.raises(JourneyArtifactError, match="byte budget"):
        verify_baseline_journeys(path, _journeys())

    assert requested
    assert sum(requested) <= artifact_module._MAX_ARTIFACT_BYTES + 1


def test_baseline_journey_segmented_short_reads_do_not_hide_trailing_junk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "baseline-journeys.json"
    write_baseline_journeys(path, _journeys())
    canonical = path.read_bytes()
    path.write_bytes(canonical + b"trailing-junk")
    chunks = iter((canonical, b"trailing-junk", b""))

    def segmented_read(descriptor: int, count: int) -> bytes:
        del descriptor
        chunk = next(chunks)
        assert len(chunk) <= count
        return chunk

    monkeypatch.setattr("repotrial.trial.journey_artifact.os.read", segmented_read)

    with pytest.raises(JourneyArtifactError, match="invalid"):
        verify_baseline_journeys(path, _journeys())


def test_baseline_journey_symlink_is_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    path = tmp_path / "baseline-journeys.json"
    path.symlink_to(outside)

    with pytest.raises(JourneyArtifactError, match="regular file"):
        verify_baseline_journeys(path, _journeys())

    assert outside.read_bytes() == b"outside"
