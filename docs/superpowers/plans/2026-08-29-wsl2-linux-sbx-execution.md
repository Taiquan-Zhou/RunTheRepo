# WSL2 Linux SBX Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore RepoTrial's original isolated Docker Compose trial flow by running the existing DockerSbxProvider on a calibrated dedicated WSL2 Ubuntu environment, while closing the pre-existing shared total-duration defect and documenting the verified operating model.

**Architecture:** Keep Windows as the desktop entry point and place RepoTrial, the official Linux `sbx` CLI, sandboxd, and all high-volume runtime state inside a dedicated Ubuntu 24.04 WSL2 distro on D. Preserve the existing SandboxProvider and frozen orchestration; only one Provider-local production correction is authorized so every sandbox in one RepoTrial run shares the first successful create's workload deadline. After environment and Provider calibration pass, hand execution back to Task 2 of the existing M7.5 compatibility-recovery plan.

**Tech Stack:** Windows 11, WSL 2.6.3, Ubuntu 24.04, KVM, systemd, Docker Sandboxes v0.39.0, Python 3.12, uv 0.11.5, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-29-wsl2-linux-sbx-execution-design.md`

## Global Constraints

- The product goal remains `public GitHub URL -> exact commit -> isolated Compose trial -> deterministic Journeys -> bounded experiments -> report -> forced cleanup`.
- WSL2 changes only the trusted execution environment; it does not change LangGraph, RunState, GraphState, report/API/CLI semantics, Journey, mutation, KEEP, or ROLLBACK semantics.
- Keep the existing `SandboxProvider` interface and `DockerSbxProvider`; do not add another backend or Provider.
- Never execute target Docker Compose, target commands, or a target Docker socket on Windows, Docker Desktop, or the WSL Linux host.
- Allowed trusted host components are the Linux `sbx` subprocess, sandboxd, the official SBX policy/network proxy, and loopback-only SBX port publication.
- Keep Windows SBX v0.39.0 installed and stopped. Do not start, stop, restart, reset, repair, or mutate its retained state during this plan.
- Do not run `wsl --shutdown`, modify global `.wslconfig`, update WSL globally, or terminate, unregister, move, or modify the existing `Ubuntu` distro.
- Create only `RepoTrial-Ubuntu` at `D:\DockerData\WSL\RepoTrial-Ubuntu` through documented WSL `--name`, `--location`, and `--vhd-size 120GB` options.
- Require at least 150 GB free on D before provisioning; record free space before and after. This is a precondition, not a permanent reserve guarantee.
- Install only official `DockerSandboxes-linux-amd64-ubuntu2404.deb` v0.39.0 from `https://github.com/docker/sbx-releases/releases/download/v0.39.0/DockerSandboxes-linux-amd64-ubuntu2404.deb` with SHA-256 `bf36b1ac0a8daf5ee2ff44d138cfba578b3af6812733056c8000982b184f1631`.
- Use a dedicated ext4 checkout under `/home/repotrial/src`; never run real calibration or Pilot from `/mnt/c` or `/mnt/d`.
- PID hard bound remains the Owner-accepted known unsupported limitation for this Docker SBX Pilot. Do not emulate or claim it.
- Every code behavior change uses observed RED -> GREEN and an independent implementer/reviewer gate.
- Environment mutations are controller-executed because they affect host state outside the worktree; independent reviewers remain read-only.
- No push, merge, manifest change, repository replacement, model integration, or M7.5 attempt occurs in this plan.
- Runtime-input identity covers `src/repotrial`, `pyproject.toml`, `uv.lock`,
  trusted calibration fixture, and runtime configuration. Record
  `eval/real_repos.yaml` SHA-256 separately. Tests are verification inputs, not
  runtime inputs.

---

### Task 1: Make total duration shared across one Provider run

**Files:**
- Modify: `src/repotrial/sandbox/docker_sbx.py`
- Modify: `tests/unit/sandbox/test_docker_sbx_commands.py`

