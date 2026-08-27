import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from repotrial.agent.state import GraphContext, GraphState, StageName
from repotrial.compose.mutations import apply_mutation
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import ExperimentVerdict, Verdict
from repotrial.domain.models import ExperimentRecord, RunState
from repotrial.hardening.engine import ExperimentContext, run_experiment
from repotrial.hardening.policy import propose_mutation
from repotrial.models.base import RecoveryAction
from repotrial.trial.planner import propose_recovery

type RunGraph = CompiledStateGraph[GraphState, GraphContext, GraphState, GraphState]
type NodeUpdate = dict[str, object]
type BootRoute = Literal["boot", "journeys", "report_or_next"]
type MutationRoute = Literal["experiment", "report_or_next"]
type ReportRoute = Literal["propose_mutation", "__end__"]

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_GRAPH_RECURSION_LIMIT = 64


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
    builder.add_edge("baseline", "boot")
    builder.add_conditional_edges("boot", _boot_route)
    builder.add_edge("journeys", "observe")
    builder.add_edge("observe", "propose_mutation")
    builder.add_conditional_edges("propose_mutation", _mutation_route)
    builder.add_edge("experiment", "decide")
    builder.add_edge("decide", "report_or_next")
    builder.add_conditional_edges("report_or_next", _report_route)
    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
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
        prepared_config,
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
    result = await graph.ainvoke(None, prepared_config, context=run_context)
    return _validated_result(result, run_id)


async def _intake(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    run = await runtime.context.operations.intake(state.run.model_copy(deep=True))
    return {"run": _same_run(state.run, run), "stage_history": _visit(state, "intake")}


async def _baseline(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    run = await runtime.context.operations.baseline(state.run.model_copy(deep=True))
    return {
        "run": _same_run(state.run, run),
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
    result = await runtime.context.operations.boot(
        state.run.model_copy(deep=True),
        runtime.context.provider,
        env,
        attempt,
    )
    if result.attempt != attempt:
        raise ValueError("boot result attempt does not match requested attempt")
    update: NodeUpdate = {
        "boot_attempt": attempt,
        "boot_verdict": result.verdict,
        "stage_history": _visit(state, "boot"),
    }
    if result.verdict is Verdict.PASS:
        return update
    if result.verdict is Verdict.UNSUPPORTED:
        update["run"] = state.run.model_copy(update={"stop_reason": "boot_unsupported"})
        return update

    fingerprint = _boot_error_fingerprint(result.logs)
    repeated = (
        state.repeated_error_count + 1 if fingerprint == state.boot_error_hash else 0
    )
    recovery = await propose_recovery(
        result.logs,
        runtime.context.readme_excerpt,
        set(runtime.context.allowed_env_keys),
        repeated,
        runtime.context.model,
    )
    update["boot_error_hash"] = fingerprint
    update["repeated_error_count"] = repeated
    await _apply_recovery(state, update, recovery, runtime.context)
    return update


async def _apply_recovery(
    state: GraphState,
    update: NodeUpdate,
    recovery: RecoveryAction,
    context: GraphContext,
) -> None:
    if recovery.action == "stop":
        update["run"] = state.run.model_copy(
            update={"stop_reason": "boot_recovery_stopped"}
        )
        return
    if recovery.action == "retry":
        return
    if recovery.action == "wait":
        seconds = recovery.params.get("seconds")
        if type(seconds) is not int:
            raise ValueError("validated wait action is malformed")
        await context.sleep(seconds)
        return
    if recovery.action == "set_env":
        key = recovery.params.get("key")
        value = recovery.params.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("validated set_env action is malformed")
        recovery_env = dict(state.recovery_env)
        recovery_env[key] = value
        update["recovery_env"] = recovery_env
        return
    raise ValueError("validated recovery action is unsupported")


def _boot_route(state: GraphState) -> BootRoute:
    if state.run.stop_reason is not None:
        return "report_or_next"
    if state.boot_verdict is Verdict.PASS:
        return "journeys"
    return "boot"


async def _journeys(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    results = await runtime.context.operations.journeys(
        state.run.model_copy(deep=True), runtime.context.provider
    )
    run = state.run.model_copy(update={"baseline_journey_results": list(results)})
    return {"run": run, "stage_history": _visit(state, "journeys")}


async def _observe(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    observation = await runtime.context.operations.observe(
        state.run.model_copy(deep=True), runtime.context.provider
    )
    run = state.run.model_copy(update={"baseline_observation": observation})
    return {"run": run, "stage_history": _visit(state, "observe")}


def _propose_mutation(state: GraphState) -> NodeUpdate:
    decision = propose_mutation(state.run)
    run = state.run
    if decision.stop_reason is not None:
        run = run.model_copy(update={"stop_reason": decision.stop_reason})
    return {
        "run": run,
        "pending_mutation": decision.mutation,
        "pending_experiment": None,
        "stage_history": _visit(state, "propose_mutation"),
    }


def _mutation_route(state: GraphState) -> MutationRoute:
    return "experiment" if state.pending_mutation is not None else "report_or_next"


async def _experiment(state: GraphState, runtime: Runtime[GraphContext]) -> NodeUpdate:
    mutation = state.pending_mutation
    if mutation is None:
        raise ValueError("experiment requires a pending mutation")
    context = runtime.context
    token = _run_token(state.run.run_id)
    index = len(state.run.experiments)
    experiment_context = ExperimentContext(
        workspace=context.workspace,
        overlay_path=context.overlay_dir / f"{token}-{index:04d}.overlay.yaml",
        artifact_dir=context.artifact_dir,
        env={**context.env, **state.recovery_env},
        container_port=context.container_port,
    )
    record = await run_experiment(
        state.run.model_copy(deep=True),
        mutation,
        context.provider,
        context=experiment_context,
    )
    return {
        "pending_experiment": record,
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
    overlay = _overlay_relative_path(state, runtime.context)
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
        "stage_history": _visit(state, "decide"),
    }


def _report_or_next(state: GraphState) -> NodeUpdate:
    return {"stage_history": _visit(state, "report_or_next")}


def _report_route(state: GraphState) -> ReportRoute:
    return "__end__" if state.run.stop_reason is not None else "propose_mutation"


def _same_run(previous: RunState, returned: object) -> RunState:
    if not isinstance(returned, RunState):
        raise TypeError("stage operation must return RunState")
    if returned.run_id != previous.run_id:
        raise ValueError("stage operation cannot change run_id")
    return returned


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


def _overlay_relative_path(state: GraphState, context: GraphContext) -> str:
    index = len(state.run.experiments)
    path = (
        context.overlay_dir / f"{_run_token(state.run.run_id)}-{index:04d}.overlay.yaml"
    )
    workspace = _real_directory(context.workspace, "workspace")
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
        _REPARSE_POINT and path_stat.st_file_attributes & _REPARSE_POINT
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
