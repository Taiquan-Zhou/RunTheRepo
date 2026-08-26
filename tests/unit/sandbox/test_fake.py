import asyncio
import inspect
from pathlib import Path
from typing import cast

import pytest

from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider
from repotrial.sandbox.fake import FakeSandboxProvider


def test_sandbox_provider_exposes_the_frozen_async_contract() -> None:
    assert inspect.isabstract(SandboxProvider)
    assert SandboxProvider.__abstractmethods__ == {
        "copy",
        "create",
        "destroy",
        "exec",
        "network_log",
        "publish_port",
    }

    expected_parameters = {
        "create": ["self", "workspace", "name"],
        "exec": ["self", "sandbox_id", "argv", "timeout_s"],
        "publish_port": ["self", "sandbox_id", "container_port"],
        "copy": ["self", "sandbox_id", "remote_path", "local_path"],
        "network_log": ["self", "sandbox_id"],
        "destroy": ["self", "sandbox_id"],
    }
    for method_name, parameters in expected_parameters.items():
        method = getattr(SandboxProvider, method_name)
        assert inspect.iscoroutinefunction(method)
        assert list(inspect.signature(method).parameters) == parameters

    assert inspect.signature(SandboxProvider.exec).parameters["timeout_s"].default == 60
    assert ExecResult(exit_code=0, stdout="ok", stderr="") == ExecResult(
        exit_code=0,
        stdout="ok",
        stderr="",
    )
    assert NetworkLogResult(supported=True).events == []


