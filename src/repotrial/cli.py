from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer

from repotrial.config import create_run_layout, generate_run_id


def _is_supported_github_url(url: str) -> bool:
    parsed = urlsplit(url)
    path_components = parsed.path.split("/")
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
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
