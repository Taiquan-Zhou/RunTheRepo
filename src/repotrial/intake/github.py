"""GitHub repository intake boundary."""

import asyncio
import os
import re
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from string import ascii_letters, digits
from typing import Literal
from urllib.parse import SplitResult, urlsplit

from repotrial.domain.models import PinnedRepo, RepoRef

COMMAND_TIMEOUT_SECONDS = 120
REAP_TIMEOUT_SECONDS = 5
NETWORK_PREPARATION_TIMEOUT_SECONDS = 300
NETWORK_MAX_ATTEMPTS = 2
NETWORK_RETRY_DELAY_SECONDS = 1
_MAX_GIT_OUTPUT_BYTES = 65_536
_GIT_READ_CHUNK_BYTES = 8_192
_FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_RAW_URI_CHARACTERS = frozenset(ascii_letters + digits + "-._~:/?#[]@!$&'()*+,;=%")
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

type GitFailureClass = Literal[
    "dns",
    "transport",
    "http",
    "authentication",
    "missing_ref",
    "local_io",
    "unknown",
]


class RepoIntakeError(RuntimeError):
    """A sanitized repository-intake failure."""

    def __init__(
        self,
        operation: str,
        returncode: int | None = None,
        *,
        failure_class: GitFailureClass | None = None,
        process_cleanup_succeeded: bool | None = None,
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.failure_class = failure_class
        self.process_cleanup_succeeded = process_cleanup_succeeded
        message = f"repository intake failed: {operation}"
        if returncode is not None:
            message = f"{message} (returncode={returncode})"
        super().__init__(message)


@dataclass(frozen=True)
class _DestinationClaim:
    path: Path
    device: int
    inode: int
    file_type: int
    directory_fd: int | None


def parse_github_url(url: str, requested_ref: str | None = None) -> RepoRef:
    """Validate and canonicalize a GitHub HTTPS owner/repository URL."""
    _validate_requested_ref(requested_ref)
    parsed = _parse_credential_free_https_url(url)
    if parsed is None or parsed.hostname is None:
        raise RepoIntakeError("invalid_github_url")
    if parsed.hostname.lower() != "github.com":
        raise RepoIntakeError("invalid_github_url")
    if parsed.netloc.lower() != "github.com":
        raise RepoIntakeError("invalid_github_url")

    components = parsed.path.split("/")
    if len(components) != 3 or components[0] or not all(components[1:]):
        raise RepoIntakeError("invalid_github_url")
    owner, repository = components[1:]
    if any(
        component in {".", ".."} or "\\" in component for component in components[1:]
    ):
        raise RepoIntakeError("invalid_github_url")
    if repository.endswith(".git"):
        repository = repository.removesuffix(".git")
    if not repository:
        raise RepoIntakeError("invalid_github_url")

    return RepoRef(
        url=f"https://github.com/{owner}/{repository}",
        owner=owner,
        repo=repository,
        requested_ref=requested_ref,
    )


async def pin_repository(
    url: str,
    dest: Path,
    requested_ref: str | None = None,
) -> PinnedRepo:
    """Clone a validated GitHub repository and return its immutable commit pin."""
    repo = parse_github_url(url, requested_ref)
    commit_sha, local_path = await clone_and_resolve(
        repo.url, dest, requested_ref=repo.requested_ref
    )
    return PinnedRepo(repo=repo, commit_sha=commit_sha, local_path=local_path)


async def clone_and_resolve(
    source: str,
    dest: Path,
    requested_ref: str | None = None,
) -> tuple[str, Path]:
    """Clone a trusted local fixture or safe HTTPS source and resolve its HEAD."""
    _validate_requested_ref(requested_ref)
    clone_source = _normalize_clone_source(source)
    destination = _normalize_destination(dest)

    parsed_source = _parse_credential_free_https_url(clone_source)
    is_remote = parsed_source is not None
    is_github_remote = bool(
        is_remote
        and parsed_source is not None
        and parsed_source.hostname is not None
        and parsed_source.hostname.casefold() == "github.com"
    )

    async def run_attempt() -> tuple[str, Path]:
        git_arguments = [
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
        ]
        if is_remote:
            git_arguments.extend(("-c", "http.version=HTTP/1.1"))
        is_exact_remote_commit = bool(
            is_remote
            and requested_ref is not None
            and _FULL_COMMIT_SHA.fullmatch(requested_ref)
        )
        if is_exact_remote_commit:
            assert requested_ref is not None
            clone_arguments = [
                *git_arguments,
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--no-tags",
                "--",
                clone_source,
                str(destination),
            ]
            await _run_git("clone", *clone_arguments)
            repository_arguments = (*git_arguments, "-C", str(destination))
            await _run_git(
                "fetch",
                *repository_arguments,
                "fetch",
                "--filter=blob:none",
                "--no-tags",
                "--",
                "origin",
                requested_ref,
            )
            await _run_git(
                "checkout",
                *repository_arguments,
                "checkout",
                "--detach",
                requested_ref,
            )
        else:
            clone_arguments = [*git_arguments, "clone", "--filter=blob:none"]
            if (
                requested_ref is not None
                and _FULL_COMMIT_SHA.fullmatch(requested_ref) is None
            ):
                clone_arguments.append(f"--branch={requested_ref}")
            clone_arguments.extend(("--", clone_source, str(destination)))
            await _run_git("clone", *clone_arguments)
            if requested_ref is not None and _FULL_COMMIT_SHA.fullmatch(requested_ref):
                await _run_git(
                    "checkout",
                    "-C",
                    str(destination),
                    "checkout",
                    "--detach",
                    requested_ref,
                )
        sha_output = await _run_git(
            "resolve", "-C", str(destination), "rev-parse", "--verify", "HEAD^{commit}"
        )
        commit_sha = sha_output.decode("ascii", errors="replace").strip().lower()
        if _FULL_COMMIT_SHA.fullmatch(commit_sha) is None:
            raise RepoIntakeError("invalid_commit_sha")
        if (
            requested_ref is not None
            and _FULL_COMMIT_SHA.fullmatch(requested_ref) is not None
            and commit_sha != requested_ref
        ):
            raise RepoIntakeError("commit_sha_mismatch")
        return commit_sha, destination

    async def run_preparation() -> tuple[str, Path]:
        attempt = 0
        while True:
            attempt += 1
            claim = _claim_destination(destination)
            try:
                return await run_attempt()
            except asyncio.CancelledError:
                _remove_owned_destination(claim)
                raise
            except RepoIntakeError as error:
                owner_cleanup_succeeded = _remove_owned_destination(claim)
                can_retry = (
                    is_github_remote
                    and attempt < NETWORK_MAX_ATTEMPTS
                    and _is_retryable_network_error(error)
                    and owner_cleanup_succeeded
                    and _destination_is_absent(destination)
                )
                if not can_retry:
                    raise
                await asyncio.sleep(NETWORK_RETRY_DELAY_SECONDS)
            finally:
                _close_directory_fd(claim.directory_fd)

    if not is_github_remote:
        return await run_preparation()
    try:
        async with asyncio.timeout(NETWORK_PREPARATION_TIMEOUT_SECONDS):
            return await run_preparation()
    except TimeoutError:
        raise RepoIntakeError("preparation_timeout") from None


def _is_retryable_network_error(error: RepoIntakeError) -> bool:
    if error.operation in {"clone_timeout", "fetch_timeout", "checkout_timeout"}:
        return error.process_cleanup_succeeded is True
    if error.operation in {"clone", "fetch", "checkout"}:
        if error.process_cleanup_succeeded is False:
            return False
        return error.failure_class in {"dns", "transport"}
    return False


def _destination_is_absent(destination: Path) -> bool:
    return not destination.exists() and not destination.is_symlink()


def _validate_requested_ref(requested_ref: str | None) -> None:
    if requested_ref is not None and (
        not requested_ref or _contains_control(requested_ref)
    ):
        raise RepoIntakeError("invalid_requested_ref")


def _normalize_clone_source(source: str) -> str:
    local_source = _resolve_existing_local_source(source)
    if local_source is not None:
        return local_source

    parsed = _parse_credential_free_https_url(source)
    if parsed is None:
        raise RepoIntakeError("invalid_clone_source")
    return source


def _resolve_existing_local_source(source: str) -> str | None:
    if "://" in source and not _is_windows_drive_path(source):
        return None
    source_path = Path(source)
    if source_path.exists():
        return str(source_path.resolve())
    return None


def _is_windows_drive_path(value: str) -> bool:
    return (
        len(value) >= 3
        and value[0] in ascii_letters
        and value[1] == ":"
        and value[2] in {"/", "\\"}
    )


def _parse_credential_free_https_url(url: str) -> SplitResult | None:
    if (
        not url
        or _contains_control(url)
        or "%" in url
        or "?" in url
        or "#" in url
        or any(character not in _RAW_URI_CHARACTERS for character in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() != parsed.hostname.lower()
    ):
        return None
    return parsed


def _contains_control(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _normalize_destination(dest: Path) -> Path:
    destination = Path(dest)
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError:
        raise RepoIntakeError("destination_parent") from None
    if not parent.is_dir() or not destination.name:
        raise RepoIntakeError("destination_parent")
    return parent / destination.name


def _claim_destination(destination: Path) -> _DestinationClaim:
    try:
        destination.mkdir(exist_ok=False)
    except FileExistsError:
        raise RepoIntakeError("destination_exists") from None
    except OSError:
        raise RepoIntakeError("destination_create") from None
    directory_fd: int | None = None
    identity: os.stat_result | None = None
    try:
        identity = destination.lstat()
        if not stat.S_ISDIR(identity.st_mode):
            raise RepoIntakeError("destination_claim")

        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            directory_fd = os.open(destination, flags)
        except PermissionError:
            if os.name != "nt":
                raise

        if directory_fd is not None:
            os.set_inheritable(directory_fd, False)
            opened_identity = os.fstat(directory_fd)
            current_identity = destination.lstat()
            if (
                opened_identity.st_dev != identity.st_dev
                or opened_identity.st_ino != identity.st_ino
                or stat.S_IFMT(opened_identity.st_mode) != stat.S_IFMT(identity.st_mode)
                or current_identity.st_dev != identity.st_dev
                or current_identity.st_ino != identity.st_ino
                or stat.S_IFMT(current_identity.st_mode)
                != stat.S_IFMT(identity.st_mode)
            ):
                raise RepoIntakeError("destination_claim")
            identity = opened_identity
    except (OSError, RepoIntakeError) as error:
        if identity is not None:
            _remove_owned_destination(
                _DestinationClaim(
                    path=destination,
                    device=identity.st_dev,
                    inode=identity.st_ino,
                    file_type=stat.S_IFMT(identity.st_mode),
                    directory_fd=directory_fd,
                ),
                allow_unanchored_empty_root_removal=True,
            )
        _close_directory_fd(directory_fd)
        if isinstance(error, RepoIntakeError):
            raise
        raise RepoIntakeError("destination_claim") from None
    assert identity is not None
    return _DestinationClaim(
        path=destination,
        device=identity.st_dev,
        inode=identity.st_ino,
        file_type=stat.S_IFMT(identity.st_mode),
        directory_fd=directory_fd,
    )


def _git_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_") and name.upper() != "SSH_ASKPASS"
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    return environment


def _classify_git_failure(stderr: bytes) -> GitFailureClass:
    normalized = stderr.decode("utf-8", "ignore").casefold()
    if (
        "could not resolve host" in normalized
        or "could not resolve proxy" in normalized
    ):
        return "dns"
    if "authentication failed" in normalized or "permission denied" in normalized:
        return "authentication"
    if "requested url returned error" in normalized:
        return "http"
    if "couldn't find remote ref" in normalized or "not our ref" in normalized:
        return "missing_ref"
    if any(
        marker in normalized
        for marker in (
            "certificate verify failed",
            "ssl certificate problem",
            "unable to get local issuer certificate",
            "self signed certificate",
        )
    ):
        return "unknown"
    if "failed to connect" in normalized or "tls" in normalized:
        return "transport"
    if any(
        marker in normalized
        for marker in (
            "connection reset by peer",
            "recv failure",
            "curl 56",
            "curl 28",
            "operation timed out",
            "remote peer closed",
        )
    ):
        return "transport"
    return "unknown"


async def _read_bounded_git_stream(stream: asyncio.StreamReader) -> bytes:
    retained = bytearray()
    while True:
        chunk = await stream.read(_GIT_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(retained)
        remaining = _MAX_GIT_OUTPUT_BYTES - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])


async def _collect_git_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes, int]:
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    if stdout_stream is None or stderr_stream is None:
        raise OSError("git subprocess pipes are unavailable")

    stdout_task = asyncio.create_task(_read_bounded_git_stream(stdout_stream))
    stderr_task = asyncio.create_task(_read_bounded_git_stream(stderr_stream))
    try:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        returncode = await process.wait()
        return stdout, stderr, returncode
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


async def _run_git(operation: str, *arguments: str) -> bytes:
    process: asyncio.subprocess.Process | None = None
    stdout = b""
    stderr = b""
    returncode: int | None = None
    failure_operation: str | None = None
    failure_class: GitFailureClass | None = None
    process_cleanup_succeeded: bool | None = None
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
            try:
                process = await asyncio.create_subprocess_exec(
                    "git",
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_git_environment(),
                    start_new_session=(os.name == "posix"),
                )
            except FileNotFoundError:
                failure_operation = "git_unavailable"
                failure_class = "local_io"
            except OSError:
                failure_operation = f"{operation}_io"
                failure_class = "local_io"
            if process is not None:
                try:
                    stdout, stderr, returncode = await _collect_git_output(process)
                except OSError:
                    failure_operation = f"{operation}_io"
                    failure_class = "local_io"
    except TimeoutError:
        if process is not None:
            process_cleanup_succeeded = await _kill_and_reap(process)
        raise RepoIntakeError(
            f"{operation}_timeout",
            process_cleanup_succeeded=process_cleanup_succeeded,
        ) from None
    except asyncio.CancelledError:
        if process is not None:
            await _kill_and_reap(process)
        raise

    if failure_operation is not None:
        if process is not None:
            process_cleanup_succeeded = await _kill_and_reap(process)
        raise RepoIntakeError(
            failure_operation,
            failure_class=failure_class,
            process_cleanup_succeeded=process_cleanup_succeeded,
        ) from None
    assert process is not None
    assert returncode is not None
    if returncode != 0:
        raise RepoIntakeError(
            operation,
            returncode,
            failure_class=_classify_git_failure(stderr),
        ) from None
    return stdout


async def _kill_and_reap(process: asyncio.subprocess.Process) -> bool:
    group_cleanup_succeeded = _kill_process_group(process)
    try:
        returncode = await asyncio.wait_for(
            process.wait(), timeout=REAP_TIMEOUT_SECONDS
        )
    except (OSError, ProcessLookupError, TimeoutError):
        return False
    return group_cleanup_succeeded and (
        returncode is not None or process.returncode is not None
    )


def _kill_process_group(process: asyncio.subprocess.Process) -> bool:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return True
        except (AttributeError, OSError):
            pass
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    return os.name != "posix" and process.returncode is not None


def _remove_owned_destination(
    claim: _DestinationClaim,
    *,
    allow_unanchored_empty_root_removal: bool = False,
) -> bool:
    supports_fd_anchored_cleanup = _supports_fd_anchored_cleanup()
    if not supports_fd_anchored_cleanup and not allow_unanchored_empty_root_removal:
        return False
    if claim.directory_fd is not None:
        if not _directory_fd_matches_claim(claim):
            return False
        if supports_fd_anchored_cleanup:
            try:
                entries_removed = _remove_directory_entries(claim.directory_fd)
            except RecursionError:
                return False
            if not entries_removed or not _directory_fd_matches_claim(claim):
                return False
    if not _path_matches_claim(claim):
        return False
    try:
        claim.path.rmdir()
    except OSError:
        return False
    return True


def _supports_fd_anchored_cleanup() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.listdir in os.supports_fd
        and os.stat in os.supports_follow_symlinks
        and bool(_O_DIRECTORY)
        and bool(_O_NOFOLLOW)
    )


