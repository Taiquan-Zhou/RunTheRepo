import asyncio
import copy
import hashlib
import inspect
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from repotrial.domain.models import ObservationSnapshot
from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider
from repotrial.trial.observer import (
    ObservationCollectionError,
    ObservationParseError,
    collect_observation,
)

CONTAINER_A = "a" * 12
CONTAINER_B = "b" * 64
DISCOVERY_ARGV = (
    "docker",
    "compose",
    "-f",
    "compose.yaml",
    "ps",
    "--all",
    "--no-trunc",
    "--orphans=false",
    "--format",
    "json",
)
OVERLAY_DISCOVERY_ARGV = (
    "docker",
    "compose",
    "-f",
    "compose.yaml",
    "-f",
    "candidate.overlay.yml",
    "ps",
    "--all",
    "--no-trunc",
    "--orphans=false",
    "--format",
    "json",
)
TOP_FORMAT = "pid,ppid,user,comm"
TOP_HEADER = "PID PPID USER COMMAND"


class ScriptedProvider(SandboxProvider):
    def __init__(
        self,
        scripts: Mapping[tuple[str, ...], ExecResult | BaseException],
        network_result: NetworkLogResult | BaseException | None = None,
    ) -> None:
        self.scripts = dict(scripts)
        self.network_result = network_result or NetworkLogResult(
            events=[], supported=True, unsupported_reason=None
        )
        self.calls: list[tuple[object, ...]] = []

    async def create(self, workspace: Path, name: str) -> str:
        raise AssertionError("observer must not create a sandbox")

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self.calls.append(("exec", sandbox_id, tuple(argv), timeout_s))
        response = self.scripts[tuple(argv)]
        if isinstance(response, BaseException):
            raise response
        return response

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        raise AssertionError("observer must not publish ports")

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        raise AssertionError("observer must not copy files")

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        self.calls.append(("network_log", sandbox_id))
        if isinstance(self.network_result, BaseException):
            raise self.network_result
        return self.network_result

    async def destroy(self, sandbox_id: str) -> None:
        raise AssertionError("observer must not destroy the sandbox")


