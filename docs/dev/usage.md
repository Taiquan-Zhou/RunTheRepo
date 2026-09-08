# Usage guide

[Back to RunTheRepo](../../README.md)

Detailed CLI, Journey, proxy and safety reference. For the supported preview
scope and current evidence, see [release closeout](release-closeout.md).

## Fastest supported setup

For a trusted local preview, start the loopback-only console from the supported
WSL checkout:

```bash
uv run repotrial serve --port 8765
```

Open `http://127.0.0.1:8765/`. The page is a thin wrapper around the existing
`inspect` CLI: it accepts a public GitHub URL, a full lowercase commit SHA, the
container port, and optional safe Compose/model settings. The model key remains
in the server environment and is never accepted or displayed by the page. One
trial can run at a time; status and elapsed time are real subprocess state, and
reports are served only from validated, server-owned run artifacts. This local
preview does not provide public binding, arbitrary shell or file downloads,
durable job history, granular graph progress, or real-target browser journeys.

The tested topology is Windows 11 -> dedicated Ubuntu 24.04 WSL2 distro ->
official Linux Docker Sandboxes v0.42.0 -> disposable Linux sandbox. The
checkout must be on the distro's ext4 filesystem. Target Compose workloads must
never run through host Docker or Docker Desktop.

Prerequisites:

- WSL2 with systemd, nested KVM, and `/dev/kvm` available;
- Docker Sandboxes v0.42.0 installed, authenticated, and running with the
  reviewed default-deny network policy;
- Git and network access to GitHub, the selected model endpoint, and required
  container registries.

The supported installation path is a source checkout with the locked `uv`
environment; arbitrary `pip` dependency combinations are not claimed as
validated. Install `uv` directly if it is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /path/to/RepoTrial
uv sync --locked --all-groups
uv run playwright install chromium
```

The complete one-time WSL/SBX setup, including the pinned SBX package hash and
Playwright OS dependencies, is in
[`docs/dev/wsl2-linux-sbx-setup.md`](wsl2-linux-sbx-setup.md).

Before a real run, require a healthy runtime and empty inventory:

```bash
uv run repotrial doctor
uv run repotrial doctor --json
```

The read-only doctor prints one `PASS`, `FAIL`, or `UNSUPPORTED` line per
check and ends with `READY` or `NOT_READY`; a blocking failure exits with code
2. The JSON form contains only the stable report object (`ready` and ordered
`checks`). The known Docker Sandboxes PID hard-bound limitation is reported as
non-blocking `UNSUPPORTED`.

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

When a configured model returns an empty proposal or policy-invalid output, no
model-proposed request is executed; the fallback is fixed `GET /` with expected
status `200`. Model transport failures, timeouts, or an unconfigured model can
still end a run with `insufficient_coverage`; the recorded `stop_reason` is
preserved.

### Operator-authored HTTP Journeys

Use `--journeys-file` to supply a validated, operator-authored Journey snapshot.
The file is read before intake and is never copied into or used to modify the
pinned repository. It is scoped to deterministic HTTP behavior; it is not a
fabricated real canary and does not grant shell, browser-host, credential, or
other execution authority.

Minimal example:

```json
{
  "journeys": [
    {
      "journey_id": "health",
      "name": "Health endpoint",
      "steps": [
        {
          "step_id": "get-health",
          "tool": "http",
          "action": "request",
          "params": {"method": "GET", "path": "/health"},
          "assertions": [
            {"kind": "status_code", "target": "response.status", "expected": 200}
          ]
        }
      ]
    }
  ]
}
```

Pass it to an exact-commit inspection:

```bash
uv run repotrial inspect https://github.com/OWNER/REPOSITORY \
  --provider docker-sbx \
  --commit-sha 0123456789abcdef0123456789abcdef01234567 \
  --journeys-file ./operator-journeys.json
```

The JSON and HTML reports retain only the input source kind and SHA-256
identities for provenance, never the host path or source bytes.

### Bounded bearer reuse (operator-authored evidence)

An operator-authored HTTP Journey may capture a short-lived bearer from a
successful login response and explicitly use it on later steps. These are
parameter fragments, not a complete Journey file:

```json
{"auth": {"capture_bearer": "token"}}
{"auth": {"use_bearer": true}}
```

Use only disposable test accounts; never put real credentials or tokens in a
Journey file. `capture_bearer` accepts one bounded dotted JSON-object path and
`use_bearer` is valid only after a prior capture in the same Journey. The token
is ephemeral and is not written to reports, evidence, stdout, or the Journey;
arbitrary headers, cookies, and environment interpolation remain unsupported.
This canary demonstrates operator-authored input only; the model retains its
GET-only policy. Authorization is sent only on explicit `use_bearer` steps.

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

- **GitHub, registry, JWKS, or `uv` downloads hang:** for clients that honor the
  standard proxy variables, verify the proxy is reachable from WSL by its
  gateway IP. Do not use `localhost` for a Windows proxy under WSL NAT.
- **The model attempt records `transport_error`:** the model client does not
  inherit `HTTP_PROXY` or `HTTPS_PROXY`. Verify direct or network-layer routing
  (for example, an approved TUN/VPN route) from WSL to the configured endpoint,
  or select an approved, directly reachable OpenAI-compatible
  `--model-endpoint`. Never put model credentials in the endpoint URL.
- **`sbx diagnose` fails or `sbx list` is non-empty:** stop new trials, retain
  the exact output and lifecycle evidence, and investigate the owned sandbox
  ID. Do not hide the failure with reset/restart loops.
- **`repotrial doctor` rejects `sbx_version`:** install and select the reviewed
  Docker Sandboxes `v0.42.0`. The old `v0.39.0` runtime is historical and is
  rejected by the current doctor.
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
- Docker Sandboxes v0.42.0 does not expose the required PID hard bound.
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
