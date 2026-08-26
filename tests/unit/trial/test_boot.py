import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from repotrial.domain.enums import Verdict
from repotrial.sandbox.base import ExecResult
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.boot import BootResult, boot_compose

COMPOSE_PATH = "/workspace/compose.yml"
UP_ARGV = ("docker", "compose", "-f", COMPOSE_PATH, "up", "-d")
PS_ARGV = (
    "docker",
    "compose",
    "-f",
    COMPOSE_PATH,
    "ps",
    "--all",
    "--format",
    "json",
)
LOGS_ARGV = (
    "docker",
    "compose",
    "-f",
    COMPOSE_PATH,
    "logs",
    "--no-color",
    "--tail",
    "200",
)


def _result(*, exit_code: int = 0, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _healthy_ps() -> str:
    return json.dumps({"Service": "web", "State": "running", "Health": "healthy"})


def _run_with_active_sandbox(
    provider: FakeSandboxProvider,
    *,
    env: dict[str, str] | None = None,
    attempt: int = 1,
) -> BootResult:
    async def exercise() -> BootResult:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        return await boot_compose(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            env or {},
            attempt,
        )

    return asyncio.run(exercise())


def test_empty_env_uses_direct_fixed_commands_and_returns_healthy_pass() -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(stdout="created"),
            PS_ARGV: _result(
                stdout=(
                    '{"Service":"web","State":"RUNNING","Health":"HEALTHY"}\n'
                    '{"Service":"worker","State":"running","Health":null}\n'
                )
            ),
            LOGS_ARGV: _result(stdout="ready", stderr="warning"),
        }
    )

    result = _run_with_active_sandbox(provider, attempt=7)

    assert result.verdict is Verdict.PASS
    assert result.attempt == 7
    assert result.service_states == {
        "web": "running/healthy",
        "worker": "running",
    }
    assert result.logs == {
        "up": "created",
        "ps": (
            '{"Service":"web","State":"RUNNING","Health":"HEALTHY"}\n'
            '{"Service":"worker","State":"running","Health":null}\n'
        ),
        "logs": "ready\nwarning",
    }
    assert provider.calls == [
        ("create", Path("missing-workspace"), "trial"),
        ("exec", "sandbox-1", UP_ARGV, 120),
        ("exec", "sandbox-1", PS_ARGV, 30),
        ("exec", "sandbox-1", LOGS_ARGV, 30),
    ]


def test_nonempty_env_is_sorted_prefixed_and_not_mutated() -> None:
    env = {"ZED": "last", "ALPHA": "first"}
    original = dict(env)
    prefix = ("env", "ALPHA=first", "ZED=last")
    up_argv = (*prefix, *UP_ARGV)
    ps_argv = (*prefix, *PS_ARGV)
    logs_argv = (*prefix, *LOGS_ARGV)
    provider = FakeSandboxProvider(
        scripts={
            up_argv: _result(),
            ps_argv: _result(stdout=_healthy_ps()),
            logs_argv: _result(),
        }
    )

    result = _run_with_active_sandbox(provider, env=env)

    assert result.verdict is Verdict.PASS
    assert env == original
    assert provider.calls == [
        ("create", Path("missing-workspace"), "trial"),
        ("exec", "sandbox-1", up_argv, 120),
        ("exec", "sandbox-1", ps_argv, 30),
        ("exec", "sandbox-1", logs_argv, 30),
    ]


@pytest.mark.parametrize(
    ("row", "expected_state"),
    [
        (
            {"Service": "web", "State": "exited", "Health": None},
            "exited",
        ),
        (
            {"Service": "web", "State": "running", "Health": "unhealthy"},
            "running/unhealthy",
        ),
        (
            {"Service": "web", "State": "starting", "Health": "healthy"},
            "starting/healthy",
        ),
    ],
)
def test_nonready_state_or_health_fails_closed(
    row: dict[str, str | None], expected_state: str
) -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=json.dumps(row)),
            LOGS_ARGV: _result(stdout="the application says it is ready"),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.FAIL
    assert result.service_states == {"web": expected_state}


