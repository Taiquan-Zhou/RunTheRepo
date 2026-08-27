import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from repotrial.domain.models import ObservationSnapshot
from repotrial.sandbox.base import ExecResult, NetworkLogResult, SandboxProvider

_COMMAND_TIMEOUT_SECONDS = 30
_MAX_CONTAINERS = 64
_MAX_COMMAND_STDOUT = 1_048_576
_MAX_TOTAL_STDOUT = 16_777_216
_MAX_DIFF_ROWS = 4_096
_MAX_PROCESS_ROWS = 4_096
_MAX_NETWORK_EVENTS = 4_096
_MAX_JSON_NODES = 100_000
_MAX_JSON_DEPTH = 64
_MAX_STRING_LENGTH = 65_536
_MAX_COMPOSE_PATH_LENGTH = 4_096
_MAX_SERVICE_LENGTH = 128
_MAX_ARTIFACT_BYTES = 24 * 1024 * 1024
_REDACTION = "[REDACTED]"
_CONTAINER_ID_PATTERN = re.compile(r"(?:[0-9a-f]{12}|[0-9a-f]{64})\Z")
_DIFF_OPERATIONS = {"A": "added", "C": "changed", "D": "deleted"}

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class ObservationCollectionError(RuntimeError):
    pass


class ObservationParseError(ObservationCollectionError):
    pass


@dataclass(slots=True)
class _CollectionBudget:
    stdout_characters: int = 0
    json_nodes: int = 0

    def accept_stdout(self, stdout: str) -> None:
        if len(stdout) > _MAX_COMMAND_STDOUT:
            raise ObservationParseError("collector stdout exceeds resource limit")
        self.stdout_characters += len(stdout)
        if self.stdout_characters > _MAX_TOTAL_STDOUT:
            raise ObservationParseError("collector stdout exceeds total resource limit")

    def copy_json(self, value: object) -> JsonValue:
        return self._copy_json(value, depth=1, active=set())

    def _copy_json(self, value: object, *, depth: int, active: set[int]) -> JsonValue:
        if depth > _MAX_JSON_DEPTH:
            raise ObservationParseError("JSON value exceeds depth limit")
        self.json_nodes += 1
        if self.json_nodes > _MAX_JSON_NODES:
            raise ObservationParseError("JSON value exceeds node limit")

        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ObservationParseError("JSON value contains non-finite number")
            return value
        if isinstance(value, str):
            _require_bounded_string(value, "JSON string")
            return value
        if type(value) is list:
            identity = id(value)
            if identity in active:
                raise ObservationParseError("JSON value contains a cycle")
            active.add(identity)
            try:
                return [
                    self._copy_json(item, depth=depth + 1, active=active)
                    for item in value
                ]
            finally:
                active.remove(identity)
        if type(value) is dict:
            identity = id(value)
            if identity in active:
                raise ObservationParseError("JSON value contains a cycle")
            active.add(identity)
            try:
                copied: dict[str, JsonValue] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ObservationParseError("JSON object key is not a string")
                    _require_bounded_string(key, "JSON object key")
                    copied[key] = self._copy_json(item, depth=depth + 1, active=active)
                return copied
            finally:
                active.remove(identity)
        raise ObservationParseError("unsupported JSON value")


