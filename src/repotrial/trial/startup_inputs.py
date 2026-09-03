"""Deterministic planning for the narrowly supported repository startup input."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import posixpath
import re
import stat
import sys
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.trial.boot import _validated_compose_env_prefix

_MAX_SOURCE_BYTES: Final = 65_536
_MAX_LINE_BYTES: Final = 4_096
_MAX_ASSIGNMENTS: Final = 64
_SYNTHETIC_VALUE: Final = "repotrial-synthetic-value"
_POLICY_ID: Final = "startup-input-v1-option-b"
_SOURCE_NAME: Final = ".env.sample"
_TARGET_NAME: Final = ".env"
_ASSIGNMENT_RE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z")
_PORTABLE_LITERAL_RE: Final = re.compile(r"[A-Za-z0-9._:/@+%,-]+\Z")
_PATH_COMPONENT_RE: Final = re.compile(r"[A-Za-z0-9._:-]+\Z")
_CONTROL_EXACT: Final = frozenset(
    {"PATH", "HOME", "PYTHONHOME", "PYTHONPATH", "XDG_CONFIG_HOME"}
)
_CONTROL_PREFIXES: Final = ("COMPOSE_", "DOCKER_", "DYLD_", "LD_")
_SECRET_COMPONENTS: Final = frozenset({"PASSWORD", "SECRET", "TOKEN"})
_GUEST_CLONE_ROOT: Final = "/workspace"
_MAX_ADAPTER_OUTPUT_BYTES: Final = 1_024
_MAX_RESOLVED_CONFIG_BYTES: Final = 4 * 1024 * 1024
_MAX_RESOLVED_CONFIG_NODES: Final = 100_000
_MAX_RESOLVED_CONFIG_DEPTH: Final = 128
_MAX_EVIDENCE_BYTES: Final = 32 * 1024
_MAX_PAYLOAD_BYTES: Final = 90_000
_MAX_BIND_SOURCES: Final = 128
_MAX_BIND_SOURCE_BYTES: Final = 4_096

# This is code-owned and intentionally not assembled from repository input.
_ADAPTER_SCRIPT: Final = """\
set -eu
umask 077
set -C

if [ "$#" -ne 5 ]; then
    exit 20
fi
source_path=$1
target_path=$2
source_sha256=$3
output_sha256=$4
payload=$5

root=$(pwd -P)
[ "$root" = "/workspace" ] || exit 21
[ "$source_path" = ".env.sample" ] || exit 22
[ "$target_path" = ".env" ] || exit 22
[ "$(dirname "$source_path")" = "." ] || exit 22
[ "$(dirname "$target_path")" = "." ] || exit 22

[ -f "$source_path" ] && [ ! -L "$source_path" ] || exit 23
source_hash=$(sha256sum "$source_path" | cut -d ' ' -f 1)
[ "$source_hash" = "$source_sha256" ] || exit 24

if [ -e "$target_path" ] || [ -L "$target_path" ]; then
    exit 25
fi

payload_bytes=$(printf '%s' "$payload" | wc -c)
[ "$payload_bytes" -le 90000 ] || exit 26
if ! printf '%s' "$payload" | base64 -d > "$target_path"; then
    if [ -f "$target_path" ] && [ ! -L "$target_path" ]; then
        rm -f "$target_path"
    fi
    exit 27
fi
chmod 600 "$target_path"
[ -f "$target_path" ] && [ ! -L "$target_path" ] || exit 28
target_mode=$(stat -c '%a' "$target_path")
[ "$target_mode" = "600" ] || exit 29
target_hash=$(sha256sum "$target_path" | cut -d ' ' -f 1)
[ "$target_hash" = "$output_sha256" ] || exit 30

printf 'root=%s\nmode=%s\n' "$root" "$target_mode"
"""
_ADAPTER_SHA256: Final = (
    "6c183581aa20385d694faf4f00ee19331ac57dfbf98cad553637df654be45fda"
)
_BIND_VALIDATOR_SCRIPT: Final = """\
set -eu

[ "$(pwd -P)" = "/workspace" ] || exit 40
for source_path do
    [ -e "$source_path" ] || exit 41
    resolved=$(realpath -e "$source_path") || exit 41
    case "$resolved" in
        /workspace|/workspace/*) ;;
        *) exit 42 ;;
    esac
    basename=${resolved##*/}
    case "$basename" in
        docker.sock|containerd.sock|cri-dockerd.sock|podman.sock|docker_engine)
            exit 43
            ;;
    esac