**Interfaces:**
- Consumes: one fresh `DockerSbxProvider` per CLI/API run and the existing `time.monotonic()` injection seam.
- Produces: private `self._trial_deadline: float | None`; unchanged public Provider interface.
- Invariant: first create uses a local deadline before probe/create and persists it only after create plus network-policy setup succeeds.
- Invariant: all later create/exec/publish/copy/network-log workload calls share that deadline; destroy and cleanup bypass it.

- [ ] **Step 1: Record the task BASE and update the ignored SDD ledger**

Run:

```powershell
git status --short
git rev-parse HEAD
git branch --show-current
```

Require a clean worktree on `codex/m7.5-compatibility-recovery`. Record the BASE
in `.superpowers/sdd/2026-08-29-wsl2-linux-sbx-execution/progress.md` with
`apply_patch`.

- [ ] **Step 2: Write sequential-create RED tests**

Replace the old independent-deadline expectation with tests equivalent to:

```python
def test_sequential_sandboxes_share_first_successful_trial_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    _install_deterministic_clock(monkeypatch, clock)
    spawner = _SbxSpawner()
    provider = _provider(monkeypatch, spawner, total_duration_s=10)

    first_id = _create(provider, tmp_path)
    asyncio.run(provider.destroy(first_id))
    clock.value = 2
    second_id = _create(provider, tmp_path)

    assert provider._trial_deadline == 10.0
    assert provider._sandbox_deadlines[second_id] == 10.0
```

Add these exact focused test names and assertions:

- `test_expired_trial_prevents_second_create_before_probe`: after a successful
  first lifecycle, set the clock to the shared deadline, snapshot spawner calls,
  assert second create raises `total_duration_exhausted`, and assert the call
  snapshot is unchanged.
- `test_failed_first_create_leaves_no_trial_deadline`: force non-zero first
  create, assert `_trial_deadline is None`, `_sandbox_deadlines == {}`, and owned
  partial cleanup remains the final subprocess.
- `test_failed_later_create_does_not_reset_trial_deadline`: succeed once at
  deadline `10.0`, force the later create to fail at clock `2.0`, and assert the
  Provider deadline remains exactly `10.0` after cleanup.
- `test_separate_provider_instances_have_independent_trial_deadlines`: create
  Provider A at clock `0.0`, Provider B at `2.0`, and assert deadlines `10.0`
  and `12.0` respectively.
- `test_destroy_after_shared_deadline_expiry_still_forces_cleanup`: expire the
  shared deadline, destroy the active owned ID, and assert final argv is exactly
  `("sbx", "rm", "--force", sandbox_id)`.

The exhausted-second-create test snapshots subprocess calls after the first
destroy and asserts no version/help/create subprocess is added.

- [ ] **Step 3: Run RED and preserve the failure reason**

Run:

```powershell
uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py -k "trial_deadline or sequential_sandboxes or expired_trial_prevents_second_create"
```

Expected RED: the second sandbox receives `clock.value + total_duration_s`, or
`_trial_deadline` is absent, demonstrating budget reset.

- [ ] **Step 4: Implement the minimal Provider-local deadline**

In `DockerSbxProvider.__init__` add:

```python
self._trial_deadline: float | None = None
```

At `create()` entry use:

```python
deadline = self._trial_deadline
first_successful_create = deadline is None
if deadline is None:
    deadline = time.monotonic() + self._policy.total_duration_s
```

Use `deadline` for probe, create, and sandbox-scoped policy setup. Only after all
three succeed:

```python
if first_successful_create:
    self._trial_deadline = deadline
self._sandbox_deadlines[sandbox_id] = deadline
```

Do not clear `_trial_deadline` in `_force_destroy()`. Do not add a watchdog,
thread, public setter, Graph field, or Provider method.

- [ ] **Step 5: Run GREEN and all applicable quality gates**

Run:

