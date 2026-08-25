from collections.abc import Callable
from pathlib import Path
from uuid import uuid4


def generate_run_id() -> str:
    return str(uuid4())


def create_run_layout(
    artifacts_root: Path, run_id_generator: Callable[[], str] = generate_run_id
) -> tuple[str, Path]:
    run_id = run_id_generator()
    run_path = artifacts_root / run_id
    run_path.mkdir(parents=True)
    for directory_name in ("evidence", "experiments", "report"):
        (run_path / directory_name).mkdir()
    return run_id, run_path
