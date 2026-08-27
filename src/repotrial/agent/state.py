import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from repotrial.domain.enums import Verdict
from repotrial.domain.models import (
    ExperimentRecord,
    JourneyResult,
    Mutation,
    ObservationSnapshot,
    RunState,
)
from repotrial.models.base import ModelAdapter
from repotrial.sandbox.base import SandboxProvider
from repotrial.trial.boot import BootResult

type StageName = Literal[
    "intake",
    "baseline",
    "boot",
    "journeys",
    "observe",
    "propose_mutation",
    "experiment",
    "decide",
    "report_or_next",
]
type RecoverySleep = Callable[[float], Awaitable[None]]


class StageOperations(Protocol):
    """Exact baseline service seam; each call must finish owned lifecycles."""

    async def intake(self, state: RunState) -> RunState: ...

    async def baseline(self, state: RunState) -> RunState: ...

    async def boot(
        self,
        state: RunState,
        provider: SandboxProvider,
        env: Mapping[str, str],
        attempt: int,
    ) -> BootResult: ...

    async def journeys(
        self, state: RunState, provider: SandboxProvider
    ) -> list[JourneyResult]: ...

    async def observe(
        self, state: RunState, provider: SandboxProvider
    ) -> ObservationSnapshot: ...


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class GraphContext:
    operations: StageOperations
    provider: SandboxProvider
    workspace: Path
    artifact_dir: Path
    overlay_dir: Path
    accepted_compose_dir: Path
    env: Mapping[str, str]
    allowed_env_keys: frozenset[str]
    readme_excerpt: str
    container_port: int
    model: ModelAdapter | None = None
    sleep: RecoverySleep = _default_sleep


class GraphState(BaseModel):
    """Checkpoint-safe orchestration data; runtime resources stay in context."""

    model_config = ConfigDict(extra="forbid")

    run: RunState
    stage_history: list[StageName] = Field(default_factory=list, max_length=64)
    boot_attempt: int = Field(default=0, ge=0, le=4)
    boot_verdict: Verdict | None = None
    boot_error_hash: str | None = None
    repeated_error_count: int = Field(default=0, ge=0, le=3)
    recovery_env: dict[str, str] = Field(default_factory=dict)
    pending_mutation: Mutation | None = None
    pending_experiment: ExperimentRecord | None = None
