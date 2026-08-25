from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from repotrial.config import create_run_layout, generate_run_id
from repotrial.intake.github import RepoIntakeError, parse_github_url


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
        try:
            parse_github_url(url)
        except RepoIntakeError:
            raise typer.BadParameter(
                "URL must be a GitHub HTTPS owner/repository URL"
            ) from None

        run_id, run_path = create_run_layout(artifacts_root, run_id_generator)
        typer.echo(f"run_id={run_id}")
        typer.echo(f"artifact_path={run_path}")

    return app


app = create_app()
