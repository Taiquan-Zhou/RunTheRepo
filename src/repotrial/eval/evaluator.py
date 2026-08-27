import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from langgraph.errors import NodeCancelledError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from repotrial.agent.graph import ainvoke_run, build_run_graph
from repotrial.agent.state import GraphContext, GraphState, StageName
from repotrial.compose.parser import ComposeParseError, load_compose
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import ExperimentRecord, RunState
from repotrial.eval.metrics import (
    boot_recovery_rate,
    cleanup_success_rate,
    hardening_acceptance_precision,
    journey_success_rate,
    replay_consistency,
    unnecessary_privilege_removal_recall,
)
from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider

type MetricValue = float | Literal["unavailable"]
type FixtureStatus = Literal["completed", "failed", "unavailable"]
type GroundTruthMutation = Literal[
    "set_non_root",
    "drop_all_caps",
    "set_read_only",
    "add_tmpfs",
    "drop_privileged",
    "remove_docker_socket",
    "bridge_network",
]
type _FixtureCommandRoute = Literal[
    "compose_up",
    "compose_boot_ps",
    "compose_observer_ps",
    "compose_logs",
    "inspect",
    "diff",
    "top",
]

_BoundedId = Annotated[str, StringConstraints(pattern=r"[a-z0-9][a-z0-9_-]{0,63}")]
_BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=512)]
_MAX_JSON_BYTES = 64 * 1024
_MAX_README_BYTES = 64 * 1024
_MAX_MANIFESTS = 64
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(.*)\Z")
_COMPOSE_ROUTES: dict[tuple[str, ...], _FixtureCommandRoute] = {
    ("up", "-d"): "compose_up",
    ("ps", "--all", "--format", "json"): "compose_boot_ps",
    (
        "ps",
        "--all",
        "--no-trunc",
        "--orphans=false",
        "--format",
        "json",
    ): "compose_observer_ps",
    ("logs", "--no-color", "--tail", "200"): "compose_logs",
}
_TOP_FORMAT = "pid=,ppid=,user=,comm="


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Manifest(_StrictModel):
    schema_version: Literal[1]
    fixture_id: _BoundedId
    compose: _RelativePath
    ground_truth: _RelativePath
    readme: _RelativePath | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("compose", "ground_truth", "readme")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_relative_path_text(value)
        return value


class _GroundTruth(_StrictModel):
    schema_version: Literal[1]
    fixture_id: _BoundedId
    service: _BoundedText
    container_port: int = Field(ge=1, le=65_535)
    journey_path: Annotated[
        str, StringConstraints(pattern=r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}")
    ]
    http_status: int = Field(ge=100, le=599)
    allowed_env_keys: list[_BoundedText] = Field(max_length=16)
    recoverable_missing_env: _BoundedText | None
    writes_tmp: bool
    required_capabilities: list[_BoundedText] = Field(max_length=16)
    expected_keep_mutations: list[GroundTruthMutation] = Field(max_length=16)
    redundant_privileges: list[GroundTruthMutation] = Field(max_length=16)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_ground_truth(self) -> "_GroundTruth":
        collections: Sequence[tuple[str, Sequence[str]]] = (
            ("allowed_env_keys", self.allowed_env_keys),
            ("required_capabilities", self.required_capabilities),
            ("expected_keep_mutations", self.expected_keep_mutations),
            ("redundant_privileges", self.redundant_privileges),
        )
        for label, values in collections:
            if len(values) != len(set(values)):
                raise ValueError(f"{label} contains duplicates")
        if (
            self.recoverable_missing_env is not None
            and self.recoverable_missing_env not in self.allowed_env_keys
        ):
            raise ValueError("recoverable env must be allowlisted")
        if not set(self.redundant_privileges) <= set(self.expected_keep_mutations):
            raise ValueError("redundant privileges must be expected KEEP mutations")
        return self


class ExperimentEvaluation(_StrictModel):
    mutation_type: MutationType
    service: _BoundedText
    verdict: ExperimentVerdict
    reason: _BoundedText


class JourneyEvaluation(_StrictModel):
    journey_id: _BoundedText
    verdict: Verdict | None