def _result(stdout: str = "", *, exit_code: int = 0, stderr: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _discovery_line(service: str, container_id: str) -> str:
    return json.dumps({"Service": service, "ID": container_id})


def _scripts_for(
    containers: list[tuple[str, str]],
    *,
    discovery_stdout: str | None = None,
) -> dict[tuple[str, ...], ExecResult | BaseException]:
    if discovery_stdout is None:
        discovery_stdout = "\n".join(
            _discovery_line(service, container_id)
            for service, container_id in containers
        )
    scripts: dict[tuple[str, ...], ExecResult | BaseException] = {
        DISCOVERY_ARGV: _result(discovery_stdout)
    }
    for _, container_id in containers:
        scripts[("docker", "inspect", container_id)] = _result(
            json.dumps([{"Id": container_id, "Config": {"Env": []}}])
        )
        scripts[("docker", "diff", container_id)] = _result()
        scripts[
            (
                "docker",
                "top",
                container_id,
                "-eo",
                TOP_FORMAT,
            )
        ] = _result(TOP_HEADER + "\n")
    return scripts


def _collect(provider: SandboxProvider, artifact_path: Path) -> ObservationSnapshot:
    return asyncio.run(
        collect_observation(
            provider=provider,
            sandbox_id="sandbox-1",
            compose_path="compose.yaml",
            artifact_path=artifact_path,
        )
    )


def test_public_collect_observation_signature_remains_unchanged() -> None:
    assert str(inspect.signature(collect_observation)) == (
        "(provider: repotrial.sandbox.base.SandboxProvider, sandbox_id: str, "
        "compose_path: str, artifact_path: pathlib.Path, *, "
        "overlay_path: str | None = None) -> "
        "repotrial.domain.models.ObservationSnapshot"
    )


def test_public_collection_without_diagnostics_creates_only_audit_artifact(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "observation.json"

    _collect(ScriptedProvider(_scripts_for([])), artifact_path)

    assert list(tmp_path.iterdir()) == [artifact_path]


def _assert_parse_failure(
    tmp_path: Path,
    scripts: Mapping[tuple[str, ...], ExecResult | BaseException],
    network_result: NetworkLogResult | None = None,
) -> None:
    artifact_path = tmp_path / "observation.json"
    provider = ScriptedProvider(scripts, network_result)

    with pytest.raises(ObservationParseError):
        _collect(provider, artifact_path)

    assert not artifact_path.exists()


def test_collects_two_containers_in_sorted_order_with_exact_commands_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def host_execution_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("observer attempted host execution")

    monkeypatch.setattr(subprocess, "run", host_execution_forbidden)
    monkeypatch.setattr(subprocess, "Popen", host_execution_forbidden)
    monkeypatch.setattr(os, "system", host_execution_forbidden)

    discovery_stdout = (
        f'{{"Service":"web","ID":"{CONTAINER_B}"}}\n'
        f'{{"Service":"api","ID":"{CONTAINER_A}"}}\n'
    )
    scripts = _scripts_for(
        [("web", CONTAINER_B), ("api", CONTAINER_A)],
        discovery_stdout=discovery_stdout,
    )
    api_inspect_stdout = json.dumps(
        [
            {
                "Id": CONTAINER_A,
                "Config": {
                    "Env": [
                        "ORDINARY=visible-value",
                        "TOKEN=secret-token",
                        "PASSWORD=hunter2",
                        "PRIVATE_KEY=-----BEGIN PRIVATE KEY-----key-material",
                    ]
                },
                "State": {"Running": True},
            }
        ],
        separators=(",", ":"),
    )
    web_inspect_stdout = json.dumps(
        [{"Id": CONTAINER_B, "Config": {"Env": ["MODE=production"]}}],
        separators=(",", ":"),
    )
    api_diff_stdout = "A /tmp/created\nC /app/config\nD /tmp/removed\n"
    api_top_stdout = TOP_HEADER + "\n1 0 root api-server\n22 1 app worker\n"
    web_diff_stdout = "C /var/cache/web\n"
    web_top_stdout = TOP_HEADER + "\n7 1 nginx nginx\n"
    scripts[("docker", "inspect", CONTAINER_A)] = _result(api_inspect_stdout)
    scripts[("docker", "diff", CONTAINER_A)] = _result(api_diff_stdout)
    scripts[("docker", "top", CONTAINER_A, "-eo", TOP_FORMAT)] = _result(api_top_stdout)
    scripts[("docker", "inspect", CONTAINER_B)] = _result(web_inspect_stdout)
    scripts[("docker", "diff", CONTAINER_B)] = _result(web_diff_stdout)
    scripts[("docker", "top", CONTAINER_B, "-eo", TOP_FORMAT)] = _result(web_top_stdout)
    network_events = [
        {"protocol": "tcp", "destination": "db:5432", "bytes": 42},
        {"protocol": "udp", "destination": "dns:53", "meta": {"ok": True}},
    ]
    original_network_events = copy.deepcopy(network_events)
    provider = ScriptedProvider(
        scripts,
        NetworkLogResult(
            events=network_events, supported=True, unsupported_reason=None
        ),
    )
    artifact_path = tmp_path / "observation.json"

    snapshot = _collect(provider, artifact_path)

    redacted_api = {
        "Id": CONTAINER_A,
        "Config": {
            "Env": [
                "ORDINARY=[REDACTED]",
                "TOKEN=[REDACTED]",
                "PASSWORD=[REDACTED]",
                "PRIVATE_KEY=[REDACTED]",
            ]
        },
        "State": {"Running": True},
    }
    redacted_web = {
        "Id": CONTAINER_B,
        "Config": {"Env": ["MODE=[REDACTED]"]},
    }
    assert snapshot == ObservationSnapshot(
        inspect={
            "api": [{"container_id": CONTAINER_A, "data": redacted_api}],
            "web": [{"container_id": CONTAINER_B, "data": redacted_web}],
        },
        file_changes=[
            {
                "service": "api",
                "container_id": CONTAINER_A,
                "operation": "added",
                "path": "/tmp/created",
            },
            {
                "service": "api",
                "container_id": CONTAINER_A,
                "operation": "changed",
                "path": "/app/config",
            },
            {
                "service": "api",
                "container_id": CONTAINER_A,
                "operation": "deleted",
                "path": "/tmp/removed",
            },
            {
                "service": "web",
                "container_id": CONTAINER_B,
                "operation": "changed",
                "path": "/var/cache/web",
            },
        ],
        process_events=[
            {
                "service": "api",
                "container_id": CONTAINER_A,
                "pid": 1,
                "ppid": 0,
                "user": "root",
                "command": "api-server",
            },
            {
                "service": "api",
                "container_id": CONTAINER_A,
                "pid": 22,
                "ppid": 1,
                "user": "app",
                "command": "worker",
            },
            {
                "service": "web",
                "container_id": CONTAINER_B,
                "pid": 7,
                "ppid": 1,
                "user": "nginx",
                "command": "nginx",
            },
        ],
        network_events=original_network_events,
        unsupported_collectors=[],
    )
    assert provider.calls == [
        ("exec", "sandbox-1", DISCOVERY_ARGV, 30),
        ("exec", "sandbox-1", ("docker", "inspect", CONTAINER_A), 30),
        ("exec", "sandbox-1", ("docker", "diff", CONTAINER_A), 30),
        (
            "exec",
            "sandbox-1",
            (
                "docker",
                "top",
                CONTAINER_A,
                "-eo",
                TOP_FORMAT,
            ),
            30,
        ),
        ("exec", "sandbox-1", ("docker", "inspect", CONTAINER_B), 30),
        ("exec", "sandbox-1", ("docker", "diff", CONTAINER_B), 30),
        (
            "exec",
            "sandbox-1",
            (
                "docker",
                "top",
                CONTAINER_B,
                "-eo",
                TOP_FORMAT,
            ),
            30,
        ),
        ("network_log", "sandbox-1"),
    ]

    assert network_events == original_network_events
    snapshot.network_events[0]["bytes"] = 99
    assert network_events == original_network_events

    artifact_bytes = artifact_path.read_bytes()
    assert artifact_bytes.endswith(b"\n")
    artifact = json.loads(artifact_bytes)
    assert set(artifact) == {
        "schema_version",
        "discovery",
        "services",
        "network_runtime",
        "snapshot",
    }
    assert artifact["schema_version"] == 1
    assert artifact["discovery"] == {
        "argv": list(DISCOVERY_ARGV),
        "stdout_sha256": hashlib.sha256(discovery_stdout.encode()).hexdigest(),
        "parsed": [
            {"service": "api", "container_id": CONTAINER_A},
            {"service": "web", "container_id": CONTAINER_B},
        ],
    }
    assert artifact["services"]["api"] == [
        {
            "container_id": CONTAINER_A,
            "inspect": {
                "argv": ["docker", "inspect", CONTAINER_A],
                "stdout_sha256": hashlib.sha256(
                    api_inspect_stdout.encode()
                ).hexdigest(),
                "parsed": redacted_api,
            },
            "diff": {
                "argv": ["docker", "diff", CONTAINER_A],
                "stdout_sha256": hashlib.sha256(api_diff_stdout.encode()).hexdigest(),
                "parsed": [
                    {"operation": "added", "path": "/tmp/created"},
                    {"operation": "changed", "path": "/app/config"},
                    {"operation": "deleted", "path": "/tmp/removed"},
                ],
            },
            "top": {
                "argv": [
                    "docker",
                    "top",
                    CONTAINER_A,
                    "-eo",
                    TOP_FORMAT,
                ],
                "stdout_sha256": hashlib.sha256(api_top_stdout.encode()).hexdigest(),
                "parsed": [
                    {"pid": 1, "ppid": 0, "user": "root", "command": "api-server"},
                    {"pid": 22, "ppid": 1, "user": "app", "command": "worker"},
                ],
            },
        }
    ]
    assert artifact["network_runtime"] == {
        "supported": True,
        "unsupported_reason": None,
        "parsed": original_network_events,
    }
    expected_snapshot = snapshot.model_copy(deep=True)
    expected_snapshot.network_events[0]["bytes"] = 42
    assert artifact["snapshot"] == expected_snapshot.model_dump(mode="json")
    serialized = artifact_bytes.decode("utf-8")
    for secret in (
        "visible-value",
        "secret-token",
        "hunter2",
        "key-material",
        "stderr-secret",
    ):
        assert secret not in serialized
    assert artifact_bytes == (
        json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def test_overlay_discovery_uses_base_then_overlay_in_exact_order(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider({OVERLAY_DISCOVERY_ARGV: _result()})
    artifact_path = tmp_path / "observation.json"

    snapshot = asyncio.run(
        collect_observation(
            provider,
            "sandbox-1",
            "compose.yaml",
            artifact_path,
            overlay_path="candidate.overlay.yml",
        )
    )

    assert snapshot == ObservationSnapshot()
    assert provider.calls == [
        ("exec", "sandbox-1", OVERLAY_DISCOVERY_ARGV, 30),
        ("network_log", "sandbox-1"),
    ]
    audit = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert audit["discovery"]["argv"] == list(OVERLAY_DISCOVERY_ARGV)


@pytest.mark.parametrize("overlay_path", [cast(str, 7), "bad\0overlay.yml"])
def test_invalid_overlay_fails_before_observer_provider_calls(
    tmp_path: Path, overlay_path: str
) -> None:
    provider = ScriptedProvider(_scripts_for([]))

    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            collect_observation(
                provider,
                "sandbox-1",
                "compose.yaml",
                tmp_path / "observation.json",
                overlay_path=overlay_path,
            )
        )

    assert provider.calls == []


def test_identical_inputs_create_byte_identical_artifacts(tmp_path: Path) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    _collect(ScriptedProvider(copy.deepcopy(scripts)), first)
    _collect(ScriptedProvider(copy.deepcopy(scripts)), second)

    assert first.read_bytes() == second.read_bytes()


def test_unicode_service_without_category_c_characters_is_accepted(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(_scripts_for([("服务.一", CONTAINER_A)]))

    snapshot = _collect(provider, tmp_path / "observation.json")

    assert list(snapshot.inspect) == ["服务.一"]


def test_literal_unicode_line_separator_in_service_is_preserved_with_crlf_framing(
    tmp_path: Path,
) -> None:
    service = "api\u2028worker"
    discovery_stdout = (
        json.dumps(
            {"Service": service, "ID": CONTAINER_A},
            ensure_ascii=False,
        )
        + "\r\n"
    )
    provider = ScriptedProvider(
        _scripts_for(
            [(service, CONTAINER_A)],
            discovery_stdout=discovery_stdout,
        )
    )

    snapshot = _collect(provider, tmp_path / "observation.json")

    assert list(snapshot.inspect) == [service]


@pytest.mark.parametrize(
    ("network_result", "expected_unsupported"),
    [
        (
            NetworkLogResult(events=[], supported=True, unsupported_reason=None),
            [],
        ),
        (
            NetworkLogResult(
                events=[],
                supported=False,
                unsupported_reason="runtime capture unavailable",
            ),
            ["network_runtime"],
        ),
    ],
)
def test_empty_network_result_distinguishes_observed_from_unsupported(
    tmp_path: Path,
    network_result: NetworkLogResult,
    expected_unsupported: list[str],
) -> None:
    artifact_path = tmp_path / "observation.json"
    snapshot = _collect(
        ScriptedProvider(_scripts_for([]), network_result), artifact_path
    )

    assert snapshot.network_events == []
    assert snapshot.unsupported_collectors == expected_unsupported
    audit_network = json.loads(artifact_path.read_text(encoding="utf-8"))[
        "network_runtime"
    ]
    assert audit_network == {
        "supported": network_result.supported,
        "unsupported_reason": network_result.unsupported_reason,
        "parsed": [],
    }


@pytest.mark.parametrize(
    "discovery_stdout",
    [
        "[]",
        '{"Service":"api","Service":"web","ID":"' + CONTAINER_A + '"}',
        '{"Service":"api","ID":"' + CONTAINER_A + '","extra":NaN}',
        '{"Service":"   ","ID":"' + CONTAINER_A + '"}',
        '{"Service":"bad\\u200bname","ID":"' + CONTAINER_A + '"}',
        _discovery_line("s" * 129, CONTAINER_A),
        _discovery_line("api", "A" * 12),
        _discovery_line("api", "a" * 13),
        _discovery_line("api", CONTAINER_A)
        + "\n"
        + _discovery_line("api", CONTAINER_A),
        _discovery_line("api", CONTAINER_A)
        + "\n"
        + _discovery_line("web", CONTAINER_A),
    ],
)
def test_malformed_or_duplicate_discovery_rows_fail_closed(
    tmp_path: Path, discovery_stdout: str
) -> None:
    _assert_parse_failure(
        tmp_path,
        _scripts_for([], discovery_stdout=discovery_stdout),
    )


@pytest.mark.parametrize(
    "inspect_stdout",
    [
        "{}",
        "[]",
        "[{},{}]",
        "[1]",
        '[{"State":{},"State":{}}]',
        '[{"value":NaN}]',
        '[{"Config":{"Env":"TOKEN=secret"}}]',
        '[{"Config":{"Env":[1]}}]',
        '[{"Config":{"Env":["missing-assignment"]}}]',
        '[{"Config":{"Env":["=missing-name"]}}]',
    ],
)
def test_malformed_inspect_output_fails_closed(
    tmp_path: Path, inspect_stdout: str
) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "inspect", CONTAINER_A)] = _result(inspect_stdout)

    _assert_parse_failure(tmp_path, scripts)


@pytest.mark.parametrize(
    "diff_stdout",
    [
        "X /unknown\n",
        "A\n",
        "A \n",
        "AA /two-symbols\n",
        "A /ok\nmalformed\n",
        "A /bad\x01path\n",
    ],
)
def test_unknown_or_malformed_diff_output_fails_closed(
    tmp_path: Path, diff_stdout: str
) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "diff", CONTAINER_A)] = _result(diff_stdout)

    _assert_parse_failure(tmp_path, scripts)