def test_fake_provider_runs_a_full_lifecycle_with_an_ordered_call_ledger(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-that-does-not-exist"
    local_path = tmp_path / "artifacts" / "result.json"
    exec_result = ExecResult(exit_code=0, stdout="ready\n", stderr="")
    network_result = NetworkLogResult(
        events=[{"destination": "database", "port": 5432}],
        supported=True,
    )
    provider = FakeSandboxProvider(
        scripts={("docker", "compose", "up", "-d"): exec_result},
        ports={8080: 49152},
        network_result=network_result,
    )

    async def exercise() -> tuple[str, ExecResult, int, NetworkLogResult]:
        sandbox_id = await provider.create(workspace, "trial")
        result = await provider.exec(
            sandbox_id,
            ["docker", "compose", "up", "-d"],
            timeout_s=45,
        )
        host_port = await provider.publish_port(sandbox_id, 8080)
        await provider.copy(sandbox_id, "/run/result.json", local_path)
        observed = await provider.network_log(sandbox_id)
        await provider.destroy(sandbox_id)
        return sandbox_id, result, host_port, observed

    sandbox_id, result, host_port, observed = asyncio.run(exercise())

    assert sandbox_id == "sandbox-1"
    assert result == exec_result
    assert host_port == 49152
    assert observed == network_result
    assert provider.calls == [
        ("create", workspace, "trial"),
        ("exec", "sandbox-1", ("docker", "compose", "up", "-d"), 45),
        ("publish_port", "sandbox-1", 8080),
        ("copy", "sandbox-1", "/run/result.json", local_path),
        ("network_log", "sandbox-1"),
        ("destroy", "sandbox-1"),
    ]
    assert not workspace.exists()
    assert not local_path.exists()


def test_create_returns_unique_deterministic_ids_per_provider(tmp_path: Path) -> None:
    first_provider = FakeSandboxProvider()
    second_provider = FakeSandboxProvider()

    async def create_ids() -> tuple[str, str, str]:
        first = await first_provider.create(tmp_path / "missing-one", "one")
        second = await first_provider.create(tmp_path / "missing-two", "two")
        other = await second_provider.create(tmp_path / "missing-three", "three")
        return first, second, other

    assert asyncio.run(create_ids()) == ("sandbox-1", "sandbox-2", "sandbox-1")


def test_exec_snapshots_argv_and_never_executes_it_on_the_host(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist.txt"
    argv = ["python", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    provider = FakeSandboxProvider(
        scripts={tuple(argv): ExecResult(exit_code=17, stdout="scripted", stderr="no")}
    )

    async def exercise() -> ExecResult:
        sandbox_id = await provider.create(tmp_path / "missing-workspace", "trial")
        return await provider.exec(sandbox_id, argv, timeout_s=9)

    result = asyncio.run(exercise())
    argv.append("mutated-after-call")

    assert result == ExecResult(exit_code=17, stdout="scripted", stderr="no")
    assert provider.calls[1] == (
        "exec",
        "sandbox-1",
        (
            "python",
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ),
        9,
    )
    assert not marker.exists()


def test_exec_rejects_non_list_argv_at_the_provider_boundary(tmp_path: Path) -> None:
    provider = FakeSandboxProvider(
        scripts={("whoami",): ExecResult(exit_code=0, stdout="fake", stderr="")}
    )

    async def exercise() -> None:
        sandbox_id = await provider.create(tmp_path, "trial")
        with pytest.raises(TypeError, match="argv must be a list"):
            await provider.exec(sandbox_id, cast(list[str], ("whoami",)))

    asyncio.run(exercise())
    assert provider.calls == [
        ("create", tmp_path, "trial"),
        ("exec", "sandbox-1", ("whoami",), 60),
    ]


def test_missing_script_and_port_entries_fail_loudly(tmp_path: Path) -> None:
    provider = FakeSandboxProvider()

    async def exercise() -> None:
        sandbox_id = await provider.create(tmp_path, "trial")
        with pytest.raises(KeyError, match="no scripted result"):
            await provider.exec(sandbox_id, ["missing"])
        with pytest.raises(KeyError, match="no published port"):
            await provider.publish_port(sandbox_id, 8080)

    asyncio.run(exercise())


def test_constructor_defensively_copies_script_and_port_mappings(
    tmp_path: Path,
) -> None:
    scripts = {("status",): ExecResult(exit_code=0, stdout="configured", stderr="")}
    ports = {80: 41000}
    provider = FakeSandboxProvider(scripts=scripts, ports=ports)
    scripts.clear()
    ports.clear()

    async def exercise() -> tuple[ExecResult, int]:
        sandbox_id = await provider.create(tmp_path, "trial")
        result = await provider.exec(sandbox_id, ["status"])
        host_port = await provider.publish_port(sandbox_id, 80)
        return result, host_port

    assert asyncio.run(exercise()) == (
        ExecResult(exit_code=0, stdout="configured", stderr=""),
        41000,
    )


def test_destroyed_and_unknown_sandboxes_fail_without_affecting_other_state(
    tmp_path: Path,
) -> None:
    provider = FakeSandboxProvider(
        scripts={("status",): ExecResult(exit_code=0, stdout="active", stderr="")}
    )

    async def exercise() -> ExecResult:
        destroyed_id = await provider.create(tmp_path, "destroyed")
        active_id = await provider.create(tmp_path, "active")
        await provider.destroy(destroyed_id)
        with pytest.raises(RuntimeError, match="sandbox is not active"):
            await provider.exec(destroyed_id, ["status"])
        with pytest.raises(RuntimeError, match="sandbox is not active"):
            await provider.network_log("sandbox-unknown")
        return await provider.exec(active_id, ["status"])

    assert asyncio.run(exercise()) == ExecResult(
        exit_code=0,
        stdout="active",
        stderr="",
    )
    assert provider.calls == [
        ("create", tmp_path, "destroyed"),
        ("create", tmp_path, "active"),
        ("destroy", "sandbox-1"),
        ("exec", "sandbox-1", ("status",), 60),
        ("network_log", "sandbox-unknown"),
        ("exec", "sandbox-2", ("status",), 60),
    ]


def test_destroy_rejects_an_unknown_sandbox_and_records_the_attempt() -> None:
    provider = FakeSandboxProvider()

    with pytest.raises(RuntimeError, match="sandbox is not active"):
        asyncio.run(provider.destroy("sandbox-unknown"))

    assert provider.calls == [("destroy", "sandbox-unknown")]


def test_network_log_distinguishes_observed_empty_from_unsupported(
    tmp_path: Path,
) -> None:
    observed_provider = FakeSandboxProvider(
        network_result=NetworkLogResult(events=[], supported=True)
    )
    unsupported_provider = FakeSandboxProvider()

    async def read_network_results() -> tuple[NetworkLogResult, NetworkLogResult]:
        observed_id = await observed_provider.create(tmp_path, "observed")
        unsupported_id = await unsupported_provider.create(tmp_path, "unsupported")
        observed = await observed_provider.network_log(observed_id)
        unsupported = await unsupported_provider.network_log(unsupported_id)
        return observed, unsupported

    observed, unsupported = asyncio.run(read_network_results())

    assert observed == NetworkLogResult(
        events=[],
        supported=True,
        unsupported_reason=None,
    )
    assert unsupported.events == []
    assert unsupported.supported is False
    assert unsupported.unsupported_reason
