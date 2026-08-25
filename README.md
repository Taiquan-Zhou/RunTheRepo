# RepoTrial

RepoTrial is currently an M0 Python skeleton for a reproducible deployment-trial tool for untrusted Docker Compose web applications.

## Current M0 capabilities

- A typed Python package with locked development dependencies and local quality gates.
- Frozen Pydantic domain models that serialize the planned run, evidence, journey, observation, and experiment contracts as JSON.
- `repotrial doctor`, which currently confirms only that the CLI starts successfully.
- `repotrial inspect --dry-run https://github.com/owner/repository`, which validates the GitHub HTTPS URL and creates an empty run layout at `artifacts/<run_id>/` with `evidence/`, `experiments/`, and `report/` directories.

The dry-run command does not clone or execute the repository. Clone/commit pinning, Compose parsing, sandbox providers, agent orchestration, journey execution, observation, and hardening are not implemented yet.

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

The package does not execute target repositories. Future target workloads must run only through disposable sandbox providers, with no production secrets, personal credentials, SSH keys, cloud tokens, or host Docker socket exposure. README, UI, logs, and repository content are untrusted data and cannot expand tool permissions.

## Status

M0.1 through M0.3 are implemented locally and are undergoing the M0 milestone gate. This is not a claim that RepoTrial is safe or has produced a globally least-privilege configuration; those claims are outside the project contract.
