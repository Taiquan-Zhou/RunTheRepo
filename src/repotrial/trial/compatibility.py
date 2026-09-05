"""Verify compatibility artifacts in a managed sandbox clone."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import time
import unicodedata
from pathlib import Path

from repotrial.compose.compatibility import CompatibilityError
from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.lifecycle import CleanupError

_GUEST_VERIFY_TIMEOUT_S = 30
_GUEST_MATERIALIZATION_TIMEOUT_S = 30
_MAX_GUEST_VERIFY_OUTPUT_BYTES = 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_GUEST_RELATIVE_PATH_BYTES = 4096
_MAX_COMPATIBILITY_ARTIFACT_BYTES = 1_048_576
_MAX_COMPATIBILITY_PAYLOAD_BYTES = 1_398_104
_MAX_COMPATIBILITY_EVIDENCE_BYTES = 32 * 1024
_COMPATIBILITY_RELATIVE_PATH = ".repotrial-overlays/compatibility.overlay.yaml"
_EXPERIMENT_RELATIVE_PATH = ".repotrial-overlays/experiment.overlay.yaml"
_ACCEPTED_COMPOSE_PATTERN = re.compile(
    r"\.repotrial-accepted/accepted-[0-9]{4}-[0-9a-f]{16}\.compose\.yaml\Z"
)

# This is code-owned and intentionally not assembled from repository input.
_COMPATIBILITY_ADAPTER_SCRIPT = """\
set -eu
umask 077
set -C

if [ "$#" -ne 3 ]; then
    exit 20
fi
target_path=$1
output_sha256=$2
payload=$3

