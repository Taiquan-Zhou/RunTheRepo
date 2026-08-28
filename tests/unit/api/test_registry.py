import asyncio
import traceback
from dataclasses import asdict

import pytest

import repotrial.api.registry as registry_module
from repotrial.api.registry import (
    InMemoryRunRegistry,
    PostgresRunRegistry,
    RegistryUnavailableError,
    RunLifecycle,
    RunRecord,
)


async def _invoke_boundary(registry: PostgresRunRegistry, operation: str) -> object:
    if operation == "initialize":
        return await registry.initialize()
    if operation == "get":
        return await registry.get("run-1")
    if operation == "execute":
        return await registry._execute("SELECT %s", (1,))
    if operation == "interrupt":
        return await registry.interrupt_running()
    if operation == "fetch_one":
        return await registry._fetch_one("SELECT %s", (1,))
    if operation == "create":
        return await registry.create_running("run-1", "https://github.com/a/b")
    if operation == "complete":
        return await registry.complete(
            "run-1",
            RunLifecycle.COMPLETED,
            commit_sha="a" * 40,
            report_available=True,
        )
    raise AssertionError(f"unknown operation: {operation}")


def _install_connect_failure(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    class FailingConnection:
        @staticmethod
        async def connect(_database_url: str) -> object:
            raise failure

    monkeypatch.setattr(registry_module, "AsyncConnection", FailingConnection)


@pytest.mark.parametrize(
    "operation",
    ["initialize", "get", "execute", "interrupt", "fetch_one", "create", "complete"],
)
def test_postgres_boundaries_translate_arbitrary_adapter_failures_without_secrets(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = LookupError("DBSECRET")
    _install_connect_failure(monkeypatch, failure)
    registry = PostgresRunRegistry("postgresql://user:DBSECRET@database/app")

    with pytest.raises(RegistryUnavailableError) as raised:
        asyncio.run(_invoke_boundary(registry, operation))

    formatted = "".join(traceback.format_exception(raised.value))
    assert str(raised.value) == ""
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert "DBSECRET" not in formatted
    assert "DBSECRET" not in caplog.text


def test_postgres_health_returns_false_for_arbitrary_adapter_failure_without_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_connect_failure(monkeypatch, LookupError("DBSECRET"))
    registry = PostgresRunRegistry("postgresql://user:DBSECRET@database/app")

    healthy = asyncio.run(registry.health())

    assert healthy is False
    assert "DBSECRET" not in caplog.text


@pytest.mark.parametrize(
    "operation",
    [
        "initialize",
        "health",
        "get",
        "execute",
        "interrupt",
        "fetch_one",
        "create",
        "complete",
    ],
)
def test_postgres_boundaries_propagate_original_cancellation_identity(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = asyncio.CancelledError(f"{operation} cancellation")
    _install_connect_failure(monkeypatch, cancellation)
    registry = PostgresRunRegistry("postgresql://database/app")

    async def exercise() -> object:
        if operation == "health":
            return await registry.health()
        return await _invoke_boundary(registry, operation)

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(exercise())

    assert raised.value is cancellation
    assert raised.value.args == (f"{operation} cancellation",)


def test_registry_records_only_lifecycle_metadata_and_interrupts_running() -> None:
    registry = InMemoryRunRegistry()

    async def exercise() -> tuple[RunRecord | None, RunRecord | None]:
        running = await registry.create_running(
            "run-1", "https://github.com/owner/repository"
        )
        completed = await registry.create_running(
            "run-2", "https://github.com/owner/completed"
        )
        await registry.complete(
            completed.run_id,
            RunLifecycle.COMPLETED,
            commit_sha="a" * 40,
            report_available=True,
        )
        await registry.interrupt_running()
        return await registry.get(running.run_id), await registry.get(completed.run_id)

    interrupted, completed = asyncio.run(exercise())

    assert interrupted is not None
    assert completed is not None
    assert interrupted.lifecycle is RunLifecycle.INTERRUPTED
    assert completed.lifecycle is RunLifecycle.COMPLETED
    assert set(asdict(interrupted)) == {
        "run_id",
        "repo_url",
        "lifecycle",
        "created_at",
        "updated_at",
        "commit_sha",
        "report_available",
    }
