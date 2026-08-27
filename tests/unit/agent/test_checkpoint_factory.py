import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import ModuleType

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from pydantic import BaseModel

from repotrial.agent.graph import build_run_graph
from repotrial.agent.state import GraphState
from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    Journey,
    JourneyAssertion,
    JourneyResult,
    JourneyStep,
    Mutation,
    ObservationSnapshot,
    RiskFinding,
    RunState,
)


class _UnlistedPayload(BaseModel):
    command: str


class _FakeAsyncSaver:
    def __init__(
        self,
        events: list[str],
        *,
        setup_error: RuntimeError | None = None,
    ) -> None:
        self.events = events
        self.serde: SerializerProtocol | None = None
        self.setup_error = setup_error

    async def setup(self) -> None:
        self.events.append("setup")
        if self.setup_error is not None:
            raise self.setup_error


class _FailingEntryContext:
    def __init__(self, events: list[str], error: RuntimeError) -> None:
        self.events = events
        self.error = error

    async def __aenter__(self) -> _FakeAsyncSaver:
        self.events.append("enter")
        raise self.error

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc_value, traceback
        self.events.append("exit")
        return False


def _checkpoint_module() -> ModuleType:
    return importlib.import_module("repotrial.agent.checkpoint")


def _checkpoint_values() -> tuple[object, ...]:
    assertion = JourneyAssertion(
        kind="status_code",
        target="response.status",
        expected=200,
    )
    step = JourneyStep(
        step_id="health-request",
        tool="http",
        action="request",
        params={"method": "GET", "path": "/health"},
        assertions=[assertion],
    )
    journey = Journey(journey_id="health", name="Health", steps=[step])
    result = JourneyResult(
        journey_id=journey.journey_id,
        verdict=Verdict.PASS,
        passed_steps=1,
        total_steps=1,
        evidence_paths=["artifacts/health.json"],
    )
    risk = RiskFinding(
        finding_id="root-user:web",
        kind="root_user",
        service="web",
        severity=80,
        evidence={"user": "0"},
    )
    observation = ObservationSnapshot(
        inspect={"web": {"Config": {"User": "1000"}}},
        process_events=[{"pid": 1, "command": "web"}],
    )
    mutation = Mutation(
        mutation_id="policy:set_non_root:web",
        type=MutationType.SET_NON_ROOT,
        service="web",
        params={"user": "1000"},
    )
    experiment = ExperimentRecord(
        experiment_id="experiment-1",
        parent_config_hash="sha256:parent",
        candidate_config_hash="sha256:candidate",
        mutation=mutation,
        boot=Verdict.PASS,
        journeys=[result],
        before=observation,
        after=observation,
        verdict=ExperimentVerdict.KEEP,
        reason="journeys_passed",
    )
    run = RunState(
        run_id="checkpoint-run",
        repo_url="https://github.com/example/repo",
        commit_sha="a" * 40,
        compose_path="compose.yaml",
        baseline_config_hash="sha256:parent",
        current_config_hash="sha256:candidate",
        risk_findings=[risk],
        journeys=[journey],
        baseline_journey_results=[result],
        baseline_observation=observation,
        experiments=[experiment],
        artifacts=["artifacts/health.json"],
    )
    graph_state = GraphState(
        run=run,
        stage_history=["intake", "baseline", "boot"],
        boot_attempt=1,
        boot_verdict=Verdict.PASS,
        pending_journey_results=[result],
        pending_observation=observation,
        pending_mutation=mutation,
        pending_experiment=experiment,
        pending_overlay_path="overlays/candidate.yaml",
        pending_overlay_materialized=True,
    )
    return (
        graph_state,
        run,
        risk,
        assertion,
        step,
        journey,
        result,
        observation,
        mutation,
        experiment,
        Verdict.FAIL,
        ExperimentVerdict.ROLLBACK,
        MutationType.DROP_ALL_CAPS,
    )


def _postgres_factory(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    events: list[str],
    saver: _FakeAsyncSaver,
) -> list[tuple[str, SerializerProtocol]]:
    calls: list[tuple[str, SerializerProtocol]] = []

    def factory(
        database_url: str, *, serde: SerializerProtocol
    ) -> AbstractAsyncContextManager[_FakeAsyncSaver]:
        events.append("factory")
        calls.append((database_url, serde))
        saver.serde = serde

        @asynccontextmanager
        async def managed() -> AsyncIterator[_FakeAsyncSaver]:
            events.append("enter")
            try:
                yield saver
            finally:
                events.append("exit")

        return managed()

    monkeypatch.setattr(
        module.AsyncPostgresSaver,
        "from_conn_string",
        staticmethod(factory),
    )
    return calls


def test_none_yields_memory_saver_usable_by_async_run_graph() -> None:
    module = _checkpoint_module()

    async def exercise() -> None:
        async with module.build_checkpointer(None) as saver:
            assert isinstance(saver, InMemorySaver)
            graph = build_run_graph(checkpointer=saver)
            assert graph.checkpointer is saver
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": "memory-checkpoint"}}
            )
            assert snapshot.values == {}

    asyncio.run(exercise())


