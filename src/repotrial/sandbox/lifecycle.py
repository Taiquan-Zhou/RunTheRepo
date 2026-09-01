import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, TextIO

from repotrial.sandbox.base import (
    PartialCreateCleanupContext,
    SandboxProvider,
    find_sandbox_failure_evidence,
    get_partial_create_cleanup_context,
    serialize_sandbox_failure_evidence,
)

# Audit and provider boundaries must retain process-control failures until cleanup.
_BOUNDARY_FAILURES = (BaseException,)


class CleanupError(RuntimeError):
    def __init__(
        self,
        sandbox_id: str,
        destroy_failure: BaseException,
        body_failure: BaseException | None,
        *,
        create_failure: BaseException | None = None,
        initial_cleanup_failure: BaseException | None = None,
        audit_failures: tuple[BaseException, ...] = (),
        secondary_failures: tuple[BaseException, ...] = (),
    ) -> None:
        super().__init__(f"sandbox cleanup failed for {sandbox_id}")
        self.sandbox_id = sandbox_id
        self.destroy_failure = destroy_failure
        self.body_failure = body_failure
        self.create_failure = create_failure
        self.initial_cleanup_failure = initial_cleanup_failure
        self.audit_failures = audit_failures
        self.secondary_failures = secondary_failures


@dataclass
class _Outcome:
    body_failure: BaseException | None = None
    destroy_failure: BaseException | None = None
    audit_failures: list[BaseException] = field(default_factory=list)
    cancellations: list[asyncio.CancelledError] = field(default_factory=list)


def _write_event(
    artifact: TextIO,
    event: str,
    sandbox_id: str | None = None,
    exception: BaseException | None = None,
    *,
    state: str | None = None,
    cleanup_exception: BaseException | None = None,
) -> None:
    record: dict[str, object] = {"event": event}
    if sandbox_id is not None:
        record["sandbox_id"] = sandbox_id
    if exception is not None:
        record["exception_type"] = type(exception).__name__
        failure_evidence = find_sandbox_failure_evidence(exception)
        if failure_evidence is not None:
            record["failure"] = serialize_sandbox_failure_evidence(failure_evidence)
    if state is not None:
        record["state"] = state
    if cleanup_exception is not None:
        record["cleanup_exception_type"] = type(cleanup_exception).__name__
    artifact.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    artifact.flush()


def _record_event(
    outcome: _Outcome,
    artifact: TextIO,
    event: str,
    sandbox_id: str | None = None,
    exception: BaseException | None = None,
    *,
    state: str | None = None,
    cleanup_exception: BaseException | None = None,
) -> None:
    try:
        _write_event(
            artifact,
            event,
            sandbox_id,
            exception,
            state=state,
            cleanup_exception=cleanup_exception,
        )
    except _BOUNDARY_FAILURES as failure:
        outcome.audit_failures.append(failure)


def _close_artifact(outcome: _Outcome, artifact: TextIO) -> None:
    try:
        artifact.close()
    except _BOUNDARY_FAILURES as failure:
        outcome.audit_failures.append(failure)


async def _destroy_boundary(
    provider: SandboxProvider,
    sandbox_id: str,
) -> BaseException | None:
    while True:
        try:
            await provider.destroy(sandbox_id)
        except asyncio.CancelledError as failure:
            cleanup_task = asyncio.current_task()
            if cleanup_task is None or cleanup_task.cancelling() == 0:
                return failure
            while cleanup_task.cancelling():
                cleanup_task.uncancel()
        except _BOUNDARY_FAILURES as failure:
            return failure
        else:
            return None


def _secondary_cause(failures: list[BaseException]) -> BaseException:
    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup("sandbox lifecycle secondary failures", failures)


def _raise_primary(
    primary: BaseException,
    secondary_failures: list[BaseException],
) -> NoReturn:
    retained = list(secondary_failures)
    if primary.__cause__ is not None and all(
        failure is not primary.__cause__ for failure in retained
    ):
        retained.insert(0, primary.__cause__)
    if retained:
        primary.__cause__ = _secondary_cause(retained)
    raise primary


def _raise_outcome(outcome: _Outcome, sandbox_id: str) -> None:
    if outcome.destroy_failure is not None:
        cleanup_error = CleanupError(
            sandbox_id,
            outcome.destroy_failure,
            outcome.body_failure,
            audit_failures=tuple(outcome.audit_failures),
            secondary_failures=tuple(outcome.cancellations),
        )
        raise cleanup_error from outcome.destroy_failure

    cancellations = list(outcome.cancellations)
    primary = outcome.body_failure
    if primary is None and cancellations:
        primary = cancellations.pop(0)
    if primary is not None:
        _raise_primary(primary, [*outcome.audit_failures, *cancellations])
    if outcome.audit_failures:
        _raise_primary(outcome.audit_failures[0], outcome.audit_failures[1:])


