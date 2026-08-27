from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from ruamel.yaml.comments import TaggedScalar

from repotrial.compose.mutations import MutationError, apply_mutation
from repotrial.compose.overlay import write_overlay
from repotrial.compose.parser import canonical_compose_json, load_compose
from repotrial.domain.enums import MutationType
from repotrial.domain.models import Mutation


def _mutation(kind: MutationType, **overrides: object) -> Mutation:
    data: dict[str, object] = {
        "mutation_id": f"mutation-{kind.value}",
        "type": kind,
        "service": "app",
    }
    data.update(overrides)
    return Mutation.model_validate(data)


def _load_tagged_service(tmp_path: Path, name: str, body: str) -> dict[str, object]:
    path = tmp_path / name
    path.write_text(f"services:\n  app:\n{body}", encoding="utf-8")
    return load_compose(path)


@pytest.mark.parametrize(
    ("kind", "definition", "expected"),
    [
        (MutationType.SET_NON_ROOT, {"user": "root"}, {"user": "65532:65532"}),
        (
            MutationType.DROP_ALL_CAPS,
            {"cap_drop": ["NET_RAW"]},
            {"cap_drop": ["ALL"]},
        ),
        (MutationType.SET_READ_ONLY, {"read_only": False}, {"read_only": True}),
        (
            MutationType.ADD_TMPFS,
            {"tmpfs": ["/run", "/cache"]},
            {"tmpfs": ["/run", "/cache", "/tmp"]},
        ),
        (
            MutationType.DROP_PRIVILEGED,
            {"privileged": True},
            {"privileged": False},
        ),
        (
            MutationType.REMOVE_DOCKER_SOCKET,
            {
                "volumes": [
                    "./settings:/settings:ro",
                    "/var/run/docker.sock:/socket:ro",
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                        "read_only": True,
                    },
                ]
            },
            {"volumes": ["./settings:/settings:ro"]},
        ),
        (MutationType.BRIDGE_NETWORK, {"network_mode": "host"}, {}),
    ],
)
def test_apply_mutation_performs_exactly_one_supported_service_change(
    kind: MutationType, definition: dict[str, object], expected: dict[str, object]
) -> None:
    """Removing a mutation's effect must make this consumer-visible contract fail."""
    base = {
        "name": "demo",
        "services": {
            "app": {"image": "nginx", **definition},
            "db": {"image": "postgres"},
        },
    }
    before = deepcopy(base)

    candidate = apply_mutation(base, _mutation(kind))

    assert candidate["services"]["app"] == {"image": "nginx", **expected}
    assert candidate["services"]["db"] == base["services"]["db"]
    assert candidate["name"] == "demo"
    assert base == before


@pytest.mark.parametrize(
    ("kind", "definition"),
    [
        (MutationType.SET_NON_ROOT, {"user": "65532:65532"}),
        (MutationType.DROP_ALL_CAPS, {"cap_drop": ["ALL"]}),
        (MutationType.SET_READ_ONLY, {"read_only": True}),
        (MutationType.ADD_TMPFS, {"tmpfs": ["/tmp"]}),
        (MutationType.DROP_PRIVILEGED, {"privileged": False}),
        (MutationType.REMOVE_DOCKER_SOCKET, {"volumes": ["./data:/data"]}),
        (MutationType.BRIDGE_NETWORK, {"network_mode": "bridge"}),
    ],
)
def test_apply_mutation_rejects_already_satisfied_or_inapplicable_targets(
    kind: MutationType, definition: dict[str, object]
) -> None:
    """Accepting a no-op would create a false hardening candidate."""
    base = {"services": {"app": definition}}

    with pytest.raises(MutationError):
        apply_mutation(base, _mutation(kind))


def test_apply_mutation_preserves_base_canonical_material_and_detaches_alias_target(
    tmp_path: Path,
) -> None:
    """Mutating a shared alias must not silently harden its non-target sibling."""
    source = tmp_path / "base.yml"
    source.write_text(
        "# source comment\nservices:\n  base: &shared\n    image: nginx\n    read_only: false\n  app: *shared\n",
        encoding="utf-8",
    )
    base = load_compose(source)
    material = canonical_compose_json(base)
    base_services = base["services"]
    assert base_services["app"] is base_services["base"]

    candidate = apply_mutation(base, _mutation(MutationType.SET_READ_ONLY))

    assert canonical_compose_json(base) == material
    assert base_services["app"] is base_services["base"]
    assert candidate["services"]["app"] is not candidate["services"]["base"]
    assert candidate["services"]["app"]["read_only"] is True
    assert candidate["services"]["base"]["read_only"] is False
    assert candidate.ca.comment is not None


def test_apply_mutation_has_deterministic_changed_canonical_material() -> None:
    """A nondeterministic candidate would invalidate reproducible experiment identity."""
    base = {"services": {"app": {"image": "nginx", "read_only": False}}}

    first = apply_mutation(base, _mutation(MutationType.SET_READ_ONLY))
    second = apply_mutation(base, _mutation(MutationType.SET_READ_ONLY))

    assert canonical_compose_json(first) == canonical_compose_json(second)
    assert canonical_compose_json(first) != canonical_compose_json(base)


