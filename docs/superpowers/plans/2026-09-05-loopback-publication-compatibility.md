# Loopback Publication Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make loopback-only Compose port publications reachable through the sandbox provider using one audited trial-only overlay.

**Architecture:** A pure compatibility planner validates one unique port mapping and produces a minimal `!override` overlay. The graph persists its path/hash before baseline and runtime consumers apply it before any hardening overlay, leaving source/candidate identity and hardening decisions unchanged.

**Tech Stack:** Python 3.12, ruamel.yaml, Pydantic, pytest, LangGraph, uv.

**Spec:** `docs/superpowers/specs/2026-09-05-loopback-publication-compatibility-design.md`

## Global Constraints

- Work only in `/home/repotrial/src/RepoTrial-m7.5-release-gap` on branch `codex/m7.5-release-gap-execution`; never edit the Windows checkout.
- Use RED -> GREEN -> REFACTOR and retain the exact failing/passing commands in the task report.
- Never change the source Compose or accepted Compose to implement compatibility.
- Compatibility execution order is source/accepted Compose, compatibility overlay, hardening overlay.
- Compatibility never changes baseline/current/parent/candidate configuration hashes, KEEP/ROLLBACK, or convergence counts.
- Fail closed for ranges, ambiguity, host networking, unsupported tags, collisions, and artifact tampering.
- Keep exact-SHA intake, no host fallback, cleanup fail-closed, disk bound, host-side total-duration, and PID limitation unchanged.
- Do not run a real sandbox or restart/reset the SBX daemon from an implementation task.
- Do not push or merge.

---

### Task 1: Compatibility planner, immutable artifact, and report identity

**Files:**
- Create: `src/repotrial/compose/compatibility.py`
- Create: `tests/unit/compose/test_compatibility.py`
- Modify: `src/repotrial/domain/models.py`
- Modify: `src/repotrial/report/render.py`
- Modify: `tests/unit/report/test_render.py`

**Interfaces:**
- Produces: `CompatibilityError(reason: str)` with public `.reason`.
- Produces: frozen `CompatibilityArtifact(path: Path, sha256: str)`.
- Produces: `write_loopback_compatibility_overlay(compose: dict[str, object], container_port: int, path: Path) -> CompatibilityArtifact | None`.
- Produces: optional `RunState.compatibility_overlay_path: str | None` and `RunState.compatibility_overlay_sha256: str | None`.
- Consumes: safe parsed mappings from `load_compose`; the writer must not parse text or mutate its input.

- [ ] **Step 1: Write selector and writer RED tests**

  Add focused tests proving: the changedetection-style short binding emits
  `ports: !override` and changes only `127.0.0.1` to `0.0.0.0`; long syntax and
  bracketed IPv6 preserve target/published/protocol and extra long fields; no
  eligible binding returns `None` without creating a file; input remains equal
  to a deep copy; ranges, nonnumeric hostnames, bad protocol/port, tagged port
  values, multiple matches, host networking, and published-port/protocol
  collisions raise stable `CompatibilityError.reason` values; existing paths
  and symlinks are rejected; persisted SHA-256 matches exact file bytes.

- [ ] **Step 2: Run RED**

  Run: `.venv/bin/uv run pytest -q tests/unit/compose/test_compatibility.py`

  Expected: FAIL because `repotrial.compose.compatibility` does not exist.

- [ ] **Step 3: Implement the minimal planner/writer**

  Use `ipaddress.ip_address(...).is_loopback`, `deepcopy`, ruamel round-trip
  nodes, `CommentedSeq.yaml_set_ctag(Tag(suffix="!override"))`, and exclusive
  text creation. Validate `container_port` as an exact integer in `1..65535`.
  Convert only the unique selected entry's host IP to `0.0.0.0`; copy all other
  entries unchanged. Hash bytes read back from the newly created regular file.
  Translate all persistence failures to sanitized `CompatibilityError` reasons.

- [ ] **Step 4: Run planner GREEN and quality checks**

  Run:

  ```bash
  .venv/bin/uv run pytest -q tests/unit/compose/test_compatibility.py
  .venv/bin/uv run ruff check src/repotrial/compose/compatibility.py tests/unit/compose/test_compatibility.py
  .venv/bin/uv run ruff format --check src/repotrial/compose/compatibility.py tests/unit/compose/test_compatibility.py
  .venv/bin/uv run mypy src/repotrial/compose/compatibility.py
  ```

  Expected: all pass.

- [ ] **Step 5: Write state/report RED tests**

  Add tests proving the report emits
  `artifacts.compatibility_overlay = {reference, sha256}`, excludes that path
  from `experiment_overlays`, and emits both values as `null` when absent.

- [ ] **Step 6: Run report RED**

  Run: `.venv/bin/uv run pytest -q tests/unit/report/test_render.py`

  Expected: FAIL because the compatibility fields/report object are missing.

- [ ] **Step 7: Implement state/report projection and run GREEN**

  Add the two optional typed fields to `RunState`, project them in the report,
  and filter the compatibility reference from experiment overlays.

  Run:

  ```bash
  .venv/bin/uv run pytest -q tests/unit/report/test_render.py tests/unit/compose/test_compatibility.py
  .venv/bin/uv run ruff check src/repotrial/domain/models.py src/repotrial/report/render.py tests/unit/report/test_render.py
  .venv/bin/uv run ruff format --check src/repotrial/domain/models.py src/repotrial/report/render.py tests/unit/report/test_render.py
  .venv/bin/uv run mypy src/repotrial
  ```

  Expected: all pass.

