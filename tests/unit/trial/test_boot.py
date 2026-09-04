import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from repotrial.domain.enums import Verdict
from repotrial.sandbox.base import ExecResult
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.boot import BootResult, _boot_compose_with_evidence, boot_compose

COMPOSE_PATH = "/workspace/compose.yml"
UP_ARGV = (
    "docker",
    "compose",
    "-f",
    COMPOSE_PATH,
    "up",
    "-d",
    "--wait",
    "--wait-timeout",
    "60",
)
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
OVERLAY_PATH = "/workspace/candidate.overlay.yml"
OVERLAY_UP_ARGV = (
    "docker",
    "compose",
    "-f",
    COMPOSE_PATH,
    "-f",
    OVERLAY_PATH,
    "up",
    "-d",
    "--wait",
    "--wait-timeout",
    "60",
)
OVERLAY_PS_ARGV = (
    "docker",
    "compose",
    "-f",
    COMPOSE_PATH,
    "-f",
    OVERLAY_PATH,
    "ps",
    "--all",
    "--format",
    "json",
)
OVERLAY_LOGS_ARGV = (
    "docker",
    "compose",
    "-f",
    COMPOSE_PATH,
    "-f",
    OVERLAY_PATH,
    "logs",
    "--no-color",
    "--tail",
    "200",
)


def _result(*, exit_code: int = 0, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _healthy_ps() -> str:
    return json.dumps(
        {"Service": "web", "State": "running", "Health": "healthy", "ExitCode": 0}
    )


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


def test_private_boot_evidence_checkpoints_commands_final_readiness_and_redacts_env(
    tmp_path: Path,
) -> None:
    secret = "runtime-secret-value"
    prefix = ("env", f"PUBLIC_DSN={secret}")
    provider = FakeSandboxProvider(
        scripts={
            (*prefix, *UP_ARGV): _result(exit_code=1, stdout="up", stderr=secret),
            (*prefix, *PS_ARGV): _result(stdout=_healthy_ps()),
            (*prefix, *LOGS_ARGV): _result(stdout="logs"),
        }
    )
    evidence_path = tmp_path / "baseline-boot-attempt.json"

    async def exercise() -> BootResult:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        return await _boot_compose_with_evidence(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {"PUBLIC_DSN": secret},
            attempt=1,
            evidence_path=evidence_path,
        )

    result = asyncio.run(exercise())

    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert result.verdict is Verdict.FAIL
    assert payload["attempt"] == 1
    assert payload["compose_path"] == COMPOSE_PATH
    assert [item["name"] for item in payload["commands"]] == ["up", "ps", "logs"]
    assert payload["final"] == {
        "service_states": {"web": "running/healthy"},
        "verdict": "fail",
    }
    assert secret not in evidence_path.read_text(encoding="utf-8")
    assert '"env"' not in evidence_path.read_text(encoding="utf-8")


def test_private_boot_evidence_records_exception_class_before_reraising_provider_error(
    tmp_path: Path,
) -> None:
    provider = FakeSandboxProvider()
    evidence_path = tmp_path / "baseline-boot-attempt.json"

    async def exercise() -> None:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        with pytest.raises(KeyError):
            await _boot_compose_with_evidence(
                provider,
                sandbox_id,
                COMPOSE_PATH,
                {"APP_VALUE": "never-persist-this"},
                attempt=1,
                evidence_path=evidence_path,
            )

    asyncio.run(exercise())

    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["commands"] == [{"exception_type": "KeyError", "name": "up"}]
    assert "never-persist-this" not in evidence_path.read_text(encoding="utf-8")


def test_private_boot_logs_retain_stderr_error_after_saturated_stdout(
    tmp_path: Path,
) -> None:
    error = "literal-stderr-error-at-tail"
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=_healthy_ps()),
            LOGS_ARGV: _result(stdout="x" * 70_000, stderr=error),
        }
    )
    evidence_path = tmp_path / "baseline-boot-attempt.json"

    async def exercise() -> BootResult:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        return await _boot_compose_with_evidence(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {},
            attempt=1,
            evidence_path=evidence_path,
        )

    result = asyncio.run(exercise())

    assert error in result.logs["logs"]


