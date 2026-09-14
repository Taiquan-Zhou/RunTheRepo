"""Bounded, evidence-backed projection of a running trial phase."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Final, Literal

Phase = Literal[
    "preparing_repository",
    "preparing_environment",
    "starting_application",
    "checking_application",
    "collecting_observations",
    "hardening",
    "finalizing",
]

PHASES: Final[tuple[Phase, ...]] = (
    "preparing_repository",
    "preparing_environment",
    "starting_application",
    "checking_application",
    "collecting_observations",
    "hardening",
    "finalizing",
)
_ATTEMPT = re.compile(
    r"^(baseline|experiment)-([0-9a-f]{16})-([0-9]{4})-attempt-[0-9]{2}$"
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_FILE = 256 * 1024
_MAX_ATTEMPTS = 64
_MAX_EVENTS = 256


def project_progress(
    workspace: Path, run_id: str, *, now: float | None = None
) -> dict[str, object]:
    """Return a small status projection from only this run's trusted artifacts."""
    result: dict[str, object] = {
        "phase": "preparing_repository",
        "completed_phases": [],
    }
    if not _valid_run_id(run_id):
        return result

    run_path = workspace / "artifacts" / run_id
    evidence_root = run_path / "evidence"
    if not _real_dir(run_path) or not _real_dir(evidence_root):
        return result

    run_token = hashlib.sha256(run_id.encode("utf-8", errors="replace")).hexdigest()[
        :16
    ]
    attempts: list[_Attempt] = []
    for entry in _scandir(evidence_root):
        if len(attempts) >= _MAX_ATTEMPTS:
            break
        if not entry.is_dir(follow_symlinks=False):
            continue
        match = _ATTEMPT.fullmatch(entry.name)
        if match is None or match.group(2) != run_token:
            continue
        attempt = _read_attempt(Path(entry.path), match, run_token)
        if attempt is not None:
            attempts.append(attempt)

    for attempt in attempts:
        if attempt.purpose == "baseline":
            attempt.journey_complete = _baseline_journey_complete(
                run_path, run_token, attempt
            )

    if not attempts:
        return result

    events = [event for attempt in attempts for event in attempt.events]
    events.sort(key=lambda item: (item.mtime, item.order))
    latest = events[-1] if events else None
    baseline = [attempt for attempt in attempts if attempt.purpose == "baseline"]
    experiments = [attempt for attempt in attempts if attempt.purpose == "experiment"]
    completed: set[Phase] = set()

    if any(_baseline_plan_valid(run_path, run_token) for _ in baseline):
        completed.add("preparing_repository")
    if any(attempt.create_success for attempt in baseline):
        completed.add("preparing_environment")
    if any(attempt.boot_pass for attempt in baseline):
        completed.add("starting_application")
    if any(attempt.journey_complete for attempt in baseline):
        completed.add("checking_application")
    if any(attempt.observation for attempt in baseline):
        completed.add("collecting_observations")

    terminal = _matching_terminal_mtime(run_path, run_id)
    terminal_mtime = terminal[0] if terminal is not None else None
    terminal_completed = terminal[1] if terminal is not None else False
    if terminal_completed:
        completed.add("finalizing")

    if terminal_completed:
        phase: Phase = "finalizing"
    elif experiments:
        phase = "hardening"
    elif baseline:
        current = baseline[-1]
        if current.observation or current.journey_complete:
            phase = "collecting_observations"
        elif current.boot_pass:
            phase = "checking_application"
        elif current.boot_or_step or current.create_success:
            phase = "starting_application"
        else:
            phase = "preparing_environment"
    else:
        phase = "preparing_environment"

    result["phase"] = phase
    result["completed_phases"] = [name for name in PHASES if name in completed]
    if experiments:
        result["experiment_index"] = max(attempt.index for attempt in experiments) + 1
    if latest is not None:
        result["latest_event"] = latest.event
        if latest.operation is not None:
            result["latest_operation"] = latest.operation

    evidence_mtime = max(
        [attempt.mtime for attempt in attempts]
        + [event.mtime for event in events]
        + ([terminal_mtime] if terminal_mtime is not None else [])
    )
    current_time = time.time() if now is None else now
    result["evidence_age_seconds"] = round(max(0.0, current_time - evidence_mtime), 3)
    return result


