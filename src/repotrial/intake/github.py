"""GitHub repository intake boundary."""

import asyncio
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from string import ascii_letters, digits
from urllib.parse import SplitResult, urlsplit

from repotrial.domain.models import PinnedRepo, RepoRef

COMMAND_TIMEOUT_SECONDS = 120
REAP_TIMEOUT_SECONDS = 5
_FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_RAW_URI_CHARACTERS = frozenset(ascii_letters + digits + "-._~:/?#[]@!$&'()*+,;=%")


class RepoIntakeError(RuntimeError):
    """A sanitized repository-intake failure."""

    def __init__(self, operation: str, returncode: int | None = None) -> None:
        self.operation = operation
        self.returncode = returncode
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
    claim = _claim_destination(destination)

    try:
        clone_arguments = [
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "clone",
            "--filter=blob:none",
        ]
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
    except asyncio.CancelledError:
        _remove_owned_destination(claim)
        raise
    except RepoIntakeError:
        _remove_owned_destination(claim)
        raise
    finally:
        _close_directory_fd(claim.directory_fd)


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
    except OSError:
        if identity is not None:
            _remove_owned_destination(
                _DestinationClaim(
                    path=destination,
                    device=identity.st_dev,
                    inode=identity.st_ino,
                    file_type=stat.S_IFMT(identity.st_mode),
                    directory_fd=None,
                )
            )
        _close_directory_fd(directory_fd)
        raise RepoIntakeError("destination_claim") from None
    except RepoIntakeError:
        if identity is not None:
            _remove_owned_destination(
                _DestinationClaim(
                    path=destination,
                    device=identity.st_dev,
                    inode=identity.st_ino,
                    file_type=stat.S_IFMT(identity.st_mode),
                    directory_fd=None,
                )
            )
        _close_directory_fd(directory_fd)
        raise
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


async def _run_git(operation: str, *arguments: str) -> bytes:
    process: asyncio.subprocess.Process | None = None
    communicate_failed = False
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                "git",
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_git_environment(),
            )
            try:
                stdout, _stderr = await process.communicate()
            except OSError:
                communicate_failed = True
    except TimeoutError as error:
        if process is not None:
            await _kill_and_reap(process)
        raise RepoIntakeError(f"{operation}_timeout") from error
    except asyncio.CancelledError:
        if process is not None:
            await _kill_and_reap(process)
        raise
    except FileNotFoundError as error:
        raise RepoIntakeError("git_unavailable") from error
    except OSError:
        if process is not None:
            await _kill_and_reap(process)
        raise RepoIntakeError(f"{operation}_io") from None

    assert process is not None
    if communicate_failed:
        await _kill_and_reap(process)
        raise RepoIntakeError(f"{operation}_io") from None
    if process.returncode != 0:
        raise RepoIntakeError(operation, process.returncode) from None
    return stdout


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=REAP_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        pass


def _remove_owned_destination(claim: _DestinationClaim) -> None:
    if claim.directory_fd is not None:
        try:
            leased_identity = os.fstat(claim.directory_fd)
        except OSError:
            return
        if (
            leased_identity.st_dev != claim.device
            or leased_identity.st_ino != claim.inode
            or stat.S_IFMT(leased_identity.st_mode) != claim.file_type
        ):
            return
    try:
        current_identity = claim.path.lstat()
    except OSError:
        return
    if (
        not stat.S_ISDIR(current_identity.st_mode)
        or current_identity.st_dev != claim.device
        or current_identity.st_ino != claim.inode
        or stat.S_IFMT(current_identity.st_mode) != claim.file_type
    ):
        return
    try:
        quarantine = Path(
            tempfile.mkdtemp(prefix=".repotrial-cleanup-", dir=claim.path.parent)
        )
    except OSError:
        return
    quarantined_path = quarantine / "owned"
    try:
        os.rename(claim.path, quarantined_path)
    except OSError:
        _remove_empty_directory(quarantine)
        return
    try:
        quarantined_identity = quarantined_path.lstat()
    except OSError:
        return
    if (
        not stat.S_ISDIR(quarantined_identity.st_mode)
        or quarantined_identity.st_dev != claim.device
        or quarantined_identity.st_ino != claim.inode
        or stat.S_IFMT(quarantined_identity.st_mode) != claim.file_type
    ):
        return
    try:
        shutil.rmtree(quarantined_path, ignore_errors=True)
    except OSError:
        return
    _remove_empty_directory(quarantine)


def _remove_empty_directory(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def _close_directory_fd(directory_fd: int | None) -> None:
    if directory_fd is None:
        return
    try:
        os.close(directory_fd)
    except OSError:
        pass
