import hashlib
import os
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from repotrial.compose.mutations import MutationError, apply_mutation
from repotrial.compose.overlay import write_overlay
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import ExperimentVerdict, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    Journey,
    JourneyResult,
    Mutation,
    ObservationSnapshot,
    RunState,
)
from repotrial.hardening.policy import ExperimentDecision, decide_candidate
from repotrial.journey.http_runner import run_http_journey
from repotrial.journey.playwright_runner import run_playwright_journey
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import DockerSbxError
from repotrial.sandbox.lifecycle import CleanupError, managed_sandbox
from repotrial.trial.boot import _validated_env_prefix, boot_compose
from repotrial.trial.observer import (
    _collect_observation_with_evidence,
    collect_observation,
)
from repotrial.trial.startup_inputs import (
    _ADAPTER_SHA256,
    StartupInputPlan,
    StartupInputUnsupported,
    materialize_startup_input,
    plan_startup_input,
    record_startup_input_rejection,
    verify_startup_input_attempt_history,
    verify_startup_input_identity,
)

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_ORDINARY_STAGE_ERRORS = (RuntimeError, OSError, ValueError, TypeError, KeyError)


class _ExperimentSandboxFailure(RuntimeError):
    def __init__(
        self,
        public_reason: str,
        *,
        boot: Verdict,
        journeys: tuple[JourneyResult, ...] = (),
        after: ObservationSnapshot | None = None,
    ) -> None:
        super().__init__(public_reason)
        self.public_reason = public_reason
        self.boot = boot
        self.journeys = journeys
        self.after = after


@dataclass(frozen=True, slots=True)
class ExperimentContext:
    workspace: Path
    overlay_path: Path
    artifact_dir: Path
    env: Mapping[str, str]
    container_port: int
    prior_attempt_directories: tuple[Path, ...] = ()
    startup_input_identity_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _PreparedExperiment:
    compose_path: str
    overlay_path: Path
    overlay_relative: str
    env: dict[str, str]
    parent_hash: str
    candidate_hash: str
    base: dict[str, object]
    candidate: dict[str, object]
    journeys: tuple[Journey, ...]
    baseline_error: str | None
    lifecycle_artifact: Path
    observation_artifact: Path
    observation_evidence: Path
    startup_input_evidence: Path
    startup_input_plan: StartupInputPlan | None
    startup_input_error: str | None
    evidence_dirs: tuple[Path, ...]
    sandbox_name: str


async def run_experiment(
    state: RunState,
    mutation: Mutation,
    provider: SandboxProvider,
    *,
    context: ExperimentContext,
) -> ExperimentRecord:
    prepared = _prepare(state, mutation, context)
    if prepared.baseline_error is not None:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.UNSUPPORTED,
            verdict=ExperimentVerdict.STOP,
            reason=prepared.baseline_error,
        )

    recorded_parent = state.current_config_hash
    if recorded_parent is None:
        recorded_parent = state.baseline_config_hash
    if recorded_parent is not None and recorded_parent != prepared.parent_hash:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.UNSUPPORTED,
            verdict=ExperimentVerdict.STOP,
            reason="parent_hash_mismatch",
        )

    if prepared.startup_input_error is not None:
        record_startup_input_rejection(
            prepared.startup_input_evidence,
            compose_relative_path=prepared.compose_path,
            reason=prepared.startup_input_error,
        )
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.UNSUPPORTED,
            verdict=ExperimentVerdict.STOP,
            reason="startup_input_unsupported",
        )

    _validate_artifact_targets(prepared)
    try:
        write_overlay(prepared.base, prepared.candidate, prepared.overlay_path)
    except MutationError:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.UNSUPPORTED,
            verdict=ExperimentVerdict.STOP,
            reason="overlay_write_failed",
        )

    try:
        async with managed_sandbox(
            provider,
            context.workspace,
            prepared.sandbox_name,
            lifecycle_artifact=prepared.lifecycle_artifact,
        ) as sandbox_id:
            return await _run_candidate(
                state,
                mutation,
                provider,
                context,
                prepared,
                sandbox_id,
            )
    except CleanupError:
        raise
    except _ExperimentSandboxFailure as failure:
        return _record(
            state,
            mutation,
            prepared,
            boot=failure.boot,
            journeys=failure.journeys,
            after=failure.after,
            verdict=ExperimentVerdict.STOP,
            reason=failure.public_reason,
        )
    except _ORDINARY_STAGE_ERRORS:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.UNSUPPORTED,
            verdict=ExperimentVerdict.STOP,
            reason="sandbox_failed",
        )