async def collect_observation(
    provider: SandboxProvider,
    sandbox_id: str,
    compose_path: str,
    artifact_path: Path,
    *,
    overlay_path: str | None = None,
) -> ObservationSnapshot:
    _validate_inputs(sandbox_id, compose_path, artifact_path, overlay_path)
    budget = _CollectionBudget()
    discovery_argv = [
        "docker",
        "compose",
        "-f",
        compose_path,
    ]
    if overlay_path is not None:
        discovery_argv.extend(["-f", overlay_path])
    discovery_argv.extend(
        [
            "ps",
            "--all",
            "--no-trunc",
            "--orphans=false",
            "--format",
            "json",
        ]
    )
    discovery_result = await _exec_collector(
        provider, sandbox_id, discovery_argv, budget
    )
    containers = _parse_discovery(discovery_result.stdout, budget)

    inspect_by_service: dict[str, list[dict[str, object]]] = {}
    file_changes: list[dict[str, object]] = []
    process_events: list[dict[str, object]] = []
    audit_services: dict[str, list[dict[str, object]]] = {}

    for service, container_id in containers:
        inspect_argv = ["docker", "inspect", container_id]
        diff_argv = ["docker", "diff", container_id]
        top_argv = [
            "docker",
            "top",
            container_id,
            "-eo",
            "pid=,ppid=,user=,comm=",
        ]
        inspect_result = await _exec_collector(
            provider, sandbox_id, inspect_argv, budget
        )
        inspect_data = _parse_inspect(inspect_result.stdout, budget)
        diff_result = await _exec_collector(provider, sandbox_id, diff_argv, budget)
        parsed_diff = _parse_diff(diff_result.stdout, budget)
        top_result = await _exec_collector(provider, sandbox_id, top_argv, budget)
        parsed_top = _parse_top(top_result.stdout, budget)

        inspect_by_service.setdefault(service, []).append(
            {"container_id": container_id, "data": inspect_data}
        )
        file_changes.extend(
            {
                "service": service,
                "container_id": container_id,
                "operation": row["operation"],
                "path": row["path"],
            }
            for row in parsed_diff
        )
        process_events.extend(
            {
                "service": service,
                "container_id": container_id,
                "pid": row["pid"],
                "ppid": row["ppid"],
                "user": row["user"],
                "command": row["command"],
            }
            for row in parsed_top
        )
        audit_services.setdefault(service, []).append(
            {
                "container_id": container_id,
                "inspect": _command_audit(
                    inspect_argv, inspect_result.stdout, inspect_data
                ),
                "diff": _command_audit(diff_argv, diff_result.stdout, parsed_diff),
                "top": _command_audit(top_argv, top_result.stdout, parsed_top),
            }
        )

    network_result = await provider.network_log(sandbox_id)
    network_events, unsupported_collectors = _validated_network_result(
        network_result, budget
    )
    snapshot = ObservationSnapshot(
        inspect=inspect_by_service,
        file_changes=file_changes,
        process_events=process_events,
        network_events=network_events,
        unsupported_collectors=unsupported_collectors,
    )
    audit = {
        "schema_version": 1,
        "discovery": _command_audit(
            discovery_argv,
            discovery_result.stdout,
            [
                {"service": service, "container_id": container_id}
                for service, container_id in containers
            ],
        ),
        "services": audit_services,
        "network_runtime": {
            "supported": network_result.supported,
            "unsupported_reason": network_result.unsupported_reason,
            "parsed": network_events,
        },
        "snapshot": snapshot.model_dump(mode="json"),
    }
    _write_artifact(artifact_path, audit)
    return snapshot


def _validate_inputs(
    sandbox_id: str,
    compose_path: str,
    artifact_path: Path,
    overlay_path: str | None,
) -> None:
    if not isinstance(sandbox_id, str):
        raise TypeError("sandbox_id must be a string")
    if not sandbox_id:
        raise ValueError("sandbox_id must not be empty")
    _validate_compose_path(compose_path, "compose_path")
    if overlay_path is not None:
        _validate_compose_path(overlay_path, "overlay_path")
    if not isinstance(artifact_path, Path):
        raise TypeError("artifact_path must be a Path")
    if artifact_path.exists() or artifact_path.is_symlink():
        raise ObservationCollectionError("artifact target is already in use")
    if not artifact_path.parent.is_dir():
        raise ObservationCollectionError("artifact parent does not exist")