def test_add_tmpfs_preserves_other_entries_and_deduplicates_tmp() -> None:
    """Leaving duplicate /tmp mounts would make the candidate's intent ambiguous."""
    base = {"services": {"app": {"tmpfs": ["/run", "/tmp", "/tmp"]}}}

    candidate = apply_mutation(base, _mutation(MutationType.ADD_TMPFS))

    assert candidate["services"]["app"]["tmpfs"] == ["/run", "/tmp"]


@pytest.mark.parametrize("entry", ["/run", "/run:size=64m"])
def test_add_tmpfs_normalizes_a_valid_scalar_entry_without_losing_options(
    entry: str,
) -> None:
    """Rejecting Compose's scalar tmpfs form would discard a valid mount option."""
    base = {"services": {"app": {"tmpfs": entry}}}

    candidate = apply_mutation(base, _mutation(MutationType.ADD_TMPFS))

    assert candidate["services"]["app"]["tmpfs"] == [entry, "/tmp"]


def test_add_tmpfs_rejects_an_effectively_present_scalar_tmp() -> None:
    """Creating a candidate for an existing scalar /tmp mount is a semantic no-op."""
    base = {"services": {"app": {"tmpfs": "/tmp"}}}

    with pytest.raises(MutationError):
        apply_mutation(base, _mutation(MutationType.ADD_TMPFS))


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        (MutationType.SET_NON_ROOT, "    user: !override '65532:65532'\n"),
        (MutationType.DROP_ALL_CAPS, "    cap_drop: !override [ALL]\n"),
        (MutationType.SET_READ_ONLY, "    read_only: !override true\n"),
        (MutationType.ADD_TMPFS, "    tmpfs: !override /tmp\n"),
        (MutationType.DROP_PRIVILEGED, ""),
        (MutationType.DROP_PRIVILEGED, "    privileged: !reset null\n"),
        (MutationType.DROP_PRIVILEGED, "    privileged: !override false\n"),
        (MutationType.REMOVE_DOCKER_SOCKET, "    volumes: !reset []\n"),
    ],
)
def test_apply_mutation_rejects_effective_noop_values(
    tmp_path: Path, kind: MutationType, body: str
) -> None:
    """Ignoring Compose tags/defaults would create candidates with no semantic change."""
    base = _load_tagged_service(tmp_path, f"{kind.value}-{len(body)}.yml", body)

    with pytest.raises(MutationError):
        apply_mutation(base, _mutation(kind))


def test_bridge_network_removes_an_effective_override_host_declaration(
    tmp_path: Path,
) -> None:
    """A tagged host network is still host networking and must be removed."""
    base = _load_tagged_service(
        tmp_path, "override-host.yml", "    network_mode: !override host\n"
    )

    candidate = apply_mutation(base, _mutation(MutationType.BRIDGE_NETWORK))

    assert "network_mode" not in candidate["services"]["app"]


def test_apply_mutation_rejects_unknown_tag_values_before_mutating() -> None:
    """Treating an unknown YAML tag as a scalar could turn ambiguity into a false fix."""
    unknown = TaggedScalar("true", tag="!unknown")
    base = {"services": {"app": {"privileged": unknown}}}

    with pytest.raises(MutationError):
        apply_mutation(base, _mutation(MutationType.DROP_PRIVILEGED))

    assert base["services"]["app"]["privileged"] is unknown


@pytest.mark.parametrize(
    "base, mutation",
    [
        (
            {"services": {"app": {}}},
            _mutation(MutationType.SET_READ_ONLY, mutation_id=""),
        ),
        ({"services": []}, _mutation(MutationType.SET_READ_ONLY)),
        ({"services": {1: {}}}, _mutation(MutationType.SET_READ_ONLY)),
        ({"services": {"app": []}}, _mutation(MutationType.SET_READ_ONLY)),
        (
            {"services": {"app": {}}},
            _mutation(MutationType.SET_READ_ONLY, params={"x": 1}),
        ),
        ({"services": {"other": {}}}, _mutation(MutationType.SET_READ_ONLY)),
        (
            {"services": {"app": {"tmpfs": 7}}},
            _mutation(MutationType.ADD_TMPFS),
        ),
    ],
)
def test_apply_mutation_fails_closed_for_invalid_contracts(
    base: dict[str, object], mutation: Mutation
) -> None:
    """Malformed plans must not yield a partially changed Compose document."""
    before = deepcopy(base)

    with pytest.raises(MutationError):
        apply_mutation(base, mutation)

    assert base == before


