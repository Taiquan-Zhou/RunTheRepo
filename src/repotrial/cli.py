from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import typer

from repotrial.agent.graph import ainvoke_run, build_run_graph
from repotrial.agent.state import GraphContext, RepositoryPinner
from repotrial.config import create_run_layout, generate_run_id
from repotrial.domain.models import PinnedRepo, RepoRef, RunState
from repotrial.intake.github import (
    RepoIntakeError,
    clone_and_resolve,
    parse_github_url,
    pin_repository,
)
from repotrial.models.base import ModelAdapter
from repotrial.models.openai_compat import OpenAICompatibleModelAdapter
from repotrial.report.render import TrialReportPaths, render_trial_report
from repotrial.run_outcome import classify_terminal_outcome
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import (
    PID_HARD_BOUND_LIMITATION,
    DockerSbxError,
    DockerSbxPolicy,
    DockerSbxProvider,
    DockerSbxUnsupportedError,
)

type ProviderFactory = Callable[[Literal["fake", "docker-sbx"]], SandboxProvider]

_FROZEN_MAX_EXPERIMENTS = 8
_DEFAULT_CONTAINER_PORT = 8080
_FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ATTEMPT_EVIDENCE_SCHEMA_VERSION = 1
_INTAKE_FAILURE_EVIDENCE_SCHEMA_VERSION = 1
type _IntakeEvidenceStatus = Literal["written", "collision", "write_failed"]
_INTAKE_FAILURE_CLASSES = frozenset(
    {
        "dns",
        "transport",
        "http",
        "authentication",
        "missing_ref",
        "local_io",
        "unknown",
    }
)
_DEFAULT_DOCKER_SBX_POLICY = DockerSbxPolicy(
    cpus=1,
    memory_mb=1024,
    pids_limit=64,
    disk_mb=8192,
    total_duration_s=900,
)


