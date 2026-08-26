import json
import os
from io import StringIO
from pathlib import Path
from typing import Self

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import (
    DoubleQuotedScalarString,
    SingleQuotedScalarString,
)

from repotrial.compose import parser
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


def test_load_compose_preserves_quote_styles_without_affecting_canonical_json(
    tmp_path: Path,
) -> None:
    quoted = load_compose(
        _write_compose(
            tmp_path,
            "double: \"nginx\"\nsingle: 'nginx'\n",
        )
    )
    plain = load_compose(_write_compose(tmp_path, "value: nginx\n"))
    single = load_compose(_write_compose(tmp_path, "value: 'nginx'\n"))
    double = load_compose(_write_compose(tmp_path, 'value: "nginx"\n'))
    rendered = StringIO()
    yaml = YAML(typ="rt", pure=True)

    yaml.dump(quoted, rendered)

    assert isinstance(quoted["double"], DoubleQuotedScalarString)
    assert isinstance(quoted["single"], SingleQuotedScalarString)
    assert 'double: "nginx"' in rendered.getvalue()
    assert "single: 'nginx'" in rendered.getvalue()
    assert canonical_compose_json(plain) == canonical_compose_json(single)
    assert canonical_compose_json(single) == canonical_compose_json(double)


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


def test_load_compose_rejects_path_replaced_after_initial_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose_path = _write_compose(tmp_path, "services: {app: {image: nginx}}\n")
    attacker_path = tmp_path / "attacker.yml"
    attacker_path.write_text("services: {app: {image: attacker}}\n", encoding="utf-8")
    original_lstat = Path.lstat
    swapped = False

    def lstat_then_swap(path: Path) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(path)
        if path == compose_path and not swapped:
            swapped = True
            compose_path.unlink()
            os.symlink(attacker_path, compose_path)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_then_swap)

    with pytest.raises(ComposeParseError):
        load_compose(compose_path)


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


@pytest.mark.parametrize("tag", ["!reset", "!override"])
@pytest.mark.parametrize(
    ("plain_value", "quoted_value"),
    [
        ("null", '"null"'),
        ("true", '"true"'),
        ("1", '"1"'),
        ("1.0", '"1.0"'),
    ],
)
def test_canonical_compose_json_distinguishes_tagged_plain_scalar_types(
    tmp_path: Path, tag: str, plain_value: str, quoted_value: str
) -> None:
    plain = load_compose(_write_compose(tmp_path, f"value: {tag} {plain_value}\n"))
    quoted = load_compose(_write_compose(tmp_path, f"value: {tag} {quoted_value}\n"))

    assert canonical_compose_json(plain) != canonical_compose_json(quoted)


@pytest.mark.parametrize("tag", ["!reset", "!override"])
def test_canonical_compose_json_preserves_allowed_tags_for_all_node_kinds(
    tmp_path: Path, tag: str
) -> None:
    scalar = load_compose(_write_compose(tmp_path, f"value: {tag} text\n"))
    sequence = load_compose(_write_compose(tmp_path, f"value: {tag}\n  - text\n"))
    mapping = load_compose(_write_compose(tmp_path, f"value: {tag}\n  child: text\n"))
    single_quoted = load_compose(_write_compose(tmp_path, f"value: {tag} 'text'\n"))
    double_quoted = load_compose(_write_compose(tmp_path, f'value: {tag} "text"\n'))

    assert getattr(scalar["value"], "tag", None) is not None
    assert getattr(sequence["value"], "tag", None) is not None
    assert getattr(mapping["value"], "tag", None) is not None
    assert canonical_compose_json(scalar) != canonical_compose_json(sequence)
    assert canonical_compose_json(sequence) != canonical_compose_json(mapping)
    assert canonical_compose_json(single_quoted) == canonical_compose_json(
        double_quoted
    )


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


