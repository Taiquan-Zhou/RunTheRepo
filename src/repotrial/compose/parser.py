"""Safe Compose YAML parsing and deterministic canonical hash material."""

import json
import math
import os
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
from ruamel.yaml.nodes import ScalarNode

_MAX_COMPOSE_BYTES = 4 * 1024 * 1024
_MAX_SEMANTIC_NODES = 100_000
_MAX_INTEGER_DIGITS = 4_300
# The root mapping counts toward this simultaneous collection limit.
_MAX_OPEN_COLLECTIONS = 128
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
    try:
        source = _read_compose_source(path)
        yaml = _new_round_trip_yaml()
        _validate_yaml_events(yaml, source)
        loaded = yaml.load(source)
        if not isinstance(loaded, dict):
            raise _ComposeValidationError("root_not_mapping")
        _canonical_tree(loaded, yaml)
    except _ComposeValidationError as error:
        raise ComposeParseError(error.reason) from None
    except (OverflowError, RecursionError, UnicodeError, ValueError, YAMLError):
        raise ComposeParseError("invalid_yaml") from None
    return cast(dict[str, object], loaded)


def canonical_compose_json(compose: dict[str, object]) -> str:
    """Return compact deterministic JSON hash material for a Compose mapping."""
    try:
        canonical_tree = _canonical_tree(compose, _new_round_trip_yaml())
    except _ComposeValidationError as error:
        raise ComposeParseError(error.reason) from None
    except (OverflowError, RecursionError, UnicodeError, ValueError, YAMLError):
        raise ComposeParseError("invalid_compose") from None
    try:
        return json.dumps(
            canonical_tree,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (OverflowError, RecursionError, UnicodeError, ValueError):
        raise ComposeParseError("invalid_compose") from None


def _new_round_trip_yaml() -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    return yaml


def _read_compose_source(path: Path) -> bytes:
    try:
        with path.open("rb") as source_file:
            handle_stat = os.fstat(source_file.fileno())
            if not _is_regular_file(handle_stat):
                raise ComposeParseError("invalid_path")
            handle_identity = _file_identity(handle_stat)
            _require_path_identity(path, handle_identity)
            if handle_stat.st_size > _MAX_COMPOSE_BYTES:
                raise ComposeParseError("file_too_large")
            source = source_file.read(_MAX_COMPOSE_BYTES + 1)
            if len(source) > _MAX_COMPOSE_BYTES:
                raise ComposeParseError("file_too_large")
            final_handle_stat = os.fstat(source_file.fileno())
            if (
                not _is_regular_file(final_handle_stat)
                or _file_identity(final_handle_stat) != handle_identity
            ):
                raise ComposeParseError("invalid_path")
            if final_handle_stat.st_size > _MAX_COMPOSE_BYTES:
                raise ComposeParseError("file_too_large")
            _require_path_identity(path, handle_identity)
            return source
    except OSError:
        raise ComposeParseError("invalid_path") from None


def _require_path_identity(path: Path, expected: tuple[int, int, int]) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise ComposeParseError("invalid_path") from None
    if not _is_regular_file(path_stat) or _file_identity(path_stat) != expected:
        raise ComposeParseError("invalid_path")


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int]:
    if stat_result.st_ino == 0:
        raise ComposeParseError("invalid_path")
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat.S_IFMT(stat_result.st_mode),
    )


