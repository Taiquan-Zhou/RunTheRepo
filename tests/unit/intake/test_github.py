import asyncio
from pathlib import Path

import pytest

from repotrial.domain.models import RepoRef
from repotrial.intake import github


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"fatal: unable to access: Could not resolve host", "dns"),
        (b"fatal: unable to access: Failed to connect", "transport"),
        (
            b"fatal: unable to access: The requested URL returned error: 503",
            "http",
        ),
        (b"fatal: Authentication failed", "authentication"),
        (b"fatal: couldn't find remote ref pinned", "missing_ref"),
        (b"unrecognized secret text", "unknown"),
    ],
)
def test_git_failure_classification_is_sanitized(stderr: bytes, expected: str) -> None:
    assert github._classify_git_failure(stderr) == expected


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            (
                b"could not resolve host; authentication failed; "
                b"requested URL returned error: 503; couldn't find remote ref; "
                b"failed to connect"
            ),
            "dns",
        ),
        (
            (
                b"authentication failed; requested URL returned error: 503; "
                b"couldn't find remote ref; failed to connect"
            ),
            "authentication",
        ),
        (
            (
                b"requested URL returned error: 503; couldn't find remote ref; "
                b"failed to connect"
            ),
            "http",
        ),
        (
            b"couldn't find remote ref; failed to connect",
            "missing_ref",
        ),
        (b"failed to connect; unrelated token", "transport"),
        (b"unrecognized secret text", "unknown"),
    ],
)
def test_git_failure_classification_obeys_fixed_priority(
    stderr: bytes, expected: str
) -> None:
    assert github._classify_git_failure(stderr) == expected


def test_parse_github_url_canonicalizes_case_and_literal_git_suffix() -> None:
    assert callable(getattr(github, "parse_github_url", None))

    repo = github.parse_github_url("https://GitHub.com/Owner/Project.git")

    assert repo == RepoRef(
        url="https://github.com/Owner/Project",
        owner="Owner",
        repo="Project",
        requested_ref=None,
    )


def test_parse_github_url_preserves_requested_branch_or_tag_exactly() -> None:
    repo = github.parse_github_url(
        "https://github.com/Owner/Project", requested_ref="release/v1"
    )

    assert repo.requested_ref == "release/v1"


@pytest.mark.parametrize(
    ("url", "requested_ref"),
    [
        ("http://github.com/owner/repo", None),
        ("https://user@github.com/owner/repo", None),
        ("https://github.com:443/owner/repo", None),
        ("https://github.com/owner/repo/", None),
        ("https://github.com/owner//repo", None),
        ("https://github.com/owner/repo?ref=main", None),
        ("https://github.com/owner/repo#readme", None),
        ("https://github.com/owner/repo?", None),
        ("https://github.com/owner/repo#", None),
        ("https://github.com/owner/repo%2Egit", None),
        ("https://github.com/owner/repo with space", None),
        ("https://github.com/owner/repo\n", None),
        ("https://example.com/owner/repo", None),
        ("https://github.com/owner/repo", ""),
        ("https://github.com/owner/repo", "main\nnext"),
    ],
)
def test_parse_github_url_rejects_ambiguous_or_unsafe_input(
    url: str, requested_ref: str | None
) -> None:
    with pytest.raises(github.RepoIntakeError):
        github.parse_github_url(url, requested_ref)


def test_pin_repository_rejects_invalid_url_before_destination_creation(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "clone destination"

    with pytest.raises(github.RepoIntakeError):
        asyncio.run(
            github.pin_repository("https://example.com/owner/repo", destination)
        )

    assert not destination.exists()
