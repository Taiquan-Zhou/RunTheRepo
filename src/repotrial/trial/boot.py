import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from repotrial.domain.enums import Verdict
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.trial.boot_evidence import _BootEvidenceSession
from repotrial.trial.image_template import (
    ImageTemplateError,
    validate_runtime_compose_mapping,
    verify_compose_image_identity,
)

_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CONTROL_ENV_KEYS = {
    "HOME",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "XDG_CONFIG_HOME",
}
_CONTROL_ENV_PREFIXES = ("COMPOSE_", "DOCKER_", "DYLD_", "LD_")
_ASSIGNMENT_START_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_-])(?P<quote>[\"']?)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P=quote)[ \t]*[:=][ \t]*)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer[ \t]+[^\s,;]+")
_REDACTION = "[REDACTED]"
_MARKER_OVERFLOW_REDACTION = "[REDACTED: excessive truncation markers]"
_LOG_LIMIT = 65_536
_COMPOSE_UP_TIMEOUT_S = 600
_COMPOSE_READINESS_RECHECK_TIMEOUT_S = 120
_COMPOSE_CONFIG_TIMEOUT_S = 30
_READINESS_ERROR_PATTERN = re.compile(
    r"(?:dependency failed to start: )?container [A-Za-z0-9_.-]+ is unhealthy\Z"
)
_MAX_DECLARED_SECRET_KEYS = 32
_MAX_DECLARED_SECRET_KEY_LENGTH = 128
_SYNTHETIC_VALUE = "repotrial-synthetic-value"
_TRUNCATION_MARKER = "\n...[truncated]"
_PEM_BEGIN = "-----BEGIN "
_PEM_END = "-----END "
_PEM_TERMINATOR = "-----"
_MAX_PEM_LABEL_LENGTH = 64


class BootResult(BaseModel):
    verdict: Verdict
    service_states: dict[str, str]
    logs: dict[str, str]
    attempt: int
    recovery_env: dict[str, str] = Field(default_factory=dict)


async def boot_compose(
    provider: SandboxProvider,
    sandbox_id: str,
    compose_path: str,
    env: dict[str, str],
    attempt: int,
    *,
    overlay_path: str | None = None,
    compatibility_overlay_path: str | None = None,
    unset_env_keys: Sequence[str] = (),
    project_directory: str | None = None,
    declared_secret_env_keys: Collection[str] = (),
) -> BootResult:
    return await _boot_compose_with_evidence(
        provider,
        sandbox_id,
        compose_path,
        env,
        attempt,
        overlay_path=overlay_path,
        compatibility_overlay_path=compatibility_overlay_path,
        unset_env_keys=unset_env_keys,
        project_directory=project_directory,
        declared_secret_env_keys=declared_secret_env_keys,
    )


async def _boot_compose_with_evidence(
    provider: SandboxProvider,
    sandbox_id: str,
    compose_path: str,
    env: dict[str, str],
    attempt: int,
    *,
    evidence_path: Path | None = None,
    overlay_path: str | None = None,
    compatibility_overlay_path: str | None = None,
    unset_env_keys: Sequence[str] = (),
    project_directory: str | None = None,
    declared_secret_env_keys: Collection[str] = (),
) -> BootResult:
    effective_env = dict(env)
    declared_secret_keys = _bounded_declared_secret_keys(declared_secret_env_keys)
    runtime_plan = provider.runtime_image_plan()
    runtime_overlay_path = await provider.prepare_runtime_image_bindings(sandbox_id)
    if runtime_plan is not None and runtime_overlay_path is None:
        raise ImageTemplateError("runtime_image_overlay_missing")
    docker_compose = ["docker", "compose"]
    if project_directory is not None:
        _validate_project_directory(project_directory)
        docker_compose.extend(["--project-directory", project_directory])
    docker_compose.extend(["-f", compose_path])
    if compatibility_overlay_path is not None:
        _validate_compose_path(compatibility_overlay_path, "compatibility_overlay_path")
        docker_compose.extend(["-f", compatibility_overlay_path])
    if overlay_path is not None:
        _validate_compose_path(overlay_path, "overlay_path")
        docker_compose.extend(["-f", overlay_path])
    if runtime_overlay_path is not None:
        _validate_compose_path(runtime_overlay_path, "runtime_overlay_path")
        docker_compose.extend(["-f", runtime_overlay_path])
    redaction_env = dict(effective_env)
    redaction_env.update(
        {
            key: _SYNTHETIC_VALUE
            for key in declared_secret_keys
            if key not in redaction_env
        }
    )
    evidence = (
        None
        if evidence_path is None
        else _BootEvidenceSession(
            evidence_path,
            redaction_env,
            attempt=attempt,
            compose_path=compose_path,
        )
    )

    try:
        recovered_env, config_result = await _preflight_compose(
            provider,
            sandbox_id,
            docker_compose,
            compose_path,
            effective_env,
            declared_secret_keys,
            unset_env_keys,
        )
    except BaseException as error:
        if evidence is not None:
            evidence.record_exception("config", error)
        raise
    if evidence is not None and config_result is not None:
        evidence.record_command("config", config_result)

    if runtime_plan is not None:
        runtime_config = await provider.exec(
            sandbox_id,
            [
                *_validated_compose_env_prefix(
                    compose_path, effective_env, unset_env_keys
                ),
                *docker_compose,
                "config",
                "--format",
                "json",
            ],
            timeout_s=_COMPOSE_CONFIG_TIMEOUT_S,
        )
        if not isinstance(runtime_config, ExecResult) or runtime_config.exit_code != 0:
            raise ImageTemplateError("runtime_compose_mapping_invalid")
        validate_runtime_compose_mapping(runtime_config.stdout, runtime_plan)

    prefix = _validated_compose_env_prefix(
        compose_path,
        effective_env,
        unset_env_keys,
    )
    expected_image_identity = provider.expected_image_identity_sha256()
    template_active = expected_image_identity is not None
    if expected_image_identity is not None:
        try:
            await verify_compose_image_identity(
                provider,
                sandbox_id,
                expected_image_identity,
                compose_path=compose_path,
                env=effective_env,
                unset_env_keys=unset_env_keys,
            )
        except BaseException as error:
            if evidence is not None:
                evidence.record_exception("up", error)
            raise
    try:
        up_argv = [
            *prefix,
            *docker_compose,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "60",
        ]
        if template_active:
            up_argv.extend(["--pull", "never", "--no-build"])
        up = await provider.exec(
            sandbox_id,
            up_argv,
            timeout_s=_COMPOSE_UP_TIMEOUT_S,
        )
    except BaseException as error:
        if evidence is not None:
            evidence.record_exception("up", error)
        raise
    if not isinstance(up, ExecResult):
        error = TypeError("boot command returned a malformed result")
        if evidence is not None:
            evidence.record_exception("up", error)
        raise error
    try:
        ps = await provider.exec(
            sandbox_id,
            [*prefix, *docker_compose, "ps", "--all", "--format", "json"],
            timeout_s=30,
        )
    except BaseException as error:
        if evidence is not None:
            evidence.record_command("up", up)
            evidence.record_exception("ps", error)
        raise
    if not isinstance(ps, ExecResult):
        error = TypeError("boot command returned a malformed result")
        if evidence is not None:
            evidence.record_command("up", up)
            evidence.record_exception("ps", error)
        raise error

    initial_snapshot = _parse_readiness_snapshot(ps.stdout)
    recheck = _should_recheck_readiness(up, ps, initial_snapshot)
    final_up = up
    final_ps = ps
    if recheck:
        if evidence is not None:
            evidence.record_command("up_initial", up)
            evidence.record_command("ps_initial", ps)
        try:
            recheck_up_argv = [
                *prefix,
                *docker_compose,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "60",
            ]
            if template_active:
                recheck_up_argv.extend(["--pull", "never", "--no-build"])
            else:
                recheck_up_argv.append("--no-build")
            recheck_up_argv.append("--no-recreate")
            recheck_up = await provider.exec(
                sandbox_id,
                recheck_up_argv,
                timeout_s=_COMPOSE_READINESS_RECHECK_TIMEOUT_S,
            )
        except BaseException as error:
            if evidence is not None:
                evidence.record_exception("up_recheck", error)
            raise
        if not isinstance(recheck_up, ExecResult):
            error = TypeError("boot command returned a malformed result")
            if evidence is not None:
                evidence.record_exception("up_recheck", error)
            raise error
        if evidence is not None:
            evidence.record_command("up_recheck", recheck_up)
        try:
            recheck_ps = await provider.exec(
                sandbox_id,
                [*prefix, *docker_compose, "ps", "--all", "--format", "json"],
                timeout_s=30,
            )
        except BaseException as error:
            if evidence is not None:
                evidence.record_exception("ps_final", error)
            raise
        if not isinstance(recheck_ps, ExecResult):
            error = TypeError("boot command returned a malformed result")
            if evidence is not None:
                evidence.record_exception("ps_final", error)
            raise error
        final_up = recheck_up
        final_ps = recheck_ps
        if evidence is not None:
            evidence.record_command("ps_final", recheck_ps)
    elif evidence is not None:
        evidence.record_command("up", up)
        evidence.record_command("ps", ps)

    try:
        logs = await provider.exec(
            sandbox_id,
            [*prefix, *docker_compose, "logs", "--no-color", "--tail", "200"],
            timeout_s=30,
        )
    except BaseException as error:
        if evidence is not None:
            evidence.record_exception("logs", error)
        raise
    if not isinstance(logs, ExecResult):
        error = TypeError("boot command returned a malformed result")
        if evidence is not None:
            evidence.record_exception("logs", error)
        raise error
    if evidence is not None:
        evidence.record_command("logs", logs)

    service_states, all_services_ready = _parse_service_states(final_ps.stdout)
    final_snapshot = _parse_readiness_snapshot(final_ps.stdout) if recheck else None
    final_ids_match = not recheck or (
        initial_snapshot is not None
        and final_snapshot is not None
        and initial_snapshot.container_ids == final_snapshot.container_ids
    )
    if recheck:
        all_services_ready = (
            final_snapshot is not None and final_snapshot.all_ready and final_ids_match
        )
    workload_commands_succeeded = final_up.exit_code == 0 and final_ps.exit_code == 0
    verdict = (
        Verdict.PASS
        if workload_commands_succeeded and all_services_ready
        else Verdict.FAIL
    )
    sensitive_values = _sensitive_env_values(effective_env)
    if _SYNTHETIC_VALUE in effective_env.values():
        sensitive_values = tuple(
            sorted(
                {*sensitive_values, _SYNTHETIC_VALUE},
                key=lambda value: (-len(value), value),
            )
        )
    if evidence is not None:
        evidence.finalize(verdict, service_states)

    return BootResult(
        verdict=verdict,
        service_states=service_states,
        logs={
            "up": _sanitize_result(final_up, sensitive_values),
            "ps": _sanitize_result(final_ps, sensitive_values),
            "logs": _sanitize_logs_result(logs, sensitive_values),
        },
        attempt=attempt,
        recovery_env=recovered_env,
    )


async def _preflight_compose(
    provider: SandboxProvider,
    sandbox_id: str,
    docker_compose: list[str],
    compose_path: str,
    env: dict[str, str],
    declared_secret_keys: frozenset[str],
    unset_env_keys: Sequence[str],
) -> tuple[dict[str, str], ExecResult | None]:
    """Resolve only declared Compose secret inputs in the active sandbox."""
    recovered: dict[str, str] = {}
    if not declared_secret_keys:
        return recovered, None
    for key in sorted(declared_secret_keys):
        if key in env or not _is_safe_preflight_env_key(key):
            continue
        env[key] = _SYNTHETIC_VALUE
        recovered[key] = _SYNTHETIC_VALUE

    prefix = _validated_compose_env_prefix(compose_path, env, unset_env_keys)
    config = await provider.exec(
        sandbox_id,
        [*prefix, *docker_compose, "config", "--quiet"],
        timeout_s=_COMPOSE_CONFIG_TIMEOUT_S,
    )
    if not isinstance(config, ExecResult):
        raise TypeError("boot command returned a malformed result")
    return recovered, config


def _bounded_declared_secret_keys(
    keys: Collection[str],
) -> frozenset[str]:
    if isinstance(keys, (str, bytes, bytearray)):
        return frozenset()
    try:
        values = list(keys)
    except TypeError:
        return frozenset()
    if len(values) > _MAX_DECLARED_SECRET_KEYS:
        return frozenset()
    if any(
        not isinstance(key, str)
        or len(key) > _MAX_DECLARED_SECRET_KEY_LENGTH
        or not _is_safe_preflight_env_key(key)
        for key in values
    ):
        return frozenset()
    return frozenset(values)


def _is_safe_preflight_env_key(key: str) -> bool:
    normalized_key = key.upper()
    return (
        _ENV_KEY_PATTERN.fullmatch(key) is not None
        and normalized_key not in _CONTROL_ENV_KEYS
        and not normalized_key.startswith(_CONTROL_ENV_PREFIXES)
    )


@dataclass(frozen=True)
class _ReadinessSnapshot:
    container_ids: frozenset[str]
    all_running: bool
    all_ready: bool
    has_healthy_or_starting: bool


def _should_recheck_readiness(
    up: ExecResult,
    ps: ExecResult,
    snapshot: _ReadinessSnapshot | None,
) -> bool:
    return (
        up.exit_code == 1
        and _is_readiness_error(up.stderr)
        and ps.exit_code == 0
        and snapshot is not None
        and snapshot.all_running
        and snapshot.has_healthy_or_starting
    )


def _is_readiness_error(stderr: str) -> bool:
    if not isinstance(stderr, str) or len(stderr) > _LOG_LIMIT:
        return False
    nonempty_lines = [line for line in stderr.splitlines() if line.strip()]
    if not nonempty_lines:
        return False
    message = nonempty_lines[-1]
    return message == "timeout waiting for dependencies" or (
        _READINESS_ERROR_PATTERN.fullmatch(message) is not None
    )


def _parse_readiness_snapshot(output: str) -> _ReadinessSnapshot | None:
    if not isinstance(output, str) or len(output) > _LOG_LIMIT:
        return None
    rows = [line for line in output.splitlines() if line.strip()]
    if not rows:
        return None

    container_ids: set[str] = set()
    all_running = True
    all_ready = True
    has_healthy_or_starting = False
    for line in rows:
        try:
            decoded: object = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
            )
        except (ValueError, RecursionError):
            return None
        if not isinstance(decoded, dict) or not {
            "Service",
            "State",
            "Health",
            "ExitCode",
            "ID",
        }.issubset(decoded):
            return None
        service = decoded["Service"]
        state = decoded["State"]
        health = decoded["Health"]
        exit_code = decoded["ExitCode"]
        container_id = decoded["ID"]
        if (
            not isinstance(service, str)
            or not service.strip()
            or not isinstance(state, str)
            or not (isinstance(health, str) or health is None)
            or type(exit_code) is not int
            or exit_code < 0
            or not isinstance(container_id, str)
            or not container_id.strip()
            or container_id in container_ids
        ):
            return None
        container_ids.add(container_id)
        normalized_state = state.lower()
        normalized_health = health.lower() if health is not None else ""
        if normalized_state != "running" or exit_code != 0:
            all_running = False
            all_ready = False
        if normalized_health not in {"", "healthy", "starting"}:
            return None
        if normalized_health in {"healthy", "starting"}:
            has_healthy_or_starting = True
        if normalized_health not in {"", "healthy"}:
            all_ready = False

    return _ReadinessSnapshot(
        container_ids=frozenset(container_ids),
        all_running=all_running,
        all_ready=all_ready and has_healthy_or_starting,
        has_healthy_or_starting=has_healthy_or_starting,
    )


