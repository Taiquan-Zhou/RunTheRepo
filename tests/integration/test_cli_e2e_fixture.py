import asyncio
import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from repotrial import cli
from repotrial.agent.state import GraphContext, GraphState
from repotrial.cli import create_app
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    JourneyResult,
    Mutation,
    PinnedRepo,
    RepoRef,
    RunState,
)
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult
from repotrial.sandbox.docker_sbx import DockerSbxUnsupportedError
from repotrial.sandbox.fake import FakeSandboxProvider

ModelT = TypeVar("ModelT", bound=BaseModel)
FIXED_RUN_ID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "a" * 12


class FakeModelAdapter:
    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT:
        del system, user
        return schema.model_validate(
            RecoveryAction(action="stop", params={}, reason="fixture stop").model_dump()
        )


class FixtureProvider(FakeSandboxProvider):
    def __init__(
        self,
        *,
        host_port: int | None = None,
        container_port: int = 8080,
        baseline_healthy: bool = True,
        candidate_boot_unavailable: bool = False,
        create_error: BaseException | None = None,
    ) -> None:
        super().__init__(ports={} if host_port is None else {container_port: host_port})
        self.baseline_healthy = baseline_healthy
        self.candidate_boot_unavailable = candidate_boot_unavailable
        self.create_error = create_error
        self.roles: dict[str, str] = {}
        self.active_sandboxes: set[str] = set()

    async def create(self, workspace: Path, name: str) -> str:
        if self.create_error is not None:
            raise self.create_error
        sandbox_id = await super().create(workspace, name)
        self.roles[sandbox_id] = (
            "baseline" if name.startswith("repotrial-baseline-") else "candidate"
        )
        self.active_sandboxes.add(sandbox_id)
        return sandbox_id

    async def destroy(self, sandbox_id: str) -> None:
        await super().destroy(sandbox_id)
        self.active_sandboxes.discard(sandbox_id)

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self._require_active(sandbox_id)
        command = tuple(argv)
        self.calls.append(("exec", sandbox_id, command, timeout_s))
        if (
            self.roles[sandbox_id] == "candidate"
            and self.candidate_boot_unavailable
            and command[-5:] == ("up", "-d", "--wait", "--wait-timeout", "60")
        ):
            raise KeyError("candidate backend capability is unavailable")
        healthy = self.baseline_healthy or self.roles[sandbox_id] == "candidate"
        if command[-5:] == ("up", "-d", "--wait", "--wait-timeout", "60"):
            return ExecResult(exit_code=0 if healthy else 1, stdout="", stderr="")
        if command[-4:] == ("ps", "--all", "--format", "json"):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "Service": "web",
                        "State": "running" if healthy else "exited",
                        "Health": "healthy" if healthy else "",
                        "ExitCode": 0 if healthy else 1,
                    }
                ),
                stderr="",
            )
        if command[-4:] == ("logs", "--no-color", "--tail", "200"):
            return ExecResult(exit_code=0, stdout="", stderr="")
        if command[-6:] == (
            "ps",
            "--all",
            "--no-trunc",
            "--orphans=false",
            "--format",
            "json",
        ):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps({"Service": "web", "ID": CONTAINER_ID}),
                stderr="",
            )
        if command == ("docker", "inspect", CONTAINER_ID):
            return ExecResult(
                exit_code=0,
                stdout=json.dumps([{"Config": {"Env": []}}]),
                stderr="",
            )
        if command == ("docker", "diff", CONTAINER_ID):
            return ExecResult(exit_code=0, stdout="", stderr="")
        if command == ("docker", "top", CONTAINER_ID, "-eo", "pid,ppid,user,comm"):
            return ExecResult(
                exit_code=0,
                stdout="PID PPID USER COMMAND\n",
                stderr="",
            )
        raise AssertionError(f"unexpected provider command: {command!r}")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def healthy_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def fixed_run_id() -> str:
    return FIXED_RUN_ID