def _is_regular_file(stat_result: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(stat_result, "st_file_attributes", 0)
    return stat.S_ISREG(stat_result.st_mode) and not (
        reparse_flag and file_attributes & reparse_flag
    )


def _validate_yaml_events(yaml: YAML, source: bytes) -> None:
    document_count = 0
    semantic_node_count = 0
    collection_anchors: list[str | None] = []
    active_collection_anchors: dict[str, int] = {}
    for event in yaml.parse(source):
        if isinstance(event, DocumentStartEvent):
            document_count += 1
        if isinstance(
            event, (AliasEvent, MappingStartEvent, ScalarEvent, SequenceStartEvent)
        ):
            semantic_node_count += 1
            if semantic_node_count > _MAX_SEMANTIC_NODES:
                raise _ComposeValidationError("resource_limit")
        if isinstance(event, (MappingStartEvent, ScalarEvent, SequenceStartEvent)):
            _validate_event_tag(event.tag)
        if isinstance(event, ScalarEvent):
            _validate_scalar_event(event, yaml)
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            if len(collection_anchors) >= _MAX_OPEN_COLLECTIONS:
                raise _ComposeValidationError("resource_limit")
            collection_anchors.append(event.anchor)
            if event.anchor is not None:
                active_collection_anchors[event.anchor] = (
                    active_collection_anchors.get(event.anchor, 0) + 1
                )
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            completed_anchor = collection_anchors.pop()
            if completed_anchor is not None:
                active_count = active_collection_anchors[completed_anchor]
                if active_count == 1:
                    del active_collection_anchors[completed_anchor]
                else:
                    active_collection_anchors[completed_anchor] = active_count - 1
        elif (
            isinstance(event, AliasEvent)
            and event.anchor is not None
            and event.anchor in active_collection_anchors
        ):
            raise _ComposeValidationError("cyclic_alias")
    if document_count != 1:
        raise _ComposeValidationError("document_count")


def _validate_event_tag(tag: str | None) -> None:
    if tag is not None and tag not in _ALLOWED_COMPOSE_TAGS:
        raise _ComposeValidationError("unsupported_tag")


def _validate_scalar_event(event: ScalarEvent, yaml: YAML) -> None:
    if event.style is not None:
        return
    resolved_tag = yaml.resolver.resolve(ScalarNode, event.value, (True, False))
    if str(resolved_tag) != "tag:yaml.org,2002:int":
        return
    if sum(character.isdecimal() for character in event.value) > _MAX_INTEGER_DIGITS:
        raise _ComposeValidationError("numeric_limit")


def _canonical_tree(value: object, yaml: YAML) -> dict[str, object]:
    active_nodes: set[int] = set()
    budget = _NodeBudget()
    canonical = _canonical_node(value, active_nodes, budget, yaml)
    if not isinstance(value, dict) or not isinstance(canonical, dict):
        raise _ComposeValidationError("root_not_mapping")
    return canonical


def _canonical_node(
    value: object, active_nodes: set[int], budget: _NodeBudget, yaml: YAML
) -> dict[str, object]:
    budget.consume()
    if isinstance(value, TaggedScalar):
        return _canonical_tagged_scalar(value, active_nodes, budget, yaml)
    if isinstance(value, dict):
        return _canonical_mapping(value, active_nodes, budget, yaml)
    if isinstance(value, list):
        return _canonical_sequence(value, active_nodes, budget, yaml)
    return _canonical_scalar(value)


def _canonical_tagged_scalar(
    value: TaggedScalar, active_nodes: set[int], budget: _NodeBudget, yaml: YAML
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
            "value": _canonical_node(
                _resolve_tagged_scalar(value, yaml), active_nodes, budget, yaml
            ),
        }
    finally:
        active_nodes.remove(identity)


def _canonical_mapping(
    value: dict[object, object], active_nodes: set[int], budget: _NodeBudget, yaml: YAML
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
                    _canonical_node(value[key], active_nodes, budget, yaml),
                ]
            )
        return {"kind": "mapping", "tag": tag, "items": items}
    finally:
        active_nodes.remove(identity)


def _canonical_sequence(
    value: list[object], active_nodes: set[int], budget: _NodeBudget, yaml: YAML
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
            "items": [
                _canonical_node(item, active_nodes, budget, yaml) for item in value
            ],
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


def _resolve_tagged_scalar(value: TaggedScalar, yaml: YAML) -> object:
    scalar_value = value.value
    if not isinstance(scalar_value, str):
        raise _ComposeValidationError("unsupported_value")
    if value.style is not None:
        return scalar_value
    resolved_tag = yaml.resolver.resolve(ScalarNode, scalar_value, (True, False))
    return yaml.constructor.construct_object(
        ScalarNode(tag=resolved_tag, value=scalar_value)
    )


def _validated_node_tag(value: object) -> str | None:
    tag = getattr(value, "tag", None)
    tag_value = getattr(tag, "value", None)
    if tag_value is None:
        return None
    if not isinstance(tag_value, str) or tag_value not in _ALLOWED_COMPOSE_TAGS:
        raise _ComposeValidationError("unsupported_tag")
    return tag_value
