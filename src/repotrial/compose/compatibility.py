"""Plan an immutable Compose overlay for loopback-only publications."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import re
import stat
from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tag import Tag


class CompatibilityError(ValueError):
    """A sanitized compatibility planning or artifact persistence failure."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"compatibility planning failed: {reason}")


@dataclass(frozen=True, slots=True)
class CompatibilityArtifact:
    """The path and persisted-byte identity of one compatibility overlay."""

    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class CompatibilityPlan:
    """The side-effect-free serialized bytes for one compatibility overlay."""

    payload: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _ShortParts:
    published: str
    target: str
    protocol_suffix: str | None


@dataclass(frozen=True, slots=True)
class _PortBinding:
    service: str
    index: int
    target: int
    published: int
    protocol: Literal["tcp", "udp"]
    host_ip: str | None
    loopback: bool
    short_parts: _ShortParts | None


def write_loopback_compatibility_overlay(
    compose: dict[str, object], container_port: int, path: Path
) -> CompatibilityArtifact | None:
    """Write a trial-only overlay that widens one loopback publication.

    The input is expected to be the round-trip mapping returned by
    :func:`repotrial.compose.parser.load_compose`.  It is never modified.
    """
    plan = plan_loopback_compatibility_overlay(compose, container_port)
    if plan is None:
        return None
    return _persist_overlay(plan.payload, path)


