"""Lifecycle metadata store kept separate from LangGraph checkpoints."""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from psycopg import AsyncConnection
from psycopg import Error as PsycopgError


class RunLifecycle(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    TRIAL_FAILED = "TRIAL_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    repo_url: str
    lifecycle: RunLifecycle
    created_at: datetime
    updated_at: datetime
    commit_sha: str | None = None
    report_available: bool = False


class RegistryUnavailableError(RuntimeError):
    """Credential-free control-plane registry failure."""


class RunRegistry(Protocol):
    async def initialize(self) -> None: ...

    async def health(self) -> bool: ...

    async def interrupt_running(self) -> None: ...

    async def create_running(self, run_id: str, repo_url: str) -> RunRecord: ...

    async def get(self, run_id: str) -> RunRecord | None: ...

    async def complete(
        self,
        run_id: str,
        lifecycle: RunLifecycle,
        *,
        commit_sha: str | None,
        report_available: bool,
    ) -> RunRecord: ...


class InMemoryRunRegistry:
    """Injectable test registry; production must use a durable registry."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self._records: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self._available:
            raise RegistryUnavailableError()

    async def health(self) -> bool:
        return self._available

    async def interrupt_running(self) -> None:
        now = _now()
        async with self._lock:
            self._records = {
                key: replace(record, lifecycle=RunLifecycle.INTERRUPTED, updated_at=now)
                if record.lifecycle is RunLifecycle.RUNNING
                else record
                for key, record in self._records.items()
            }

    async def create_running(self, run_id: str, repo_url: str) -> RunRecord:
        if not self._available:
            raise RegistryUnavailableError()
        now = _now()
        record = RunRecord(run_id, repo_url, RunLifecycle.RUNNING, now, now)
        async with self._lock:
            if run_id in self._records:
                raise RegistryUnavailableError()
            self._records[run_id] = record
        return record

    async def get(self, run_id: str) -> RunRecord | None:
        if not self._available:
            raise RegistryUnavailableError()
        return self._records.get(run_id)

    async def complete(
        self,
        run_id: str,
        lifecycle: RunLifecycle,
        *,
        commit_sha: str | None,
        report_available: bool,
    ) -> RunRecord:
        if not self._available:
            raise RegistryUnavailableError()
        if lifecycle is RunLifecycle.RUNNING:
            raise ValueError("terminal lifecycle required")
        async with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RegistryUnavailableError()
            updated = replace(
                record,
                lifecycle=lifecycle,
                updated_at=_now(),
                commit_sha=commit_sha,
                report_available=report_available,
            )
            self._records[run_id] = updated
        return updated


def _now() -> datetime:
    return datetime.now(UTC)


class PostgresRunRegistry:
    """Small dedicated lifecycle table; it never reads checkpoint data."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def initialize(self) -> None:
        try:
            async with (
                await AsyncConnection.connect(self._database_url) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(
                    """
                        CREATE TABLE IF NOT EXISTS repotrial_run_registry (
                            run_id TEXT PRIMARY KEY,
                            repo_url TEXT NOT NULL,
                            lifecycle TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL,
                            commit_sha TEXT NULL,
                            report_available BOOLEAN NOT NULL DEFAULT FALSE
                        )
                        """
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - driver boundary
            raise RegistryUnavailableError() from error

    async def health(self) -> bool:
        try:
            async with (
                await AsyncConnection.connect(self._database_url) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute("SELECT 1")
                return await cursor.fetchone() is not None
        except asyncio.CancelledError:
            raise
        except (PsycopgError, RegistryUnavailableError):
            return False

    async def interrupt_running(self) -> None:
        await self._execute(
            "UPDATE repotrial_run_registry SET lifecycle = %s, updated_at = %s "
            "WHERE lifecycle = %s",
            (RunLifecycle.INTERRUPTED.value, _now(), RunLifecycle.RUNNING.value),
        )

    async def create_running(self, run_id: str, repo_url: str) -> RunRecord:
        now = _now()
        return await self._fetch_one(
            """
            INSERT INTO repotrial_run_registry
                (run_id, repo_url, lifecycle, created_at, updated_at, report_available)
            VALUES (%s, %s, %s, %s, %s, FALSE)
            RETURNING run_id, repo_url, lifecycle, created_at, updated_at, commit_sha, report_available
            """,
            (run_id, repo_url, RunLifecycle.RUNNING.value, now, now),
        )

    async def get(self, run_id: str) -> RunRecord | None:
        try:
            async with (
                await AsyncConnection.connect(self._database_url) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(
                    "SELECT run_id, repo_url, lifecycle, created_at, updated_at, commit_sha, report_available "
                    "FROM repotrial_run_registry WHERE run_id = %s",
                    (run_id,),
                )
                row = cast(tuple[object, ...] | None, await cursor.fetchone())
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - driver boundary
            raise RegistryUnavailableError() from error
        return None if row is None else _record_from_row(row)

    async def complete(
        self,
        run_id: str,
        lifecycle: RunLifecycle,
        *,
        commit_sha: str | None,
        report_available: bool,
    ) -> RunRecord:
        if lifecycle is RunLifecycle.RUNNING:
            raise ValueError("terminal lifecycle required")
        return await self._fetch_one(
            """
            UPDATE repotrial_run_registry
            SET lifecycle = %s, updated_at = %s, commit_sha = %s, report_available = %s
            WHERE run_id = %s
            RETURNING run_id, repo_url, lifecycle, created_at, updated_at, commit_sha, report_available
            """,
            (lifecycle.value, _now(), commit_sha, report_available, run_id),
        )

    async def _execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        try:
            async with (
                await AsyncConnection.connect(self._database_url) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(statement, parameters)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - driver boundary
            raise RegistryUnavailableError() from error

    async def _fetch_one(
        self, statement: str, parameters: tuple[object, ...]
    ) -> RunRecord:
        try:
            async with (
                await AsyncConnection.connect(self._database_url) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(statement, parameters)
                row = cast(tuple[object, ...] | None, await cursor.fetchone())
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - driver boundary
            raise RegistryUnavailableError() from error
        if row is None:
            raise RegistryUnavailableError()
        return _record_from_row(row)


def _record_from_row(row: tuple[object, ...]) -> RunRecord:
    if len(row) != 7:
        raise RegistryUnavailableError()
    run_id, repo_url, lifecycle_value = row[0], row[1], row[2]
    if (
        not isinstance(run_id, str)
        or not isinstance(repo_url, str)
        or not isinstance(lifecycle_value, str)
    ):
        raise RegistryUnavailableError()
    created_at, updated_at = row[3], row[4]
    commit_sha, report_available = row[5], row[6]
    if not isinstance(created_at, datetime) or not isinstance(updated_at, datetime):
        raise RegistryUnavailableError()
    if commit_sha is not None and not isinstance(commit_sha, str):
        raise RegistryUnavailableError()
    if not isinstance(report_available, bool):
        raise RegistryUnavailableError()
    try:
        lifecycle = RunLifecycle(lifecycle_value)
    except ValueError:
        raise RegistryUnavailableError() from None
    return RunRecord(
        run_id,
        repo_url,
        lifecycle,
        created_at,
        updated_at,
        commit_sha,
        report_available,
    )
