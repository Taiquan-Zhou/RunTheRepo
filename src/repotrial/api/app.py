"""Minimal synchronous FastAPI control plane."""

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repotrial.agent.checkpoint import build_checkpointer
from repotrial.agent.graph import RunGraph, ainvoke_run, build_run_graph
from repotrial.agent.state import GraphContext, GraphState
from repotrial.config import generate_run_id
from repotrial.domain.models import RunState
from repotrial.intake.github import RepoIntakeError, parse_github_url, pin_repository
from repotrial.models.base import ModelAdapter
from repotrial.models.openai_compat import (
    ModelAdapterError,
    OpenAICompatibleModelAdapter,
)
from repotrial.report.render import TrialReportPaths, render_trial_report
from repotrial.run_outcome import TerminalOutcome, classify_terminal_outcome
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import (
    DockerSbxPolicy,
    DockerSbxProvider,
    DockerSbxUnsupportedError,
)

from .registry import (
    InMemoryRunRegistry,
    PostgresRunRegistry,
    RegistryUnavailableError,
    RunLifecycle,
    RunRecord,
    RunRegistry,
)

_MAX_REPO_URL_LENGTH = 2_048
_MAX_MODEL_ENDPOINT_LENGTH = 2_048
_MAX_MODEL_NAME_LENGTH = 256
_DEFAULT_POLICY = DockerSbxPolicy(
    cpus=1.5, memory_mb=512, pids_limit=64, disk_mb=2048, total_duration_s=300
)

type ProviderFactory = Callable[[], SandboxProvider]
type GraphRunner = Callable[[RunGraph, RunState, GraphContext], Awaitable[GraphState]]
type ReportRenderer = Callable[[RunState, Path], TrialReportPaths]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: Annotated[str, Field(min_length=1, max_length=_MAX_REPO_URL_LENGTH)]
    model_endpoint: Annotated[
        str | None, Field(default=None, max_length=_MAX_MODEL_ENDPOINT_LENGTH)
    ]
    model_name: Annotated[
        str | None, Field(default=None, max_length=_MAX_MODEL_NAME_LENGTH)
    ]

    @field_validator("repo_url")
    @classmethod
    def _github_url(cls, value: str) -> str:
        try:
            parse_github_url(value)
        except RepoIntakeError:
            raise ValueError("must be a GitHub HTTPS owner/repository URL") from None
        return value

    @model_validator(mode="after")
    def _model_pair(self) -> "RunRequest":
        if (self.model_endpoint is None) != (self.model_name is None):
            raise ValueError("model_endpoint and model_name must be provided together")
        if self.model_endpoint is not None:
            OpenAICompatibleModelAdapter(self.model_endpoint, self.model_name or "")
        return self


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    outcome: str


class RunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    repo_url: str
    lifecycle: str
    commit_sha: str | None
    report_available: bool


class _Services:
    def __init__(self, graph: RunGraph | None) -> None:
        self.graph = graph


def create_app(
    *,
    artifacts_root: Path = Path("artifacts"),
    database_url: str | None = None,
    registry: RunRegistry | None = None,
    run_id_generator: Callable[[], str] = generate_run_id,
    provider_factory: ProviderFactory | None = None,
    graph_runner: GraphRunner | None = None,
    report_renderer: ReportRenderer = render_trial_report,
) -> FastAPI:
    selected_database_url = database_url or os.environ.get("REPOTRIAL_DATABASE_URL")
    selected_registry = registry or (
        PostgresRunRegistry(selected_database_url)
        if selected_database_url is not None
        else InMemoryRunRegistry(available=False)
    )
    selected_provider_factory = provider_factory or _default_provider
    selected_graph_runner = graph_runner or _run_graph

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = _Services(None)
        try:
            await selected_registry.initialize()
            async with build_checkpointer(selected_database_url) as checkpointer:
                app.state.services = _Services(
                    build_run_graph(checkpointer=checkpointer)
                )
                await selected_registry.interrupt_running()
                yield
        except asyncio.CancelledError:
            raise
        except (RegistryUnavailableError, RuntimeError, ValueError):
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        if app.state.services.graph is None or not await _registry_healthy(
            selected_registry
        ):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return {"status": "ok"}

    @app.post("/runs", status_code=status.HTTP_201_CREATED, response_model=RunResponse)
    async def create_run(request: RunRequest) -> RunResponse:
        services: _Services = app.state.services
        if services.graph is None or not await _registry_healthy(selected_registry):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        run_id = run_id_generator()
        try:
            await selected_registry.create_running(run_id, request.repo_url)
        except asyncio.CancelledError:
            raise
        except RegistryUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            ) from None

        try:
            run_path = _create_run_layout(artifacts_root, run_id)
            model = _model_from_request(request)
            state = RunState(run_id=run_id, repo_url=request.repo_url)
            graph_state = await selected_graph_runner(
                services.graph,
                state,
                _graph_context(run_path, model, selected_provider_factory()),
            )
            final_state = graph_state.run
            if not isinstance(final_state, RunState):
                raise TypeError("graph returned an invalid final state")
            report_renderer(final_state, run_path / "report")
            classified = classify_terminal_outcome(final_state)
            lifecycle = _lifecycle_for(classified.outcome)
            await selected_registry.complete(
                run_id,
                lifecycle,
                commit_sha=final_state.commit_sha,
                report_available=True,
            )
            return RunResponse(run_id=run_id, outcome=classified.outcome.value)
        except asyncio.CancelledError:
            raise
        except DockerSbxUnsupportedError:
            await _complete_safely(selected_registry, run_id, RunLifecycle.UNSUPPORTED)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"run_id": run_id, "outcome": "execution_unsupported"},
            ) from None
        except (
            ModelAdapterError,
            OSError,
            RepoIntakeError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            await _complete_safely(
                selected_registry, run_id, RunLifecycle.INTERNAL_ERROR
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"run_id": run_id, "outcome": "internal_error"},
            ) from None

    @app.get("/runs/{run_id}", response_model=RunMetadata)
    async def get_run(run_id: str) -> RunMetadata:
        record = await _record_or_404(selected_registry, run_id)
        return RunMetadata(
            run_id=record.run_id,
            repo_url=record.repo_url,
            lifecycle=record.lifecycle.value,
            commit_sha=record.commit_sha,
            report_available=record.report_available,
        )

    @app.get("/runs/{run_id}/report")
    async def get_report(run_id: str, request: Request) -> FileResponse:
        record = await _record_or_404(selected_registry, run_id)
        if not record.report_available:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        representation = _representation(request.headers.get("accept"))
        if representation is None:
            raise HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE)
        report_path = artifacts_root / record.run_id / "report" / representation
        if not _safe_regular_file(report_path, artifacts_root):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        return FileResponse(
            report_path,
            media_type="text/html"
            if representation.endswith("html")
            else "application/json",
        )

    return app


