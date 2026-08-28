import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repotrial.api.registry import InMemoryRunRegistry, RunLifecycle


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
