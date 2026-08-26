"""Deterministic, static baseline risk findings for Compose mappings."""

import math
import re
from collections.abc import Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.nodes import ScalarNode

from repotrial.domain.models import RiskFinding

_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_ALLOWED_YAML_TAGS = frozenset({"!override", "!reset"})
_SHORT_VOLUME_MODE_TOKENS = frozenset(
    {
        "cached",
        "consistent",
        "delegated",
        "private",
        "ro",
        "rprivate",
        "rshared",
        "rslave",
        "rw",
        "shared",
        "slave",
        "z",
        "Z",
    }
)
_RESET = object()
_UNSUPPORTED = object()


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
    source_and_target, mode = _split_short_volume_mode(volume)
    source, separator, target = source_and_target.rpartition(":")
    if not separator or not target or not _is_host_path(source):
        return None
    return source, mode is None or "ro" not in mode.split(",")


def _split_short_volume_mode(volume: str) -> tuple[str, str | None]:
    source_and_target, separator, candidate = volume.rpartition(":")
    if not separator:
        return volume, None
    tokens = candidate.split(",")
    if "," in candidate or all(token in _SHORT_VOLUME_MODE_TOKENS for token in tokens):
        return source_and_target, candidate
    return volume, None


def _is_host_path(source: str) -> bool:
    return source.startswith(("/", "./", "../", "~/", ".\\", "..\\", "\\\\")) or bool(
        _WINDOWS_DRIVE_PATH.match(source)
    )


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
    yaml = YAML(typ="rt", pure=True)
    resolved_tag = yaml.resolver.resolve(ScalarNode, scalar_value, (True, False))
    return yaml.constructor.construct_object(
        ScalarNode(tag=resolved_tag, value=scalar_value)
    )


def _json_safe_evidence(evidence: dict[str, object]) -> dict[str, object]:
    return {
        key: _json_safe_value(value, set()) for key, value in sorted(evidence.items())
    }


def _json_safe_value(value: object, active: set[int]) -> object:
    tag = _yaml_tag(value)
    if tag is not None:
        if tag not in _ALLOWED_YAML_TAGS:
            return None
        return {
            "$yaml_tag": tag,
            "value": _json_safe_untagged_value(_tagged_underlying_value(value), active),
        }
    return _json_safe_untagged_value(value, active)


def _json_safe_untagged_value(value: object, active: set[int]) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        if id(value) in active:
            return None
        active.add(id(value))
        try:
            return {
                key: _json_safe_value(value[key], active)
                for key in sorted(key for key in value if isinstance(key, str))
            }
        finally:
            active.remove(id(value))
    if isinstance(value, list):
        if id(value) in active:
            return None
        active.add(id(value))
        try:
            return [_json_safe_value(item, active) for item in value]
        finally:
            active.remove(id(value))
    return None


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