@pytest.mark.parametrize(
    "top_stdout",
    [
        "1 0 root\n",
        "1 0 root app extra\n",
        "-1 0 root app\n",
        "one 0 root app\n",
        "1 -2 root app\n",
        "1 0 ro\x01ot app\n",
    ],
)
def test_unknown_or_malformed_top_output_fails_closed(
    tmp_path: Path, top_stdout: str
) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "top", CONTAINER_A, "-eo", TOP_FORMAT)] = _result(
        TOP_HEADER + "\n" + top_stdout
    )

    _assert_parse_failure(tmp_path, scripts)


def test_top_uses_header_format_and_parses_standard_output(tmp_path: Path) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "top", CONTAINER_A, "-eo", TOP_FORMAT)] = _result(
        TOP_HEADER + "\n1 0 root app\n"
    )

    snapshot = _collect(ScriptedProvider(scripts), tmp_path / "observation.json")

    assert snapshot.process_events == [
        {
            "service": "api",
            "container_id": CONTAINER_A,
            "pid": 1,
            "ppid": 0,
            "user": "root",
            "command": "app",
        }
    ]


@pytest.mark.parametrize(
    "top_stdout",
    [
        "",
        "1 0 root app\n",
        "PID PPID USER CMD\n1 0 root app\n",
        "PID PPID USER COMMAND EXTRA\n1 0 root app\n",
        TOP_HEADER + "\n" + TOP_HEADER + "\n1 0 root app\n",
    ],
)
def test_missing_wrong_or_duplicate_top_header_fails_closed(
    tmp_path: Path, top_stdout: str
) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "top", CONTAINER_A, "-eo", TOP_FORMAT)] = _result(top_stdout)

    _assert_parse_failure(tmp_path, scripts)


