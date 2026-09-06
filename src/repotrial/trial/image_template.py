"""Bounded Compose image inventories and per-trial image preparation."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from repotrial.sandbox.base import ExecResult, SandboxProvider
from repotrial.sandbox.docker_sbx import DockerSbxError

_IMAGE_ID_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIGEST_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TAG_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_MAX_IMAGE_RECORDS: Final = 256
_MAX_IMAGE_OUTPUT_BYTES: Final = 256 * 1024
_MAX_IMAGE_FIELD_BYTES: Final = 512
_MAX_COMMAND_OUTPUT_BYTES: Final = 65_536
_IMAGE_FIELDS: Final = frozenset({"ID", "Repository", "Tag", "Digest"})
_IMAGE_LIST_ARGV: Final = (
    "docker",
    "image",
    "ls",
    "--all",
    "--no-trunc",
    "--digests",
    "--format",
    (
        '{"ID":{{json .ID}},"Repository":{{json .Repository}},'
        '"Tag":{{json .Tag}},"Digest":{{json .Digest}}}'
    ),
)
_IMAGE_COMMAND_TIMEOUT_S: Final = 600
_REASON_PATTERN: Final = re.compile(r"[a-z0-9_]{1,64}\Z")


class ImageInventoryError(ValueError):
    """A bounded, fail-closed image inventory parsing error."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ImageTemplateError(RuntimeError):
    """A bounded, fail-closed Compose image-template error."""

    reason: str

    def __init__(self, reason: str) -> None:
        if _REASON_PATTERN.fullmatch(reason) is None:
            raise ValueError("image-template reason is invalid")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True, order=True)
class ImageRecord:
    """One normalized immutable Docker image reference."""

    image_id: str
    repository: str
    tag: str
    digest: str

    @property
    def id(self) -> str:
        """Return the Docker image ID using Docker's conventional field name."""

        return self.image_id

    def as_public_record(self) -> dict[str, str]:
        """Return only the public image identity fields used for hashing."""

        return {
            "digest": self.digest,
            "id": self.image_id,
            "repository": self.repository,
            "tag": self.tag,
        }


# Keep a descriptive alias for callers that prefer the inventory-specific name.
ImageInventoryRecord = ImageRecord


@dataclass(frozen=True, slots=True)
class ImageInventory:
    """Canonical image records and their immutable inventory identity."""

    records: tuple[ImageRecord, ...]
    sha256: str

    def __post_init__(self) -> None:
        if not self.records:
            raise ImageInventoryError("image_inventory_empty")
        if self.records != tuple(sorted(self.records)):
            raise ImageInventoryError("image_inventory_unsorted")
        expected = _inventory_sha256(self.records)
        if self.sha256 != expected:
            raise ImageInventoryError("image_inventory_hash_mismatch")

    @property
    def identity_sha256(self) -> str:
        return self.sha256

    def as_public_record(self) -> dict[str, object]:
        """Return a bounded persistence shape without environment values."""

        return {
            "records": [record.as_public_record() for record in self.records],
            "sha256": self.sha256,
        }


def parse_image_inventory(output: str | bytes) -> ImageInventory:
    """Parse Docker's newline-delimited image-list JSON strictly.

    The parser accepts only the four fields requested by the command format and
    rejects ambiguous or unbounded output before exposing an inventory.
    """

    text = _decode_inventory_output(output)
    records: list[ImageRecord] = []
    seen: set[ImageRecord] = set()
    lines = text.splitlines()
    for line in lines:
        if not line.strip():
            raise ImageInventoryError("image_inventory_blank_line")
        if len(records) >= _MAX_IMAGE_RECORDS:
            raise ImageInventoryError("image_inventory_too_many_records")
        try:
            decoded = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
            )
        except ImageInventoryError:
            raise
        except (RecursionError, ValueError):
            raise ImageInventoryError("image_inventory_invalid_json") from None
        if not isinstance(decoded, dict) or set(decoded) != _IMAGE_FIELDS:
            raise ImageInventoryError("image_inventory_schema_invalid")
        record = ImageRecord(
            image_id=_normalize_image_id(decoded["ID"]),
            repository=_normalize_repository(decoded["Repository"]),
            tag=_normalize_tag(decoded["Tag"]),
            digest=_normalize_digest(decoded["Digest"]),
        )
        if record in seen:
            raise ImageInventoryError("image_inventory_duplicate")
        seen.add(record)
        records.append(record)

    if not records:
        raise ImageInventoryError("image_inventory_empty")
    normalized = tuple(sorted(records))
    return ImageInventory(records=normalized, sha256=_inventory_sha256(normalized))