def test_remove_docker_socket_uses_existing_short_and_long_bind_grammar() -> None:
    """A grammar drift could leave Windows or read-only socket access behind."""
    retained = [
        "named-volume:/data",
        "./cache:/cache",
        {"type": "volume", "source": "cache", "target": "/cache2"},
    ]
    base = {
        "services": {
            "app": {
                "volumes": [
                    retained[0],
                    "/var/run/docker.sock:/socket:rw,z",
                    retained[1],
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/docker",
                    },
                    retained[2],
                ]
            }
        }
    }

    candidate = apply_mutation(base, _mutation(MutationType.REMOVE_DOCKER_SOCKET))

    assert candidate["services"]["app"]["volumes"] == retained


def _merge_service(
    base: dict[str, object], overlay: dict[str, object]
) -> dict[str, object]:
    """Tiny test-only checker for the recursive merge rules exercised here."""
    result = deepcopy(base)
    for key, value in overlay.items():
        tag = getattr(getattr(value, "tag", None), "value", None)
        if tag == "!reset":
            result.pop(key, None)
        elif tag == "!override":
            result[key] = list(value)
        elif isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge_service(result[key], value)
        else:
            result[key] = value
    return result


@pytest.mark.parametrize(
    "kind",
    [
        MutationType.SET_READ_ONLY,
        MutationType.DROP_ALL_CAPS,
        MutationType.BRIDGE_NETWORK,
    ],
)
def test_write_overlay_is_minimal_loadable_and_merges_to_candidate(
    tmp_path: Path, kind: MutationType
) -> None:
    """Wrong tags or unrelated fields would produce different Compose semantics."""
    definitions: dict[MutationType, dict[str, object]] = {
        MutationType.SET_READ_ONLY: {"image": "nginx", "read_only": False},
        MutationType.DROP_ALL_CAPS: {"image": "nginx", "cap_drop": ["NET_RAW"]},
        MutationType.BRIDGE_NETWORK: {"image": "nginx", "network_mode": "host"},
    }
    base = {"services": {"app": definitions[kind], "db": {"image": "postgres"}}}
    candidate = apply_mutation(base, _mutation(kind))
    output = tmp_path / f"{kind.value}.yaml"

    result = write_overlay(base, candidate, output)
    overlay = load_compose(output)
    app_overlay = overlay["services"]["app"]

    assert result == output
    assert set(overlay) == {"services"}
    assert set(overlay["services"]) == {"app"}
    assert (
        _merge_service(base["services"]["app"], app_overlay)
        == candidate["services"]["app"]
    )
    if kind is MutationType.DROP_ALL_CAPS:
        assert getattr(app_overlay["cap_drop"].tag, "value", None) == "!override"
    if kind is MutationType.BRIDGE_NETWORK:
        assert getattr(app_overlay["network_mode"].tag, "value", None) == "!reset"


def test_write_overlay_recursively_merges_mapping_add_change_and_removal(
    tmp_path: Path,
) -> None:
    """A shallow overlay merge would keep the removed nested environment entry."""
    base = {
        "services": {
            "app": {
                "image": "nginx",
                "environment": {
                    "KEEP": "base",
                    "CHANGE": "before",
                    "REMOVE": "obsolete",
                },
            }
        }
    }
    candidate = {
        "services": {
            "app": {
                "image": "nginx",
                "environment": {"KEEP": "base", "CHANGE": "after", "ADD": "new"},
            }
        }
    }
    output = tmp_path / "recursive-overlay.yml"

    write_overlay(base, candidate, output)
    overlay = load_compose(output)

    assert (
        _merge_service(base["services"]["app"], overlay["services"]["app"])
        == candidate["services"]["app"]
    )


def test_write_overlay_rejects_non_single_service_candidates_and_preserves_inputs(
    tmp_path: Path,
) -> None:
    """An overlay that can alter a peer service violates the mutation boundary."""
    base = {"services": {"app": {"read_only": False}, "db": {"image": "postgres"}}}
    candidate = apply_mutation(base, _mutation(MutationType.SET_READ_ONLY))
    candidate["services"]["db"]["image"] = "attacker"
    base_material = canonical_compose_json(base)
    candidate_before = deepcopy(candidate)

    with pytest.raises(MutationError):
        write_overlay(base, candidate, tmp_path / "invalid.yml")

    assert canonical_compose_json(base) == base_material
    assert candidate == candidate_before


def test_write_overlay_creates_only_a_new_regular_file_without_input_mutation(
    tmp_path: Path,
) -> None:
    """Overwriting an existing path could replace an audit artifact or follow a link."""
    base = {"services": {"app": {"read_only": False}}}
    candidate = apply_mutation(base, _mutation(MutationType.SET_READ_ONLY))
    base_before = deepcopy(base)
    candidate_before = deepcopy(candidate)
    output = tmp_path / "overlay.yml"

    assert write_overlay(base, candidate, output) == output
    assert output.is_file()
    assert base == base_before
    assert candidate == candidate_before
    with pytest.raises(MutationError):
        write_overlay(base, candidate, output)

    link = tmp_path / "overlay-link.yml"
    try:
        link.symlink_to(output)
    except OSError:
        pytest.skip("symlinks unavailable on this Windows test host")
    with pytest.raises(MutationError):
        write_overlay(base, candidate, link)