```powershell
uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py
uv run ruff check src/repotrial/sandbox/docker_sbx.py tests/unit/sandbox/test_docker_sbx_commands.py
uv run ruff format --check src/repotrial/sandbox/docker_sbx.py tests/unit/sandbox/test_docker_sbx_commands.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q
uv run pytest --cov=repotrial --cov-branch --cov-report=term-missing
uv run pre-commit run --all-files
git diff --check
```

Require the configured coverage gate (`fail_under = 85`) to pass and record the
exact full-suite counts. Do not defer these gates to Task 5 or compare away a new
failure against an unrelated environment baseline.

- [ ] **Step 6: Independent review and commit**

Reviewer checks first-create failure, later-create expiry, cleanup bypass,
cancellation, independent Provider instances, no public/state changes, and no
deadline extension. Resolve all Critical/Important findings, then commit:

```powershell
git add -- src/repotrial/sandbox/docker_sbx.py tests/unit/sandbox/test_docker_sbx_commands.py
git commit -m "fix: share sandbox deadline across one trial"
```

---

### Task 2: Add a trusted clone-mode Compose calibration fixture

**Files:**
- Create: `tests/integration/sandbox/fixtures/wsl2_compose/compose.yaml`
- Create: `tests/integration/sandbox/fixtures/wsl2_compose/www/index.html`
- Create: `tests/integration/sandbox/fixtures/wsl2_compose/www/health.txt`
- Create: `tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py`
- Create: `tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py`

**Interfaces:**
- Consumes: existing `DockerSbxProvider`, `managed_sandbox`, disk allocation, network policy, shared deadline, and explicit opt-in test convention.
- Produces: a trusted, immutable Linux calibration input included in the runtime fingerprint; no production import or package data.
- Image: `busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662`.

- [ ] **Step 1: Write fixture-contract RED tests before fixture files**

The always-on contract test must load `compose.yaml` using safe ruamel parsing
and assert:

```python
assert service["image"] == (
    "busybox:1.36.1@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
assert service["command"] == ["httpd", "-f", "-p", "8080", "-h", "/www"]
assert service["volumes"] == ["./www:/www:ro"]
assert service["healthcheck"]["test"] == [
    "CMD",
    "wget",
    "-q",
    "-O",
    "-",
    "http://127.0.0.1:8080/health.txt",
]
assert "ports" not in service
assert "privileged" not in service
assert "network_mode" not in service
assert "/var/run/docker.sock" not in fixture_text
```

Also assert `index.html` is exactly `repotrial-wsl2-sbx-ok\n` and `health.txt`
is exactly `ok\n`.

- [ ] **Step 2: Run fixture RED**

Run:

```powershell
uv run pytest -q tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py
```

Expected: FAIL because the fixture files do not exist.

- [ ] **Step 3: Add the minimal trusted fixture**

Create this exact Compose shape:

```yaml
services:
  web:
    image: busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
    command: [httpd, -f, -p, "8080", -h, /www]
    volumes:
      - ./www:/www:ro
    healthcheck:
      test: [CMD, wget, -q, -O, -, http://127.0.0.1:8080/health.txt]
      interval: 1s
      timeout: 2s
      retries: 30
      start_period: 1s
```

Do not add a Dockerfile, mutable tag, host port, host path, secret, or Docker
socket.

- [ ] **Step 4: Write the opt-in calibration integration test**

Mark only real-runtime tests with:

```python
pytestmark = pytest.mark.skipif(
    os.environ.get("REPOTRIAL_RUN_WSL2_SBX_CALIBRATION") != "1",
    reason="UNSUPPORTED: set REPOTRIAL_RUN_WSL2_SBX_CALIBRATION=1",
)
```

The test must reject non-Linux, non-WSL2, missing `/dev/kvm`, non-ext4 fixture
paths, missing `sbx`, or missing `git` as explicit `UNSUPPORTED`. It creates a
temporary exact Git clone of the trusted fixture and uses only Provider argv.