@pytest.mark.parametrize("oversized_field", ["pid", "ppid"])
def test_oversized_decimal_process_id_fails_as_context_free_parse_error(
    tmp_path: Path, oversized_field: str
) -> None:
    fields = {
        "pid": ["9" * 5_000, "0", "root", "app"],
        "ppid": ["1", "9" * 5_000, "root", "app"],
    }[oversized_field]
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "top", CONTAINER_A, "-eo", TOP_FORMAT)] = _result(
        TOP_HEADER + "\n" + " ".join(fields) + "\n"
    )
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(ObservationParseError) as raised:
        _collect(ScriptedProvider(scripts), artifact_path)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert not artifact_path.exists()


@pytest.mark.parametrize("failing_command", ["discovery", "inspect", "diff", "top"])
def test_nonzero_collector_command_raises_without_output_leakage(
    tmp_path: Path, failing_command: str
) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    keys = {
        "discovery": DISCOVERY_ARGV,
        "inspect": ("docker", "inspect", CONTAINER_A),
        "diff": ("docker", "diff", CONTAINER_A),
        "top": (
            "docker",
            "top",
            CONTAINER_A,
            "-eo",
            TOP_FORMAT,
        ),
    }
    scripts[keys[failing_command]] = _result(
        "stdout-super-secret", exit_code=9, stderr="stderr-super-secret"
    )
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(ObservationCollectionError) as raised:
        _collect(ScriptedProvider(scripts), artifact_path)

    assert not isinstance(raised.value, ObservationParseError)
    assert "stdout-super-secret" not in str(raised.value)
    assert "stderr-super-secret" not in str(raised.value)
    assert not artifact_path.exists()


