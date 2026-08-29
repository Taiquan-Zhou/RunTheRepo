# RepoTrial WSL2 Linux SBX Execution Design

**Status:** Independently reviewed and Owner approved

**Date:** 2026-08-29

**Owner-approved direction:** Run RepoTrial and Docker Sandboxes inside a dedicated WSL2 Ubuntu environment while Windows remains the desktop/control entry point.

## 1. Decision

RepoTrial will move its M7.5 development and Pilot execution from the broken
Windows Docker Sandboxes runtime to the official Linux Docker Sandboxes package
inside a dedicated Ubuntu 24.04 WSL2 distribution.

This is an execution-environment correction, not a product-architecture change.
The existing `DockerSbxProvider` remains the only real Provider, and the frozen
RepoTrial flow remains:

```text
public GitHub URL
  -> exact commit pin
  -> DockerSbxProvider
  -> disposable Docker SBX microVM
  -> Docker Compose workload
  -> deterministic Journeys and verification
  -> bounded experiments with KEEP/ROLLBACK
  -> report and forced cleanup
```

The existing compatibility-recovery design remains authoritative for Boot,
Recovery, Journey, experiment, evidence, and cohort behavior. This document
changes only the host environment assumptions and the environment-verification
ladder needed before that plan resumes.

One pre-existing implementation defect was found while reviewing this design:
`DockerSbxProvider.create()` currently creates a new deadline for every sandbox,
while one RepoTrial run reuses one Provider across baseline retries and candidate
experiments. The previously approved total-duration task required one deadline
from the first `create()` across all workload operations in that Provider run.
Section 9 defines the isolated TDD correction required before M7.5 resumes. This
is closure of an existing frozen requirement, not a WSL-driven feature.

For the dedicated Linux environment only, this approved design supersedes the
older plan's blanket prohibition on daemon lifecycle commands during setup:
provisioning may start and stop the new Linux sandboxd as required for official
installation and calibration. It does not authorize reset/restart loops, manual
state mutation, or lifecycle changes to the retained Windows runtime. Once the
Linux environment gate passes, Pilot execution again consumes the healthy daemon
without changing it.

## 2. Evidence and motivation

The current Windows host reproduced the same sandboxd self-connect failure on:

- stable Docker Sandboxes v0.39.0 after an official reset and fresh state;
- official nightly `v0.42.0-rc1-34-gc5fab2cd4` from commit
  `c5fab2cd4dd5194c5c3377b67dbcb2b025d42a5d` after fresh state;
- both the existing RepoTrial real-provider smoke and a raw trusted `sbx create`.

In each case sandboxd reported that it could not connect to its own
`.../sandboxd/docker.sock`. The Docker issue describing this Windows
in-process-moby AF_UNIX self-connect failure remains open. Reinstalling the same
Windows runtime again would not test a new causal variable.

The local machine already proves the prerequisites for a bounded Linux route:

- WSL 2.6.3 with Ubuntu 24.04 available;
- `/dev/kvm` present inside WSL2;
- Intel VMX/EPT visible to the WSL2 kernel;
- systemd active;
- Docker publishes an official Ubuntu 24.04 Docker Sandboxes package;
- sufficient memory and D-drive capacity for a dedicated sparse WSL VHD.

The initial package is pinned to Docker Sandboxes v0.39.0
`DockerSandboxes-linux-amd64-ubuntu2404.deb`, SHA-256
`bf36b1ac0a8daf5ee2ff44d138cfba578b3af6812733056c8000982b184f1631`.

Docker publishes the Linux package, but this design does not claim that the
specific WSL2 topology is supported merely because the package installs. Real
calibration is a hard gate before any RepoTrial Pilot execution.

## 3. Goals

1. Restore a reproducible real SBX execution path without changing RepoTrial's
   product identity or frozen architecture.
2. Preserve the current `SandboxProvider` interface and `DockerSbxProvider`
   behavior unless a real Linux run exposes a generic cross-platform defect.
3. Keep all high-volume Linux/SBX state on D using documented WSL placement,
   without manually moving VHD or SBX state files.
4. Resume the retained M7.5 three-repository diagnostic and frozen ten-repository
   cohort only after the environment gate passes.
5. Make the verified Windows + WSL2 operating model reproducible in project
   documentation.

## 4. Non-goals

- No new SandboxProvider or sandbox backend in this change.
- No direct Docker Desktop/host-Docker execution fallback.
- No LangGraph, `RunState`, `GraphState`, report, API, CLI semantic, Journey,
  mutation, experiment, KEEP, or ROLLBACK redesign.
