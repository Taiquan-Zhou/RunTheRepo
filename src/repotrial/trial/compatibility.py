"""Verify compatibility artifacts in a managed sandbox clone."""

from __future__ import annotations

import posixpath
import re
import unicodedata

from repotrial.compose.compatibility import CompatibilityError
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.lifecycle import CleanupError

_GUEST_VERIFY_TIMEOUT_S = 5
_MAX_GUEST_VERIFY_OUTPUT_BYTES = 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_GUEST_RELATIVE_PATH_BYTES = 4096


async def verify_guest_compatibility_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    relative_path: str,
    expected_sha256: str,
) -> None:
    """Verify the cloned compatibility artifact before any workload command."""

    _validate_identity(relative_path, expected_sha256)
    try:
        result = await provider.exec(
            sandbox_id,
            ["sha256sum", "--", relative_path],
            timeout_s=_GUEST_VERIFY_TIMEOUT_S,
        )
    except CleanupError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise CompatibilityError("guest_verify_failed") from None
    if not isinstance(result, ExecResult):
        raise CompatibilityError("guest_output_malformed")

    stdout = _bounded_output(result.stdout)
    stderr = _bounded_output(result.stderr)
    if stdout is None or stderr is None:
        raise CompatibilityError("guest_output_oversize")
    if type(result.exit_code) is not int:
        raise CompatibilityError("guest_output_malformed")
    if result.exit_code != 0:
        if stdout:
            raise CompatibilityError("guest_output_malformed")
        raise CompatibilityError("guest_artifact_missing")
    if stderr:
        raise CompatibilityError("guest_output_malformed")

    digest, reported_path = _parse_digest_output(stdout)
    if reported_path != relative_path:
        raise CompatibilityError("guest_output_malformed")
    if digest != expected_sha256:
        raise CompatibilityError("guest_hash_mismatch")


def _validate_identity(relative_path: str, expected_sha256: str) -> None:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path.encode("utf-8", errors="surrogatepass"))
        > _MAX_GUEST_RELATIVE_PATH_BYTES
        or relative_path.startswith("/")
        or "\\" in relative_path
        or posixpath.normpath(relative_path) != relative_path
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        or any(
            unicodedata.category(character).startswith("C")
            for character in relative_path
        )
    ):
        raise CompatibilityError("guest_path_invalid")
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise CompatibilityError("identity_incomplete")


def _bounded_output(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > _MAX_GUEST_VERIFY_OUTPUT_BYTES:
        return None
    try:
        if len(value.encode("utf-8", errors="strict")) > _MAX_GUEST_VERIFY_OUTPUT_BYTES:
            return None
    except UnicodeError:
        return None
    return value


def _parse_digest_output(stdout: str) -> tuple[str, str]:
    if not stdout.endswith("\n") or stdout.count("\n") != 1:
        raise CompatibilityError("guest_output_malformed")
    digest, separator, reported_path = stdout[:-1].partition("  ")
    if (
        separator != "  "
        or _SHA256_PATTERN.fullmatch(digest) is None
        or not reported_path
    ):
        raise CompatibilityError("guest_output_malformed")
    return digest, reported_path