The primary test performs:

```text
managed create --clone
-> docker compose up -d --wait --wait-timeout 60
-> docker compose ps --format json
-> getconf _NPROCESSORS_ONLN == 1
-> /proc/meminfo reports 0 < MemTotal <= 1 GiB
-> df -B1 -P for /, /var/lib/docker, and cloned workspace
-> public HTTP egress succeeds
-> metadata/private/mandatory-host probes fail and policy evidence confirms deny
-> publish_port(8080) returns a loopback endpoint serving the exact marker
-> copy a guest result to a safe local destination
-> destroy
```

Use these deterministic oracles:

- Before creating the fixture repository, place a randomized sentinel in its
  parent directory. Inside the sandbox, assert `git rev-parse HEAD` equals the
  temporary fixture's exact commit, the expected marker is present, the parent
  sentinel is absent, and the guest workspace does not expose any path outside
  that committed fixture.
- Derive expected R/D/W from `calculate_disk_allocation(policy.disk_mb)`. Resolve
  the backing mount for `/`, `/var/lib/docker`, and the cloned workspace with
  `findmnt`; require three distinct filesystems, and require each `df -B1 -P`
  total to be positive and no greater than its corresponding configured MiB
  value in bytes. Formatting overhead may reduce visible capacity; no lower
  percentage is treated as a safety proof.
- Public egress is a bounded GET of `http://example.com/` whose body contains
  `Example Domain`. Fixed deny probes are
  `http://169.254.169.254/latest/meta-data/`, `http://10.255.255.1:81/`, and
  `http://host.docker.internal:80/`; each must fail within its command timeout,
  and `network_log()` must be supported and contain a blocked/denied event that
  corresponds to the probed IP/hostname or its frozen policy rule. A mere
  connection failure without policy evidence is not PASS.
- `host_port = await publish_port(sandbox_id, 8080)` must return a valid `int`.
  A trusted WSL-host client must receive the exact fixture marker from
  `http://127.0.0.1:{host_port}`. Keep the frozen return type unchanged and rely
  on the existing strict ports parser/unit contract to reject wildcard,
  hostname, or non-loopback mappings. Copy must produce byte-identical content
  at a fresh destination.
- Wrap each primary, timeout, and cancellation case in its own managed lifecycle.
  After each case, query `sbx list` and require an empty inventory; cleanup
  uncertainty or an unowned residue is a hard failure.

Use direct argv, bounded timeouts, and deterministic parsers. Do not use
`shell=True`, `sh -c`, host Docker, host Compose, or Docker Desktop.

The timeout test uses a separate Provider/run after images are cached, gives it
a bounded real budget, executes `sleep` past the remaining deadline, confirms a
second workload call fails before spawning, and verifies managed forced cleanup
plus an empty inventory. Cancellation is a separate `asyncio` cancellation case
so timeout and cancellation evidence are not conflated.

- [ ] **Step 5: Run fixture GREEN and static gates**

Run without the opt-in variable:

```powershell
uv run pytest -q tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py
uv run pytest -q tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py
uv run ruff check tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py
uv run ruff format --check tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py
git diff --check
```

Expected: contract PASS; real calibration explicitly skipped, not reported PASS.

- [ ] **Step 6: Independent review and commit**

Reviewer verifies the fixture has no host mounts/secrets, all commands cross the
Provider boundary, resource checks are real rather than help-token checks, and
cleanup is fail-closed. Commit:

```powershell
git add -- tests/integration/sandbox/fixtures/wsl2_compose tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py
git commit -m "test: add trusted Linux SBX calibration fixture"
```

---

### Task 3: Provision the dedicated WSL2 distro on D

**Files:**
- No tracked repository changes.
- Update ignored ledger: `.superpowers/sdd/2026-08-29-wsl2-linux-sbx-execution/progress.md`

