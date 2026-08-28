import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repotrial.agent.state import GraphState
from repotrial.api.registry import InMemoryRunRegistry, RunLifecycle
from repotrial.domain.models import RunState


class CountingRegistry(InMemoryRunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.creates = 0

    async def create_running(self, run_id: str, repo_url: str):
        self.creates += 1
        return await super().create_running(run_id, repo_url)


def test_healthz_reports_ready_control_plane() -> None:
    from repotrial.api.app import create_app

    with TestClient(create_app(registry=InMemoryRunRegistry())) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