def plan_loopback_compatibility_overlay(
    compose: dict[str, object], container_port: int
) -> CompatibilityPlan | None:
    """Plan compatibility overlay bytes without touching the filesystem."""

    _validate_container_port(container_port)
    bindings = _collect_bindings(compose)
    matches = [
        binding
        for binding in bindings
        if binding.loopback and binding.target == container_port
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise CompatibilityError("ambiguous_loopback_binding")

    selected = matches[0]
    for binding in bindings:
        if _same_binding(binding, selected):
            continue
        if (
            binding.published == selected.published
            and binding.protocol == selected.protocol
        ):
            raise CompatibilityError("published_port_collision")

    overlay = _compatibility_overlay(compose, selected)
    payload = _serialize_overlay(overlay)
    return CompatibilityPlan(
        payload=payload, sha256=hashlib.sha256(payload).hexdigest()
    )


def _validate_container_port(container_port: object) -> None:
    if type(container_port) is not int or not 1 <= container_port <= 65_535:
        raise CompatibilityError("invalid_container_port")


def _collect_bindings(compose: object) -> list[_PortBinding]:
    if not isinstance(compose, Mapping):
        raise CompatibilityError("invalid_compose")
    raw_services = compose.get("services")
    if _tag_name(raw_services) not in (None, "!override", "!reset"):
        raise CompatibilityError("unsupported_tag")
    if _tag_name(raw_services) == "!reset" or not isinstance(raw_services, Mapping):
        raise CompatibilityError("invalid_services")

    bindings: list[_PortBinding] = []
    for service_name, raw_service in raw_services.items():
        if not isinstance(service_name, str) or not isinstance(raw_service, Mapping):
            raise CompatibilityError("invalid_service")
        _reject_host_networking(raw_service)
        if "ports" not in raw_service:
            continue
        raw_ports = raw_service["ports"]
        ports_tag = _tag_name(raw_ports)
        if ports_tag == "!reset":
            continue
        if ports_tag not in (None, "!override"):
            raise CompatibilityError("unsupported_tag")
        if not isinstance(raw_ports, list):
            raise CompatibilityError("invalid_ports")
        for index, entry in enumerate(raw_ports):
            bindings.append(_parse_port_entry(service_name, index, entry))
    return bindings


def _reject_host_networking(service: Mapping[object, object]) -> None:
    if "network_mode" not in service:
        return
    raw_mode = service["network_mode"]
    mode_tag = _tag_name(raw_mode)
    if mode_tag == "!reset":
        return
    if mode_tag not in (None, "!override"):
        raise CompatibilityError("unsupported_tag")
    mode = raw_mode
    if mode_tag == "!override":
        mode = getattr(raw_mode, "value", raw_mode)
    if mode == "host":
        raise CompatibilityError("host_networking")


def _parse_port_entry(service: str, index: int, entry: object) -> _PortBinding:
    if _tag_name(entry) is not None:
        raise CompatibilityError("tagged_port")
    if isinstance(entry, str):
        return _parse_short_entry(service, index, entry)
    if isinstance(entry, Mapping):
        return _parse_long_entry(service, index, entry)
    raise CompatibilityError("invalid_port")


def _parse_short_entry(service: str, index: int, entry: str) -> _PortBinding:
    body, protocol, protocol_suffix = _split_protocol(entry)
    host_ip: str | None
    published_text: str
    target_text: str

    if body.startswith("["):
        close = body.find("]")
        if close < 0 or close + 1 >= len(body) or body[close + 1] != ":":
            raise CompatibilityError("invalid_port")
        host_ip = body[1:close]
        parts = _split_short_parts(body[close + 2 :])
        if len(parts) != 2:
            raise CompatibilityError("invalid_port")
        published_text, target_text = parts
    else:
        parts = _split_short_parts(body)
        if len(parts) == 2:
            host_ip = None
            published_text, target_text = parts
        elif len(parts) == 3:
            host_ip, published_text, target_text = parts
        else:
            raise CompatibilityError("invalid_port")

    parsed_host, loopback = _parse_host_ip(host_ip)
    published = _parse_port_number(published_text)
    target = _parse_port_number(target_text)
    return _PortBinding(
        service=service,
        index=index,
        target=target,
        published=published,
        protocol=protocol,
        host_ip=parsed_host,
        loopback=loopback,
        short_parts=_ShortParts(
            published=published_text,
            target=target_text,
            protocol_suffix=protocol_suffix,
        ),
    )


def _split_short_parts(body: str) -> list[str]:
    parts: list[str] = []
    start = 0
    in_interpolation = False
    index = 0
    while index < len(body):
        character = body[index]
        if in_interpolation:
            if character == "{":
                raise CompatibilityError("invalid_port")
            if character == "}":
                in_interpolation = False
            index += 1
            continue
        if character == "$" and index + 1 < len(body) and body[index + 1] == "{":
            in_interpolation = True
            index += 2
            continue
        if character == "}":
            raise CompatibilityError("invalid_port")
        if character == ":":
            parts.append(body[start:index])
            start = index + 1
        index += 1
    if in_interpolation:
        raise CompatibilityError("invalid_port")
    parts.append(body[start:])
    return parts


def _split_protocol(
    entry: str,
) -> tuple[str, Literal["tcp", "udp"], str | None]:
    if "/" not in entry:
        return entry, "tcp", None
    if entry.count("/") != 1:
        raise CompatibilityError("invalid_protocol")
    body, suffix = entry.rsplit("/", 1)
    if suffix == "tcp":
        return body, "tcp", "/tcp"
    if suffix == "udp":
        return body, "udp", "/udp"
    raise CompatibilityError("invalid_protocol")


def _parse_long_entry(
    service: str, index: int, entry: Mapping[object, object]
) -> _PortBinding:
    if _tag_name(entry) is not None:
        raise CompatibilityError("tagged_port")
    if "target" not in entry or "published" not in entry:
        raise CompatibilityError("invalid_port")
    target = _parse_port_number(entry["target"])
    published = _parse_port_number(entry["published"])

    if "protocol" not in entry:
        protocol: Literal["tcp", "udp"] = "tcp"
    else:
        raw_protocol = entry["protocol"]
        if _tag_name(raw_protocol) is not None:
            raise CompatibilityError("tagged_port")
        if raw_protocol == "tcp":
            protocol = "tcp"
        elif raw_protocol == "udp":
            protocol = "udp"
        else:
            raise CompatibilityError("invalid_protocol")

    if "host_ip" not in entry:
        host_ip = None
        loopback = False
    else:
        host_ip, loopback = _parse_host_ip(entry["host_ip"])
    return _PortBinding(
        service=service,
        index=index,
        target=target,
        published=published,
        protocol=protocol,
        host_ip=host_ip,
        loopback=loopback,
        short_parts=None,
    )


def _parse_host_ip(value: object) -> tuple[str | None, bool]:
    if _tag_name(value) is not None:
        raise CompatibilityError("tagged_port")
    if value is None:
        return None, False
    if not isinstance(value, str) or not value:
        raise CompatibilityError("host_not_numeric")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        raise CompatibilityError("host_not_numeric") from None
    return value, parsed.is_loopback


def _parse_port_number(value: object) -> int:
    if _tag_name(value) is not None:
        raise CompatibilityError("tagged_port")
    if isinstance(value, bool):
        raise CompatibilityError("invalid_port")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        interpolation = re.fullmatch(
            r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-|-)[0-9]+\}", value
        )
        if interpolation:
            default_text = value.rsplit("}", 1)[0].rsplit("-", 1)[1]
            try:
                number = int(default_text)
            except ValueError:
                raise CompatibilityError("invalid_port") from None
        elif "{" in value or "}" in value:
            raise CompatibilityError("invalid_port")
        elif "-" in value:
            raise CompatibilityError("port_range")
        else:
            if not value or not value.isascii() or not value.isdigit():
                raise CompatibilityError("invalid_port")
            try:
                number = int(value)
            except ValueError:
                raise CompatibilityError("invalid_port") from None
    else:
        raise CompatibilityError("invalid_port")
    if not 1 <= number <= 65_535:
        raise CompatibilityError("invalid_port")
    return number