def create_app(
    artifacts_root: Path = Path("artifacts"),
    run_id_generator: Callable[[], str] = generate_run_id,
    provider_factory: ProviderFactory | None = None,
    model: ModelAdapter | None = None,
) -> typer.Typer:
    # Keep validation errors readable in captured and non-interactive output.
    # Typer forces Rich terminal rendering in GitHub Actions, where narrow
    # capture widths can crop the actual error message to an ellipsis.
    app = typer.Typer(rich_markup_mode=None)

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
        commit_sha: Annotated[str | None, typer.Option("--commit-sha")] = None,
        container_port: Annotated[
            int, typer.Option("--container-port")
        ] = _DEFAULT_CONTAINER_PORT,
        compose_path: Annotated[str | None, typer.Option("--compose-path")] = None,
        model_endpoint: Annotated[str | None, typer.Option("--model-endpoint")] = None,
        model_name: Annotated[str | None, typer.Option("--model-name")] = None,
    ) -> None:
        if max_experiments != _FROZEN_MAX_EXPERIMENTS:
            raise typer.BadParameter(
                f"--max-experiments must be {_FROZEN_MAX_EXPERIMENTS}"
            )
        _validate_commit_sha(commit_sha)
        _validate_container_port(container_port)
        configured_model = _configured_model(model, model_endpoint, model_name)
        if dry_run:
            _validate_github_url(url)
            dry_run_id, dry_run_path = create_run_layout(
                artifacts_root, run_id_generator
            )
            typer.echo(f"run_id={dry_run_id}")
            typer.echo(f"artifact_path={dry_run_path}")
            return
        selected_provider = _validate_provider(provider)
        if selected_provider == "docker-sbx" and commit_sha is None:
            raise typer.BadParameter(
                "--commit-sha is required for non-dry-run docker-sbx inspection"
            )
        if selected_provider == "fake" and provider_factory is None:
            typer.echo(
                "--provider fake is unavailable without test injection", err=True
            )
            raise typer.Exit(2)
        actual_verified_sha: str | None = None

        def record_verified_sha(value: str) -> None:
            nonlocal actual_verified_sha
            actual_verified_sha = value

        repository_pinner = _repository_pinner_for(
            url,
            expected_commit_sha=commit_sha,
            on_pinned=record_verified_sha,
        )
        run_id: str | None = None
        run_path: Path | None = None
        attempt_start: _AttemptStart | None = None

        try:
            run_id, run_path = create_run_layout(artifacts_root, run_id_generator)
            attempt_start = _begin_attempt_timing()
            if attempt_start is None:
                _exit_after_terminal_evidence(
                    run_id,
                    run_path,
                    _timing_failure_evidence(
                        repo_url=url,
                        expected_sha=commit_sha,
                        actual_verified_sha=actual_verified_sha,
                        container_port=container_port,
                        compose_path=compose_path,
                        exit_code=4,
                        timing=_timing_error(),
                    ),
                    4,
                )
            context_run_path = run_path.resolve(strict=True)
            workspace = context_run_path / "workspace"
            typer.echo(f"run_id={run_id}")
            typer.echo(f"artifact_path={run_path}")
            result = asyncio.run(
                ainvoke_run(
                    build_run_graph(),
                    RunState(
                        run_id=run_id,
                        repo_url=url,
                        compose_path=compose_path,
                    ),
                    context=GraphContext(
                        provider=_make_provider(selected_provider, provider_factory),
                        workspace=workspace,
                        artifact_dir=context_run_path / "evidence",
                        overlay_dir=workspace / ".repotrial-overlays",
                        accepted_compose_dir=workspace / ".repotrial-accepted",
                        env={},
                        allowed_env_keys=frozenset(),
                        readme_excerpt="",
                        container_port=container_port,
                        model=configured_model,
                        repository_pinner=repository_pinner,
                    ),
                )
            )
            stop_reason = _required_terminal_stop_reason(result.run.stop_reason)
            report_paths = render_trial_report(result.run, run_path / "report")
            terminal = classify_terminal_outcome(result.run)
            timing = _complete_attempt_timing(attempt_start)
            if timing.timing_status == "evidence_timing_error":
                _exit_after_terminal_evidence(
                    run_id,
                    run_path,
                    _timing_failure_evidence(
                        repo_url=result.run.repo_url,
                        expected_sha=commit_sha,
                        actual_verified_sha=(
                            actual_verified_sha or result.run.commit_sha
                        ),
                        container_port=container_port,
                        compose_path=compose_path,
                        exit_code=4,
                        timing=timing,
                        report_paths=report_paths,
                    ),
                    4,
                )
            evidence_path = _persist_attempt_evidence(
                run_path,
                _terminal_evidence(
                    run_id=run_id,
                    repo_url=result.run.repo_url,
                    expected_sha=commit_sha,
                    actual_verified_sha=(actual_verified_sha or result.run.commit_sha),
                    container_port=container_port,
                    compose_path=compose_path,
                    exit_code=terminal.exit_code,
                    terminal_outcome=terminal.outcome.value,
                    stop_reason=stop_reason,
                    report_paths=report_paths,
                    timing=timing,
                ),
            )
        except DockerSbxUnsupportedError as error:
            if run_path is None:
                typer.echo(f"inspect failed: {type(error).__name__}", err=True)
                raise typer.Exit(2) from None
            timing = _complete_attempt_timing(attempt_start)
            exit_code = 4 if timing.timing_status == "evidence_timing_error" else 2
            _exit_after_terminal_evidence(
                run_id,
                run_path,
                _exception_evidence(
                    error,
                    repo_url=url,
                    expected_sha=commit_sha,
                    actual_verified_sha=actual_verified_sha,
                    container_port=container_port,
                    compose_path=compose_path,
                    exit_code=exit_code,
                    timing=timing,
                ),
                exit_code,
            )
        except typer.Exit:
            raise
        except BaseException as error:
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                if run_path is not None:
                    timing = _complete_attempt_timing(attempt_start)
                    _persist_control_flow_evidence(
                        error,
                        run_id=run_id,
                        run_path=run_path,
                        repo_url=url,
                        expected_sha=commit_sha,
                        actual_verified_sha=actual_verified_sha,
                        container_port=container_port,
                        compose_path=compose_path,
                        timing=timing,
                    )
                raise
            if not isinstance(error, Exception):
                raise
            if run_path is None:
                typer.echo(f"inspect failed: {type(error).__name__}", err=True)
                raise typer.Exit(4) from None
            timing = _complete_attempt_timing(attempt_start)
            evidence = _exception_evidence(
                error,
                repo_url=url,
                expected_sha=commit_sha,
                actual_verified_sha=actual_verified_sha,
                container_port=container_port,
                compose_path=compose_path,
                exit_code=4,
                timing=timing,
            )
            if isinstance(error, RepoIntakeError):
                assert run_id is not None
                evidence["private_intake_evidence_status"] = (
                    _persist_intake_failure_evidence(
                        run_path,
                        run_id=run_id,
                        error=error,
                    )
                )
            _exit_after_terminal_evidence(
                run_id,
                run_path,
                evidence,
                4,
            )
        typer.echo(f"report_json={report_paths.json_path}")
        typer.echo(f"report_html={report_paths.html_path}")
        typer.echo(f"attempt_evidence={evidence_path}")
        raise typer.Exit(terminal.exit_code)

    return app


