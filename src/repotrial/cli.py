import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

import typer

from repotrial.agent.graph import ainvoke_run, build_run_graph
from repotrial.agent.state import GraphContext, RepositoryPinner
from repotrial.config import create_run_layout, generate_run_id
from repotrial.domain.enums import ExperimentVerdict, Verdict
from repotrial.domain.models import PinnedRepo, RepoRef, RunState
from repotrial.intake.github import (
    RepoIntakeError,
    clone_and_resolve,
    parse_github_url,
    pin_repository,
)
from repotrial.models.base import ModelAdapter
from repotrial.report.render import render_trial_report
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import (
    DockerSbxPolicy,
    DockerSbxProvider,
    DockerSbxUnsupportedError,
)

type ProviderFactory = Callable[[Literal["fake", "docker-sbx"]], SandboxProvider]

_FROZEN_MAX_EXPERIMENTS = 8
_DEFAULT_CONTAINER_PORT = 8080
_DEFAULT_DOCKER_SBX_POLICY = DockerSbxPolicy(
    cpus=1.5,
    memory_mb=512,
    pids_limit=64,
    disk_mb=2048,
    total_duration_s=300,
)


def create_app(
    artifacts_root: Path = Path("artifacts"),
    run_id_generator: Callable[[], str] = generate_run_id,
    provider_factory: ProviderFactory | None = None,
    model: ModelAdapter | None = None,
) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def doctor() -> None:
        typer.echo("ok")

    @app.command()
    def inspect(
        url: str,
        dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
        provider: Annotated[str | None, typer.Option("--provider")] = None,
        max_experiments: Annotated[
            int, typer.Option("--max-experiments")
        ] = _FROZEN_MAX_EXPERIMENTS,
    ) -> None:
        if dry_run:
            _validate_github_url(url)
            run_id, run_path = create_run_layout(artifacts_root, run_id_generator)
            typer.echo(f"run_id={run_id}")
            typer.echo(f"artifact_path={run_path}")
            return
        if max_experiments != _FROZEN_MAX_EXPERIMENTS:
            raise typer.BadParameter(
                f"--max-experiments must be {_FROZEN_MAX_EXPERIMENTS}"
            )
        selected_provider = _validate_provider(provider)
        if selected_provider == "fake" and provider_factory is None:
            typer.echo(
                "--provider fake is unavailable without test injection", err=True
            )
            raise typer.Exit(2)
        repository_pinner = _repository_pinner_for(url)

        try:
            run_id, run_path = create_run_layout(artifacts_root, run_id_generator)
            workspace = run_path / "workspace"
            typer.echo(f"run_id={run_id}")
            typer.echo(f"artifact_path={run_path}")
            result = asyncio.run(
                ainvoke_run(
                    build_run_graph(),
                    RunState(run_id=run_id, repo_url=url),
                    context=GraphContext(
                        provider=_make_provider(selected_provider, provider_factory),
                        workspace=workspace,
                        artifact_dir=run_path / "evidence",
                        overlay_dir=workspace / ".repotrial-overlays",
                        accepted_compose_dir=workspace / ".repotrial-accepted",
                        env={},
                        allowed_env_keys=frozenset(),
                        readme_excerpt="",
                        container_port=_DEFAULT_CONTAINER_PORT,
                        model=model,
                        repository_pinner=repository_pinner,
                    ),
                )
            )
            report_paths = render_trial_report(result.run, run_path / "report")
        except DockerSbxUnsupportedError:
            raise typer.Exit(2) from None
        except Exception as error:
            typer.echo(f"inspect failed: {type(error).__name__}", err=True)
            raise typer.Exit(4) from error
        typer.echo(f"report_json={report_paths.json_path}")
        typer.echo(f"report_html={report_paths.html_path}")
        raise typer.Exit(_exit_code(result.run))

    return app


def _validate_github_url(url: str) -> None:
    try:
        parse_github_url(url)
    except RepoIntakeError:
        raise typer.BadParameter(
            "URL must be a GitHub HTTPS owner/repository URL"
        ) from None


def _validate_provider(provider: str | None) -> Literal["fake", "docker-sbx"]:
    if provider == "fake":
        return "fake"
    if provider == "docker-sbx":
        return "docker-sbx"
    raise typer.BadParameter("--provider must be fake or docker-sbx")


def _make_provider(
    provider_name: Literal["fake", "docker-sbx"],
    provider_factory: ProviderFactory | None,
) -> SandboxProvider:
    if provider_factory is not None:
        return provider_factory(provider_name)
    if provider_name == "docker-sbx":
        return DockerSbxProvider(_DEFAULT_DOCKER_SBX_POLICY)
    raise typer.BadParameter(
        "fake provider is available only through dependency injection"
    )


def _repository_pinner_for(url: str) -> RepositoryPinner:
    local_path = Path(url)
    if local_path.exists():
        return _local_repository_pinner(local_path)
    _validate_github_url(url)
    return pin_repository


def _local_repository_pinner(source: Path) -> RepositoryPinner:
    resolved_source = source.resolve(strict=True)

    async def pin_local(
        url: str, destination: Path, requested_ref: str | None
    ) -> PinnedRepo:
        del url
        commit_sha, local_path = await clone_and_resolve(
            str(resolved_source), destination, requested_ref=requested_ref
        )
        return PinnedRepo(
            repo=RepoRef(
                url=str(resolved_source),
                owner="local",
                repo=resolved_source.name,
                requested_ref=requested_ref,
            ),
            commit_sha=commit_sha,
            local_path=local_path,
        )

    return pin_local


def _exit_code(run: RunState) -> int:
    if run.stop_reason == "boot_unsupported" or any(
        record.boot is Verdict.UNSUPPORTED for record in run.experiments
    ):
        return 2
    if any(
        result.verdict is Verdict.UNSUPPORTED for result in run.baseline_journey_results
    ) or any(
        result.verdict is Verdict.UNSUPPORTED
        for record in run.experiments
        for result in record.journeys
    ):
        return 2
    baseline_passed = bool(run.baseline_journey_results) and all(
        result.verdict is Verdict.PASS for result in run.baseline_journey_results
    )
    experiment_stopped = any(
        record.verdict is ExperimentVerdict.STOP for record in run.experiments
    )
    if run.stop_reason is not None and baseline_passed and not experiment_stopped:
        return 0
    return 3


app = create_app()
