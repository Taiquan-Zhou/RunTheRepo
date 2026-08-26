import json
import re

from pydantic import BaseModel

from repotrial.domain.enums import Verdict
from repotrial.sandbox.base import ExecResult, SandboxProvider

_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CONTROL_ENV_KEYS = {
    "HOME",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "XDG_CONFIG_HOME",
}
_CONTROL_ENV_PREFIXES = ("COMPOSE_", "DOCKER_", "DYLD_", "LD_")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9_-])(?P<quote>[\"']?)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P=quote)[ \t]*[:=][ \t]*)"
    r"[^\r\n]*"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer[ \t]+[^\s,;]+")
_REDACTION = "[REDACTED]"
_LOG_LIMIT = 65_536
_TRUNCATION_MARKER = "\n...[truncated]"


class BootResult(BaseModel):
    verdict: Verdict
    service_states: dict[str, str]
    logs: dict[str, str]
    attempt: int


async def boot_compose(
    provider: SandboxProvider,
    sandbox_id: str,
    compose_path: str,
    env: dict[str, str],
    attempt: int,
) -> BootResult:
    prefix = _validated_env_prefix(compose_path, env)
    docker_compose = ["docker", "compose", "-f", compose_path]

    up = await provider.exec(
        sandbox_id,
        [*prefix, *docker_compose, "up", "-d"],
        timeout_s=120,
    )
    ps = await provider.exec(
        sandbox_id,
        [*prefix, *docker_compose, "ps", "--all", "--format", "json"],
        timeout_s=30,
    )
    logs = await provider.exec(
        sandbox_id,
        [*prefix, *docker_compose, "logs", "--no-color", "--tail", "200"],
        timeout_s=30,
    )

    service_states, all_services_ready = _parse_service_states(ps.stdout)
    all_commands_succeeded = all(result.exit_code == 0 for result in (up, ps, logs))
    verdict = (
        Verdict.PASS if all_commands_succeeded and all_services_ready else Verdict.FAIL
    )
    sensitive_values = _sensitive_env_values(env)

    return BootResult(
        verdict=verdict,
        service_states=service_states,
        logs={
            "up": _sanitize_result(up, sensitive_values),
            "ps": _sanitize_result(ps, sensitive_values),
            "logs": _sanitize_result(logs, sensitive_values),
        },
        attempt=attempt,
    )


def _validated_env_prefix(compose_path: str, env: dict[str, str]) -> list[str]:
    if not isinstance(compose_path, str):
        raise TypeError("compose_path must be a string")
    if "\0" in compose_path:
        raise ValueError("compose_path must not contain NUL")

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
        if not {"Service", "State", "Health"}.issubset(decoded):
            valid = False
            continue

        service = decoded["Service"]
        state = decoded["State"]
        health = decoded["Health"]
        if (
            not isinstance(service, str)
            or not service.strip()
            or not isinstance(state, str)
            or not (isinstance(health, str) or health is None)
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
        if normalized_state != "running" or normalized_health not in {
            "",
            "healthy",
        }:
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
    return any(part in {"token", "password", "secret"} for part in parts) or any(
        parts[index : index + 2] == ["api", "key"] for index in range(len(parts) - 1)
    )


def _sanitize_result(result: ExecResult, sensitive_values: tuple[str, ...]) -> str:
    combined = _combine_output(result.stdout, result.stderr)
    combined = _redact_truncated_sensitive_prefixes(combined, sensitive_values)
    for value in sensitive_values:
        combined = combined.replace(value, _REDACTION)
    combined = _BEARER_PATTERN.sub(lambda match: f"Bearer {_REDACTION}", combined)
    combined = _ASSIGNMENT_PATTERN.sub(_redact_sensitive_assignment, combined)
    if len(combined) <= _LOG_LIMIT:
        return combined
    retained = _LOG_LIMIT - len(_TRUNCATION_MARKER)
    return f"{combined[:retained]}{_TRUNCATION_MARKER}"


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_env_key(match.group("name")):
        return match.group(0)
    return f"{match.group('prefix')}{_REDACTION}"


def _redact_truncated_sensitive_prefixes(
    text: str, sensitive_values: tuple[str, ...]
) -> str:
    marker_indexes = [
        match.start() for match in re.finditer(re.escape(_TRUNCATION_MARKER), text)
    ]
    for marker_index in reversed(marker_indexes):
        preceding = text[:marker_index]
        prefix_length = max(
            (_longest_prefix_at_end(value, preceding) for value in sensitive_values),
            default=0,
        )
        if prefix_length:
            start = marker_index - prefix_length
            text = f"{text[:start]}{_REDACTION}{text[marker_index:]}"
    return text


def _longest_prefix_at_end(value: str, text: str) -> int:
    suffix = text[-len(value) :]
    separator = "\0"
    while separator in suffix:
        separator += "\0"
    sequence = f"{value}{separator}{suffix}"
    prefix_lengths = [0] * len(sequence)
    for index in range(1, len(sequence)):
        candidate = prefix_lengths[index - 1]
        while candidate and sequence[index] != sequence[candidate]:
            candidate = prefix_lengths[candidate - 1]
        if sequence[index] == sequence[candidate]:
            candidate += 1
        prefix_lengths[index] = candidate
    return min(prefix_lengths[-1], len(value))


def _combine_output(stdout: str, stderr: str) -> str:
    if not stdout:
        return stderr
    if not stderr:
        return stdout
    separator = "" if stdout.endswith("\n") else "\n"
    return f"{stdout}{separator}{stderr}"
