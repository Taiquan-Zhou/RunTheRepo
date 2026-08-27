"""Deterministic assertion evaluation for bounded HTTP responses."""

import json
import re
from collections.abc import Mapping
from typing import Any

from repotrial.domain.models import JourneyAssertion

_DOTTED_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")


def evaluate_assertion(
    assertion: JourneyAssertion, *, status_code: int, text: str
) -> tuple[bool, str | None]:
    """Return a deterministic outcome and, on failure, its safe category."""
    if assertion.kind == "status_code":
        if assertion.target != "response.status" or type(assertion.expected) is not int:
            return False, "invalid_assertion"
        return assertion.expected == status_code, "assertion:status_code"

    if assertion.kind == "text_contains":
        if assertion.target != "response.text" or not isinstance(
            assertion.expected, str
        ):
            return False, "invalid_assertion"
        return assertion.expected in text, "assertion:text_contains"

    if assertion.kind == "json_path_equals":
        if not _DOTTED_PATH.fullmatch(assertion.target):
            return False, "invalid_json_path"
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            return False, "malformed_json"
        current: object = parsed
        for part in assertion.target.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False, "json_path_missing"
            current = current[part]
        return current == assertion.expected, "assertion:json_path_equals"

    return False, "invalid_assertion"
