import asyncio
import json
from pathlib import Path
from typing import Self

import pytest

from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider
from repotrial.sandbox.lifecycle import CleanupError, managed_sandbox


class _Provider(SandboxProvider):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def create(self, workspace: Path, name: str) -> str:
        self.calls.append(("create", workspace, name))
        return "sandbox-17"

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        raise AssertionError("exec is outside this test double's contract")

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        raise AssertionError("publish_port is outside this test double's contract")

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        raise AssertionError("copy is outside this test double's contract")

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        raise AssertionError("network_log is outside this test double's contract")

    async def destroy(self, sandbox_id: str) -> None:
        self.calls.append(("destroy", sandbox_id))


class _FailingArtifact:
    def __init__(
        self,
        failure: OSError,
        fail_on_write: int,
        *,
        keep_failing: bool = True,
    ) -> None:
        self.failure = failure
        self.fail_on_write = fail_on_write
        self.keep_failing = keep_failing
        self.write_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def write(self, data: str) -> int:
        self.write_count += 1
        should_fail = (
            self.write_count >= self.fail_on_write
            if self.keep_failing
            else self.write_count == self.fail_on_write
        )
        if should_fail:
            raise self.failure
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ScriptedArtifact:
    def __init__(
        self,
        failure_at: tuple[str, str | None],
        failure: BaseException,
    ) -> None:
        self.failure_at = failure_at
        self.failure = failure
        self.operations: list[tuple[str, str | None]] = []
        self.current_event: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def write(self, data: str) -> int:
        event = json.loads(data)["event"]
        self.current_event = event
        self._record("write", event)
        return len(data)

    def flush(self) -> None:
        self._record("flush", self.current_event)

    def close(self) -> None:
        self._record("close", None)

    def _record(self, operation: str, event: str | None) -> None:
        entry = (operation, event)
        self.operations.append(entry)
        if entry == self.failure_at:
            raise self.failure


def _patch_artifact_open(
    monkeypatch: pytest.MonkeyPatch,
    artifact: object,
) -> None:
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: artifact)


def _read_events(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_success_creates_yields_and_destroys_once_with_ordered_audit(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    artifact = tmp_path / "lifecycle.jsonl"
    yielded: list[str] = []

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ) as sandbox_id:
            yielded.append(sandbox_id)

    asyncio.run(exercise())

    assert yielded == ["sandbox-17"]
    assert provider.calls == [
        ("create", tmp_path / "workspace", "trial"),
        ("destroy", "sandbox-17"),
    ]
    assert _read_events(artifact) == [
        {"event": "create_attempt"},
        {"event": "create_success", "sandbox_id": "sandbox-17"},
        {"event": "destroy_attempt", "sandbox_id": "sandbox-17"},
        {"event": "destroy_success", "sandbox_id": "sandbox-17"},
    ]


def test_body_exception_is_preserved_after_destroy(tmp_path: Path) -> None:
    provider = _Provider()
    artifact = tmp_path / "lifecycle.jsonl"
    body_failure = ValueError("body failed")

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ):
            raise body_failure

    with pytest.raises(ValueError) as raised:
        asyncio.run(exercise())

    assert raised.value is body_failure
    assert provider.calls == [
        ("create", tmp_path / "workspace", "trial"),
        ("destroy", "sandbox-17"),
    ]
    assert _read_events(artifact)[-2:] == [
        {"event": "destroy_attempt", "sandbox_id": "sandbox-17"},
        {"event": "destroy_success", "sandbox_id": "sandbox-17"},
    ]


def test_create_exception_is_recorded_without_destroy(tmp_path: Path) -> None:
    create_failure = LookupError("create failed")

    class CreateFailingProvider(_Provider):
        async def create(self, workspace: Path, name: str) -> str:
            self.calls.append(("create", workspace, name))
            raise create_failure

    provider = CreateFailingProvider()
    artifact = tmp_path / "lifecycle.jsonl"

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ):
            raise AssertionError("body must not run")

    with pytest.raises(LookupError) as raised:
        asyncio.run(exercise())

    assert raised.value is create_failure
    assert provider.calls == [("create", tmp_path / "workspace", "trial")]
    assert _read_events(artifact) == [
        {"event": "create_attempt"},
        {"event": "create_failure", "exception_type": "LookupError"},
    ]


