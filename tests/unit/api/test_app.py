import asyncio
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from ruamel.yaml import YAML

from repotrial.agent.state import GraphState
from repotrial.api.registry import (
    InMemoryRunRegistry,
    RegistryUnavailableError,
    RunLifecycle,
    RunRecord,
)
from repotrial.domain.enums import Verdict
from repotrial.domain.models import JourneyResult, RunState
from repotrial.report.render import render_trial_report
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import DockerSbxPolicy, DockerSbxUnsupportedError
from repotrial.sandbox.fake import FakeSandboxProvider


class CountingRegistry(InMemoryRunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.creates = 0

    async def create_running(self, run_id: str, repo_url: str):
        self.creates += 1
        return await super().create_running(run_id, repo_url)


class CleanupFailingRegistry(InMemoryRunRegistry):
    def __init__(self, failure: BaseException | None) -> None:
        super().__init__()
        self.failure = failure

    async def complete(
        self,
        run_id: str,
        lifecycle: RunLifecycle,
        *,
        commit_sha: str | None,
        report_available: bool,
    ) -> RunRecord:
        if lifecycle is RunLifecycle.INTERRUPTED and self.failure is not None:
            raise self.failure
        return await super().complete(
            run_id,
            lifecycle,
            commit_sha=commit_sha,
            report_available=report_available,
        )


def _completed_state(run_id: str = "run-fixed") -> RunState:
    return RunState(
        run_id=run_id,
        repo_url="https://github.com/a/b",
        commit_sha="a" * 40,
        stop_reason="no_more_mutations",
        baseline_journey_results=[
            JourneyResult(
                journey_id="journey-1",
                verdict=Verdict.PASS,
                passed_steps=1,
                total_steps=1,
            )
        ],
    )


def _prepare_report(
    root: Path,
    registry: InMemoryRunRegistry,
    *,
    run_id: str = "run-1",
) -> None:
    asyncio.run(registry.create_running(run_id, "https://github.com/a/b"))
    asyncio.run(
        registry.complete(
            run_id,
            RunLifecycle.COMPLETED,
            commit_sha="a" * 40,
            report_available=True,
        )
    )
    report_dir = root / run_id / "report"
    report_dir.mkdir(parents=True)
    (report_dir / "trial-report.json").write_text(
        '{"representation":"json"}', encoding="utf-8"
    )
    (report_dir / "trial-report.html").write_text(
        "<p>representation:html</p>", encoding="utf-8"
    )


def test_healthz_reports_ready_without_probing_sandbox() -> None:
    from repotrial.api.app import create_app

    provider_calls: list[str] = []

    def provider_factory() -> FakeSandboxProvider:
        provider_calls.append("provider")
        return FakeSandboxProvider()

    with TestClient(
        create_app(registry=InMemoryRunRegistry(), provider_factory=provider_factory)
    ) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert provider_calls == []


def test_api_default_provider_uses_supported_resource_minimums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import repotrial.api.app as app_module

    captured_policies: list[DockerSbxPolicy] = []
    provider = FakeSandboxProvider()

    def recording_provider(policy: DockerSbxPolicy) -> SandboxProvider:
        captured_policies.append(policy)
        return provider

    monkeypatch.setattr(app_module, "DockerSbxProvider", recording_provider)

    assert app_module._default_provider() is provider
    assert len(captured_policies) == 1
    assert captured_policies[0].cpus == 1
    assert isinstance(captured_policies[0].cpus, int)
    assert captured_policies[0].memory_mb == 1024
    assert captured_policies[0].pids_limit == 64
    assert captured_policies[0].disk_mb == 2048
    assert captured_policies[0].total_duration_s == 900


def test_healthz_is_unavailable_without_ready_registry() -> None:
    from repotrial.api.app import create_app

    with TestClient(
        create_app(registry=InMemoryRunRegistry(available=False))
    ) as client:
        response = client.get("/healthz")

    assert response.status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {"repo_url": "https://user:password@github.com/a/b"},
        {"repo_url": "https://github.com/a/b", "model_endpoint": "https://model"},
        {
            "repo_url": "https://github.com/a/b",
            "model_endpoint": "https://user:password@model/v1",
            "model_name": "model",
        },
        {"repo_url": "https://github.com/a/b", "provider": "host"},
    ],
)
def test_invalid_run_requests_fail_before_registry_write(
    payload: dict[str, str],
) -> None:
    from repotrial.api.app import create_app

    registry = CountingRegistry()
    with TestClient(create_app(registry=registry)) as client:
        response = client.post("/runs", json=payload)

    assert response.status_code == 422
    assert registry.creates == 0


