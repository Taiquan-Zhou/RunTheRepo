from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repotrial.doctor import DoctorCheck, DoctorReport
from repotrial.local_web.app import ModelSettingsInput, create_app
from repotrial.local_web.model_discovery import ModelDiscoveryError
from repotrial.local_web.runner import CliRequest, Report, RunnerBusyError
from repotrial.local_web.settings import ModelSettingsStore, ModelSettingsUpdate


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
    client = TestClient(
        create_app(
            tmp_path,
            runner=actual,
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    )
    return client, actual


def _token(client: TestClient) -> str:
    response = client.get("/", headers={"host": "127.0.0.1"})
    assert response.status_code == 200
    marker = 'name="csrf-token" content="'
    return response.text.split(marker, 1)[1].split('"', 1)[0]


def test_usage_guide_view_is_readable_and_does_not_load_console(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    response = client.get("/?view=guide&lang=en", headers={"host": "127.0.0.1"})
    assert response.status_code == 200
    assert "User guide" in response.text
    assert "8080:3000" in response.text
    assert "docker ps" in response.text
    assert "{{.Names}}" in response.text
    assert "{{.Ports}}" in response.text
    assert "my-web  127.0.0.1:8080-&gt;3000/tcp" in response.text
    assert 'id="trial-form"' not in response.text
    assert "<script" not in response.text

    chinese = client.get("/?view=guide&lang=unsupported", headers={"host": "127.0.0.1"})
    assert chinese.status_code == 200
    assert "使用文档" in chinese.text
    assert "{{.Names}}" in chinese.text
    assert "{{.Ports}}" in chinese.text
    assert "最后填：3000" in chinese.text


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

    class ReadyServices:
        def ensure_ready(self) -> DoctorReport:
            return DoctorReport(
                checks=(DoctorCheck("sbx_daemon", "PASS", True, "running", None),)
            )

    doctor = lambda: DoctorReport(
        checks=(
            DoctorCheck("linux", "PASS", True, "ok", None),
            DoctorCheck("sbx_diagnose", "PASS", True, "ok", None),
            DoctorCheck("sbx_inventory", "PASS", True, "ok", None),
        )
    )
    client = TestClient(
        create_app(
            tmp_path,
            runner=runner,
            doctor=doctor,
            services=ReadyServices(),
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    )
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

    class ReadyServices:
        def ensure_ready(self) -> DoctorReport:
            return DoctorReport(
                checks=tuple(
                    DoctorCheck(name, "PASS", True, "running", None)
                    for name in (
                        "sbx_daemon",
                        "sbx_diagnose",
                        "sbx_inventory",
                    )
                )
            )

    fake_runner = FakeRunner()
    with TestClient(
        create_app(
            tmp_path,
            runner=fake_runner,
            services=ReadyServices(),
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    ) as client:
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
    "endpoint",
    [
        "https://model.example/v1",
        "http://model.example/v1",
        "ftp://model.example/v1",
        "https://user:secret@model.example/v1",
        "https:///v1",
    ],
)
def test_job_rejects_legacy_model_fields(tmp_path: Path, endpoint: str) -> None:
    client, _ = _client(tmp_path)
    headers = {"host": "127.0.0.1", "x-csrf-token": _token(client)}
    payload = {
        "url": "https://github.com/acme/demo",
        "commit_sha": "a" * 40,
        "container_port": 8080,
        "model_endpoint": endpoint,
        "model_name": "model-x",
    }
    assert client.post("/api/jobs", json=payload, headers=headers).status_code == 422


def test_model_settings_api_never_returns_key_and_supports_clear(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    store = ModelSettingsStore(tmp_path / "settings.json")
    client = TestClient(create_app(tmp_path, runner=runner, settings_store=store))
    token = _token(client)
    headers = {"host": "127.0.0.1", "x-csrf-token": token}

    initial = client.get("/api/settings/model", headers=headers)
    assert initial.status_code == 200
    assert initial.json() == {
        "provider": None,
        "endpoint": None,
        "model_name": None,
        "api_key_configured": False,
    }
    saved = client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "api_key": "secret-value"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["api_key_configured"] is True
    assert "secret-value" not in saved.text
    fetched = client.get("/api/settings/model", headers=headers)
    assert "secret-value" not in fetched.text
    assert fetched.json()["endpoint"] == "https://api.deepseek.com"

    cleared = client.delete("/api/settings/model/key", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["api_key_configured"] is False
    cleared_all = client.delete("/api/settings/model", headers=headers)
    assert cleared_all.status_code == 200
    assert cleared_all.json() == {
        "provider": None,
        "endpoint": None,
        "model_name": None,
        "api_key_configured": False,
    }
    assert (
        client.get("/api/settings/model", headers=headers).json() == cleared_all.json()
    )


def test_model_settings_api_requires_csrf_and_job_payload_is_secret_free(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    store = ModelSettingsStore(tmp_path / "settings.json")
    client = TestClient(create_app(tmp_path, runner=runner, settings_store=store))
    token = _token(client)
    payload = {
        "url": "https://github.com/acme/demo",
        "commit_sha": "a" * 40,
        "container_port": 8080,
    }
    assert (
        client.put(
            "/api/settings/model",
            json={"provider": "deepseek", "api_key": "secret-value"},
            headers={"host": "127.0.0.1", "x-csrf-token": "wrong"},
        ).status_code
        == 403
    )
    headers = {"host": "127.0.0.1", "x-csrf-token": token}
    assert (
        client.put(
            "/api/settings/model",
            json={"provider": "deepseek", "api_key": "secret-value"},
            headers=headers,
        ).status_code
        == 200
    )
    assert client.post("/api/jobs", json=payload, headers=headers).status_code == 202
    assert runner.submissions[-1]["model_api_key"] == "secret-value"
    assert runner.submissions[-1]["model_endpoint"] == "https://api.deepseek.com"


def test_unready_required_service_blocks_job_submission(tmp_path: Path) -> None:
    class UnreadyServices:
        def ensure_ready(self) -> DoctorReport:
            return DoctorReport(
                checks=(DoctorCheck("sbx_daemon", "FAIL", True, "down", "start"),)
            )

    runner = FakeRunner()
    with TestClient(
        create_app(
            tmp_path,
            runner=runner,
            services=UnreadyServices(),
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    ) as client:
        token = _token(client)
        response = client.post(
            "/api/jobs",
            json={
                "url": "https://github.com/acme/demo",
                "commit_sha": "a" * 40,
                "container_port": 8080,
            },
            headers={"host": "127.0.0.1", "x-csrf-token": token},
        )
        assert response.status_code == 409
        assert runner.submissions == []


def test_secret_validation_and_repr_never_echo_key(tmp_path: Path) -> None:
    secret = "x" * 4097
    repr_secret = "repr-short-secret"
    store = ModelSettingsStore(tmp_path / "settings.json")
    client = TestClient(create_app(tmp_path, settings_store=store))
    token = _token(client)
    response = client.put(
        "/api/settings/model",
        json={"provider": "deepseek", "api_key": secret},
        headers={"host": "127.0.0.1", "x-csrf-token": token},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert repr_secret not in repr(
        ModelSettingsInput(provider="deepseek", api_key=repr_secret)
    )
    assert repr_secret not in repr(
        CliRequest(
            url="https://github.com/acme/demo",
            commit_sha="a" * 40,
            container_port=8080,
            model_api_key=repr_secret,
        )
    )


def test_model_endpoint_query_and_fragment_are_rejected_without_echo(
    tmp_path: Path,
) -> None:
    store = ModelSettingsStore(tmp_path / "settings.json")
    client = TestClient(create_app(tmp_path, settings_store=store))
    token = _token(client)
    for endpoint in (
        "https://model.example/v1?api_key=secret-value",
        "https://model.example/v1#secret-value",
    ):
        response = client.put(
            "/api/settings/model",
            json={
                "provider": "custom",
                "endpoint": endpoint,
                "model_name": "model",
            },
            headers={"host": "127.0.0.1", "x-csrf-token": token},
        )
        assert response.status_code == 422
        assert "secret-value" not in response.text


def test_lifespan_empty_service_report_blocks_submission(tmp_path: Path) -> None:
    class EmptyServices:
        def ensure_ready(self) -> DoctorReport:
            return DoctorReport(checks=())

    runner = FakeRunner()
    with TestClient(
        create_app(
            tmp_path,
            runner=runner,
            services=EmptyServices(),
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    ) as client:
        token = _token(client)
        response = client.post(
            "/api/jobs",
            json={
                "url": "https://github.com/acme/demo",
                "commit_sha": "a" * 40,
                "container_port": 8080,
            },
            headers={"host": "127.0.0.1", "x-csrf-token": token},
        )
        assert response.status_code == 409
        assert runner.submissions == []


@pytest.mark.parametrize("missing", ["sbx_daemon", "sbx_diagnose", "sbx_inventory"])
def test_incomplete_service_report_is_not_verified(
    tmp_path: Path, missing: str
) -> None:
    checks = tuple(
        DoctorCheck(name, "PASS", True, "ok", None)
        for name in ("sbx_daemon", "sbx_diagnose", "sbx_inventory")
        if name != missing
    )

    class IncompleteServices:
        def ensure_ready(self) -> DoctorReport:
            return DoctorReport(checks=checks)

    def incomplete_doctor() -> DoctorReport:
        return DoctorReport(checks=checks)

    runner = FakeRunner()
    with TestClient(
        create_app(
            tmp_path,
            runner=runner,
            services=IncompleteServices(),
            doctor=incomplete_doctor,
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    ) as client:
        token = _token(client)
        page = client.get("/", headers={"host": "127.0.0.1"})
        assert '"ready": false' in page.text
        headers = {"host": "127.0.0.1", "x-csrf-token": token}
        doctor = client.post("/api/doctor", headers=headers)
        assert doctor.status_code == 200
        assert doctor.json()["ready"] is False
        job = client.post(
            "/api/jobs",
            json={
                "url": "https://github.com/acme/demo",
                "commit_sha": "a" * 40,
                "container_port": 8080,
            },
            headers=headers,
        )
        assert job.status_code == 409
        assert runner.submissions == []


@pytest.mark.parametrize(
    "endpoint",
    ["https://[broken", "https://:443/v1", "https://example.com:bad/v1"],
)
def test_model_endpoint_errors_are_secret_safe_422(
    tmp_path: Path, endpoint: str
) -> None:
    client = TestClient(
        create_app(
            tmp_path, settings_store=ModelSettingsStore(tmp_path / "settings.json")
        )
    )
    token = _token(client)
    response = client.put(
        "/api/settings/model",
        json={
            "provider": "custom",
            "endpoint": endpoint,
            "model_name": "model",
            "api_key": "endpoint-secret",
        },
        headers={"host": "127.0.0.1", "x-csrf-token": token},
    )

    assert response.status_code == 422
    assert "endpoint-secret" not in response.text


class RecordingModelDiscovery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, endpoint: str, api_key: str | None) -> tuple[str, ...]:
        self.calls.append((endpoint, api_key))
        return ("qwen", "deepseek-chat")


def test_model_discovery_reuses_only_key_for_same_endpoint(
    tmp_path: Path,
) -> None:
    store = ModelSettingsStore(tmp_path / "settings.json")
    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="https://old.example/v1",
            model_name="old-model",
            api_key="stored-secret",
        )
    )
    discovery = RecordingModelDiscovery()
    client = TestClient(
        create_app(
            tmp_path,
            settings_store=store,
            model_discovery=discovery,
        )
    )
    token = _token(client)
    headers = {"host": "127.0.0.1", "x-csrf-token": token}

    same = client.post(
        "/api/settings/model/discover",
        json={"endpoint": "https://old.example/v1", "api_key": ""},
        headers=headers,
    )
    changed = client.post(
        "/api/settings/model/discover",
        json={"endpoint": "https://new.example/v1", "api_key": ""},
        headers=headers,
    )
    supplied = client.post(
        "/api/settings/model/discover",
        json={"endpoint": "https://new.example/v1", "api_key": "new-secret"},
        headers=headers,
    )

    assert same.status_code == 200
    assert changed.status_code == 200
    assert supplied.status_code == 200
    assert discovery.calls == [
        ("https://old.example/v1", "stored-secret"),
        ("https://new.example/v1", None),
        ("https://new.example/v1", "new-secret"),
    ]
    assert (
        client.post(
            "/api/settings/model/discover",
            json={"endpoint": "https://old.example/v1", "api_key": ""},
            headers={**headers, "x-csrf-token": "wrong"},
        ).status_code
        == 403
    )


def test_model_discovery_accepts_async_injected_service(tmp_path: Path) -> None:
    async def discovery(_: str, __: str | None) -> tuple[str, ...]:
        return ("async-model",)

    client = TestClient(
        create_app(
            tmp_path,
            model_discovery=discovery,
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    )
    token = _token(client)
    response = client.post(
        "/api/settings/model/discover",
        json={"endpoint": "https://model.example/v1", "api_key": ""},
        headers={"host": "127.0.0.1", "x-csrf-token": token},
    )

    assert response.status_code == 200
    assert response.json() == {"models": ["async-model"]}


def test_model_discovery_error_is_safe(tmp_path: Path) -> None:
    def fail(_: str, __: str | None) -> tuple[str, ...]:
        raise ModelDiscoveryError("unauthorized")

    client = TestClient(
        create_app(
            tmp_path,
            model_discovery=fail,
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    )
    token = _token(client)
    response = client.post(
        "/api/settings/model/discover",
        json={"endpoint": "https://model.example/v1", "api_key": "secret-value"},
        headers={"host": "127.0.0.1", "x-csrf-token": token},
    )

    assert response.status_code == 502
    assert "secret-value" not in response.text
    assert "upstream" not in response.text.lower()


def test_model_discovery_rejects_unsafe_key_without_calling_upstream(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def discovery(endpoint: str, api_key: str | None) -> tuple[str, ...]:
        calls.append((endpoint, api_key))
        return ("unused",)

    client = TestClient(
        create_app(
            tmp_path,
            model_discovery=discovery,
            settings_store=ModelSettingsStore(tmp_path / "settings.json"),
        )
    )
    token = _token(client)
    response = client.post(
        "/api/settings/model/discover",
        json={"endpoint": "https://model.example/v1", "api_key": "bad\rkey"},
        headers={"host": "127.0.0.1", "x-csrf-token": token},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "api_key is invalid"
    assert calls == []


from repotrial.local_web.runner import JobNotActiveError, JobNotFoundError


class StopRouteRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.stop_calls: list[str] = []

    async def stop(self, job_id: str) -> dict[str, object]:
        if job_id == "missing":
            raise JobNotFoundError(job_id)
        if not self.active:
            raise JobNotActiveError(job_id)
        self.stop_calls.append(job_id)
        return {"job_id": job_id, "state": "running", "control": "stopping"}


def test_stop_route_enforces_guard_and_maps_runner_outcomes(tmp_path: Path) -> None:
    runner = StopRouteRunner()
    runner.active = True
    client, _ = _client(tmp_path, runner)
    token = _token(client)
    valid_headers = {"host": "127.0.0.1", "x-csrf-token": token}

    wrong = client.post(
        "/api/jobs/active/stop",
        headers={**valid_headers, "x-csrf-token": "wrong"},
    )
    assert wrong.status_code == 403
    external = client.post(
        "/api/jobs/active/stop",
        headers={**valid_headers, "origin": "https://evil.example"},
    )
    assert external.status_code == 403

    accepted = client.post("/api/jobs/active/stop", headers=valid_headers)
    assert accepted.status_code == 202
    assert accepted.json()["control"] == "stopping"
    assert runner.stop_calls == ["active"]

    missing = client.post("/api/jobs/missing/stop", headers=valid_headers)
    assert missing.status_code == 404

    runner.active = False
    ended = client.post("/api/jobs/active/stop", headers=valid_headers)
    assert ended.status_code == 409
