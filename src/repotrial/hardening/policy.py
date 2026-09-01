import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import ExperimentRecord, JourneyResult, Mutation, RunState

type ExperimentDecision = tuple[ExperimentVerdict, str]

_SAFE_REASON_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_MUTATION_PRIORITY = (
    MutationType.DROP_PRIVILEGED,
    MutationType.REMOVE_DOCKER_SOCKET,
    MutationType.SET_NON_ROOT,
    MutationType.DROP_ALL_CAPS,
    MutationType.SET_READ_ONLY,
    MutationType.BRIDGE_NETWORK,
)
_MUTATION_BY_FINDING_KIND = {
    "privileged": MutationType.DROP_PRIVILEGED,
    "docker_socket_rw": MutationType.REMOVE_DOCKER_SOCKET,
    "root_user": MutationType.SET_NON_ROOT,
    "root_user_possible": MutationType.DROP_ALL_CAPS,
    "cap_add": MutationType.DROP_ALL_CAPS,
    "writable_rootfs": MutationType.SET_READ_ONLY,
    "host_network": MutationType.BRIDGE_NETWORK,
}


@dataclass(frozen=True, slots=True)
class MutationPolicyDecision:
    mutation: Mutation | None
    stop_reason: str | None


def propose_mutation(state: RunState) -> MutationPolicyDecision:
    """Propose one bounded hardening mutation without changing ``state``."""
    if any(record.verdict is ExperimentVerdict.STOP for record in state.experiments):
        return _stopped("experiment_stopped")
    if not any(
        result.verdict is Verdict.PASS for result in state.baseline_journey_results
    ):
        return _stopped("insufficient_coverage")
    if len(state.experiments) >= 8:
        return _stopped("experiment_budget_exhausted")
    if len(state.experiments) >= 3 and all(
        record.verdict is ExperimentVerdict.ROLLBACK
        for record in state.experiments[-3:]
    ):
        return _stopped("consecutive_failures")

    follow_up = _follow_up_mutation(state)
    if follow_up is not None:
        return MutationPolicyDecision(mutation=follow_up, stop_reason=None)

    attempted = {
        (record.mutation.type, record.mutation.service) for record in state.experiments
    }
    candidates: dict[tuple[MutationType, str], int] = {}
    for finding in state.risk_findings:
        mutation_type = _MUTATION_BY_FINDING_KIND.get(finding.kind)
        if mutation_type is None:
            continue
        identity = mutation_type, finding.service
        if identity not in attempted:
            candidates[identity] = max(candidates.get(identity, -1), finding.severity)

    if not candidates:
        return _stopped("no_remaining_mutations")

    priority = {
        mutation_type: index for index, mutation_type in enumerate(_MUTATION_PRIORITY)
    }
    (mutation_type, service), _ = min(
        candidates.items(),
        key=lambda item: (priority[item[0][0]], -item[1], item[0][1]),
    )
    return MutationPolicyDecision(
        mutation=Mutation(
            mutation_id=f"policy:{mutation_type.value}:{service}",
            type=mutation_type,
            service=service,
        ),
        stop_reason=None,
    )


def _stopped(reason: str) -> MutationPolicyDecision:
    return MutationPolicyDecision(mutation=None, stop_reason=reason)


def _follow_up_mutation(state: RunState) -> Mutation | None:
    if _can_add_tmpfs(state):
        service = state.experiments[-1].mutation.service
        return Mutation(
            mutation_id=f"policy:add_tmpfs:{service}:after-read-only",
            type=MutationType.ADD_TMPFS,
            service=service,
        )
    if _can_retry_read_only(state):
        service = state.experiments[-1].mutation.service
        return Mutation(
            mutation_id=f"policy:set_read_only:{service}:after-tmpfs",
            type=MutationType.SET_READ_ONLY,
            service=service,
        )
    return None


def _can_add_tmpfs(state: RunState) -> bool:
    if not state.experiments:
        return False
    latest = state.experiments[-1]
    service = latest.mutation.service
    return (
        latest.mutation.type is MutationType.SET_READ_ONLY
        and latest.verdict is ExperimentVerdict.ROLLBACK
        and _has_tmp_change(latest, service)
        and _effective_config_hash(state) == latest.parent_config_hash
        and not _was_attempted(state.experiments, MutationType.ADD_TMPFS, service)
    )


def _can_retry_read_only(state: RunState) -> bool:
    if len(state.experiments) < 2:
        return False
    read_only, tmpfs = state.experiments[-2:]
    service = read_only.mutation.service
    return (
        read_only.mutation.type is MutationType.SET_READ_ONLY
        and read_only.verdict is ExperimentVerdict.ROLLBACK
        and tmpfs.mutation.type is MutationType.ADD_TMPFS
        and tmpfs.mutation.service == service
        and tmpfs.verdict is ExperimentVerdict.KEEP
        and tmpfs.mutation.mutation_id == f"policy:add_tmpfs:{service}:after-read-only"
        and tmpfs.parent_config_hash == read_only.parent_config_hash
        and _effective_config_hash(state) == tmpfs.candidate_config_hash
        and _has_tmp_change(read_only, service)
        and not any(
            record.mutation.type is MutationType.SET_READ_ONLY
            and record.mutation.service == service
            and record.parent_config_hash == tmpfs.candidate_config_hash
            for record in state.experiments
        )
    )


def _effective_config_hash(state: RunState) -> str | None:
    return state.current_config_hash or state.baseline_config_hash


def _was_attempted(
    experiments: Sequence[ExperimentRecord], mutation_type: MutationType, service: str
) -> bool:
    return any(
        record.mutation.type is mutation_type and record.mutation.service == service
        for record in experiments
    )


def _has_tmp_change(record: ExperimentRecord, service: str) -> bool:
    for observation in (record.before, record.after):
        if observation is None:
            continue
        for change in observation.file_changes:
            path = change.get("path")
            if (
                change.get("service") == service
                and isinstance(path, str)
                and (path == "/tmp" or path.startswith("/tmp/"))
            ):
                return True
    return False


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
