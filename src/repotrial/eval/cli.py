from pathlib import Path
from typing import Annotated

import typer

from repotrial.eval.evaluator import run_benchmark

app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def main(
    fixtures: Annotated[
        Path,
        typer.Option(
            "--fixtures",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = Path("eval/manifests"),
    result_dir: Annotated[
        Path,
        typer.Option("--result-dir", file_okay=False, dir_okay=True),
    ] = Path("eval/results"),
) -> None:
    destination = run_benchmark(fixtures, result_dir)
    typer.echo(destination)