def test_serializer_round_trips_only_checkpoint_safe_state_types() -> None:
    module = _checkpoint_module()

    async def exercise() -> None:
        async with module.build_checkpointer(None) as saver:
            for value in _checkpoint_values():
                restored = saver.serde.loads_typed(saver.serde.dumps_typed(value))
                assert type(restored) is type(value)
                assert restored == value

            unlisted = _UnlistedPayload(command="read-secret")
            restored_unlisted = saver.serde.loads_typed(
                saver.serde.dumps_typed(unlisted)
            )
            assert restored_unlisted == {"command": "read-secret"}
            assert not isinstance(restored_unlisted, _UnlistedPayload)
            with pytest.raises(TypeError):
                saver.serde.dumps_typed(object())

    asyncio.run(exercise())


def test_postgres_enters_sets_up_yields_and_closes_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _checkpoint_module()
    events: list[str] = []
    saver = _FakeAsyncSaver(events)
    calls = _postgres_factory(monkeypatch, module, events, saver)

    async def exercise() -> None:
        async with module.build_checkpointer(
            "postgresql://repotrial:secret@db.invalid/repotrial"
        ) as yielded:
            events.append("yield")
            assert yielded is saver
            assert saver.serde is calls[0][1]

    asyncio.run(exercise())

    assert calls[0][0] == "postgresql://repotrial:secret@db.invalid/repotrial"
    assert events == ["factory", "enter", "setup", "yield", "exit"]


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_postgres_library_url_schemes_reach_the_async_factory(
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
) -> None:
    module = _checkpoint_module()
    events: list[str] = []
    saver = _FakeAsyncSaver(events)
    calls = _postgres_factory(monkeypatch, module, events, saver)
    database_url = f"{scheme}://db.invalid/repotrial"

    async def exercise() -> None:
        async with module.build_checkpointer(database_url):
            pass

    asyncio.run(exercise())

    assert [call[0] for call in calls] == [database_url]
    assert events == ["factory", "enter", "setup", "exit"]


def test_postgres_factory_failure_propagates_without_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _checkpoint_module()
    failure = RuntimeError("factory failed")
    calls: list[str] = []

    def factory(
        database_url: str, *, serde: SerializerProtocol
    ) -> AbstractAsyncContextManager[_FakeAsyncSaver]:
        del serde
        calls.append(database_url)
        raise failure

    monkeypatch.setattr(
        module.AsyncPostgresSaver,
        "from_conn_string",
        staticmethod(factory),
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError) as raised:
            async with module.build_checkpointer("postgresql://db.invalid/repotrial"):
                raise AssertionError("failed factory must not yield")
        assert raised.value is failure

    asyncio.run(exercise())
    assert calls == ["postgresql://db.invalid/repotrial"]


def test_postgres_entry_failure_follows_context_manager_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _checkpoint_module()
    failure = RuntimeError("connection failed")
    events: list[str] = []

    def factory(
        database_url: str, *, serde: SerializerProtocol
    ) -> _FailingEntryContext:
        del database_url, serde
        events.append("factory")
        return _FailingEntryContext(events, failure)

    monkeypatch.setattr(
        module.AsyncPostgresSaver,
        "from_conn_string",
        staticmethod(factory),
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError) as raised:
            async with module.build_checkpointer("postgresql://db.invalid/repotrial"):
                raise AssertionError("failed entry must not yield")
        assert raised.value is failure

    asyncio.run(exercise())
    assert events == ["factory", "enter"]


def test_postgres_setup_failure_propagates_and_closes_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _checkpoint_module()
    failure = RuntimeError("setup failed")
    events: list[str] = []
    saver = _FakeAsyncSaver(events, setup_error=failure)
    _postgres_factory(monkeypatch, module, events, saver)

    async def exercise() -> None:
        with pytest.raises(RuntimeError) as raised:
            async with module.build_checkpointer("postgresql://db.invalid/repotrial"):
                raise AssertionError("failed setup must not yield")
        assert raised.value is failure

    asyncio.run(exercise())
    assert events == ["factory", "enter", "setup", "exit"]


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        "   ",
        " postgresql://db.invalid/repotrial",
        "https://db.invalid/repotrial",
        "sqlite:///repotrial.db",
        "postgresql:dbname=repotrial",
        "postgresql://db.invalid/repotrial\n",
        "postgres://user:\x00@db.invalid/repotrial",
    ],
)
def test_invalid_urls_fail_before_the_postgres_factory(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    module = _checkpoint_module()
    calls: list[str] = []

    def factory(
        accepted_url: str, *, serde: SerializerProtocol
    ) -> AbstractAsyncContextManager[_FakeAsyncSaver]:
        del serde
        calls.append(accepted_url)
        raise AssertionError("invalid URL reached PostgreSQL factory")

    monkeypatch.setattr(
        module.AsyncPostgresSaver,
        "from_conn_string",
        staticmethod(factory),
    )

    with pytest.raises(ValueError):
        module.build_checkpointer(database_url)

    assert calls == []