def test_destroy_exception_is_recorded_and_chained_from_cleanup_error(
    tmp_path: Path,
) -> None:
    destroy_failure = OSError("destroy failed")

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    provider = DestroyFailingProvider()
    artifact = tmp_path / "lifecycle.jsonl"

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ):
            pass

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    assert raised.value.sandbox_id == "sandbox-17"
    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.body_failure is None
    assert raised.value.__cause__ is destroy_failure
    assert provider.calls[-1] == ("destroy", "sandbox-17")
    assert _read_events(artifact)[-2:] == [
        {"event": "destroy_attempt", "sandbox_id": "sandbox-17"},
        {
            "event": "destroy_failure",
            "exception_type": "OSError",
            "sandbox_id": "sandbox-17",
        },
    ]


def test_body_and_destroy_failures_are_both_retained(tmp_path: Path) -> None:
    body_failure = ValueError("body failed")
    destroy_failure = OSError("destroy failed")

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            raise body_failure

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    assert raised.value.body_failure is body_failure
    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.__cause__ is destroy_failure
    assert provider.calls[-1] == ("destroy", "sandbox-17")


@pytest.mark.parametrize("extra_cancellations", [0, 2])
def test_cancellation_waits_for_destroy_without_leaving_background_tasks(
    extra_cancellations: int,
    tmp_path: Path,
) -> None:
    async def exercise() -> list[tuple[object, ...]]:
        body_started = asyncio.Event()
        destroy_started = asyncio.Event()
        allow_destroy = asyncio.Event()
        destroy_completed = False

        class BlockingDestroyProvider(_Provider):
            async def destroy(self, sandbox_id: str) -> None:
                nonlocal destroy_completed
                self.calls.append(("destroy", sandbox_id))
                destroy_started.set()
                await allow_destroy.wait()
                destroy_completed = True

        provider = BlockingDestroyProvider()

        async def run_trial() -> None:
            async with managed_sandbox(
                provider,
                tmp_path / "workspace",
                "trial",
                lifecycle_artifact=tmp_path / "lifecycle.jsonl",
            ):
                body_started.set()
                await asyncio.Event().wait()

        trial_task = asyncio.create_task(run_trial())
        await body_started.wait()
        trial_task.cancel()
        await destroy_started.wait()
        for _ in range(extra_cancellations):
            trial_task.cancel()
            await asyncio.sleep(0)

        assert trial_task.done() is False
        allow_destroy.set()
        with pytest.raises(asyncio.CancelledError):
            await trial_task
        assert destroy_completed is True
        await asyncio.sleep(0)
        remaining = {
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        }
        assert remaining == set()
        return provider.calls

    assert asyncio.run(exercise()) == [
        ("create", tmp_path / "workspace", "trial"),
        ("destroy", "sandbox-17"),
    ]


def test_asyncio_run_shutdown_drains_an_already_started_destroy_child(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    destroy_started: asyncio.Event
    parent_tasks: list[asyncio.Task[None]] = []
    cleanup_tasks: list[asyncio.Task[object]] = []
    shutdown_diagnostics: list[dict[str, object]] = []
    destroy_calls: list[str] = []
    direct_cancellations: list[asyncio.CancelledError] = []
    destroy_completed = False

    class FiniteDestroyProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            nonlocal destroy_completed
            destroy_calls.append(sandbox_id)
            cleanup_task = asyncio.current_task()
            assert cleanup_task is not None
            cleanup_tasks.append(cleanup_task)
            destroy_started.set()
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as cancellation:
                direct_cancellations.append(cancellation)
                raise
            destroy_completed = True

    provider = FiniteDestroyProvider()

    async def run_trial() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    async def abandon_parent_after_cleanup_starts() -> None:
        nonlocal destroy_started
        destroy_started = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: shutdown_diagnostics.append(context)
        )
        parent_tasks.append(asyncio.create_task(run_trial()))
        await destroy_started.wait()

    asyncio.run(abandon_parent_after_cleanup_starts())

    assert destroy_completed is True
    assert destroy_calls == ["sandbox-17", "sandbox-17"]
    assert len(direct_cancellations) == 1
    assert len(cleanup_tasks) == 2
    assert cleanup_tasks[0] is cleanup_tasks[1]
    assert cleanup_tasks[0].done() is True
    assert cleanup_tasks[0].cancelled() is False
    assert cleanup_tasks[0].result() is None
    assert len(parent_tasks) == 1
    assert parent_tasks[0].cancelled() is True
    with pytest.raises(asyncio.CancelledError) as shutdown_result:
        parent_tasks[0].exception()
    assert shutdown_result.value.args == ()
    assert shutdown_diagnostics == []
    diagnostics = capsys.readouterr().err.lower()
    assert "never retrieved" not in diagnostics
    assert "unhandled exception during asyncio.run() shutdown" not in diagnostics
    assert all(
        "unhandled exception" not in record.message.lower() for record in caplog.records
    )