@pytest.mark.parametrize(
    "payload,sentinel",
    [
        ({"repo_url": "https://user:TOPSECRET@github.com/a/b"}, "TOPSECRET"),
        (
            {
                "repo_url": "https://github.com/a/b",
                "model_endpoint": "https://user:MODELSECRET@model/v1",
                "model_name": "model",
            },
            "MODELSECRET",
        ),
    ],
)
def test_invalid_credentials_are_not_reflected_in_validation_response(
    payload: dict[str, str], sentinel: str
) -> None:
    from repotrial.api.app import create_app

    registry = CountingRegistry()
    with TestClient(create_app(registry=registry)) as client:
        response = client.post("/runs", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    assert sentinel not in response.text
    assert registry.creates == 0


def test_returned_unsupported_outcome_is_recorded_then_returns_503(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    async def runner(*_args: object) -> GraphState:
        return GraphState(
            run=RunState(
                run_id="run-fixed",
                repo_url="https://github.com/a/b",
                stop_reason="boot_unsupported",
            )
        )

    registry = InMemoryRunRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"run_id": "run-fixed", "outcome": "execution_unsupported"}
    }
    record = asyncio.run(registry.get("run-fixed"))
    assert record is not None
    assert record.lifecycle is RunLifecycle.UNSUPPORTED
    assert record.report_available is True


def test_unexpected_runner_error_is_recorded_and_sanitized(tmp_path: Path) -> None:
    from repotrial.api.app import create_app

    async def runner(*_args: object) -> GraphState:
        raise LookupError("RUNNERSECRET")

    registry = InMemoryRunRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 500
    assert response.json() == {
        "detail": {"run_id": "run-fixed", "outcome": "internal_error"}
    }
    assert "RUNNERSECRET" not in response.text
    record = asyncio.run(registry.get("run-fixed"))
    assert record is not None
    assert record.lifecycle is RunLifecycle.INTERNAL_ERROR


def test_unknown_run_and_unavailable_report_use_stable_statuses(tmp_path: Path) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    asyncio.run(registry.create_running("run-1", "https://github.com/a/b"))
    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        unknown = client.get("/runs/no-such-run")
        report = client.get("/runs/run-1/report")

    assert unknown.status_code == 404
    assert report.status_code == 409


def test_report_negotiates_json_html_and_rejects_other_accept_types(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    asyncio.run(registry.create_running("run-1", "https://github.com/a/b"))
    asyncio.run(
        registry.complete(
            "run-1",
            RunLifecycle.COMPLETED,
            commit_sha="a" * 40,
            report_available=True,
        )
    )
    report_dir = tmp_path / "run-1" / "report"
    report_dir.mkdir(parents=True)
    (report_dir / "trial-report.json").write_text('{"ok":true}', encoding="utf-8")
    (report_dir / "trial-report.html").write_text("<h1>ok</h1>", encoding="utf-8")

    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        json_response = client.get("/runs/run-1/report")
        html_response = client.get(
            "/runs/run-1/report", headers={"Accept": "text/html"}
        )
        xml_response = client.get(
            "/runs/run-1/report", headers={"Accept": "application/xml"}
        )

    assert json_response.status_code == 200
    assert json_response.json() == {"ok": True}
    assert html_response.status_code == 200
    assert html_response.text == "<h1>ok</h1>"
    assert xml_response.status_code == 406


@pytest.mark.parametrize(
    "accept", ["application/jsonp", "application/json;q=0", "application/json;q=0.0"]
)
def test_report_rejects_near_miss_or_unacceptable_json(
    accept: str, tmp_path: Path
) -> None:
    from repotrial.api.app import _representation

    assert _representation(accept) is None


def test_report_path_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    from repotrial.api.app import _safe_regular_file

    root = tmp_path / "artifacts"
    target = root / "other" / "report"
    target.mkdir(parents=True)
    (target / "trial-report.json").write_text("{}", encoding="utf-8")
    run_path = root / "run-1"
    run_path.mkdir()
    link = run_path / "report"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unsupported: {error}")

    assert not _safe_regular_file(link / "trial-report.json", root)


def test_run_layout_leaves_graph_workspace_claimable(tmp_path: Path) -> None:
    from repotrial.api.app import _create_run_layout

    run_path = _create_run_layout(tmp_path, "run-1")

    assert not (run_path / "workspace").exists()
    assert {path.name for path in run_path.iterdir()} == {
        "evidence",
        "experiments",
        "report",
    }


@pytest.mark.parametrize(
    "final_state,expected_outcome,expected_lifecycle",
    [
        (_completed_state(), "completed", RunLifecycle.COMPLETED),
        (
            RunState(run_id="run-fixed", repo_url="https://github.com/a/b"),
            "trial_failed",
            RunLifecycle.TRIAL_FAILED,
        ),
    ],
)
def test_post_run_completes_synchronously_with_terminal_metadata_and_report(
    final_state: RunState,
    expected_outcome: str,
    expected_lifecycle: RunLifecycle,
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    events: list[str] = []

    async def runner(*_args: object) -> GraphState:
        events.append("runner")
        return GraphState(run=final_state)

    def renderer(state: RunState, report_dir: Path):
        events.append("renderer")
        return render_trial_report(state, report_dir)

    registry = InMemoryRunRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
            report_renderer=renderer,
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})
        metadata = client.get("/runs/run-fixed")
        report = client.get("/runs/run-fixed/report")

    assert events == ["runner", "renderer"]
    assert response.status_code == 201
    assert response.json() == {"run_id": "run-fixed", "outcome": expected_outcome}
    assert metadata.json() == {
        "run_id": "run-fixed",
        "repo_url": "https://github.com/a/b",
        "lifecycle": expected_lifecycle.value,
        "commit_sha": final_state.commit_sha,
        "report_available": True,
    }
    assert report.status_code == 200
    assert report.json()["identity"]["run_id"] == "run-fixed"