class _Event:
    __slots__ = ("event", "mtime", "operation", "order")

    def __init__(
        self, event: str, operation: str | None, mtime: float, order: int
    ) -> None:
        self.event = event
        self.operation = operation
        self.mtime = mtime
        self.order = order


class _Attempt:
    __slots__ = (
        "boot_or_step",
        "boot_pass",
        "create_attempt",
        "create_success",
        "destroy_success",
        "events",
        "index",
        "journey_complete",
        "mtime",
        "observation",
        "purpose",
        "step_paths",
    )

    def __init__(self, purpose: str, index: int, mtime: float) -> None:
        self.purpose = purpose
        self.index = index
        self.mtime = mtime
        self.events: list[_Event] = []
        self.create_attempt = False
        self.create_success = False
        self.destroy_success = False
        self.boot_or_step = False
        self.boot_pass = False
        self.journey_complete = False
        self.step_paths: list[Path] = []
        self.observation = False


def _read_attempt(path: Path, match: re.Match[str], run_token: str) -> _Attempt | None:
    marker = path / ".repotrial-attempt.json"
    marker_bytes, marker_mtime = _read_regular(marker, 8 * 1024)
    if marker_bytes is None or marker_mtime is None:
        return None
    try:
        marker_value = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(marker_value, dict):
        return None
    purpose = marker_value.get("purpose")
    index = marker_value.get("index")
    if (
        purpose != match.group(1)
        or marker_value.get("run_token") != run_token
        or type(index) is not int
        or index != int(match.group(3))
        or index < 0
    ):
        return None

    attempt = _Attempt(purpose, index, marker_mtime)
    for entry in _scandir(path):
        if entry.name == ".repotrial-attempt.json":
            continue
        if entry.is_symlink():
            continue
        if entry.is_file(follow_symlinks=False):
            file_path = Path(entry.path)
            file_mtime = _mtime(file_path)
            if file_mtime is None:
                continue
            attempt.mtime = max(attempt.mtime, file_mtime)
            if entry.name.endswith("-lifecycle.jsonl"):
                _read_lifecycle(
                    file_path,
                    file_mtime,
                    attempt,
                    warmup=entry.name.startswith("warmup-"),
                )
            elif entry.name.endswith("-boot-attempt.json"):
                attempt.boot_or_step = True
                boot_value, _ = _read_json(file_path, _MAX_FILE)
                if (
                    isinstance(boot_value, dict)
                    and isinstance(boot_value.get("final"), dict)
                    and boot_value["final"].get("verdict") == "pass"
                ):
                    attempt.boot_pass = True
            elif "observation" in entry.name:
                attempt.observation = True
        elif entry.is_dir(follow_symlinks=False) and "-journey-" in entry.name:
            for step in _scandir(Path(entry.path)):
                if (
                    step.is_file(follow_symlinks=False)
                    and step.name.startswith("step-")
                    and step.name.endswith(".json")
                ):
                    step_mtime = _mtime(Path(step.path))
                    if step_mtime is not None:
                        attempt.mtime = max(attempt.mtime, step_mtime)
                        attempt.boot_or_step = True
                        attempt.step_paths.append(Path(step.path))
    return attempt


def _baseline_plan_valid(run_path: Path, run_token: str) -> bool:
    plan_path = run_path / "evidence" / f"run-{run_token}" / "baseline-journeys.json"
    plan, _ = _read_json(plan_path, _MAX_FILE)
    return isinstance(plan, dict) and isinstance(plan.get("journeys"), list)


def _baseline_journey_complete(
    run_path: Path, run_token: str, attempt: _Attempt
) -> bool:
    plan_path = run_path / "evidence" / f"run-{run_token}" / "baseline-journeys.json"
    plan, _ = _read_json(plan_path, _MAX_FILE)
    if not isinstance(plan, dict) or not isinstance(plan.get("journeys"), list):
        return False
    planned = 0
    for journey in plan["journeys"]:
        if isinstance(journey, dict) and isinstance(journey.get("steps"), list):
            planned += len(journey["steps"])
    if planned <= 0 or len(attempt.step_paths) != planned:
        return False
    for step_path in attempt.step_paths:
        value, _ = _read_json(step_path, _MAX_FILE)
        if not isinstance(value, dict) or value.get("failure_category") is not None:
            return False
        assertions = value.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            return False
        if not all(
            isinstance(assertion, dict) and assertion.get("outcome") == "passed"
            for assertion in assertions
        ):
            return False
    return True


