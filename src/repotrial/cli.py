from collections.abc import Callable
from pathlib import Path
from string import ascii_letters, digits, hexdigits
from typing import Annotated
from urllib.parse import urlsplit

import typer

from repotrial.config import create_run_layout, generate_run_id

_RAW_URI_CHARACTERS = frozenset(ascii_letters + digits + "-._~:/?#[]@!$&'()*+,;=%")


def _has_valid_raw_uri_form(url: str) -> bool:
    if not url or any(character not in _RAW_URI_CHARACTERS for character in url):
        return False
    return all(
        character != "%"
        or (
            index + 2 < len(url)
            and url[index + 1] in hexdigits
            and url[index + 2] in hexdigits
        )
        for index, character in enumerate(url)
    )


def _is_supported_github_url(url: str) -> bool:
    if not _has_valid_raw_uri_form(url):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    path_components = parsed.path.split("/")
    return (
        parsed.scheme == "https"
        and hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and "?" not in url
        and "#" not in url
        and len(path_components) == 3
        and path_components[0] == ""
        and all(path_components[1:])
    )


def create_app(
    artifacts_root: Path = Path("artifacts"),
    run_id_generator: Callable[[], str] = generate_run_id,
) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def doctor() -> None:
        typer.echo("ok")

    @app.command()
    def inspect(
        url: str,
        dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    ) -> None:
        if not dry_run:
            raise typer.BadParameter("--dry-run is required")
        if not _is_supported_github_url(url):
            raise typer.BadParameter("URL must be a GitHub HTTPS owner/repository URL")

        run_id, run_path = create_run_layout(artifacts_root, run_id_generator)
        typer.echo(f"run_id={run_id}")
        typer.echo(f"artifact_path={run_path}")

    return app


app = create_app()