def _decode_inventory_output(output: str | bytes) -> str:
    if isinstance(output, bytes):
        if len(output) > _MAX_IMAGE_OUTPUT_BYTES:
            raise ImageInventoryError("image_inventory_output_too_large")
        try:
            text = output.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ImageInventoryError("image_inventory_invalid_utf8") from None
    elif isinstance(output, str):
        text = output
    else:
        raise ImageInventoryError("image_inventory_output_invalid")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ImageInventoryError("image_inventory_invalid_utf8") from None
    if len(encoded) > _MAX_IMAGE_OUTPUT_BYTES:
        raise ImageInventoryError("image_inventory_output_too_large")
    return text


def _normalize_image_id(value: object) -> str:
    if not isinstance(value, str) or _IMAGE_ID_PATTERN.fullmatch(value) is None:
        raise ImageInventoryError("image_inventory_id_invalid")
    return value


def _normalize_repository(value: object) -> str:
    text = _normalize_public_text(value, "repository")
    if text == "<none>":
        return ""
    parts = text.lower().split("/")
    if any(not part for part in parts):
        raise ImageInventoryError("image_inventory_repository_invalid")
    first = parts[0]
    if first in {"docker.io", "index.docker.io"}:
        parts[0] = "docker.io"
        if len(parts) == 2:
            parts.insert(1, "library")
    elif len(parts) == 1:
        parts = ["docker.io", "library", parts[0]]
    elif "." not in first and ":" not in first and first != "localhost":
        parts.insert(0, "docker.io")
    return "/".join(parts)


def _normalize_tag(value: object) -> str:
    text = _normalize_public_text(value, "tag")
    if text == "<none>":
        return ""
    if _TAG_PATTERN.fullmatch(text) is None:
        raise ImageInventoryError("image_inventory_tag_invalid")
    return text


def _normalize_digest(value: object) -> str:
    text = _normalize_public_text(value, "digest").lower()
    if text == "<none>":
        return ""
    if _DIGEST_PATTERN.fullmatch(text) is None:
        raise ImageInventoryError("image_inventory_digest_invalid")
    return text