- No repository-specific workaround and no frozen manifest/SHA replacement.
- No model integration.
- No claim that Docker SBX provides the known unsupported PID hard bound.
- No deletion or migration of the user's existing `Ubuntu` WSL distribution.
- No claim that native Windows SBX is repaired.
- No extension of total duration to trusted intake or final report rendering;
  the frozen safety budget begins at the first sandbox `create()` and bounds all
  subsequent untrusted-workload Provider operations in that run.

## 5. Options considered

### 5.1 Dedicated WSL2 Ubuntu + official Linux SBX — selected

Create a dedicated `RepoTrial-Ubuntu` distribution on D, install the official
Ubuntu 24.04 SBX package, and run RepoTrial entirely from its ext4 filesystem.

Advantages:

- smallest architecture impact;
- reuses the current Provider and all safety/evidence logic;
- keeps the Windows desktop workflow;
- local prerequisites already exist;
- separate distribution makes setup and rollback reproducible.

Risk:

- WSL2 is a specific Linux virtualization topology and must pass a real KVM/SBX
  lifecycle calibration before it is accepted.

### 5.2 Native Ubuntu VM + official Linux SBX — fallback

Use a dedicated native Ubuntu VM if WSL2 fails for a WSL-specific virtualization
reason. RepoTrial and the same `DockerSbxProvider` would still run inside Linux.

This has higher setup and resource cost but does not require product code or
Provider changes. It is authorized only by a separate Owner gate after WSL2
calibration evidence identifies a WSL-specific blocker.

### 5.3 New VM-backed Provider — deferred architecture option

Implement a new Provider only if the official Linux SBX runtime also fails in a
native Linux environment or cannot satisfy the remaining accepted MVP safety
semantics. This would require a separate architecture decision and is not part
of this design.

## 6. Host and storage topology

```text
Windows 11 desktop
  -> WSL2 distribution: RepoTrial-Ubuntu
       location: D:\DockerData\WSL\RepoTrial-Ubuntu
       filesystem: dedicated sparse ext4 VHD managed by WSL
       -> RepoTrial source/worktrees under /home/repotrial
       -> official Linux sbx CLI and sandboxd
            -> disposable SBX microVM
                 -> guest Docker daemon
                 -> untrusted pinned Compose workload
```

The distribution is created using the installed WSL CLI's documented
`--name`, `--location`, and `--vhd-size` options. Existing WSL distributions
are not moved, exported, imported, or modified. No VHD or SBX state directory
is moved manually.

The dedicated distro uses a 120 GB sparse VHD maximum. Provisioning must first
confirm at least 150 GB free on D and record free space before and after setup.
This is a capacity precondition, not a guarantee against unrelated future D-drive
consumption. RepoTrial's existing per-sandbox `disk_mb` allocation remains the
workload disk hard budget; the distro VHD maximum is a host-capacity guard, not a
replacement for it.

The dedicated distro provides filesystem, userspace, and SBX-state separation;
it is not a separate physical host from other WSL distributions. This work must
not execute `wsl --shutdown`, terminate or unregister the existing `Ubuntu`,
modify any existing distro, edit global `.wslconfig`, update WSL globally, or
perform another operation that affects all distributions without a separate
Owner maintenance gate.

## 7. Trust boundary and safety semantics

The Windows host and dedicated WSL distribution are trusted RepoTrial developer
infrastructure. The target repository and Compose workload remain untrusted and
run only inside the disposable SBX microVM.

| Requirement | Enforcement in this design |
|---|---|
| Isolation | Docker SBX disposable microVM; calibration required |
| CPU | Existing `DockerSbxProvider` `--cpus` capability probe and create argument |
| Memory | Existing `--memory` capability probe and create argument |
| PID hard bound | Known unsupported limitation; disclosed, never emulated or claimed |
| Disk | Existing three official SBX filesystem-size environment variables and exact budget sum |
| Workload-phase whole-trial duration | One host-side monotonic deadline per Provider/run, created by the first `create()` and shared by every later sandbox workload operation; cleanup bypasses it |
| Cleanup | Existing managed lifecycle plus `sbx rm --force`; empty final inventory required |
| Network | Frozen deny rules plus sandbox-scoped public egress allowance; no host fallback |
| Exact source | Existing intake pins and verifies the manifest commit before workload execution |

No real personal credentials, Docker Desktop socket, host Docker socket, or SSH
keys are exposed to target workloads. `sbx login` is performed interactively in
the dedicated trusted distribution; its secrets are not copied into the repo or
recorded in test artifacts.