def test_thrown_unsupported_is_recorded_and_sanitized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from repotrial.api.app import create_app

    async def runner(*_args: object) -> GraphState:
        raise DockerSbxUnsupportedError("missing_capability", stderr="SBXSECRET")

    registry = InMemoryRunRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    record = asyncio.run(registry.get("run-fixed"))
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"run_id": "run-fixed", "outcome": "execution_unsupported"}
    }
    assert "SBXSECRET" not in response.text
    assert "SBXSECRET" not in caplog.text
    assert record is not None
    assert record.lifecycle is RunLifecycle.UNSUPPORTED
    assert record.report_available is False
    assert "SBXSECRET" not in repr(record)


def test_thrown_unsupported_registry_failure_becomes_recorded_internal_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from repotrial.api.app import create_app

    class UnsupportedWriteFailingRegistry(InMemoryRunRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.terminal_attempts: list[RunLifecycle] = []

        async def complete(
            self,
            run_id: str,
            lifecycle: RunLifecycle,
            *,
            commit_sha: str | None,
            report_available: bool,
        ) -> RunRecord:
            self.terminal_attempts.append(lifecycle)
            if lifecycle is RunLifecycle.UNSUPPORTED:
                raise RegistryUnavailableError("DBSECRET")
            return await super().complete(
                run_id,
                lifecycle,
                commit_sha=commit_sha,
                report_available=report_available,
            )

    async def runner(*_args: object) -> GraphState:
        raise DockerSbxUnsupportedError("missing_capability")

    registry = UnsupportedWriteFailingRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    record = asyncio.run(registry.get("run-fixed"))
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"run_id": "run-fixed", "outcome": "internal_error"}
    }
    assert registry.terminal_attempts == [
        RunLifecycle.UNSUPPORTED,
        RunLifecycle.INTERNAL_ERROR,
    ]
    assert record is not None
    assert record.lifecycle is RunLifecycle.INTERNAL_ERROR
    assert record.report_available is False
    assert "DBSECRET" not in response.text
    assert "DBSECRET" not in caplog.text
    assert "DBSECRET" not in repr(record)


