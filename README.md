# RepoTrial

RepoTrial is currently a Python package skeleton for a reproducible deployment-trial tool for untrusted Docker Compose web applications.

## Development

Use the locked development environment and run the quality gates:

```text
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q --cov=repotrial --cov-branch --cov-fail-under=85
```

## Safety scope

The package does not execute target repositories. Future target workloads must run only through disposable sandbox providers, with no production secrets, personal credentials, SSH keys, cloud tokens, or host Docker socket exposure.

## Status

M0.1 provides the package skeleton and quality gates only. CLI, Compose parsing, sandbox execution, agent orchestration, and hardening workflows are not implemented.