def test_parse_error_does_not_retain_raw_stdout_in_exception_chain(
    tmp_path: Path,
) -> None:
    raw_secret = "raw-parser-secret"
    scripts = _scripts_for([], discovery_stdout=f'{{"Service":"{raw_secret}"')

    with pytest.raises(ObservationParseError) as raised:
        _collect(ScriptedProvider(scripts), tmp_path / "observation.json")

    assert raw_secret not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_unencodable_stdout_is_not_retained_in_exception_chain(
    tmp_path: Path,
) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    scripts[("docker", "inspect", CONTAINER_A)] = _result(
        '[{"Config":{"Env":[]},"raw":"\ud800"}]'
    )

    with pytest.raises(ObservationParseError) as raised:
        _collect(ScriptedProvider(scripts), tmp_path / "observation.json")

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "network_result",
    [
        NetworkLogResult(
            events=[{"destination": "db"}],
            supported=False,
            unsupported_reason="not supported",
        ),
        NetworkLogResult(
            events=[], supported=True, unsupported_reason="contradictory reason"
        ),
        NetworkLogResult(events=[], supported=False, unsupported_reason=None),
        NetworkLogResult(events=[], supported=False, unsupported_reason=""),
    ],
)
def test_contradictory_or_incomplete_network_results_fail_closed(
    tmp_path: Path, network_result: NetworkLogResult
) -> None:
    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