def _validated_env_prefix(compose_path: str, env: dict[str, str]) -> list[str]:
    _validate_compose_path(compose_path, "compose_path")

    assignments: list[str] = []
    for key, value in env.items():
        if not isinstance(key, str):
            raise TypeError("environment keys must be strings")
        if "\0" in key:
            raise ValueError("environment keys must not contain NUL")
        if _ENV_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("environment key is not portable")
        normalized_key = key.upper()
        if normalized_key in _CONTROL_ENV_KEYS or normalized_key.startswith(
            _CONTROL_ENV_PREFIXES
        ):
            raise ValueError("environment key controls the Compose toolchain")
        if not isinstance(value, str):
            raise TypeError("environment values must be strings")
        if "\0" in value:
            raise ValueError("environment values must not contain NUL")
        assignments.append(f"{key}={value}")

    if not assignments:
        return []
    return ["env", *sorted(assignments)]


def _validated_compose_env_prefix(
    compose_path: str,
    env: Mapping[str, str],
    unset_env_keys: Sequence[str] = (),
) -> list[str]:
    """Build one deterministic Compose process environment prefix."""
    existing = _validated_env_prefix(compose_path, dict(env))
    validated_keys: list[str] = []
    for key in unset_env_keys:
        if not isinstance(key, str):
            raise TypeError("unset environment keys must be strings")
        if _ENV_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("unset environment key is not portable")
        validated_keys.append(key)
    unset: list[str] = []
    for key in sorted(set(validated_keys)):
        unset.extend(["-u", key])
    if not unset:
        return existing
    assignments = existing[1:] if existing else []
    return ["env", *unset, *assignments]


