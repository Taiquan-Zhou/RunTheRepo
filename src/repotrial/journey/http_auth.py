"""Bounded bearer declarations for HTTP Journeys."""

import re
from dataclasses import dataclass

_CAPTURE_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*\Z")
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_MAX_CAPTURE_PATH_LENGTH = 128
_MAX_CAPTURE_SEGMENTS = 8
_MAX_TOKEN_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class HttpAuth:
    capture_path: str | None = None
    use_bearer: bool = False


def parse_http_auth(value: object) -> HttpAuth | None:
    if type(value) is not dict or len(value) != 1:
        return None
    if "capture_bearer" in value:
        path = value["capture_bearer"]
        if (
            not isinstance(path, str)
            or len(path) > _MAX_CAPTURE_PATH_LENGTH
            or _CAPTURE_PATH.fullmatch(path) is None
            or len(path.split(".")) > _MAX_CAPTURE_SEGMENTS
        ):
            return None
        return HttpAuth(capture_path=path)
    if "use_bearer" in value and value["use_bearer"] is True:
        return HttpAuth(use_bearer=True)
    return None


def valid_bearer_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_TOKEN_LENGTH
        and _BEARER_TOKEN.fullmatch(value) is not None
    )


def extract_bearer_token(value: object, path: str) -> str | None:
    current = value
    for segment in path.split("."):
        if type(current) is not dict or segment not in current:
            return None
        current = current[segment]
    if not valid_bearer_token(current):
        return None
    assert isinstance(current, str)
    return current