def _validate_compose_path(path: object, label: str) -> None:
    if not isinstance(path, str):
        raise TypeError(f"{label} must be a string")
    if not path:
        raise ValueError(f"{label} must not be empty")
    if len(path) > _MAX_COMPOSE_PATH_LENGTH:
        raise ValueError(f"{label} exceeds length limit")
    if _contains_unicode_category_c(path):
        raise ValueError(f"{label} contains a control character")


async def _exec_collector(
    provider: SandboxProvider,
    sandbox_id: str,
    argv: list[str],
    budget: _CollectionBudget,
) -> ExecResult:
    result = await provider.exec(
        sandbox_id,
        argv,
        timeout_s=_COMMAND_TIMEOUT_SECONDS,
    )
    if (
        not isinstance(result, ExecResult)
        or type(result.exit_code) is not int
        or not isinstance(result.stdout, str)
        or not isinstance(result.stderr, str)
    ):
        raise ObservationParseError("collector returned a malformed result")
    if result.exit_code != 0:
        raise ObservationCollectionError("collector command failed")
    budget.accept_stdout(result.stdout)
    return result


def _parse_discovery(stdout: str, budget: _CollectionBudget) -> list[tuple[str, str]]:
    containers: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for line in stdout.split("\n"):
        line = line.removesuffix("\r")
        if not line.strip():
            continue
        decoded = budget.copy_json(_decode_json(line))
        if not isinstance(decoded, dict):
            raise ObservationParseError("discovery row is not an object")
        service = decoded.get("Service")
        container_id = decoded.get("ID")
        if not isinstance(service, str) or not _valid_service(service):
            raise ObservationParseError("discovery row has an invalid service")
        if (
            not isinstance(container_id, str)
            or _CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        ):
            raise ObservationParseError("discovery row has an invalid container ID")
        if container_id in seen_ids:
            raise ObservationParseError("discovery contains a duplicate container")
        seen_ids.add(container_id)
        containers.append((service, container_id))
        if len(containers) > _MAX_CONTAINERS:
            raise ObservationParseError("discovery exceeds container limit")
    return sorted(containers)


def _parse_inspect(stdout: str, budget: _CollectionBudget) -> dict[str, JsonValue]:
    decoded = budget.copy_json(_decode_json(stdout))
    if not isinstance(decoded, list) or len(decoded) != 1:
        raise ObservationParseError("inspect output must contain one object")
    inspect_data = decoded[0]
    if not isinstance(inspect_data, dict):
        raise ObservationParseError("inspect output must contain one object")
    config = inspect_data.get("Config")
    if config is None:
        return inspect_data
    if not isinstance(config, dict):
        raise ObservationParseError("inspect Config is malformed")
    if "Env" not in config or config["Env"] is None:
        return inspect_data
    env = config["Env"]
    if not isinstance(env, list):
        raise ObservationParseError("inspect Config.Env is malformed")
    redacted_env: list[JsonValue] = []
    for entry in env:
        if not isinstance(entry, str) or "=" not in entry:
            raise ObservationParseError("inspect Config.Env entry is malformed")
        name, _ = entry.split("=", 1)
        if not name or _contains_unicode_category_c(name):
            raise ObservationParseError("inspect Config.Env entry is malformed")
        redacted_env.append(f"{name}={_REDACTION}")
    config["Env"] = redacted_env
    return inspect_data


def _parse_diff(stdout: str, budget: _CollectionBudget) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in _DIFF_OPERATIONS:
            raise ObservationParseError("diff output contains a malformed row")
        path = parts[1]
        if not path or not _valid_bounded_text(path):
            raise ObservationParseError("diff output contains an invalid path")
        copied = budget.copy_json(
            {"operation": _DIFF_OPERATIONS[parts[0]], "path": path}
        )
        rows.append(cast(dict[str, JsonValue], copied))
        if len(rows) > _MAX_DIFF_ROWS:
            raise ObservationParseError("diff output exceeds row limit")
    return rows