async def _run_graph(
    graph: RunGraph, state: RunState, context: GraphContext
) -> GraphState:
    return await ainvoke_run(graph, state, context=context)


def _default_provider() -> SandboxProvider:
    return DockerSbxProvider(_DEFAULT_POLICY)


def _model_from_request(request: RunRequest) -> ModelAdapter | None:
    if request.model_endpoint is None:
        return None
    return OpenAICompatibleModelAdapter(
        request.model_endpoint,
        request.model_name or "",
        api_key=os.environ.get("REPOTRIAL_MODEL_API_KEY"),
    )


def _graph_context(
    run_path: Path, model: ModelAdapter | None, provider: SandboxProvider
) -> GraphContext:
    workspace = run_path / "workspace"
    return GraphContext(
        provider=provider,
        workspace=workspace,
        artifact_dir=run_path / "evidence",
        overlay_dir=workspace / ".repotrial-overlays",
        accepted_compose_dir=workspace / ".repotrial-accepted",
        env={},
        allowed_env_keys=frozenset(),
        readme_excerpt="",
        container_port=8080,
        model=model,
        repository_pinner=pin_repository,
    )


def _create_run_layout(artifacts_root: Path, run_id: str) -> Path:
    run_path = artifacts_root / run_id
    run_path.mkdir(parents=True, exist_ok=False)
    for name in ("evidence", "experiments", "report", "workspace"):
        (run_path / name).mkdir()
    return run_path


async def _registry_healthy(registry: RunRegistry) -> bool:
    try:
        return await registry.health()
    except asyncio.CancelledError:
        raise
    except RegistryUnavailableError:
        return False


async def _record_or_404(registry: RunRegistry, run_id: str) -> RunRecord:
    try:
        record = await registry.get(run_id)
    except asyncio.CancelledError:
        raise
    except RegistryUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from None
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return record


async def _complete_safely(
    registry: RunRegistry, run_id: str, lifecycle: RunLifecycle
) -> None:
    try:
        await registry.complete(
            run_id, lifecycle, commit_sha=None, report_available=False
        )
    except asyncio.CancelledError:
        raise
    except RegistryUnavailableError:
        return


def _lifecycle_for(outcome: TerminalOutcome) -> RunLifecycle:
    return {
        TerminalOutcome.COMPLETED: RunLifecycle.COMPLETED,
        TerminalOutcome.EXECUTION_UNSUPPORTED: RunLifecycle.UNSUPPORTED,
        TerminalOutcome.TRIAL_FAILED: RunLifecycle.TRIAL_FAILED,
    }[outcome]


def _representation(accept: str | None) -> str | None:
    if accept is None or "*/*" in accept or "application/json" in accept:
        return "trial-report.json"
    if "text/html" in accept:
        return "trial-report.html"
    return None


def _safe_regular_file(path: Path, artifacts_root: Path) -> bool:
    try:
        root = artifacts_root.resolve(strict=True)
        candidate = path.resolve(strict=True)
        mode = path.lstat().st_mode
    except OSError:
        return False
    return (
        candidate.is_relative_to(root) and stat.S_ISREG(mode) and not path.is_symlink()
    )
