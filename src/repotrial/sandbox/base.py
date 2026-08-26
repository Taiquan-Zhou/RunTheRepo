from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class NetworkLogResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    supported: bool
    unsupported_reason: str | None = None


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
    async def destroy(self, sandbox_id: str) -> None: ...