**Interfaces:**
- Produces: registered WSL2 distro `RepoTrial-Ubuntu`, exact location `D:\DockerData\WSL\RepoTrial-Ubuntu`, default user `repotrial`, 120 GB sparse VHD maximum.
- Does not consume or mutate the existing `Ubuntu` distro.

- [ ] **Step 1: Capture non-mutating preflight**

Run and retain output:

```powershell
git status --short
git rev-parse HEAD
wsl --version
wsl --status
wsl --list --verbose
wsl --list --online
Get-PSDrive -Name D | Select-Object Name,Used,Free
Test-Path -LiteralPath 'D:\DockerData'
Test-Path -LiteralPath 'D:\DockerData\WSL\RepoTrial-Ubuntu'
sbx version
sbx daemon status
```

Require: D free >=150 GB; `RepoTrial-Ubuntu` absent; existing `Ubuntu` recorded;
Windows SBX v0.39.0 stopped; Git clean. If any target exists, STOP without
provisioning and report exact state.

- [ ] **Step 2: Install only the dedicated distro using official WSL placement**

Run exactly once:

```powershell
wsl --install Ubuntu-24.04 --name RepoTrial-Ubuntu --location "D:\DockerData\WSL\RepoTrial-Ubuntu" --version 2 --vhd-size 120GB --no-launch
```

Do not use export/import, manual VHD movement, `wsl --shutdown`, or global WSL
configuration.

If official WSL output says Windows must restart, record the completed install
command and exact message, ask the Owner to restart Windows, then resume at Step
3. Do not rerun `wsl --install` after the restart.

- [ ] **Step 3: Create the dedicated non-root user without collecting a password**

Run as distro root:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- useradd --create-home --shell /bin/bash repotrial
wsl -d RepoTrial-Ubuntu -u root -- usermod --append --groups adm,kvm repotrial
wsl --manage RepoTrial-Ubuntu --set-default-user repotrial
```

Do not grant passwordless sudo. Future package-management commands explicitly
select distro user `root`; RepoTrial execution uses `repotrial`.

- [ ] **Step 4: Verify distro identity and isolation**

Run:

```powershell
wsl -d RepoTrial-Ubuntu -- cat /etc/os-release
wsl -d RepoTrial-Ubuntu -- uname -a
wsl -d RepoTrial-Ubuntu -- id
wsl -d RepoTrial-Ubuntu -- ls -l /dev/kvm
wsl -d RepoTrial-Ubuntu -- ps -p 1 -o comm=
wsl --list --verbose
Get-PSDrive -Name D | Select-Object Name,Used,Free
```

Require Ubuntu 24.04, WSL version 2, default user `repotrial`, `/dev/kvm`,
systemd PID 1, unchanged existing `Ubuntu`, and D free-space evidence.

- [ ] **Step 5: Independent environment review**

A read-only reviewer compares pre/post distro lists, D path, user/group facts,
and confirms no global WSL or existing-distro operation occurred. Record verdict
in the ignored ledger. This task has no Git commit.

---

### Task 4: Install and authenticate official Linux SBX

**Files:**
- No tracked repository changes.
- Update ignored ledger: `.superpowers/sdd/2026-08-29-wsl2-linux-sbx-execution/progress.md`

**Interfaces:**
- Produces: authenticated Linux SBX v0.39.0 daemon with fail-closed global policy and empty inventory inside `RepoTrial-Ubuntu`.

- [ ] **Step 1: Install bounded prerequisites as distro root**

Run:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- apt-get update
wsl -d RepoTrial-Ubuntu -u root -- apt-get install -y ca-certificates curl git python3.12 python3.12-venv
```

Do not install Docker Engine or Docker Desktop inside WSL.

- [ ] **Step 2: Download and independently verify the official package**

