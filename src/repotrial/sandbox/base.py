from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class NetworkLogResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    supported: bool
    unsupported_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PartialCreateCleanupContext:
    """Owned sandbox identity and failures from an unconfirmed create cleanup."""

    sandbox_id: str
    create_failure: BaseException
    cleanup_failure: BaseException


class _PartialCreateCleanupCarrier(Protocol):
    partial_create_cleanup: PartialCreateCleanupContext


def attach_partial_create_cleanup_context(
    create_failure: BaseException,
    sandbox_id: str,
    cleanup_failure: BaseException,
) -> PartialCreateCleanupContext:
    context = PartialCreateCleanupContext(
        sandbox_id=sandbox_id,
        create_failure=create_failure,
        cleanup_failure=cleanup_failure,
    )
    carrier = cast(_PartialCreateCleanupCarrier, create_failure)
    carrier.partial_create_cleanup = context
    return context


def get_partial_create_cleanup_context(
    create_failure: BaseException,
) -> PartialCreateCleanupContext | None:
    context = getattr(create_failure, "partial_create_cleanup", None)
    if (
        isinstance(context, PartialCreateCleanupContext)
        and context.create_failure is create_failure
    ):
        return context
    return None


class SandboxProvider(ABC):
    @abstractmethod
    async def create(self, workspace: Path, name: str) -> str: ...

    @abstractmethod
    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult: ...

    @abstractmethod
    async def publish_port(self, sandbox_id: str, container_port: int) -> int: ...

    @abstractmethod
    async def copy(
        self, sandbox_id: str, remote_path: str, local_path: Path
    ) -> None: ...

    @abstractmethod
    async def network_log(self, sandbox_id: str) -> NetworkLogResult: ...

    @abstractmethod
    async def destroy(self, sandbox_id: str) -> None:
        """Retry-safe cleanup for owned IDs; unknown or unowned IDs fail closed."""
        ...