def test_load_compose_rejects_oversized_metadata_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose_path = tmp_path / "oversized.yml"
    compose_path.write_bytes(b"#" + b"x" * (4 * 1024 * 1024))
    original_open = Path.open
    read_calls = 0

    class NoReadHandle:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> Self:
            self._handle.__enter__()
            return self

        def __exit__(self, *arguments: object) -> None:
            self._handle.__exit__(*arguments)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            nonlocal read_calls
            read_calls += 1
            raise AssertionError(f"unexpected read size: {size}")

    def guarded_open(
        path: Path, *arguments: object, **keywords: object
    ) -> NoReadHandle:
        return NoReadHandle(original_open(path, *arguments, **keywords))

    def unexpected_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unexpected read_bytes: {path}")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "read_bytes", unexpected_read_bytes)

    with pytest.raises(ComposeParseError):
        load_compose(compose_path)

    assert read_calls == 0


def test_load_compose_uses_bounded_read_when_metadata_is_smaller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose_path = _write_compose(tmp_path, "services: {}\n")
    original_open = Path.open
    read_sizes: list[int] = []

    class BoundedReadHandle:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> Self:
            self._handle.__enter__()
            return self

        def __exit__(self, *arguments: object) -> None:
            self._handle.__exit__(*arguments)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return self._handle.read(size)

    def bounded_open(
        path: Path, *arguments: object, **keywords: object
    ) -> BoundedReadHandle:
        return BoundedReadHandle(original_open(path, *arguments, **keywords))

    def unexpected_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unexpected read_bytes: {path}")

    monkeypatch.setattr(Path, "open", bounded_open)
    monkeypatch.setattr(Path, "read_bytes", unexpected_read_bytes)

    assert load_compose(compose_path)["services"] == {}
    assert read_sizes == [4 * 1024 * 1024 + 1]


def test_load_compose_rejects_event_budget_before_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    excessive_nodes = "items: [" + ",".join("item" for _ in range(100_001)) + "]\n"
    compose_path = _write_compose(tmp_path, excessive_nodes)

    def unexpected_load(instance: object, source: object) -> object:
        raise AssertionError("unexpected constructor invocation")

    monkeypatch.setattr(parser.YAML, "load", unexpected_load)

    with pytest.raises(ComposeParseError):
        load_compose(compose_path)


def test_load_compose_rejects_pathological_integer_before_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untrusted_integer = "9" * 5_000
    compose_path = _write_compose(tmp_path, f"value: {untrusted_integer}\n")

    def unexpected_load(instance: object, source: object) -> object:
        raise AssertionError("unexpected constructor invocation")

    monkeypatch.setattr(parser.YAML, "load", unexpected_load)

    with pytest.raises(ComposeParseError) as error:
        load_compose(compose_path)

    assert untrusted_integer not in str(error.value)


def test_load_compose_sanitizes_huge_integer_exception(tmp_path: Path) -> None:
    untrusted_integer = "9" * 5_000
    compose_path = _write_compose(tmp_path, f"value: {untrusted_integer}\n")

    with pytest.raises(ComposeParseError) as load_error:
        load_compose(compose_path)

    assert untrusted_integer not in str(load_error.value)
    assert "Exceeds the limit" not in str(load_error.value)


def test_canonical_compose_json_sanitizes_deep_mapping_exception() -> None:
    nested: dict[str, object] = {"leaf": "value"}
    for _ in range(1_500):
        nested = {"child": nested}

    with pytest.raises(ComposeParseError) as canonical_error:
        canonical_compose_json(nested)

    assert "maximum recursion depth" not in str(canonical_error.value)


def test_load_compose_accepts_exact_file_size_limit(tmp_path: Path) -> None:
    compose_path = tmp_path / "exact-limit.yml"
    prefix = b"services: {}\n#"
    compose_path.write_bytes(prefix + b"x" * (4 * 1024 * 1024 - len(prefix)))

    assert load_compose(compose_path)["services"] == {}
