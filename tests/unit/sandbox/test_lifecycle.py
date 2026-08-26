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
    assert raised.value.body_failure is artifact_failure
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