def _remove_directory_entries(directory_fd: int) -> bool:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError:
        return False
    return all(_remove_directory_entry(directory_fd, name) for name in names)


def _remove_directory_entry(parent_fd: int, name: str) -> bool:
    try:
        entry_identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISDIR(entry_identity.st_mode) and not _is_reparse_point(entry_identity):
        return _remove_child_directory(parent_fd, name, entry_identity)
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def _remove_child_directory(
    parent_fd: int, name: str, expected_identity: os.stat_result
) -> bool:
    child_fd: int | None = None
    opened_identity: os.stat_result | None = None
    try:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        os.set_inheritable(child_fd, False)
        opened_identity = os.fstat(child_fd)
        if not _same_identity(opened_identity, expected_identity):
            return False
        if not _remove_directory_entries(child_fd):
            return False
        if not _same_identity(os.fstat(child_fd), opened_identity):
            return False
    except OSError:
        return False
    finally:
        _close_directory_fd(child_fd)

    assert opened_identity is not None
    try:
        current_identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(current_identity, opened_identity):
            return False
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def _directory_fd_matches_claim(claim: _DestinationClaim) -> bool:
    assert claim.directory_fd is not None
    try:
        identity = os.fstat(claim.directory_fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(identity.st_mode)
        and identity.st_dev == claim.device
        and identity.st_ino == claim.inode
        and stat.S_IFMT(identity.st_mode) == claim.file_type
    )


def _path_matches_claim(claim: _DestinationClaim) -> bool:
    try:
        identity = claim.path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(identity.st_mode)
        and identity.st_dev == claim.device
        and identity.st_ino == claim.inode
        and stat.S_IFMT(identity.st_mode) == claim.file_type
    )


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    )


def _is_reparse_point(identity: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(
        reparse_point and getattr(identity, "st_file_attributes", 0) & reparse_point
    )


def _close_directory_fd(directory_fd: int | None) -> None:
    if directory_fd is None:
        return
    try:
        os.close(directory_fd)
    except OSError:
        pass