class FixtureRunResult(_StrictModel):
    run_index: int = Field(ge=1, le=2)
    status: FixtureStatus
    comparable: bool
    stage_history: list[StageName] = Field(max_length=64)
    boot_attempts: int = Field(ge=0, le=4)
    boot_verdict: Verdict | None
    journeys: list[JourneyEvaluation] = Field(max_length=16)
    experiments: list[ExperimentEvaluation] = Field(max_length=8)
    stop_reason: _BoundedText
    created_runners: int = Field(ge=0, le=32)
    destroyed_runners: int = Field(ge=0, le=32)
    cleanup_successes: list[bool] = Field(max_length=32)
    model_calls: Literal[0] = 0
    allowed_env_keys: list[_BoundedText] = Field(max_length=16)
    recovery_env_keys: list[_BoundedText] = Field(max_length=16)
    unexpected_commands: list[_BoundedText] = Field(max_length=16)
    verdict_projection: list[_BoundedText] = Field(max_length=32)


class FixtureBenchmarkResult(_StrictModel):
    fixture_id: _BoundedId
    status: FixtureStatus
    stop_reason: _BoundedText
    runs: list[FixtureRunResult] = Field(min_length=2, max_length=2)
    replay_consistent: bool | None


class BenchmarkResult(_StrictModel):
    schema_version: Literal["repotrial-eval/v1"] = "repotrial-eval/v1"
    generated_at: _BoundedText
    fixtures: list[FixtureBenchmarkResult] = Field(max_length=_MAX_MANIFESTS)
    metrics: dict[str, MetricValue]


@dataclass(frozen=True, slots=True)
class _LoadedFixture:
    manifest: _Manifest
    ground_truth: _GroundTruth
    compose_path: Path
    readme_excerpt: str


@dataclass(slots=True)
class _SandboxState:
    workspace: Path
    container_id: str
    active_config: dict[str, object] | None = None
    active_compose_files: tuple[str, ...] | None = None
    active_env: dict[str, str] | None = None
    discovered_container_id: str | None = None
    readiness_failure: str | None = None
    server: asyncio.AbstractServer | None = None
    port: int | None = None


@dataclass(frozen=True, slots=True)
class _FixtureCommand:
    route: _FixtureCommandRoute
    compose_files: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


