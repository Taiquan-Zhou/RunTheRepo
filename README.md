# RepoTrial

RepoTrial is an experimental deployment-trial and least-privilege validation tool
for public GitHub Docker Compose web applications. It pins a repository commit,
runs the workload through a disposable `SandboxProvider`, executes deterministic
journeys, evaluates hardening mutations with KEEP/ROLLBACK semantics, and renders
JSON/HTML evidence.

## Current status

M0–M7.4 and the M7.5 sandbox prerequisites are implemented locally. The latest
recovered metric-bearing M7.5 canary covered repositories #1–#3, but did **not**
meet its release gate:

- canary success: `0/3` (required `>=2/3`);
- healthy Boot + observer + report: `2/3`;
- exact pinned SHA and cleanup: `3/3`;
- repositories #4–#10: not run after the canary gate failed.

All `3/3` repositories produced no non-empty Journey set. Umami and Listmonk
completed healthy Boot, observation, and report generation, but both ended with
model `policy_rejected` and `0/0` Journeys. changedetection.io received a
successful model response with an accepted empty Journey set, then timed out
during bounded Compose startup before a final Boot verdict. These are failed Pilot
results, not successful or safety claims. See
[`docs/dev/pilot-report.md`](docs/dev/pilot-report.md) for the frozen cohort,
attempt ledger, evidence references, and limitation analysis.

`M7.5 PILOT COMPLETE — MVP LIMITATIONS IDENTIFIED`

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

## Supported execution

On the current Windows machine, the official Windows-hosted `sbx.exe` CLI/daemon
v0.39.0 (`C:\Users\zztq\AppData\Local\DockerSandboxes\bin\sbx.exe`, invoked as
`sbx.exe daemon start`) is healthy and was the actual execution path for the
recovered M7.5 canary. `sbx diagnose` reported `12/12` PASS, with
`WHvCapabilityCodeHypervisorPresent=true`; target workloads still run inside a
disposable Linux sandbox. Earlier Windows/WSL recovery and calibration records are
historical or alternative evidence only, and the WSL2 setup is not required for
the current canary. This execution path does not claim a PID hard bound; the
known `pid_hard_bound_unsupported` limitation remains disclosed.

RepoTrial never falls back to Windows Docker Desktop, a host Docker Engine, or
host-side Compose. The [WSL2 Linux SBX setup guide](docs/dev/wsl2-linux-sbx-setup.md)
and [accepted calibration evidence](docs/dev/pilot-evidence/wsl2-linux-sbx-calibration.md)
are retained as alternative/historical reference material.

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
