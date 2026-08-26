import json
from pathlib import Path

import pytest

from repotrial.compose.parser import (
    ComposeParseError,
    canonical_compose_json,
    load_compose,
)


def _write_compose(tmp_path: Path, content: str) -> Path:
    compose_path = tmp_path / "compose.yml"
    compose_path.write_text(content, encoding="utf-8")
    return compose_path


def test_load_compose_preserves_anchor_alias_and_round_trip_information(
    tmp_path: Path,
) -> None:
    compose = load_compose(
        _write_compose(
            tmp_path,
            "# project comment\n"
            "services:\n"
            "  base: &base\n"
            "    image: nginx # image comment\n"
            "  app: *base\n",
        )
    )

    services = compose["services"]
    assert services["app"] is services["base"]
    assert services["base"].anchor.value == "base"
    assert compose.ca.comment is not None


def test_load_compose_preserves_environment_placeholder_strings(tmp_path: Path) -> None:
    compose = load_compose(
        _write_compose(
            tmp_path,
            "services:\n"
            "  app:\n"
            '    image: "${IMAGE:-nginx}"\n'
            "    environment:\n"
            "      TOKEN: ${TOKEN?required}\n",
        )
    )

    assert compose["services"]["app"]["image"] == "${IMAGE:-nginx}"
    assert compose["services"]["app"]["environment"]["TOKEN"] == "${TOKEN?required}"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "# comment only\n",
        "plain scalar\n",
        "- list\n",
        "services: {}\n---\nname: second\n",
        "services: {}\nservices: {}\n",
        "services: [\n",
    ],
)
def test_load_compose_rejects_invalid_empty_or_non_mapping_documents(
    tmp_path: Path, content: str
) -> None:
    untrusted_marker = "untrusted-value-must-not-leak"
    compose_path = _write_compose(tmp_path, f"{content}{untrusted_marker}")

    with pytest.raises(ComposeParseError) as error:
        load_compose(compose_path)

    assert untrusted_marker not in str(error.value)


def test_load_compose_rejects_explicit_python_tag_without_execution_or_leak(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "must-not-exist"
    untrusted_marker = "custom-tag-payload-must-not-leak"
    compose_path = _write_compose(
        tmp_path,
        "payload: !!python/object/apply:builtins.eval "
        "[\"__import__('pathlib').Path(r'"
        f"{sentinel.as_posix()}').write_text('executed')\"]\n"
        f"marker: {untrusted_marker}\n",
    )

    with pytest.raises(ComposeParseError) as error:
        load_compose(compose_path)

    assert not sentinel.exists()
    assert untrusted_marker not in str(error.value)


def test_load_compose_preserves_official_compose_tags_in_canonical_material(
    tmp_path: Path,
) -> None:
    tagged = load_compose(
        _write_compose(
            tmp_path,
            "services:\n"
            "  app:\n"
            "    environment: !override\n"
            "      - MODE=test\n"
            "    ports: !reset []\n",
        )
    )
    plain = load_compose(
        _write_compose(
            tmp_path,
            "services:\n  app:\n    environment:\n      - MODE=test\n    ports: []\n",
        )
    )

    assert getattr(tagged["services"]["app"]["environment"], "tag", None) is not None
    assert getattr(tagged["services"]["app"]["ports"], "tag", None) is not None
    assert canonical_compose_json(tagged) != canonical_compose_json(plain)


def test_load_compose_rejects_non_finite_scalars_and_unsupported_mapping_keys(
    tmp_path: Path,
) -> None:
    for content in ("value: .nan\n", "value: .inf\n", "1: value\n"):
        with pytest.raises(ComposeParseError):
            load_compose(_write_compose(tmp_path, content))


def test_load_compose_rejects_cyclic_aliases_and_resource_boundaries(
    tmp_path: Path,
) -> None:
    with pytest.raises(ComposeParseError):
        load_compose(_write_compose(tmp_path, "loop: &loop [*loop]\n"))

    oversized = tmp_path / "oversized.yml"
    oversized.write_bytes(b"#" + b"x" * (4 * 1024 * 1024))
    with pytest.raises(ComposeParseError):
        load_compose(oversized)

    excessive_nodes = "items: [" + ",".join("item" for _ in range(100_001)) + "]\n"
    with pytest.raises(ComposeParseError):
        load_compose(_write_compose(tmp_path, excessive_nodes))


def test_canonical_compose_json_is_stable_across_mapping_order_and_formatting(
    tmp_path: Path,
) -> None:
    first = load_compose(
        _write_compose(
            tmp_path,
            "services:\n"
            "  app:\n"
            "    image: nginx # formatting is not semantic\n"
            "name: demo\n",
        )
    )
    second = load_compose(
        _write_compose(
            tmp_path,
            '{name: demo, services: {app: {image: "nginx"}}}\n',
        )
    )

    material = canonical_compose_json(first)

    assert material == canonical_compose_json(second)
    assert material == json.dumps(
        json.loads(material), ensure_ascii=False, separators=(",", ":")
    )


def test_canonical_compose_json_distinguishes_scalar_types_sequence_order_and_tags(
    tmp_path: Path,
) -> None:
    assert canonical_compose_json({"value": "1"}) != canonical_compose_json(
        {"value": 1}
    )
    assert canonical_compose_json(
        {"items": ["first", "second"]}
    ) != canonical_compose_json({"items": ["second", "first"]})

    tagged = load_compose(_write_compose(tmp_path, "value: !reset []\n"))
    untagged = load_compose(_write_compose(tmp_path, "value: []\n"))

    assert canonical_compose_json(tagged) != canonical_compose_json(untagged)


def test_canonical_compose_json_rejects_cycles_and_unsupported_values() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ComposeParseError):
        canonical_compose_json(cyclic)
    with pytest.raises(ComposeParseError):
        canonical_compose_json({"value": object()})
    with pytest.raises(ComposeParseError):
        canonical_compose_json({"valid": True, 1: "unsupported key"})
