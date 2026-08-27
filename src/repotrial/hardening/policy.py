import hashlib
import re
from collections.abc import Sequence

from repotrial.domain.enums import ExperimentVerdict, Verdict
from repotrial.domain.models import JourneyResult

type ExperimentDecision = tuple[ExperimentVerdict, str]

_SAFE_REASON_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def decide_candidate(
    boot: Verdict,
    journeys: Sequence[JourneyResult],
    *,
    expected_journeys: int,
) -> ExperimentDecision | None:
    """Return a deterministic terminal decision, or ``None`` while replay continues."""
    if boot is Verdict.FAIL:
        return ExperimentVerdict.ROLLBACK, "boot_regression"
    if boot is Verdict.UNSUPPORTED:
        return ExperimentVerdict.STOP, "boot_unsupported"
    if expected_journeys == 0:
        return ExperimentVerdict.STOP, "insufficient_coverage"

    for result in journeys:
        token = _reason_id(result.journey_id)
        if result.verdict is Verdict.FAIL:
            return ExperimentVerdict.ROLLBACK, f"journey_regression:{token}"
        if result.verdict is Verdict.UNSUPPORTED:
            return ExperimentVerdict.STOP, f"journey_unsupported:{token}"
    if len(journeys) == expected_journeys:
        return ExperimentVerdict.KEEP, "baseline_pass_journeys_preserved"
    return None


def _reason_id(journey_id: str) -> str:
    if _SAFE_REASON_ID.fullmatch(journey_id) is not None:
        return journey_id
    digest = hashlib.sha256(journey_id.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256-{digest[:16]}"