The Windows SBX daemon remains stopped during Linux execution. Windows and Linux
SBX state, daemon, inventory, and evidence must never be combined in one run.

The Owner-accepted PID ruling recorded in `docs/dev/pilot-report.md` applies only
to the current Docker SBX Pilot: PID hard-bound protection is unsupported, is not
claimed, and cannot be substituted with `ulimit`, Compose `pids_limit`, polling,
or memory limits. This exception does not relax any other safety gate and does
not automatically apply to a future backend or release claim.

### 7.1 No-host-fallback definition

Forbidden fallback means any target Compose or target command executing through:

- Windows Docker Desktop or its Docker socket;
- a Linux host Docker Engine or host Docker socket;
- Windows or Linux host-side Compose;
- an ad-hoc host shell outside the existing trusted Provider control path.

Allowed trusted infrastructure is the Linux `sbx` subprocess, Linux sandboxd,
the official SBX policy/network proxy, and loopback-only SBX port publication.
The Provider must continue to use `sbx create --clone`. Its source must be a
dedicated exact-SHA pinned clone containing no host credentials or unrelated
workspace files.

The calibration does not infer this boundary from an absence of warning text.
It records create/exec/ports/copy/remove argv, process identity, engine/socket
provenance, clone workspace identity, and host Docker socket non-use.

## 8. Environment calibration gate

Calibration changes the host environment only. It does not modify RepoTrial
production code or tests.

### 8.1 Provisioning evidence

Record:

- Windows, WSL, distro, kernel, KVM, systemd, CPU virtualization, memory, and
  storage facts;
- the exact official SBX package URL, version, and SHA-256;
- the WSL distribution name and documented D-drive location;
- the calibrated RepoTrial execution HEAD and runtime-input fingerprint.

### 8.2 SBX runtime evidence

Require all of the following inside `RepoTrial-Ubuntu`:

1. `sbx version` returns the pinned installed version.
2. `sbx diagnose --output json` reports all required checks PASS.
3. Authentication succeeds through the official login flow.
4. The frozen global network policy is initialized fail-closed.
5. Initial `sbx list` is empty.
6. A basic trusted fixture completes:
   `create --clone -> exec echo ok -> publish port -> copy -> destroy`.
7. A trusted clone-mode Compose fixture proves on the real Linux runtime:
   - guest Docker Engine and `docker compose up` start a health-checked Web app;
   - CPU, memory, and all three disk capacities are observable at the configured
     bounds without destructive fill or stress testing;
   - bounded public egress succeeds while private, metadata, and frozen host
     destinations are denied;
   - the published endpoint is loopback-only and serves the expected response;
   - an induced bounded workload timeout prevents further workload subprocesses,
     cancellation is retained, forced destroy still executes, and cleanup ends
     with an empty inventory;
   - the cloned guest workspace is the dedicated fixture and exposes no files
     outside that source.
8. Final `sbx list` is empty after each fixture.
9. Daemon and diagnostic logs contain no Windows self-connect equivalent,
   cleanup uncertainty, or host fallback.

The fixture must live on the distro's ext4 filesystem. A mount under `/mnt/c`
or `/mnt/d` is not accepted for calibration because it would mix Windows file
semantics into the Linux result.

### 8.3 RepoTrial provider evidence

After raw SBX and trusted Compose calibration pass:

1. clone the exact current branch/commit into the distro ext4 filesystem;
2. create the locked Python/uv environment without sharing the Windows virtual
   environment;
3. run the existing opt-in `DockerSbxProvider` integration smoke;
4. prove disk allocation, shared workload-phase total-duration, forced cleanup,
   and no-host-fallback tests on Linux;
5. require clean final inventory and clean Git status.

If the raw SBX gate fails, no RepoTrial code may be changed to compensate. If
the raw gate passes but the existing Provider smoke fails, systematic debugging
must first classify the difference as a generic RepoTrial cross-platform defect
or an environment defect.

## 9. Pre-M7.5 total-duration correction and production-change boundary

The current implementation starts `time.monotonic() + total_duration_s` inside
every `DockerSbxProvider.create()`. Because CLI and API already create one fresh
Provider instance per RepoTrial run, the smallest correct change is Provider-local
and does not require a public interface or state change:

1. the first `create()` establishes a local monotonic deadline before probe/create
   work and persists it as the Provider-run deadline only after create succeeds;