def _validate_compose_path(path: object, label: str) -> None:
    if not isinstance(path, str):
        raise TypeError(f"{label} must be a string")
    if "\0" in path:
        raise ValueError(f"{label} must not contain NUL")


def _validate_project_directory(project_directory: object) -> None:
    if not isinstance(project_directory, str):
        raise TypeError("project_directory must be a string")
    if project_directory != ".":
        raise ValueError("project_directory must be the clone root")


def _parse_service_states(output: str) -> tuple[dict[str, str], bool]:
    if len(output) > _LOG_LIMIT:
        return {}, False

    labels_by_service: dict[str, set[str]] = {}
    valid = True
    row_count = 0
    ready = True

    for line in output.splitlines():
        if not line.strip():
            continue
        row_count += 1
        try:
            decoded: object = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
            )
        except (ValueError, RecursionError):
            valid = False
            continue
        if not isinstance(decoded, dict):
            valid = False
            continue
        if not {"Service", "State", "Health", "ExitCode"}.issubset(decoded):
            valid = False
            continue

        service = decoded["Service"]
        state = decoded["State"]
        health = decoded["Health"]
        exit_code = decoded["ExitCode"]
        if (
            not isinstance(service, str)
            or not service.strip()
            or not isinstance(state, str)
            or not (isinstance(health, str) or health is None)
            or type(exit_code) is not int
            or exit_code < 0
        ):
            valid = False
            continue

        service = service.strip()
        normalized_state = state.lower()
        normalized_health = health.lower() if health is not None else ""
        label = normalized_state or "<empty>"
        if normalized_health:
            label = f"{label}/{normalized_health}"
        labels_by_service.setdefault(service, set()).add(label)
        if (
            normalized_state != "running"
            or normalized_health not in {"", "healthy"}
            or exit_code != 0
        ):
            ready = False

    service_states = {
        service: ", ".join(sorted(labels))
        for service, labels in sorted(labels_by_service.items())
    }
    all_services_ready = valid and row_count > 0 and ready
    return service_states, all_services_ready


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError("duplicate JSON key")
        decoded[key] = value
    return decoded


