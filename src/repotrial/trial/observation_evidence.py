import ctypes
import hashlib
import importlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, cast

from repotrial.sandbox.base import ExecResult

type ObservationOperation = Literal[
    "discovery", "inspect", "diff", "top", "network", "audit"
]
type EvidencePhase = Literal[
    "provider_execution",
    "result_validation",
    "parsing",
    "serialization",
    "atomic_persistence",
]
type EvidenceReason = Literal[
    "collector_started",
    "collector_succeeded",
    "provider_exception",
    "nonzero_exit",
    "malformed_exec_result",
    "malformed_network_result",
    "parse_failure",
    "resource_limit_exceeded",
    "network_unsupported",
    "serialization_failed",
    "evidence_too_large",
    "destination_collision",
    "parent_invalid",
    "persistence_failed",
    "close_failed",
    "audit_started",
    "audit_serialization_failed",
    "audit_too_large",
    "audit_destination_collision",
    "audit_persistence_failed",
    "audit_persisted",
]

_SCHEMA_VERSION = 1
_MAX_EVENTS = 390
_MAX_EVENT_BYTES = 4_096
_MAX_LEDGER_BYTES = 1_048_576
_MAX_ARGV_COUNT = 64
_MAX_ARGV_BYTES = 65_536
_MAX_HASHED_OUTPUT_BYTES = 65_536
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_IS_WINDOWS = os.name == "nt"
_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_OPERATIONS: frozenset[str] = frozenset(
    {"discovery", "inspect", "diff", "top", "network", "audit"}
)
_COLLECTOR_OPERATIONS: frozenset[str] = frozenset(
    {"discovery", "inspect", "diff", "top", "network"}
)
_TERMINAL_TAXONOMY: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("provider_execution", "failure", "provider_exception"),
        ("result_validation", "failure", "nonzero_exit"),
        ("result_validation", "failure", "malformed_exec_result"),
        ("result_validation", "failure", "malformed_network_result"),
        ("parsing", "failure", "parse_failure"),
        ("parsing", "failure", "resource_limit_exceeded"),
        ("parsing", "success", "collector_succeeded"),
        ("parsing", "success", "network_unsupported"),
    }
)
_AUDIT_TERMINAL_TAXONOMY: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("serialization", "failure", "audit_serialization_failed"),
        ("serialization", "failure", "audit_too_large"),
        ("atomic_persistence", "failure", "audit_destination_collision"),
        ("atomic_persistence", "failure", "audit_persistence_failed"),
        ("atomic_persistence", "success", "audit_persisted"),
    }
)
_CLOSE_FAILURE_NOTE = (
    "secondary observation evidence close failed; descriptor ownership is uncertain"
)
_PERSISTENCE_FAILURE_NOTE = (
    "secondary observation evidence persistence failed while preserving primary error"
)


class ObservationEvidenceError(RuntimeError):
    """Private observer evidence could not be safely retained."""

    def __init__(self, phase: EvidencePhase, reason: EvidenceReason) -> None:
        super().__init__("observation evidence could not be retained")
        self.phase = phase
        self.reason = reason


