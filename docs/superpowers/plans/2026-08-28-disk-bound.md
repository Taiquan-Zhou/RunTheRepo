# Disk Bound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the obsolete `--disk-limit` sandbox flag with three official Docker Sandboxes disk-size environment variables and enforce a calibrated disk budget.

**Architecture:** Keep disk allocation in the Docker Sandboxes provider. A small pure allocator validates `disk_mb` and computes root, Docker data, and cloned-workspace sizes; create subprocesses receive only the official environment variables. Capacity topology is verified by the real disposable integration smoke rather than adding another production lifecycle step.

**Tech Stack:** Python 3.12, asyncio subprocesses, pytest, Docker Sandboxes `sbx` v0.39.0.

**Spec:** Owner request: `RepoTrial — 继续 Disk Bound` (current task message).

## Global Constraints

- Do not run `sbx daemon start`, `sbx daemon stop`, `sbx daemon restart`, or `sbx reset`.
- Use the existing daemon only; daemon status is running and `sbx diagnose` passes.
- Work only in `D:\A all code\RepoTrial-disk-bound` on branch `codex/m7.5-disk-bound` at BASE_SHA `005102f087c863848ad2336d031e7ce42cd80e63`.
- Do not modify RepoTrial production code outside the Disk Bound implementation or its tests.
- Do not perform PID, total-duration, M7.5, push, or merge work.
- Use `DOCKER_SANDBOXES_ROOT_SIZE`, `DOCKER_SANDBOXES_DOCKER_SIZE`, and `DOCKER_SANDBOXES_CLONED_WORKSPACE_SIZE`; remove `--disk-limit`.
- Preserve the invariant `R + D + W = disk_mb`.

## Calibration Evidence

On Windows with Docker Sandboxes v0.39.0, disposable real sandboxes were
created and destroyed while changing one official control at a time. Root and
Docker data both created, reported positive capacity, executed `echo ok`, and
destroyed at 1 MiB, the smallest positive integer MiB policy value. Clone
workspace sizes 1 and 2 MiB did not contain a valid clone; 3 MiB had zero free
space; 4 MiB had only 1 KiB free and emitted an initial-fetch warning. At 5 MiB
the clone had positive free space, no initial-fetch warning, the expected HEAD,
and a clean `git fsck --full`. The calibrated working floors are therefore
`root_floor=1`, `docker_floor=1`, and `workspace_floor=5` MiB. These are
empirical compatibility floors for this installed version and trusted fixture,
not vendor-published theoretical minima.

The final default-budget mapping is 1m/2042m/5m. The 5 MiB workspace floor
probe reported:

```text
/dev/vde        3821568 2699264 755712 79% /d/A all code/RepoTrial
HEAD=005102f087c863848ad2336d031e7ce42cd80e63
FSCK_EXIT=0
```

The probes used disposable clone shells with the three official environment
variables set only for each create process, followed by forced removal and a
clean `sbx list`. The cloned workspace is `/d/A all code/RepoTrial` on
`/dev/vde`; `/run/sandbox/source` is the host read-only source mount and is not
workspace-capacity evidence.

### Task 1: Calibrated disk allocation contract

**Files:**
- Modify: `src/repotrial/sandbox/docker_sbx.py`
- Test: `tests/unit/sandbox/test_docker_sbx_commands.py`

**Interfaces:**
- Produces a pure allocation result containing root, Docker, and workspace sizes in MB.
- The provider rejects policies whose `disk_mb` is below the calibrated root plus workspace floors.

- [ ] Write a failing unit test for exact allocation and insufficient-budget rejection.
- [ ] Run the focused test and verify it fails for the missing allocator.
- [ ] Implement the smallest typed allocator and calibrated floor constants.
- [ ] Run the focused test and verify it passes.

### Task 2: Official environment-variable create contract

**Files:**
- Modify: `src/repotrial/sandbox/docker_sbx.py`
- Test: `tests/unit/sandbox/test_docker_sbx_commands.py`

**Interfaces:**
- Create invokes `sbx create` without `--disk-limit`.
- Create passes the three official variables only for the create subprocess.

- [ ] Write a failing test asserting the exact create argv and environment values.
- [ ] Run the focused test and verify it fails because the old flag is still present.
- [ ] Implement create environment propagation and remove the obsolete flag from capability requirements and argv.
- [ ] Run the focused test and verify it passes.

### Task 3: Real capacity and lifecycle verification

**Files:**
- No additional production files.

**Interfaces:**
- A disposable real smoke proves the requested root, Docker data, and cloned-workspace capacities on their actual mounts.
- The smoke executes `echo ok`, destroys the sandbox, and leaves `sbx list` clean.

- [ ] Create a trusted disposable clone with the default 1m/2042m/5m mapping.
- [ ] Inspect `/`, `/var/lib/docker`, and the actual cloned-workspace mount.
- [ ] Verify expected HEAD, `git fsck`, and `echo ok`.
- [ ] Destroy the sandbox and verify `sbx list` is clean.

### Task 4: Real validation and gates

**Files:**
- No new production files.

- [ ] Run the real disposable create → capacity check → `echo ok` → destroy flow using the existing daemon and a Temp workspace.
- [ ] Run the full unit and integration gates required by the repository.
- [ ] Review the diff for scope, invariants, and accidental changes.
- [ ] Request an independent code review and resolve important findings.
- [ ] Commit the verified Disk Bound changes without push or merge.