def _validate_github_url(url: str) -> None:
    try:
        parse_github_url(url)
    except RepoIntakeError:
        raise typer.BadParameter(
            "URL must be a GitHub HTTPS owner/repository URL"
        ) from None


def _validate_commit_sha(commit_sha: str | None) -> None:
    if commit_sha is not None and _FULL_COMMIT_SHA.fullmatch(commit_sha) is None:
        raise typer.BadParameter(
            "--commit-sha must be a lowercase 40-character hex SHA"
        )


def _validate_container_port(container_port: int) -> None:
    if (
        isinstance(container_port, bool)
        or not isinstance(container_port, int)
        or not 1 <= container_port <= 65_535
    ):
        raise typer.BadParameter("--container-port must be an integer from 1 to 65535")


def _configured_model(
    injected_model: ModelAdapter | None,
    model_endpoint: str | None,
    model_name: str | None,
) -> ModelAdapter | None:
    if model_endpoint is None and model_name is None:
        return injected_model
    if model_endpoint is None or model_name is None:
        raise typer.BadParameter(
            "--model-endpoint and --model-name must be provided together"
        )
    return OpenAICompatibleModelAdapter(
        model_endpoint,
        model_name,
        api_key=os.environ.get("REPOTRIAL_MODEL_API_KEY"),
    )


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


def _repository_pinner_for(
    url: str,
    *,
    expected_commit_sha: str | None,
    on_pinned: Callable[[str], None],
) -> RepositoryPinner:
    local_path = Path(url)
    if local_path.exists():
        pinner = _local_repository_pinner(local_path)
    else:
        _validate_github_url(url)
        pinner = pin_repository

    async def pin_expected(
        repository_url: str, destination: Path, requested_ref: str | None
    ) -> PinnedRepo:
        pinned = await pinner(
            repository_url,
            destination,
            expected_commit_sha if expected_commit_sha is not None else requested_ref,
        )
        on_pinned(pinned.commit_sha)
        if expected_commit_sha is not None and pinned.commit_sha != expected_commit_sha:
            raise RepoIntakeError("commit_sha_mismatch")
        return pinned

    return pin_expected


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


def _terminal_evidence(
    *,
    run_id: str,
    repo_url: str,
    expected_sha: str | None,
    actual_verified_sha: str | None,
    container_port: int,
    compose_path: str | None,
    exit_code: int,
    terminal_outcome: str,
    stop_reason: str | None,
    report_paths: TrialReportPaths,
    timing: _AttemptTiming,
) -> dict[str, object]:
    return {
        "actual_verified_sha": actual_verified_sha,
        "compose_path": compose_path,
        "container_port": container_port,
        "exit_code": exit_code,
        "expected_sha": expected_sha,
        "known_limitations": [PID_HARD_BOUND_LIMITATION],
        "monotonic_duration_s": timing.monotonic_duration_s,
        "report_paths": {
            "html": str(report_paths.html_path),
            "json": str(report_paths.json_path),
        },
        "repository_url": repo_url,
        "run_id": run_id,
        "schema_version": _ATTEMPT_EVIDENCE_SCHEMA_VERSION,
        "started_at_utc": timing.started_at_utc,
        "stop_reason": stop_reason,
        "terminal_outcome": terminal_outcome,
        "ended_at_utc": timing.ended_at_utc,
        "timing_status": timing.timing_status,
    }


