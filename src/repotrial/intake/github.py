"""GitHub repository intake boundary."""

import asyncio
import os
import re
import shutil
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
        message = f"repository intake failed: {operation}"
        if returncode is not None:
            message = f"{message} (returncode={returncode})"
        super().__init__(message)


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
    _claim_destination(destination)

    try:
        clone_arguments = [
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "clone",
            "--filter=blob:none",
        ]
        if requested_ref is not None:
            clone_arguments.append(f"--branch={requested_ref}")
        clone_arguments.extend(("--", clone_source, str(destination)))
        await _run_git("clone", *clone_arguments)
        sha_output = await _run_git(
            "resolve", "-C", str(destination), "rev-parse", "--verify", "HEAD^{commit}"
        )
        commit_sha = sha_output.decode("ascii", errors="replace").strip().lower()
        if _FULL_COMMIT_SHA.fullmatch(commit_sha) is None:
            raise RepoIntakeError("invalid_commit_sha")
        return commit_sha, destination
    except asyncio.CancelledError:
        _remove_owned_destination(destination)
        raise
    except RepoIntakeError:
        _remove_owned_destination(destination)
        raise


def _validate_requested_ref(requested_ref: str | None) -> None:
    if requested_ref is not None and (
        not requested_ref or _contains_control(requested_ref)
    ):
        raise RepoIntakeError("invalid_requested_ref")


def _normalize_clone_source(source: str) -> str:
    if "://" in source:
        parsed = _parse_credential_free_https_url(source)
        if parsed is None:
            raise RepoIntakeError("invalid_clone_source")
        return source

    source_path = Path(source)
    if source_path.exists():
        return str(source_path.resolve())

    parsed = _parse_credential_free_https_url(source)
    if parsed is None:
        raise RepoIntakeError("invalid_clone_source")
    return source


def _parse_credential_free_https_url(url: str) -> SplitResult | None:
    if (
        not url
        or _contains_control(url)
        or "%" in url
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
    if not destination.parent.is_dir():
        raise RepoIntakeError("destination_parent")
    if destination.exists() or destination.is_symlink():
        raise RepoIntakeError("destination_exists")
    return destination.resolve(strict=False)


def _claim_destination(destination: Path) -> None:
    try:
        destination.mkdir(exist_ok=False)
    except FileExistsError:
        raise RepoIntakeError("destination_exists") from None
    except OSError:
        raise RepoIntakeError("destination_create") from None


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
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_environment(),
        )
    except FileNotFoundError as error:
        raise RepoIntakeError("git_unavailable") from error
    except OSError:
        raise RepoIntakeError(f"{operation}_spawn") from None

    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=COMMAND_TIMEOUT_SECONDS
        )
    except TimeoutError as error:
        await _kill_and_reap(process)
        raise RepoIntakeError(f"{operation}_timeout") from error
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise

    if process.returncode != 0:
        raise RepoIntakeError(operation, process.returncode) from None
    return stdout


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=REAP_TIMEOUT_SECONDS)
    except TimeoutError:
        pass


def _remove_owned_destination(destination: Path) -> None:
    if not destination.is_symlink():
        shutil.rmtree(destination, ignore_errors=True)