def _prepare(
    state: RunState, mutation: Mutation, context: ExperimentContext
) -> _PreparedExperiment:
    workspace = _real_directory(context.workspace, "workspace")
    compose_path, compose_file = _compose_file(workspace, state.compose_path)
    overlay_path, overlay_relative = _overlay_target(workspace, context.overlay_path)
    artifact_dir = _real_directory(context.artifact_dir, "artifact_dir")
    if artifact_dir.is_relative_to(workspace):
        raise ValueError("artifact_dir must resolve outside workspace")
    if type(context.container_port) is not int:
        raise TypeError("container_port must be an integer")
    if not 1 <= context.container_port <= 65_535:
        raise ValueError("container_port is outside the valid range")
    env = _snapshot_env(context.env)
    _validated_env_prefix(compose_path, env)

    base = load_compose(compose_file)
    candidate = apply_mutation(base, mutation)
    parent_hash = _compose_hash(base)
    candidate_hash = _compose_hash(candidate)
    startup_input_plan: StartupInputPlan | None = None
    startup_input_error: str | None = None
    try:
        startup_input_plan = plan_startup_input(workspace, compose_path)
        if startup_input_plan is not None:
            if f"sha256:{startup_input_plan.compose_config_hash}" != parent_hash:
                raise StartupInputUnsupported("compose_identity_mismatch")
            if context.startup_input_identity_path is None:
                raise StartupInputUnsupported("startup_identity_missing")
            verify_startup_input_identity(
                context.startup_input_identity_path,
                startup_input_plan,
                adapter_sha256=_ADAPTER_SHA256,
            )
            verify_startup_input_attempt_history(
                context.prior_attempt_directories, startup_input_plan
            )
        elif context.startup_input_identity_path is not None and (
            context.startup_input_identity_path.exists()
            or context.startup_input_identity_path.is_symlink()
        ):
            raise StartupInputUnsupported("startup_identity_missing_plan")
    except StartupInputUnsupported as error:
        startup_input_error = error.reason
    journeys, baseline_error = _select_baseline_journeys(state)
    token = candidate_hash.removeprefix("sha256:")[:16]
    return _PreparedExperiment(
        compose_path=compose_path,
        overlay_path=overlay_path,
        overlay_relative=overlay_relative,
        env=env,
        parent_hash=parent_hash,
        candidate_hash=candidate_hash,
        base=base,
        candidate=candidate,
        journeys=journeys,
        baseline_error=baseline_error,
        lifecycle_artifact=artifact_dir / f"candidate-{token}-lifecycle.jsonl",
        observation_artifact=artifact_dir / f"candidate-{token}-observation.json",
        observation_evidence=(artifact_dir / "candidate-observation-boundary.jsonl"),
        startup_input_evidence=artifact_dir / "startup-input-attempt.jsonl",
        startup_input_plan=startup_input_plan,
        startup_input_error=startup_input_error,
        evidence_dirs=tuple(
            artifact_dir / f"candidate-{token}-journey-{index:04d}"
            for index in range(len(journeys))
        ),
        sandbox_name=f"repotrial-candidate-{token}",
    )


def _select_baseline_journeys(
    state: RunState,
) -> tuple[tuple[Journey, ...], str | None]:
    result_ids = [result.journey_id for result in state.baseline_journey_results]
    if len(result_ids) != len(set(result_ids)):
        return (), "invalid_baseline:duplicate_result_id"
    journey_ids = [journey.journey_id for journey in state.journeys]
    if len(journey_ids) != len(set(journey_ids)):
        return (), "invalid_baseline:duplicate_journey_id"

    definitions = {journey.journey_id: journey for journey in state.journeys}
    selected: list[Journey] = []
    for result in state.baseline_journey_results:
        if result.verdict is not Verdict.PASS:
            continue
        journey = definitions.get(result.journey_id)
        if journey is None:
            return (), "invalid_baseline:missing_or_ambiguous_journey"
        selected.append(journey)
    if not selected:
        return (), "insufficient_coverage"
    return tuple(selected), None


def _real_directory(path: object, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"{label} must be an existing real directory") from None
    if not stat.S_ISDIR(path_stat.st_mode) or _is_link(path, path_stat):
        raise ValueError(f"{label} must be an existing real directory")
    return resolved