def _exception_evidence(
    error: BaseException,
    *,
    repo_url: str,
    expected_sha: str | None,
    actual_verified_sha: str | None,
    container_port: int,
    compose_path: str | None,
    exit_code: int,
    timing: _AttemptTiming,
) -> dict[str, object]:
    return {
        "actual_verified_sha": actual_verified_sha,
        "compose_path": compose_path,
        "container_port": container_port,
        "exception_type": type(error).__name__,
        "exit_code": exit_code,
        "expected_sha": expected_sha,
        "known_limitations": [PID_HARD_BOUND_LIMITATION],
        "monotonic_duration_s": timing.monotonic_duration_s,
        "report_paths": {"html": None, "json": None},
        "repository_url": repo_url,
        "schema_version": _ATTEMPT_EVIDENCE_SCHEMA_VERSION,
        "started_at_utc": timing.started_at_utc,
        "stop_reason": _sanitized_exception_stop_reason(error),
        "terminal_outcome": "exception",
        "ended_at_utc": timing.ended_at_utc,
        "timing_status": timing.timing_status,
    }


def _sanitized_exception_stop_reason(error: BaseException) -> str:
    if isinstance(error, DockerSbxError):
        return f"sandbox:{error.operation}:{error.reason}"
    if isinstance(error, DockerSbxUnsupportedError):
        return f"sandbox_unsupported:{error.reason}"
    if isinstance(error, RepoIntakeError):
        return f"intake:{error.operation}"
    return f"internal:{type(error).__name__.lower()}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _AttemptStart:
    started_at_utc: str
    started_monotonic_s: float


@dataclass(frozen=True, slots=True)
class _AttemptTiming:
    started_at_utc: str | None
    ended_at_utc: str | None
    monotonic_duration_s: float | None
    timing_status: Literal["complete", "evidence_timing_error"]


class EvidenceTimingError(Exception):
    pass


def _begin_attempt_timing() -> _AttemptStart | None:
    try:
        started_at_utc = _format_aware_utc(_utc_now())
        started_monotonic_s = time.monotonic()
        if not math.isfinite(started_monotonic_s):
            raise ValueError("attempt start monotonic clock is invalid")
    except (
        ArithmeticError,
        OSError,
        RuntimeError,
        StopIteration,
        TypeError,
        ValueError,
    ):
        return None
    return _AttemptStart(started_at_utc, started_monotonic_s)


def _complete_attempt_timing(start: _AttemptStart | None) -> _AttemptTiming:
    if start is None:
        return _timing_error()
    try:
        ended_at_utc = _format_aware_utc(_utc_now())
        duration_s = time.monotonic() - start.started_monotonic_s
        if not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError("attempt monotonic duration is invalid")
    except (
        ArithmeticError,
        OSError,
        RuntimeError,
        StopIteration,
        TypeError,
        ValueError,
    ):
        return _timing_error()
    return _AttemptTiming(
        started_at_utc=start.started_at_utc,
        ended_at_utc=ended_at_utc,
        monotonic_duration_s=duration_s,
        timing_status="complete",
    )


def _format_aware_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("attempt time must be timezone-aware")
    return value.isoformat()


def _timing_error() -> _AttemptTiming:
    return _AttemptTiming(
        started_at_utc=None,
        ended_at_utc=None,
        monotonic_duration_s=None,
        timing_status="evidence_timing_error",
    )


