"""Deterministic, static baseline risk findings for Compose mappings."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ruamel.yaml import YAML
from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import ScalarNode

from repotrial.domain.models import RiskFinding

_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_ALLOWED_YAML_TAGS = frozenset({"!override", "!reset"})
_MAX_EVIDENCE_DEPTH = 48
_MAX_EVIDENCE_NODES = 10_000
_RESET = object()
_UNSUPPORTED = object()


@dataclass
class _EvidenceBudget:
    remaining: int = _MAX_EVIDENCE_NODES

    def consume(self) -> bool:
        if self.remaining == 0:
            return False
        self.remaining -= 1
        return True

    def reserve(self, count: int) -> bool:
        if count > self.remaining:
            return False
        self.remaining -= count
        return True


@dataclass(frozen=True)
class _ShortVolumeCandidate:
    separator_index: int
    target_end: int
    mode_start: int | None


@dataclass(frozen=True)
class _EvidenceValue:
    value: object
    reason: str | None = None
    stop: bool = False


def analyze_risk(compose: dict[str, object]) -> list[RiskFinding]:
    """Return deterministic baseline findings without modifying ``compose``."""
    services = _effective_value(compose.get("services"))
    if not isinstance(services, Mapping):
        return []

    findings: list[RiskFinding] = []
    for service, raw_definition in services.items():
        definition = _effective_value(raw_definition)
        if not isinstance(service, str) or not isinstance(definition, Mapping):
            continue
        _analyze_service(service, definition, findings)

    return sorted(
        findings,
        key=lambda finding: (
            -finding.severity,
            finding.kind,
            finding.service,
            finding.finding_id,
        ),
    )


def risk_score(findings: list[RiskFinding]) -> int:
    """Return the unbounded sum of the supplied finding severities."""
    return sum(finding.severity for finding in findings)


def _analyze_service(
    service: str, definition: Mapping[object, object], findings: list[RiskFinding]
) -> None:
    raw_privileged = definition.get("privileged")
    privileged = _effective_value(raw_privileged)
    if privileged is True:
        _add_finding(
            findings, "privileged", service, 100, {"privileged": raw_privileged}
        )

    raw_network_mode = definition.get("network_mode")
    network_mode = _effective_value(raw_network_mode)
    if network_mode == "host":
        _add_finding(
            findings, "host_network", service, 70, {"network_mode": raw_network_mode}
        )

    raw_pid = definition.get("pid")
    pid = _effective_value(raw_pid)
    if pid == "host":
        _add_finding(findings, "host_pid", service, 70, {"pid": raw_pid})

    raw_cap_add = definition.get("cap_add")
    cap_add = _effective_value(raw_cap_add)
    if isinstance(cap_add, list) and cap_add:
        _add_finding(findings, "cap_add", service, 50, {"cap_add": raw_cap_add})

    _analyze_user(service, definition, findings)
    _analyze_rootfs(service, definition, findings)
    _analyze_volumes(service, definition.get("volumes"), findings)


def _analyze_user(
    service: str, definition: Mapping[object, object], findings: list[RiskFinding]
) -> None:
    declared = "user" in definition
    raw_user = definition.get("user")
    user = _effective_value(raw_user)
    if _is_root_user(user):
        _add_finding(findings, "root_user", service, 30, {"user": raw_user})
    elif (
        user is _RESET
        or not declared
        or user is None
        or (isinstance(user, str) and user == "")
    ):
        _add_finding(
            findings,
            "root_user_possible",
            service,
            30,
            {"user": raw_user, "declared": declared},
        )


def _analyze_rootfs(
    service: str, definition: Mapping[object, object], findings: list[RiskFinding]
) -> None:
    declared = "read_only" in definition
    raw_read_only = definition.get("read_only")
    read_only = _effective_value(raw_read_only)
    if read_only is not True:
        _add_finding(
            findings,
            "writable_rootfs",
            service,
            25,
            {"read_only": raw_read_only, "declared": declared},
        )


def _analyze_volumes(
    service: str, volumes: object, findings: list[RiskFinding]
) -> None:
    effective_volumes = _effective_value(volumes)
    if not isinstance(effective_volumes, list):
        return

    socket_binds: list[object] = []
    host_binds: list[object] = []
    for volume in effective_volumes:
        bind = _recognized_host_bind(volume)
        if bind is None:
            continue
        source, read_write = bind
        if not read_write:
            continue
        if _is_docker_socket(source):
            socket_binds.append(volume)
        else:
            host_binds.append(volume)

    if socket_binds:
        _add_finding(
            findings,
            "docker_socket_rw",
            service,
            90,
            {"volumes": socket_binds},
        )
    if host_binds:
        _add_finding(findings, "rw_host_bind", service, 25, {"volumes": host_binds})


def _recognized_host_bind(volume: object) -> tuple[str, bool] | None:
    effective_volume = _effective_value(volume)
    if effective_volume is _RESET or effective_volume is _UNSUPPORTED:
        return None

    if isinstance(effective_volume, Mapping):
        bind_type = _effective_value(effective_volume.get("type"))
        if bind_type != "bind":
            return None
        source = _effective_value(effective_volume.get("source"))
        if not isinstance(source, str) or not _is_host_path(source):
            return None
        read_only = _effective_value(effective_volume.get("read_only"))
        return source, read_only is not True

    if not isinstance(effective_volume, str):
        return None
    return _short_host_bind(effective_volume)


def _short_host_bind(volume: str) -> tuple[str, bool] | None:
    final_separator = volume.rfind(":")
    if final_separator >= 0 and _is_scalar_mode_range(
        volume, final_separator + 1, len(volume)
    ):
        candidate = _scan_short_volume_candidate(
            volume, final_separator, final_separator + 1
        )
    else:
        candidate = _scan_short_volume_candidate(volume, len(volume), None)

    if candidate is None:
        return None
    source, _, mode = _materialize_short_volume_candidate(volume, candidate)
    return source, mode is None or "ro" not in mode.split(",")


def _scan_short_volume_candidate(
    volume: str, target_end: int, mode_start: int | None
) -> _ShortVolumeCandidate | None:
    candidate: _ShortVolumeCandidate | None = None
    for separator_index in range(target_end - 1, -1, -1):
        if volume[separator_index] != ":":
            continue
        if not _is_host_path_prefix(volume, separator_index):
            continue
        if not _is_container_path_range(volume, separator_index + 1, target_end):
            continue
        if candidate is not None:
            return None
        candidate = _ShortVolumeCandidate(separator_index, target_end, mode_start)
    return candidate


def _materialize_short_volume_candidate(
    volume: str, candidate: _ShortVolumeCandidate
) -> tuple[str, str, str | None]:
    source = volume[: candidate.separator_index]
    target = volume[candidate.separator_index + 1 : candidate.target_end]
    mode = None if candidate.mode_start is None else volume[candidate.mode_start :]
    return source, target, mode


def _is_scalar_mode_range(volume: str, start: int, end: int) -> bool:
    if start >= end:
        return False
    token_start = start
    for index in range(start, end):
        character = volume[index]
        if character == ",":
            if index == token_start:
                return False
            token_start = index + 1
            continue
        if not (
            "A" <= character <= "Z"
            or "a" <= character <= "z"
            or "0" <= character <= "9"
            or character in "._-"
        ):
            return False
    return token_start < end


def _is_host_path_prefix(volume: str, end: int) -> bool:
    if end >= 1 and volume[0] == "/":
        return True
    if end >= 2 and volume[0] in {".", "~"} and volume[1] in {"/", "\\"}:
        return True
    if (
        end >= 3
        and volume[0] == "."
        and volume[1] == "."
        and volume[2]
        in {
            "/",
            "\\",
        }
    ):
        return True
    if end >= 2 and volume[0] == "\\" and volume[1] == "\\":
        return True
    return (
        end >= 3
        and _is_ascii_letter(volume[0])
        and volume[1] == ":"
        and volume[2] in {"/", "\\"}
    )


def _is_container_path_range(volume: str, start: int, end: int) -> bool:
    if start >= end:
        return False
    if volume[start] == "/":
        return True
    if end - start >= 2 and volume[start] == "\\" and volume[start + 1] == "\\":
        return True
    return (
        end - start >= 3
        and _is_ascii_letter(volume[start])
        and volume[start + 1] == ":"
        and volume[start + 2] in {"/", "\\"}
    )


def _is_ascii_letter(character: str) -> bool:
    return "A" <= character <= "Z" or "a" <= character <= "z"


def _is_host_path(source: str) -> bool:
    return source.startswith(("/", "./", "../", "~/", ".\\", "..\\", "\\\\")) or bool(
        _WINDOWS_DRIVE_PATH.match(source)
    )


def _is_container_path(target: str) -> bool:
    return target.startswith(("/", "\\\\")) or bool(_WINDOWS_DRIVE_PATH.match(target))


def _is_docker_socket(source: str) -> bool:
    return source.replace("\\", "/") == "/var/run/docker.sock"


def _is_root_user(user: object) -> bool:
    if isinstance(user, bool):
        return False
    if isinstance(user, int):
        return user == 0
    if not isinstance(user, str):
        return False
    return user.split(":", maxsplit=1)[0] in {"root", "0"}


def _effective_value(value: object) -> object:
    tag = _yaml_tag(value)
    if tag is None:
        return value
    if tag == "!reset":
        return _RESET
    if tag == "!override":
        return _tagged_underlying_value(value)
    return _UNSUPPORTED


def _yaml_tag(value: object) -> str | None:
    tag = getattr(value, "tag", None)
    tag_value = getattr(tag, "value", None)
    if isinstance(tag_value, str) and tag_value in _ALLOWED_YAML_TAGS:
        return tag_value
    if isinstance(tag_value, str):
        return tag_value
    return None


def _tagged_underlying_value(value: object) -> object:
    if not isinstance(value, TaggedScalar):
        return value
    scalar_value = value.value
    if not isinstance(scalar_value, str):
        return _UNSUPPORTED
    if value.style is not None:
        return scalar_value
    try:
        yaml = YAML(typ="rt", pure=True)
        resolved_tag = yaml.resolver.resolve(ScalarNode, scalar_value, (True, False))
        return yaml.constructor.construct_object(
            ScalarNode(tag=resolved_tag, value=scalar_value)
        )
    except (OverflowError, RecursionError, UnicodeError, ValueError, YAMLError):
        return _UNSUPPORTED


def _json_safe_evidence(evidence: dict[str, object]) -> dict[str, object]:
    budget = _EvidenceBudget()
    if not budget.consume() or not budget.reserve(len(evidence)):
        return _evidence_unsupported("node_limit")

    safe_evidence: dict[str, object] = {}
    for key in sorted(evidence):
        if budget.remaining == 0:
            return _evidence_unsupported("node_limit")
        safe_value = _json_safe_value(evidence[key], set(), 1, budget)
        if safe_value.reason == "depth":
            safe_evidence[key] = _evidence_unsupported("depth_limit")
            break
        if safe_value.reason == "node":
            if not safe_value.stop:
                return _evidence_unsupported("node_limit")
            safe_evidence[key] = safe_value.value
            break
        safe_evidence[key] = safe_value.value
    return safe_evidence


def _json_safe_value(
    value: object, active: set[int], depth: int, budget: _EvidenceBudget
) -> _EvidenceValue:
    tag = _yaml_tag(value)
    if tag is not None:
        if tag not in _ALLOWED_YAML_TAGS:
            return _json_safe_scalar(None, budget)
        if depth >= _MAX_EVIDENCE_DEPTH:
            return _evidence_limit("depth")
        if not budget.consume():
            return _evidence_limit("node")
        safe_value = _json_safe_untagged_value(
            _tagged_underlying_value(value), active, depth + 1, budget
        )
        if safe_value.reason == "depth":
            return _evidence_marker_result("depth")
        if safe_value.reason == "node":
            return _evidence_marker_result("node", stop=True)
        return _EvidenceValue({"$yaml_tag": tag, "value": safe_value.value})
    return _json_safe_untagged_value(value, active, depth, budget)


def _json_safe_untagged_value(
    value: object, active: set[int], depth: int, budget: _EvidenceBudget
) -> _EvidenceValue:
    if value is None or isinstance(value, (bool, int, str)):
        return _json_safe_scalar(value, budget)
    if isinstance(value, float):
        return _json_safe_scalar(value if math.isfinite(value) else None, budget)
    if isinstance(value, Mapping):
        return _json_safe_mapping(value, active, depth, budget)
    if isinstance(value, list):
        return _json_safe_sequence(value, active, depth, budget)
    return _json_safe_scalar(None, budget)


def _json_safe_scalar(value: object, budget: _EvidenceBudget) -> _EvidenceValue:
    if not budget.consume():
        return _evidence_limit("node")
    return _EvidenceValue(value)


def _json_safe_mapping(
    value: Mapping[object, object],
    active: set[int],
    depth: int,
    budget: _EvidenceBudget,
) -> _EvidenceValue:
    if id(value) in active:
        return _json_safe_scalar(None, budget)
    if depth >= _MAX_EVIDENCE_DEPTH:
        return _evidence_limit("depth")
    if not budget.consume():
        return _evidence_limit("node")
    if not budget.reserve(len(value)):
        return _evidence_marker_result("node", stop=True)

    active.add(id(value))
    try:
        safe_mapping: dict[str, object] = {}
        for key in sorted(key for key in value if isinstance(key, str)):
            if budget.remaining == 0:
                return _evidence_marker_result("node", stop=True)
            safe_value = _json_safe_value(value[key], active, depth + 1, budget)
            if safe_value.reason == "depth":
                return _evidence_marker_result("depth")
            if safe_value.reason == "node":
                return _evidence_marker_result("node", stop=True)
            safe_mapping[key] = safe_value.value
        return _EvidenceValue(safe_mapping)
    finally:
        active.remove(id(value))


def _json_safe_sequence(
    value: list[object], active: set[int], depth: int, budget: _EvidenceBudget
) -> _EvidenceValue:
    if id(value) in active:
        return _json_safe_scalar(None, budget)
    if depth >= _MAX_EVIDENCE_DEPTH:
        return _evidence_limit("depth")
    if not budget.consume():
        return _evidence_limit("node")
    if len(value) > budget.remaining:
        return _evidence_marker_result("node", stop=True)

    active.add(id(value))
    try:
        safe_sequence: list[object] = []
        for index in range(len(value)):
            if budget.remaining == 0:
                return _evidence_marker_result("node", stop=True)
            safe_value = _json_safe_value(value[index], active, depth + 1, budget)
            if safe_value.reason == "depth":
                return _evidence_marker_result("depth")
            if safe_value.reason == "node":
                return _evidence_marker_result("node", stop=True)
            safe_sequence.append(safe_value.value)
        return _EvidenceValue(safe_sequence)
    finally:
        active.remove(id(value))


def _evidence_limit(reason: str) -> _EvidenceValue:
    return _EvidenceValue(None, reason)


def _evidence_marker_result(reason: str, stop: bool = False) -> _EvidenceValue:
    return _EvidenceValue(_evidence_unsupported(f"{reason}_limit"), reason, stop)


def _evidence_unsupported(reason: str) -> dict[str, object]:
    return {"$evidence_unsupported": reason}


def _add_finding(
    findings: list[RiskFinding],
    kind: str,
    service: str,
    severity: int,
    evidence: dict[str, object],
) -> None:
    findings.append(
        RiskFinding(
            finding_id=f"{kind}:{service}",
            kind=kind,
            service=service,
            severity=severity,
            evidence=_json_safe_evidence(evidence),
        )
    )