def _compose_file(workspace: Path, value: object) -> tuple[str, Path]:
    if not isinstance(value, str):
        raise TypeError("compose_path must be a string")
    if not value or _contains_controls(value) or "\\" in value:
        raise ValueError("compose_path must be a safe relative path")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("compose_path must be a safe relative path")
    target = workspace
    try:
        target_stat = target.lstat()
        for component in relative.parts:
            target /= component
            target_stat = target.lstat()
            if _is_link(target, target_stat):
                raise ValueError("compose_path must name an existing regular file")
        resolved = target.resolve(strict=True)
    except OSError:
        raise ValueError("compose_path must name an existing regular file") from None
    if not resolved.is_relative_to(workspace) or not stat.S_ISREG(target_stat.st_mode):
        raise ValueError("compose_path must name an existing regular file")
    return value, resolved


def _overlay_target(workspace: Path, value: object) -> tuple[Path, str]:
    if not isinstance(value, Path):
        raise TypeError("overlay_path must be a Path")
    target = value if value.is_absolute() else workspace / value
    if target.exists() or target.is_symlink() or _contains_controls(target.name):
        raise ValueError("overlay_path must be unused")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError:
        raise ValueError("overlay_path parent must exist") from None
    if not parent.is_dir() or not parent.is_relative_to(workspace):
        raise ValueError("overlay_path parent must resolve inside workspace")
    normalized = parent / target.name
    if not normalized.is_relative_to(workspace):
        raise ValueError("overlay_path must resolve inside workspace")
    return normalized, normalized.relative_to(workspace).as_posix()


