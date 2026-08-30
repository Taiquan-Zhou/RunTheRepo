# WSL2 Linux Docker Sandboxes calibration

## Scope and execution gate

This record covers only the trusted WSL2/Linux execution environment for the
existing `DockerSbxProvider`. It does not run the frozen real-repository cohort,
change the `SandboxProvider` interface, or alter LangGraph, RunState, report,
API, CLI, Journey, mutation, KEEP, or ROLLBACK semantics.

Result: **PASS for trusted calibration and M7.5 environment handoff**. The
known unsupported PID hard bound remains disclosed below and is not claimed.

## Calibrated execution HEAD

- Git HEAD: `760f73c15aba839a71ac036699b527334808b73f`
- Windows controller worktree was clean during calibration; during independent
  review only this evidence report was untracked
- WSL ext4 checkout: clean, detached at the same HEAD
- Checkout root: `/home/repotrial/src/RepoTrial`
- Checkout filesystem: `ext4`
- Calibration command completed with `3 passed in 170.66s`
- Real Provider smoke completed with `1 passed in 21.94s`

The earlier successful calibration at `cfe98b767b1e6937d80801ecf554c3ca3feb53c0`
is retained as development evidence only. Source changes after that run changed
the runtime fingerprint, so the complete trusted calibration was rerun at the
HEAD above before this report was accepted.

## Runtime-input and manifest fingerprints

The runtime-input hash uses sorted tracked paths. Each entry contributes UTF-8
relative path, NUL, an eight-byte big-endian content length, then raw file bytes.
The 51-file set is `src/repotrial`, `pyproject.toml`, `uv.lock`,
`deploy/docker-compose.yml`, `eval/manifests`, and the trusted WSL2 fixture.

- Runtime-input files: `51`
- `runtime_input_fingerprint`:
  `3598b61c3fca786bfcf9b81f9e5d030ea38613f13736ecb92bd5d14d7feabcff`
- `eval/real_repos.yaml` SHA-256:
  `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`
- Four-file calibration/Provider verification hash:
  `d0a7efe4a17a13af9f1288820bf00684d8580d8d8d2a662315ccd16a8719299c`

The environment fingerprint is SHA-256 over canonical sorted-key compact JSON
containing the calibrated HEAD, manifest hash, distro, WSL kernel, SBX
version/commit/package version/package hash, guest Compose version, and global
network-policy hash.

- Environment fingerprint:
  `521b50be7c0ca064799f1f0f7e1cdd478fddc765d9ab6d5f8c49d2e26d13b005`

## Windows, WSL, distro, KVM, systemd, and storage facts

- Windows entry host: Windows 11 build `10.0.26200.9168`
- WSL package: `2.6.3.0`; distro version: WSL2
- Distro: `RepoTrial-Ubuntu`, Ubuntu `24.04.4 LTS`
- Registered base path: `D:\DockerData\WSL\RepoTrial-Ubuntu`
- Default UID: `1000` (`repotrial`)
- WSL kernel: `6.6.87.2-microsoft-standard-WSL2`
- PID 1: `systemd`
- `/dev/kvm`: character device, mode `0660`, owner `root:kvm`, accessible to
  the default user
- Distro root and RepoTrial checkout: ext4 on `/dev/sdd`
- Distro VHD maximum requested during provisioning: `120 GB`
- SBX diagnose after final cleanup: `12 pass / 0 warn / 0 fail / 0 skip`

The pre-existing default `Ubuntu` distro was not modified. `docker-desktop` and
the native Windows SBX runtime remained stopped during authoritative Linux
calibration.

## Official package URL, version, and SHA-256

- Recorded Docker official release URL:
  `https://github.com/docker/sbx-releases/releases/download/v0.39.0/DockerSandboxes-linux-amd64-ubuntu2404.deb`
- Installed package: `docker-sbx`
- Debian package version: `0.39.0-1~ubuntu.24.04~noble`
- Package file SHA-256:
  `bf36b1ac0a8daf5ee2ff44d138cfba578b3af6812733056c8000982b184f1631`
- Installed CLI identity:
  `sbx version: v0.39.0 def8cb0523a77e757bdd6ef52b459fe374f3783e`