def test_scaled_replica_states_are_aggregated_without_good_overwriting_bad() -> None:
    ps_output = "\n".join(
        [
            json.dumps({"Service": "web", "State": "running", "Health": "unhealthy"}),
            json.dumps({"Service": "web", "State": "running", "Health": "healthy"}),
            json.dumps({"Service": "web", "State": "running", "Health": "healthy"}),
        ]
    )
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=ps_output),
            LOGS_ARGV: _result(),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.FAIL
    assert result.service_states == {"web": "running/healthy, running/unhealthy"}


@pytest.mark.parametrize(
    ("up_code", "ps_code", "logs_code"),
    [(17, 0, 0), (0, 18, 0), (0, 0, 19)],
)
def test_any_nonzero_command_fails_but_all_evidence_calls_still_run(
    up_code: int, ps_code: int, logs_code: int
) -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(exit_code=up_code),
            PS_ARGV: _result(exit_code=ps_code, stdout=_healthy_ps()),
            LOGS_ARGV: _result(exit_code=logs_code),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.FAIL
    assert result.service_states == {"web": "running/healthy"}
    assert provider.calls[1:] == [
        ("exec", "sandbox-1", UP_ARGV, 120),
        ("exec", "sandbox-1", PS_ARGV, 30),
        ("exec", "sandbox-1", LOGS_ARGV, 30),
    ]


@pytest.mark.parametrize(
    "ps_output",
    [
        "",
        "not-json",
        "[]",
        '{"Service":"","State":"running","Health":"healthy"}',
        '{"Service":" \\t","State":"running","Health":"healthy"}',
        '{"Service":"web","State":7,"Health":"healthy"}',
        '{"Service":"web","State":"running","Health":7}',
        '{"Service":"web","State":"running"}',
        pytest.param(
            '{"Service":"web","State":"running","Health":"healthy",'
            f'"Huge":{"9" * 5_000}}}',
            id="resource-limit-integer",
        ),
        pytest.param(
            '{"Service":"attacker","Service":"web",'
            '"State":"running","Health":"healthy"}',
            id="duplicate-service",
        ),
        pytest.param(
            '{"Service":"web","State":"exited","State":"running","Health":"healthy"}',
            id="duplicate-state",
        ),
        pytest.param(
            '{"Service":"web","State":"running","Health":"unhealthy",'
            '"Health":"healthy"}',
            id="duplicate-health",
        ),
        pytest.param(
            '{"Service":"web","State":"running","Health":"healthy","Metric":NaN}',
            id="nan-constant",
        ),
        pytest.param(
            '{"Service":"web","State":"running","Health":"healthy","Metric":Infinity}',
            id="infinity-constant",
        ),
    ],
)
def test_empty_malformed_or_invalid_schema_ps_never_infers_success_from_logs(
    ps_output: str,
) -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(stdout="success"),
            PS_ARGV: _result(stdout=ps_output),
            LOGS_ARGV: _result(stdout="healthy running ready success"),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.FAIL
    assert result.service_states == {}
    assert result.logs["logs"] == "healthy running ready success"


def test_oversized_ps_fails_before_returning_parsed_service_state() -> None:
    healthy_row = _healthy_ps()
    ps_output = healthy_row + (" " * (65_537 - len(healthy_row)))
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=ps_output),
            LOGS_ARGV: _result(stdout="ready"),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert len(ps_output) == 65_537
    assert result.verdict is Verdict.FAIL
    assert result.service_states == {}


def test_sensitive_evidence_is_redacted_before_every_source_is_truncated() -> None:
    supplied_secret = "env-secret-value-93a427"
    sensitive_prefix = (
        "Authorization: Bearer bearer-value-123\n"
        "TOKEN=token-value-456\n"
        "Password: password-value-789\n"
        "Secret: secret phrase with spaces\n"
        "api-key: api-key-value-012\n"
        '{"password":"json-password-value-345"}\r\n'
        "DATABASE_PASSWORD=database-password-value-678\r\n"
        "DB_TOKEN = db-token-value-901\r\n"
        "X-aPi-KeY: mixed-case-api-value-234\r\n"
        f"leaked={supplied_secret}\n"
        "APP_REQUIRED_TOKEN is required\n"
    )
    oversized = sensitive_prefix + ("x" * 70_000)
    provider = FakeSandboxProvider(
        scripts={
            (
                "env",
                f"DATABASE_PASSWORD={supplied_secret}",
                *UP_ARGV,
            ): _result(stdout=oversized),
            (
                "env",
                f"DATABASE_PASSWORD={supplied_secret}",
                *PS_ARGV,
            ): _result(stdout=_healthy_ps(), stderr=oversized),
            (
                "env",
                f"DATABASE_PASSWORD={supplied_secret}",
                *LOGS_ARGV,
            ): _result(stderr=oversized),
        }
    )

    result = _run_with_active_sandbox(
        provider,
        env={"DATABASE_PASSWORD": supplied_secret},
    )

    for value in result.logs.values():
        assert "bearer-value-123" not in value
        assert "token-value-456" not in value
        assert "password-value-789" not in value
        assert "phrase with spaces" not in value
        assert "api-key-value-012" not in value
        assert "json-password-value-345" not in value
        assert "database-password-value-678" not in value
        assert "db-token-value-901" not in value
        assert "mixed-case-api-value-234" not in value
        assert supplied_secret not in value
        assert "[REDACTED]" in value
        assert "APP_REQUIRED_TOKEN is required" in value
        assert len(value) == 65_536
        assert value.endswith("\n...[truncated]")


