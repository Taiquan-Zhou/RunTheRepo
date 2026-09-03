from __future__ import annotations

import argparse
from pathlib import Path

from typer.testing import CliRunner

from repotrial.cli import create_app
from repotrial.sandbox.base import SandboxProvider
from repotrial.sandbox.docker_sbx import DockerSbxPolicy, DockerSbxProvider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_id")
    parser.add_argument("url")
    parser.add_argument("commit_sha")
    parser.add_argument("compose_path")
    parser.add_argument("port", type=int)
    parser.add_argument("disk_mb", type=int, choices=(4096, 8192))
    parser.add_argument("model_endpoint")
    args = parser.parse_args()
    policy = DockerSbxPolicy(
        cpus=1,
        memory_mb=1024,
        pids_limit=64,
        disk_mb=args.disk_mb,
        total_duration_s=900,
    )

    def provider_factory(selected: str) -> SandboxProvider:
        if selected != "docker-sbx":
            raise ValueError("unexpected provider")
        return DockerSbxProvider(policy)

    app = create_app(
        artifacts_root=Path("artifacts"), provider_factory=provider_factory
    )
    result = CliRunner().invoke(
        app,
        [
            "inspect",
            args.url,
            "--provider",
            "docker-sbx",
            "--commit-sha",
            args.commit_sha,
            "--container-port",
            str(args.port),
            "--compose-path",
            args.compose_path,
            "--model-endpoint",
            args.model_endpoint,
            "--model-name",
            "deepseek-v4-flash",
        ],
        catch_exceptions=False,
    )
    print(result.output, end="")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