class _FixtureProvider(SandboxProvider):
    """Private trusted harness; it interprets fixed commands and never executes them."""

    def __init__(self, ground_truth: _GroundTruth) -> None:
        self._ground_truth = ground_truth
        self._sandboxes: dict[str, _SandboxState] = {}
        self.created_ids: list[str] = []
        self.destroyed_ids: list[str] = []
        self.unexpected_commands: list[str] = []

    async def create(self, workspace: Path, name: str) -> str:
        del name
        sandbox_id = (
            f"eval-{self._ground_truth.fixture_id}-{len(self.created_ids) + 1:04d}"
        )
        if sandbox_id in self._sandboxes:
            raise ValueError("fixture sandbox identity collision")
        self._sandboxes[sandbox_id] = _SandboxState(
            workspace=workspace,
            container_id=_container_id(sandbox_id),
        )
        self.created_ids.append(sandbox_id)
        return sandbox_id

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        del timeout_s
        sandbox = self._owned_sandbox(sandbox_id)
        command = _route_fixture_command(sandbox, self._ground_truth, argv)
        if command is None:
            return self._unexpected(argv)
        if command.route.startswith("compose_"):
            return self._compose_command(sandbox, command)
        if command.route == "inspect":
            return self._inspect_result(sandbox)
        if command.route == "diff":
            stdout = "C /tmp/repotrial-eval\n" if self._ground_truth.writes_tmp else ""
            return ExecResult(exit_code=0, stdout=stdout, stderr="")
        if command.route == "top":
            return ExecResult(
                exit_code=0,
                stdout="1 0 65532 fixture-app\n",
                stderr="",
            )
        raise RuntimeError("fixture command route is unsupported")

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        sandbox = self._owned_sandbox(sandbox_id)
        if container_port != self._ground_truth.container_port:
            raise ValueError("unexpected fixture container port")
        if sandbox.readiness_failure is not None or sandbox.active_config is None:
            raise ValueError("fixture workload is not ready")
        if sandbox.server is None:
            sandbox.server = await asyncio.start_server(
                lambda reader, writer: self._serve(sandbox, reader, writer),
                "127.0.0.1",
                0,
            )
            sockets = sandbox.server.sockets
            if not sockets:
                raise RuntimeError("fixture server has no listening socket")
            address = sockets[0].getsockname()
            if not isinstance(address, tuple) or type(address[1]) is not int:
                raise RuntimeError("fixture server returned an invalid address")
            sandbox.port = address[1]
        assert sandbox.port is not None
        return sandbox.port

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        del remote_path, local_path
        self._owned_sandbox(sandbox_id)
        raise ValueError("fixture provider copy is unsupported")

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        self._owned_sandbox(sandbox_id)
        return NetworkLogResult(
            supported=True,
            events=[
                {
                    "protocol": "tcp",
                    "destination": "fixture.local:8080",
                    "bytes": 32,
                }
            ],
        )

    async def destroy(self, sandbox_id: str) -> None:
        sandbox = self._owned_sandbox(sandbox_id)
        if sandbox_id in self.destroyed_ids:
            raise ValueError("fixture sandbox was already destroyed")
        if sandbox.server is not None:
            sandbox.server.close()
            await sandbox.server.wait_closed()
        self.destroyed_ids.append(sandbox_id)
        del self._sandboxes[sandbox_id]

    def _owned_sandbox(self, sandbox_id: str) -> _SandboxState:
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise ValueError("unknown fixture sandbox")
        return sandbox

    def _compose_command(
        self, sandbox: _SandboxState, command: _FixtureCommand
    ) -> ExecResult:
        if command.route == "compose_up":
            sandbox.active_config = _effective_service_config(
                sandbox.workspace,
                command.compose_files,
                self._ground_truth.service,
            )
            sandbox.active_compose_files = command.compose_files
            sandbox.active_env = dict(command.env)
            sandbox.discovered_container_id = None
            sandbox.readiness_failure = _readiness_failure(
                self._ground_truth,
                sandbox.active_config,
                command.env,
            )
            return ExecResult(
                exit_code=0 if sandbox.readiness_failure is None else 1,
                stdout="",
                stderr=sandbox.readiness_failure or "",
            )
        if sandbox.active_config is None:
            return ExecResult(exit_code=1, stdout="", stderr="compose not started")
        if command.route == "compose_logs":
            return ExecResult(
                exit_code=0,
                stdout=(sandbox.readiness_failure or "fixture ready") + "\n",
                stderr="",
            )
        row: dict[str, object]
        if command.route == "compose_observer_ps":
            sandbox.discovered_container_id = sandbox.container_id
            row = {
                "Service": self._ground_truth.service,
                "ID": sandbox.discovered_container_id,
            }
        else:
            ready = sandbox.readiness_failure is None
            row = {
                "Service": self._ground_truth.service,
                "State": "running" if ready else "exited",
                "Health": "healthy" if ready else "unhealthy",
                "ExitCode": 0 if ready else 1,
            }
        return ExecResult(
            exit_code=0,
            stdout=json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
            stderr="",
        )

    def _inspect_result(self, sandbox: _SandboxState) -> ExecResult:
        config = sandbox.active_config or {}
        payload: list[dict[str, object]] = [
            {
                "Id": sandbox.container_id,
                "Config": {"User": str(config.get("user", "")), "Env": []},
                "HostConfig": {
                    "Privileged": config.get("privileged") is True,
                    "ReadonlyRootfs": config.get("read_only") is True,
                    "CapDrop": _string_list(config.get("cap_drop")),
                    "Tmpfs": {"/tmp": ""} if _has_tmpfs(config) else {},
                },
            }
        ]
        return ExecResult(
            exit_code=0,
            stdout=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            stderr="",
        )

    async def _serve(
        self,
        sandbox: _SandboxState,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            first_line = request.split(b"\r\n", 1)[0]
            parts = first_line.split()
            path = parts[1].decode("ascii") if len(parts) == 3 else ""
            status = (
                self._ground_truth.http_status
                if path == self._ground_truth.journey_path
                else 404
            )
            reason = "OK" if 200 <= status < 300 else "Fixture Failure"
            body = json.dumps({"fixture": self._ground_truth.fixture_id}).encode()
            writer.write(
                (
                    f"HTTP/1.1 {status} {reason}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, UnicodeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _unexpected(self, argv: Sequence[str]) -> ExecResult:
        material = json.dumps(list(argv), separators=(",", ":"), ensure_ascii=True)
        digest = hashlib.sha256(material.encode()).hexdigest()[:16]
        self.unexpected_commands.append(f"sha256-{digest}")
        return ExecResult(
            exit_code=127, stdout="", stderr="unsupported fixture command"
        )


def evaluate_benchmark(manifest_dir: Path) -> BenchmarkResult:
    fixtures = _load_fixtures(manifest_dir)
    generated_at = _utc_now().isoformat().replace("+00:00", "Z")
    return asyncio.run(_evaluate_loaded_fixtures(fixtures, generated_at))


def write_benchmark_result(result: BenchmarkResult, result_dir: Path) -> Path:
    if not isinstance(result_dir, Path):
        raise TypeError("result_dir must be a Path")
    result_dir.mkdir(parents=True, exist_ok=True)
    directory_stat = result_dir.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or _is_link(result_dir, directory_stat):
        raise ValueError("result_dir must be a real directory")
    generated = datetime.fromisoformat(result.generated_at)
    filename = generated.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ.json")
    destination = result_dir / filename
    serialized = (
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with destination.open("xb") as output:
        output.write(serialized)
    return destination


def run_benchmark(manifest_dir: Path, result_dir: Path) -> Path:
    return write_benchmark_result(evaluate_benchmark(manifest_dir), result_dir)


async def _evaluate_loaded_fixtures(
    fixtures: Sequence[_LoadedFixture], generated_at: str
) -> BenchmarkResult:
    evaluated: list[tuple[_LoadedFixture, FixtureBenchmarkResult]] = []
    for fixture in fixtures:
        runs = [await _execute_fixture_once(fixture, run_index) for run_index in (1, 2)]
        replay = (
            runs[0].verdict_projection == runs[1].verdict_projection
            if all(run.comparable for run in runs)
            else None
        )
        if any(run.status == "unavailable" for run in runs):
            status: FixtureStatus = "unavailable"
        elif all(run.status == "completed" for run in runs):
            status = "completed"
        else:
            status = "failed"
        stop_reason = _fixture_stop_reason(runs)
        evaluated.append(
            (
                fixture,
                FixtureBenchmarkResult(
                    fixture_id=fixture.manifest.fixture_id,
                    status=status,
                    stop_reason=stop_reason,
                    runs=runs,
                    replay_consistent=replay,
                ),
            )
        )
    results = [result for _, result in evaluated]
    return BenchmarkResult(
        generated_at=generated_at,
        fixtures=results,
        metrics=_calculate_metrics(evaluated),
    )


async def _execute_fixture_once(
    fixture: _LoadedFixture, run_index: int
) -> FixtureRunResult:
    provider = _FixtureProvider(fixture.ground_truth)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"repotrial-eval-{fixture.manifest.fixture_id}-"
        ) as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            artifact_dir = root / "artifacts"
            workspace.mkdir()
            artifact_dir.mkdir()
            shutil.copyfile(fixture.compose_path, workspace / "compose.yml")
            context = GraphContext(
                provider=provider,
                workspace=workspace,
                artifact_dir=artifact_dir,
                overlay_dir=workspace / ".repotrial-overlays",
                accepted_compose_dir=workspace / ".repotrial-accepted",
                env={},
                allowed_env_keys=frozenset(fixture.ground_truth.allowed_env_keys),
                readme_excerpt=fixture.readme_excerpt,
                container_port=fixture.ground_truth.container_port,
                model=None,
            )
            state = RunState(
                run_id=f"eval-{fixture.manifest.fixture_id}-{run_index}",
                repo_url=f"fixture://{fixture.manifest.fixture_id}",
                commit_sha="e" * 40,
                compose_path="compose.yml",
            )
            graph_state = await ainvoke_run(build_run_graph(), state, context=context)
        return _project_run(fixture, provider, run_index, graph_state)
    except NodeCancelledError as error:
        if isinstance(error.__cause__, asyncio.CancelledError):
            raise error.__cause__
        raise
    except Exception as error:  # noqa: BLE001 -- Outer adapter retains failures.
        return _failed_fixture_run(fixture, provider, run_index, error)


def _project_run(
    fixture: _LoadedFixture,
    provider: _FixtureProvider,
    run_index: int,
    state: GraphState,
) -> FixtureRunResult:
    run = state.run
    result_by_id = {
        result.journey_id: result for result in run.baseline_journey_results
    }
    journeys = [
        JourneyEvaluation(
            journey_id=journey.journey_id,
            verdict=(
                result_by_id[journey.journey_id].verdict
                if journey.journey_id in result_by_id
                else None
            ),
        )
        for journey in run.journeys
    ]
    experiments = [_experiment_evaluation(record) for record in run.experiments]
    available = _is_available_run(state, journeys, experiments)
    status: FixtureStatus
    if not available:
        status = "unavailable"
    elif _is_completed_run(state):
        status = "completed"
    else:
        status = "failed"
    stop_reason = run.stop_reason or "missing_stop_reason"
    projection = _verdict_projection(state, journeys, experiments)
    cleanup = [
        sandbox_id in provider.destroyed_ids for sandbox_id in provider.created_ids
    ]
    return FixtureRunResult(
        run_index=run_index,
        status=status,
        comparable=available,
        stage_history=list(state.stage_history),
        boot_attempts=state.boot_attempt,
        boot_verdict=state.boot_verdict,
        journeys=journeys,
        experiments=experiments,
        stop_reason=stop_reason,
        created_runners=len(provider.created_ids),
        destroyed_runners=len(provider.destroyed_ids),
        cleanup_successes=cleanup,
        allowed_env_keys=sorted(fixture.ground_truth.allowed_env_keys),
        recovery_env_keys=sorted(state.recovery_env),
        unexpected_commands=list(provider.unexpected_commands),
        verdict_projection=projection,
    )


def _failed_fixture_run(
    fixture: _LoadedFixture,
    provider: _FixtureProvider,
    run_index: int,
    error: BaseException,
) -> FixtureRunResult:
    reason = f"evaluator_error:{_error_token(error)}"
    cleanup = [
        sandbox_id in provider.destroyed_ids for sandbox_id in provider.created_ids
    ]
    return FixtureRunResult(
        run_index=run_index,
        status="unavailable",
        comparable=False,
        stage_history=[],
        boot_attempts=0,
        boot_verdict=None,
        journeys=[],
        experiments=[],
        stop_reason=reason,
        created_runners=len(provider.created_ids),
        destroyed_runners=len(provider.destroyed_ids),
        cleanup_successes=cleanup,
        allowed_env_keys=sorted(fixture.ground_truth.allowed_env_keys),
        recovery_env_keys=[],
        unexpected_commands=list(provider.unexpected_commands),
        verdict_projection=[],
    )


def _experiment_evaluation(record: ExperimentRecord) -> ExperimentEvaluation:
    return ExperimentEvaluation(
        mutation_type=record.mutation.type,
        service=record.mutation.service,
        verdict=record.verdict,
        reason=record.reason,
    )


def _is_completed_run(state: GraphState) -> bool:
    results = {
        result.journey_id: result for result in state.run.baseline_journey_results
    }
    journeys_pass = bool(state.run.journeys) and all(
        results.get(journey.journey_id) is not None
        and results[journey.journey_id].verdict is Verdict.PASS
        for journey in state.run.journeys
    )
    return (
        state.boot_verdict is Verdict.PASS
        and journeys_pass
        and not any(
            record.verdict is ExperimentVerdict.STOP for record in state.run.experiments
        )
        and state.run.stop_reason is not None
    )


def _is_available_run(
    state: GraphState,
    journeys: Sequence[JourneyEvaluation],
    experiments: Sequence[ExperimentEvaluation],
) -> bool:
    boot_outcome = _functional_outcome(state.boot_verdict)
    if boot_outcome is not True:
        return boot_outcome is False
    journey_outcomes = [_functional_outcome(item.verdict) for item in journeys]
    if any(outcome is None for outcome in journey_outcomes):
        return False
    if any(outcome is False for outcome in journey_outcomes):
        return True
    return all(_experiment_outcome(item.verdict) is not None for item in experiments)


def _functional_outcome(verdict: Verdict | None) -> bool | None:
    if verdict is Verdict.PASS:
        return True
    if verdict is Verdict.FAIL:
        return False
    return None


def _experiment_outcome(verdict: ExperimentVerdict) -> bool | None:
    if verdict is ExperimentVerdict.KEEP:
        return True
    if verdict is ExperimentVerdict.ROLLBACK:
        return False
    return None


def _verdict_projection(
    state: GraphState,
    journeys: Sequence[JourneyEvaluation],
    experiments: Sequence[ExperimentEvaluation],
) -> list[str]:
    projection = [
        f"boot_attempts:{state.boot_attempt}",
        f"boot:{state.boot_verdict.value if state.boot_verdict is not None else 'none'}",
    ]
    projection.extend(
        (
            f"journey:{item.journey_id}:"
            f"{item.verdict.value if item.verdict is not None else 'unobserved'}"
        )
        for item in journeys
    )
    projection.extend(
        (
            f"experiment:{item.mutation_type.value}:{item.service}:"
            f"{item.verdict.value}:{item.reason}"
        )
        for item in experiments
    )
    projection.append(f"stop:{state.run.stop_reason or 'missing_stop_reason'}")
    return projection


def _fixture_stop_reason(runs: Sequence[FixtureRunResult]) -> str:
    reasons = {run.stop_reason for run in runs}
    return next(iter(reasons)) if len(reasons) == 1 else "run_stop_reason_mismatch"


def _calculate_metrics(
    evaluated: Sequence[tuple[_LoadedFixture, FixtureBenchmarkResult]],
) -> dict[str, MetricValue]:
    boot_samples: list[bool | None] = []
    journey_samples: list[bool | None] = []
    precision_samples: list[bool | None] = []
    recall_samples: list[bool | None] = []
    cleanup_samples: list[bool | None] = []
    replay_samples: list[bool | None] = []

    for fixture, result in evaluated:
        truth = fixture.ground_truth
        for run in result.runs:
            cleanup_samples.extend(run.cleanup_successes)
            if run.status == "unavailable":
                if truth.recoverable_missing_env is not None:
                    boot_samples.append(None)
                journey_samples.append(None)
                precision_samples.append(None)
                recall_samples.extend(None for _ in truth.redundant_privileges)
                continue
            if truth.recoverable_missing_env is not None:
                boot_outcome = _functional_outcome(run.boot_verdict)
                boot_samples.append(
                    run.boot_attempts > 1 if boot_outcome is True else boot_outcome
                )
            journey_samples.extend(
                _functional_outcome(journey.verdict) for journey in run.journeys
            )
            keep_types = [
                experiment.mutation_type.value
                for experiment in run.experiments
                if experiment.verdict is ExperimentVerdict.KEEP
            ]
            precision_samples.extend(
                mutation in truth.expected_keep_mutations for mutation in keep_types
            )
            recall_samples.extend(
                mutation in keep_types for mutation in truth.redundant_privileges
            )
        replay_samples.append(result.replay_consistent)

    return {
        "boot_recovery_rate": _render_metric(boot_recovery_rate(boot_samples)),
        "journey_success_rate": _render_metric(journey_success_rate(journey_samples)),
        "hardening_acceptance_precision": _render_metric(
            hardening_acceptance_precision(precision_samples)
        ),
        "unnecessary_privilege_removal_recall": _render_metric(
            unnecessary_privilege_removal_recall(recall_samples)
        ),
        "cleanup_success_rate": _render_metric(cleanup_success_rate(cleanup_samples)),
        "replay_consistency": _render_metric(replay_consistency(replay_samples)),
    }


def _render_metric(value: float | None) -> MetricValue:
    return "unavailable" if value is None else value


def _load_fixtures(manifest_dir: Path) -> list[_LoadedFixture]:
    directory = _real_directory(manifest_dir, "manifest directory")
    project_root = _real_directory(directory.parent.parent, "benchmark root")
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    if not entries or len(entries) > _MAX_MANIFESTS:
        raise ValueError("manifest collection size is invalid")
    fixtures: list[_LoadedFixture] = []
    seen_ids: set[str] = set()
    seen_inputs: set[Path] = set()
    for path in entries:
        metadata = path.lstat()
        if (
            path.suffix != ".json"
            or not stat.S_ISREG(metadata.st_mode)
            or _is_link(path, metadata)
        ):
            raise ValueError("manifest directory contains an invalid entry")
        manifest = _read_json_model(path, _Manifest, "manifest")
        compose_path = _project_file(project_root, manifest.compose, "compose")
        ground_truth_path = _project_file(
            project_root, manifest.ground_truth, "ground truth"
        )
        readme_path = (
            _project_file(project_root, manifest.readme, "README")
            if manifest.readme is not None
            else None
        )
        if manifest.fixture_id in seen_ids:
            raise ValueError("duplicate fixture id")
        inputs = [compose_path, ground_truth_path]
        if readme_path is not None:
            inputs.append(readme_path)
        if len(inputs) != len(set(inputs)) or seen_inputs.intersection(inputs):
            raise ValueError("duplicate fixture input")
        seen_ids.add(manifest.fixture_id)
        seen_inputs.update(inputs)
        ground_truth = _read_json_model(ground_truth_path, _GroundTruth, "ground truth")
        if ground_truth.fixture_id != manifest.fixture_id:
            raise ValueError("fixture id mismatch")
        _validate_compose_fixture(compose_path, ground_truth.service)
        readme_excerpt = (
            _read_regular_text(readme_path, _MAX_README_BYTES, "README")
            if readme_path is not None
            else f"[Fixture journey]({ground_truth.journey_path})"
        )
        fixtures.append(
            _LoadedFixture(
                manifest=manifest,
                ground_truth=ground_truth,
                compose_path=compose_path,
                readme_excerpt=readme_excerpt,
            )
        )
    return fixtures


def _read_json_model[ModelT: BaseModel](
    path: Path, model: type[ModelT], label: str
) -> ModelT:
    raw = _read_regular_bytes(path, _MAX_JSON_BYTES, label)
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        return model.model_validate(decoded)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ):
        raise ValueError(f"invalid {label}") from None


def _read_regular_text(path: Path, limit: int, label: str) -> str:
    raw = _read_regular_bytes(path, limit, label)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"invalid {label}") from None


def _read_regular_bytes(path: Path, limit: int, label: str) -> bytes:
    initial = path.lstat()
    if (
        not stat.S_ISREG(initial.st_mode)
        or _is_link(path, initial)
        or initial.st_size > limit
    ):
        raise ValueError(f"invalid {label}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_link(path, current)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_size > limit
            or not _same_identity(initial, opened)
            or not _same_identity(opened, current)
        ):
            raise ValueError(f"invalid {label}")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = None
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"invalid {label}")
        return raw
    except OSError:
        raise ValueError(f"invalid {label}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _project_file(project_root: Path, value: str, label: str) -> Path:
    _validate_relative_path_text(value)
    relative = Path(value)
    target = project_root
    try:
        for component in relative.parts:
            target /= component
            metadata = target.lstat()
            if _is_link(target, metadata):
                raise ValueError(f"invalid {label}")
        resolved = target.resolve(strict=True)
    except OSError:
        raise ValueError(f"invalid {label}") from None
    if not resolved.is_relative_to(project_root) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"invalid {label}")
    return resolved