def test_provider_truncated_sensitive_env_prefixes_are_redacted_at_markers() -> None:
    supplied_secret = "marker-sensitive-value-93a427-tail"
    stdout_prefix = "marker-sensitive-value-93a"
    stderr_prefix = "marker-sensitive-value-93a427"
    prefix = ("env", f"APP_TOKEN={supplied_secret}")
    provider = FakeSandboxProvider(
        scripts={
            (*prefix, *UP_ARGV): _result(
                stdout=f"stdout leaked={stdout_prefix}\n...[truncated]"
            ),
            (*prefix, *PS_ARGV): _result(stdout=_healthy_ps()),
            (*prefix, *LOGS_ARGV): _result(
                stderr=f"stderr leaked={stderr_prefix}\n...[truncated]"
            ),
        }
    )

    result = _run_with_active_sandbox(
        provider,
        env={"APP_TOKEN": supplied_secret},
    )

    assert stdout_prefix not in result.logs["up"]
    assert stderr_prefix not in result.logs["logs"]
    assert result.logs["up"] == "stdout leaked=[REDACTED]\n...[truncated]"
    assert result.logs["logs"] == "stderr leaked=[REDACTED]\n...[truncated]"


@pytest.mark.parametrize(
    ("compose_path", "env"),
    [
        ("/workspace/com\x00pose.yml", {}),
        (cast(str, 7), {}),
        (COMPOSE_PATH, {"BAD-KEY": "value"}),
        (COMPOSE_PATH, {"BAD\x00KEY": "value"}),
        (COMPOSE_PATH, {"GOOD_KEY": "bad\x00value"}),
        (COMPOSE_PATH, cast(dict[str, str], {"GOOD_KEY": 7})),
        (COMPOSE_PATH, {"PATH": "/attacker/bin"}),
        (COMPOSE_PATH, {"home": "/attacker/home"}),
        (COMPOSE_PATH, {"XdG_Config_Home": "/attacker/config"}),
        (COMPOSE_PATH, {"pythonhome": "/attacker/python"}),
        (COMPOSE_PATH, {"PYTHONPATH": "/attacker/modules"}),
        (COMPOSE_PATH, {"ld_preload": "/attacker/loader.so"}),
        (COMPOSE_PATH, {"DyLd_Insert_Libraries": "/attacker/loader.dylib"}),
        (COMPOSE_PATH, {"docker_host": "tcp://attacker:2375"}),
        (COMPOSE_PATH, {"compose_project_name": "attacker"}),
    ],
)
def test_invalid_path_or_env_fails_before_any_provider_call(
    compose_path: str, env: dict[str, str]
) -> None:
    provider = FakeSandboxProvider()

    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            boot_compose(
                provider,
                "sandbox-not-needed",
                compose_path,
                env,
                attempt=1,
            )
        )

    assert provider.calls == []


def test_provider_exception_propagates_without_becoming_a_false_result() -> None:
    provider = FakeSandboxProvider()

    async def exercise() -> None:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        with pytest.raises(KeyError, match="no scripted result"):
            await boot_compose(
                provider,
                sandbox_id,
                COMPOSE_PATH,
                {},
                attempt=1,
            )

    asyncio.run(exercise())

    assert provider.calls == [
        ("create", Path("missing-workspace"), "trial"),
        ("exec", "sandbox-1", UP_ARGV, 120),
    ]