def test_compose_up_uses_extended_timeout_without_changing_readiness_or_evidence_timeouts() -> (
    None
):
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(stdout="created"),
            PS_ARGV: _result(
                stdout=(
                    '{"Service":"web","State":"RUNNING","Health":"HEALTHY","ExitCode":0}\n'
                    '{"Service":"worker","State":"running","Health":null,"ExitCode":0}\n'
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
            '{"Service":"web","State":"RUNNING","Health":"HEALTHY","ExitCode":0}\n'
            '{"Service":"worker","State":"running","Health":null,"ExitCode":0}\n'
        ),
        "logs": "ready\nwarning",
    }
    assert provider.calls == [
        ("create", Path("missing-workspace"), "trial"),
        ("exec", "sandbox-1", UP_ARGV, 600),
        ("exec", "sandbox-1", PS_ARGV, 30),
        ("exec", "sandbox-1", LOGS_ARGV, 30),
    ]


def test_candidate_overlay_is_applied_after_base_for_every_compose_command() -> None:
    provider = FakeSandboxProvider(
        scripts={
            OVERLAY_UP_ARGV: _result(),
            OVERLAY_PS_ARGV: _result(stdout=_healthy_ps()),
            OVERLAY_LOGS_ARGV: _result(),
        }
    )

    async def exercise() -> BootResult:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        return await boot_compose(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {},
            attempt=2,
            overlay_path=OVERLAY_PATH,
        )

    result = asyncio.run(exercise())

    assert result.verdict is Verdict.PASS
    assert provider.calls[1:] == [
        ("exec", "sandbox-1", OVERLAY_UP_ARGV, 600),
        ("exec", "sandbox-1", OVERLAY_PS_ARGV, 30),
        ("exec", "sandbox-1", OVERLAY_LOGS_ARGV, 30),
    ]


@pytest.mark.parametrize("overlay_path", [cast(str, 7), "bad\0overlay.yml"])
def test_invalid_overlay_fails_before_any_provider_call(overlay_path: str) -> None:
    provider = FakeSandboxProvider()

    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            boot_compose(
                provider,
                "sandbox-not-needed",
                COMPOSE_PATH,
                {},
                attempt=1,
                overlay_path=overlay_path,
            )
        )

    assert provider.calls == []


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
        ("exec", "sandbox-1", up_argv, 600),
        ("exec", "sandbox-1", ps_argv, 30),
        ("exec", "sandbox-1", logs_argv, 30),
    ]


def test_startup_input_prefix_is_shared_by_all_boot_commands() -> None:
    prefix = (
        "env",
        "-u",
        "APP_MODE",
        "-u",
        "LD_HOST_PORT",
        "-u",
        "ld_preload",
        "APP_MODE=test",
    )
    compose = (
        "docker",
        "compose",
        "--project-directory",
        ".",
        "-f",
        COMPOSE_PATH,
    )
    up_argv = (*prefix, *compose, "up", "-d", "--wait", "--wait-timeout", "60")
    ps_argv = (*prefix, *compose, "ps", "--all", "--format", "json")
    logs_argv = (*prefix, *compose, "logs", "--no-color", "--tail", "200")
    provider = FakeSandboxProvider(
        scripts={
            up_argv: _result(),
            ps_argv: _result(stdout=_healthy_ps()),
            logs_argv: _result(),
        }
    )

    async def exercise() -> BootResult:
        sandbox_id = await provider.create(Path("missing-workspace"), "trial")
        return await boot_compose(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {"APP_MODE": "test"},
            attempt=1,
            unset_env_keys=("ld_preload", "APP_MODE", "LD_HOST_PORT"),
            project_directory=".",
        )

    result = asyncio.run(exercise())

    assert result.verdict is Verdict.PASS
    assert provider.calls[1:] == [
        ("exec", "sandbox-1", up_argv, 600),
        ("exec", "sandbox-1", ps_argv, 30),
        ("exec", "sandbox-1", logs_argv, 30),
    ]


def test_invalid_unset_key_fails_before_boot_exec() -> None:
    provider = FakeSandboxProvider()

    with pytest.raises(ValueError, match="unset environment key is not portable"):
        asyncio.run(
            boot_compose(
                provider,
                "sandbox-1",
                COMPOSE_PATH,
                {},
                attempt=1,
                unset_env_keys=("BAD-NAME",),
                project_directory=".",
            )
        )

    assert provider.calls == []


def test_startup_input_project_directory_is_fixed_to_clone_root() -> None:
    provider = FakeSandboxProvider()

    with pytest.raises(ValueError, match="project_directory must be the clone root"):
        asyncio.run(
            boot_compose(
                provider,
                "sandbox-1",
                COMPOSE_PATH,
                {},
                attempt=1,
                project_directory="/tmp",
            )
        )

    assert provider.calls == []