- [ ] **Step 8: Self-review and commit**

  Inspect `git diff --check` and the complete task diff. Commit only Task 1
  files with message `feat: plan loopback publication compatibility`.

### Task 2: Baseline/candidate parity and checkpoint-safe graph threading

**Files:**
- Modify: `src/repotrial/agent/graph.py`
- Modify: `src/repotrial/hardening/engine.py`
- Modify: `src/repotrial/trial/boot.py`
- Modify: `src/repotrial/trial/observer.py`
- Modify: `src/repotrial/trial/startup_inputs.py`
- Modify: `tests/unit/agent/test_graph.py`
- Modify: `tests/unit/hardening/test_engine.py`
- Modify: `tests/unit/trial/test_boot.py`
- Modify: `tests/unit/trial/test_observer.py`
- Modify: `tests/unit/trial/test_startup_inputs.py`

**Interfaces:**
- Consumes: `write_loopback_compatibility_overlay`, `CompatibilityArtifact`, and `CompatibilityError` from Task 1.
- Consumes: `RunState.compatibility_overlay_path` and `RunState.compatibility_overlay_sha256` from Task 1.
- Produces: optional keyword `compatibility_overlay_path: str | None` on Boot, observation, and startup-input materialization boundaries.
- Produces: optional `ExperimentContext.compatibility_overlay_path: Path | None`.

- [ ] **Step 1: Write command-order RED tests**

  Extend Boot, observer, and startup-input tests so commands with both overlays
  contain `-f compose.yaml -f compatibility.overlay.yaml -f candidate.overlay.yaml`
  in that exact order. Preserve existing one-overlay and no-overlay behavior.

- [ ] **Step 2: Run boundary RED**

  Run:

  ```bash
  .venv/bin/uv run pytest -q tests/unit/trial/test_boot.py tests/unit/trial/test_observer.py tests/unit/trial/test_startup_inputs.py
  ```

  Expected: FAIL because the compatibility keyword is not accepted/threaded.

- [ ] **Step 3: Implement minimal ordered Compose-file threading**

  Validate the compatibility path with the same boundary rules as the existing
  overlay path, append it before the existing overlay, and do not alter command
  timeouts, environment construction, evidence redaction, or parsing.

- [ ] **Step 4: Run boundary GREEN**

  Re-run the Step 2 command. Expected: all pass.

- [ ] **Step 5: Write graph/engine RED tests**

  Add tests proving: baseline materializes and records one compatibility overlay
  before sandbox creation; baseline Boot and observation receive it; candidates
  receive compatibility before hardening for startup validation, Boot, and
  observation; hardening parent/candidate hashes and verdict are unchanged;
  source Compose bytes are unchanged; no eligible mapping creates no artifact;
  planner rejection routes to `compatibility:<reason>` without sandbox creation;
  checkpoint resume accepts the exact artifact and rejects deletion,
  replacement, symlink insertion, path/hash mismatch, and a newly different
  planned artifact.

- [ ] **Step 6: Run graph/engine RED**

  Run:

  ```bash
  .venv/bin/uv run pytest -q tests/unit/agent/test_graph.py tests/unit/hardening/test_engine.py
  ```

  Expected: FAIL on missing compatibility selection/threading/replay checks.

- [ ] **Step 7: Implement graph and engine integration**

  In baseline, plan the artifact at a deterministic direct child of the verified
  overlay directory, persist its relative path/hash in `RunState`, append its
  reference once, and verify rather than rewrite on replay. Add a conditional
  baseline route so `CompatibilityError` yields `compatibility:<reason>` and
  reaches reporting without sandbox creation. Validate the existing artifact
  again before every candidate, pass it via `ExperimentContext`, and thread it
  before the hardening overlay without applying it to any hash input.

- [ ] **Step 8: Run integration GREEN and scoped quality gates**

  Run:

  ```bash
  .venv/bin/uv run pytest -q tests/unit/compose/test_compatibility.py tests/unit/trial/test_boot.py tests/unit/trial/test_observer.py tests/unit/trial/test_startup_inputs.py tests/unit/hardening/test_engine.py tests/unit/agent/test_graph.py tests/unit/report/test_render.py
  .venv/bin/uv run ruff check src/repotrial tests/unit/compose/test_compatibility.py tests/unit/trial/test_boot.py tests/unit/trial/test_observer.py tests/unit/trial/test_startup_inputs.py tests/unit/hardening/test_engine.py tests/unit/agent/test_graph.py tests/unit/report/test_render.py
  .venv/bin/uv run ruff format --check src/repotrial tests/unit/compose/test_compatibility.py tests/unit/trial/test_boot.py tests/unit/trial/test_observer.py tests/unit/trial/test_startup_inputs.py tests/unit/hardening/test_engine.py tests/unit/agent/test_graph.py tests/unit/report/test_render.py
  .venv/bin/uv run mypy src/repotrial
  git diff --check
  ```

  Expected: all pass.

- [ ] **Step 9: Self-review and commit**

  Confirm the diff contains no real-sandbox call, repo-specific branch, source
  Compose rewrite, hash-policy change, or weakened failure. Commit Task 2 files
  with message `fix: apply loopback compatibility across trials`.