def test_single_stdout_limit_is_checked_before_parsing(tmp_path: Path) -> None:
    oversized = "raw-secret" + "x" * 1_048_567
    scripts = _scripts_for([], discovery_stdout=oversized)
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(ObservationParseError) as raised:
        _collect(ScriptedProvider(scripts), artifact_path)

    assert "raw-secret" not in str(raised.value)
    assert type(raised.value) is ObservationParseError
    assert not artifact_path.exists()


def test_total_stdout_limit_is_checked_before_later_output_parsing(
    tmp_path: Path,
) -> None:
    containers = [(f"service-{index}", f"{index + 1:012x}") for index in range(9)]
    scripts = _scripts_for(containers)
    blank_limit = " " * 1_048_576
    for _, container_id in containers:
        scripts[("docker", "diff", container_id)] = _result(blank_limit)
        scripts[
            (
                "docker",
                "top",
                container_id,
                "-eo",
                TOP_FORMAT,
            )
        ] = _result(blank_limit)

    _assert_parse_failure(tmp_path, scripts)


def test_container_limit_fails_before_per_container_collection(tmp_path: Path) -> None:
    containers = [(f"service-{index}", f"{index + 1:012x}") for index in range(65)]
    provider = ScriptedProvider(_scripts_for(containers))
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(ObservationParseError):
        _collect(provider, artifact_path)

    assert provider.calls == [("exec", "sandbox-1", DISCOVERY_ARGV, 30)]
    assert not artifact_path.exists()


