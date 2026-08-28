import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from repotrial import cli
from repotrial.cli import create_app
from repotrial.models.base import RecoveryAction
from repotrial.sandbox.base import ExecResult
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
        baseline_healthy: bool = True,
        candidate_boot_unavailable: bool = False,
        create_error: BaseException | None = None,
    ) -> None:
        super().__init__(ports={} if host_port is None else {8080: host_port})
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
            and command[-2:] == ("up", "-d")
        ):
            raise KeyError("candidate backend capability is unavailable")
        healthy = self.baseline_healthy or self.roles[sandbox_id] == "candidate"
        if command[-2:] == ("up", "-d"):
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
        if command == ("docker", "top", CONTAINER_ID, "-eo", "pid=,ppid=,user=,comm="):
            return ExecResult(exit_code=0, stdout="", stderr="")
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


def test_inspect_runs_local_git_fixture_to_a_report_and_cleans_up(
    tmp_path: Path,
) -> None:
    source = create_fixture_repo(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    with healthy_server() as port:
        provider = FixtureProvider(host_port=port)
        result = CliRunner().invoke(
            make_app(artifacts_root, provider),
            ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
        )

    run_path = artifacts_root / FIXED_RUN_ID
    report_path = run_path / "report" / "trial-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.exit_code == 0, result.output
    assert (run_path / "workspace" / "compose.yaml").is_file()
    assert (
        report["identity"]["commit_sha"]
        == subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
    )
    assert report["artifacts"]["experiment_overlays"]
    assert (run_path / "report" / "trial-report.html").is_file()
    assert list((run_path / "evidence").rglob("*.json"))
    assert any(call[0] == "create" for call in provider.calls)
    assert len([call for call in provider.calls if call[0] == "create"]) == len(
        [call for call in provider.calls if call[0] == "destroy"]
    )
    assert provider.active_sandboxes == set()


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
    provider = FixtureProvider(create_error=RuntimeError("unexpected fixture failure"))

    result = CliRunner().invoke(
        make_app(artifacts_root, provider),
        ["inspect", str(source), "--provider", "fake", "--max-experiments", "8"],
    )

    assert result.exit_code == 4, result.output


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