def _timing_failure_evidence(
    *,
    repo_url: str,
    expected_sha: str | None,
    actual_verified_sha: str | None,
    container_port: int,
    compose_path: str | None,
    exit_code: int,
    timing: _AttemptTiming,
    report_paths: TrialReportPaths | None = None,
) -> dict[str, object]:
    evidence = _exception_evidence(
        EvidenceTimingError(),
        repo_url=repo_url,
        expected_sha=expected_sha,
        actual_verified_sha=actual_verified_sha,
        container_port=container_port,
        compose_path=compose_path,
        exit_code=exit_code,
        timing=timing,
    )
    evidence["stop_reason"] = "internal:evidence_timing_error"
    if report_paths is not None:
        evidence["report_paths"] = {
            "html": str(report_paths.html_path),
            "json": str(report_paths.json_path),
        }
    return evidence


def _required_terminal_stop_reason(stop_reason: str | None) -> str:
    if not isinstance(stop_reason, str) or not stop_reason:
        raise ValueError("graph terminal result is missing stop_reason")
    return stop_reason


def _persist_control_flow_evidence(
    error: BaseException,
    *,
    run_id: str | None,
    run_path: Path | None,
    repo_url: str,
    expected_sha: str | None,
    actual_verified_sha: str | None,
    container_port: int,
    compose_path: str | None,
    timing: _AttemptTiming,
) -> None:
    if run_path is None:
        return
    evidence = _exception_evidence(
        error,
        repo_url=repo_url,
        expected_sha=expected_sha,
        actual_verified_sha=actual_verified_sha,
        container_port=container_port,
        compose_path=compose_path,
        exit_code=_control_flow_exit_code(error),
        timing=timing,
    )
    evidence["run_id"] = run_id
    evidence["stop_reason"] = _control_flow_stop_reason(error)
    evidence["terminal_outcome"] = "control_flow_exception"
    try:
        evidence_path = _persist_attempt_evidence(run_path, evidence)
    except (OSError, TypeError, ValueError):
        typer.echo("inspect failed: TerminalEvidenceWriteError", err=True)
        raise typer.Exit(4) from None
    typer.echo(f"attempt_evidence={evidence_path}")


def _control_flow_stop_reason(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "control:cancelled"
    if isinstance(error, KeyboardInterrupt):
        return "control:keyboard_interrupt"
    if isinstance(error, SystemExit):
        return "control:system_exit"
    raise TypeError("unsupported control-flow exception")


def _control_flow_exit_code(error: BaseException) -> int:
    if isinstance(error, SystemExit):
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return int(error.code)
        return 1
    return 130


def _persist_intake_failure_evidence(
    run_path: Path,
    *,
    run_id: str,
    error: RepoIntakeError,
) -> _IntakeEvidenceStatus:
    path = run_path / "intake-failure.json"
    failure_class = error.failure_class
    if failure_class is None or failure_class not in _INTAKE_FAILURE_CLASSES:
        failure_class = "unknown"
    evidence = {
        "failure_class": failure_class,
        "operation": error.operation,
        "returncode": error.returncode,
        "run_id": run_id,
        "schema_version": _INTAKE_FAILURE_EVIDENCE_SCHEMA_VERSION,
    }
    created = False
    try:
        with path.open("x", encoding="utf-8") as output:
            created = True
            json.dump(evidence, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        if not created:
            return "collision"
        try:
            path.unlink()
        except OSError:
            pass
        return "write_failed"
    except (OSError, TypeError, UnicodeError, ValueError):
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        return "write_failed"
    return "written"


def _persist_attempt_evidence(run_path: Path, evidence: dict[str, object]) -> Path:
    path = run_path / "attempt-result.json"
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _exit_after_terminal_evidence(
    run_id: str | None,
    run_path: Path | None,
    evidence: dict[str, object],
    exit_code: int,
) -> NoReturn:
    if run_id is not None:
        evidence["run_id"] = run_id
    if run_path is not None:
        try:
            evidence_path = _persist_attempt_evidence(run_path, evidence)
        except (OSError, TypeError, ValueError):
            typer.echo("inspect failed: TerminalEvidenceWriteError", err=True)
            raise typer.Exit(4) from None
        typer.echo(f"attempt_evidence={evidence_path}")
    typer.echo(f"inspect failed: {evidence['exception_type']}", err=True)
    raise typer.Exit(exit_code) from None


app = create_app()
