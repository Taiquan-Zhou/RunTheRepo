"""Safe Compose YAML parsing and deterministic canonical hash material."""

import json
import math
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

_MAX_COMPOSE_BYTES = 4 * 1024 * 1024
_MAX_SEMANTIC_NODES = 100_000
_ALLOWED_COMPOSE_TAGS = frozenset({"!reset", "!override"})


class ComposeParseError(RuntimeError):
    """A sanitized Compose parsing or canonicalization failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"compose parse failed: {reason}")


class _ComposeValidationError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


@dataclass
class _NodeBudget:
    count: int = 0

    def consume(self) -> None:
        self.count += 1
        if self.count > _MAX_SEMANTIC_NODES:
            raise _ComposeValidationError("resource_limit")


def load_compose(path: Path) -> dict[str, object]:
    """Load one safe Compose mapping without altering its round-trip metadata."""
    source = _read_compose_source(path)
    yaml = YAML(typ="rt", pure=True)
    yaml.allow_duplicate_keys = False
    try:
        _validate_yaml_events(yaml, source)
        loaded = yaml.load(source)
        if not isinstance(loaded, dict):
            raise _ComposeValidationError("root_not_mapping")
        _canonical_tree(loaded)
    except _ComposeValidationError as error:
        raise ComposeParseError(error.reason) from None
    except (RecursionError, UnicodeError, YAMLError):
        raise ComposeParseError("invalid_yaml") from None
    return cast(dict[str, object], loaded)


def canonical_compose_json(compose: dict[str, object]) -> str:
    """Return compact deterministic JSON hash material for a Compose mapping."""
    try:
        canonical_tree = _canonical_tree(compose)
    except _ComposeValidationError as error:
        raise ComposeParseError(error.reason) from None
    return json.dumps(
        canonical_tree,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_compose_source(path: Path) -> bytes:
    try:
        path_mode = path.lstat().st_mode
    except OSError:
        raise ComposeParseError("invalid_path") from None
    if not stat.S_ISREG(path_mode):
        raise ComposeParseError("invalid_path")
    try:
        source = path.read_bytes()
    except OSError:
        raise ComposeParseError("unreadable_path") from None
    if len(source) > _MAX_COMPOSE_BYTES:
        raise ComposeParseError("file_too_large")
    return source


def _validate_yaml_events(yaml: YAML, source: bytes) -> None:
    document_count = 0
    collection_anchors: list[str | None] = []
    for event in yaml.parse(source):
        if isinstance(event, DocumentStartEvent):
            document_count += 1
        if isinstance(event, (MappingStartEvent, ScalarEvent, SequenceStartEvent)):
            _validate_event_tag(event.tag)
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            collection_anchors.append(event.anchor)
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            collection_anchors.pop()
        elif isinstance(event, AliasEvent) and event.anchor in collection_anchors:
            raise _ComposeValidationError("cyclic_alias")
    if document_count != 1:
        raise _ComposeValidationError("document_count")


def _validate_event_tag(tag: str | None) -> None:
    if tag is not None and tag not in _ALLOWED_COMPOSE_TAGS:
        raise _ComposeValidationError("unsupported_tag")


def _canonical_tree(value: object) -> dict[str, object]:
    active_nodes: set[int] = set()
    budget = _NodeBudget()
    canonical = _canonical_node(value, active_nodes, budget)
    if not isinstance(value, dict) or not isinstance(canonical, dict):
        raise _ComposeValidationError("root_not_mapping")
    return canonical


def _canonical_node(
    value: object, active_nodes: set[int], budget: _NodeBudget
) -> dict[str, object]:
    budget.consume()
    if isinstance(value, TaggedScalar):
        return _canonical_tagged_scalar(value, active_nodes, budget)
    if isinstance(value, dict):
        return _canonical_mapping(value, active_nodes, budget)
    if isinstance(value, list):
        return _canonical_sequence(value, active_nodes, budget)
    return _canonical_scalar(value)


def _canonical_tagged_scalar(
    value: TaggedScalar, active_nodes: set[int], budget: _NodeBudget
) -> dict[str, object]:
    tag = _validated_node_tag(value)
    if tag is None:
        raise _ComposeValidationError("unsupported_value")
    identity = id(value)
    if identity in active_nodes:
        raise _ComposeValidationError("cyclic_alias")
    active_nodes.add(identity)
    try:
        return {
            "kind": "tagged",
            "tag": tag,
            "value": _canonical_node(value.value, active_nodes, budget),
        }
    finally:
        active_nodes.remove(identity)


def _canonical_mapping(
    value: dict[object, object], active_nodes: set[int], budget: _NodeBudget
) -> dict[str, object]:
    tag = _validated_node_tag(value)
    identity = id(value)
    if identity in active_nodes:
        raise _ComposeValidationError("cyclic_alias")
    active_nodes.add(identity)
    try:
        items: list[list[object]] = []
        keys: list[str] = []
        for key in value:
            if not isinstance(key, str):
                raise _ComposeValidationError("unsupported_mapping_key")
            keys.append(key)
        for key in sorted(keys):
            budget.consume()
            items.append(
                [
                    {"kind": "string", "value": key},
                    _canonical_node(value[key], active_nodes, budget),
                ]
            )
        return {"kind": "mapping", "tag": tag, "items": items}
    finally:
        active_nodes.remove(identity)


def _canonical_sequence(
    value: list[object], active_nodes: set[int], budget: _NodeBudget
) -> dict[str, object]:
    tag = _validated_node_tag(value)
    identity = id(value)
    if identity in active_nodes:
        raise _ComposeValidationError("cyclic_alias")
    active_nodes.add(identity)
    try:
        return {
            "kind": "sequence",
            "tag": tag,
            "items": [_canonical_node(item, active_nodes, budget) for item in value],
        }
    finally:
        active_nodes.remove(identity)


def _canonical_scalar(value: object) -> dict[str, object]:
    tag = _validated_node_tag(value)
    if value is None:
        return {"kind": "null", "tag": tag}
    if isinstance(value, bool):
        return {"kind": "boolean", "tag": tag, "value": value}
    if isinstance(value, int):
        return {"kind": "integer", "tag": tag, "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _ComposeValidationError("non_finite_float")
        return {"kind": "float", "tag": tag, "value": repr(float(value))}
    if isinstance(value, str):
        return {"kind": "string", "tag": tag, "value": value}
    raise _ComposeValidationError("unsupported_value")


def _validated_node_tag(value: object) -> str | None:
    tag = getattr(value, "tag", None)
    tag_value = getattr(tag, "value", None)
    if tag_value is None:
        return None
    if not isinstance(tag_value, str) or tag_value not in _ALLOWED_COMPOSE_TAGS:
        raise _ComposeValidationError("unsupported_tag")
    return tag_value