def test_repeated_direct_cleanup_task_cancellation_retries_until_confirmed(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[list[str], list[asyncio.CancelledError]]:
        allow_destroy = asyncio.Event()
        cleanup_tasks: list[asyncio.Task[object]] = []
        destroy_calls: list[str] = []
        direct_cancellations: list[asyncio.CancelledError] = []

        class BlockingDestroyProvider(_Provider):
            async def destroy(self, sandbox_id: str) -> None:
                destroy_calls.append(sandbox_id)
                cleanup_task = asyncio.current_task()
                assert cleanup_task is not None
                cleanup_tasks.append(cleanup_task)
                try:
                    await allow_destroy.wait()
                except asyncio.CancelledError as cancellation:
                    direct_cancellations.append(cancellation)
                    raise

        provider = BlockingDestroyProvider()

        async def run_trial() -> None:
            async with managed_sandbox(
                provider,
                tmp_path / "workspace",
                "trial",
                lifecycle_artifact=tmp_path / "lifecycle.jsonl",
            ):
                pass

        parent_task = asyncio.create_task(run_trial())
        while len(cleanup_tasks) < 1:
            await asyncio.sleep(0)
        cleanup_tasks[0].cancel("first direct cleanup cancellation")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(cleanup_tasks) == 2
        cleanup_tasks[0].cancel("second direct cleanup cancellation")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(cleanup_tasks) == 3
        allow_destroy.set()
        await parent_task

        assert all(task is cleanup_tasks[0] for task in cleanup_tasks)
        assert cleanup_tasks[0].cancelled() is False
        assert cleanup_tasks[0].result() is None
        return destroy_calls, direct_cancellations

    destroy_calls, direct_cancellations = asyncio.run(exercise())

    assert destroy_calls == ["sandbox-17", "sandbox-17", "sandbox-17"]
    assert [cancellation.args for cancellation in direct_cancellations] == [
        ("first direct cleanup cancellation",),
        ("second direct cleanup cancellation",),
    ]


def test_unopenable_artifact_prevents_provider_calls(tmp_path: Path) -> None:
    provider = _Provider()
    artifact_directory = tmp_path / "artifact-directory"
    artifact_directory.mkdir()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact_directory,
        ):
            raise AssertionError("body must not run")

    with pytest.raises(OSError):
        asyncio.run(exercise())

    assert provider.calls == []


@pytest.mark.parametrize("fail_on_write", [2, 3, 4])
def test_artifact_failure_after_create_still_destroys_sandbox(
    fail_on_write: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_failure = OSError("artifact failed")
    failing_artifact = _FailingArtifact(artifact_failure, fail_on_write)
    artifact_path = tmp_path / "lifecycle.jsonl"

    def open_artifact(path: Path, *args: object, **kwargs: object) -> _FailingArtifact:
        assert path == artifact_path
        return failing_artifact

    monkeypatch.setattr(Path, "open", open_artifact)
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact_path,
        ):
            pass

    with pytest.raises(OSError) as raised:
        asyncio.run(exercise())

    assert raised.value is artifact_failure
    assert provider.calls == [
        ("create", tmp_path / "workspace", "trial"),
        ("destroy", "sandbox-17"),
    ]


def test_create_failure_is_preserved_when_failure_audit_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_failure = LookupError("create failed")
    artifact_failure = OSError("artifact failed")

    class CreateFailingProvider(_Provider):
        async def create(self, workspace: Path, name: str) -> str:
            self.calls.append(("create", workspace, name))
            raise create_failure

    failing_artifact = _FailingArtifact(
        artifact_failure,
        2,
        keep_failing=False,
    )
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: failing_artifact)
    provider = CreateFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            raise AssertionError("body must not run")

    with pytest.raises(LookupError) as raised:
        asyncio.run(exercise())

    assert raised.value is create_failure
    assert raised.value.__cause__ is artifact_failure
    assert provider.calls == [("create", tmp_path / "workspace", "trial")]


