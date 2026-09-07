from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repotrial.doctor import DoctorCheck, DoctorReport
from repotrial.local_web.app import create_app
from repotrial.local_web.runner import Report, RunnerBusyError


class FakeRunner:
    def __init__(self) -> None:
        self.active = False
        self.submissions: list[dict[str, object]] = []

    async def submit(self, request: object) -> str:
        if self.active:
            raise RunnerBusyError("a trial is already running")
        self.active = True
        self.submissions.append(asdict(request))
        return "11111111-1111-4111-8111-111111111111"

    async def status(self, job_id: str) -> dict[str, object]:
        return {"job_id": job_id, "state": "running", "elapsed_seconds": 0.0}

    async def report(self, job_id: str, kind: str) -> Report | None:
        if job_id != "known":
            return None
        return Report(
            body=b"<script>blocked</script>" if kind == "html" else b'{"ok":true}',
            media_type="text/html" if kind == "html" else "application/json",
        )

    async def shutdown(self) -> None:
        self.active = False


def _client(
    tmp_path: Path, runner: FakeRunner | None = None
) -> tuple[TestClient, FakeRunner]:
    actual = runner or FakeRunner()
    client = TestClient(create_app(tmp_path, runner=actual))
    return client, actual


def _token(client: TestClient) -> str:
    response = client.get("/", headers={"host": "127.0.0.1"})
    assert response.status_code == 200
    marker = 'name="csrf-token" content="'
    return response.text.split(marker, 1)[1].split('"', 1)[0]


def test_submit_rejects_wrong_token_and_external_origin(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    token = _token(client)
    payload = {
        "url": "https://github.com/acme/demo",
        "commit_sha": "a" * 40,
        "container_port": 8080,
    }

    wrong = client.post(
        "/api/jobs",
        json=payload,
        headers={"host": "127.0.0.1", "x-csrf-token": "wrong"},
    )
    assert wrong.status_code == 403
    external = client.post(
        "/api/jobs",
        json=payload,
        headers={
            "host": "127.0.0.1",
            "origin": "https://evil.example",
            "x-csrf-token": token,
        },
    )
    assert external.status_code == 403


def test_second_active_job_is_rejected(tmp_path: Path) -> None:
    client, runner = _client(tmp_path)
    token = _token(client)
    payload = {
        "url": "https://github.com/acme/demo",
        "commit_sha": "b" * 40,
        "container_port": 8080,
    }
    headers = {"host": "localhost", "x-csrf-token": token}
    assert client.post("/api/jobs", json=payload, headers=headers).status_code == 202
    runner.active = True
    assert client.post("/api/jobs", json=payload, headers=headers).status_code == 409


def test_terminal_evidence_must_match_job_and_reports(tmp_path: Path) -> None:
    runner = FakeRunner()
    client, _ = _client(tmp_path, runner)
    _token(client)
    response = client.get(
        "/api/jobs/11111111-1111-4111-8111-111111111111",
        headers={"host": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "running"


def test_host_must_be_loopback(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_doctor_is_idle_only_and_input_is_strictly_validated(tmp_path: Path) -> None:
    client, runner = _client(tmp_path)
    token = _token(client)
    headers = {"host": "127.0.0.1", "x-csrf-token": token}
    doctor = lambda: DoctorReport(
        checks=(DoctorCheck("linux", "PASS", True, "ok", None),)
    )
    client = TestClient(create_app(tmp_path, runner=runner, doctor=doctor))
    token = _token(client)
    headers["x-csrf-token"] = token
    result = client.post("/api/doctor", headers=headers)
    assert result.status_code == 200
    assert result.json()["ready"] is True
    runner.active = True
    assert client.post("/api/doctor", headers=headers).status_code == 409
    invalid = client.post(
        "/api/jobs",
        json={
            "url": "https://github.com/a/b",
            "commit_sha": "A" * 40,
            "container_port": 8080,
        },
        headers=headers,
    )
    assert invalid.status_code == 422


def test_report_routes_are_fixed_and_html_is_sandboxed(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert (
        client.get(
            "/api/jobs/unknown/report/json", headers={"host": "localhost"}
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/jobs/known/report/nope", headers={"host": "localhost"}
        ).status_code
        == 404
    )
    html = client.get("/api/jobs/known/report/html", headers={"host": "localhost"})
    assert html.status_code == 200
    assert "sandbox" in html.headers["content-security-policy"]
    assert client.get(
        "/api/jobs/known/report/json", headers={"host": "localhost"}
    ).json() == {"ok": True}


@pytest.mark.parametrize(
    "changes",
    [
        {"compose_path": "../compose.yml"},
        {"compose_path": "/tmp/compose.yml"},
        {"model_endpoint": "https://model.example/v1"},
        {"model_name": "model-x"},
    ],
)
def test_job_input_rejects_unsafe_or_unpaired_optional_values(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    client, _ = _client(tmp_path)
    headers = {"host": "127.0.0.1", "x-csrf-token": _token(client)}
    payload: dict[str, object] = {
        "url": "https://github.com/acme/demo",
        "commit_sha": "a" * 40,
        "container_port": 8080,
    }
    payload.update(changes)
    assert client.post("/api/jobs", json=payload, headers=headers).status_code == 422


def test_same_origin_requires_matching_scheme_host_and_port(tmp_path: Path) -> None:
    payload = {
        "url": "https://github.com/acme/demo.git",
        "commit_sha": "a" * 40,
        "container_port": 8080,
    }
    fake_runner = FakeRunner()
    with TestClient(create_app(tmp_path, runner=fake_runner)) as client:
        token = _token(client)
        headers = {
            "host": "127.0.0.1:8765",
            "origin": "http://127.0.0.1:8765",
            "x-csrf-token": token,
        }
        assert (
            client.post("/api/jobs", json=payload, headers=headers).status_code == 202
        )
        assert fake_runner.submissions[-1]["url"] == "https://github.com/acme/demo"
        assert (
            client.post(
                "/api/jobs",
                json=payload,
                headers={**headers, "origin": "http://127.0.0.1:8764"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/jobs",
                json=payload,
                headers={**headers, "origin": "http://127.0.0.1:notaport"},
            ).status_code
            == 403
        )


@pytest.mark.parametrize(
    ("endpoint", "expected_status"),
    [
        ("https://model.example/v1", 202),
        ("http://model.example/v1", 202),
        ("ftp://model.example/v1", 422),
        ("https://user:secret@model.example/v1", 422),
        ("https:///v1", 422),
    ],
)
def test_model_endpoint_uri_validation(
    tmp_path: Path, endpoint: str, expected_status: int
) -> None:
    client, _ = _client(tmp_path)
    headers = {"host": "127.0.0.1", "x-csrf-token": _token(client)}
    payload = {
        "url": "https://github.com/acme/demo",
        "commit_sha": "a" * 40,
        "container_port": 8080,
        "model_endpoint": endpoint,
        "model_name": "model-x",
    }
    assert (
        client.post("/api/jobs", json=payload, headers=headers).status_code
        == expected_status
    )