class ObservationEvidenceRecorder:
    """Append bounded collector-boundary metadata to one claimed JSONL file."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("observation evidence path must be a Path")
        parent_identity = _validate_parent(path.parent)
        _reject_existing_destination(path)
        self._path = path
        self._descriptor: int | None = _create_destination(path, parent_identity)
        self._sequence = 0
        self._written_bytes = 0
        self._active_operation: ObservationOperation | None = None

    @property
    def path(self) -> Path:
        return self._path

    def record_start(
        self,
        operation: ObservationOperation,
        argv: list[str],
        *,
        phase: EvidencePhase = "provider_execution",
        reason: EvidenceReason = "collector_started",
    ) -> None:
        _validate_operation(operation)
        _validate_start_taxonomy(operation, phase, reason)
        if self._active_operation is not None:
            raise ValueError("collector evidence already has an active operation")
        event = self._base_event(operation, argv)
        event.update(
            {
                "exception_class": None,
                "outcome": "start",
                "phase": phase,
                "reason": reason,
                "stderr": None,
                "stdout": None,
            }
        )
        self._append(event)
        self._active_operation = operation

    def record_terminal(
        self,
        operation: ObservationOperation,
        argv: list[str],
        *,
        phase: EvidencePhase,
        outcome: Literal["success", "failure"],
        reason: EvidenceReason,
        result: ExecResult | None = None,
        exception: BaseException | None = None,
    ) -> None:
        _validate_operation(operation)
        _validate_terminal_taxonomy(operation, phase, outcome, reason)
        if self._active_operation != operation:
            raise ValueError(
                "collector evidence terminal operation does not match start"
            )
        event = self._base_event(operation, argv)
        if isinstance(result, ExecResult):
            stdout = _output_metadata(result.stdout)
            stderr = _output_metadata(result.stderr)
        else:
            stdout = None
            stderr = None
        event.update(
            {
                "exit_code": (
                    result.exit_code
                    if isinstance(result, ExecResult) and type(result.exit_code) is int
                    else None
                ),
                "exception_class": _exception_class(exception),
                "outcome": outcome,
                "phase": phase,
                "reason": reason,
                "stderr": stderr,
                "stdout": stdout,
            }
        )
        self._append(event)
        self._active_operation = None

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            os.close(descriptor)
        except OSError as error:
            close_error = ObservationEvidenceError("atomic_persistence", "close_failed")
            raise close_error from error

    def close_preserving_primary(self, primary: BaseException) -> None:
        if self._descriptor is None:
            return
        try:
            self.close()
        except ObservationEvidenceError:
            _add_note(primary, _CLOSE_FAILURE_NOTE)

    def record_terminal_preserving_primary(
        self,
        operation: ObservationOperation,
        argv: list[str],
        *,
        phase: EvidencePhase,
        reason: EvidenceReason,
        result: ExecResult | None,
        primary: BaseException,
    ) -> None:
        try:
            self.record_terminal(
                operation,
                argv,
                phase=phase,
                outcome="failure",
                reason=reason,
                result=result,
                exception=primary if reason == "provider_exception" else None,
            )
        except ObservationEvidenceError:
            _add_note(primary, _PERSISTENCE_FAILURE_NOTE)

    def _base_event(
        self, operation: ObservationOperation, argv: list[str]
    ) -> dict[str, object]:
        argv_count, argv_sha256 = _argv_metadata(argv)
        return {
            "argv_count": argv_count,
            "argv_sha256": argv_sha256,
            "exit_code": None,
            "operation": operation,
            "schema_version": _SCHEMA_VERSION,
            "sequence": self._sequence + 1,
        }

    def _append(self, event: dict[str, object]) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
        if self._sequence >= _MAX_EVENTS:
            raise ObservationEvidenceError("serialization", "evidence_too_large")
        payload = _serialize_event(event)
        if self._written_bytes + len(payload) > _MAX_LEDGER_BYTES:
            raise ObservationEvidenceError("serialization", "evidence_too_large")
        primary: ObservationEvidenceError | None = None
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except OSError as error:
            primary = ObservationEvidenceError(
                "atomic_persistence", "persistence_failed"
            )
            raise primary from error
        finally:
            if primary is not None:
                self._close_preserving_failure(primary)
        self._sequence += 1
        self._written_bytes += len(payload)

    def _close_preserving_failure(self, primary: BaseException) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            os.close(descriptor)
        except OSError:
            _add_note(primary, _CLOSE_FAILURE_NOTE)


def _validate_operation(operation: object) -> None:
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError("invalid observation evidence operation")


def _validate_start_taxonomy(operation: object, phase: object, reason: object) -> None:
    if operation == "audit":
        valid = phase == "serialization" and reason == "audit_started"
    else:
        valid = (
            operation in _COLLECTOR_OPERATIONS
            and phase == "provider_execution"
            and reason == "collector_started"
        )
    if not valid:
        raise ValueError("invalid observation evidence start taxonomy")


def _validate_terminal_taxonomy(
    operation: object, phase: object, outcome: object, reason: object
) -> None:
    if not isinstance(phase, str) or phase not in {
        "provider_execution",
        "result_validation",
        "parsing",
        "serialization",
        "atomic_persistence",
    }:
        raise ValueError("invalid observation evidence phase")
    if not isinstance(outcome, str) or outcome not in {"success", "failure"}:
        raise ValueError("invalid observation evidence outcome")
    if operation == "audit":
        valid_reason = (phase, outcome, reason) in _AUDIT_TERMINAL_TAXONOMY
    else:
        valid_reason = (
            operation in _COLLECTOR_OPERATIONS
            and (phase, outcome, reason) in _TERMINAL_TAXONOMY
        )
    if not isinstance(reason, str) or not valid_reason:
        raise ValueError("invalid observation evidence reason")


def _argv_metadata(argv: object) -> tuple[int, str]:
    if (
        type(argv) is not list
        or len(argv) > _MAX_ARGV_COUNT
        or any(not isinstance(item, str) for item in argv)
    ):
        raise ValueError("invalid observation evidence argv")
    try:
        serialized = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ObservationEvidenceError(
            "serialization", "serialization_failed"
        ) from None
    if len(serialized) > _MAX_ARGV_BYTES:
        raise ObservationEvidenceError("serialization", "evidence_too_large")
    return len(argv), hashlib.sha256(serialized).hexdigest()


def _output_metadata(value: str) -> dict[str, object]:
    encoded_bytes = 0
    hashed_bytes = 0
    digest = hashlib.sha256()
    for offset in range(0, len(value), 4_096):
        encoded = value[offset : offset + 4_096].encode("utf-8", errors="replace")
        encoded_bytes += len(encoded)
        remaining = _MAX_HASHED_OUTPUT_BYTES - hashed_bytes
        if remaining > 0:
            accepted = encoded[:remaining]
            digest.update(accepted)
            hashed_bytes += len(accepted)
    return {
        "encoded_bytes": encoded_bytes,
        "hashed_bytes": hashed_bytes,
        "sha256": digest.hexdigest(),
        "truncated": hashed_bytes < encoded_bytes,
    }


def _exception_class(exception: BaseException | None) -> str | None:
    if exception is None:
        return None
    name = type(exception).__name__
    return name if _EXCEPTION_CLASS.fullmatch(name) is not None else "Exception"


def _serialize_event(event: dict[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(
                event,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise ObservationEvidenceError(
            "serialization", "serialization_failed"
        ) from None
    if len(payload) > _MAX_EVENT_BYTES:
        raise ObservationEvidenceError("serialization", "evidence_too_large")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write made no progress")
        offset += written


def _validate_parent(path: Path) -> object:
    try:
        identity = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "parent_invalid"
        ) from error
    if not stat.S_ISDIR(identity.st_mode) or _is_link(identity) or resolved != path:
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")
    return identity


def _reject_existing_destination(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error
    raise ObservationEvidenceError("atomic_persistence", "destination_collision")


def _create_destination(path: Path, expected_parent: object) -> int:
    if _IS_WINDOWS:
        return _create_windows_destination(path, expected_parent)
    return _create_posix_destination(path, expected_parent)


def _create_posix_destination(path: Path, expected_parent: object) -> int:
    parent_descriptor = _open_posix_parent(path.parent, expected_parent)
    descriptor: int | None = None
    try:
        descriptor = _open_destination(path.name, dir_fd=parent_descriptor)
        _validate_destination_descriptor(descriptor)
        _validate_parent_identity(path.parent, expected_parent)
    except BaseException as error:
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            _close_descriptor_preserving(owned_descriptor, error)
        owned_parent = parent_descriptor
        parent_descriptor = -1
        _close_descriptor_preserving(owned_parent, error)
        raise

    owned_parent = parent_descriptor
    parent_descriptor = -1
    try:
        os.close(owned_parent)
    except OSError as error:
        close_error = ObservationEvidenceError("atomic_persistence", "close_failed")
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            _close_descriptor_preserving(owned_descriptor, close_error)
        raise close_error from error
    if descriptor is None:
        raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
    return descriptor


def _open_posix_parent(path: Path, expected_parent: object) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "parent_invalid"
        ) from error
    try:
        held_identity = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(held_identity.st_mode)
            or _is_link(held_identity)
            or not _same_identity(held_identity, expected_parent)
        ):
            raise ObservationEvidenceError("atomic_persistence", "parent_invalid")
    except BaseException as error:
        owned_descriptor = descriptor
        descriptor = -1
        _close_descriptor_preserving(owned_descriptor, error)
        raise
    return descriptor


def _create_windows_destination(path: Path, expected_parent: object) -> int:
    try:
        parent_handle = _open_windows_directory_handle(path.parent)
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "parent_invalid"
        ) from error
    descriptor: int | None = None
    try:
        _validate_windows_parent_handle(path.parent, parent_handle, expected_parent)
        descriptor = _open_windows_destination(path.name, parent_handle)
        _validate_destination_descriptor(descriptor)
        _validate_windows_parent_handle(path.parent, parent_handle, expected_parent)
    except BaseException as error:
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            _close_descriptor_preserving(owned_descriptor, error)
        owned_handle = parent_handle
        parent_handle = -1
        _close_windows_handle_preserving(owned_handle, error)
        raise

    owned_handle = parent_handle
    parent_handle = -1
    try:
        _close_windows_directory_handle(owned_handle)
    except OSError as error:
        close_error = ObservationEvidenceError("atomic_persistence", "close_failed")
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            _close_descriptor_preserving(owned_descriptor, close_error)
        raise close_error from error
    if descriptor is None:
        raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
    return descriptor


def _open_destination(path: str | Path, *, dir_fd: int | None = None) -> int:
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if dir_fd is None:
            descriptor = os.open(path, flags, 0o600)
        else:
            descriptor = os.open(path, flags, 0o600, dir_fd=dir_fd)
    except FileExistsError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "destination_collision"
        ) from error
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error
    return descriptor


def _validate_destination_descriptor(descriptor: int) -> None:
    try:
        identity = os.fstat(descriptor)
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error
    if not stat.S_ISREG(identity.st_mode):
        raise ObservationEvidenceError("atomic_persistence", "persistence_failed")


def _validate_parent_identity(path: Path, expected_parent: object) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "parent_invalid"
        ) from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or _is_link(current)
        or not _same_identity(current, expected_parent)
    ):
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")


def _same_identity(left: object, right: object) -> bool:
    return (
        getattr(left, "st_dev", None),
        getattr(left, "st_ino", None),
    ) == (
        getattr(right, "st_dev", None),
        getattr(right, "st_ino", None),
    )


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("creation_time_low", ctypes.c_uint32),
        ("creation_time_high", ctypes.c_uint32),
        ("access_time_low", ctypes.c_uint32),
        ("access_time_high", ctypes.c_uint32),
        ("write_time_low", ctypes.c_uint32),
        ("write_time_high", ctypes.c_uint32),
        ("volume_serial_number", ctypes.c_uint32),
        ("file_size_high", ctypes.c_uint32),
        ("file_size_low", ctypes.c_uint32),
        ("number_of_links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("maximum_length", ctypes.c_ushort),
        ("buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("root_directory", ctypes.c_void_p),
        ("object_name", ctypes.POINTER(_UnicodeString)),
        ("attributes", ctypes.c_ulong),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("status_or_pointer", ctypes.c_void_p),
        ("information", ctypes.c_size_t),
    ]


class _WindowsFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int | None: ...


class _WindowsLoader(Protocol):
    def __call__(self, name: str, *, use_last_error: bool) -> object: ...


class _MsvcrtModule(Protocol):
    def open_osfhandle(self, handle: int, flags: int) -> int: ...


def _windows_function(name: str, *, library: str = "kernel32") -> _WindowsFunction:
    loader = getattr(ctypes, "WinDLL", None)
    if not callable(loader):
        raise OSError("Windows API is unavailable")
    windows_library = cast(_WindowsLoader, loader)(library, use_last_error=True)
    return cast(_WindowsFunction, getattr(windows_library, name))


def _windows_last_error() -> int:
    get_last_error = getattr(ctypes, "get_last_error", None)
    if not callable(get_last_error):
        return 0
    return cast(Callable[[], int], get_last_error)()


def _open_windows_directory_handle(path: Path) -> int:
    create_file = _windows_function("CreateFileW")
    create_file.argtypes = cast(
        list[object],
        [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ],
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x0080,
        0x00000001 | 0x00000002,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle:
        raise OSError(_windows_last_error(), "Windows directory handle open failed")
    return int(handle)


def _open_windows_destination(name: str, parent_handle: int) -> int:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    object_name = _UnicodeString(
        encoded_length,
        encoded_length + ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(name_buffer, ctypes.c_void_p),
    )
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        ctypes.c_void_p(parent_handle),
        ctypes.pointer(object_name),
        0x00000040,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    child_handle = ctypes.c_void_p()
    nt_create_file = _windows_function("NtCreateFile", library="ntdll")
    nt_create_file.argtypes = cast(
        list[object],
        [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ],
    )
    nt_create_file.restype = ctypes.c_long
    status = nt_create_file(
        ctypes.byref(child_handle),
        0x00000004 | 0x00000080 | 0x00100000,
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        2,
        0x00000020 | 0x00000040 | 0x00200000,
        None,
        0,
    )
    if status is None:
        raise ObservationEvidenceError("atomic_persistence", "persistence_failed")
    unsigned_status = status & 0xFFFFFFFF
    if status < 0 or child_handle.value is None:
        failure = ObservationEvidenceError(
            "atomic_persistence",
            (
                "destination_collision"
                if unsigned_status == 0xC0000035
                else "persistence_failed"
            ),
        )
        if child_handle.value is not None:
            owned_failed_handle = int(child_handle.value)
            child_handle.value = None
            _close_windows_handle_preserving(owned_failed_handle, failure)
        raise failure
    owned_handle = int(child_handle.value)
    try:
        return _windows_handle_to_descriptor(owned_handle)
    except BaseException as error:
        _close_windows_handle_preserving(owned_handle, error)
        if isinstance(error, ObservationEvidenceError):
            raise
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error


def _windows_handle_to_descriptor(handle: int) -> int:
    msvcrt = cast(_MsvcrtModule, importlib.import_module("msvcrt"))
    try:
        return msvcrt.open_osfhandle(handle, os.O_APPEND | getattr(os, "O_BINARY", 0))
    except OSError as error:
        raise ObservationEvidenceError(
            "atomic_persistence", "persistence_failed"
        ) from error


def _validate_windows_parent_handle(
    path: Path, handle: int, expected_parent: object
) -> None:
    get_information = _windows_function("GetFileInformationByHandle")
    get_information.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    get_information.restype = ctypes.c_int
    information = _ByHandleFileInformation()
    if not get_information(ctypes.c_void_p(handle), ctypes.byref(information)):
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")
    if not information.file_attributes & 0x00000010:
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")
    if information.file_attributes & 0x00000400:
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")
    _validate_parent_identity(path, expected_parent)
    expected_ino = getattr(expected_parent, "st_ino", 0)
    handle_ino = (information.file_index_high << 32) | information.file_index_low
    if expected_ino and handle_ino and expected_ino != handle_ino:
        raise ObservationEvidenceError("atomic_persistence", "parent_invalid")


def _close_windows_directory_handle(handle: int) -> None:
    close_handle = _windows_function("CloseHandle")
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        raise OSError(_windows_last_error(), "Windows directory handle close failed")


def _close_descriptor_preserving(descriptor: int, primary: BaseException) -> None:
    try:
        os.close(descriptor)
    except OSError:
        _add_note(primary, _CLOSE_FAILURE_NOTE)


def _close_windows_handle_preserving(handle: int, primary: BaseException) -> None:
    try:
        _close_windows_directory_handle(handle)
    except OSError:
        _add_note(primary, _CLOSE_FAILURE_NOTE)


def _is_link(identity: object) -> bool:
    mode = getattr(identity, "st_mode", 0)
    attributes = getattr(identity, "st_file_attributes", 0)
    return stat.S_ISLNK(mode) or bool(_REPARSE_POINT and attributes & _REPARSE_POINT)


def _add_note(primary: BaseException, note: str) -> None:
    if note not in getattr(primary, "__notes__", ()):
        primary.add_note(note)
