import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repotrial.sandbox.docker_sbx import (
    DockerSbxPolicy,
    DockerSbxProvider,
    DockerSbxUnsupportedError,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_SBX_TESTS") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_SBX_TESTS=1 for the real sbx smoke test",
)


def test_real_sbx_disposable_lifecycle(tmp_path: Path) -> None:
    if shutil.which("sbx") is None:
        pytest.skip("UNSUPPORTED: sbx executable is unavailable")
    if shutil.which("git") is None:
        pytest.skip("UNSUPPORTED: git executable is unavailable for clone smoke")

    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(workspace)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    (workspace / "README.md").write_text("# smoke\n", encoding="utf-8")
    for argv in (
        ["git", "config", "user.email", "repotrial-smoke@example.invalid"],
        ["git", "config", "user.name", "RepoTrial Smoke"],
        ["git", "add", "README.md"],
        ["git", "commit", "--quiet", "-m", "smoke fixture"],
    ):
        subprocess.run(
            argv,
            cwd=workspace,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    provider = DockerSbxProvider(
        DockerSbxPolicy(
            cpus=1,
            memory_mb=1024,
            pids_limit=64,
            disk_mb=2048,
            total_duration_s=120,
        )
    )

    async def exercise() -> None:
        sandbox_id: str | None = None
        try:
            sandbox_id = await provider.create(workspace, "smoke")
            result = await provider.exec(sandbox_id, ["echo", "ok"], timeout_s=30)
            assert result.exit_code == 0
            assert result.stdout.strip() == "ok"
        except DockerSbxUnsupportedError as error:
            pytest.skip(f"UNSUPPORTED: {error.reason}")
        finally:
            if sandbox_id is not None:
                await provider.destroy(sandbox_id)

    asyncio.run(exercise())
