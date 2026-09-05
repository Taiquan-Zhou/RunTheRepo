import errno
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

if TYPE_CHECKING:
    # langgraph.types exposes this open config at runtime but omits it from __all__.
    type RunnableConfig = dict[str, Any]

else:
    from langgraph.types import RunnableConfig
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from repotrial.agent.checkpoint import build_memory_checkpointer
from repotrial.agent.state import GraphContext, GraphState, StageName
from repotrial.compose.compatibility import (
    CompatibilityArtifact,
    CompatibilityError,
    CompatibilityPlan,
    plan_loopback_compatibility_overlay,
    write_loopback_compatibility_overlay,
)
from repotrial.compose.mutations import apply_mutation
from repotrial.compose.parser import (
    ComposeParseError,
    canonical_compose_json,
    load_compose,
)
from repotrial.compose.risk import analyze_risk
from repotrial.domain.enums import ExperimentVerdict, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    Journey,
    JourneyResult,
    PinnedRepo,
    RunState,
)
from repotrial.hardening.engine import ExperimentContext, run_experiment
from repotrial.hardening.policy import propose_mutation
from repotrial.intake.compose_discovery import discover_compose
from repotrial.journey.http_runner import run_http_journey
from repotrial.journey.playwright_runner import run_playwright_journey
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.lifecycle import managed_sandbox
from repotrial.trial import boot as boot_module
from repotrial.trial.boot import BootResult, _boot_compose_with_evidence, boot_compose
from repotrial.trial.boot_evidence import record_recovery_evidence
from repotrial.trial.compatibility import (
    materialize_guest_compatibility_overlay,
    verify_guest_compatibility_overlay,
)
from repotrial.trial.journey_artifact import (
    verify_baseline_journeys,
    write_or_verify_baseline_journeys,
)
from repotrial.trial.journey_context import derive_journey_readme_excerpt
from repotrial.trial.observer import _collect_observation_with_evidence
from repotrial.trial.planner import (
    _plan_journeys_with_evidence,
    _propose_recovery_with_evidence,
    plan_journeys,
    propose_recovery,
)
from repotrial.trial.recovery_context import (
    derive_recovery_context,
    project_recovery_evidence,
)
from repotrial.trial.startup_inputs import (
    _ADAPTER_SHA256,
    StartupInputUnsupported,
    materialize_startup_input,
    plan_startup_input,
    record_startup_input_rejection,
    verify_startup_input_attempt_history,
    write_or_verify_startup_input_identity,
)

type RunGraph = CompiledStateGraph[GraphState, GraphContext, GraphState, GraphState]
type NodeUpdate = dict[str, object]
type BaselineRoute = Literal["boot", "report_or_next"]
type BootRoute = Literal["boot", "journeys", "report_or_next"]
type MutationRoute = Literal["experiment", "report_or_next"]
type ExperimentRoute = Literal["decide", "report_or_next"]
type ReportRoute = Literal["propose_mutation", "__end__"]

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_GRAPH_RECURSION_LIMIT = 64
_FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_ATTEMPT_SLOTS = 4
_COMPATIBILITY_OVERLAY_NAME = "compatibility.overlay.yaml"
_MAX_COMPATIBILITY_ARTIFACT_BYTES = 1_048_576


def build_run_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    interrupt_after: Sequence[StageName] = (),
) -> RunGraph:
    builder = StateGraph(GraphState, context_schema=GraphContext)
    builder.add_node("intake", _intake)
    builder.add_node("baseline", _baseline)
    builder.add_node("boot", _boot)
    builder.add_node("journeys", _journeys)
    builder.add_node("observe", _observe)
    builder.add_node("propose_mutation", _propose_mutation)
    builder.add_node("experiment", _experiment)
    builder.add_node("decide", _decide)
    builder.add_node("report_or_next", _report_or_next)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "baseline")
    builder.add_conditional_edges("baseline", _baseline_route)
    builder.add_conditional_edges("boot", _boot_route)
    builder.add_edge("journeys", "observe")
    builder.add_edge("observe", "propose_mutation")
    builder.add_conditional_edges("propose_mutation", _mutation_route)
    builder.add_conditional_edges("experiment", _experiment_route)
    builder.add_edge("decide", "report_or_next")
    builder.add_conditional_edges("report_or_next", _report_route)
    return builder.compile(
        checkpointer=(
            checkpointer if checkpointer is not None else build_memory_checkpointer()
        ),
        interrupt_after=list(interrupt_after),
        name="repotrial",
    )


async def ainvoke_run(
    graph: RunGraph,
    state: RunState,
    *,
    context: GraphContext,
    config: RunnableConfig | None = None,
) -> GraphState:
    run_context = _snapshot_context(context)
    prepared_config = _run_config(state.run_id, config)
    result = await graph.ainvoke(
        GraphState(run=state.model_copy(deep=True)),
        cast(Any, prepared_config),
        context=run_context,
    )
    return _validated_result(result, state.run_id)


async def aresume_run(
    graph: RunGraph,
    run_id: str,
    *,
    context: GraphContext,
    config: RunnableConfig | None = None,
) -> GraphState:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    run_context = _snapshot_context(context)
    prepared_config = _run_config(run_id, config)
    result = await graph.ainvoke(None, cast(Any, prepared_config), context=run_context)
    return _validated_result(result, run_id)


