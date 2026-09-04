"""Tests for loopback publication compatibility artifacts."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from ruamel.yaml import YAML

from repotrial.compose.compatibility import (
    CompatibilityArtifact,
    CompatibilityError,
    write_loopback_compatibility_overlay,
)


def _compose(ports: object, *, network_mode: object = None) -> dict[str, object]:
    service: dict[str, object] = {"image": "example/app", "ports": ports}
    if network_mode is not None:
        service["network_mode"] = network_mode
    return {"services": {"app": service}}


def _load_overlay(path: Path) -> dict[str, object]:
    yaml = YAML(typ="rt", pure=True)
    loaded = yaml.load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_changedetection_short_binding_only_widens_loopback_and_tags_ports(
    tmp_path: Path,
) -> None:
    compose = _compose(
        [
            "127.0.0.1:5000:5000",
            "127.0.0.1:9090:9090/udp",
            "0.0.0.0:8080:8080",
        ]
    )

    artifact = write_loopback_compatibility_overlay(
        compose, container_port=5000, path=tmp_path / "compatibility.overlay.yaml"
    )

    assert isinstance(artifact, CompatibilityArtifact)
    overlay = _load_overlay(artifact.path)
    ports = overlay["services"]["app"]["ports"]
    assert getattr(ports, "tag", None).value == "!override"
    assert list(ports) == [
        "0.0.0.0:5000:5000",
        "127.0.0.1:9090:9090/udp",
        "0.0.0.0:8080:8080",
    ]


def test_long_binding_preserves_order_extra_fields_and_protocol(
    tmp_path: Path,
) -> None:
    compose = _compose(
        [
            {
                "target": 5000,
                "published": 5001,
                "host_ip": "127.0.0.1",
                "protocol": "udp",
                "mode": "host",
                "name": "stable-extra-field",
            },
            {
                "target": 9000,
                "published": 9001,
                "host_ip": "192.0.2.10",
                "protocol": "tcp",
                "mode": "ingress",
            },
        ]
    )

    artifact = write_loopback_compatibility_overlay(
        compose, container_port=5000, path=tmp_path / "compatibility.overlay.yml"
    )

    assert artifact is not None
    ports = _load_overlay(artifact.path)["services"]["app"]["ports"]
    selected = ports[0]
    assert list(selected) == [
        "target",
        "published",
        "host_ip",
        "protocol",
        "mode",
        "name",
    ]
    assert dict(selected) == {
        "target": 5000,
        "published": 5001,
        "host_ip": "0.0.0.0",
        "protocol": "udp",
        "mode": "host",
        "name": "stable-extra-field",
    }
    assert dict(ports[1]) == {
        "target": 9000,
        "published": 9001,
        "host_ip": "192.0.2.10",
        "protocol": "tcp",
        "mode": "ingress",
    }


def test_bracketed_ipv6_short_binding_preserves_ports_and_protocol(
    tmp_path: Path,
) -> None:
    compose = _compose(["[::1]:5000:5000/tcp", "127.0.0.1:6000:6000"])

    artifact = write_loopback_compatibility_overlay(
        compose, container_port=5000, path=tmp_path / "compatibility.overlay.yaml"
    )

    assert artifact is not None
    ports = _load_overlay(artifact.path)["services"]["app"]["ports"]
    assert list(ports) == ["0.0.0.0:5000:5000/tcp", "127.0.0.1:6000:6000"]


def test_no_eligible_binding_returns_none_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "compatibility.overlay.yaml"

    artifact = write_loopback_compatibility_overlay(
        _compose(["0.0.0.0:5000:5000", "192.0.2.10:5001:5000"]),
        container_port=5000,
        path=path,
    )

    assert artifact is None
    assert not path.exists()


def test_writer_does_not_mutate_input(tmp_path: Path) -> None:
    compose = _compose(
        [
            {"target": 5000, "published": 5000, "host_ip": "127.0.0.1"},
            "127.0.0.1:6000:6000",
        ]
    )
    before = deepcopy(compose)

    write_loopback_compatibility_overlay(
        compose, container_port=5000, path=tmp_path / "compatibility.overlay.yaml"
    )

    assert compose == before


@pytest.mark.parametrize(
    ("ports", "reason"),
    [
        (["127.0.0.1:5000-5001:5000-5001"], "port_range"),
        (["localhost:5000:5000"], "host_not_numeric"),
        (["127.0.0.1:5000:5000/sctp"], "invalid_protocol"),
        (["127.0.0.1:0:5000"], "invalid_port"),
        (["127.0.0.1:5000:65536"], "invalid_port"),
    ],
)
def test_invalid_short_bindings_fail_closed(
    tmp_path: Path, ports: object, reason: str
) -> None:
    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose(ports),
            container_port=5000,
            path=tmp_path / f"{reason}.overlay.yaml",
        )

    assert error.value.reason == reason


def test_tagged_port_value_fails_closed(tmp_path: Path) -> None:
    yaml = YAML(typ="rt", pure=True)
    compose = yaml.load(
        "services:\n"
        "  app:\n"
        "    image: example/app\n"
        "    ports:\n"
        "      - 127.0.0.1:5000:5000\n"
        "      - !override 127.0.0.1:6000:6000\n"
    )

    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            compose,
            container_port=5000,
            path=tmp_path / "tagged.overlay.yaml",
        )

    assert error.value.reason == "tagged_port"


def test_multiple_matching_loopback_bindings_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose(["127.0.0.1:5000:5000", "[::1]:5001:5000"]),
            container_port=5000,
            path=tmp_path / "ambiguous.overlay.yaml",
        )

    assert error.value.reason == "ambiguous_loopback_binding"


def test_host_networking_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose(["127.0.0.1:5000:5000"], network_mode="host"),
            container_port=5000,
            path=tmp_path / "host.overlay.yaml",
        )

    assert error.value.reason == "host_networking"


def test_published_port_protocol_collision_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose(["127.0.0.1:5000:5000", "0.0.0.0:5000:5001"]),
            container_port=5000,
            path=tmp_path / "collision.overlay.yaml",
        )

    assert error.value.reason == "published_port_collision"


@pytest.mark.parametrize("path_kind", ["file", "directory", "symlink"])
def test_existing_or_linked_artifact_paths_are_rejected(
    tmp_path: Path, path_kind: str
) -> None:
    path = tmp_path / "compatibility.overlay.yaml"
    if path_kind == "file":
        path.write_text("existing", encoding="utf-8")
    elif path_kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target.overlay.yaml"
        target.write_text("target", encoding="utf-8")
        path.symlink_to(target)

    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose(["127.0.0.1:5000:5000"]),
            container_port=5000,
            path=path,
        )

    assert error.value.reason == "artifact_path_exists"


def test_container_port_requires_exact_integer_in_valid_range(tmp_path: Path) -> None:
    for container_port in (True, 0, 65536, "5000", 5000.0):
        with pytest.raises(CompatibilityError) as error:
            write_loopback_compatibility_overlay(
                _compose(["127.0.0.1:5000:5000"]),
                container_port=cast(int, container_port),
                path=tmp_path / f"invalid-{container_port!s}.overlay.yaml",
            )
        assert error.value.reason == "invalid_container_port"


def test_persisted_sha256_matches_exact_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "compatibility.overlay.yaml"

    artifact = write_loopback_compatibility_overlay(
        _compose(["127.0.0.1:5000:5000"]), container_port=5000, path=path
    )

    assert artifact is not None
    assert artifact.path == path
    assert artifact.sha256 == sha256(path.read_bytes()).hexdigest()


def test_path_replaced_after_close_before_first_check_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "compatibility.overlay.yaml"
    replacement = tmp_path / "replacement.overlay.yaml"
    replacement.write_text("replacement", encoding="utf-8")
    original_lstat = Path.lstat
    replaced = False

    def replace_before_first_path_check(candidate: Path) -> object:
        nonlocal replaced
        if candidate == path and not replaced:
            replaced = True
            path.unlink()
            replacement.replace(path)
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", replace_before_first_path_check)

    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose(["127.0.0.1:5000:5000"]),
            container_port=5000,
            path=path,
        )

    assert replaced
    assert error.value.reason == "artifact_persistence_failed"


@pytest.mark.parametrize("field", ["target", "published", "host_ip"])
def test_tagged_long_binding_values_fail_closed(tmp_path: Path, field: str) -> None:
    value = {
        "target": "5000",
        "published": "5000",
        "host_ip": "127.0.0.1",
    }
    value[field] = f"!override {value[field]}"
    yaml = YAML(typ="rt", pure=True)
    compose = yaml.load(
        "services:\n"
        "  app:\n"
        "    image: example/app\n"
        "    ports:\n"
        "      - target: "
        f"{value['target']}\n"
        "        published: "
        f"{value['published']}\n"
        "        host_ip: "
        f"{value['host_ip']}\n"
    )

    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            compose,
            container_port=5000,
            path=tmp_path / f"tagged-{field}.overlay.yaml",
        )

    assert error.value.reason == "tagged_port"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("target", "5000-5001", "port_range"),
        ("published", "5000-5001", "port_range"),
        ("protocol", "sctp", "invalid_protocol"),
    ],
)
def test_invalid_long_binding_values_fail_closed(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    binding: dict[str, object] = {
        "target": 5000,
        "published": 5000,
        "host_ip": "127.0.0.1",
    }
    binding[field] = value

    with pytest.raises(CompatibilityError) as error:
        write_loopback_compatibility_overlay(
            _compose([binding]),
            container_port=5000,
            path=tmp_path / f"invalid-{field}.overlay.yaml",
        )

    assert error.value.reason == reason


def test_same_long_published_port_with_different_protocol_can_coexist(
    tmp_path: Path,
) -> None:
    compose = _compose(
        [
            {
                "target": 5000,
                "published": 5000,
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
            },
            {
                "target": 5001,
                "published": 5000,
                "host_ip": "0.0.0.0",
                "protocol": "udp",
            },
        ]
    )

    artifact = write_loopback_compatibility_overlay(
        compose,
        container_port=5000,
        path=tmp_path / "different-protocol.overlay.yaml",
    )

    assert artifact is not None