@pytest.mark.parametrize(
    "cleanup_failure,expected_lifecycle",
    [
        (None, RunLifecycle.INTERRUPTED),
        (LookupError("CLEANUPSECRET"), RunLifecycle.RUNNING),
    ],
)
def test_thrown_unsupported_terminal_cancellation_preserves_primary_and_interrupts(
    cleanup_failure: BaseException | None,
    expected_lifecycle: RunLifecycle,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from repotrial.api.app import create_app

    primary = asyncio.CancelledError("TERMINAL_CANCEL")

    class UnsupportedCancellationRegistry(InMemoryRunRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.terminal_attempts: list[RunLifecycle] = []

        async def complete(
            self,
            run_id: str,
            lifecycle: RunLifecycle,
            *,
            commit_sha: str | None,
            report_available: bool,
        ) -> RunRecord:
            self.terminal_attempts.append(lifecycle)
            if lifecycle is RunLifecycle.UNSUPPORTED:
                raise primary
            if lifecycle is RunLifecycle.INTERRUPTED and cleanup_failure is not None:
                raise cleanup_failure
            return await super().complete(
                run_id,
                lifecycle,
                commit_sha=commit_sha,
                report_available=report_available,
            )

    async def runner(*_args: object) -> GraphState:
        raise DockerSbxUnsupportedError("missing_capability")

    registry = UnsupportedCancellationRegistry()

    async def exercise() -> tuple[BaseException, RunRecord | None]:
        app = create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            with pytest.raises(asyncio.CancelledError) as raised:
                await client.post("/runs", json={"repo_url": "https://github.com/a/b"})
        return raised.value, await registry.get("run-fixed")

    propagated, record = asyncio.run(exercise())

    assert propagated is primary
    assert propagated.args == ("TERMINAL_CANCEL",)
    assert registry.terminal_attempts == [
        RunLifecycle.UNSUPPORTED,
        RunLifecycle.INTERRUPTED,
    ]
    assert record is not None
    assert record.lifecycle is expected_lifecycle
    assert "CLEANUPSECRET" not in "".join(traceback.format_exception(propagated))
    assert "CLEANUPSECRET" not in caplog.text


@pytest.mark.parametrize(
    "secondary_kind,expected_lifecycle",
    [
        ("none", RunLifecycle.INTERRUPTED),
        ("registry", RunLifecycle.RUNNING),
        ("lookup", RunLifecycle.RUNNING),
        ("cancel", RunLifecycle.RUNNING),
    ],
)
def test_post_cancellation_preserves_primary_identity_during_best_effort_interrupt(
    secondary_kind: str,
    expected_lifecycle: RunLifecycle,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from repotrial.api.app import create_app

    primary = asyncio.CancelledError("PRIMARY_CANCEL")
    secondary: BaseException | None
    if secondary_kind == "registry":
        secondary = RegistryUnavailableError("CLEANUPSECRET")
    elif secondary_kind == "lookup":
        secondary = LookupError("CLEANUPSECRET")
    elif secondary_kind == "cancel":
        secondary = asyncio.CancelledError("CLEANUPSECRET")
    else:
        secondary = None
    registry = CleanupFailingRegistry(secondary)

    async def runner(*_args: object) -> GraphState:
        raise primary

    async def exercise() -> tuple[BaseException, RunRecord | None, str | None]:
        app = create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
        response_body: str | None = None
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            try:
                response = await client.post(
                    "/runs", json={"repo_url": "https://github.com/a/b"}
                )
                response_body = response.text
            except asyncio.CancelledError as raised:
                propagated: BaseException = raised
            else:
                raise AssertionError("cancellation must propagate")
        return propagated, await registry.get("run-fixed"), response_body

    propagated, record, response_body = asyncio.run(exercise())

    assert propagated is primary
    assert propagated.args == ("PRIMARY_CANCEL",)
    assert "CLEANUPSECRET" not in "".join(traceback.format_exception(propagated))
    assert "CLEANUPSECRET" not in caplog.text
    assert response_body is None
    assert record is not None
    assert record.lifecycle is expected_lifecycle


def test_post_cancellation_after_running_insert_is_best_effort_interrupted(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    primary = asyncio.CancelledError("CREATE_CANCEL")

    class InsertThenCancelRegistry(InMemoryRunRegistry):
        async def create_running(self, run_id: str, repo_url: str) -> RunRecord:
            await super().create_running(run_id, repo_url)
            raise primary

    registry = InsertThenCancelRegistry()

    async def exercise() -> tuple[BaseException, RunRecord | None]:
        app = create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            with pytest.raises(asyncio.CancelledError) as raised:
                await client.post("/runs", json={"repo_url": "https://github.com/a/b"})
        return raised.value, await registry.get("run-fixed")

    propagated, record = asyncio.run(exercise())

    assert propagated is primary
    assert record is not None
    assert record.lifecycle is RunLifecycle.INTERRUPTED


def test_external_secondary_cancellation_cannot_replace_primary_cancellation(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    primary = asyncio.CancelledError("PRIMARY_CANCEL")

    class BlockingCleanupRegistry(InMemoryRunRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_started = asyncio.Event()

        async def complete(
            self,
            run_id: str,
            lifecycle: RunLifecycle,
            *,
            commit_sha: str | None,
            report_available: bool,
        ) -> RunRecord:
            if lifecycle is RunLifecycle.INTERRUPTED:
                self.cleanup_started.set()
                await asyncio.Event().wait()
            return await super().complete(
                run_id,
                lifecycle,
                commit_sha=commit_sha,
                report_available=report_available,
            )

    registry = BlockingCleanupRegistry()

    async def runner(*_args: object) -> GraphState:
        raise primary

    async def exercise() -> tuple[BaseException, RunRecord | None]:
        app = create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            request_task = asyncio.create_task(
                client.post("/runs", json={"repo_url": "https://github.com/a/b"})
            )
            await registry.cleanup_started.wait()
            request_task.cancel("SECONDARY_CANCEL")
            with pytest.raises(asyncio.CancelledError) as raised:
                await request_task
        return raised.value, await registry.get("run-fixed")

    propagated, record = asyncio.run(exercise())

    assert propagated is primary
    assert propagated.args == ("PRIMARY_CANCEL",)
    assert record is not None
    assert record.lifecycle is RunLifecycle.RUNNING


@pytest.mark.parametrize(
    "accept,expected_media_type,expected_marker",
    [
        (
            "application/json;q=0.1, application/json;q=0.9, text/html;q=0.5",
            "application/json",
            "json",
        ),
        (
            "application/json;q=0, */*;q=1",
            "text/html",
            "html",
        ),
        ("application/*", "application/json", "json"),
        ("text/*", "text/html", "html"),
        ("*/*", "application/json", "json"),
        (
            " Application/JSON ; charset=utf-8 ; Q=0.8 , text/html;q=0.7 ",
            "application/json",
            "json",
        ),
        (
            "application/json;profile=v1;q=0.4, text/html;level=1;q=0.8",
            "text/html",
            "html",
        ),
        (
            "text/html;q=1, application/json;q=1",
            "application/json",
            "json",
        ),
        (
            "application/json;q=0, application/json;q=0.7, text/html;q=0.6",
            "application/json",
            "json",
        ),
        (
            "application/json;q=0, application/*;q=1, */*;q=0.5",
            "text/html",
            "html",
        ),
        (
            "application/json;q=0., text/html;q=1.000",
            "text/html",
            "html",
        ),
    ],
)
def test_report_uses_specific_effective_quality_and_server_preference(
    accept: str,
    expected_media_type: str,
    expected_marker: str,
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    _prepare_report(tmp_path, registry)
    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        response = client.get("/runs/run-1/report", headers={"Accept": accept})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected_media_type)
    assert expected_marker in response.text


@pytest.mark.parametrize(
    "accept",
    [
        "application/json;q = 0",
        "application/json;q= 0",
        "application/json;q=0;q=1",
        "application/json;q=",
        "application/json;q=.5",
        "application/json;q=00.5",
        "application/json;q=1.001",
        "application/json;q=2",
        "application/json;q=-0.1",
        "application/json;q=NaN",
        "application/json;q=inf",
        "application/json;q=0.1234",
        'application/json;q="0.5"',
    ],
)
def test_report_rejects_malformed_or_out_of_range_weights(
    accept: str, tmp_path: Path
) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    _prepare_report(tmp_path, registry)
    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        response = client.get("/runs/run-1/report", headers={"Accept": accept})

    assert response.status_code == 406


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"repo_url": "http://github.com/a/b"},
        {"repo_url": " https://github.com/a/b"},
        {"repo_url": "https://github.com/a/b "},
        {"repo_url": "https://github.com/a/b\n"},
        {"repo_url": "https://github.com/" + "a" * 2_100},
        {"repo_url": "https://github.com/a/b", "model_endpoint": "https://model"},
        {"repo_url": "https://github.com/a/b", "model_name": "model"},
        {
            "repo_url": "https://github.com/a/b",
            "model_endpoint": "https://model/" + "a" * 2_100,
            "model_name": "model",
        },
        {
            "repo_url": "https://github.com/a/b",
            "model_endpoint": "https://model/v1",
            "model_name": "m" * 257,
        },
        {
            "repo_url": "https://github.com/a/b",
            "model_endpoint": "https://model/v1?token=secret",
            "model_name": "model",
        },
        {
            "repo_url": "https://github.com/a/b",
            "model_endpoint": "https://model/v1",
            "model_name": "   ",
        },
        {"repo_url": "https://github.com/a/b", "command": ["whoami"]},
    ],
)
def test_full_invalid_request_matrix_has_zero_run_side_effects(
    payload: dict[str, object], tmp_path: Path
) -> None:
    from repotrial.api.app import create_app

    registry = CountingRegistry()
    calls: list[str] = []

    def provider_factory() -> FakeSandboxProvider:
        calls.append("provider")
        return FakeSandboxProvider()

    async def runner(*_args: object) -> GraphState:
        calls.append("graph")
        return GraphState(run=_completed_state())

    with TestClient(
        create_app(
            artifacts_root=tmp_path / "artifacts",
            registry=registry,
            provider_factory=provider_factory,
            graph_runner=runner,
        )
    ) as client:
        response = client.post("/runs", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    assert registry.creates == 0
    assert calls == []
    assert not (tmp_path / "artifacts").exists()


def test_startup_interrupts_only_preexisting_running_records(tmp_path: Path) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    asyncio.run(registry.create_running("running", "https://github.com/a/running"))
    asyncio.run(registry.create_running("complete", "https://github.com/a/complete"))
    asyncio.run(
        registry.complete(
            "complete",
            RunLifecycle.COMPLETED,
            commit_sha="b" * 40,
            report_available=True,
        )
    )

    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        running = client.get("/runs/running")
        complete = client.get("/runs/complete")

    assert running.json()["lifecycle"] == "INTERRUPTED"
    assert running.json()["report_available"] is False
    assert complete.json()["lifecycle"] == "COMPLETED"
    assert complete.json()["commit_sha"] == "b" * 40
    assert complete.json()["report_available"] is True


def test_failed_startup_interrupt_sweep_never_publishes_graph_or_retries(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    class OnceFailingSweepRegistry(InMemoryRunRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.interrupt_calls = 0
            self.create_calls = 0

        async def interrupt_running(self) -> None:
            self.interrupt_calls += 1
            if self.interrupt_calls == 1:
                raise RegistryUnavailableError("SWEEPSECRET")
            await super().interrupt_running()

        async def create_running(self, run_id: str, repo_url: str) -> RunRecord:
            self.create_calls += 1
            return await super().create_running(run_id, repo_url)

    registry = OnceFailingSweepRegistry()
    asyncio.run(registry.create_running("stale-run", "https://github.com/a/stale"))
    registry.create_calls = 0
    assert asyncio.run(registry.health()) is True
    execution_calls: list[str] = []

    def provider_factory() -> FakeSandboxProvider:
        execution_calls.append("provider")
        return FakeSandboxProvider()

    async def runner(*_args: object) -> GraphState:
        execution_calls.append("graph")
        return GraphState(
            run=RunState(run_id="new-run", repo_url="https://github.com/a/b")
        )

    artifacts_root = tmp_path / "artifacts"
    with TestClient(
        create_app(
            artifacts_root=artifacts_root,
            registry=registry,
            run_id_generator=lambda: "new-run",
            provider_factory=provider_factory,
            graph_runner=runner,
        )
    ) as client:
        first_health = client.get("/healthz")
        later_health = client.get("/healthz")
        post = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    stale = asyncio.run(registry.get("stale-run"))
    assert first_health.status_code == 503
    assert later_health.status_code == 503
    assert post.status_code == 503
    assert registry.interrupt_calls == 1
    assert registry.create_calls == 0
    assert execution_calls == []
    assert not artifacts_root.exists()
    assert stale is not None
    assert stale.lifecycle is RunLifecycle.RUNNING


def test_graph_runs_once_with_correlated_thread_state_context_and_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.api.app as app_module

    provider = FakeSandboxProvider()
    graph_calls: list[tuple[GraphState, dict[str, object], object]] = []
    rendered: list[tuple[RunState, Path]] = []

    class RecordingGraph:
        async def ainvoke(
            self,
            state: GraphState,
            config: dict[str, object],
            *,
            context: object,
        ) -> dict[str, object]:
            graph_calls.append((state, config, context))
            return GraphState(run=_completed_state()).model_dump(mode="python")

    graph = RecordingGraph()
    monkeypatch.setattr(app_module, "build_run_graph", lambda **_kwargs: graph)

    def renderer(state: RunState, report_dir: Path):
        rendered.append((state, report_dir))
        return render_trial_report(state, report_dir)

    registry = InMemoryRunRegistry()
    app = app_module.create_app(
        artifacts_root=tmp_path,
        registry=registry,
        run_id_generator=lambda: "run-fixed",
        provider_factory=lambda: provider,
        report_renderer=renderer,
    )
    with TestClient(app) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 201
    assert len(graph_calls) == 1
    input_state, config, context = graph_calls[0]
    assert input_state.run == RunState(
        run_id="run-fixed", repo_url="https://github.com/a/b"
    )
    assert config["configurable"] == {"thread_id": "run-fixed"}
    assert context.provider is provider
    assert context.workspace == tmp_path / "run-fixed" / "workspace"
    assert context.artifact_dir == tmp_path / "run-fixed" / "evidence"
    assert context.overlay_dir == context.workspace / ".repotrial-overlays"
    assert context.accepted_compose_dir == context.workspace / ".repotrial-accepted"
    assert rendered == [(_completed_state(), tmp_path / "run-fixed" / "report")]
    record = asyncio.run(registry.get("run-fixed"))
    assert record is not None
    assert record.commit_sha == "a" * 40
    assert record.report_available is True


def test_registry_and_checkpoint_prerequisite_errors_are_stable(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    registry = CountingRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path / "artifacts",
            registry=registry,
            database_url="not-a-postgresql-url",
        )
    ) as client:
        checkpoint = client.get("/healthz")
        checkpoint_post = client.post(
            "/runs", json={"repo_url": "https://github.com/a/b"}
        )

    unavailable = InMemoryRunRegistry(available=False)
    with TestClient(create_app(registry=unavailable)) as client:
        registry_health = client.get("/healthz")
        registry_get = client.get("/runs/run-1")

    assert checkpoint.status_code == 503
    assert checkpoint_post.status_code == 503
    assert registry.creates == 0
    assert not (tmp_path / "artifacts").exists()
    assert registry_health.status_code == 503
    assert registry_get.status_code == 503


def test_registry_create_failure_returns_503_before_artifacts(tmp_path: Path) -> None:
    from repotrial.api.app import create_app

    class CreateFailingRegistry(InMemoryRunRegistry):
        async def create_running(self, run_id: str, repo_url: str) -> RunRecord:
            raise RegistryUnavailableError()

    with TestClient(
        create_app(
            artifacts_root=tmp_path / "artifacts",
            registry=CreateFailingRegistry(),
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 503
    assert not (tmp_path / "artifacts").exists()


def test_registry_terminal_write_failure_returns_stable_internal_error(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    class FirstCompleteFailingRegistry(InMemoryRunRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.completes = 0

        async def complete(
            self,
            run_id: str,
            lifecycle: RunLifecycle,
            *,
            commit_sha: str | None,
            report_available: bool,
        ) -> RunRecord:
            self.completes += 1
            if self.completes == 1:
                raise RegistryUnavailableError()
            return await super().complete(
                run_id,
                lifecycle,
                commit_sha=commit_sha,
                report_available=report_available,
            )

    async def runner(*_args: object) -> GraphState:
        return GraphState(run=_completed_state())

    registry = FirstCompleteFailingRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
    ) as client:
        response = client.post("/runs", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 500
    assert response.json() == {
        "detail": {"run_id": "run-fixed", "outcome": "internal_error"}
    }
    record = asyncio.run(registry.get("run-fixed"))
    assert record is not None
    assert record.lifecycle is RunLifecycle.INTERNAL_ERROR
    assert record.report_available is False


@pytest.mark.parametrize("entry_kind", ["missing", "directory"])
def test_report_rejects_missing_and_non_regular_final_entry(
    entry_kind: str, tmp_path: Path
) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    _prepare_report(tmp_path, registry)
    report_path = tmp_path / "run-1" / "report" / "trial-report.json"
    report_path.unlink()
    if entry_kind == "directory":
        report_path.mkdir()

    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        response = client.get("/runs/run-1/report")

    assert response.status_code == 409


@pytest.mark.parametrize("link_kind", ["parent", "final"])
def test_report_endpoint_rejects_parent_and_final_symlinks(
    link_kind: str, tmp_path: Path
) -> None:
    from repotrial.api.app import create_app

    registry = InMemoryRunRegistry()
    asyncio.run(registry.create_running("run-1", "https://github.com/a/b"))
    asyncio.run(
        registry.complete(
            "run-1",
            RunLifecycle.COMPLETED,
            commit_sha="a" * 40,
            report_available=True,
        )
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "trial-report.json").write_text('{"outside":true}', encoding="utf-8")
    run_path = tmp_path / "run-1"
    run_path.mkdir()
    report_dir = run_path / "report"
    try:
        if link_kind == "parent":
            report_dir.symlink_to(outside, target_is_directory=True)
        else:
            report_dir.mkdir()
            (report_dir / "trial-report.json").symlink_to(outside / "trial-report.json")
    except OSError as error:
        pytest.skip(f"symlinks unsupported: {error}")

    with TestClient(create_app(artifacts_root=tmp_path, registry=registry)) as client:
        response = client.get("/runs/run-1/report")

    assert response.status_code == 409


def test_report_rejects_traversal_from_untrusted_registry_metadata(
    tmp_path: Path,
) -> None:
    from repotrial.api.app import create_app

    now = datetime.now(UTC)
    traversal = RunRecord(
        run_id="..",
        repo_url="https://github.com/a/b",
        lifecycle=RunLifecycle.COMPLETED,
        created_at=now,
        updated_at=now,
        commit_sha="a" * 40,
        report_available=True,
    )

    class TraversalRegistry(InMemoryRunRegistry):
        async def get(self, run_id: str) -> RunRecord | None:
            return traversal

    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    outside = tmp_path / "report"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "trial-report.json").write_text('{"secret":true}', encoding="utf-8")
    with TestClient(
        create_app(artifacts_root=artifacts_root, registry=TraversalRegistry())
    ) as client:
        response = client.get("/runs/run-1/report")

    assert response.status_code == 409
    assert "secret" not in response.text


def test_report_supports_default_relative_artifacts_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repotrial.api.app import create_app

    monkeypatch.chdir(tmp_path)
    root = Path("relative-artifacts")
    registry = InMemoryRunRegistry()
    _prepare_report(root, registry)
    with TestClient(create_app(artifacts_root=root, registry=registry)) as client:
        response = client.get("/runs/run-1/report")

    assert response.status_code == 200
    assert response.json() == {"representation": "json"}


def test_runtime_secret_sentinels_never_reach_response_logs_or_registry(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from repotrial.api.app import create_app

    monkeypatch.setenv("REPOTRIAL_MODEL_API_KEY", "MODELSECRET")

    async def runner(*_args: object) -> GraphState:
        raise LookupError("DBSECRET MODELSECRET RUNNERSECRET")

    registry = InMemoryRunRegistry()
    with TestClient(
        create_app(
            artifacts_root=tmp_path,
            registry=registry,
            run_id_generator=lambda: "run-fixed",
            graph_runner=runner,
        )
    ) as client:
        response = client.post(
            "/runs",
            json={
                "repo_url": "https://github.com/a/b",
                "model_endpoint": "https://model.example/v1",
                "model_name": "model",
            },
        )

    record = asyncio.run(registry.get("run-fixed"))
    assert record is not None
    exposed = response.text + caplog.text + repr(record)
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"run_id": "run-fixed", "outcome": "internal_error"}
    }
    assert "DBSECRET" not in exposed
    assert "MODELSECRET" not in exposed
    assert "RUNNERSECRET" not in exposed
    for artifact in tmp_path.rglob("*"):
        if artifact.is_file():
            content = artifact.read_text(encoding="utf-8")
            assert "DBSECRET" not in content
            assert "MODELSECRET" not in content
            assert "RUNNERSECRET" not in content


def test_deployment_compose_is_exactly_the_bounded_api_postgres_topology() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "docker-compose.yml"
    parsed = YAML(typ="safe", pure=True).load(compose_path.read_text(encoding="utf-8"))

    services = parsed["services"]
    volumes = parsed["volumes"]
    assert set(services) == {"api", "postgres"}
    assert set(volumes) == {"repotrial-artifacts", "repotrial-postgres"}
    assert services["api"]["ports"] == ["127.0.0.1:${REPOTRIAL_API_PORT:-8000}:8000"]
    assert services["api"]["volumes"] == ["repotrial-artifacts:/app/artifacts"]
    assert services["postgres"]["volumes"] == [
        "repotrial-postgres:/var/lib/postgresql/data"
    ]
    assert services["api"].get("privileged") is not True
    assert services["postgres"].get("privileged") is not True
    assert all(
        "/var/run/docker.sock" not in str(mount)
        for service in services.values()
        for mount in service.get("volumes", [])
    )
    assert all(
        forbidden not in service_name.lower()
        for service_name in services
        for forbidden in ("ui", "model", "worker", "sandbox")
    )