2. every later `create()` and every `exec`, `publish_port`, `copy`, and
   `network_log` uses the same deadline;
3. when remaining time is non-positive, no new workload subprocess starts;
4. a failed first create leaves no persisted deadline, preserving the frozen
   create-failure contract; after any create succeeds, later create calls in the
   same Provider run cannot reset or extend the budget;
5. `destroy` and failure cleanup remain deadline-independent and forced;
6. separate Provider instances retain independent deadlines, preserving API run
   isolation and test injection.

TDD must first demonstrate the existing reset bug across two sequential sandbox
creates in one Provider, then prove RED -> GREEN for shared budget, exhausted
second create, cleanup bypass, cancellation, monotonic time, and independent
Provider instances. This task is completed and independently reviewed before
the Linux Compose timeout calibration or M7.5 Task 2.

The expected outcome is zero production-code change for environment migration.
The total-duration correction above is a separately identified frozen-requirement
closure. If later real Linux evidence exposes another generic cross-platform
defect, the only permitted code scope is the smallest correction inside:

- `src/repotrial/sandbox/docker_sbx.py`;
- its focused unit/integration tests;
- minimal composition-root selection only if an OS-independent path is not
  already used.

Every behavior correction requires RED -> GREEN, focused review, and all
applicable gates. If a fix would alter any frozen orchestration, state, report,
Journey, mutation, or experiment contract, stop for Owner review.

## 10. Resuming M7.5 and cohort attribution

After the environment and Provider gates pass, resume the existing
`2026-08-29-m7.5-compatibility-recovery` plan at Task 2:

1. derive a deterministic environment fingerprint from the RepoTrial HEAD,
   manifest SHA-256, distro/version, WSL kernel, SBX version/commit/package
   SHA-256, guest Compose version, and global policy hash;
2. assign a documentation-only `cohort_id` to the Linux execution; do not add it
   to `RunState`, Graph checkpoints, API, CLI, or public report schemas;
3. run Umami, Listmonk, and changedetection.io in frozen order and at exact SHAs;
4. retain each attempt with explicit platform, distro, SBX version, execution
   HEAD, and metric attribution;
5. authorize a production correction only from the plan's existing dominant
   generic-cause rule;
6. run the 2/3 canary gate and then the unchanged ten-repository cohort on one
   frozen HEAD;
7. preserve all historical Windows attempts as append-only evidence.

Linux attempts do not overwrite or reinterpret Windows results. A platform
change creates a new execution cohort; final metrics must come from one Linux
execution HEAD and one calibrated environment.

Existing Windows attempt IDs, roles, attribution, and historical metrics remain
immutable. They are marked only at cohort level as superseded for the current
release decision, without rewriting attempt rows. Linux Task 2 attempts are
`diagnostic-only`. After all approved corrections, final Linux repositories #1–#3
are simultaneously the canary and the metric-bearing first three entries of the
new cohort; if the canary passes, execution continues with #4–#10 without
rerunning #1–#3. Current release metrics and all-attempt safety gates aggregate
only the current cohort, while a separate cross-cohort audit summary retains
historical failures and supersession links.

## 11. Documentation contract

Documentation changes occur only after the corresponding environment behavior
has been verified. They include:

- `README.md`: supported/recommended execution topology and concise quick start;
- `docs/dev/wsl2-linux-sbx-setup.md`: reproducible install, authentication,
  policy, verification, data location, update, and troubleshooting procedures;
- `docs/dev/pilot-evidence/wsl2-linux-sbx-calibration.md`: authoritative
  calibration commands, exit states, version/environment fingerprint, package
  digest, policy snapshot/hash, resource observations, lifecycle evidence,
  initial/final inventories, raw artifact index, calibrated execution HEAD,
  runtime-input fingerprint, and reviewer verdict;
- `docs/dev/status.md`: Windows blocker, selected Linux route, completed gates,
  and current limitations;
- the compatibility recovery spec/plan: an explicit pointer to this approved
  environment amendment;
- `docs/dev/pilot-report.md`: exact Linux execution platform and evidence for
  every new attempt.

The setup guide must distinguish:

- Docker-official package and CLI behavior;
- Microsoft-official WSL placement behavior;
- RepoTrial-tested WSL2 compatibility;
- unverified or unsupported claims.

The authoritative product-scope document is not edited merely to record a
developer environment correction. If later evidence changes the public product
contract, that requires a separate Owner-approved documentation decision.

## 12. Validation and review

### Environment gates