No host Docker Engine or host Docker socket is an execution fallback for this
configuration.

## SBX, guest Docker, and Compose versions

- SBX CLI/daemon: `v0.39.0`
- Guest Docker client/server: `29.7.2`
- Guest containerd: `2.3.3`
- Guest runc: `1.4.3`
- Guest Docker Compose: `v5.5.0`

Guest component versions were captured inside a disposable trusted sandbox and
do not imply a host Docker installation.

## Global policy snapshot and hash

The active immutable global network rule was:

```json
{"rules":[{"applies_to":"all","decision":"deny","editable":false,"id":"default-deny-all","layer":"local","name":"default-deny-all","origin":"local","policy_name":"default-deny-all","resource_type":"network","resources":["**"],"scope":"global","status":"active"}]}
```

- Canonical global network-policy SHA-256:
  `e745ff8da1066eba64b0bbf7865ff91a308b0b216f81c5c375091e0a13b46d8b`
- Provider per-sandbox policy adds only the requested public access plus the
  mandatory private, metadata, host-alias, loopback, and local-network denies.
- Missing or malformed policy/network evidence fails closed.

The official policy listing also contains Docker's immutable global filesystem
read/write rules. The hash above intentionally identifies only the global
network rule that is part of RepoTrial's network execution gate.

## Initial inventory

Before authoritative execution, official `sbx list` returned exactly:

```text
No sandboxes found.
Launch one: sbx run claude
```

## Raw lifecycle result

The reviewed environment lifecycle used a trusted empty ext4 Git repository:

```text
create --clone -> exec echo ok -> ports publish -> cp -> rm --force
```

Observed result: official template pull/create succeeded, `exec` returned exact
`ok`, the published application endpoint bound to host loopback, sandbox-to-host
copy succeeded, force removal succeeded, and inventory returned empty. No target
repository or host Docker fallback was involved.

## Compose, resource, network, port, and copy result

The final primary calibration used the pinned trusted BusyBox Compose fixture.

- Clone: exact trusted fixture commit, clean Git status, expected tracked files,
  and no parent sentinel exposure
- Compose: `docker compose up --wait` succeeded; Compose v5 JSON `ps` reported
  the web service running and healthy
- CPU: guest reported exactly `1` online processor
- Memory: guest `MemTotal` was positive and no greater than `1024 MiB`
- Disk: policy `512 MiB` mapped to root `1 MiB`, Docker data `506 MiB`, and
  cloned workspace `5 MiB`; all observed totals were at or below their assigned
  bounds and all three mount identities were distinct
- Public network: `example.com` succeeded through the SBX policy proxy
- Mandatory denies: metadata (`169.254.169.254`), private IPv4
  (`10.255.255.1`), and host alias (`host.docker.internal`, normalized by SBX
  to `localhost`) each failed and produced fresh blocked policy evidence
- Port: inner fixture mapping `8080:8080`; outer `sbx ports` result required one
  unique requested `tcp4` mapping bound to `127.0.0.1`; the exact HTTP marker was
  read without using the control-process upstream proxy
- Copy: sandbox-to-host copy completed within the bounded control path
- Normal destroy: succeeded and removed the owned sandbox

No destructive disk-fill or untrusted-repository operation was used.

## Timeout, cancellation, and forced-cleanup result

- Timeout calibration: one shared host-side monotonic deadline covered the
  sandbox trial; an in-flight `sleep 120` exhausted the `45 s` total budget;
  a subsequent workload command was rejected before subprocess spawn; forced
  cleanup remained outside the exhausted trial deadline
- Cancellation calibration: cancellation was preserved after a real workload
  subprocess started; bounded kill/reap and sandbox cleanup completed
- Lifecycle artifacts were created with exclusive ownership semantics and
  recorded cleanup state
- Both scenarios ended with exact empty official inventory

## Provider smoke

At the calibrated HEAD, the opt-in real Provider smoke was not skipped:

```text
REPOTRIAL_RUN_SBX_TESTS=1 ... test_docker_sbx_smoke.py
1 passed in 21.94s
```

