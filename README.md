# RepoTrial

RepoTrial trials an untrusted public GitHub Docker Compose web application at an
exact commit inside a disposable Docker Sandbox. It boots the application,
runs deterministic journeys, tests least-privilege mutations with
KEEP/ROLLBACK semantics, writes JSON/HTML evidence, and attempts forced
destruction of every sandbox. Cleanup is fail-closed when destruction cannot be
verified.

## Current status

This branch is a demo-ready release candidate; it is not a published or tagged
release. On execution HEAD
`4c13b8b9521427b9491e0de8a9f77e64169754ac`, two representative public
repositories completed the full URL -> exact SHA -> sandbox -> Compose ->
journey -> report -> cleanup path:

| Repository | Pinned commit | Result | Run ID |
| --- | --- | --- | --- |
| changedetection.io | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `exit_code=0`; `completed` / `no_remaining_mutations`; `GET /` -> `200` assertion passed; JSON+HTML; create/destroy success; inventory empty | `f883cb75-405c-4f01-b99c-b30c0cb2c8d0` |
| Listmonk | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `exit_code=0`; `completed` / `consecutive_failures`; baseline `GET /` -> `200` assertion passed; JSON+HTML; create/destroy success; inventory empty | `df30728a-ab0a-4eec-b251-afd407400d71` |

Both runs exited with code 0, produced JSON and HTML reports, and recorded
successful create/destroy lifecycle events for every created sandbox. The
official `sbx list` was empty after each run. The Listmonk
`consecutive_failures` stop reason is from rolled-back hardening candidates
after the baseline Journey passed; it is not a baseline failure.

Fresh quality gates: `1794 passed, 11 skipped, 1 warning`; branch coverage is
86.07% (`>=85%`). Ruff check, Ruff format (140 files), mypy (47 files), and
pre-commit all-files pass. `uv lock --check`, `uv build` (sdist + wheel), a
fresh wheel install, and `repotrial --help` smoke also pass.

## Fastest supported setup

The tested topology is Windows 11 -> dedicated Ubuntu 24.04 WSL2 distro ->
official Linux Docker Sandboxes v0.39.0 -> disposable Linux sandbox. The
checkout must be on the distro's ext4 filesystem. Target Compose workloads must
never run through host Docker or Docker Desktop.

Prerequisites:

- WSL2 with systemd, nested KVM, and `/dev/kvm` available;
- Docker Sandboxes v0.39.0 installed, authenticated, and running with the
  reviewed default-deny network policy;
- Git and network access to GitHub, the selected model endpoint, and required
  container registries.

Install `uv` directly if it is missing, then create the locked environment:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /path/to/RepoTrial
uv sync --locked --all-groups
uv run playwright install chromium
```

The complete one-time WSL/SBX setup, including the pinned SBX package hash and
Playwright OS dependencies, is in
[`docs/dev/wsl2-linux-sbx-setup.md`](docs/dev/wsl2-linux-sbx-setup.md).

Before a real run, require a healthy runtime and empty inventory:

```bash
sbx version
sbx diagnose --output json
sbx policy ls --type network --json
sbx list
```

Provide the model key only through the process environment. This prompt avoids
putting the key in shell history or repository files:

```bash
read -rsp 'Model API key: ' REPOTRIAL_MODEL_API_KEY && echo
export REPOTRIAL_MODEL_API_KEY
```

Run one commit-pinned, auditable trial. Replace every placeholder with values
for the target repository; `--container-port` is the application's internal
web port, not a random host port.

```bash
uv run repotrial inspect https://github.com/OWNER/REPOSITORY \
  --provider docker-sbx \
  --commit-sha 0123456789abcdef0123456789abcdef01234567 \
  --container-port 8080 \
  --compose-path docker-compose.yml \
  --model-endpoint https://MODEL-ENDPOINT/v1 \
  --model-name MODEL_NAME
```

RepoTrial prints a `run_id` and artifact directory. The primary outputs are:

```text
artifacts/<run_id>/attempt-result.json
artifacts/<run_id>/report/trial-report.json
artifacts/<run_id>/report/trial-report.html
artifacts/<run_id>/evidence/
```

A trustworthy successful run has matching `expected_sha` and
`actual_verified_sha`, `exit_code: 0`, complete reports, successful lifecycle
destroy events, and an empty `sbx list`. Inventory emptiness alone is not proof
that cleanup succeeded.

## Journey and model boundary

An explicit repository Journey declaration may contain write methods; it remains
subject to schema validation and deterministic verifiers. Autonomous model
output is narrower: RepoTrial retains only evidence-supported `GET` journeys.
External links, images, file paths, and generalized API prose do not authorize a
route.

When model output is absent, untrusted, or policy-invalid, no model-proposed
request is executed; the fallback is fixed `GET /` with expected status `200`.
Model transport failures and timeouts can still end a run with
`insufficient_coverage`; the recorded `stop_reason` is preserved.

## Proxy networks (TUN optional; Rule or Global)

TUN mode is not required. Windows Rule or Global mode is acceptable when WSL,
the SBX daemon, the disposable sandbox, and the RepoTrial client all have the
required connectivity. With WSL's default NAT networking, a Windows proxy
listening on `localhost` is not automatically reachable inside WSL. Use the
Windows host's WSL-reachable LAN/gateway IP and keep loopback in `NO_PROXY`:

```bash
export HTTP_PROXY='http://<windows-host-gateway-ip>:<port>'
export HTTPS_PROXY="$HTTP_PROXY"
export NO_PROXY='localhost,127.0.0.1'
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTPS_PROXY"
export no_proxy="$NO_PROXY"
```

Docker Sandboxes has separate daemon and sandbox proxy settings; configure them
during initial SBX setup as documented in the WSL guide. Do not inject the
upstream proxy or its credentials into the untrusted target workload.

## Troubleshooting

- **GitHub, registry, JWKS, model, or `uv` downloads hang:** verify the proxy is
  reachable from WSL by its gateway IP. Do not use `localhost` for a Windows
  proxy under WSL NAT.
- **`sbx diagnose` fails or `sbx list` is non-empty:** stop new trials, retain
  the exact output and lifecycle evidence, and investigate the owned sandbox
  ID. Do not hide the failure with reset/restart loops.
- **No report is produced:** inspect `attempt-result.json`, then the referenced
  evidence. The nonzero exit and `stop_reason` are intentional fail-closed
  diagnostics.
- **Boot cannot find the service:** verify the repository's Compose filename
  and internal HTTP port, then pass the correct `--compose-path` and
  `--container-port`.
- **`/dev/kvm` or systemd is unavailable:** the host is unsupported; do not
  bypass isolation or fall back to host Docker.

## Safety scope and known limitation

- Every real repository requires a full 40-character commit SHA; requested and
  resolved SHAs are recorded.
- Target workloads run only through `SandboxProvider` in disposable sandboxes;
  there is no host Docker/Compose fallback. Cleanup is fail-closed: a cleanup
  failure remains a failed run, and an empty inventory is not cleanup success.
- CPU, memory, disk, and host-side total-duration bounds remain enforced by the
  current provider path.
- Docker Sandboxes v0.39.0 does not expose the required PID hard bound.
  RepoTrial records `pid_hard_bound_unsupported` and does not claim fork-bomb
  protection.
- Model keys are supplied through the process environment only; never print or
  record them.
- Reports are tested-journey/workload-conditioned results, not proof that a
  repository is globally safe or globally least-privileged.

## Development gates

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q --cov=repotrial --cov-branch --cov-report=term-missing
uvx pre-commit run --all-files
uv build
```