def _snapshot_env(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("env must be a mapping")
    snapshot: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError("environment keys and values must be strings")
        snapshot[key] = item
    return snapshot


def _compose_hash(compose: dict[str, object]) -> str:
    material = canonical_compose_json(compose).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _validate_artifact_targets(prepared: _PreparedExperiment) -> None:
    targets = [
        prepared.lifecycle_artifact,
        prepared.observation_artifact,
        *prepared.evidence_dirs,
    ]
    if prepared.startup_input_plan is not None:
        targets.extend([prepared.startup_input_evidence, prepared.observation_evidence])
    for target in targets:
        if target.exists() or target.is_symlink():
            raise ValueError("experiment artifact target is already in use")


def _is_link(path: Path, path_stat: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _contains_controls(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


async def _run_candidate(
    state: RunState,
    mutation: Mutation,
    provider: SandboxProvider,
    context: ExperimentContext,
    prepared: _PreparedExperiment,
    sandbox_id: str,
) -> ExperimentRecord:
    startup_plan = prepared.startup_input_plan
    if startup_plan is not None:
        try:
            await materialize_startup_input(
                provider,
                sandbox_id,
                startup_plan,
                compose_path=prepared.compose_path,
                compose_env=prepared.env,
                evidence_path=prepared.startup_input_evidence,
                overlay_path=prepared.overlay_relative,
            )
        except CleanupError:
            raise
        except _ORDINARY_STAGE_ERRORS:
            return _record(
                state,
                mutation,
                prepared,
                boot=Verdict.UNSUPPORTED,
                verdict=ExperimentVerdict.STOP,
                reason="startup_input_unsupported",
            )
    try:
        if startup_plan is None:
            boot = await boot_compose(
                provider,
                sandbox_id,
                prepared.compose_path,
                prepared.env,
                attempt=1,
                overlay_path=prepared.overlay_relative,
            )
        else:
            boot = await boot_compose(
                provider,
                sandbox_id,
                prepared.compose_path,
                prepared.env,
                attempt=1,
                overlay_path=prepared.overlay_relative,
                unset_env_keys=startup_plan.all_source_key_names,
                project_directory=".",
            )
    except DockerSbxError as error:
        raise _ExperimentSandboxFailure(
            "boot_failed",
            boot=Verdict.UNSUPPORTED,
        ) from error
    except CleanupError:
        raise
    except _ORDINARY_STAGE_ERRORS:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.UNSUPPORTED,
            verdict=ExperimentVerdict.STOP,
            reason="boot_failed",
        )
    decision = decide_candidate(
        boot.verdict, (), expected_journeys=len(prepared.journeys)
    )
    if decision is not None:
        return _record_decision(
            state, mutation, prepared, boot.verdict, (), None, decision
        )

    try:
        if startup_plan is None:
            after = await collect_observation(
                provider,
                sandbox_id,
                prepared.compose_path,
                prepared.observation_artifact,
                overlay_path=prepared.overlay_relative,
            )
        else:
            after = await _collect_observation_with_evidence(
                provider,
                sandbox_id,
                prepared.compose_path,
                prepared.observation_artifact,
                overlay_path=prepared.overlay_relative,
                evidence_path=prepared.observation_evidence,
                env=prepared.env,
                unset_env_keys=startup_plan.all_source_key_names,
                project_directory=".",
            )
    except CleanupError:
        raise
    except DockerSbxError as error:
        raise _ExperimentSandboxFailure(
            "observation_failed",
            boot=boot.verdict,
        ) from error
    except _ORDINARY_STAGE_ERRORS:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.PASS,
            verdict=ExperimentVerdict.STOP,
            reason="observation_failed",
        )

    try:
        host_port = await provider.publish_port(sandbox_id, context.container_port)
    except CleanupError:
        raise
    except DockerSbxError as error:
        raise _ExperimentSandboxFailure(
            "publish_failed",
            boot=boot.verdict,
            after=after,
        ) from error
    except _ORDINARY_STAGE_ERRORS:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.PASS,
            after=after,
            verdict=ExperimentVerdict.STOP,
            reason="publish_failed",
        )
    if type(host_port) is not int or not 1 <= host_port <= 65_535:
        return _record(
            state,
            mutation,
            prepared,
            boot=Verdict.PASS,
            after=after,
            verdict=ExperimentVerdict.STOP,
            reason="publish_failed",
        )

    base_url = f"http://127.0.0.1:{host_port}"
    replayed: list[JourneyResult] = []
    for journey, evidence_dir in zip(
        prepared.journeys, prepared.evidence_dirs, strict=True
    ):
        try:
            result = await _replay_journey(journey, base_url, evidence_dir)
        except CleanupError:
            raise
        except _ORDINARY_STAGE_ERRORS:
            return _record(
                state,
                mutation,
                prepared,
                boot=Verdict.PASS,
                journeys=replayed,
                after=after,
                verdict=ExperimentVerdict.STOP,
                reason="journey_failed",
            )
        replayed.append(result)
        decision = decide_candidate(
            Verdict.PASS,
            replayed,
            expected_journeys=len(prepared.journeys),
        )
        if decision is not None:
            return _record_decision(
                state,
                mutation,
                prepared,
                Verdict.PASS,
                replayed,
                after,
                decision,
            )
    raise AssertionError("non-empty replay must produce a terminal decision")


async def _replay_journey(
    journey: Journey, base_url: str, evidence_dir: Path
) -> JourneyResult:
    tools = {step.tool for step in journey.steps}
    if tools == {"http"}:
        return await run_http_journey(
            journey, base_url=base_url, evidence_dir=evidence_dir
        )
    if tools == {"browser"}:
        return await run_playwright_journey(
            journey, base_url=base_url, evidence_dir=evidence_dir
        )
    return JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.UNSUPPORTED,
        passed_steps=0,
        total_steps=len(journey.steps),
        failure_reason="journey:unsupported_tool_mix",
    )


def _record_decision(
    state: RunState,
    mutation: Mutation,
    prepared: _PreparedExperiment,
    boot: Verdict,
    journeys: Sequence[JourneyResult],
    after: ObservationSnapshot | None,
    decision: ExperimentDecision,
) -> ExperimentRecord:
    verdict, reason = decision
    return _record(
        state,
        mutation,
        prepared,
        boot=boot,
        journeys=journeys,
        after=after,
        verdict=verdict,
        reason=reason,
    )


def _record(
    state: RunState,
    mutation: Mutation,
    prepared: _PreparedExperiment,
    *,
    boot: Verdict,
    verdict: ExperimentVerdict,
    reason: str,
    journeys: Sequence[JourneyResult] = (),
    after: ObservationSnapshot | None = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=mutation.mutation_id,
        parent_config_hash=prepared.parent_hash,
        candidate_config_hash=prepared.candidate_hash,
        mutation=mutation,
        boot=boot,
        journeys=list(journeys),
        before=state.baseline_observation,
        after=after,
        verdict=verdict,
        reason=reason,
    )