@pytest.mark.parametrize("collector", ["diff", "top"])
def test_per_container_row_limits_fail_closed(tmp_path: Path, collector: str) -> None:
    scripts = _scripts_for([("api", CONTAINER_A)])
    if collector == "diff":
        scripts[("docker", "diff", CONTAINER_A)] = _result("A /path\n" * 4_097)
    else:
        scripts[("docker", "top", CONTAINER_A, "-eo", TOP_FORMAT)] = _result(
            TOP_HEADER + "\n" + "1 0 root app\n" * 4_097
        )

    _assert_parse_failure(tmp_path, scripts)


def test_network_event_limit_fails_closed(tmp_path: Path) -> None:
    network_result = NetworkLogResult(
        events=[{"index": index} for index in range(4_097)],
        supported=True,
        unsupported_reason=None,
    )

    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


def test_json_node_limit_fails_closed(tmp_path: Path) -> None:
    network_result = NetworkLogResult(
        events=[{"values": [0] * 100_001}],
        supported=True,
        unsupported_reason=None,
    )

    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


def test_json_depth_limit_fails_closed(tmp_path: Path) -> None:
    nested: dict[str, object] = {}
    for _ in range(65):
        nested = {"next": nested}
    network_result = NetworkLogResult(
        events=[nested], supported=True, unsupported_reason=None
    )

    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


def test_json_string_limit_fails_closed(tmp_path: Path) -> None:
    network_result = NetworkLogResult(
        events=[{"value": "x" * 65_537}],
        supported=True,
        unsupported_reason=None,
    )

    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


@pytest.mark.parametrize("bad_value", [object(), {1, 2, 3}])
def test_unsupported_python_network_values_fail_closed(
    tmp_path: Path, bad_value: object
) -> None:
    network_result = NetworkLogResult.model_construct(
        events=[{"value": bad_value}], supported=True, unsupported_reason=None
    )

    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


def test_cyclic_network_value_fails_closed(tmp_path: Path) -> None:
    event: dict[str, object] = {}
    event["self"] = event
    network_result = NetworkLogResult.model_construct(
        events=[event], supported=True, unsupported_reason=None
    )

    _assert_parse_failure(tmp_path, _scripts_for([]), network_result)


def test_artifact_size_limit_is_checked_before_destination_open(tmp_path: Path) -> None:
    repeated = "x" * 65_536
    network_result = NetworkLogResult(
        events=[{"value": repeated} for _ in range(400)],
        supported=True,
        unsupported_reason=None,
    )
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(ObservationCollectionError):
        _collect(ScriptedProvider(_scripts_for([]), network_result), artifact_path)

    assert not artifact_path.exists()