Create the directory as root, then download as `repotrial` with HTTPS-only,
redirect-following, fail-closed curl behavior:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- install -d -o repotrial -g repotrial -m 0755 /home/repotrial/downloads
wsl -d RepoTrial-Ubuntu -- curl --fail --show-error --location --proto "=https" --tlsv1.2 --output /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb https://github.com/docker/sbx-releases/releases/download/v0.39.0/DockerSandboxes-linux-amd64-ubuntu2404.deb
wsl -d RepoTrial-Ubuntu -- sha256sum /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb
wsl -d RepoTrial-Ubuntu -- stat -c "%U:%G %a %n" /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb
```

Require the digest exactly and owner/group `repotrial:repotrial`:

```text
bf36b1ac0a8daf5ee2ff44d138cfba578b3af6812733056c8000982b184f1631
```

If the hash differs, delete nothing, install nothing, retain evidence, and STOP.

- [ ] **Step 3: Install the verified package and inspect the installed CLI**

Run as root:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- apt-get install -y /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb
```

Then as `repotrial` run `sbx version`, `sbx daemon start --help`,
`sbx diagnose --help`, `sbx create --help`, `sbx policy --help`, and `sbx ports
--help`. Require v0.39.0 and the capabilities consumed by the existing Provider.

- [ ] **Step 4: Start only the Linux daemon with the frozen policy**

Run once inside `RepoTrial-Ubuntu`:

```text
sbx daemon start --detach --policy deny-all
```

Do not reset/restart on failure. Capture exact status and daemon logs and STOP if
startup fails.

- [ ] **Step 5: Complete official authentication**

Run `sbx login` as `repotrial`. If device authentication requires Owner action,
show only the official URL/code; never record cookies, tokens, or secret files.

Then run:

```text
sbx diagnose --output json
sbx policy ls --type network --json
sbx list
```

Require all diagnostics PASS, immutable global deny-all, and empty inventory.

- [ ] **Step 6: Run a raw trusted basic lifecycle**

Create an empty ext4 Git workspace under `/home/repotrial/calibration/basic`, then
use installed CLI syntax to perform:

```text
create --clone -> exec echo ok -> ports publish -> cp -> rm --force
```

Require exact `echo ok`, successful copy, and final empty `sbx list`. On any
failure, attempt only the official forced removal for the owned exact ID, retain
logs, and STOP. Do not patch RepoTrial.

- [ ] **Step 7: Independent runtime review**

Reviewer verifies package digest, version, KVM/daemon identity, policy snapshot,
raw lifecycle, copy/port commands, cleanup, and no host Docker installation.
Record verdict in the ledger. This task has no Git commit.

---

### Task 5: Run the RepoTrial Linux calibration and close environment evidence

**Files:**
- Create: `docs/dev/pilot-evidence/wsl2-linux-sbx-calibration.md`
- May modify only if a real generic cross-platform defect is proven: `src/repotrial/sandbox/docker_sbx.py`, its focused tests.

**Interfaces:**
- Consumes: reviewed Task 1 deadline, Task 2 trusted fixture, healthy Task 4 Linux daemon.
- Produces: calibrated execution HEAD, runtime-input fingerprint, environment fingerprint, and reviewed authoritative calibration report.

- [ ] **Step 1: Freeze and clone the exact execution HEAD into ext4**

On Windows record `git rev-parse HEAD` and require clean status. In WSL clone the
local repository without hardlinks into `/home/repotrial/src/RepoTrial`, checkout
that exact commit in detached mode, and verify:

```text
git rev-parse HEAD
git status --porcelain=v1
findmnt -T /home/repotrial/src/RepoTrial -n -o FSTYPE
```

Require exact HEAD, clean status, and ext4. Do not execute from `/mnt/d`.

- [ ] **Step 2: Build an independent locked Python environment**

Create `/home/repotrial/.local/share/repotrial-uv`, install `uv==0.11.5` into
that bootstrap venv, then from the ext4 checkout run:

```text
uv lock --check
uv sync --locked --all-groups
uv run python --version
uv --version
```

Do not reuse the Windows `.venv`.

- [ ] **Step 3: Compute immutable fingerprints before calibration**

Record:

