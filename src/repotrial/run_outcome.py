"""Shared deterministic terminal-outcome classification for CLI and API."""

from dataclasses import dataclass
from enum import StrEnum

from repotrial.domain.enums import ExperimentVerdict, Verdict
from repotrial.domain.models import RunState


class TerminalOutcome(StrEnum):
    COMPLETED = "completed"
    EXECUTION_UNSUPPORTED = "execution_unsupported"
    TRIAL_FAILED = "trial_failed"


@dataclass(frozen=True, slots=True)
class ClassifiedOutcome:
    outcome: TerminalOutcome
    exit_code: int


def classify_terminal_outcome(run: RunState) -> ClassifiedOutcome:
    if run.stop_reason == "boot_unsupported" or any(
        record.boot is Verdict.UNSUPPORTED for record in run.experiments
    ):
        return ClassifiedOutcome(TerminalOutcome.EXECUTION_UNSUPPORTED, 2)
    if any(
        result.verdict is Verdict.UNSUPPORTED for result in run.baseline_journey_results
    ) or any(
        result.verdict is Verdict.UNSUPPORTED
        for record in run.experiments
        for result in record.journeys
    ):
        return ClassifiedOutcome(TerminalOutcome.EXECUTION_UNSUPPORTED, 2)
    baseline_passed = bool(run.baseline_journey_results) and all(
        result.verdict is Verdict.PASS for result in run.baseline_journey_results
    )
    experiment_stopped = any(
        record.verdict is ExperimentVerdict.STOP for record in run.experiments
    )
    if run.stop_reason is not None and baseline_passed and not experiment_stopped:
        return ClassifiedOutcome(TerminalOutcome.COMPLETED, 0)
    return ClassifiedOutcome(TerminalOutcome.TRIAL_FAILED, 3)
