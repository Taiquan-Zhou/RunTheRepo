"""Deterministic assertion evaluation for bounded HTTP responses."""

import json
import re
from collections.abc import Mapping
from typing import Any

from repotrial.domain.models import JourneyAssertion

_DOTTED_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")


def validate_assertion(assertion: JourneyAssertion) -> str | None:
    if assertion.kind == "status_code":
        if assertion.target != "response.status" or type(assertion.expected) is not int:
            return "invalid_assertion"
        return None
    if assertion.kind == "text_contains":
        if assertion.target != "response.text" or not isinstance(
            assertion.expected, str
        ):
            return "invalid_assertion"
        return None
    if assertion.kind == "json_path_equals":
        if not _DOTTED_PATH.fullmatch(assertion.target):
            return "invalid_json_path"
        return None
    return "invalid_assertion"


def _json_equals(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        if not isinstance(expected, dict) or actual.keys() != expected.keys():
            return False
        return all(_json_equals(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list):
        if not isinstance(expected, list) or len(actual) != len(expected):
            return False
        return all(
            _json_equals(item, expected[index]) for index, item in enumerate(actual)
        )
    return actual == expected


def evaluate_assertion(
    assertion: JourneyAssertion, *, status_code: int, text: str
) -> tuple[bool, str | None]:
    """Return a deterministic outcome and, on failure, its safe category."""
    validation_error = validate_assertion(assertion)
    if validation_error is not None:
        return False, validation_error
    if assertion.kind == "status_code":
        return assertion.expected == status_code, "assertion:status_code"

    if assertion.kind == "text_contains":
        return assertion.expected in text, "assertion:text_contains"

    if assertion.kind == "json_path_equals":
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            return False, "malformed_json"
        current: object = parsed
        for part in assertion.target.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False, "json_path_missing"
            current = current[part]
        return _json_equals(current, assertion.expected), "assertion:json_path_equals"
    raise AssertionError("validated assertion kind was not evaluated")
