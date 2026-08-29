# RepoTrial

RepoTrial is an experimental deployment-trial and least-privilege validation tool
for public GitHub Docker Compose web applications. It pins a repository commit,
runs the workload through a disposable `SandboxProvider`, executes deterministic
journeys, evaluates hardening mutations with KEEP/ROLLBACK semantics, and renders
JSON/HTML evidence.

## Current status

M0–M7.4 and the M7.5 sandbox prerequisites are implemented locally. The first
frozen 10-repository M7.5 Pilot has completed, but it did **not** meet the release
threshold:

- autonomous successes: `0/10` (required `>=7/10`);
- meaningful convergences: `0/10` (required `>=5/10`);
- exact stop-reason coverage: `12/12`;
- aggregate cleanup: `9/10 = 90%` (required `100%`);
- P50/P95 duration: `UNKNOWN` because one repository had no metric-bearing run.

Nine metric-bearing attempts stopped at `boot_recovery_stopped`; Paperless-ngx
stopped during repository clone before workload execution. These are failed Pilot
results, not successful or safety claims. See
[`docs/dev/pilot-report.md`](docs/dev/pilot-report.md) for the frozen cohort,
attempt ledger, evidence references, and Kill Criteria analysis.

## Safety scope

- Target Compose workloads must execute only through a disposable sandbox
  provider. There is no host Docker fallback.
- Real production secrets, personal credentials, SSH keys, cloud tokens, and the
  host Docker socket are not exposed to target workloads.
- CPU, memory, disk, and host-side whole-trial duration bounds are applied by the
  current Docker Sandboxes provider.
- Docker Sandboxes v0.39.0 does not provide the frozen PID hard bound. Every Pilot
  attempt discloses `pid_hard_bound_unsupported`; RepoTrial does not claim
  fork-bomb/PID hard protection.
- Reports describe only tested-journey/workload-conditioned hardened candidates.
  They are not global safety or least-privilege proofs.

## Development

Use the locked environment and run the repository gates:

```text
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q --cov=repotrial --cov-branch --cov-fail-under=85
```

The real-repository Pilot requires a healthy, authenticated Docker Sandboxes
daemon and exact manifest commit SHAs. Do not execute an untrusted target Compose
directly on the host.