@pytest.mark.parametrize(
    ("row", "expected_state"),
    [
        (
            {"Service": "web", "State": "exited", "Health": None, "ExitCode": 0},
            "exited",
        ),
        (
            {
                "Service": "web",
                "State": "running",
                "Health": "unhealthy",
                "ExitCode": 0,
            },
            "running/unhealthy",
        ),
        (
            {
                "Service": "web",
                "State": "starting",
                "Health": "healthy",
                "ExitCode": 0,
            },
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
            json.dumps(
                {
                    "Service": "web",
                    "State": "running",
                    "Health": "unhealthy",
                    "ExitCode": 0,
                }
            ),
            json.dumps(
                {
                    "Service": "web",
                    "State": "running",
                    "Health": "healthy",
                    "ExitCode": 0,
                }
            ),
            json.dumps(
                {
                    "Service": "web",
                    "State": "running",
                    "Health": "healthy",
                    "ExitCode": 0,
                }
            ),
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
    ("up_code", "ps_code"),
    [(17, 0), (0, 18)],
)
def test_any_nonzero_command_fails_but_all_evidence_calls_still_run(
    up_code: int, ps_code: int
) -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(exit_code=up_code),
            PS_ARGV: _result(exit_code=ps_code, stdout=_healthy_ps()),
            LOGS_ARGV: _result(),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.FAIL
    assert result.service_states == {"web": "running/healthy"}
    assert provider.calls[1:] == [
        ("exec", "sandbox-1", UP_ARGV, 600),
        ("exec", "sandbox-1", PS_ARGV, 30),
        ("exec", "sandbox-1", LOGS_ARGV, 30),
    ]


@pytest.mark.parametrize(
    "row",
    [
        {"Service": "web", "State": "running", "Health": "healthy"},
        {
            "Service": "web",
            "State": "running",
            "Health": "healthy",
            "ExitCode": True,
        },
        {
            "Service": "web",
            "State": "running",
            "Health": "healthy",
            "ExitCode": "0",
        },
        {
            "Service": "web",
            "State": "running",
            "Health": "healthy",
            "ExitCode": -1,
        },
        {
            "Service": "web",
            "State": "running",
            "Health": "healthy",
            "ExitCode": 7,
        },
        {"Service": "web", "State": "exited", "Health": None, "ExitCode": 7},
    ],
)
def test_ps_requires_nonnegative_integer_exit_codes_for_every_service_record(
    row: dict[str, str | int | bool | None],
) -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=json.dumps(row)),
            LOGS_ARGV: _result(),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.FAIL


def test_logs_failure_is_auditable_but_does_not_override_valid_workload() -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=_healthy_ps()),
            LOGS_ARGV: _result(
                exit_code=23,
                stderr="TOKEN=logs-only-secret\\ncollector unavailable",
            ),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.PASS
    assert "exit_code=23" in result.logs["logs"]
    assert "logs-only-secret" not in result.logs["logs"]
    assert "[REDACTED]" in result.logs["logs"]


def test_logs_failure_context_keeps_persisted_evidence_within_the_log_budget() -> None:
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(),
            PS_ARGV: _result(stdout=_healthy_ps()),
            LOGS_ARGV: _result(
                exit_code=23, stderr="collector output\\n" + ("x" * 70_000)
            ),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.verdict is Verdict.PASS
    assert "exit_code=23" in result.logs["logs"]
    assert len(result.logs["logs"]) <= 65_536


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
        "level=INFO ToKeN=same-line-token-value-567\r\n"
        '{"user":"alice","PaSsWoRd":"same-line-json-value-890"}\r\n'
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
        assert "same-line-token-value-567" not in value
        assert "same-line-json-value-890" not in value
        assert "level=INFO ToKeN=[REDACTED]" in value
        assert '{"user":"alice","PaSsWoRd":[REDACTED]' in value
        assert supplied_secret not in value
        assert "[REDACTED]" in value
        assert "APP_REQUIRED_TOKEN is required" in value
        assert len(value) <= 65_536
        assert value.count("\n...[truncated]") == 1
        assert value.endswith("x" * 64)


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


def test_complete_sensitive_value_containing_marker_is_redacted_atomically() -> None:
    supplied_secret = "atomic-secret-prefix\n...[truncated]atomic-secret-suffix"
    prefix = ("env", f"APP_TOKEN={supplied_secret}")
    provider = FakeSandboxProvider(
        scripts={
            (*prefix, *UP_ARGV): _result(stdout=f"leaked={supplied_secret}\ntrailing"),
            (*prefix, *PS_ARGV): _result(stdout=_healthy_ps()),
            (*prefix, *LOGS_ARGV): _result(),
        }
    )

    result = _run_with_active_sandbox(
        provider,
        env={"APP_TOKEN": supplied_secret},
    )

    assert result.logs["up"] == "leaked=[REDACTED]\ntrailing"
    assert "atomic-secret-prefix" not in result.logs["up"]
    assert "atomic-secret-suffix" not in result.logs["up"]