def _reject_nonstandard_constant(_: str) -> object:
    raise ValueError("non-standard JSON constant")


def _sensitive_env_values(env: dict[str, str]) -> tuple[str, ...]:
    values = {
        value for key, value in env.items() if value and _is_sensitive_env_key(key)
    }
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _is_sensitive_env_key(key: str) -> bool:
    parts = re.split(r"[_-]+", key.lower())
    return (
        any(part in {"token", "password", "secret"} for part in parts)
        or any(
            parts[index : index + 2] == ["api", "key"]
            for index in range(len(parts) - 1)
        )
        or "private" in parts
        and "key" in parts
    )


def _sanitize_result(result: ExecResult, sensitive_values: tuple[str, ...]) -> str:
    combined = _combine_output(result.stdout, result.stderr)
    for value in sensitive_values:
        combined = combined.replace(value, _REDACTION)
    if combined.count(_TRUNCATION_MARKER) > 2:
        return _MARKER_OVERFLOW_REDACTION
    combined = _redact_truncated_sensitive_prefixes(combined, sensitive_values)
    combined = _BEARER_PATTERN.sub(lambda match: f"Bearer {_REDACTION}", combined)
    combined = _redact_pem_private_keys(combined)
    combined = _redact_sensitive_assignments(combined)
    return _bound_raw_evidence(combined)