def test_artifact_and_destroy_failure_keep_cleanup_error_on_top(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_failure = OSError("artifact failed")
    destroy_failure = RuntimeError("destroy failed")

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    failing_artifact = _FailingArtifact(artifact_failure, 2)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: failing_artifact)
    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.body_failure is None
    assert artifact_failure in raised.value.audit_failures
    assert raised.value.__cause__ is destroy_failure
    assert provider.calls[-1] == ("destroy", "sandbox-17")


def test_outer_handled_exception_is_not_mistaken_for_lifecycle_failure(
    tmp_path: Path,
) -> None:
    outer_failure = ValueError("outer failure")
    destroy_failure = RuntimeError("destroy failed")
    artifact = tmp_path / "lifecycle.jsonl"

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ):
            pass

    try:
        raise outer_failure
    except ValueError:
        with pytest.raises(CleanupError) as raised:
            asyncio.run(exercise())

    assert raised.value.body_failure is None
    assert _read_events(artifact) == [
        {"event": "create_attempt"},
        {"event": "create_success", "sandbox_id": "sandbox-17"},
        {"event": "destroy_attempt", "sandbox_id": "sandbox-17"},
        {
            "event": "destroy_failure",
            "exception_type": "RuntimeError",
            "sandbox_id": "sandbox-17",
        },
    ]


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_non_os_audit_failure_cannot_replace_create_failure(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_failure = LookupError("create failed")
    audit_failure = ValueError("audit failed")

    class CreateFailingProvider(_Provider):
        async def create(self, workspace: Path, name: str) -> str:
            self.calls.append(("create", workspace, name))
            raise create_failure

    artifact = _ScriptedArtifact(
        (operation, "create_failure"),
        audit_failure,
    )
    _patch_artifact_open(monkeypatch, artifact)
    provider = CreateFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            raise AssertionError("body must not run")

    with pytest.raises(LookupError) as raised:
        asyncio.run(exercise())

    assert raised.value is create_failure
    assert raised.value.__cause__ is audit_failure
    assert provider.calls == [("create", tmp_path / "workspace", "trial")]


@pytest.mark.parametrize(
    ("operation", "event", "audit_failure"),
    [
        ("write", "create_success", SystemExit("create_success write")),
        ("flush", "create_success", ValueError("create_success flush")),
        ("write", "destroy_attempt", KeyboardInterrupt("destroy_attempt write")),
        ("flush", "destroy_attempt", ValueError("destroy_attempt flush")),
        ("write", "destroy_success", SystemExit("destroy_success write")),
        ("flush", "destroy_success", ValueError("destroy_success flush")),
    ],
)
def test_post_create_audit_baseexception_never_bypasses_destroy(
    operation: str,
    event: str,
    audit_failure: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _ScriptedArtifact((operation, event), audit_failure)
    _patch_artifact_open(monkeypatch, artifact)
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    with pytest.raises(type(audit_failure)) as raised:
        asyncio.run(exercise())

    assert raised.value is audit_failure
    assert provider.calls == [
        ("create", tmp_path / "workspace", "trial"),
        ("destroy", "sandbox-17"),
    ]


@pytest.mark.parametrize(
    ("operation", "audit_failure"),
    [
        ("write", SystemExit("destroy_failure write")),
        ("flush", ValueError("destroy_failure flush")),
    ],
)
def test_destroy_failure_audit_baseexception_cannot_replace_cleanup_error(
    operation: str,
    audit_failure: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroy_failure = RuntimeError("destroy failed")

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    artifact = _ScriptedArtifact(
        (operation, "destroy_failure"),
        audit_failure,
    )
    _patch_artifact_open(monkeypatch, artifact)
    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.audit_failures == (audit_failure,)
    assert raised.value.__cause__ is destroy_failure
    assert provider.calls[-1] == ("destroy", "sandbox-17")


@pytest.mark.parametrize("destroy_fails", [False, True])
def test_artifact_close_baseexception_obeys_final_failure_priority(
    destroy_fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_failure = KeyboardInterrupt("close failed")
    destroy_failure = RuntimeError("destroy failed")

    class ConfigurableProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            if destroy_fails:
                raise destroy_failure

    artifact = _ScriptedArtifact(("close", None), close_failure)
    _patch_artifact_open(monkeypatch, artifact)
    provider = ConfigurableProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    expected = CleanupError if destroy_fails else KeyboardInterrupt
    with pytest.raises(expected) as raised:
        asyncio.run(exercise())

    assert provider.calls[-1] == ("destroy", "sandbox-17")
    if destroy_fails:
        assert isinstance(raised.value, CleanupError)
        assert raised.value.destroy_failure is destroy_failure
        assert raised.value.audit_failures == (close_failure,)
    else:
        assert raised.value is close_failure


def test_create_failure_remains_primary_when_artifact_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_failure = LookupError("create failed")
    close_failure = SystemExit("close failed")

    class CreateFailingProvider(_Provider):
        async def create(self, workspace: Path, name: str) -> str:
            self.calls.append(("create", workspace, name))
            raise create_failure

    artifact = _ScriptedArtifact(("close", None), close_failure)
    _patch_artifact_open(monkeypatch, artifact)
    provider = CreateFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            raise AssertionError("body must not run")

    with pytest.raises(LookupError) as raised:
        asyncio.run(exercise())

    assert raised.value is create_failure
    assert raised.value.__cause__ is close_failure
    assert provider.calls == [("create", tmp_path / "workspace", "trial")]


@pytest.mark.parametrize(
    "destroy_failure",
    [SystemExit("destroy exited"), KeyboardInterrupt("destroy interrupted")],
)
def test_destroy_process_control_failure_becomes_observed_cleanup_error(
    destroy_failure: BaseException,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    with pytest.raises((CleanupError, SystemExit, KeyboardInterrupt)) as raised:
        asyncio.run(exercise())

    assert isinstance(raised.value, CleanupError)
    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.__cause__ is destroy_failure
    diagnostics = capsys.readouterr().err.lower()
    assert "never retrieved" not in diagnostics
    assert "unhandled exception during asyncio.run() shutdown" not in diagnostics
    assert all(
        "unhandled exception" not in record.message.lower() for record in caplog.records
    )


def test_annotated_destroy_cancellation_is_preserved_verbatim(tmp_path: Path) -> None:
    destroy_cause = RuntimeError("provider cleanup cause")
    destroy_failure = asyncio.CancelledError("provider destroy cancelled")
    destroy_failure.add_note("process cleanup unconfirmed: reap_timeout")
    destroy_failure.__cause__ = destroy_cause
    cancellation_counts: list[int] = []

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            task = asyncio.current_task()
            assert task is not None
            cancellation_counts.append(task.cancelling())
            raise destroy_failure

    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.destroy_failure.args == ("provider destroy cancelled",)
    assert raised.value.destroy_failure.__notes__ == [
        "process cleanup unconfirmed: reap_timeout"
    ]
    assert raised.value.destroy_failure.__cause__ is destroy_cause
    assert raised.value.__cause__ is destroy_failure
    assert cancellation_counts == [0]
    assert provider.calls == [
        ("create", tmp_path / "workspace", "trial"),
        ("destroy", "sandbox-17"),
    ]


def test_repeated_cancellation_preserves_original_body_cancellation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        body_started = asyncio.Event()
        destroy_started = asyncio.Event()
        allow_destroy = asyncio.Event()
        body_cancellations: list[asyncio.CancelledError] = []

        class BlockingDestroyProvider(_Provider):
            async def destroy(self, sandbox_id: str) -> None:
                self.calls.append(("destroy", sandbox_id))
                destroy_started.set()
                await allow_destroy.wait()

        provider = BlockingDestroyProvider()

        async def run_trial() -> None:
            async with managed_sandbox(
                provider,
                tmp_path / "workspace",
                "trial",
                lifecycle_artifact=tmp_path / "lifecycle.jsonl",
            ):
                body_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as cancellation:
                    body_cancellations.append(cancellation)
                    raise

        trial_task = asyncio.create_task(run_trial())
        await body_started.wait()
        trial_task.cancel("first cancellation")
        await destroy_started.wait()
        trial_task.cancel("second cancellation")
        await asyncio.sleep(0)
        trial_task.cancel("third cancellation")
        await asyncio.sleep(0)
        allow_destroy.set()

        with pytest.raises(asyncio.CancelledError) as raised:
            await trial_task

        assert raised.value is body_cancellations[0]
        assert raised.value.args == ("first cancellation",)
        assert isinstance(raised.value.__cause__, BaseExceptionGroup)
        assert [failure.args for failure in raised.value.__cause__.exceptions] == [
            ("second cancellation",),
            ("third cancellation",),
        ]
        remaining = {
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        }
        assert remaining == set()

    asyncio.run(exercise())


def test_cleanup_error_types_original_and_secondary_cancellations(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        body_started = asyncio.Event()
        destroy_started = asyncio.Event()
        allow_destroy = asyncio.Event()
        body_cancellations: list[asyncio.CancelledError] = []
        destroy_failure = RuntimeError("destroy failed")

        class BlockingFailingProvider(_Provider):
            async def destroy(self, sandbox_id: str) -> None:
                self.calls.append(("destroy", sandbox_id))
                destroy_started.set()
                await allow_destroy.wait()
                raise destroy_failure

        provider = BlockingFailingProvider()

        async def run_trial() -> None:
            async with managed_sandbox(
                provider,
                tmp_path / "workspace",
                "trial",
                lifecycle_artifact=tmp_path / "lifecycle.jsonl",
            ):
                body_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as cancellation:
                    body_cancellations.append(cancellation)
                    raise

        trial_task = asyncio.create_task(run_trial())
        await body_started.wait()
        trial_task.cancel("body cancellation")
        await destroy_started.wait()
        trial_task.cancel("cleanup cancellation")
        await asyncio.sleep(0)
        allow_destroy.set()

        with pytest.raises(CleanupError) as raised:
            await trial_task

        assert raised.value.destroy_failure is destroy_failure
        assert raised.value.body_failure is body_cancellations[0]
        assert [failure.args for failure in raised.value.secondary_failures] == [
            ("cleanup cancellation",)
        ]
        assert raised.value.__cause__ is destroy_failure

    asyncio.run(exercise())


def test_body_and_audit_failure_preserve_both_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_failure = RuntimeError("body failed")
    audit_failure = ValueError("audit failed")
    artifact = _ScriptedArtifact(
        ("write", "destroy_attempt"),
        audit_failure,
    )
    _patch_artifact_open(monkeypatch, artifact)
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            raise body_failure

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(exercise())

    assert raised.value is body_failure
    assert raised.value.__cause__ is audit_failure
    assert provider.calls[-1] == ("destroy", "sandbox-17")


def test_body_audit_and_destroy_failure_preserve_all_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_failure = RuntimeError("body failed")
    audit_failure = ValueError("audit failed")
    destroy_failure = OSError("destroy failed")

    class DestroyFailingProvider(_Provider):
        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            raise destroy_failure

    artifact = _ScriptedArtifact(
        ("flush", "destroy_attempt"),
        audit_failure,
    )
    _patch_artifact_open(monkeypatch, artifact)
    provider = DestroyFailingProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            raise body_failure

    with pytest.raises(CleanupError) as raised:
        asyncio.run(exercise())

    assert raised.value.body_failure is body_failure
    assert raised.value.destroy_failure is destroy_failure
    assert raised.value.audit_failures == (audit_failure,)
    assert raised.value.__cause__ is destroy_failure


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_existing_or_linked_artifact_is_rejected_before_create(
    link_kind: str,
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.jsonl"
    victim.write_text("DO NOT OVERWRITE", encoding="utf-8")
    artifact = tmp_path / "lifecycle.jsonl"
    if link_kind == "symlink":
        artifact.symlink_to(victim)
    else:
        artifact.hardlink_to(victim)
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ):
            raise AssertionError("body must not run")

    with pytest.raises(FileExistsError):
        asyncio.run(exercise())

    assert provider.calls == []
    assert victim.read_text(encoding="utf-8") == "DO NOT OVERWRITE"


def test_existing_regular_artifact_is_not_truncated(tmp_path: Path) -> None:
    artifact = tmp_path / "lifecycle.jsonl"
    artifact.write_text("existing evidence", encoding="utf-8")
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=artifact,
        ):
            raise AssertionError("body must not run")

    with pytest.raises(FileExistsError):
        asyncio.run(exercise())

    assert provider.calls == []
    assert artifact.read_text(encoding="utf-8") == "existing evidence"


def test_concurrent_same_artifact_allows_only_one_provider_create(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        first_create_started = asyncio.Event()
        allow_first_create = asyncio.Event()

        class BlockingCreateProvider(_Provider):
            async def create(self, workspace: Path, name: str) -> str:
                self.calls.append(("create", workspace, name))
                first_create_started.set()
                await allow_first_create.wait()
                return "sandbox-first"

        first_provider = BlockingCreateProvider()
        second_provider = _Provider()
        artifact = tmp_path / "lifecycle.jsonl"

        async def run_first() -> None:
            async with managed_sandbox(
                first_provider,
                tmp_path / "first-workspace",
                "first",
                lifecycle_artifact=artifact,
            ):
                pass

        async def run_second() -> None:
            async with managed_sandbox(
                second_provider,
                tmp_path / "second-workspace",
                "second",
                lifecycle_artifact=artifact,
            ):
                raise AssertionError("second body must not run")

        first_task = asyncio.create_task(run_first())
        await first_create_started.wait()
        second_failure: BaseException | None = None
        try:
            await run_second()
        except FileExistsError as failure:
            second_failure = failure
        finally:
            allow_first_create.set()
            await first_task

        assert isinstance(second_failure, FileExistsError)
        return first_provider.calls, second_provider.calls

    first_calls, second_calls = asyncio.run(exercise())

    assert first_calls == [
        ("create", tmp_path / "first-workspace", "first"),
        ("destroy", "sandbox-first"),
    ]
    assert second_calls == []


def test_artifact_resolving_inside_workspace_is_rejected_before_create(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            workspace,
            "trial",
            lifecycle_artifact=workspace / "lifecycle.jsonl",
        ):
            raise AssertionError("body must not run")

    with pytest.raises(ValueError, match="outside workspace"):
        asyncio.run(exercise())

    assert provider.calls == []


def test_artifact_parent_symlink_into_workspace_is_rejected_before_create(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(workspace, target_is_directory=True)
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            workspace,
            "trial",
            lifecycle_artifact=linked_parent / "lifecycle.jsonl",
        ):
            raise AssertionError("body must not run")

    with pytest.raises(ValueError, match="outside workspace"):
        asyncio.run(exercise())

    assert provider.calls == []
    assert not (workspace / "lifecycle.jsonl").exists()


def test_missing_artifact_parent_is_not_created(tmp_path: Path) -> None:
    parent = tmp_path / "missing-parent"
    provider = _Provider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=parent / "lifecycle.jsonl",
        ):
            raise AssertionError("body must not run")

    with pytest.raises(FileNotFoundError):
        asyncio.run(exercise())

    assert provider.calls == []
    assert not parent.exists()


@pytest.mark.parametrize("failure_stage", ["none", "create", "destroy"])
def test_every_successful_event_write_is_immediately_flushed(
    failure_stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_failure = RuntimeError(f"{failure_stage} failed")

    class ConfigurableProvider(_Provider):
        async def create(self, workspace: Path, name: str) -> str:
            self.calls.append(("create", workspace, name))
            if failure_stage == "create":
                raise provider_failure
            return "sandbox-17"

        async def destroy(self, sandbox_id: str) -> None:
            self.calls.append(("destroy", sandbox_id))
            if failure_stage == "destroy":
                raise provider_failure

    artifact = _ScriptedArtifact(
        ("never", None),
        AssertionError("must not fail"),
    )
    _patch_artifact_open(monkeypatch, artifact)
    provider = ConfigurableProvider()

    async def exercise() -> None:
        async with managed_sandbox(
            provider,
            tmp_path / "workspace",
            "trial",
            lifecycle_artifact=tmp_path / "lifecycle.jsonl",
        ):
            pass

    if failure_stage == "none":
        asyncio.run(exercise())
        events = [
            "create_attempt",
            "create_success",
            "destroy_attempt",
            "destroy_success",
        ]
    elif failure_stage == "create":
        with pytest.raises(RuntimeError):
            asyncio.run(exercise())
        events = ["create_attempt", "create_failure"]
    else:
        with pytest.raises(CleanupError):
            asyncio.run(exercise())
        events = [
            "create_attempt",
            "create_success",
            "destroy_attempt",
            "destroy_failure",
        ]

    expected_operations = [
        operation
        for event in events
        for operation in [("write", event), ("flush", event)]
    ]
    assert artifact.operations == [*expected_operations, ("close", None)]