@pytest.mark.parametrize(
    ("sandbox_id", "compose_path"),
    [
        ("", "compose.yaml"),
        ("sandbox-1", ""),
        ("sandbox-1", "x" * 4_097),
        ("sandbox-1", "bad\0compose.yaml"),
        ("sandbox-1", "bad\x1fcompose.yaml"),
        ("sandbox-1", "bad\u200bcompose.yaml"),
    ],
)
def test_invalid_inputs_fail_before_provider_calls(
    tmp_path: Path, sandbox_id: str, compose_path: str
) -> None:
    provider = ScriptedProvider(_scripts_for([]))

    with pytest.raises((TypeError, ValueError, ObservationCollectionError)):
        asyncio.run(
            collect_observation(
                provider,
                sandbox_id,
                compose_path,
                tmp_path / "observation.json",
            )
        )

    assert provider.calls == []


def test_non_string_compose_path_fails_before_provider_calls(tmp_path: Path) -> None:
    provider = ScriptedProvider(_scripts_for([]))

    with pytest.raises(TypeError):
        asyncio.run(
            collect_observation(
                provider,
                "sandbox-1",
                cast(str, Path("compose.yaml")),
                tmp_path / "observation.json",
            )
        )

    assert provider.calls == []


def test_existing_artifact_is_not_overwritten_and_provider_is_not_called(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "observation.json"
    artifact_path.write_text("sentinel", encoding="utf-8")
    provider = ScriptedProvider(_scripts_for([]))

    with pytest.raises(ObservationCollectionError):
        _collect(provider, artifact_path)

    assert artifact_path.read_text(encoding="utf-8") == "sentinel"
    assert provider.calls == []


def test_symlink_artifact_is_not_followed_and_provider_is_not_called(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_text("sentinel", encoding="utf-8")
    artifact_path = tmp_path / "observation.json"
    artifact_path.symlink_to(target)
    provider = ScriptedProvider(_scripts_for([]))

    with pytest.raises(ObservationCollectionError):
        _collect(provider, artifact_path)

    assert target.read_text(encoding="utf-8") == "sentinel"
    assert artifact_path.is_symlink()
    assert provider.calls == []


def test_missing_artifact_parent_is_not_created_or_collected(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "missing"
    provider = ScriptedProvider(_scripts_for([]))

    with pytest.raises(ObservationCollectionError):
        _collect(provider, parent / "observation.json")

    assert not parent.exists()
    assert provider.calls == []


class SentinelProviderError(RuntimeError):
    pass


@pytest.mark.parametrize("boundary", ["exec", "network"])
def test_provider_exceptions_propagate_unchanged(tmp_path: Path, boundary: str) -> None:
    sentinel = SentinelProviderError("provider-secret")
    if boundary == "exec":
        provider = ScriptedProvider({DISCOVERY_ARGV: sentinel})
    else:
        provider = ScriptedProvider(_scripts_for([]), sentinel)
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(SentinelProviderError) as raised:
        _collect(provider, artifact_path)

    assert raised.value is sentinel
    assert not artifact_path.exists()


@pytest.mark.parametrize("boundary", ["exec", "network"])
def test_caller_cancellation_propagates_without_fallback(
    tmp_path: Path, boundary: str
) -> None:
    cancellation = asyncio.CancelledError()
    if boundary == "exec":
        provider = ScriptedProvider({DISCOVERY_ARGV: cancellation})
    else:
        provider = ScriptedProvider(_scripts_for([]), cancellation)
    artifact_path = tmp_path / "observation.json"

    with pytest.raises(asyncio.CancelledError) as raised:
        _collect(provider, artifact_path)

    assert raised.value is cancellation
    assert not artifact_path.exists()