```text
calibrated_execution_head
runtime_input_fingerprint = sha256(src/repotrial tree + pyproject.toml + uv.lock + runtime configuration + trusted fixture)
manifest_sha256 = sha256(eval/real_repos.yaml)
verification_tests_sha256 = sha256(relevant calibration/Provider test files)
Ubuntu release + WSL kernel + /dev/kvm identity
sbx version/commit + package SHA-256
guest docker compose version
global policy canonical JSON SHA-256
```

Use deterministic sorted path order and raw file bytes. Keep the manifest and
verification-test hashes separate from `runtime_input_fingerprint`; tests are
audit evidence, not runtime identity. Never include auth state or secrets in a
fingerprint.

- [ ] **Step 4: Run static fixture and focused Provider gates on Linux**

Run:

```text
uv run pytest -q tests/integration/sandbox/test_wsl2_sbx_fixture_contract.py
uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py
REPOTRIAL_RUN_SBX_TESTS=1 uv run pytest -q tests/integration/sandbox/test_docker_sbx_smoke.py
```

Require PASS, not skip, for the real smoke.

- [ ] **Step 5: Run the full trusted WSL2 calibration**

Run:

```text
REPOTRIAL_RUN_WSL2_SBX_CALIBRATION=1 uv run pytest -q -s tests/integration/sandbox/test_wsl2_linux_sbx_calibration.py
```

Require real PASS for Compose health, CPU/memory/disk observations, public egress,
mandatory denies, loopback publication, copy, shared deadline timeout,
cancellation, forced cleanup, clone boundary, and final inventory.

If raw SBX remains healthy but this test exposes a generic Provider defect,
preserve the failed evidence and use systematic debugging. One TDD correction at
a time is allowed only inside the Task 5 file boundary, followed by focused
review and a commit. If the failure is WSL/SBX infrastructure, do not patch
RepoTrial; stop for the native-Ubuntu fallback decision.

- [ ] **Step 6: Run the Linux repository quality baseline**