def _parse_top(stdout: str, budget: _CollectionBudget) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4 or not parts[0].isascii() or not parts[0].isdecimal():
            raise ObservationParseError("top output contains a malformed row")
        if not parts[1].isascii() or not parts[1].isdecimal():
            raise ObservationParseError("top output contains a malformed row")
        user, command = parts[2], parts[3]
        if not _valid_bounded_text(user) or not _valid_bounded_text(command):
            raise ObservationParseError("top output contains invalid text")
        try:
            process_ids = (int(parts[0]), int(parts[1]))
        except ValueError:
            process_ids = None
        if process_ids is None:
            raise ObservationParseError("top output contains a malformed row")
        pid, ppid = process_ids
        copied = budget.copy_json(
            {
                "pid": pid,
                "ppid": ppid,
                "user": user,
                "command": command,
            }
        )
        rows.append(cast(dict[str, JsonValue], copied))
        if len(rows) > _MAX_PROCESS_ROWS:
            raise ObservationParseError("top output exceeds row limit")
    return rows


def _validated_network_result(
    result: NetworkLogResult, budget: _CollectionBudget
) -> tuple[list[dict[str, JsonValue]], list[str]]:
    if not isinstance(result, NetworkLogResult):
        raise ObservationParseError("network collector returned a malformed result")
    supported = result.supported
    reason = result.unsupported_reason
    events = result.events
    if type(supported) is not bool or type(events) is not list:
        raise ObservationParseError("network collector returned a malformed result")
    if len(events) > _MAX_NETWORK_EVENTS:
        raise ObservationParseError("network events exceed event limit")
    if supported:
        if reason is not None:
            raise ObservationParseError("network collector result is contradictory")
        unsupported_collectors: list[str] = []
    else:
        if events or not isinstance(reason, str) or not reason:
            raise ObservationParseError("network collector result is contradictory")
        _require_bounded_string(reason, "network unsupported reason")
        unsupported_collectors = ["network_runtime"]

    copied = budget.copy_json(events)
    if not isinstance(copied, list) or any(
        not isinstance(event, dict) for event in copied
    ):
        raise ObservationParseError("network event is not an object")
    return cast(list[dict[str, JsonValue]], copied), unsupported_collectors


def _command_audit(argv: list[str], stdout: str, parsed: object) -> dict[str, object]:
    try:
        encoded_stdout = stdout.encode("utf-8")
    except UnicodeEncodeError:
        encoded_stdout = None
    if encoded_stdout is None:
        raise ObservationParseError("collector stdout is not valid UTF-8 text")
    digest = hashlib.sha256(encoded_stdout).hexdigest()
    return {"argv": list(argv), "stdout_sha256": digest, "parsed": parsed}


def _write_artifact(artifact_path: Path, audit: dict[str, object]) -> None:
    try:
        serialized = (
            json.dumps(
                audit,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ObservationParseError("audit data is not serializable") from error
    if len(serialized) > _MAX_ARTIFACT_BYTES:
        raise ObservationCollectionError("audit artifact exceeds size limit")
    try:
        with artifact_path.open("xb") as artifact_file:
            artifact_file.write(serialized)
    except FileExistsError as error:
        raise ObservationCollectionError("artifact target is already in use") from error
    except OSError as error:
        raise ObservationCollectionError(
            "audit artifact could not be written"
        ) from error


def _decode_json(text: str) -> object:
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (ValueError, RecursionError):
        decoded = None
        valid = False
    else:
        valid = True
    if not valid:
        raise ObservationParseError("collector returned malformed JSON")
    return decoded


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


def _valid_service(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_SERVICE_LENGTH
        and bool(value.strip())
        and not _contains_unicode_category_c(value)
    )


def _valid_bounded_text(value: str) -> bool:
    return 0 < len(value) <= _MAX_STRING_LENGTH and not _contains_unicode_category_c(
        value
    )


def _require_bounded_string(value: str, label: str) -> None:
    if len(value) > _MAX_STRING_LENGTH:
        raise ObservationParseError(f"{label} exceeds string limit")


def _contains_unicode_category_c(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)