def _sanitize_logs_result(result: ExecResult, sensitive_values: tuple[str, ...]) -> str:
    sanitized = _sanitize_result(result, sensitive_values)
    if result.exit_code == 0:
        return sanitized
    context = f"[logs command failed: exit_code={result.exit_code}]"
    if not sanitized:
        return context
    evidence_limit = _LOG_LIMIT - len(context) - 1
    sanitized = _bound_raw_evidence(sanitized, evidence_limit)
    return f"{context}\n{sanitized}"


def _bound_raw_evidence(text: str, limit: int = _LOG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    payload_limit = limit - len(_TRUNCATION_MARKER)
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    return f"{text[:head_limit]}{_TRUNCATION_MARKER}{text[-tail_limit:]}"


def _redact_sensitive_assignments(text: str) -> str:
    redacted_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        body, ending = _split_line_ending(line)
        for match in _ASSIGNMENT_START_PATTERN.finditer(body):
            if _is_sensitive_env_key(match.group("name")):
                body = f"{body[: match.end()]}{_REDACTION}"
                break
        redacted_lines.append(f"{body}{ending}")
    return "".join(redacted_lines)


def _redact_pem_private_keys(text: str) -> str:
    redacted_parts: list[str] = []
    retained_start = 0
    scan_start = 0
    while True:
        header_start = text.find(_PEM_BEGIN, scan_start)
        if header_start < 0:
            redacted_parts.append(text[retained_start:])
            return "".join(redacted_parts)
        header_end = _private_key_marker_end(text, header_start, _PEM_BEGIN)
        if header_end is None:
            scan_start = header_start + len(_PEM_BEGIN)
            continue
        footer_end = _find_private_key_footer(text, header_end)
        redacted_parts.append(text[retained_start:header_start])
        redacted_parts.append(_REDACTION)
        if footer_end is None:
            return "".join(redacted_parts)
        retained_start = footer_end
        scan_start = footer_end


def _find_private_key_footer(text: str, start: int) -> int | None:
    scan_start = start
    while True:
        footer_start = text.find(_PEM_END, scan_start)
        if footer_start < 0:
            return None
        footer_end = _private_key_marker_end(text, footer_start, _PEM_END)
        if footer_end is not None:
            return footer_end
        scan_start = footer_start + len(_PEM_END)


def _private_key_marker_end(text: str, start: int, prefix: str) -> int | None:
    label_start = start + len(prefix)
    terminator_start = text.find(
        _PEM_TERMINATOR,
        label_start,
        label_start + _MAX_PEM_LABEL_LENGTH + len(_PEM_TERMINATOR),
    )
    if terminator_start < 0:
        return None
    label = text[label_start:terminator_start]
    if (
        not label
        or len(label) > _MAX_PEM_LABEL_LENGTH
        or not (label == "PRIVATE KEY" or label.endswith(" PRIVATE KEY"))
        or any(
            not (character.isupper() or character.isdigit() or character == " ")
            for character in label
        )
    ):
        return None
    return terminator_start + len(_PEM_TERMINATOR)


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\r", "\n")):
        return line[:-1], line[-1]
    return line, ""


def _redact_truncated_sensitive_prefixes(
    text: str, sensitive_values: tuple[str, ...]
) -> str:
    marker_indexes = [
        match.start() for match in re.finditer(re.escape(_TRUNCATION_MARKER), text)
    ]
    max_sensitive_length = max(map(len, sensitive_values), default=0)
    for marker_index in reversed(marker_indexes):
        window_start = max(0, marker_index - max_sensitive_length)
        preceding = text[window_start:marker_index]
        prefix_length = max(
            (_longest_prefix_at_end(value, preceding) for value in sensitive_values),
            default=0,
        )
        if prefix_length:
            start = marker_index - prefix_length
            text = f"{text[:start]}{_REDACTION}{text[marker_index:]}"
    return text


def _longest_prefix_at_end(value: str, text: str) -> int:
    maximum_length = min(len(value), len(text))
    value_prefix = value[:maximum_length]
    suffix = text[-maximum_length:]
    separator = "\0"
    while separator in suffix:
        separator += "\0"
    sequence = f"{value_prefix}{separator}{suffix}"
    prefix_lengths = [0] * len(sequence)
    for index in range(1, len(sequence)):
        candidate = prefix_lengths[index - 1]
        while candidate and sequence[index] != sequence[candidate]:
            candidate = prefix_lengths[candidate - 1]
        if sequence[index] == sequence[candidate]:
            candidate += 1
        prefix_lengths[index] = candidate
    return min(prefix_lengths[-1], maximum_length)


def _combine_output(stdout: str, stderr: str) -> str:
    if not stdout:
        return stderr
    if not stderr:
        return stdout
    separator = "" if stdout.endswith("\n") else "\n"
    return f"{stdout}{separator}{stderr}"