## Linux quality baseline

- Ruff lint: PASS
- Ruff format: `90 files already formatted`
- mypy: `Success: no issues found in 39 source files`
- Full pytest: `1178 passed, 5 skipped, 1 warning in 69.37s`
- Branch coverage rerun: `1178 passed, 5 skipped`; `88.78%` total against the
  required `85%`
- pre-commit all files: trailing whitespace, EOF, YAML, Ruff, and Ruff format
  all PASS
- `git diff --check`: PASS
- Warning: existing Starlette/httpx deprecation only

The five default skips are one opt-in real smoke and three opt-in full
calibration cases plus one platform-specific case. The real smoke and all three
calibration cases were separately enabled and passed as recorded above.

## Client proxy A/B and operating requirement

The daemon and sandbox upstream proxy settings are both configured through the
official `sbx settings` interface. Under the Windows proxy's Rule mode, explicit
requests through the approved LAN endpoint returned HTTP 200 for
`login.docker.com` JWKS, Docker auth, and GitHub in `0.7–1.4 s`.

A first final-HEAD calibration invocation without client `HTTP_PROXY` /
`HTTPS_PROXY` retained one primary pass but failed the timeout and cancellation
creates before workload start because the `sbx` client could not verify its
session token against the Docker JWKS endpoint. Inventory remained empty. With
the same Rule mode and explicit client proxy environment, `sbx diagnose`
returned `12/12` PASS and the complete calibration returned `3 passed`.

Operational requirement: launch RepoTrial and direct `sbx` control commands with
the approved client proxy variables when this Windows/WSL network requires the
LAN proxy. `NO_PROXY` must retain `localhost,127.0.0.1`. This is separate from
the official daemon/sandbox proxy settings and requires no daemon restart.

## Raw artifact and log index

- Trusted fixture:
  `tests/integration/sandbox/fixtures/wsl2_compose/`
- Calibration oracle:
  `tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py`
- Provider smoke:
  `tests/integration/sandbox/test_docker_sbx_smoke.py`
- Provider contract tests:
  `tests/unit/sandbox/test_docker_sbx_commands.py`
- Detailed append-only execution ledger:
  `.superpowers/sdd/2026-08-29-wsl2-linux-sbx-execution/progress.md`
- Environment daemon/client logs:
  `/home/repotrial/.local/state/sandboxes/sandboxes/sandboxd/daemon.log` and
  `client.log`

The report contains no credentials, login codes, proxy credentials, or secret
paths. Runtime logs remain local and are not committed.

## Final inventory

After the proxy-corrected full calibration, real Provider smoke, and final
diagnose, official `sbx list` again returned exact empty inventory. Diagnose was
`12 pass / 0 warn / 0 fail / 0 skip`.

## No-host-fallback provenance

- All target-like commands crossed the existing `DockerSbxProvider` argv
  boundary into the official Linux `sbx` CLI
- Host Docker was never selected as a fallback
- The WSL host has no Docker socket exposed to the fixture
- Sandbox guest Docker is confined to the disposable sandbox
- Ports were accepted only from strict official JSON and host loopback binding
- Every failure path attempted owned sandbox cleanup and the final inventory was
  independently checked

## Known PID limitation

Docker Sandboxes v0.39.0 does not provide the frozen PID hard bound required to
claim fork-bomb/PID hard protection. RepoTrial does not emulate it with
`ulimit`, Compose `pids_limit`, process polling, or memory limits. Pilot evidence
must retain `pid_hard_bound_unsupported`. This Owner-accepted known unsupported
limitation is disclosed; it is not represented as satisfied.

## Reviewer verdict

**APPROVED** — `0` Critical, `0` Important, `1` non-blocking wording Minor,
which was resolved before commit. The reviewer independently reproduced the
runtime, manifest, verification-test, environment, policy, and package hashes;
confirmed the exact HEAD/ext4 checkout, package/runtime identities, `12/12`
diagnose, empty inventory, stopped Windows runtime, proxy-layer separation,
no-host-fallback provenance, PID disclosure, and all required report sections;
and found no credentials or login material in the report or indexed evidence.
