# RepoTrial

RepoTrial trials an untrusted public GitHub Docker Compose web application at an
exact commit inside a disposable Docker Sandbox. It boots the application,
runs deterministic journeys, tests least-privilege mutations with
KEEP/ROLLBACK semantics, writes JSON/HTML evidence, and attempts forced
destruction of every sandbox. Cleanup is fail-closed when destruction cannot be
verified.

## Current status

The current branch is demo-ready, but is not yet a tagged M7.5 release. On HEAD
`69d1b78fc96972c3dee0420db2cc3cc089814fa2`, two representative public
repositories completed the full URL -> exact SHA -> sandbox -> Compose ->
journey -> report -> cleanup path:

| Repository | Pinned commit | Result | Run ID |
| --- | --- | --- | --- |
| Uptime Kuma | `a852e21eba4ecf339624b404518c5bc7fad6d45c` | `1/1` journey PASS; cleanup PASS | `36396c07-4937-4e1a-adbd-53a910fcb223` |
| Listmonk | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `1/1` journey PASS; cleanup PASS | `de0ea361-a0e0-499c-b925-3db13595b898` |

Both runs produced JSON and HTML reports, recorded `destroy_success` for every
created sandbox, and ended with an empty official `sbx list`. The current full
quality gate is `1693 passed, 11 skipped`, with 87.28% branch coverage; Ruff,
format, mypy, pre-commit, locked build, and wheel-install smoke checks pass.

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

## Proxy networks, including Windows Rule mode

TUN mode is not required. With WSL's default NAT networking, a Windows proxy
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
- There is no host Docker/Compose fallback. Cleanup failure remains a failed run
  even if a later inventory query is empty.
- CPU, memory, disk, and host-side total-duration bounds remain enforced by the
  current provider path.
- Docker Sandboxes v0.39.0 does not expose the required PID hard bound.
  RepoTrial records `pid_hard_bound_unsupported` and does not claim fork-bomb
  protection.
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
