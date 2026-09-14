"""Owner-only persistence for optional local model settings."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast
from urllib.parse import urlparse

ModelProvider = Literal["deepseek", "custom"]
DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"
_MAX_FILE_BYTES = 16 * 1024
_MAX_API_KEY_CHARS = 4096
_REQUIRED_RECORD_KEYS = frozenset({"provider", "endpoint", "model_name", "api_key"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class SettingsValidationError(ValueError):
    """Raised when settings cannot be safely validated or persisted."""


class _SettingsRecord(TypedDict):
    provider: ModelProvider | None
    endpoint: str | None
    model_name: str | None
    api_key: str


@dataclass(frozen=True, slots=True)
class PublicModelSettings:
    provider: ModelProvider | None
    endpoint: str | None
    model_name: str | None
    api_key_configured: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "api_key_configured": self.api_key_configured,
        }


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedModelSettings:
    provider: ModelProvider
    endpoint: str
    model_name: str
    api_key: str | None


@dataclass(frozen=True, slots=True, repr=False)
class ModelSettingsUpdate:
    provider: str | None
    endpoint: str | None = None
    model_name: str | None = None
    api_key: str = ""


class ModelSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()

    def load_public(self) -> PublicModelSettings:
        record = self._load_record()
        if record is None:
            return _empty_public()
        return _public(record)

    def load_resolved(self) -> ResolvedModelSettings | None:
        record = self._load_record()
        if record is None or record["provider"] is None:
            return None
        return ResolvedModelSettings(
            provider=record["provider"],
            endpoint=cast(str, record["endpoint"]),
            model_name=cast(str, record["model_name"]),
            api_key=record["api_key"] or None,
        )

    def save(self, update: ModelSettingsUpdate) -> PublicModelSettings:
        if not isinstance(update, ModelSettingsUpdate):
            raise SettingsValidationError("invalid model settings update")
        current = self._load_record()
        provider, endpoint, model_name = _normalize_configuration(update)
        api_key = update.api_key
        _validate_api_key(api_key)
        if (
            not api_key
            and current is not None
            and same_endpoint(current["endpoint"], endpoint)
        ):
            api_key = current["api_key"]
        record: _SettingsRecord = {
            "provider": provider,
            "endpoint": endpoint,
            "model_name": model_name,
            "api_key": api_key,
        }
        data = _encode_record(record)
        self._atomic_write(data)
        return _public(record)

    def clear_key(self) -> PublicModelSettings:
        record = self._load_record()
        if record is None:
            return _empty_public()
        record["api_key"] = ""
        data = _encode_record(record)
        self._atomic_write(data)
        return _public(record)

    def clear(self) -> PublicModelSettings:
        record: _SettingsRecord = {
            "provider": None,
            "endpoint": None,
            "model_name": None,
            "api_key": "",
        }
        self._atomic_write(_encode_record(record))
        return _public(record)

    def _load_record(self) -> _SettingsRecord | None:
        fd = _open_settings_file(self.path)
        if fd is None:
            return None
        try:
            encoded = _read_bounded(fd)
        except OSError:
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if encoded is None:
            return None
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None
        return _record_from_payload(payload)

    def _atomic_write(self, data: bytes) -> None:
        parent_fd, filename = _open_settings_parent(self.path, create=True)
        temporary_name: str | None = None
        temporary_fd = -1
        replaced = False
        try:
            _validate_existing_target(parent_fd, filename)
            for _ in range(16):
                candidate = f".model-settings-{secrets.token_hex(16)}"
                try:
                    temporary_fd = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_fd < 0 or temporary_name is None:
                raise OSError("could not allocate settings temporary file")
            os.fchmod(temporary_fd, 0o600)
            written = 0
            while written < len(data):
                count = os.write(temporary_fd, data[written:])
                if count <= 0:
                    raise OSError("settings temporary write made no progress")
                written += count
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            replaced = True
        except (OSError, ValueError) as error:
            raise SettingsValidationError("settings could not be saved") from error
        finally:
            if temporary_fd >= 0:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if temporary_name is not None and not replaced:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _default_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "repotrial" / "model-settings.json"


def _empty_public() -> PublicModelSettings:
    return PublicModelSettings(None, None, None, False)


def same_endpoint(left: str | None, right: str | None) -> bool:
    return (
        left is not None and right is not None and left.rstrip("/") == right.rstrip("/")
    )


def _public(record: _SettingsRecord) -> PublicModelSettings:
    return PublicModelSettings(
        provider=record["provider"],
        endpoint=record["endpoint"],
        model_name=record["model_name"],
        api_key_configured=bool(record["api_key"]),
    )


def _normalize_configuration(
    update: ModelSettingsUpdate,
) -> tuple[ModelProvider, str, str] | tuple[None, None, None]:
    provider = update.provider
    if provider is None or provider == "":
        if update.endpoint is None and update.model_name is None:
            return None, None, None
        raise SettingsValidationError("provider is required")
    if provider not in {"deepseek", "custom"}:
        raise SettingsValidationError("provider must be deepseek or custom")
    normalized_provider = cast(ModelProvider, provider)
    endpoint: str | None
    model_name: str | None
    if provider == "deepseek":
        endpoint = update.endpoint or DEEPSEEK_ENDPOINT
        model_name = update.model_name or DEEPSEEK_MODEL
    else:
        endpoint = update.endpoint
        model_name = update.model_name
    if endpoint is None or model_name is None or not model_name:
        raise SettingsValidationError("endpoint and model_name are required")
    _validate_endpoint(endpoint)
    if len(model_name) > 256 or "\x00" in model_name or not model_name.strip():
        raise SettingsValidationError("model_name is invalid")
    return normalized_provider, endpoint, model_name


def _validate_api_key(api_key: object) -> None:
    if (
        not isinstance(api_key, str)
        or len(api_key) > _MAX_API_KEY_CHARS
        or "\x00" in api_key
    ):
        raise SettingsValidationError("api_key is invalid")


def _validate_endpoint(endpoint: str) -> None:
    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise SettingsValidationError(
            "endpoint must be an HTTP(S) URL without credentials"
        ) from None
    if (
        len(endpoint) > 2048
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or (port is not None and not 1 <= port <= 65535)
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or "\x00" in endpoint
    ):
        raise SettingsValidationError(
            "endpoint must be an HTTP(S) URL without credentials"
        )


def _record_from_payload(payload: object) -> _SettingsRecord | None:
    if not isinstance(payload, dict) or set(payload) != _REQUIRED_RECORD_KEYS:
        return None
    provider = payload["provider"]
    endpoint = payload["endpoint"]
    model_name = payload["model_name"]
    api_key = payload["api_key"]
    if not isinstance(api_key, str):
        return None
    normalized: tuple[ModelProvider, str, str] | tuple[None, None, None]
    try:
        _validate_api_key(api_key)
        if provider is None:
            if endpoint is not None or model_name is not None:
                return None
            normalized = (None, None, None)
        elif (
            isinstance(provider, str)
            and (endpoint is None or isinstance(endpoint, str))
            and (model_name is None or isinstance(model_name, str))
        ):
            normalized = _normalize_configuration(
                ModelSettingsUpdate(
                    provider=provider,
                    endpoint=endpoint,
                    model_name=model_name,
                )
            )
        else:
            return None
    except SettingsValidationError:
        return None
    if normalized != (provider, endpoint, model_name):
        return None
    return _SettingsRecord(
        provider=cast(ModelProvider | None, provider),
        endpoint=cast(str | None, endpoint),
        model_name=cast(str | None, model_name),
        api_key=api_key,
    )


def _encode_record(record: _SettingsRecord) -> bytes:
    data = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise SettingsValidationError("settings exceed size limit")
    return data


def _open_settings_file(path: Path) -> int | None:
    try:
        parent_fd, filename = _open_settings_parent(path, create=False)
    except (OSError, SettingsValidationError):
        return None
    try:
        try:
            fd = os.open(
                filename,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=parent_fd,
            )
        except (FileNotFoundError, OSError):
            return None
        if not _valid_settings_fd(fd):
            os.close(fd)
            return None
        return fd
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _read_bounded(fd: int) -> bytes | None:
    result = os.fstat(fd)
    if not _valid_settings_stat(result):
        return None
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_FILE_BYTES:
        chunk = os.read(fd, _MAX_FILE_BYTES + 1 - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_FILE_BYTES:
            return None
    return None


def _valid_settings_fd(fd: int) -> bool:
    try:
        return _valid_settings_stat(os.fstat(fd))
    except OSError:
        return False


def _valid_settings_stat(result: os.stat_result) -> bool:
    return (
        stat.S_ISREG(result.st_mode)
        and result.st_uid == os.geteuid()
        and stat.S_IMODE(result.st_mode) == 0o600
        and result.st_size <= _MAX_FILE_BYTES
    )


def _open_settings_parent(path: Path, *, create: bool) -> tuple[int, str]:
    absolute = Path(os.path.abspath(path))
    filename = absolute.name
    if not filename or filename in {".", ".."}:
        raise SettingsValidationError("settings path is unsafe")
    fd = _open_directory_chain(absolute.parent, create=create)
    if fd is None:
        raise SettingsValidationError("settings parent is unsafe")
    return fd, filename


def _open_directory_chain(parent: Path, *, create: bool) -> int | None:
    absolute = Path(os.path.abspath(parent))
    anchor = absolute.anchor or "."
    try:
        fd = os.open(anchor, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    except OSError:
        return None
    keep_fd = False
    try:
        for component in absolute.parts[1:] if absolute.anchor else absolute.parts:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(
                    component,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                    dir_fd=fd,
                )
            try:
                result = os.fstat(child)
            except OSError:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise
            if not _valid_parent_stat(result):
                os.close(child)
                return None
            try:
                os.close(fd)
            except OSError:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise
            fd = child
        keep_fd = True
        return fd
    except OSError:
        return None
    finally:
        if not keep_fd:
            try:
                os.close(fd)
            except OSError:
                pass


def _valid_parent_stat(result: os.stat_result) -> bool:
    mode = stat.S_IMODE(result.st_mode)
    return (
        stat.S_ISDIR(result.st_mode)
        and not stat.S_ISLNK(result.st_mode)
        and result.st_uid in {0, os.geteuid()}
        and (not (mode & 0o022) or bool(mode & stat.S_ISVTX))
    )


def _validate_existing_target(parent_fd: int, filename: str) -> None:
    try:
        fd = os.open(
            filename,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise SettingsValidationError("settings path is unsafe") from error
    try:
        if not _valid_settings_fd(fd):
            raise SettingsValidationError("settings path is unsafe")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