done
printf 'binds=ok\n'
"""
_BIND_VALIDATOR_SHA256: Final = (
    "cb6d2457656a09dccca1a9a71ac95673cd11623e43b60704687c596ac025d166"
)


class StartupInputUnsupported(RuntimeError):
    """A fail-closed startup-input planning rejection."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class StartupInputPlan:
    """Immutable, host-derived bytes and identity for one startup input."""

    compose_relative_path: str
    compose_config_hash: str
    source_relative_path: str
    target_relative_path: str
    source_sha256: str
    output_sha256: str
    all_source_key_names: tuple[str, ...]
    accepted_key_names: tuple[str, ...]
    synthetic_key_names: tuple[str, ...]
    omitted_control_key_names: tuple[str, ...]
    expected_service_names: tuple[str, ...]
    policy_id: str
    output_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class StartupInputResult:
    """Verified guest startup-input and resolved Compose identities."""

    source_sha256: str
    output_sha256: str
    resolved_compose_sha256: str
    target_mode: int
    artifact_relative_path: str


@dataclass(frozen=True, slots=True)
class _Assignment:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    file_type: int
    size: int
    mtime_ns: int
    ctime_ns: int


class _EvidenceWriter:
    def __init__(self, path: Path) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
        except FileExistsError:
            raise StartupInputUnsupported("evidence_collision") from None
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise StartupInputUnsupported("evidence_persistence_failed") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise StartupInputUnsupported("evidence_persistence_failed")
        self._descriptor = descriptor
        self._size = 0

    def append(self, record: Mapping[str, object]) -> None:
        try:
            payload = (
                json.dumps(
                    dict(record),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
        except (OverflowError, RecursionError, TypeError, ValueError):
            raise StartupInputUnsupported("evidence_persistence_failed") from None
        if self._size + len(payload) > _MAX_EVIDENCE_BYTES:
            raise StartupInputUnsupported("evidence_persistence_failed")
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(self._descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short evidence write")
                offset += written
            os.fsync(self._descriptor)
        except OSError:
            raise StartupInputUnsupported("evidence_persistence_failed") from None
        self._size += len(payload)

    def close(self) -> None:
        os.close(self._descriptor)


async def materialize_startup_input(
    provider: SandboxProvider,
    sandbox_id: str,
    plan: StartupInputPlan,
    *,
    compose_path: str,
    compose_env: Mapping[str, str],
    evidence_path: Path,
    overlay_path: str | None = None,
) -> StartupInputResult:
    """Create and verify one guest-only input, then validate resolved Compose."""
    if not isinstance(evidence_path, Path):
        raise TypeError("startup-input evidence path must be a Path")
    if compose_path != plan.compose_relative_path:
        raise StartupInputUnsupported("compose_identity_mismatch")
    prefix = _validated_compose_env_prefix(
        compose_path, compose_env, plan.all_source_key_names
    )
    _validate_guest_relative_path(compose_path, "compose_path")
    if overlay_path is not None:
        _validate_guest_relative_path(overlay_path, "overlay_path")
    payload = base64.b64encode(plan.output_bytes).decode("ascii")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise StartupInputUnsupported("payload_too_large")
    adapter_sha256 = hashlib.sha256(_ADAPTER_SCRIPT.encode()).hexdigest()
    if adapter_sha256 != _ADAPTER_SHA256:
        raise RuntimeError("startup-input adapter identity mismatch")
    bind_validator_sha256 = hashlib.sha256(_BIND_VALIDATOR_SCRIPT.encode()).hexdigest()
    if bind_validator_sha256 != _BIND_VALIDATOR_SHA256:
        raise RuntimeError("startup-input bind validator identity mismatch")

    started = time.monotonic()
    evidence = _EvidenceWriter(evidence_path)
    start_record = _evidence_identity(plan, adapter_sha256)
    start_record.update(
        {
            "outcome": "start",
            "purpose": "startup_input_materialization",
            "sequence": 0,
            "schema_version": 1,
        }
    )
    try:
        evidence.append(start_record)
        adapter_argv = [
            *prefix,
            "sh",
            "-eu",
            "-c",
            _ADAPTER_SCRIPT,
            "repotrial-startup-input",
            plan.source_relative_path,
            plan.target_relative_path,
            plan.source_sha256,
            plan.output_sha256,
            payload,
        ]
        adapter_result = await provider.exec(sandbox_id, adapter_argv, timeout_s=30)
        if not isinstance(adapter_result, ExecResult):
            raise TypeError("startup-input adapter returned a malformed result")
        if (
            len(adapter_result.stdout.encode(errors="surrogatepass"))
            > _MAX_ADAPTER_OUTPUT_BYTES
            or len(adapter_result.stderr.encode(errors="surrogatepass"))
            > _MAX_ADAPTER_OUTPUT_BYTES
            or adapter_result.exit_code != 0
            or adapter_result.stdout != "root=/workspace\nmode=600\n"
            or adapter_result.stderr
        ):
            raise StartupInputUnsupported("guest_validation_failed")

        compose_argv = [
            *prefix,
            "docker",
            "compose",
            "--project-directory",
            ".",
            "-f",
            compose_path,
        ]
        if overlay_path is not None:
            compose_argv.extend(["-f", overlay_path])
        compose_argv.extend(["config", "--format", "json"])
        config_result = await provider.exec(sandbox_id, compose_argv, timeout_s=30)
        if not isinstance(config_result, ExecResult):
            raise TypeError("Compose config returned a malformed result")
        if config_result.exit_code != 0 or config_result.stderr:
            raise StartupInputUnsupported("resolved_config_failed")
        resolved = _parse_resolved_compose(config_result.stdout)
        bind_sources = _validate_resolved_compose(resolved, plan.expected_service_names)
        if bind_sources:
            bind_result = await provider.exec(
                sandbox_id,
                [
                    *prefix,
                    "sh",
                    "-eu",
                    "-c",
                    _BIND_VALIDATOR_SCRIPT,
                    "repotrial-bind-validator",
                    *bind_sources,
                ],
                timeout_s=30,
            )
            if not isinstance(bind_result, ExecResult):
                raise TypeError("bind validator returned a malformed result")
            if (
                len(bind_result.stdout.encode(errors="surrogatepass"))
                > _MAX_ADAPTER_OUTPUT_BYTES
                or len(bind_result.stderr.encode(errors="surrogatepass"))
                > _MAX_ADAPTER_OUTPUT_BYTES
                or bind_result.exit_code != 0
                or bind_result.stdout != "binds=ok\n"
                or bind_result.stderr
            ):
                raise StartupInputUnsupported("unsafe_resolved_compose")
        resolved_material = json.dumps(
            resolved,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        resolved_sha256 = hashlib.sha256(resolved_material).hexdigest()
        terminal = _terminal_evidence(
            plan,
            adapter_sha256,
            started,
            guest_validation="satisfied",
            reason="materialized",
            resolved_compose_sha256=resolved_sha256,
            target_mode="0600",
        )
        evidence.append(terminal)
        return StartupInputResult(
            source_sha256=plan.source_sha256,
            output_sha256=plan.output_sha256,
            resolved_compose_sha256=resolved_sha256,
            target_mode=0o600,
            artifact_relative_path=plan.target_relative_path,
        )
    except StartupInputUnsupported as error:
        terminal = _terminal_evidence(
            plan,
            adapter_sha256,
            started,
            guest_validation="failed",
            reason=error.reason,
        )
        evidence.append(terminal)
        raise
    except BaseException:
        terminal = _terminal_evidence(
            plan,
            adapter_sha256,
            started,
            guest_validation="failed",
            reason="provider_exception",
        )
        try:
            evidence.append(terminal)
        except StartupInputUnsupported:
            pass
        raise
    finally:
        try:
            evidence.close()
        except OSError:
            if sys.exception() is None:
                raise StartupInputUnsupported("evidence_persistence_failed") from None


def _evidence_identity(
    plan: StartupInputPlan, adapter_sha256: str
) -> dict[str, object]:
    return {
        "accepted_key_names": list(plan.accepted_key_names),
        "adapter_sha256": adapter_sha256,
        "all_source_key_names": list(plan.all_source_key_names),
        "argv_category": "fixed_guest_startup_input_adapter",
        "bind_validator_sha256": _BIND_VALIDATOR_SHA256,
        "compose_config_hash": plan.compose_config_hash,
        "compose_relative_path": plan.compose_relative_path,
        "expected_service_names": list(plan.expected_service_names),
        "omitted_control_key_names": list(plan.omitted_control_key_names),
        "output_sha256": plan.output_sha256,
        "policy_id": plan.policy_id,
        "project_directory": ".",
        "source_relative_path": plan.source_relative_path,
        "source_sha256": plan.source_sha256,
        "synthetic_key_names": list(plan.synthetic_key_names),
        "target_relative_path": plan.target_relative_path,
    }


def _terminal_evidence(
    plan: StartupInputPlan,
    adapter_sha256: str,
    started: float,
    *,
    guest_validation: str,
    reason: str,
    resolved_compose_sha256: str | None = None,
    target_mode: str | None = None,
) -> dict[str, object]:
    terminal = _evidence_identity(plan, adapter_sha256)
    terminal.update(
        {
            "elapsed_s": max(0.0, time.monotonic() - started),
            "guest_validation": guest_validation,
            "outcome": "terminal",
            "reason": reason,
            "sequence": 1,
            "schema_version": 1,
        }
    )
    if resolved_compose_sha256 is not None:
        terminal["resolved_compose_sha256"] = resolved_compose_sha256
    if target_mode is not None:
        terminal["target_mode"] = target_mode
    return terminal


def _validate_guest_relative_path(path: str, label: str) -> None:
    if not isinstance(path, str):
        raise TypeError(f"{label} must be a string")
    candidate = Path(path)
    if (
        not path
        or "\\" in path
        or "\0" in path
        or any(ord(character) < 32 for character in path)
        or candidate.is_absolute()
        or candidate.drive
        or ".." in candidate.parts
    ):
        raise StartupInputUnsupported(f"{label}_invalid")


def _parse_resolved_compose(stdout: str) -> dict[str, object]:
    if not isinstance(stdout, str):
        raise TypeError("Compose config stdout must be a string")
    if len(stdout.encode()) > _MAX_RESOLVED_CONFIG_BYTES:
        raise StartupInputUnsupported("resolved_config_too_large")
    try:
        resolved = json.loads(
            stdout,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
        raise StartupInputUnsupported("resolved_config_invalid") from None
    if not isinstance(resolved, dict):
        raise StartupInputUnsupported("resolved_config_invalid")
    _validate_json_budget(resolved, depth=1, budget=[0])
    return resolved


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ValueError("non-standard JSON constant")


def _validate_json_budget(value: object, *, depth: int, budget: list[int]) -> None:
    if depth > _MAX_RESOLVED_CONFIG_DEPTH:
        raise StartupInputUnsupported("resolved_config_too_large")
    budget[0] += 1
    if budget[0] > _MAX_RESOLVED_CONFIG_NODES:
        raise StartupInputUnsupported("resolved_config_too_large")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StartupInputUnsupported("resolved_config_invalid")
            _validate_json_budget(item, depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        for item in value:
            _validate_json_budget(item, depth=depth + 1, budget=budget)
    elif (
        isinstance(value, float)
        and not math.isfinite(value)
        or value is not None
        and not isinstance(value, (bool, int, float, str))
    ):
        raise StartupInputUnsupported("resolved_config_invalid")


def _validate_resolved_compose(
    resolved: Mapping[str, object], expected_service_names: tuple[str, ...]
) -> tuple[str, ...]:
    services = resolved.get("services")
    if not isinstance(services, Mapping) or set(services) != set(
        expected_service_names
    ):
        raise StartupInputUnsupported("resolved_service_mismatch")
    bind_sources: list[str] = []
    for service in services.values():
        if not isinstance(service, Mapping):
            raise StartupInputUnsupported("resolved_config_invalid")
        if service.get("privileged") is True:
            raise StartupInputUnsupported("unsafe_resolved_compose")
        if service.get("network_mode") == "host":
            raise StartupInputUnsupported("unsafe_resolved_compose")
        devices = service.get("devices")
        if devices not in (None, [], {}):
            raise StartupInputUnsupported("unsafe_resolved_compose")
        volumes = service.get("volumes")
        if volumes is not None:
            bind_sources.extend(_validate_resolved_volumes(volumes))
    unique_sources = tuple(sorted(set(bind_sources)))
    if len(unique_sources) > _MAX_BIND_SOURCES:
        raise StartupInputUnsupported("unsafe_resolved_compose")
    return unique_sources


def _validate_resolved_volumes(volumes: object) -> list[str]:
    if not isinstance(volumes, list):
        raise StartupInputUnsupported("unsafe_resolved_compose")
    bind_sources: list[str] = []
    for volume in volumes:
        if not isinstance(volume, Mapping):
            raise StartupInputUnsupported("unsafe_resolved_compose")
        source = volume.get("source")
        target = volume.get("target")
        kind = volume.get("type")
        if _is_engine_socket(source) or _is_engine_socket(target):
            raise StartupInputUnsupported("unsafe_resolved_compose")
        if kind == "bind":
            if (
                not isinstance(source, str)
                or len(source.encode()) > _MAX_BIND_SOURCE_BYTES
                or not _is_inside_guest_clone(source)
            ):
                raise StartupInputUnsupported("unsafe_resolved_compose")
            bind_sources.append(source)
        elif kind not in {"volume", "tmpfs", "image", "cluster"}:
            raise StartupInputUnsupported("unsafe_resolved_compose")
    return bind_sources


def _is_engine_socket(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower().replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    return basename in {
        "containerd.sock",
        "cri-dockerd.sock",
        "docker.sock",
        "docker_engine",
        "podman.sock",
    }


def _is_inside_guest_clone(source: str) -> bool:
    if (
        "\0" in source
        or "\\" in source
        or any(ord(character) < 32 for character in source)
        or not source.startswith("/")
    ):
        return False
    normalized = posixpath.normpath(source)
    return normalized == _GUEST_CLONE_ROOT or normalized.startswith(
        f"{_GUEST_CLONE_ROOT}/"
    )


def plan_startup_input(workspace: Path, compose_path: str) -> StartupInputPlan | None:
    """Plan a guest-only root ``.env`` for the supported Compose shape."""
    root = _resolve_workspace(workspace)
    compose_file = _resolve_compose_path(root, compose_path)
    compose = load_compose(compose_file)
    target_relative, services = _required_env_target(root, compose)
    if target_relative is None:
        return None

    target = root / Path(*target_relative.split("/"))
    _require_target_absent(target)
    source = target.parent / _SOURCE_NAME
    source_bytes = _read_source(source)
    assignments = _parse_source(source_bytes)

    ordered_assignments = sorted(assignments, key=lambda item: item.key.encode("ascii"))
    output_assignments: list[_Assignment] = []
    synthetic_keys: list[str] = []
    omitted_control_keys: list[str] = []
    for assignment in ordered_assignments:
        if _is_control_key(assignment.key):
            omitted_control_keys.append(assignment.key)
            continue
        value = assignment.value
        if value and _is_secret_looking_key(assignment.key):
            value = _SYNTHETIC_VALUE
            synthetic_keys.append(assignment.key)
        output_assignments.append(_Assignment(assignment.key, value))

    output_bytes = b"".join(
        f"{assignment.key}={assignment.value}\n".encode()
        for assignment in output_assignments
    )
    compose_config_hash = hashlib.sha256(
        canonical_compose_json(compose).encode("utf-8")
    ).hexdigest()
    source_relative_path = _relative_posix(root, source)
    target_relative_path = _relative_posix(root, target)
    expected_services = tuple(sorted(services, key=lambda value: value.encode("utf-8")))
    return StartupInputPlan(
        compose_relative_path=_relative_posix(root, compose_file),
        compose_config_hash=compose_config_hash,
        source_relative_path=source_relative_path,
        target_relative_path=target_relative_path,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        output_sha256=hashlib.sha256(output_bytes).hexdigest(),
        all_source_key_names=tuple(
            assignment.key for assignment in ordered_assignments
        ),
        accepted_key_names=tuple(assignment.key for assignment in output_assignments),
        synthetic_key_names=tuple(synthetic_keys),
        omitted_control_key_names=tuple(omitted_control_keys),
        expected_service_names=expected_services,
        policy_id=_POLICY_ID,
        output_bytes=output_bytes,
    )


def _resolve_workspace(workspace: Path) -> Path:
    try:
        root = workspace.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise StartupInputUnsupported("workspace_invalid") from None
    if not root.is_dir():
        raise StartupInputUnsupported("workspace_invalid")
    return root


def _resolve_compose_path(root: Path, compose_path: str) -> Path:
    if (
        not compose_path
        or "\\" in compose_path
        or any(ord(character) < 32 for character in compose_path)
    ):
        raise StartupInputUnsupported("compose_path_invalid")
    candidate = Path(compose_path)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise StartupInputUnsupported("compose_path_invalid")
    try:
        unresolved = root / candidate
        current = root
        for index, component in enumerate(candidate.parts):
            current /= component
            metadata = current.lstat()
            is_final = index == len(candidate.parts) - 1
            if _has_reparse_point(metadata) or stat.S_ISLNK(metadata.st_mode):
                raise StartupInputUnsupported("compose_path_invalid")
            if is_final:
                if not stat.S_ISREG(metadata.st_mode):
                    raise StartupInputUnsupported("compose_path_invalid")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise StartupInputUnsupported("compose_path_invalid")
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(root)
    except StartupInputUnsupported:
        raise
    except (OSError, RuntimeError, ValueError):
        raise StartupInputUnsupported("compose_path_invalid") from None
    return resolved


def _required_env_target(
    root: Path, compose: Mapping[str, object]
) -> tuple[str | None, set[str]]:
    services = compose.get("services")
    if not isinstance(services, Mapping) or not services:
        return None, set()

    all_services: set[str] = set()
    target_candidates: set[Path] = set()
    for service_name, service in services.items():
        if not isinstance(service_name, str) or not isinstance(service, Mapping):
            return None, set()
        all_services.add(service_name)
        if "env_file" not in service:
            continue
        entries = _env_file_entries(service["env_file"])
        if entries is None:
            return None, set()
        parsed_entries = [_parse_env_file_entry(entry) for entry in entries]
        if any(entry is None for entry in parsed_entries):
            return None, set()
        valid_entries = [entry for entry in parsed_entries if entry is not None]
        required_paths = [path for path, required in valid_entries if required]
        if not required_paths:
            continue
        if len(required_paths) != 1:
            return None, set()
        raw_target = required_paths[0]
        target = _resolve_env_target(root, raw_target)
        if target is None:
            return None, set()
        try:
            target.relative_to(root)
        except ValueError:
            raise StartupInputUnsupported("target_escape") from None
        if target != root / _TARGET_NAME:
            return None, set()
        target_candidates.add(target)

    if len(target_candidates) != 1:
        return None, set()
    return _TARGET_NAME, all_services


def _env_file_entries(value: object) -> list[object] | None:
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, list):
        return list(value)
    return None


def _parse_env_file_entry(value: object) -> tuple[str, bool] | None:
    if isinstance(value, str):
        return value, True
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    required = value.get("required", True)
    if not isinstance(path, str) or not isinstance(required, bool):
        return None
    return path, required


def _resolve_env_target(compose_parent: Path, raw_target: str) -> Path | None:
    candidate = Path(raw_target)
    if candidate.is_absolute():
        raise StartupInputUnsupported("target_escape")
    if not raw_target or "\\" in raw_target or "\x00" in raw_target:
        return None
    try:
        return (compose_parent / candidate).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _require_target_absent(target: Path) -> None:
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise StartupInputUnsupported("target_preexisting") from None
    raise StartupInputUnsupported("target_preexisting")


def _read_source(path: Path) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        raise StartupInputUnsupported("source_missing") from None
    except OSError:
        raise StartupInputUnsupported("source_invalid") from None
    if _is_linked_or_nonregular(path_stat):
        raise StartupInputUnsupported("source_linked")
    expected_identity = _identity(path_stat)
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except FileNotFoundError:
        raise StartupInputUnsupported("source_missing") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StartupInputUnsupported("source_linked") from None
        raise StartupInputUnsupported("source_invalid") from None
    try:
        initial_stat = os.fstat(descriptor)
        identity = _regular_identity(initial_stat)
        if identity != expected_identity:
            raise StartupInputUnsupported("source_changed")
        source = os.read(descriptor, _MAX_SOURCE_BYTES + 1)
        final_stat = os.fstat(descriptor)
        if _regular_identity(final_stat) != identity:
            raise StartupInputUnsupported("source_changed")
        _require_path_identity(path, identity)
        if initial_stat.st_size > _MAX_SOURCE_BYTES or len(source) > _MAX_SOURCE_BYTES:
            raise StartupInputUnsupported("source_too_large")
        if final_stat.st_size != len(source):
            raise StartupInputUnsupported("source_changed")
        return source
    except StartupInputUnsupported:
        raise
    except FileNotFoundError:
        raise StartupInputUnsupported("source_changed") from None
    except OSError:
        raise StartupInputUnsupported("source_invalid") from None
    finally:
        os.close(descriptor)


def _regular_identity(stat_result: os.stat_result) -> _FileIdentity:
    if _is_linked_or_nonregular(stat_result):
        raise StartupInputUnsupported("source_invalid")
    return _identity(stat_result)


def _identity(stat_result: os.stat_result) -> _FileIdentity:
    if stat_result.st_ino == 0:
        raise StartupInputUnsupported("source_invalid")
    return _FileIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        file_type=stat.S_IFMT(stat_result.st_mode),
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        ctime_ns=stat_result.st_ctime_ns,
    )


def _is_linked_or_nonregular(stat_result: os.stat_result) -> bool:
    return not stat.S_ISREG(stat_result.st_mode) or _has_reparse_point(stat_result)


def _has_reparse_point(stat_result: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _require_path_identity(path: Path, expected: _FileIdentity) -> None:
    try:
        current = path.lstat()
    except OSError:
        raise StartupInputUnsupported("source_changed") from None
    if _is_linked_or_nonregular(current):
        raise StartupInputUnsupported("source_changed")
    actual = _identity(current)
    if actual != expected:
        raise StartupInputUnsupported("source_changed")


def _parse_source(source: bytes) -> list[_Assignment]:
    for index, byte in enumerate(source):
        if byte == 13 and (index + 1 == len(source) or source[index + 1] != 10):
            raise StartupInputUnsupported("unsupported_syntax")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise StartupInputUnsupported("invalid_utf8") from None
    for character in text:
        if unicodedata.category(character) == "Cc" and character not in "\r\n":
            raise StartupInputUnsupported("unsupported_syntax")

    assignments: list[_Assignment] = []
    seen_keys: set[str] = set()
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
            raise StartupInputUnsupported("source_too_large")
        if not line or line.strip(" ") == "":
            continue
        if line.lstrip(" ").startswith("#"):
            continue
        match = _ASSIGNMENT_RE.fullmatch(line)
        if match is None:
            raise StartupInputUnsupported("unsupported_syntax")
        key, value = match.groups()
        if key in seen_keys:
            raise StartupInputUnsupported("duplicate_key")
        _validate_value(value)
        seen_keys.add(key)
        assignments.append(_Assignment(key, value))
        if len(assignments) > _MAX_ASSIGNMENTS:
            raise StartupInputUnsupported("too_many_assignments")
    return assignments


def _validate_value(value: str) -> None:
    if value == "":
        return
    if not value.isascii() or not _PORTABLE_LITERAL_RE.fullmatch(value):
        raise StartupInputUnsupported("unsupported_syntax")
    if value.startswith(("/", "../")) or value == "..":
        raise StartupInputUnsupported("unsupported_syntax")
    if value.startswith("./"):
        _validate_relative_path(value)
    elif "/../" in value or value.endswith("/.."):
        raise StartupInputUnsupported("unsupported_syntax")


def _validate_relative_path(value: str) -> None:
    depth = 0
    for component in value[2:].split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            depth -= 1
            if depth < 0:
                raise StartupInputUnsupported("unsupported_syntax")
            continue
        if _PATH_COMPONENT_RE.fullmatch(component) is None:
            raise StartupInputUnsupported("unsupported_syntax")
        depth += 1


def _is_control_key(key: str) -> bool:
    normalized = key.upper()
    return normalized in _CONTROL_EXACT or normalized.startswith(_CONTROL_PREFIXES)


def _is_secret_looking_key(key: str) -> bool:
    components = key.upper().split("_")
    if _SECRET_COMPONENTS.intersection(components):
        return True
    return any(
        components[index : index + 2] == ["API", "KEY"]
        for index in range(len(components) - 1)
    )


def _relative_posix(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise StartupInputUnsupported("target_escape") from None
    return relative.as_posix()