Run fresh:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q
uv run pytest --cov=repotrial --cov-branch --cov-report=term-missing
uv run pre-commit run --all-files
git diff --check
```

Record exact passed/failed/skipped counts. Linux failures are investigated on
their own evidence; they are not compared away against Windows symlink failures.

- [ ] **Step 7: Write the authoritative calibration report**

Using `apply_patch`, create the report with exact sections:

```text
Scope and execution gate
Calibrated execution HEAD
Runtime-input and manifest fingerprints
Windows/WSL/distro/KVM/systemd/storage facts
Official package URL/version/SHA-256
SBX/guest Docker/Compose versions
Global policy snapshot/hash
Initial inventory
Raw lifecycle result
Compose/resource/network/port/copy result
Timeout/cancellation/forced-cleanup result
Provider smoke
Linux quality baseline
Raw artifact/log index
Final inventory
No-host-fallback provenance
Known PID limitation
Reviewer verdict
```

Do not include credentials, device login codes, or secret file paths.

- [ ] **Step 8: Independent calibration review and commit**

Reviewer reproduces read-only fingerprints/version/inventory and checks all
required evidence. Resolve Critical/Important findings, then commit:

```powershell
git add -- docs/dev/pilot-evidence/wsl2-linux-sbx-calibration.md
git commit -m "docs: record WSL2 Linux SBX calibration"
```

Record this resulting commit as `calibration_evidence_commit` only in the next
docs-only attestation; the report does not self-reference it.

---

### Task 6: Publish the verified operating documentation and M7.5 handoff

**Files:**
- Modify: `README.md`
- Create: `docs/dev/wsl2-linux-sbx-setup.md`
- Modify: `docs/dev/status.md`
- Modify: `docs/dev/pilot-report.md`
- Modify: `docs/superpowers/specs/2026-08-29-m7.5-compatibility-recovery-design.md`
- Modify: `docs/superpowers/plans/2026-08-29-m7.5-compatibility-recovery.md`
- Update ignored ledger: `.superpowers/sdd/2026-08-29-wsl2-linux-sbx-execution/progress.md`

**Interfaces:**
- Consumes: accepted calibration report and `calibration_evidence_commit`.
- Produces: reproducible user setup, historical Windows blocker disclosure, Linux cohort protocol, and an explicit handoff to the unchanged M7.5 Task 2.

- [ ] **Step 1: Update README only from verified facts**

Add a concise supported-execution section:

```text
Recommended Windows development path: dedicated WSL2 Ubuntu 24.04 + official
Linux Docker Sandboxes. Native Windows SBX is currently blocked by the retained
sandboxd self-connect issue. RepoTrial never falls back to host Docker.
```

Link the setup guide. Keep product goals and claims unchanged.

- [ ] **Step 2: Write the reproducible setup guide**

Document exact prerequisites, D-drive placement, distro creation, non-root user,
package URL/hash, login, daemon/policy setup, verification commands, ext4 source
layout, upgrade invalidation, troubleshooting, safe retirement gate, and the
operations forbidden against existing WSL distros.

Separate labels for:

```text
Docker-official behavior
Microsoft-official WSL behavior
RepoTrial-tested compatibility
Known unsupported limitation
```

- [ ] **Step 3: Update status and Pilot ledgers without rewriting history**

Record:

- Windows stable/nightly self-connect evidence remains historical and unchanged;
- WSL2 calibration identities and `calibration_evidence_commit`;
- docs-only attestation does not identify its own future commit;
- Windows cohort remains immutable and is superseded only at cohort level for
  the next release decision;
- Linux Task 2 attempts will be `diagnostic-only`;
- final Linux #1–#3 become canary/metric-bearing only after all approved fixes;
- current-cohort metrics and cross-cohort audit are reported separately.

- [ ] **Step 4: Amend the existing compatibility spec/plan by reference only**

Add a short environment amendment pointing to the new spec, calibration report,
and this plan. Do not duplicate Tasks 2–7 or change their Boot/Recovery/Journey,
manifest, threshold, or attribution logic. Replace only the obsolete Windows
environment assumption and note the completed shared-deadline prerequisite.

- [ ] **Step 5: Verify docs-only descendant validity**

Recompute the runtime-input, manifest, and environment fingerprints. Require
identity with Task 5 and confirm the diff since `calibration_evidence_commit`
contains only allowlisted docs/evidence files.

- [ ] **Step 6: Independent documentation/goal review and commit**

Reviewer checks that the original product goal is unchanged, commands reproduce
the calibrated path, unsupported claims are explicit, Windows history remains
append-only, and no public schema/contract changed. Run:

```powershell
uv run pre-commit run --files README.md docs/dev/wsl2-linux-sbx-setup.md docs/dev/status.md docs/dev/pilot-report.md docs/superpowers/specs/2026-08-29-m7.5-compatibility-recovery-design.md docs/superpowers/plans/2026-08-29-m7.5-compatibility-recovery.md
git diff --check
```

Then commit:

```powershell
git add -- README.md docs/dev/wsl2-linux-sbx-setup.md docs/dev/status.md docs/dev/pilot-report.md docs/superpowers/specs/2026-08-29-m7.5-compatibility-recovery-design.md docs/superpowers/plans/2026-08-29-m7.5-compatibility-recovery.md
git commit -m "docs: adopt calibrated WSL2 Linux SBX execution"
```

- [ ] **Step 7: Final handoff gate**

Require clean Git status, unchanged runtime/environment fingerprints, healthy
Linux daemon, empty `sbx list`, Windows daemon still stopped, and no sandbox
residue. Record the actual M7.5 execution HEAD.

Then resume **Task 2** of
`docs/superpowers/plans/2026-08-29-m7.5-compatibility-recovery.md` using the same
subagent-driven, TDD, review, frozen manifest, and cohort gates. Do not run a
real repository in this WSL migration plan itself.
