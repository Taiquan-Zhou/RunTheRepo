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
        '{"Service":"web","State":7,"Health":"healthy"}',
        '{"Service":"web","State":"running","Health":7}',
        '{"Service":"web","State":"running"}',
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


def test_sensitive_evidence_is_redacted_before_every_source_is_truncated() -> None:
    supplied_secret = "env-secret-value-93a427"
    sensitive_prefix = (
        "Authorization: Bearer bearer-value-123\n"
        "TOKEN=token-value-456\n"
        "Password: password-value-789\n"
        "Secret: secret phrase with spaces\n"
        "api-key: api-key-value-012\n"
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
        assert supplied_secret not in value
        assert "[REDACTED]" in value
        assert "APP_REQUIRED_TOKEN is required" in value
        assert len(value) == 65_536
        assert value.endswith("\n...[truncated]")


@pytest.mark.parametrize(
    ("compose_path", "env"),
    [
        ("/workspace/com\x00pose.yml", {}),
        (cast(str, 7), {}),
        (COMPOSE_PATH, {"BAD-KEY": "value"}),
        (COMPOSE_PATH, {"BAD\x00KEY": "value"}),
        (COMPOSE_PATH, {"GOOD_KEY": "bad\x00value"}),
        (COMPOSE_PATH, cast(dict[str, str], {"GOOD_KEY": 7})),
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