def _normalize_public_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ImageInventoryError(f"image_inventory_{field}_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ImageInventoryError(f"image_inventory_{field}_invalid") from None
    if len(encoded) > _MAX_IMAGE_FIELD_BYTES:
        raise ImageInventoryError(f"image_inventory_{field}_too_large")
    text = value.strip()
    if not text or any(
        ord(character) < 32 or ord(character) == 127 or character.isspace()
        for character in text
    ):
        raise ImageInventoryError(f"image_inventory_{field}_invalid")
    return text


def _inventory_sha256(records: tuple[ImageRecord, ...]) -> str:
    canonical = json.dumps(
        [record.as_public_record() for record in records],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ImageInventoryError("image_inventory_duplicate_key")
        decoded[key] = value
    return decoded


def _reject_nonstandard_constant(_: str) -> object:
    raise ImageInventoryError("image_inventory_nonstandard_json")


async def prepare_compose_image_template(
    provider: SandboxProvider,
    sandbox_id: str,
    compose_path: str,
    env: Mapping[str, str],
    *,
    guest_workspace: str | None = None,
    overlay_path: str | None = None,
    compatibility_overlay_path: str | None = None,
    unset_env_keys: Sequence[str] = (),
    project_directory: str | None = None,
    declared_secret_env_keys: Collection[str] = (),
) -> ImageInventory:
    """Prepare and save one immutable Compose image set in a warmup sandbox."""

    if not provider.supports_runtime_templates:
        raise ImageTemplateError("runtime_template_unsupported")
    if guest_workspace is not None:
        _validate_guest_workspace(guest_workspace)
    try:
        effective_env = dict(env)
    except (TypeError, ValueError):
        raise ImageTemplateError("compose_environment_invalid") from None

    # These helpers are imported lazily to keep ``boot`` and this module
    # independent at import time while sharing the exact command validation.
    from repotrial.trial.boot import (
        _bounded_declared_secret_keys,
        _preflight_compose,
        _validated_compose_env_prefix,
    )

    try:
        docker_compose = _compose_argv(
            compose_path,
            overlay_path=overlay_path,
            compatibility_overlay_path=compatibility_overlay_path,
            project_directory=project_directory,
        )
        declared_secret_keys = _bounded_declared_secret_keys(declared_secret_env_keys)
        recovered_env, config_result = await _preflight_compose(
            provider,
            sandbox_id,
            docker_compose,
            compose_path,
            effective_env,
            declared_secret_keys,
            unset_env_keys,
        )
    except DockerSbxError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise ImageTemplateError("compose_preflight_failed") from None
    del recovered_env
    if config_result is None:
        try:
            config_prefix = _validated_compose_env_prefix(
                compose_path, effective_env, unset_env_keys
            )
        except (TypeError, ValueError):
            raise ImageTemplateError("compose_preflight_failed") from None
        config_result = await _exec(
            provider,
            sandbox_id,
            [*config_prefix, *docker_compose, "config", "--quiet"],
            "compose_preflight_failed",
            timeout_s=30,
        )
    if config_result is not None:
        _validate_successful_result(config_result, "compose_preflight_failed")

    try:
        prefix = _validated_compose_env_prefix(
            compose_path, effective_env, unset_env_keys
        )
    except (TypeError, ValueError):
        raise ImageTemplateError("compose_preflight_failed") from None
    await _exec(
        provider,
        sandbox_id,
        [*prefix, *docker_compose, "pull", "--ignore-buildable"],
        "image_pull_failed",
        timeout_s=_IMAGE_COMMAND_TIMEOUT_S,
    )
    await _exec(
        provider,
        sandbox_id,
        [*prefix, *docker_compose, "build"],
        "image_build_failed",
        timeout_s=_IMAGE_COMMAND_TIMEOUT_S,
    )
    inventory_result = await _exec(
        provider,
        sandbox_id,
        [*prefix, *_IMAGE_LIST_ARGV],
        "image_inventory_failed",
        timeout_s=60,
    )
    try:
        inventory = parse_image_inventory(inventory_result.stdout)
    except ImageInventoryError as error:
        raise ImageTemplateError(error.reason) from None

    pwd_result = await _exec(
        provider,
        sandbox_id,
        [*prefix, "pwd", "-P"],
        "guest_workspace_verification_failed",
        timeout_s=30,
    )
    pwd_guest_workspace = _parse_guest_workspace(pwd_result.stdout)
    git_root_result = await _exec(
        provider,
        sandbox_id,
        [*prefix, "git", "rev-parse", "--show-toplevel"],
        "guest_workspace_verification_failed",
        timeout_s=30,
    )
    git_guest_workspace = _parse_guest_workspace(git_root_result.stdout)
    if pwd_guest_workspace != git_guest_workspace:
        raise ImageTemplateError("guest_workspace_verification_failed")
    if guest_workspace is not None and git_guest_workspace != guest_workspace:
        raise ImageTemplateError("guest_workspace_verification_failed")

    await _exec(
        provider,
        sandbox_id,
        [*prefix, "rm", "--recursive", "--force", "--", git_guest_workspace],
        "guest_workspace_removal_failed",
        timeout_s=30,
    )
    try:
        await provider.activate_runtime_template(sandbox_id, inventory.sha256)
    except DockerSbxError:
        raise
    except ImageTemplateError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise ImageTemplateError("template_activation_failed") from None
    return inventory


async def verify_compose_image_identity(
    provider: SandboxProvider,
    sandbox_id: str,
    expected_identity_sha256: str | ImageInventory,
    *,
    compose_path: str,
    env: Mapping[str, str],
    unset_env_keys: Sequence[str] = (),
) -> None:
    """Verify the active template's image identity before Compose startup."""

    expected = (
        expected_identity_sha256.sha256
        if isinstance(expected_identity_sha256, ImageInventory)
        else expected_identity_sha256
    )
    if (
        not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-f]{64}\Z", expected) is None
    ):
        raise ImageTemplateError("image_identity_mismatch")
    try:
        from repotrial.trial.boot import _validated_compose_env_prefix

        prefix = _validated_compose_env_prefix(compose_path, env, unset_env_keys)
    except (TypeError, ValueError):
        raise ImageTemplateError("image_identity_verification_failed") from None
    result = await _exec(
        provider,
        sandbox_id,
        [*prefix, *_IMAGE_LIST_ARGV],
        "image_identity_verification_failed",
        timeout_s=60,
    )
    try:
        actual = parse_image_inventory(result.stdout)
    except ImageInventoryError:
        raise ImageTemplateError("image_identity_verification_failed") from None
    if actual.sha256 != expected:
        raise ImageTemplateError("image_identity_mismatch")


def _compose_argv(
    compose_path: str,
    *,
    overlay_path: str | None,
    compatibility_overlay_path: str | None,
    project_directory: str | None,
) -> list[str]:
    # Import the validators here so this module uses the boot runner's exact
    # accepted Compose path and project-directory contract.
    from repotrial.trial.boot import (
        _validate_compose_path,
        _validate_project_directory,
    )

    _validate_compose_path(compose_path, "compose_path")
    if project_directory is not None:
        _validate_project_directory(project_directory)
    docker_compose = ["docker", "compose"]
    if project_directory is not None:
        docker_compose.extend(["--project-directory", project_directory])
    docker_compose.extend(["-f", compose_path])
    if compatibility_overlay_path is not None:
        _validate_compose_path(compatibility_overlay_path, "compatibility_overlay_path")
        docker_compose.extend(["-f", compatibility_overlay_path])
    if overlay_path is not None:
        _validate_compose_path(overlay_path, "overlay_path")
        docker_compose.extend(["-f", overlay_path])
    return docker_compose


async def _exec(
    provider: SandboxProvider,
    sandbox_id: str,
    argv: list[str],
    reason: str,
    *,
    timeout_s: int,
) -> ExecResult:
    try:
        result = await provider.exec(sandbox_id, argv, timeout_s=timeout_s)
    except DockerSbxError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise ImageTemplateError(reason) from None
    return _validate_successful_result(result, reason)


def _validate_successful_result(result: object, reason: str) -> ExecResult:
    if not isinstance(result, ExecResult):
        raise ImageTemplateError(reason)
    for output in (result.stdout, result.stderr):
        if not isinstance(output, str):
            raise ImageTemplateError(reason)
        try:
            if len(output.encode("utf-8", errors="strict")) > _MAX_COMMAND_OUTPUT_BYTES:
                raise ImageTemplateError(reason)
        except UnicodeEncodeError:
            raise ImageTemplateError(reason) from None
    if type(result.exit_code) is not int or result.exit_code != 0:
        raise ImageTemplateError(reason)
    return result


def _validate_guest_workspace(path: object) -> None:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or "\0" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ImageTemplateError("guest_workspace_invalid")
    if not path.startswith("/") or path == "/" or posixpath.normpath(path) != path:
        raise ImageTemplateError("guest_workspace_invalid")
    if any(part in {"", ".", ".."} for part in path.split("/")[1:]):
        raise ImageTemplateError("guest_workspace_invalid")


def _parse_guest_workspace(output: object) -> str:
    if not isinstance(output, str):
        raise ImageTemplateError("guest_workspace_verification_failed")
    if output.endswith("\r\n"):
        path = output[:-2]
    elif output.endswith("\n"):
        path = output[:-1]
    else:
        raise ImageTemplateError("guest_workspace_verification_failed")
    try:
        _validate_guest_workspace(path)
    except ImageTemplateError:
        raise ImageTemplateError("guest_workspace_verification_failed") from None
    return path