async def _intake(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    context = runtime.context
    run = state.run.model_copy(deep=True)
    if run.commit_sha is None:
        pinned = await context.repository_pinner(run.repo_url, context.workspace, None)
        if not isinstance(pinned, PinnedRepo):
            raise TypeError("repository pinner must return PinnedRepo")
        workspace = _real_directory(context.workspace, "workspace")
        try:
            pinned_path = pinned.local_path.resolve(strict=True)
        except OSError:
            raise ValueError("pinned repository path is invalid") from None
        if pinned_path != workspace:
            raise ValueError("pinned repository path must equal workspace")
        if _FULL_COMMIT_SHA.fullmatch(pinned.commit_sha) is None:
            raise ValueError("repository pinner returned an invalid commit SHA")
        run.repo_url = pinned.repo.url
        run.commit_sha = pinned.commit_sha
    else:
        if _FULL_COMMIT_SHA.fullmatch(run.commit_sha) is None:
            raise ValueError("commit_sha must be a lowercase full commit SHA")
        _real_directory(context.workspace, "workspace")
    run.sandbox_id = None
    return {"run": run, "stage_history": _visit(state, "intake")}


async def _baseline(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    context = runtime.context
    workspace = _real_directory(context.workspace, "workspace")
    _ensure_directory_inside(context.overlay_dir, workspace, "overlay_dir")
    _ensure_directory_inside(
        context.accepted_compose_dir, workspace, "accepted_compose_dir"
    )
    if state.run.compose_path is None:
        compose_source = discover_compose(workspace)
    else:
        compose_source = _compose_source(workspace, state.run.compose_path)
    compose_relative = compose_source.relative_to(workspace).as_posix()
    compose = load_compose(compose_source)
    compose_hash = _compose_hash(compose)
    if (
        state.run.baseline_config_hash is not None
        and state.run.baseline_config_hash != compose_hash
    ):
        raise ValueError("baseline_config_hash does not match compose")
    if (
        state.run.current_config_hash is not None
        and state.run.current_config_hash != compose_hash
    ):
        raise ValueError("current_config_hash does not match compose")
    baseline_update = {
        "compose_path": compose_relative,
        "baseline_config_hash": compose_hash,
        "current_config_hash": compose_hash,
        "risk_findings": analyze_risk(compose),
        "journeys": list(state.run.journeys),
    }
    try:
        compatibility = _plan_or_verify_compatibility(
            state.run,
            context,
            compose,
            allow_create="baseline" not in state.stage_history,
        )
    except CompatibilityError as error:
        run = state.run.model_copy(
            deep=True,
            update={
                **baseline_update,
                "stop_reason": f"compatibility:{error.reason}",
            },
        )
        return {
            "run": run,
            "stage_history": _visit(state, "baseline"),
        }
    journeys = list(state.run.journeys)
    if not journeys:
        journey_readme_excerpt = (
            context.readme_excerpt
            if context.readme_excerpt
            else derive_journey_readme_excerpt(workspace)
        )
        if context.model is None:
            journeys = await plan_journeys(workspace, journey_readme_excerpt)
        else:
            journeys = await _plan_journeys_with_evidence(
                workspace,
                journey_readme_excerpt,
                context.model,
                evidence_dir=_run_evidence_directory(state.run, context),
            )
    write_or_verify_baseline_journeys(
        _baseline_journey_artifact(state.run, context), journeys
    )
    run = state.run.model_copy(
        deep=True,
        update={
            **baseline_update,
            "journeys": journeys,
            "compatibility_overlay_path": (
                None
                if compatibility is None
                else compatibility.path.relative_to(workspace).as_posix()
            ),
            "compatibility_overlay_sha256": (
                None if compatibility is None else compatibility.sha256
            ),
        },
    )
    if compatibility is not None:
        _append_artifact(
            run,
            compatibility.path.relative_to(workspace).as_posix(),
        )
    return {
        "run": run,
        "stage_history": _visit(state, "baseline"),
    }


async def _boot(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    attempt = state.boot_attempt + 1
    if attempt > 4:
        return {
            "run": state.run.model_copy(
                update={"stop_reason": "boot_recovery_stopped"}
            ),
            "stage_history": _visit(state, "boot"),
        }
    env = dict(runtime.context.env)
    env.update(state.recovery_env)
    context = runtime.context
    compose_path = _required_compose_path(state.run)
    try:
        compose = _current_compose(state.run, context)
        compatibility_artifact = _plan_or_verify_compatibility(
            state.run,
            context,
            compose,
            allow_create=False,
        )
    except CompatibilityError as error:
        return {
            "boot_attempt": attempt,
            "boot_verdict": Verdict.UNSUPPORTED,
            "run": state.run.model_copy(
                update={"stop_reason": f"compatibility:{error.reason}"}
            ),
            "stage_history": _visit(state, "boot"),
        }
    compatibility_path = (
        None if compatibility_artifact is None else compatibility_artifact.path
    )
    compatibility_relative = (
        None
        if compatibility_path is None
        else compatibility_path.relative_to(
            _real_directory(context.workspace, "workspace")
        ).as_posix()
    )
    recovery_context = (
        None
        if context.allowed_env_keys
        else derive_recovery_context(context.workspace, compose_path)
    )
    allowed_env_keys = (
        context.allowed_env_keys
        if recovery_context is None
        else recovery_context.allowed_env_keys
    )
    declared_secret_env_keys = (
        frozenset()
        if recovery_context is None
        else recovery_context.declared_secret_env_keys
    )
    attempt_dir, attempt_slot, prior_attempt_directories = _claim_attempt_directory(
        state.run,
        context,
        purpose="baseline",
        index=attempt,
    )
    lifecycle_artifact = attempt_dir / "baseline-lifecycle.jsonl"
    observation_artifact = attempt_dir / "baseline-observation.json"
    observation_evidence = attempt_dir / "baseline-observation-boundary.jsonl"
    evidence_artifact = attempt_dir / "baseline-boot-attempt.json"
    startup_input_evidence = attempt_dir / "startup-input-attempt.jsonl"
    compatibility_evidence = attempt_dir / "compatibility-materialization.jsonl"
    startup_input_identity = (
        _run_evidence_directory(state.run, context) / "startup-input-identity.json"
    )
    evidence_enabled = boot_compose is boot_module.boot_compose
    verify_baseline_journeys(
        _baseline_journey_artifact(state.run, context), state.run.journeys
    )
    try:
        startup_plan = plan_startup_input(context.workspace, compose_path)
        if startup_plan is None:
            if startup_input_identity.exists() or startup_input_identity.is_symlink():
                raise StartupInputUnsupported("startup_identity_missing_plan")
        else:
            if state.run.current_config_hash != (
                f"sha256:{startup_plan.compose_config_hash}"
            ):
                raise StartupInputUnsupported("compose_identity_mismatch")
            write_or_verify_startup_input_identity(
                startup_input_identity,
                startup_plan,
                adapter_sha256=_ADAPTER_SHA256,
            )
            verify_startup_input_attempt_history(
                prior_attempt_directories, startup_plan
            )
    except StartupInputUnsupported as error:
        record_startup_input_rejection(
            startup_input_evidence,
            compose_relative_path=compose_path,
            reason=error.reason,
        )
        return {
            "boot_attempt": attempt,
            "boot_verdict": Verdict.UNSUPPORTED,
            "run": state.run.model_copy(update={"stop_reason": "boot_unsupported"}),
            "stage_history": _visit(state, "boot"),
        }
    async with managed_sandbox(
        context.provider,
        context.workspace,
        (
            f"repotrial-baseline-{_run_token(state.run.run_id)}-"
            f"{attempt:02d}-{attempt_slot:02d}"
        ),
        lifecycle_artifact=lifecycle_artifact,
    ) as sandbox_id:
        try:
            if compatibility_relative is not None:
                if compatibility_path is None:
                    raise CompatibilityError("identity_incomplete")
                await materialize_guest_compatibility_overlay(
                    context.provider,
                    sandbox_id,
                    host_artifact_path=compatibility_path,
                    relative_path=compatibility_relative,
                    expected_sha256=state.run.compatibility_overlay_sha256 or "",
                    evidence_path=compatibility_evidence,
                )
                await verify_guest_compatibility_overlay(
                    context.provider,
                    sandbox_id,
                    relative_path=compatibility_relative,
                    expected_sha256=state.run.compatibility_overlay_sha256 or "",
                )
        except CompatibilityError as error:
            return {
                "boot_attempt": attempt,
                "boot_verdict": Verdict.UNSUPPORTED,
                "run": state.run.model_copy(
                    update={"stop_reason": f"compatibility:{error.reason}"}
                ),
                "stage_history": _visit(state, "boot"),
            }
        try:
            if startup_plan is not None:
                await materialize_startup_input(
                    context.provider,
                    sandbox_id,
                    startup_plan,
                    compose_path=compose_path,
                    compose_env=env,
                    evidence_path=startup_input_evidence,
                    compatibility_overlay_path=compatibility_relative,
                )
        except StartupInputUnsupported:
            result = BootResult(
                verdict=Verdict.UNSUPPORTED,
                service_states={},
                logs={},
                attempt=attempt,
            )
        else:
            if evidence_enabled:
                if startup_plan is None:
                    result = await _boot_compose_with_evidence(
                        context.provider,
                        sandbox_id,
                        compose_path,
                        env,
                        attempt,
                        evidence_path=evidence_artifact,
                        compatibility_overlay_path=compatibility_relative,
                        declared_secret_env_keys=declared_secret_env_keys,
                    )
                else:
                    result = await _boot_compose_with_evidence(
                        context.provider,
                        sandbox_id,
                        compose_path,
                        env,
                        attempt,
                        evidence_path=evidence_artifact,
                        compatibility_overlay_path=compatibility_relative,
                        unset_env_keys=startup_plan.all_source_key_names,
                        project_directory=".",
                        declared_secret_env_keys=declared_secret_env_keys,
                    )
            else:
                if startup_plan is None:
                    if compatibility_relative is None:
                        result = await boot_compose(
                            context.provider,
                            sandbox_id,
                            compose_path,
                            env,
                            attempt,
                            declared_secret_env_keys=declared_secret_env_keys,
                        )
                    else:
                        result = await boot_compose(
                            context.provider,
                            sandbox_id,
                            compose_path,
                            env,
                            attempt,
                            compatibility_overlay_path=compatibility_relative,
                            declared_secret_env_keys=declared_secret_env_keys,
                        )
                else:
                    if compatibility_relative is None:
                        result = await boot_compose(
                            context.provider,
                            sandbox_id,
                            compose_path,
                            env,
                            attempt,
                            unset_env_keys=startup_plan.all_source_key_names,
                            project_directory=".",
                            declared_secret_env_keys=declared_secret_env_keys,
                        )
                    else:
                        result = await boot_compose(
                            context.provider,
                            sandbox_id,
                            compose_path,
                            env,
                            attempt,
                            compatibility_overlay_path=compatibility_relative,
                            unset_env_keys=startup_plan.all_source_key_names,
                            project_directory=".",
                            declared_secret_env_keys=declared_secret_env_keys,
                        )
        effective_env = dict(env)
        effective_env.update(result.recovery_env)
        journey_results: list[JourneyResult] | None = None
        observation = None
        if result.verdict is Verdict.PASS:
            journey_results = await _run_baseline_journeys(
                state.run.journeys,
                context,
                sandbox_id,
                attempt_dir,
            )
            if startup_plan is None:
                observation = await _collect_observation_with_evidence(
                    context.provider,
                    sandbox_id,
                    compose_path,
                    observation_artifact,
                    evidence_path=observation_evidence,
                    env=effective_env,
                    compatibility_overlay_path=compatibility_relative,
                )
            else:
                observation = await _collect_observation_with_evidence(
                    context.provider,
                    sandbox_id,
                    compose_path,
                    observation_artifact,
                    evidence_path=observation_evidence,
                    env=effective_env,
                    compatibility_overlay_path=compatibility_relative,
                    unset_env_keys=startup_plan.all_source_key_names,
                    project_directory=".",
                )
    update: NodeUpdate = {
        "boot_attempt": attempt,
        "boot_verdict": result.verdict,
        "stage_history": _visit(state, "boot"),
    }
    if result.recovery_env:
        recovery_env = dict(state.recovery_env)
        recovery_env.update(result.recovery_env)
        update["recovery_env"] = recovery_env
        update["run"] = state.run.model_copy(
            update={
                "recovery_env_keys": sorted(
                    {*state.run.recovery_env_keys, *recovery_env}
                )
            }
        )
    if result.verdict is Verdict.PASS:
        update["pending_journey_results"] = journey_results
        update["pending_observation"] = observation
        return update
    if result.verdict is Verdict.UNSUPPORTED:
        run = update.get("run", state.run)
        if not isinstance(run, RunState):
            raise TypeError("boot update contains a malformed run state")
        update["run"] = run.model_copy(update={"stop_reason": "boot_unsupported"})
        return update

    recovery_evidence = project_recovery_evidence(result.logs)
    fingerprint = _boot_error_fingerprint(recovery_evidence.logs)
    repeated = (
        state.repeated_error_count + 1 if fingerprint == state.boot_error_hash else 0
    )
    if runtime.context.model is None:
        recovery = await propose_recovery(
            recovery_evidence.logs,
            runtime.context.readme_excerpt,
            set(allowed_env_keys),
            repeated,
        )
    else:
        recovery = await _propose_recovery_with_evidence(
            recovery_evidence.logs,
            runtime.context.readme_excerpt,
            set(allowed_env_keys),
            repeated,
            runtime.context.model,
            evidence_dir=attempt_dir,
        )
    update["boot_error_hash"] = fingerprint
    update["repeated_error_count"] = repeated
    await _apply_recovery(state, update, recovery)
    updated_run = update.get("run")
    stop_reason = updated_run.stop_reason if isinstance(updated_run, RunState) else None
    recovery_reason = recovery.reason
    for value in sorted(
        {value for value in env.values() if value},
        key=lambda value: (-len(value), value),
    ):
        recovery_reason = recovery_reason.replace(value, "[REDACTED]")
    evidence_recovery = recovery.model_copy(update={"reason": recovery_reason})
    if evidence_enabled:
        disposition: Literal["applied", "stopped", "unsupported"] = "applied"
        if recovery.action == "stop":
            disposition = "stopped"
        elif recovery.action == "wait":
            disposition = "unsupported"
        record_recovery_evidence(
            evidence_artifact,
            evidence_recovery,
            disposition=disposition,
            stop_reason=stop_reason,
        )
    return update


async def _apply_recovery(
    state: GraphState,
    update: NodeUpdate,
    recovery: RecoveryAction,
) -> None:
    updated_run = update.get("run", state.run)
    if not isinstance(updated_run, RunState):
        raise TypeError("recovery update contains a malformed run state")
    updated_env = update.get("recovery_env", state.recovery_env)
    if not isinstance(updated_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in updated_env.items()
    ):
        raise TypeError("recovery update contains malformed environment")
    if recovery.action in {"stop", "wait"}:
        update["run"] = updated_run.model_copy(
            update={"stop_reason": "boot_recovery_stopped"}
        )
        return
    if recovery.action == "retry":
        return
    if recovery.action == "set_env":
        key = recovery.params.get("key")
        value = recovery.params.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("validated set_env action is malformed")
        recovery_env = dict(updated_env)
        recovery_env[key] = value
        recovery_env_keys = sorted({*updated_run.recovery_env_keys, *recovery_env})
        update["recovery_env"] = recovery_env
        update["run"] = updated_run.model_copy(
            update={"recovery_env_keys": recovery_env_keys}
        )
        return
    raise ValueError("validated recovery action is unsupported")


def _baseline_route(state: GraphState) -> BaselineRoute:
    return "report_or_next" if state.run.stop_reason is not None else "boot"


def _plan_or_verify_compatibility(
    run: RunState,
    context: GraphContext,
    compose: dict[str, object],
    *,
    allow_create: bool,
) -> CompatibilityArtifact | None:
    workspace, overlay_dir = _compatibility_directories(context)
    target = overlay_dir / _COMPATIBILITY_OVERLAY_NAME
    plan = plan_loopback_compatibility_overlay(compose, context.container_port)
    if run.compatibility_overlay_path is not None or (
        run.compatibility_overlay_sha256 is not None
    ):
        if plan is None:
            raise CompatibilityError("plan_changed")
        verified = _verified_compatibility_path(run, context, expected_plan=plan)
        if verified is None or run.compatibility_overlay_sha256 is None:
            raise CompatibilityError("identity_incomplete")
        return CompatibilityArtifact(
            path=verified,
            sha256=plan.sha256,
        )
    if plan is None:
        try:
            target_exists = target.exists() or target.is_symlink()
        except OSError:
            raise CompatibilityError("artifact_unreadable") from None
        if target_exists:
            raise CompatibilityError("artifact_path_exists")
        return None
    if not allow_create:
        raise CompatibilityError("plan_changed")
    artifact = write_loopback_compatibility_overlay(
        compose,
        context.container_port,
        target,
    )
    if artifact is None:
        raise CompatibilityError("plan_changed")
    if not isinstance(artifact, CompatibilityArtifact):
        raise CompatibilityError("artifact_invalid")
    if artifact.path != target:
        raise CompatibilityError("planned_artifact_mismatch")
    if artifact.sha256 != plan.sha256:
        raise CompatibilityError("artifact_hash_mismatch")
    _verify_compatibility_plan(target, workspace, overlay_dir, plan)
    return artifact


def _current_compose(run: RunState, context: GraphContext) -> dict[str, object]:
    try:
        workspace = _real_directory(context.workspace, "workspace")
        compose_path = _compose_source(workspace, _required_compose_path(run))
        return load_compose(compose_path)
    except (ComposeParseError, TypeError, ValueError):
        raise CompatibilityError("compose_invalid") from None


def _compatibility_directories(context: GraphContext) -> tuple[Path, Path]:
    try:
        workspace = _real_directory(context.workspace, "workspace")
    except (TypeError, ValueError):
        raise CompatibilityError("path_invalid") from None
    overlay_path = context.overlay_dir
    if not isinstance(overlay_path, Path):
        raise CompatibilityError("path_invalid")
    try:
        _require_lexical_path_inside(overlay_path, workspace, "overlay_dir")
    except ValueError:
        raise CompatibilityError("path_invalid") from None
    try:
        _reject_linked_components(workspace, overlay_path)
    except ValueError as error:
        reason = "artifact_linked" if "link" in str(error) else "artifact_missing"
        raise CompatibilityError(reason) from None
    try:
        overlay_stat = overlay_path.lstat()
    except FileNotFoundError:
        raise CompatibilityError("artifact_missing") from None
    except OSError:
        raise CompatibilityError("path_invalid") from None
    if _is_link(overlay_path, overlay_stat):
        raise CompatibilityError("artifact_linked")
    if not stat.S_ISDIR(overlay_stat.st_mode):
        raise CompatibilityError("path_invalid")
    try:
        overlay_dir = overlay_path.resolve(strict=True)
    except FileNotFoundError:
        raise CompatibilityError("artifact_missing") from None
    except OSError:
        raise CompatibilityError("path_invalid") from None
    if not overlay_dir.is_relative_to(workspace):
        raise CompatibilityError("path_invalid")
    return workspace, overlay_dir


def _verify_compatibility_plan(
    target: Path,
    workspace: Path,
    overlay_dir: Path,
    plan: CompatibilityPlan,
) -> None:
    payload = _read_compatibility_artifact(
        target,
        workspace,
        overlay_dir,
        max_bytes=len(plan.payload),
    )
    if payload != plan.payload or hashlib.sha256(payload).hexdigest() != plan.sha256:
        raise CompatibilityError("plan_changed")


def _read_compatibility_artifact(
    target: Path,
    workspace: Path,
    overlay_dir: Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    if max_bytes is None:
        max_bytes = _MAX_COMPATIBILITY_ARTIFACT_BYTES
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
        or max_bytes > _MAX_COMPATIBILITY_ARTIFACT_BYTES
    ):
        raise CompatibilityError("artifact_oversize")
    try:
        target_stat = target.lstat()
        resolved = target.resolve(strict=True)
        parent = target.parent.resolve(strict=True)
    except FileNotFoundError:
        raise CompatibilityError("artifact_missing") from None
    except RuntimeError:
        raise CompatibilityError("artifact_linked") from None
    except (OSError, ValueError):
        raise CompatibilityError("artifact_unreadable") from None
    if _is_link(target, target_stat):
        raise CompatibilityError("artifact_linked")
    if not stat.S_ISREG(target_stat.st_mode):
        raise CompatibilityError("artifact_not_regular")
    if parent != overlay_dir or resolved != target:
        raise CompatibilityError("path_invalid")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags |= no_follow
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError:
            raise CompatibilityError("artifact_missing") from None
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EMLINK}:
                raise CompatibilityError("artifact_linked") from None
            raise CompatibilityError("artifact_unreadable") from None

        try:
            opened_stat = os.fstat(descriptor)
        except OSError:
            raise CompatibilityError("artifact_unreadable") from None
        if _is_link(target, opened_stat):
            raise CompatibilityError("artifact_linked")
        if not stat.S_ISREG(opened_stat.st_mode):
            raise CompatibilityError("artifact_not_regular")
        opened_identity = _compatibility_file_identity(opened_stat)
        if _compatibility_file_identity(target_stat) != opened_identity:
            raise CompatibilityError("artifact_changed")
        if opened_stat.st_size > max_bytes:
            raise CompatibilityError("artifact_oversize")

        payload = bytearray()
        read_limit = max_bytes + 1
        while len(payload) < read_limit:
            try:
                chunk = os.read(descriptor, min(65_536, read_limit - len(payload)))
            except OSError:
                raise CompatibilityError("artifact_unreadable") from None
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise CompatibilityError("artifact_oversize")

        try:
            final_fd_stat = os.fstat(descriptor)
            final_path_stat = target.lstat()
        except FileNotFoundError:
            raise CompatibilityError("artifact_missing") from None
        except OSError:
            raise CompatibilityError("artifact_unreadable") from None
        if _is_link(target, final_path_stat):
            raise CompatibilityError("artifact_linked")
        if not stat.S_ISREG(final_fd_stat.st_mode) or not stat.S_ISREG(
            final_path_stat.st_mode
        ):
            raise CompatibilityError("artifact_not_regular")
        if (
            _compatibility_file_identity(final_fd_stat) != opened_identity
            or _compatibility_file_identity(final_path_stat) != opened_identity
        ):
            raise CompatibilityError("artifact_changed")
        return bytes(payload)
    except CompatibilityError:
        raise
    except (OSError, ValueError):
        raise CompatibilityError("artifact_unreadable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _compatibility_file_identity(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _verified_compatibility_path(
    run: RunState,
    context: GraphContext,
    *,
    expected_plan: CompatibilityPlan | None = None,
) -> Path | None:
    path_value = run.compatibility_overlay_path
    sha256_value = run.compatibility_overlay_sha256
    if path_value is None and sha256_value is None:
        return None
    if not isinstance(path_value, str) or not isinstance(sha256_value, str):
        raise CompatibilityError("identity_incomplete")
    if _contains_controls(path_value) or "\\" in path_value:
        raise CompatibilityError("path_invalid")
    relative = Path(path_value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise CompatibilityError("path_invalid")
    workspace, overlay_dir = _compatibility_directories(context)
    expected = overlay_dir / _COMPATIBILITY_OVERLAY_NAME
    try:
        expected_relative = expected.relative_to(workspace).as_posix()
    except ValueError:
        raise CompatibilityError("planned_artifact_mismatch") from None
    if path_value != expected_relative:
        raise CompatibilityError("planned_artifact_mismatch")
    target = workspace / relative
    payload = _read_compatibility_artifact(
        target,
        workspace,
        overlay_dir,
        max_bytes=(
            _MAX_COMPATIBILITY_ARTIFACT_BYTES
            if expected_plan is None
            else len(expected_plan.payload)
        ),
    )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != sha256_value:
        raise CompatibilityError("artifact_hash_mismatch")
    if expected_plan is not None and (
        payload != expected_plan.payload or actual_sha256 != expected_plan.sha256
    ):
        raise CompatibilityError("plan_changed")
    return target


def _boot_route(state: GraphState) -> BootRoute:
    if state.run.stop_reason is not None:
        return "report_or_next"
    if state.boot_verdict is Verdict.PASS:
        return "journeys"
    return "boot"


async def _journeys(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    del runtime
    results = state.pending_journey_results
    if results is None:
        raise ValueError("journeys requires results produced by boot")
    run = state.run.model_copy(update={"baseline_journey_results": list(results)})
    return {
        "run": run,
        "pending_journey_results": None,
        "stage_history": _visit(state, "journeys"),
    }


async def _observe(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    del runtime
    observation = state.pending_observation
    if observation is None:
        raise ValueError("observe requires an observation produced by boot")
    run = state.run.model_copy(update={"baseline_observation": observation})
    return {
        "run": run,
        "pending_observation": None,
        "stage_history": _visit(state, "observe"),
    }


def _propose_mutation(state: GraphState) -> NodeUpdate:
    decision = propose_mutation(state.run)
    run = state.run
    if decision.stop_reason is not None:
        run = run.model_copy(update={"stop_reason": decision.stop_reason})
    return {
        "run": run,
        "pending_mutation": decision.mutation,
        "pending_experiment": None,
        "pending_overlay_path": None,
        "pending_overlay_materialized": None,
        "stage_history": _visit(state, "propose_mutation"),
    }


def _mutation_route(state: GraphState) -> MutationRoute:
    return "experiment" if state.pending_mutation is not None else "report_or_next"


def _experiment_route(state: GraphState) -> ExperimentRoute:
    return "report_or_next" if state.run.stop_reason is not None else "decide"


async def _experiment(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    mutation = state.pending_mutation
    if mutation is None:
        raise ValueError("experiment requires a pending mutation")
    context = runtime.context
    try:
        compose = _current_compose(state.run, context)
        compatibility_artifact = _plan_or_verify_compatibility(
            state.run,
            context,
            compose,
            allow_create=False,
        )
    except CompatibilityError as error:
        return {
            "run": state.run.model_copy(
                update={"stop_reason": f"compatibility:{error.reason}"}
            ),
            "pending_mutation": None,
            "pending_experiment": None,
            "pending_overlay_path": None,
            "pending_overlay_materialized": None,
            "stage_history": _visit(state, "experiment"),
        }
    compatibility_path = (
        None if compatibility_artifact is None else compatibility_artifact.path
    )
    token = _run_token(state.run.run_id)
    index = len(state.run.experiments)
    attempt_dir, attempt_slot, prior_attempt_directories = _claim_attempt_directory(
        state.run,
        context,
        purpose="experiment",
        index=index,
    )
    overlay_path = (
        context.overlay_dir
        / f"{token}-{index:04d}-attempt-{attempt_slot:02d}.overlay.yaml"
    )
    workspace = _real_directory(context.workspace, "workspace")
    overlay_dir = _real_directory_inside(context.overlay_dir, workspace, "overlay_dir")
    _require_lexical_path_inside(overlay_path, workspace, "overlay")
    if overlay_path.parent.resolve(strict=True) != overlay_dir:
        raise ValueError("overlay must be a direct child of overlay_dir")
    if overlay_path.exists() or overlay_path.is_symlink():
        raise ValueError("overlay attempt target is already in use")
    experiment_context = ExperimentContext(
        workspace=context.workspace,
        overlay_path=overlay_path,
        artifact_dir=attempt_dir,
        env={**context.env, **state.recovery_env},
        container_port=context.container_port,
        compatibility_overlay_path=compatibility_path,
        compatibility_overlay_sha256=state.run.compatibility_overlay_sha256,
        compatibility_overlay_evidence_path=(
            None
            if compatibility_path is None
            else attempt_dir / "compatibility-materialization.jsonl"
        ),
        prior_attempt_directories=prior_attempt_directories,
        startup_input_identity_path=(
            _run_evidence_directory(state.run, context) / "startup-input-identity.json"
        ),
    )
    verify_baseline_journeys(
        _baseline_journey_artifact(state.run, context), state.run.journeys
    )
    try:
        record = await run_experiment(
            state.run.model_copy(deep=True),
            mutation,
            context.provider,
            context=experiment_context,
        )
    except CompatibilityError as error:
        return {
            "run": state.run.model_copy(
                update={"stop_reason": f"compatibility:{error.reason}"}
            ),
            "pending_mutation": None,
            "pending_experiment": None,
            "pending_overlay_path": None,
            "pending_overlay_materialized": None,
            "stage_history": _visit(state, "experiment"),
        }
    overlay_materialized = overlay_path.exists() or overlay_path.is_symlink()
    if overlay_materialized:
        _existing_regular_file(overlay_path, workspace, "overlay")
    elif record.verdict is not ExperimentVerdict.STOP:
        raise ValueError("KEEP/ROLLBACK experiment must materialize an overlay")
    return {
        "pending_experiment": record,
        "pending_overlay_path": overlay_path.relative_to(workspace).as_posix(),
        "pending_overlay_materialized": overlay_materialized,
        "stage_history": _visit(state, "experiment"),
    }


def _decide(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    record = state.pending_experiment
    if record is None:
        raise ValueError("decide requires an experiment record")
    if any(
        item.experiment_id == record.experiment_id for item in state.run.experiments
    ):
        raise ValueError("experiment record was already appended")

    run = state.run.model_copy(deep=True)
    overlay = _selected_overlay_relative_path(state, record, runtime.context)
    if overlay is not None:
        _append_artifact(run, overlay)
    run.experiments.append(record)
    if record.verdict is ExperimentVerdict.KEEP:
        accepted = _materialize_accepted_compose(run, record, runtime.context)
        run.compose_path = accepted
        run.current_config_hash = record.candidate_config_hash
        _append_artifact(run, accepted)
    elif record.verdict is ExperimentVerdict.STOP:
        run.stop_reason = f"experiment:{record.reason}"
    return {
        "run": run,
        "pending_mutation": None,
        "pending_experiment": None,
        "pending_overlay_path": None,
        "pending_overlay_materialized": None,
        "stage_history": _visit(state, "decide"),
    }


def _report_or_next(state: GraphState) -> NodeUpdate:
    return {"stage_history": _visit(state, "report_or_next")}


def _report_route(state: GraphState) -> ReportRoute:
    return "__end__" if state.run.stop_reason is not None else "propose_mutation"


def _visit(state: GraphState, stage: StageName) -> list[StageName]:
    if len(state.stage_history) >= 64:
        raise ValueError("stage history limit exceeded")
    return [*state.stage_history, stage]


def _boot_error_fingerprint(logs: Mapping[str, str]) -> str:
    bounded = json.dumps(dict(logs), ensure_ascii=False, sort_keys=True).encode(
        "utf-8", errors="replace"
    )
    return hashlib.sha256(bounded).hexdigest()


def _run_token(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8", errors="replace")).hexdigest()[:16]


def _baseline_journey_artifact(run: RunState, context: GraphContext) -> Path:
    return _run_evidence_directory(run, context) / "baseline-journeys.json"


def _run_evidence_directory(run: RunState, context: GraphContext) -> Path:
    artifact_root = _real_directory(context.artifact_dir, "artifact_dir")
    directory = artifact_root / f"run-{_run_token(run.run_id)}"
    if directory.exists() or directory.is_symlink():
        existing = _real_directory(directory, "run evidence directory")
        if existing.parent != artifact_root:
            raise ValueError("run evidence directory must resolve inside artifact_dir")
        return existing
    try:
        directory.mkdir()
    except OSError:
        raise ValueError("run evidence directory could not be created") from None
    created = _real_directory(directory, "run evidence directory")
    if created.parent != artifact_root:
        raise ValueError("run evidence directory must resolve inside artifact_dir")
    return created


async def _run_baseline_journeys(
    journeys: Sequence[Journey],
    context: GraphContext,
    sandbox_id: str,
    artifact_dir: Path,
) -> list[JourneyResult]:
    if not journeys:
        return []
    host_port = await context.provider.publish_port(sandbox_id, context.container_port)
    if type(host_port) is not int or not 1 <= host_port <= 65_535:
        raise ValueError("published port is outside the valid range")
    base_url = f"http://127.0.0.1:{host_port}"
    results: list[JourneyResult] = []
    for index, journey in enumerate(journeys):
        evidence_dir = artifact_dir / f"baseline-journey-{index:04d}"
        tools = {step.tool for step in journey.steps}
        if tools == {"http"}:
            result = await run_http_journey(
                journey,
                base_url=base_url,
                evidence_dir=evidence_dir,
            )
        elif tools == {"browser"}:
            result = await run_playwright_journey(
                journey,
                base_url=base_url,
                evidence_dir=evidence_dir,
            )
        else:
            result = JourneyResult(
                journey_id=journey.journey_id,
                verdict=Verdict.UNSUPPORTED,
                passed_steps=0,
                total_steps=len(journey.steps),
                failure_reason="journey:unsupported_tool_mix",
            )
        results.append(result)
    return results


def _required_compose_path(run: RunState) -> str:
    if not isinstance(run.compose_path, str) or not run.compose_path:
        raise ValueError("boot requires a compose path")
    return run.compose_path


def _claim_attempt_directory(
    run: RunState,
    context: GraphContext,
    *,
    purpose: Literal["baseline", "experiment"],
    index: int,
) -> tuple[Path, int, tuple[Path, ...]]:
    workspace = _real_directory(context.workspace, "workspace")
    artifact_root = _real_directory(context.artifact_dir, "artifact_dir")
    if artifact_root.is_relative_to(workspace):
        raise ValueError("artifact_dir must resolve outside workspace")
    token = _run_token(run.run_id)
    prior_directories: list[Path] = []
    for slot in range(1, _MAX_ATTEMPT_SLOTS + 1):
        directory = artifact_root / f"{purpose}-{token}-{index:04d}-attempt-{slot:02d}"
        marker = directory / ".repotrial-attempt.json"
        expected = (
            json.dumps(
                {
                    "index": index,
                    "purpose": purpose,
                    "run_token": token,
                    "slot": slot,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if directory.exists() or directory.is_symlink():
            existing = _real_directory(directory, "attempt directory")
            if existing.parent != artifact_root:
                raise ValueError("attempt directory must resolve inside artifact_dir")
            try:
                marker_stat = marker.lstat()
                if (
                    _is_link(marker, marker_stat)
                    or not stat.S_ISREG(marker_stat.st_mode)
                    or marker_stat.st_size != len(expected)
                ):
                    raise ValueError("attempt directory ownership marker is invalid")
                with marker.open("rb") as marker_file:
                    marker_bytes = marker_file.read(len(expected) + 1)
            except OSError:
                raise ValueError(
                    "attempt directory ownership marker is invalid"
                ) from None
            if marker_bytes != expected:
                raise ValueError("attempt directory ownership marker is invalid")
            prior_directories.append(existing)
            continue
        try:
            directory.mkdir()
            with marker.open("xb") as marker_file:
                marker_file.write(expected)
        except OSError:
            raise ValueError("attempt namespace could not be claimed") from None
        claimed = _real_directory(directory, "attempt directory")
        if claimed.parent != artifact_root:
            raise ValueError("attempt directory must resolve inside artifact_dir")
        return claimed, slot, tuple(prior_directories)
    raise ValueError("attempt slots exhausted")


def _selected_overlay_relative_path(
    state: GraphState,
    record: ExperimentRecord,
    context: GraphContext,
) -> str | None:
    selected = state.pending_overlay_path
    if not isinstance(selected, str) or not selected:
        raise ValueError("decide requires the selected overlay path")
    if _contains_controls(selected) or "\\" in selected:
        raise ValueError("selected overlay path is unsafe")
    relative = Path(selected)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("selected overlay path is unsafe")
    workspace = _real_directory(context.workspace, "workspace")
    overlay_dir = _real_directory_inside(context.overlay_dir, workspace, "overlay_dir")
    path = workspace / relative
    _require_lexical_path_inside(path, workspace, "overlay")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        raise ValueError("selected overlay parent is invalid") from None
    if parent != overlay_dir:
        raise ValueError("selected overlay must be inside overlay_dir")
    materialized = state.pending_overlay_materialized
    if materialized is None:
        raise ValueError("decide requires the overlay materialization state")
    if not materialized:
        if record.verdict is not ExperimentVerdict.STOP:
            raise ValueError("only STOP may omit an overlay")
        if path.exists() or path.is_symlink():
            _existing_regular_file(path, workspace, "overlay")
            raise ValueError("pre-materialization STOP overlay must remain absent")
        return None
    if not path.exists() and not path.is_symlink():
        raise ValueError("materialized overlay must remain present")
    resolved = _existing_regular_file(path, workspace, "overlay")
    return resolved.relative_to(workspace).as_posix()


def _materialize_accepted_compose(
    run: RunState, record: ExperimentRecord, context: GraphContext
) -> str:
    workspace = _real_directory(context.workspace, "workspace")
    accepted_dir = _real_directory_inside(
        context.accepted_compose_dir, workspace, "accepted_compose_dir"
    )
    source = _compose_source(workspace, run.compose_path)
    candidate = apply_mutation(load_compose(source), record.mutation)
    candidate_hash = _compose_hash(candidate)
    if candidate_hash != record.candidate_config_hash:
        raise ValueError("accepted candidate hash mismatch")
    digest = record.candidate_config_hash.removeprefix("sha256:")[:16]
    target = accepted_dir / f"accepted-{len(run.experiments):04d}-{digest}.compose.yaml"
    if target.exists() or target.is_symlink():
        existing = _existing_regular_file(target, workspace, "accepted compose")
        if _compose_hash(load_compose(existing)) != record.candidate_config_hash:
            raise ValueError("accepted compose replay hash mismatch")
        return existing.relative_to(workspace).as_posix()

    yaml = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    created = False
    try:
        with target.open("x", encoding="utf-8") as output:
            created = True
            yaml.dump(candidate, output)
        written = _existing_regular_file(target, workspace, "accepted compose")
        if _compose_hash(load_compose(written)) != record.candidate_config_hash:
            raise ValueError("accepted compose write hash mismatch")
    except (
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        YAMLError,
    ):
        if created:
            target.unlink(missing_ok=True)
        raise
    return target.relative_to(workspace).as_posix()


def _append_artifact(run: RunState, relative_path: str) -> None:
    if relative_path not in run.artifacts:
        run.artifacts.append(relative_path)


def _compose_hash(compose: dict[str, object]) -> str:
    material = canonical_compose_json(compose).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _compose_source(workspace: Path, compose_path: object) -> Path:
    if not isinstance(compose_path, str) or not compose_path:
        raise ValueError("compose_path must be a non-empty string")
    if _contains_controls(compose_path) or "\\" in compose_path:
        raise ValueError("compose_path must be a safe relative path")
    relative = Path(compose_path)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("compose_path must be a safe relative path")
    return _existing_regular_file(workspace / relative, workspace, "compose")


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


def _real_directory_inside(path: object, workspace: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    _require_lexical_path_inside(path, workspace, label)
    _reject_linked_components(workspace, path)
    resolved = _real_directory(path, label)
    if not resolved.is_relative_to(workspace):
        raise ValueError(f"{label} must resolve inside workspace")
    return resolved


def _ensure_directory_inside(path: object, workspace: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    _require_lexical_path_inside(path, workspace, label)
    if path.exists() or path.is_symlink():
        return _real_directory_inside(path, workspace, label)
    parent = _real_directory_inside(path.parent, workspace, f"{label} parent")
    if not parent.is_relative_to(workspace):
        raise ValueError(f"{label} parent must resolve inside workspace")
    try:
        path.mkdir()
    except OSError:
        raise ValueError(f"{label} could not be created") from None
    return _real_directory_inside(path, workspace, label)


def _existing_regular_file(path: Path, workspace: Path, label: str) -> Path:
    _require_lexical_path_inside(path, workspace, label)
    _reject_linked_components(workspace, path.parent)
    try:
        resolved = path.resolve(strict=True)
        path_stat = path.lstat()
    except OSError:
        raise ValueError(f"{label} must be an existing regular file") from None
    if (
        not resolved.is_relative_to(workspace)
        or not stat.S_ISREG(path_stat.st_mode)
        or _is_link(path, path_stat)
    ):
        raise ValueError(f"{label} must be an existing regular file")
    return resolved


def _require_lexical_path_inside(path: Path, workspace: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path inside workspace")
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        raise ValueError(f"{label} must be inside workspace") from None
    if ".." in relative.parts:
        raise ValueError(f"{label} must be inside workspace")


def _reject_linked_components(workspace: Path, target: Path) -> None:
    current = workspace
    for component in target.relative_to(workspace).parts:
        current /= component
        try:
            current_stat = current.lstat()
        except OSError:
            raise ValueError("trusted path component does not exist") from None
        if _is_link(current, current_stat):
            raise ValueError("trusted path contains a link")


def _is_link(path: Path, path_stat: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        _REPARSE_POINT and getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _contains_controls(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _snapshot_context(context: GraphContext) -> GraphContext:
    return replace(
        context,
        env=dict(context.env),
        allowed_env_keys=frozenset(context.allowed_env_keys),
    )


def _run_config(run_id: str, config: RunnableConfig | None) -> RunnableConfig:
    prepared = cast(RunnableConfig, dict(config or {}))
    configurable = prepared.get("configurable")
    if configurable is None:
        configurable = {}
    if not isinstance(configurable, Mapping):
        raise TypeError("configurable must be a mapping")
    supplied_thread_id = configurable.get("thread_id")
    if supplied_thread_id is not None and supplied_thread_id != run_id:
        raise ValueError("thread_id must equal run_id")
    prepared["configurable"] = {**configurable, "thread_id": run_id}
    prepared.setdefault("recursion_limit", _GRAPH_RECURSION_LIMIT)
    return prepared


def _validated_result(result: object, run_id: str) -> GraphState:
    if not isinstance(result, Mapping):
        raise TypeError("graph returned invalid state")
    parsed = GraphState.model_validate(dict(result))
    if parsed.run.run_id != run_id:
        raise ValueError("checkpoint run_id does not match thread_id")
    return parsed