def _compatibility_overlay(
    compose: Mapping[str, object], selected: _PortBinding
) -> CommentedMap:
    try:
        copied = deepcopy(compose)
        services = copied.get("services")
        if not isinstance(services, Mapping):
            raise CompatibilityError("invalid_services")
        service = services.get(selected.service)
        if not isinstance(service, MutableMapping):
            raise CompatibilityError("invalid_service")
        ports = service.get("ports")
        if not isinstance(ports, list):
            raise CompatibilityError("invalid_ports")
        output_ports = ports if isinstance(ports, CommentedSeq) else CommentedSeq(ports)
        if selected.short_parts is not None:
            parts = selected.short_parts
            output_ports[selected.index] = (
                f"0.0.0.0:{parts.published}:{parts.target}{parts.protocol_suffix or ''}"
            )
        else:
            selected_entry = output_ports[selected.index]
            if not isinstance(selected_entry, MutableMapping):
                raise CompatibilityError("invalid_port")
            selected_entry["host_ip"] = "0.0.0.0"
        output_ports.yaml_set_ctag(Tag(suffix="!override"))
        service_overlay = CommentedMap()
        service_overlay["ports"] = output_ports
        services_overlay = CommentedMap()
        services_overlay[selected.service] = service_overlay
        overlay = CommentedMap()
        overlay["services"] = services_overlay
        return overlay
    except CompatibilityError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        raise CompatibilityError("invalid_compose") from None


def _serialize_overlay(overlay: Mapping[object, object]) -> bytes:
    yaml = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    try:
        serialized = io.StringIO()
        yaml.dump(overlay, serialized)
        return serialized.getvalue().encode("utf-8")
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError, YAMLError):
        raise CompatibilityError("artifact_persistence_failed") from None


def _persist_overlay(payload: bytes, path: Path) -> CompatibilityArtifact:
    try:
        with path.open("x+b") as output:
            written = output.write(payload)
            if written != len(payload):
                raise OSError("short artifact write")
            output.flush()
            os.fsync(output.fileno())
            created_identity = _regular_fd_identity(output.fileno())
            first_identity = _regular_file_identity(path)
            if first_identity != created_identity:
                raise CompatibilityError("artifact_persistence_failed")
            output.seek(0)
            data = output.read()
            if _regular_fd_identity(output.fileno()) != created_identity:
                raise CompatibilityError("artifact_persistence_failed")
            second_identity = _regular_file_identity(path)
            if second_identity != created_identity:
                raise CompatibilityError("artifact_persistence_failed")
    except CompatibilityError:
        raise
    except FileExistsError:
        raise CompatibilityError("artifact_path_exists") from None
    except (OSError, TypeError, ValueError, RecursionError, YAMLError):
        raise CompatibilityError("artifact_persistence_failed") from None

    try:
        closed_identity = _regular_file_identity(path)
    except (OSError, ValueError, UnicodeError):
        raise CompatibilityError("artifact_persistence_failed") from None
    if closed_identity != created_identity:
        raise CompatibilityError("artifact_persistence_failed")
    return CompatibilityArtifact(path=path, sha256=hashlib.sha256(data).hexdigest())


def _regular_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise OSError("artifact is not a regular file")
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _regular_fd_identity(fd: int) -> tuple[int, int, int, int, int]:
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise OSError("artifact is not a regular file")
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _same_binding(left: _PortBinding, right: _PortBinding) -> bool:
    return left.service == right.service and left.index == right.index


def _tag_name(value: object) -> str | None:
    tag = getattr(value, "tag", None)
    name = getattr(tag, "value", None)
    return name if isinstance(name, str) else None