async def _retry_partial_create_cleanup(
    provider: SandboxProvider,
    context: PartialCreateCleanupContext,
    create_failure: BaseException,
    artifact: TextIO,
    outcome: _Outcome,
) -> NoReturn:
    sandbox_id = context.sandbox_id
    _record_event(
        outcome,
        artifact,
        "create_cleanup_unsafe",
        sandbox_id,
        create_failure,
        state="cleanup_unsafe",
        cleanup_exception=context.cleanup_failure,
    )
    _record_event(outcome, artifact, "cleanup_retry_attempt", sandbox_id)
    cleanup_task = asyncio.create_task(_destroy_boundary(provider, sandbox_id))
    while not cleanup_task.done():
        try:
            await asyncio.wait({cleanup_task})
        except asyncio.CancelledError as cancellation:
            outcome.cancellations.append(cancellation)
    retry_failure = cleanup_task.result()
    if retry_failure is None:
        _record_event(outcome, artifact, "cleanup_retry_success", sandbox_id)
        _close_artifact(outcome, artifact)
        _raise_primary(
            create_failure,
            [*outcome.audit_failures, *outcome.cancellations],
        )

    _record_event(
        outcome,
        artifact,
        "cleanup_retry_failure",
        sandbox_id,
        retry_failure,
    )
    _close_artifact(outcome, artifact)
    cleanup_error = CleanupError(
        sandbox_id,
        retry_failure,
        None,
        create_failure=create_failure,
        initial_cleanup_failure=context.cleanup_failure,
        audit_failures=tuple(outcome.audit_failures),
        secondary_failures=tuple(outcome.cancellations),
    )
    raise cleanup_error from retry_failure


def _validate_artifact_destination(workspace: Path, artifact: Path) -> None:
    resolved_workspace = workspace.resolve(strict=False)
    resolved_artifact = artifact.resolve(strict=False)
    if resolved_artifact.is_relative_to(resolved_workspace):
        raise ValueError("lifecycle artifact must resolve outside workspace")


@asynccontextmanager
async def managed_sandbox(
    provider: SandboxProvider,
    workspace: Path,
    name: str,
    *,
    lifecycle_artifact: Path,
) -> AsyncIterator[str]:
    _validate_artifact_destination(workspace, lifecycle_artifact)
    artifact = lifecycle_artifact.open("x", encoding="utf-8", newline="\n")
    pre_create = _Outcome()
    _record_event(pre_create, artifact, "create_attempt")
    if pre_create.audit_failures:
        _close_artifact(pre_create, artifact)
        _raise_outcome(pre_create, "")

    try:
        sandbox_id = await provider.create(workspace, name)
    except _BOUNDARY_FAILURES as create_failure:
        partial_cleanup = get_partial_create_cleanup_context(create_failure)
        if partial_cleanup is not None:
            await _retry_partial_create_cleanup(
                provider,
                partial_cleanup,
                create_failure,
                artifact,
                pre_create,
            )
        _record_event(
            pre_create,
            artifact,
            "create_failure",
            exception=create_failure,
        )
        _close_artifact(pre_create, artifact)
        _raise_primary(create_failure, pre_create.audit_failures)

    outcome = _Outcome()
    _record_event(outcome, artifact, "create_success", sandbox_id)
    if not outcome.audit_failures:
        try:
            yield sandbox_id
        except _BOUNDARY_FAILURES as body_failure:
            outcome.body_failure = body_failure
            if find_sandbox_failure_evidence(body_failure) is not None:
                _record_event(
                    outcome,
                    artifact,
                    "provider_failure",
                    sandbox_id,
                    body_failure,
                )

    _record_event(outcome, artifact, "destroy_attempt", sandbox_id)
    cleanup_task = asyncio.create_task(_destroy_boundary(provider, sandbox_id))
    while not cleanup_task.done():
        try:
            await asyncio.wait({cleanup_task})
        except asyncio.CancelledError as cancellation:
            outcome.cancellations.append(cancellation)
    outcome.destroy_failure = cleanup_task.result()
    if outcome.destroy_failure is None:
        _record_event(outcome, artifact, "destroy_success", sandbox_id)
    else:
        _record_event(
            outcome,
            artifact,
            "destroy_failure",
            sandbox_id,
            outcome.destroy_failure,
        )
    _close_artifact(outcome, artifact)
    _raise_outcome(outcome, sandbox_id)