def test_private_key_evidence_is_redacted_before_persistence_and_truncation() -> None:
    supplied_private_key = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\\n"
        "env-private-key-material\\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    boundary_crossing_pem = "-----BEGIN RSA PRIVATE KEY-----\\n" + ("A" * 70_000)
    evidence = (
        "ordinary diagnostic remains visible\\n"
        "SSH_PRIVATE_KEY=log-only-private-key-material\\n"
        f"leaked={supplied_private_key}\\n"
        f"{boundary_crossing_pem}"
    )
    prefix = ("env", f"SSH_PRIVATE_KEY={supplied_private_key}")
    provider = FakeSandboxProvider(
        scripts={
            (*prefix, *UP_ARGV): _result(stdout=evidence),
            (*prefix, *PS_ARGV): _result(stdout=_healthy_ps()),
            (*prefix, *LOGS_ARGV): _result(),
        }
    )

    result = _run_with_active_sandbox(
        provider, env={"SSH_PRIVATE_KEY": supplied_private_key}
    )

    persisted = result.logs["up"]
    assert result.verdict is Verdict.PASS
    assert "ordinary diagnostic remains visible" in persisted
    assert "env-private-key-material" not in persisted
    assert "log-only-private-key-material" not in persisted
    assert "BEGIN OPENSSH PRIVATE KEY" not in persisted
    assert "BEGIN RSA PRIVATE KEY" not in persisted
    assert "A" * 100 not in persisted
    assert "[REDACTED]" in persisted


def test_multiline_private_key_assignment_is_redacted_before_assignment_lines() -> None:
    private_key = (
        "PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n"
        "multiline-private-key-body\n"
        "-----END RSA PRIVATE KEY-----"
    )
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(stdout=f"ordinary before\n{private_key}\nordinary after"),
            PS_ARGV: _result(stdout=_healthy_ps()),
            LOGS_ARGV: _result(),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert (
        result.logs["up"] == "ordinary before\nPRIVATE_KEY=[REDACTED]\nordinary after"
    )
    assert "multiline-private-key-body" not in result.logs["up"]


def test_repeated_pem_headers_are_redacted_as_one_bounded_scan() -> None:
    repeated_headers = "\n".join(
        "-----BEGIN RSA PRIVATE KEY-----" for _ in range(1_900)
    )
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(stdout=f"ordinary before\n{repeated_headers}"),
            PS_ARGV: _result(stdout=_healthy_ps()),
            LOGS_ARGV: _result(),
        }
    )

    result = _run_with_active_sandbox(provider)

    assert result.logs["up"] == "ordinary before\n[REDACTED]"


def test_pem_crossing_stdout_stderr_and_log_limit_is_fully_redacted() -> None:
    stdout = "ordinary stdout\nPRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n" + (
        "A" * 65_400
    )
    stderr = ("B" * 1_000) + "\n-----END RSA PRIVATE KEY-----\nordinary stderr"
    provider = FakeSandboxProvider(
        scripts={
            UP_ARGV: _result(stdout=stdout, stderr=stderr),
            PS_ARGV: _result(stdout=_healthy_ps()),
            LOGS_ARGV: _result(),
        }
    )

    result = _run_with_active_sandbox(provider)

    persisted = result.logs["up"]
    assert len(persisted) <= 65_536
    assert persisted == "ordinary stdout\nPRIVATE_KEY=[REDACTED]\nordinary stderr"
    assert "BEGIN RSA PRIVATE KEY" not in persisted
    assert "END RSA PRIVATE KEY" not in persisted
    assert "A" * 100 not in persisted
    assert "B" * 100 not in persisted


def test_many_markers_collapse_to_deterministic_redacted_overflow() -> None:
    supplied_secret = "sensitive-" + ("x" * 128)
    marker = "\n...[truncated]"
    attacker_output = marker * 2_000
    prefix = ("env", f"APP_TOKEN={supplied_secret}")
    provider = FakeSandboxProvider(
        scripts={
            (*prefix, *UP_ARGV): _result(stdout=attacker_output),
            (*prefix, *PS_ARGV): _result(stdout=_healthy_ps()),
            (*prefix, *LOGS_ARGV): _result(),
        }
    )

    result = _run_with_active_sandbox(
        provider,
        env={"APP_TOKEN": supplied_secret},
    )

    assert result.logs["up"] == "[REDACTED: excessive truncation markers]"


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
        ("exec", "sandbox-1", UP_ARGV, 600),
    ]
