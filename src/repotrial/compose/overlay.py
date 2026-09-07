"""Compose override artifact generation for one audited mutation candidate."""

import stat
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tag import Tag

from repotrial.compose.mutations import MutationError
from repotrial.compose.parser import (
    _MAX_OPEN_COLLECTIONS,
    ComposeParseError,
    canonical_compose_json,
    load_compose,
)


def write_overlay(
    base: dict[str, object], candidate: dict[str, object], path: Path
) -> Path:
    """Write the minimal one-service Compose override for ``candidate``."""
    target = _single_changed_service(base, candidate)
    base_services = base["services"]
    candidate_services = candidate["services"]
    assert isinstance(base_services, dict)
    assert isinstance(candidate_services, dict)
    base_service = base_services[target]
    candidate_service = candidate_services[target]
    assert isinstance(base_service, Mapping)
    assert isinstance(candidate_service, Mapping)
    changes = _service_changes(base_service, candidate_service)
    overlay = CommentedMap({"services": CommentedMap({target: changes})})
    _write_new_regular_file(overlay, path)
    return path


def write_cumulative_overlay(
    base: dict[str, object], candidate: dict[str, object], path: Path
) -> Path:
    """Write a cumulative override for every changed existing service.

    Unlike :func:`write_overlay`, this is intended for the terminal artifact and
    may contain more than one service.  The same service-only diff contract is
    retained: top-level values and the service set must be unchanged.
    """
    overlay = cumulative_overlay(base, candidate)
    _write_new_regular_file(overlay, path)
    try:
        written = load_compose(path)
        if canonical_compose_json(written) != canonical_compose_json(overlay):
            raise MutationError("cumulative overlay write verification failed")
    except (ComposeParseError, MutationError):
        _remove_new_file(path)
        raise
    return path


def cumulative_overlay(
    base: dict[str, object], candidate: dict[str, object]
) -> CommentedMap:
    """Build the validated cumulative service-only override in memory."""
    changed = _changed_services(base, candidate)
    if not changed:
        raise MutationError("candidate must change at least one existing service")
    base_services = base["services"]
    candidate_services = candidate["services"]
    assert isinstance(base_services, dict)
    assert isinstance(candidate_services, dict)
    overlay = CommentedMap({"services": CommentedMap()})
    for service in changed:
        base_service = base_services[service]
        candidate_service = candidate_services[service]
        assert isinstance(base_service, Mapping)
        assert isinstance(candidate_service, Mapping)
        overlay["services"][service] = _service_changes(base_service, candidate_service)
    return overlay


def _single_changed_service(base: object, candidate: object) -> str:
    changed = _changed_services(base, candidate)
    if len(changed) != 1:
        raise MutationError("candidate must change one existing service")
    return changed[0]


def _changed_services(base: object, candidate: object) -> list[str]:
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        raise MutationError("invalid compose mapping")
    try:
        canonical_compose_json(dict(base))
        canonical_compose_json(dict(candidate))
    except ComposeParseError as error:
        raise MutationError("invalid compose mapping") from error
    if set(base) != set(candidate) or "services" not in base:
        raise MutationError("invalid candidate")
    for key in base:
        if key != "services" and not _same_value(base[key], candidate[key]):
            raise MutationError("candidate changes outside services")
    base_services = base["services"]
    candidate_services = candidate["services"]
    if not isinstance(base_services, dict) or not isinstance(candidate_services, dict):
        raise MutationError("invalid services")
    if set(base_services) != set(candidate_services):
        raise MutationError("candidate changes services")
    if any(
        not isinstance(service, str)
        or not isinstance(base_services[service], dict)
        or not isinstance(candidate_services[service], dict)
        for service in base_services
    ):
        raise MutationError("candidate services must be mappings")
    return [
        service
        for service in base_services
        if not _same_value(base_services[service], candidate_services[service])
    ]


def _service_changes(
    base: Mapping[object, object], candidate: Mapping[object, object], depth: int = 0
) -> CommentedMap:
    if depth >= _MAX_OPEN_COLLECTIONS:
        raise MutationError("overlay nesting limit")
    changes = CommentedMap()
    for key in base:
        if not isinstance(key, str):
            raise MutationError("invalid service")
        if key not in candidate:
            changes[key] = TaggedScalar(value="null", tag="!reset")
    for key, candidate_value in candidate.items():
        if not isinstance(key, str):
            raise MutationError("invalid service")
        base_value = base.get(key)
        if key in base and _same_value(base_value, candidate_value):
            continue
        if (
            key in base
            and isinstance(base_value, Mapping)
            and isinstance(candidate_value, Mapping)
            and _yaml_tag(candidate_value) is None
        ):
            changes[key] = _service_changes(base_value, candidate_value, depth + 1)
            continue
        changes[key] = _override_value(candidate_value)
    if not changes:
        raise MutationError("candidate unchanged")
    return changes


def _yaml_tag(value: object) -> str | None:
    tag_value = getattr(getattr(value, "tag", None), "value", None)
    return tag_value if isinstance(tag_value, str) else None


def _override_value(value: object) -> object:
    copied = deepcopy(value)
    if isinstance(copied, list):
        sequence = CommentedSeq(copied)
        sequence.yaml_set_ctag(Tag(suffix="!override"))
        return sequence
    return copied


def _same_value(left: object, right: object) -> bool:
    try:
        return canonical_compose_json({"value": left}) == canonical_compose_json(
            {"value": right}
        )
    except ComposeParseError as error:
        raise MutationError("invalid compose mapping") from error


def _write_new_regular_file(overlay: Mapping[object, object], path: Path) -> None:
    yaml = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    try:
        parent_stat = path.parent.lstat()
        target_stat = path.lstat() if path.exists() or path.is_symlink() else None
    except OSError as error:
        raise MutationError("invalid overlay path") from error
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise MutationError("invalid overlay path")
    if target_stat is not None:
        raise MutationError("overlay path exists")
    created = False
    try:
        with path.open("x", encoding="utf-8") as output:
            created = True
            yaml.dump(overlay, output)
    except FileExistsError as error:
        raise MutationError("overlay path exists") from error
    except OSError as error:
        if created:
            _remove_new_file(path)
        raise MutationError("invalid overlay path") from error
    except (
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        YAMLError,
    ):
        if created:
            _remove_new_file(path)
        raise


def _remove_new_file(path: Path) -> None:
    try:
        target_stat = path.lstat()
    except OSError:
        return
    if stat.S_ISREG(target_stat.st_mode) and not stat.S_ISLNK(target_stat.st_mode):
        path.unlink(missing_ok=True)