def create_fixture_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    (repository / "compose.yaml").write_text(
        "services:\n  web:\n    image: example/web:1\n    user: '0'\n    read_only: true\n    cap_drop: [ALL]\n",
        encoding="utf-8",
    )
    (repository / "repotrial.journeys.json").write_text(
        json.dumps(
            {
                "journeys": [
                    {
                        "journey_id": "health",
                        "name": "Health",
                        "steps": [
                            {
                                "step_id": "health-request",
                                "tool": "http",
                                "action": "request",
                                "params": {"method": "GET", "path": "/health"},
                                "assertions": [
                                    {
                                        "kind": "status_code",
                                        "target": "response.status",
                                        "expected": 200,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    for arguments in (
        ("init",),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
    return repository


def make_app(artifacts_root: Path, provider: FixtureProvider):
    return create_app(
        artifacts_root=artifacts_root,
        run_id_generator=fixed_run_id,
        provider_factory=lambda name: provider,
        model=FakeModelAdapter(),
    )


def _patch_attempt_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    utc_values = iter(
        (
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 102.5))
    monkeypatch.setattr(cli, "_utc_now", lambda: next(utc_values), raising=False)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
        raising=False,
    )


def _attempt_result(artifacts_root: Path) -> dict[str, object]:
    return json.loads(
        (artifacts_root / FIXED_RUN_ID / "attempt-result.json").read_text(
            encoding="utf-8"
        )
    )


def _assert_attempt_timing(attempt: dict[str, object]) -> None:
    assert attempt["started_at_utc"] == "2026-08-29T12:00:00+00:00"
    assert attempt["ended_at_utc"] == "2026-08-29T12:01:00+00:00"
    assert attempt["monotonic_duration_s"] == 2.5


def _assert_timing_error(attempt: dict[str, object]) -> None:
    assert attempt["timing_status"] == "evidence_timing_error"
    assert attempt["started_at_utc"] is None
    assert attempt["ended_at_utc"] is None
    assert attempt["monotonic_duration_s"] is None


def test_inspect_runs_local_git_fixture_to_a_report_and_cleans_up(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port, container_port=3000)
        source_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            [
                "inspect",
                str(source),
                "--provider",
                "fake",
                "--max-experiments",
                "8",
                "--commit-sha",
                source_head,
                "--container-port",
                "3000",
                "--compose-path",
                "compose.yaml",
            ],
        )

    run_path = artifacts_root / FIXED_RUN_ID
    report_path = run_path / "report" / "trial-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    workspace = run_path / "workspace"
    assert result.exit_code == 0, result.output
    assert (workspace / ".git").is_dir()
    assert (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
        ).strip()
        == source_head
    )
    assert report["identity"]["commit_sha"] == source_head
    attempt = json.loads((run_path / "attempt-result.json").read_text(encoding="utf-8"))
    assert attempt["actual_verified_sha"] == source_head
    assert attempt["expected_sha"] == source_head
    assert attempt["container_port"] == 3000
    assert attempt["compose_path"] == "compose.yaml"
    assert attempt["stop_reason"] == "no_remaining_mutations"
    assert attempt["known_limitations"] == ["pid_hard_bound_unsupported"]
    assert f"attempt_evidence={run_path / 'attempt-result.json'}" in result.output
    assert (workspace / "compose.yaml").is_file()
    overlays = report["artifacts"]["experiment_overlays"]
    assert overlays
    for overlay in overlays:
        overlay_path = (workspace / overlay).resolve(strict=True)
        assert overlay_path.is_relative_to(workspace.resolve())
        assert overlay_path.is_file()
    assert (run_path / "report" / "trial-report.html").is_file()
    assert list((run_path / "evidence").rglob("*.json"))
    assert any(call[0] == "create" for call in provider.calls)
    assert any(call[0] == "publish_port" and call[2] == 3000 for call in provider.calls)
    assert len([call for call in provider.calls if call[0] == "create"]) == len(
        [call for call in provider.calls if call[0] == "destroy"]
    )
    assert provider.active_sandboxes == set()


def test_relative_artifacts_root_uses_absolute_graph_context_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    invocation_cwd = tmp_path / "invocation-cwd"
    invocation_cwd.mkdir()
    monkeypatch.chdir(invocation_cwd)
    artifacts_root = Path("artifacts")
    contexts: list[GraphContext] = []
    original_ainvoke_run = cli.ainvoke_run

    async def capture_context(
        graph: object, state: RunState, *, context: GraphContext
    ) -> GraphState:
        contexts.append(context)
        return await original_ainvoke_run(graph, state, context=context)

    monkeypatch.setattr(cli, "ainvoke_run", capture_context)
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    relative_run_path = artifacts_root / FIXED_RUN_ID
    absolute_run_path = (invocation_cwd / relative_run_path).resolve(strict=True)
    assert result.exit_code == 0, result.output
    assert f"artifact_path={relative_run_path}" in result.output
    assert len(contexts) == 1
    context = contexts[0]
    assert context.workspace == absolute_run_path / "workspace"
    assert context.overlay_dir == context.workspace / ".repotrial-overlays"
    assert context.accepted_compose_dir == context.workspace / ".repotrial-accepted"
    assert context.artifact_dir == absolute_run_path / "evidence"
    assert all(
        path.is_absolute()
        for path in (
            context.workspace,
            context.overlay_dir,
            context.accepted_compose_dir,
            context.artifact_dir,
        )
    )
    assert context.overlay_dir.is_relative_to(context.workspace)
    assert context.accepted_compose_dir.is_relative_to(context.workspace)
    assert not context.artifact_dir.is_relative_to(context.workspace)
    assert (absolute_run_path / "report" / "trial-report.json").is_file()
    assert provider.active_sandboxes == set()


def test_graph_terminal_evidence_records_utc_and_monotonic_attempt_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def terminal_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(run=state.model_copy(update={"stop_reason": "completed"}))

    monkeypatch.setattr(cli, "ainvoke_run", terminal_run)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 3, result.output
    _assert_attempt_timing(_attempt_result(artifacts_root))


def test_start_timing_failure_persists_sanitized_evidence_without_running_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    graph_started = False

    def fail_start_clock() -> datetime:
        raise RuntimeError("clock secret=do-not-persist")

    async def unexpected_graph(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        nonlocal graph_started
        del graph, state, context
        graph_started = True
        raise AssertionError("timing failure reached graph execution")

    monkeypatch.setattr(cli, "_utc_now", fail_start_clock)
    monkeypatch.setattr(cli, "ainvoke_run", unexpected_graph)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == 4, result.output
    assert not graph_started
    assert attempt["exception_type"] == "EvidenceTimingError"
    assert attempt["stop_reason"] == "internal:evidence_timing_error"
    _assert_timing_error(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


@pytest.mark.parametrize("failure", ["end_utc", "end_monotonic"])
def test_terminal_timing_completion_failure_fails_closed_with_evidence(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    timestamps = iter(
        (
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 102.5))

    def utc_now() -> datetime:
        if failure == "end_utc" and len(calls) == 0:
            calls.append("start")
            return next(timestamps)
        if failure == "end_utc":
            raise RuntimeError("end UTC secret=do-not-persist")
        return next(timestamps)

    def monotonic() -> float:
        if failure == "end_monotonic" and len(calls) == 0:
            calls.append("start")
            return next(monotonic_values)
        if failure == "end_monotonic":
            raise RuntimeError("end monotonic secret=do-not-persist")
        return next(monotonic_values)

    calls: list[str] = []
    monkeypatch.setattr(cli, "_utc_now", utc_now)
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=monotonic))

    async def terminal_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(run=state.model_copy(update={"stop_reason": "completed"}))

    monkeypatch.setattr(cli, "ainvoke_run", terminal_run)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == 4, result.output
    assert attempt["exception_type"] == "EvidenceTimingError"
    assert attempt["stop_reason"] == "internal:evidence_timing_error"
    assert attempt["report_paths"]["json"] == str(
        artifacts_root / FIXED_RUN_ID / "report" / "trial-report.json"
    )
    assert attempt["report_paths"]["html"] == str(
        artifacts_root / FIXED_RUN_ID / "report" / "trial-report.html"
    )
    _assert_timing_error(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


def test_missing_graph_terminal_stop_reason_fails_closed_with_sanitized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def missing_stop_reason(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(run=state)

    monkeypatch.setattr(cli, "ainvoke_run", missing_stop_reason)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == 4, result.output
    assert attempt["stop_reason"] == "internal:valueerror"
    assert attempt["terminal_outcome"] == "exception"
    _assert_attempt_timing(attempt)


@pytest.mark.parametrize(
    ("raised", "expected_exit", "expected_stop_reason"),
    [
        (
            DockerSbxUnsupportedError("capability_missing"),
            2,
            "sandbox_unsupported:capability_missing",
        ),
        (RuntimeError("secret=never-persist"), 4, "internal:runtimeerror"),
    ],
)
def test_exception_attempt_evidence_records_timing_without_error_text(
    raised: Exception,
    expected_exit: int,
    expected_stop_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def failing_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, state, context
        raise raised

    monkeypatch.setattr(cli, "ainvoke_run", failing_run)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    attempt = _attempt_result(artifacts_root)
    assert result.exit_code == expected_exit, result.output
    assert attempt["stop_reason"] == expected_stop_reason
    assert "never-persist" not in json.dumps(attempt)
    _assert_attempt_timing(attempt)


@pytest.mark.parametrize(
    ("raised", "expected_stop_reason", "expected_cli_exit"),
    [
        (asyncio.CancelledError(), "control:cancelled", None),
        (KeyboardInterrupt(), "control:keyboard_interrupt", 130),
        (SystemExit(17), "control:system_exit", 17),
        (SystemExit(), "control:system_exit", 0),
        (SystemExit("secret=do-not-persist"), "control:system_exit", 1),
        (SystemExit(True), "control:system_exit", 1),
        (SystemExit(False), "control:system_exit", 0),
    ],
)
def test_control_flow_exit_persists_sanitized_attempt_evidence_before_propagating(
    raised: BaseException,
    expected_stop_reason: str,
    expected_cli_exit: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    _patch_attempt_clocks(monkeypatch)

    async def interrupted_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, state, context
        raise raised

    monkeypatch.setattr(cli, "ainvoke_run", interrupted_run)
    if expected_cli_exit is None:
        with pytest.raises(asyncio.CancelledError) as propagated:
            CliRunner().invoke(
                make_app(artifacts_root, FixtureProvider()),
                [
                    "inspect",
                    str(source),
                    "--provider",
                    "fake",
                    "--max-experiments",
                    "8",
                ],
            )
        assert propagated.value is raised
    else:
        result = CliRunner().invoke(
            make_app(artifacts_root, FixtureProvider()),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )
        assert result.exit_code == expected_cli_exit

    attempt = _attempt_result(artifacts_root)
    assert attempt["exception_type"] == type(raised).__name__
    assert attempt["stop_reason"] == expected_stop_reason
    assert attempt["known_limitations"] == ["pid_hard_bound_unsupported"]
    if isinstance(raised, SystemExit):
        assert attempt["exit_code"] == expected_cli_exit
        assert type(attempt["exit_code"]) is int
        assert "do-not-persist" not in json.dumps(attempt)
    _assert_attempt_timing(attempt)


@pytest.mark.parametrize(
    ("raised", "expected_stop_reason", "expected_cli_exit"),
    [
        (asyncio.CancelledError(), "control:cancelled", None),
        (KeyboardInterrupt(), "control:keyboard_interrupt", 130),
        (SystemExit(17), "control:system_exit", 17),
    ],
)
def test_control_flow_timing_failure_persists_evidence_without_replacing_control_flow(
    raised: BaseException,
    expected_stop_reason: str,
    expected_cli_exit: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    start = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    monotonic_values = iter((100.0,))

    def utc_now() -> datetime:
        if len(calls) == 0:
            calls.append("start")
            return start
        raise RuntimeError("end UTC secret=do-not-persist")

    calls: list[str] = []
    monkeypatch.setattr(cli, "_utc_now", utc_now)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    async def interrupted_run(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, state, context
        raise raised

    monkeypatch.setattr(cli, "ainvoke_run", interrupted_run)
    if expected_cli_exit is None:
        with pytest.raises(asyncio.CancelledError) as propagated:
            CliRunner().invoke(
                make_app(artifacts_root, FixtureProvider()),
                [
                    "inspect",
                    str(source),
                    "--provider",
                    "fake",
                    "--max-experiments",
                    "8",
                ],
            )
        assert propagated.value is raised
    else:
        result = CliRunner().invoke(
            make_app(artifacts_root, FixtureProvider()),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )
        assert result.exit_code == expected_cli_exit

    attempt = _attempt_result(artifacts_root)
    assert attempt["exception_type"] == type(raised).__name__
    assert attempt["stop_reason"] == expected_stop_reason
    _assert_timing_error(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


def test_inspect_rejects_unfrozen_experiment_budget_before_artifacts(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    provider = FixtureProvider()

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "7"],
    )

    assert result.exit_code != 0
    assert not artifacts_root.exists()
    assert provider.calls == []


def test_dry_run_rejects_unfrozen_experiment_budget_before_artifacts(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        [
            "inspect",
            "--dry-run",
            "--max-experiments",
            "7",
            "https://github.com/a/b",
        ],
    )

    assert result.exit_code == 2, result.output
    assert not artifacts_root.exists()


def test_inspect_returns_unsupported_for_a_capability_unavailable_trial(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port, candidate_boot_unavailable=True)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    assert result.exit_code == 2, result.output
    assert provider.active_sandboxes == set()


def test_inspect_returns_trial_failed_for_a_baseline_failure(tmp_path: Path) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    provider = FixtureProvider(baseline_healthy=False)

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 3, result.output
    assert provider.active_sandboxes == set()


def test_inspect_returns_internal_error_for_an_unexpected_provider_failure(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    provider = FixtureProvider(
        create_error=RuntimeError("unexpected fixture failure secret=do-not-persist")
    )

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 4, result.output
    attempt = json.loads(
        (artifacts_root / FIXED_RUN_ID / "attempt-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt["exception_type"] == "RuntimeError"
    assert attempt["stop_reason"] == "internal:runtimeerror"
    assert "unexpected fixture failure" not in json.dumps(attempt)
    assert "do-not-persist" not in json.dumps(attempt)


def test_inspect_rejects_pinner_sha_mismatch_before_provider_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "artifacts"
    expected_sha = "a" * 40
    actual_sha = "b" * 40
    provider = FixtureProvider()

    async def mismatched_pinner(
        url: str, destination: Path, requested_ref: str | None = None
    ) -> PinnedRepo:
        assert requested_ref == expected_sha
        destination.mkdir()
        return PinnedRepo(
            repo=RepoRef(
                url=url,
                owner="owner",
                repo="repo",
                requested_ref=requested_ref,
            ),
            commit_sha=actual_sha,
            local_path=destination,
        )

    monkeypatch.setattr(cli, "pin_repository", mismatched_pinner)
    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        [
            "inspect",
            "--provider",
            "fake",
            "--commit-sha",
            expected_sha,
            "https://github.com/owner/repo",
        ],
    )

    attempt = json.loads(
        (artifacts_root / FIXED_RUN_ID / "attempt-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.exit_code == 4, result.output
    assert provider.calls == []
    assert attempt["actual_verified_sha"] == actual_sha
    assert attempt["stop_reason"] == "intake:commit_sha_mismatch"


def test_inspect_returns_internal_error_when_report_rendering_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    def fail_render(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unexpected report failure")

    monkeypatch.setattr(cli, "render_trial_report", fail_render)
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    assert result.exit_code == 4, result.output


def test_inspect_returns_unsupported_for_an_unsupported_baseline_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    async def unsupported_baseline(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(
            run=state.model_copy(
                update={
                    "baseline_journey_results": [
                        JourneyResult(
                            journey_id="unsupported-baseline",
                            verdict=Verdict.UNSUPPORTED,
                            passed_steps=0,
                            total_steps=1,
                        )
                    ],
                    "stop_reason": "insufficient_coverage",
                }
            )
        )

    monkeypatch.setattr(cli, "ainvoke_run", unsupported_baseline)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 2, result.output


def test_inspect_derives_a_baseline_journey_from_the_pinned_root_readme(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    (source / "repotrial.journeys.json").unlink()
    (source / "README.md").write_text("[health](/health)\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "repotrial.journeys.json", "README.md"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "root readme journey"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    artifacts_root = tmp_path / "artifacts"

    with healthy_server() as port:
        result = CliRunner().invoke(
            make_app(artifacts_root, FixtureProvider(host_port=port)),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    assert result.exit_code == 0, result.output
    report = json.loads(
        (artifacts_root / FIXED_RUN_ID / "report" / "trial-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["coverage"]["journeys"] == [
        {
            "classification": "PASS",
            "journey_id": "readme-1",
            "name": "GET /health",
        }
    ]


def test_inspect_returns_unsupported_for_an_unsupported_experiment_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    async def unsupported_experiment(
        graph: object, state: RunState, *, context: object
    ) -> GraphState:
        del graph, context
        return GraphState(
            run=state.model_copy(
                update={
                    "baseline_journey_results": [
                        JourneyResult(
                            journey_id="baseline",
                            verdict=Verdict.PASS,
                            passed_steps=1,
                            total_steps=1,
                        )
                    ],
                    "experiments": [
                        ExperimentRecord(
                            experiment_id="unsupported-experiment",
                            parent_config_hash="sha256:parent",
                            candidate_config_hash="sha256:candidate",
                            mutation=Mutation(
                                mutation_id="policy:set_non_root:web",
                                type=MutationType.SET_NON_ROOT,
                                service="web",
                            ),
                            boot=Verdict.PASS,
                            journeys=[
                                JourneyResult(
                                    journey_id="unsupported-experiment",
                                    verdict=Verdict.UNSUPPORTED,
                                    passed_steps=0,
                                    total_steps=1,
                                )
                            ],
                            verdict=ExperimentVerdict.STOP,
                            reason="journey_unsupported",
                        )
                    ],
                    "stop_reason": "experiment:journey_unsupported",
                }
            )
        )

    monkeypatch.setattr(cli, "ainvoke_run", unsupported_experiment)
    result = CliRunner().invoke(
        make_app(artifacts_root, FixtureProvider()),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 2, result.output


def test_inspect_rejects_uninjected_fake_provider_before_artifacts(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"

    result = CliRunner().invoke(
        create_app(artifacts_root=artifacts_root, run_id_generator=fixed_run_id),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 2, result.output
    assert "--provider fake is unavailable without test injection" in result.output
    assert not artifacts_root.exists()


def test_inspect_returns_internal_error_when_run_layout_creation_fails(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    (artifacts_root / FIXED_RUN_ID).mkdir(parents=True)
    provider = FixtureProvider()

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 4, result.output
    assert "inspect failed: FileExistsError" in result.output
    assert provider.calls == []
