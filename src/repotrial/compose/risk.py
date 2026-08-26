"""Deterministic, static baseline risk findings for Compose mappings."""

import re
from collections.abc import Mapping

from repotrial.domain.models import RiskFinding

_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def analyze_risk(compose: dict[str, object]) -> list[RiskFinding]:
    """Return deterministic baseline findings without modifying ``compose``."""
    services = compose.get("services")
    if not isinstance(services, Mapping):
        return []

    findings: list[RiskFinding] = []
    for service, definition in services.items():
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
    privileged = definition.get("privileged")
    if privileged is True:
        _add_finding(findings, "privileged", service, 100, {"privileged": privileged})

    network_mode = definition.get("network_mode")
    if network_mode == "host":
        _add_finding(
            findings, "host_network", service, 70, {"network_mode": network_mode}
        )

    pid = definition.get("pid")
    if pid == "host":
        _add_finding(findings, "host_pid", service, 70, {"pid": pid})

    cap_add = definition.get("cap_add")
    if isinstance(cap_add, list) and cap_add:
        _add_finding(findings, "cap_add", service, 50, {"cap_add": cap_add})

    _analyze_user(service, definition, findings)
    _analyze_rootfs(service, definition, findings)
    _analyze_volumes(service, definition.get("volumes"), findings)


def _analyze_user(
    service: str, definition: Mapping[object, object], findings: list[RiskFinding]
) -> None:
    declared = "user" in definition
    user = definition.get("user")
    if _is_root_user(user):
        _add_finding(findings, "root_user", service, 30, {"user": user})
    elif not declared or user is None or (isinstance(user, str) and user == ""):
        _add_finding(
            findings,
            "root_user_possible",
            service,
            30,
            {"user": user, "declared": declared},
        )


def _analyze_rootfs(
    service: str, definition: Mapping[object, object], findings: list[RiskFinding]
) -> None:
    declared = "read_only" in definition
    read_only = definition.get("read_only")
    if read_only is not True:
        _add_finding(
            findings,
            "writable_rootfs",
            service,
            25,
            {"read_only": read_only, "declared": declared},
        )


def _analyze_volumes(
    service: str, volumes: object, findings: list[RiskFinding]
) -> None:
    if not isinstance(volumes, list):
        return

    socket_binds: list[object] = []
    host_binds: list[object] = []
    for volume in volumes:
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
    if isinstance(volume, Mapping):
        if volume.get("type") != "bind":
            return None
        source = volume.get("source")
        if not isinstance(source, str) or not _is_host_path(source):
            return None
        return source, volume.get("read_only") is not True

    if not isinstance(volume, str):
        return None
    return _short_host_bind(volume)


def _short_host_bind(volume: str) -> tuple[str, bool] | None:
    for index, character in enumerate(volume):
        if character != ":":
            continue
        source = volume[:index]
        if not _is_host_path(source):
            continue
        target_and_mode = volume[index + 1 :]
        if not target_and_mode:
            return None
        _, separator, mode = target_and_mode.rpartition(":")
        return source, not (separator and mode == "ro")
    return None


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
            evidence=evidence,
        )
    )