def _read_lifecycle(
    path: Path, mtime: float, attempt: _Attempt, *, warmup: bool = False
) -> None:
    data, _ = _read_regular(path, _MAX_FILE)
    if data is None:
        return
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return
    for order, line in enumerate(lines[:_MAX_EVENTS]):
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        event = value.get("event")
        if not isinstance(event, str) or not _SAFE_VALUE.fullmatch(event):
            continue
        operation = value.get("operation")
        if not isinstance(operation, str):
            failure = value.get("failure")
            operation = failure.get("operation") if isinstance(failure, dict) else None
        if not isinstance(operation, str) or not _SAFE_VALUE.fullmatch(operation):
            operation = {
                "create_attempt": "create",
                "create_success": "create",
                "destroy_attempt": "destroy",
                "destroy_success": "destroy",
            }.get(event)
        attempt.events.append(_Event(event, operation, mtime, order))
        if not warmup:
            attempt.create_attempt |= event == "create_attempt"
            attempt.create_success |= event == "create_success"
        attempt.destroy_success |= event == "destroy_success"


def _matching_terminal_mtime(run_path: Path, run_id: str) -> tuple[float, bool] | None:
    evidence_path = run_path / "attempt-result.json"
    report_path = run_path / "report" / "trial-report.json"
    evidence, evidence_mtime = _read_json(evidence_path, _MAX_FILE)
    report, report_mtime = _read_json(report_path, _MAX_FILE)
    if (
        not isinstance(evidence, dict)
        or evidence.get("run_id") != run_id
        or not isinstance(report, dict)
    ):
        return None
    identity = report.get("identity")
    if not isinstance(identity, dict) or identity.get("run_id") != run_id:
        return None
    if evidence_mtime is None or report_mtime is None:
        return None
    completed = (
        evidence.get("terminal_outcome") == "completed"
        and evidence.get("exit_code") == 0
    )
    return max(evidence_mtime, report_mtime), completed


def _read_json(path: Path, max_bytes: int) -> tuple[object | None, float | None]:
    data, mtime = _read_regular(path, max_bytes)
    if data is None:
        return None, None
    try:
        return json.loads(data.decode("utf-8")), mtime
    except (UnicodeDecodeError, ValueError):
        return None, None


def _read_regular(path: Path, max_bytes: int) -> tuple[bytes | None, float | None]:
    if max_bytes <= 0 or not _safe_ancestors(path):
        return None, None
    descriptors: list[int] = []
    try:
        absolute = path.absolute()
        if absolute.anchor != "/":
            return None, None
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in absolute.parts[1:-1]:
            current = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            descriptors.append(current)
        descriptor = os.open(
            absolute.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current,
        )
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None, None
        data = os.read(descriptor, max_bytes + 1)
        if len(data) > max_bytes:
            return None, None
        return data, info.st_mtime
    except OSError:
        return None, None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _scandir(path: Path) -> list[os.DirEntry[str]]:
    if not _real_dir(path):
        return []
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) >= _MAX_ATTEMPTS:
                    break
    except OSError:
        return []
    return entries


def _real_dir(path: Path) -> bool:
    if not _safe_ancestors(path):
        return False
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _safe_ancestors(path: Path) -> bool:
    try:
        absolute = path.absolute()
        if absolute.anchor != "/":
            return False
        current = absolute
        while True:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return False
            if current == Path("/"):
                return True
            current = current.parent
    except OSError:
        return False


def _mtime(path: Path) -> float | None:
    if not _safe_ancestors(path):
        return None
    try:
        return path.lstat().st_mtime if not path.is_symlink() else None
    except OSError:
        return None


def _valid_run_id(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value.lower()
