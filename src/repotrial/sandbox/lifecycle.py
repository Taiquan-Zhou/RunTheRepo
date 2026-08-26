import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TextIO

from repotrial.sandbox.base import SandboxProvider


class CleanupError(RuntimeError):
    def __init__(
        self,
        sandbox_id: str,
        destroy_failure: BaseException,
        body_failure: BaseException | None,
    ) -> None:
        super().__init__(f"sandbox cleanup failed for {sandbox_id}")
        self.sandbox_id = sandbox_id
        self.destroy_failure = destroy_failure
        self.body_failure = body_failure


def _write_event(
    artifact: TextIO,
    event: str,
    sandbox_id: str | None = None,
    exception: BaseException | None = None,
) -> None:
    record = {"event": event}
    if sandbox_id is not None:
        record["sandbox_id"] = sandbox_id
    if exception is not None:
        record["exception_type"] = type(exception).__name__
    artifact.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    artifact.flush()


@asynccontextmanager
async def managed_sandbox(
    provider: SandboxProvider,
    workspace: Path,
    name: str,
    *,
    lifecycle_artifact: Path,
) -> AsyncIterator[str]:
    with lifecycle_artifact.open("w", encoding="utf-8", newline="\n") as artifact:
        _write_event(artifact, "create_attempt")
        create_completed = False
        try:
            sandbox_id = await provider.create(workspace, name)
            create_completed = True
        finally:
            if not create_completed:
                create_failure = sys.exception()
                assert create_failure is not None
                try:
                    _write_event(
                        artifact,
                        "create_failure",
                        exception=create_failure,
                    )
                except OSError as audit_failure:
                    raise create_failure from audit_failure
        body_completed = False
        try:
            _write_event(artifact, "create_success", sandbox_id)
            yield sandbox_id
            body_completed = True
        finally:
            body_failure = None if body_completed else sys.exception()
            lifecycle_failure: OSError | None = None
            try:
                _write_event(artifact, "destroy_attempt", sandbox_id)
            except OSError as failure:
                lifecycle_failure = failure
            cleanup_task = asyncio.create_task(provider.destroy(sandbox_id))
            cleanup_cancellation: asyncio.CancelledError | None = None
            while not cleanup_task.done():
                try:
                    await asyncio.wait({cleanup_task})
                except asyncio.CancelledError as cancellation:
                    cleanup_cancellation = cancellation
            destroy_failure: BaseException | None
            if cleanup_task.cancelled():
                destroy_failure = asyncio.CancelledError()
            else:
                destroy_failure = cleanup_task.exception()
            if destroy_failure is not None:
                try:
                    _write_event(
                        artifact,
                        "destroy_failure",
                        sandbox_id,
                        destroy_failure,
                    )
                except OSError as failure:
                    if lifecycle_failure is None:
                        lifecycle_failure = failure
                retained_failure = (
                    body_failure
                    if body_failure is not None
                    else lifecycle_failure or cleanup_cancellation
                )
                raise CleanupError(
                    sandbox_id, destroy_failure, retained_failure
                ) from destroy_failure
            try:
                _write_event(artifact, "destroy_success", sandbox_id)
            except OSError as failure:
                if lifecycle_failure is None:
                    lifecycle_failure = failure
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if lifecycle_failure is not None and body_failure is None:
                raise lifecycle_failure