def _validate_relative_path_text(value: str) -> None:
    if (
        not value
        or "\\" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("invalid relative path")
    relative = Path(value)
    if (
        relative.is_absolute()
        or relative.drive
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise ValueError("invalid relative path")


def _validate_compose_fixture(path: Path, service: str) -> None:
    try:
        compose = load_compose(path)
    except ComposeParseError:
        raise ValueError("invalid compose fixture") from None
    services = compose.get("services")
    if not isinstance(services, Mapping) or service not in services:
        raise ValueError("ground truth service is missing from compose")


def _real_directory(path: object, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"{label} must be a real directory") from None
    if not stat.S_ISDIR(metadata.st_mode) or _is_link(path, metadata):
        raise ValueError(f"{label} must be a real directory")
    return resolved


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError("duplicate JSON key")
        decoded[key] = value
    return decoded


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _is_link(path: Path, metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(reparse and attributes & reparse)


def _route_fixture_command(
    sandbox: _SandboxState,
    truth: _GroundTruth,
    argv: Sequence[str],
) -> _FixtureCommand | None:
    if not argv or any(not isinstance(item, str) for item in argv):
        raise ValueError("fixture provider requires argv-style commands")
    command = list(argv)
    env: dict[str, str] = {}
    has_env_prefix = command[0] == "env"
    if has_env_prefix:
        try:
            docker_index = command.index("docker", 1)
        except ValueError:
            return None
        assignments = command[1:docker_index]
        if not assignments:
            return None
        for assignment in assignments:
            if _ENV_ASSIGNMENT.fullmatch(assignment) is None:
                return None
            key, value = assignment.split("=", 1)
            if key not in truth.allowed_env_keys or key in env:
                return None
            env[key] = value
        command = command[docker_index:]

    if command[:2] == ["docker", "compose"]:
        files: list[str] = []
        index = 2
        while index < len(command) and command[index] == "-f":
            if index + 1 >= len(command):
                return None
            files.append(command[index + 1])
            index += 2
        if len(files) not in {1, 2} or len(files) != len(set(files)):
            return None
        route = _COMPOSE_ROUTES.get(tuple(command[index:]))
        if route is None:
            return None
        compose_files = tuple(files)
        if route == "compose_observer_ps":
            if has_env_prefix or compose_files != sandbox.active_compose_files:
                return None
        elif route != "compose_up" and (
            compose_files != sandbox.active_compose_files or env != sandbox.active_env
        ):
            return None
        return _FixtureCommand(
            route=route,
            compose_files=compose_files,
            env=env,
        )

    if has_env_prefix or sandbox.discovered_container_id is None:
        return None
    container_id = sandbox.discovered_container_id
    if command == ["docker", "inspect", container_id]:
        return _FixtureCommand(route="inspect")
    if command == ["docker", "diff", container_id]:
        return _FixtureCommand(route="diff")
    if command == ["docker", "top", container_id, "-eo", _TOP_FORMAT]:
        return _FixtureCommand(route="top")
    return None


def _effective_service_config(
    workspace: Path, compose_files: Sequence[str], service: str
) -> dict[str, object]:
    if not compose_files:
        raise ValueError("compose invocation has no file")
    effective: dict[str, object] = {}
    for relative_text in compose_files:
        _validate_relative_path_text(relative_text)
        path = workspace / Path(relative_text)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise ValueError("fixture compose path is invalid") from None
        if not resolved.is_relative_to(workspace):
            raise ValueError("fixture compose path escapes workspace")
        compose = load_compose(resolved)
        services = compose.get("services")
        if not isinstance(services, Mapping):
            raise TypeError("fixture compose services are invalid")
        definition = services.get(service)
        if not isinstance(definition, Mapping):
            raise TypeError("fixture service is missing")
        for key, value in definition.items():
            if not isinstance(key, str):
                raise TypeError("fixture service key is invalid")
            effective[key] = value
    return effective


def _readiness_failure(
    truth: _GroundTruth,
    config: Mapping[str, object],
    env: Mapping[str, str],
) -> str | None:
    missing = truth.recoverable_missing_env
    if missing is not None and missing not in env:
        return f"{missing} is required"
    if truth.required_capabilities and "ALL" in _string_list(config.get("cap_drop")):
        return "required capability missing"
    if truth.writes_tmp and config.get("read_only") is True and not _has_tmpfs(config):
        return "read-only filesystem blocks /tmp"
    return None


def _has_tmpfs(config: Mapping[str, object]) -> bool:
    value = config.get("tmpfs")
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = [item for item in value if isinstance(item, str)]
    else:
        values = []
    return any(item.split(":", 1)[0] == "/tmp" for item in values)


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, str)]
    return []


def _container_id(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _error_token(error: BaseException) -> str:
    name = type(error).__name__
    token = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return token[:64] or "unknown"


def _utc_now() -> datetime:
    return datetime.now(UTC)