- independent package digest verification;
- raw trusted SBX lifecycle smoke;
- trusted clone-mode Compose/resource/network/timeout calibration;
- existing opt-in RepoTrial real-provider smoke;
- final empty sandbox inventory;
- no Windows daemon use and no host Docker fallback.

### Repository gates for any code change

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q
coverage >= 85%
pre-commit
git diff --check
```

Linux establishes a new full-test baseline. Existing Windows baseline failures
remain historical evidence and are not copied, skipped, or reclassified to make
Linux green. Any Linux failure is investigated from its own evidence.

An independent reviewer must approve:

1. this design before implementation planning;
2. the environment-calibration evidence before M7.5 resumes;
3. every production-code change, if any;
4. the final documentation and frozen cohort evidence.

Calibration validity is bound to execution inputs, not to every descendant Git
commit. The calibration record uses three separate identities without requiring
a Git object to contain its own future commit hash:

- `calibrated_execution_head`: the HEAD whose runtime inputs were calibrated;
- `runtime_input_fingerprint`: a deterministic hash over `src/repotrial`,
  `pyproject.toml`, `uv.lock`, runtime configuration, and the trusted calibration
  fixture, plus the separately recorded manifest SHA-256;
- `calibration_evidence_commit`: the commit that first persists the completed
  calibration report. Because this hash cannot self-reference from inside that
  report, the next docs-only attestation in the progress ledger/status and final
  Pilot evidence records it. The attestation does not record its own commit hash.

A descendant commit that changes only allowlisted documentation/evidence files
does not invalidate calibration when the runtime-input fingerprint and every
external environment fingerprint remain identical. Changes to source, dependency
lock/configuration, manifest, trusted fixture, WSL kernel, distro release, SBX
package/version, guest Compose version, or global policy invalidate calibration
and require a rerun. Tests are not runtime inputs, but a test-only change after
calibration is not permitted on the frozen cohort execution branch because it
would break the reviewed clean-HEAD gate.

The final cohort records its actual execution HEAD and must reproduce the same
runtime-input and environment fingerprints. All ten metric-bearing attempts use
one execution HEAD. Documentation commits made after execution preserve that HEAD
as evidence and do not rewrite it as the evidence commit.

## 13. Failure and rollback rules

- If provisioning fails before distro registration completes, retain logs and
  remove only a newly created, unregistered partial directory after exact-path
  verification and Owner authorization where destructive action is required.
- If the dedicated distro is registered but calibration fails, preserve it for
  evidence and stop. Do not mutate the user's existing Ubuntu distribution.
- Retirement is a separate destructive Owner gate. After authorization, export
  or copy the retained calibration evidence, re-verify the exact registered name
  `RepoTrial-Ubuntu` and exact absolute location
  `D:\DockerData\WSL\RepoTrial-Ubuntu`, unregister only that exact distro, then
  verify the resolved cleanup target before removing only its dedicated directory.
  Name patterns, parent-directory recursive deletion, and any operation against
  the existing `Ubuntu` are forbidden.
- If raw Linux SBX fails, do not patch RepoTrial. Classify whether the cause is
  WSL-specific before proposing the native Ubuntu VM fallback.
- If Provider smoke fails after raw SBX passes, preserve exact subprocess and
  lifecycle evidence, then use TDD for at most one generic cause at a time.
- Windows stable SBX v0.39.0 remains installed and stopped; the retained Windows
  A/B state backups are not deleted by this work.
- No failed route silently activates Docker Desktop or host Compose.

## 14. Acceptance criteria

The environment migration is accepted only when:

1. `RepoTrial-Ubuntu` is independently installed on D through documented WSL
   placement, with the existing Ubuntu untouched.
2. Linux SBX diagnose passes and both trusted lifecycle/Compose calibrations
   leave no sandbox residue.
3. The total-duration regression is fixed so one Provider/run deadline is shared
   across all sandbox workload operations while cleanup remains independent.
4. The existing RepoTrial DockerSbxProvider smoke passes without host fallback.
5. Disk, shared workload-phase total-duration, network policy, exact pinning,
   and cleanup semantics remain verified; PID remains explicitly unsupported.
6. The WSL2/Linux operating guide is reproducible from a clean environment.
7. The original M7.5 diagnostic canary resumes only after these gates.

The complete project target remains:

```text
public GitHub URL -> exact commit -> autonomous isolated Compose trial
-> deterministic journey verification -> bounded hardening experiments
-> complete evidence/report -> forced cleanup
```

The environment route is successful when it restores this product flow without
changing RepoTrial into a VM-management product or weakening its safety
contracts.