root=$(pwd -P) || exit 21
case "$root" in
    /*) ;;
    *) exit 21 ;;
esac
[ "$root" != "/" ] || exit 21
git_root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 21
[ "$git_root" = "$root" ] || exit 21
[ "$target_path" = ".repotrial-overlays/compatibility.overlay.yaml" ] || exit 22

overlay_dir=.repotrial-overlays
if [ -e "$overlay_dir" ] || [ -L "$overlay_dir" ]; then
    [ -d "$overlay_dir" ] && [ ! -L "$overlay_dir" ] || exit 23
else
    mkdir "$overlay_dir" || exit 23
fi

if [ -e "$target_path" ] || [ -L "$target_path" ]; then
    exit 24
fi
payload_bytes=$(printf '%s' "$payload" | wc -c)
[ "$payload_bytes" -le 1398104 ] || exit 25
if ! printf '%s' "$payload" | base64 -d > "$target_path"; then
    if [ -f "$target_path" ] && [ ! -L "$target_path" ]; then
        rm -f "$target_path"
    fi
    exit 26
fi
chmod 600 "$target_path" || exit 27
[ -f "$target_path" ] && [ ! -L "$target_path" ] || exit 28
target_mode=$(stat -c '%a' -- "$target_path") || exit 29
[ "$target_mode" = "600" ] || exit 29
target_hash=$(sha256sum -- "$target_path" | cut -d ' ' -f 1) || exit 30
[ "$target_hash" = "$output_sha256" ] || exit 31

printf 'root=%s\npath=%s\nmode=%s\nsha256=%s\n' \
    "$root" "$target_path" "$target_mode" "$target_hash"
"""
_COMPATIBILITY_ADAPTER_SHA256 = (
    "0cb405f669bfce44fcee22a92000abf92d1826f14cbed5223f06af3e16050c19"
)
_EXPERIMENT_ADAPTER_SCRIPT = _COMPATIBILITY_ADAPTER_SCRIPT.replace(
    _COMPATIBILITY_RELATIVE_PATH, _EXPERIMENT_RELATIVE_PATH
)
_EXPERIMENT_ADAPTER_SHA256 = (
    "d0540c9aefa3bfb23cc27a7af79fbdbb8b8c2e714311acc4e1c09f1580a82827"
)
_ACCEPTED_COMPOSE_SHELL_PATTERN = (
    ".repotrial-accepted/accepted-"
    + "[0-9]" * 4
    + "-"
    + "[0-9a-f]" * 16
    + ".compose.yaml"
)
_ACCEPTED_COMPOSE_ADAPTER_SCRIPT = _COMPATIBILITY_ADAPTER_SCRIPT.replace(
    '[ "$target_path" = ".repotrial-overlays/compatibility.overlay.yaml" ] || exit 22',
    f"""case "$target_path" in
    {_ACCEPTED_COMPOSE_SHELL_PATTERN}) ;;
    *) exit 22 ;;
esac""",
).replace("overlay_dir=.repotrial-overlays", "overlay_dir=.repotrial-accepted")
_ACCEPTED_COMPOSE_ADAPTER_SHA256 = (
    "8f9d8bf5428ce0fc984efb1de416ce5c810671ed07ea466ddf570463253a1c4b"
)


class _CompatibilityEvidenceWriter:
    def __init__(self, path: Path) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
        except FileExistsError:
            raise CompatibilityError("evidence_collision") from None
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise CompatibilityError("evidence_persistence_failed") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise CompatibilityError("evidence_persistence_failed")
        self._descriptor: int | None = descriptor
        self._size = 0

    def append(self, record: dict[str, object]) -> None:
        try:
            payload = (
                json.dumps(
                    record,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (OverflowError, RecursionError, TypeError, ValueError):
            raise CompatibilityError("evidence_persistence_failed") from None
        if self._size + len(payload) > _MAX_COMPATIBILITY_EVIDENCE_BYTES:
            raise CompatibilityError("evidence_persistence_failed")
        descriptor = self._descriptor
        if descriptor is None:
            raise CompatibilityError("evidence_persistence_failed")
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short evidence write")
                offset += written
            os.fsync(descriptor)
        except OSError:
            raise CompatibilityError("evidence_persistence_failed") from None
        self._size += len(payload)

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)


async def materialize_guest_compatibility_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    host_artifact_path: Path,
    relative_path: str,
    expected_sha256: str,
    evidence_path: Path,
) -> None:
    """Copy one verified host overlay into a managed sandbox guest."""

    await _materialize_guest_overlay(
        provider,
        sandbox_id,
        host_artifact_path=host_artifact_path,
        relative_path=relative_path,
        expected_sha256=expected_sha256,
        evidence_path=evidence_path,
        expected_relative_path=_COMPATIBILITY_RELATIVE_PATH,
        adapter_script=_COMPATIBILITY_ADAPTER_SCRIPT,
        adapter_sha256=_COMPATIBILITY_ADAPTER_SHA256,
        purpose="compatibility_overlay_materialization",
        command_name="repotrial-compatibility-overlay",
    )


async def materialize_guest_experiment_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    host_artifact_path: Path,
    expected_sha256: str,
    evidence_path: Path,
) -> None:
    """Copy one verified candidate overlay to its fixed guest path."""

    await _materialize_guest_overlay(
        provider,
        sandbox_id,
        host_artifact_path=host_artifact_path,
        relative_path=_EXPERIMENT_RELATIVE_PATH,
        expected_sha256=expected_sha256,
        evidence_path=evidence_path,
        expected_relative_path=_EXPERIMENT_RELATIVE_PATH,
        adapter_script=_EXPERIMENT_ADAPTER_SCRIPT,
        adapter_sha256=_EXPERIMENT_ADAPTER_SHA256,
        purpose="experiment_overlay_materialization",
        command_name="repotrial-experiment-overlay",
    )


async def materialize_guest_accepted_compose(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    host_artifact_path: Path,
    relative_path: str,
    expected_sha256: str,
    evidence_path: Path,
) -> None:
    """Copy one generated accepted Compose file to its matching guest path."""

    if not _ACCEPTED_COMPOSE_PATTERN.fullmatch(relative_path):
        raise CompatibilityError("guest_path_invalid")
    await _materialize_guest_overlay(
        provider,
        sandbox_id,
        host_artifact_path=host_artifact_path,
        relative_path=relative_path,
        expected_sha256=expected_sha256,
        evidence_path=evidence_path,
        expected_relative_path=relative_path,
        adapter_script=_ACCEPTED_COMPOSE_ADAPTER_SCRIPT,
        adapter_sha256=_ACCEPTED_COMPOSE_ADAPTER_SHA256,
        purpose="accepted_compose_materialization",
        command_name="repotrial-accepted-compose",
    )


async def _materialize_guest_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    host_artifact_path: Path,
    relative_path: str,
    expected_sha256: str,
    evidence_path: Path,
    expected_relative_path: str,
    adapter_script: str,
    adapter_sha256: str,
    purpose: str,
    command_name: str,
) -> None:

    _validate_materialization_inputs(
        host_artifact_path,
        relative_path,
        expected_sha256,
        evidence_path,
        expected_relative_path,
    )
    started = time.monotonic()
    if hashlib.sha256(adapter_script.encode()).hexdigest() != adapter_sha256:
        raise CompatibilityError("adapter_identity_mismatch")
    evidence = _CompatibilityEvidenceWriter(evidence_path)
    identity = {
        "adapter_sha256": adapter_sha256,
        "artifact_relative_path": relative_path,
        "artifact_sha256": expected_sha256,
        "purpose": purpose,
        "schema_version": 1,
    }
    try:
        evidence.append({**identity, "outcome": "start", "sequence": 0})
        try:
            host_payload = _read_host_compatibility_artifact(
                host_artifact_path, expected_sha256
            )
            payload = base64.b64encode(host_payload).decode("ascii")
            if len(payload.encode("ascii")) > _MAX_COMPATIBILITY_PAYLOAD_BYTES:
                raise CompatibilityError("payload_oversize")
            result = await provider.exec(
                sandbox_id,
                [
                    "sh",
                    "-eu",
                    "-c",
                    adapter_script,
                    command_name,
                    relative_path,
                    expected_sha256,
                    payload,
                ],
                timeout_s=_GUEST_MATERIALIZATION_TIMEOUT_S,
            )
        except CleanupError:
            raise
        except CompatibilityError:
            raise
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            raise CompatibilityError("guest_materialization_failed") from None

        if not isinstance(result, ExecResult):
            raise CompatibilityError("guest_output_malformed")
        if type(result.exit_code) is not int:
            raise CompatibilityError("guest_output_malformed")
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise CompatibilityError("guest_output_malformed")
        stdout = _bounded_output(result.stdout)
        stderr = _bounded_output(result.stderr)
        if stdout is None or stderr is None:
            raise CompatibilityError("guest_output_oversize")
        if result.exit_code != 0:
            raise CompatibilityError("guest_materialization_failed")
        if stderr:
            raise CompatibilityError("guest_output_malformed")
        _parse_materialization_output(stdout, relative_path, expected_sha256)
        evidence.append(
            {
                **identity,
                "elapsed_s": max(0.0, time.monotonic() - started),
                "guest_validation": "satisfied",
                "outcome": "terminal",
                "reason": "materialized",
                "sequence": 1,
            }
        )
    except CleanupError:
        try:
            _append_materialization_terminal(
                evidence, identity, started, reason="cleanup_failed"
            )
        except CompatibilityError:
            pass
        raise
    except CompatibilityError as error:
        _append_materialization_terminal(
            evidence, identity, started, reason=error.reason
        )
        raise
    finally:
        try:
            evidence.close()
        except OSError:
            if sys.exception() is None:
                raise CompatibilityError("evidence_persistence_failed") from None


def _validate_materialization_inputs(
    host_artifact_path: Path,
    relative_path: str,
    expected_sha256: str,
    evidence_path: Path,
    expected_relative_path: str,
) -> None:
    if not isinstance(host_artifact_path, Path):
        raise CompatibilityError("artifact_path_invalid")
    if relative_path != expected_relative_path:
        raise CompatibilityError("artifact_path_invalid")
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise CompatibilityError("identity_incomplete")
    if not isinstance(evidence_path, Path):
        raise CompatibilityError("evidence_path_invalid")


def _append_materialization_terminal(
    evidence: _CompatibilityEvidenceWriter,
    identity: dict[str, object],
    started: float,
    *,
    reason: str,
) -> None:
    evidence.append(
        {
            **identity,
            "elapsed_s": max(0.0, time.monotonic() - started),
            "guest_validation": "failed",
            "outcome": "terminal",
            "reason": reason,
            "sequence": 1,
        }
    )


def fingerprint_host_artifact(path: Path) -> str:
    """Return a race-detected SHA-256 fingerprint of one host artifact."""

    payload = _read_host_compatibility_artifact(path, None)
    return hashlib.sha256(payload).hexdigest()


def read_host_artifact(path: Path) -> bytes:
    """Read one bounded host artifact with race and symlink checks."""

    return _read_host_compatibility_artifact(path, None)


def _read_host_compatibility_artifact(path: Path, expected_sha256: str | None) -> bytes:
    """Read a bounded regular file while retaining its path/fd identity."""

    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        raise CompatibilityError("artifact_missing") from None
    except OSError:
        raise CompatibilityError("artifact_unreadable") from None
    if _is_link(path, path_stat):
        raise CompatibilityError("artifact_linked")
    if not stat.S_ISREG(path_stat.st_mode):
        raise CompatibilityError("artifact_not_regular")
    expected_identity = _host_file_identity(path_stat)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise CompatibilityError("artifact_missing") from None
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EMLINK}:
                raise CompatibilityError("artifact_linked") from None
            raise CompatibilityError("artifact_unreadable") from None
        try:
            opened_stat = os.fstat(descriptor)
        except OSError:
            raise CompatibilityError("artifact_unreadable") from None
        if _is_link(path, opened_stat) or not stat.S_ISREG(opened_stat.st_mode):
            raise CompatibilityError("artifact_not_regular")
        opened_identity = _host_file_identity(opened_stat)
        if opened_identity != expected_identity:
            raise CompatibilityError("artifact_changed")
        if opened_stat.st_size > _MAX_COMPATIBILITY_ARTIFACT_BYTES:
            raise CompatibilityError("artifact_oversize")

        payload = bytearray()
        while len(payload) <= _MAX_COMPATIBILITY_ARTIFACT_BYTES:
            try:
                chunk = os.read(
                    descriptor,
                    min(65_536, _MAX_COMPATIBILITY_ARTIFACT_BYTES + 1 - len(payload)),
                )
            except OSError:
                raise CompatibilityError("artifact_unreadable") from None
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > _MAX_COMPATIBILITY_ARTIFACT_BYTES:
                raise CompatibilityError("artifact_oversize")

        try:
            final_fd_stat = os.fstat(descriptor)
            final_path_stat = path.lstat()
        except FileNotFoundError:
            raise CompatibilityError("artifact_missing") from None
        except OSError:
            raise CompatibilityError("artifact_unreadable") from None
        if _is_link(path, final_path_stat):
            raise CompatibilityError("artifact_linked")
        if not stat.S_ISREG(final_fd_stat.st_mode) or not stat.S_ISREG(
            final_path_stat.st_mode
        ):
            raise CompatibilityError("artifact_not_regular")
        if (
            _host_file_identity(final_fd_stat) != opened_identity
            or _host_file_identity(final_path_stat) != opened_identity
            or final_fd_stat.st_size != len(payload)
        ):
            raise CompatibilityError("artifact_changed")
        data = bytes(payload)
        if (
            expected_sha256 is not None
            and hashlib.sha256(data).hexdigest() != expected_sha256
        ):
            raise CompatibilityError("artifact_hash_mismatch")
        return data
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _host_file_identity(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _is_link(path: Path, file_stat: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(
        reparse_point and getattr(file_stat, "st_file_attributes", 0) & reparse_point
    )


def _parse_materialization_output(
    stdout: str, relative_path: str, expected_sha256: str
) -> None:
    if not stdout.endswith("\n") or stdout.count("\n") != 4:
        raise CompatibilityError("guest_output_malformed")
    lines = stdout[:-1].split("\n")
    if (
        not lines[0].startswith("root=")
        or lines[1] != f"path={relative_path}"
        or lines[2] != "mode=600"
    ):
        raise CompatibilityError("guest_output_malformed")
    root = lines[0].removeprefix("root=")
    if (
        not root.startswith("/")
        or root == "/"
        or posixpath.normpath(root) != root
        or len(root.encode("utf-8", errors="surrogatepass"))
        > _MAX_GUEST_RELATIVE_PATH_BYTES
        or any(unicodedata.category(character).startswith("C") for character in root)
    ):
        raise CompatibilityError("guest_output_malformed")
    reported_sha256 = lines[3].removeprefix("sha256=")
    if (
        not lines[3].startswith("sha256=")
        or _SHA256_PATTERN.fullmatch(reported_sha256) is None
    ):
        raise CompatibilityError("guest_output_malformed")
    if reported_sha256 != expected_sha256:
        raise CompatibilityError("guest_hash_mismatch")


async def verify_guest_compatibility_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    relative_path: str,
    expected_sha256: str,
) -> None:
    """Verify the cloned compatibility artifact before any workload command."""

    await _verify_guest_overlay(
        provider,
        sandbox_id,
        relative_path=relative_path,
        expected_sha256=expected_sha256,
    )


async def verify_guest_experiment_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    expected_sha256: str,
) -> None:
    """Verify the fixed candidate artifact before any workload command."""

    await _verify_guest_overlay(
        provider,
        sandbox_id,
        relative_path=_EXPERIMENT_RELATIVE_PATH,
        expected_sha256=expected_sha256,
    )


async def verify_guest_accepted_compose(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    relative_path: str,
    expected_sha256: str,
) -> None:
    """Verify one generated accepted Compose file at its fixed guest path."""

    if not _ACCEPTED_COMPOSE_PATTERN.fullmatch(relative_path):
        raise CompatibilityError("guest_path_invalid")
    await _verify_guest_overlay(
        provider,
        sandbox_id,
        relative_path=relative_path,
        expected_sha256=expected_sha256,
    )


async def _verify_guest_overlay(
    provider: SandboxProvider,
    sandbox_id: str,
    *,
    relative_path: str,
    expected_sha256: str,
) -> None:
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
