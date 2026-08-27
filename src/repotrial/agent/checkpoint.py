import asyncio
import unicodedata
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

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

_POSTGRESQL_URL_PREFIXES = ("postgresql://", "postgres://")
_CHECKPOINT_TYPES: tuple[type[object], ...] = (
    GraphState,
    RunState,
    RiskFinding,
    JourneyAssertion,
    JourneyStep,
    Journey,
    JourneyResult,
    ObservationSnapshot,
    Mutation,
    ExperimentRecord,
    Verdict,
    ExperimentVerdict,
    MutationType,
)
_CHECKPOINT_JSON_IDS = tuple(
    (*checkpoint_type.__module__.split("."), checkpoint_type.__name__)
    for checkpoint_type in _CHECKPOINT_TYPES
)


class CheckpointInitializationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PostgreSQL checkpoint initialization failed")


def build_checkpointer(
    database_url: str | None,
) -> AbstractAsyncContextManager[BaseCheckpointSaver[str]]:
    validated_url = _validate_database_url(database_url)
    return _managed_checkpointer(validated_url)


def _validate_database_url(database_url: str | None) -> str | None:
    if database_url is None:
        return None
    if not isinstance(database_url, str):
        raise TypeError("database_url must be a string or None")
    if (
        not database_url.strip()
        or _contains_control(database_url)
        or not database_url.startswith(_POSTGRESQL_URL_PREFIXES)
    ):
        raise ValueError("database_url must be an explicit PostgreSQL URL")
    try:
        connection_info = conninfo_to_dict(database_url)
    except ProgrammingError:
        raise ValueError("database_url must be a valid PostgreSQL URL") from None
    if any(
        _contains_control(value)
        for value in connection_info.values()
        if isinstance(value, str)
    ):
        raise ValueError("database_url must not contain control characters")
    return database_url


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _build_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=_CHECKPOINT_JSON_IDS,
        allowed_msgpack_modules=_CHECKPOINT_TYPES,
    )


def build_memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=_build_serializer())


@asynccontextmanager
async def _managed_checkpointer(
    database_url: str | None,
) -> AsyncIterator[BaseCheckpointSaver[str]]:
    if database_url is None:
        yield build_memory_checkpointer()
        return

    initialization_error: CheckpointInitializationError | None
    try:
        saver_context = AsyncPostgresSaver.from_conn_string(
            database_url,
            serde=_build_serializer(),
        )
    except asyncio.CancelledError:
        raise
    # This adapter boundary must translate every non-cancellation backend failure.
    except Exception:  # noqa: BLE001
        initialization_error = CheckpointInitializationError()
    else:
        initialization_error = None
    if initialization_error is not None:
        raise initialization_error

    exit_stack = AsyncExitStack()
    try:
        saver = await exit_stack.enter_async_context(saver_context)
    except asyncio.CancelledError:
        raise
    # This adapter boundary must translate every non-cancellation backend failure.
    except Exception:  # noqa: BLE001
        initialization_error = CheckpointInitializationError()
    else:
        initialization_error = None
    if initialization_error is not None:
        raise initialization_error

    try:
        await saver.setup()
    except asyncio.CancelledError:
        await exit_stack.aclose()
        raise
    # This adapter boundary must translate every non-cancellation backend failure.
    except Exception:  # noqa: BLE001
        await exit_stack.aclose()
        initialization_error = CheckpointInitializationError()
    else:
        initialization_error = None
    if initialization_error is not None:
        raise initialization_error

    try:
        yield saver
    finally:
        await exit_stack.aclose()
