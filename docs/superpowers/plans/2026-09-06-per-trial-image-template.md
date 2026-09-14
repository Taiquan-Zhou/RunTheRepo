# Per-Trial Image Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull or build each target Compose image set once per trial and reuse the exact immutable image store across isolated baseline and candidate sandboxes.

**Architecture:** A dedicated pre-baseline warmup sandbox resolves, pulls, and builds Compose without starting target services. `DockerSbxProvider` saves its inner Docker store as one run-owned SBX template; later creates use that template, verify the normalized image identity, and start Compose with pull/build disabled. Invocation finalization removes and verifies removal of the exact owned template through a cancellation-safe cleanup boundary.

**Tech Stack:** Python 3.12, asyncio, Pydantic, Docker Compose v2 inside Docker SBX v0.39.0, pytest, uv, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-06-per-trial-image-template-design.md`

## Global Constraints

- Work only in WSL `RepoTrial-Ubuntu` at `/home/repotrial/src/RepoTrial-m7.5-release-gap` on `codex/m8-doctor-preflight`.
- Keep the existing 900-second host-side total-duration bound; warmup and template save consume it, cleanup remains separately command-bounded.
- Never run target Compose on the host or expose the host Docker socket, API key, credentials, or real secrets.
- Reuse is per provider invocation only; never reuse a template across runs, repositories, or commits.
- Baseline and candidates retain independent workspaces, containers, networks, and volumes.
- Missing capability, identity mismatch, and unconfirmed cleanup fail closed; there is no repeated-pull or host fallback.
- Preserve exact-SHA clone verification, CPU/memory/disk/network policy, deterministic journeys, and the documented PID limitation.
- Real sandbox operations are serial. Do not start, stop, restart, or reset the SBX daemon.

---

### Task 1: Fail-closed SBX template lifecycle

**Files:**
- Modify: `src/repotrial/sandbox/base.py`
- Modify: `src/repotrial/sandbox/docker_sbx.py`
- Modify: `src/repotrial/sandbox/fake.py`
- Test: `tests/unit/sandbox/test_docker_sbx_commands.py`
- Test: `tests/unit/sandbox/test_fake.py`

**Interfaces:**
- Consumes: existing `SandboxProvider.create()`, `_run()`, deadline, ownership, and cleanup state.
- Produces: `supports_runtime_templates: bool`, `activate_runtime_template(sandbox_id: str, image_identity_sha256: str) -> None`, `expected_image_identity_sha256() -> str | None`, and `finalize_runtime_template() -> None`.

- [ ] **Step 1: Write provider-contract RED tests**

Add tests proving the fake reports no template capability and no identity, while the Docker provider requires exact help tokens for `create --template`, `template save`, `template ls --json`, and `template rm`.

```python
assert FakeSandboxProvider().supports_runtime_templates is False
assert provider.expected_image_identity_sha256() is None
assert ("sbx", "template", "save", "--help") in spawner.calls
assert ("sbx", "template", "ls", "--help") in spawner.calls
assert ("sbx", "template", "rm", "--help") in spawner.calls
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
.venv/bin/uv run pytest -q tests/unit/sandbox/test_fake.py tests/unit/sandbox/test_docker_sbx_commands.py -k 'runtime_template or template_capability'
```

Expected: failures because the typed capability and SBX commands do not exist.

- [ ] **Step 3: Add the minimal typed provider boundary**

Add safe default behavior to `SandboxProvider` so existing test doubles remain source-compatible. The fake remains explicitly unsupported. `DockerSbxProvider` returns true and stores at most one active template identity.

```python
@property
def supports_runtime_templates(self) -> bool:
    return False


async def activate_runtime_template(
    self, sandbox_id: str, image_identity_sha256: str
) -> None:
    raise RuntimeError("runtime templates are unsupported")


def expected_image_identity_sha256(self) -> str | None:
    return None


async def finalize_runtime_template(self) -> None:
    return None
```

- [ ] **Step 4: Write lifecycle RED tests**

Cover unique strict tags, pre-existing-tag rejection, save/list identity resolution, `create --template <owned-tag>`, removal by exact image ID, post-remove absence verification, nonzero removal, malformed/duplicate JSON keys, cancellation during finalization, activation failure after save, and protection of `shell-docker` plus unrelated templates.

```python
await provider.activate_runtime_template("owned-sandbox", "a" * 64)
created = await provider.create(workspace, "candidate")
assert_template_argument(spawner.calls, created)
await provider.finalize_runtime_template()
assert ("sbx", "template", "rm", saved_image_id) in spawner.calls
```

- [ ] **Step 5: Implement owned template lifecycle**

Use an unpredictable strict tag such as `repotrial-runtime:<32 lowercase hex>`. Probe capabilities before first create. Before save, list templates and reject a matching tag. After save, list again and resolve exactly one matching repository/tag to a full validated image ID. Candidate creates pass `--template <owned-tag>` while retaining `--clone` and all resource/network flags. Finalization removes the exact saved image ID without the trial deadline, lists again, and succeeds only when that ID is absent.

On save/list failure after the image may exist, attempt exact-tag cleanup. Preserve both primary and cleanup evidence if cleanup is unconfirmed. Never infer success from sandbox inventory.

- [ ] **Step 6: Run GREEN provider tests and quality checks**

```bash
.venv/bin/uv run pytest -q tests/unit/sandbox/test_fake.py tests/unit/sandbox/test_docker_sbx_commands.py
.venv/bin/uv run ruff check src/repotrial/sandbox tests/unit/sandbox
.venv/bin/uv run ruff format --check src/repotrial/sandbox tests/unit/sandbox
.venv/bin/uv run mypy src/repotrial/sandbox
```

- [ ] **Step 7: Commit Task 1**

```bash
git add src/repotrial/sandbox/base.py src/repotrial/sandbox/docker_sbx.py src/repotrial/sandbox/fake.py tests/unit/sandbox/test_docker_sbx_commands.py tests/unit/sandbox/test_fake.py
git commit -m "Add owned SBX runtime templates"
```

### Task 2: Compose image preparation and immutable identity

**Files:**
- Create: `src/repotrial/trial/image_template.py`
- Modify: `src/repotrial/trial/boot.py`
- Create: `tests/unit/trial/test_image_template.py`
- Modify: `tests/unit/trial/test_boot.py`

**Interfaces:**
- Consumes: validated Compose argv/env construction, `SandboxProvider.exec()`, and Task 1 template methods.
- Produces: `ImageInventory`, `prepare_compose_image_template(...) -> ImageInventory`, `verify_compose_image_identity(...) -> None`, and startup argv that includes `--pull never --no-build` only when an active template identity exists.

- [ ] **Step 1: Write strict image-inventory parser RED tests**

Use newline-delimited `docker image ls --all --no-trunc --digests --format '{{json .}}'` fixtures. Accept bounded unique records with full `sha256:<64 hex>` IDs, normalize `Repository`, `Tag`, and `Digest`, sort them, and hash canonical JSON. Reject invalid UTF-8/JSON, duplicate keys, missing/extra fields, abbreviated IDs, duplicate normalized records, excessive records/output, and empty inventory after a successful preparation.

```python
inventory = parse_image_inventory(valid_lines)
assert inventory.records == tuple(sorted(inventory.records))
assert len(inventory.sha256) == 64
```

- [ ] **Step 2: Run parser RED tests**

```bash
.venv/bin/uv run pytest -q tests/unit/trial/test_image_template.py -k inventory
```

Expected: import or symbol failure.

- [ ] **Step 3: Implement bounded inventory parsing**

Create frozen typed records and canonical hashing in `image_template.py`. Persist only image IDs, public repository/tag/digest strings, command outcome, and hashes; never persist Compose environment values.

- [ ] **Step 4: Write preparation RED tests**

Prove the controlled order is Compose config/preflight, `pull --ignore-buildable`, `build`, image inventory, verified guest-workspace removal by direct argv, and provider template activation. Prove target services are never started, failures are bounded, and template activation never occurs after a failed pull/build/inventory/removal.

```python
assert compose_pull[-2:] == ("pull", "--ignore-buildable")
assert compose_build[-1] == "build"
assert all("up" not in call for call in provider.exec_calls)
assert provider.activated_identity == inventory.sha256
```

- [ ] **Step 5: Implement preparation and verification**

Reuse the existing Compose preflight and validated environment prefix instead of constructing shell commands. Pull non-build services once, build buildable services once, collect the normalized identity, verify `pwd` is the absolute guest git root already proven by the provider, then remove only that exact guest workspace through direct `rm --recursive --force -- <path>` argv before saving the template.

For baseline/candidate startup, collect and compare the inventory before Compose execution. With an active identity, execute:

```python
[
    *prefix,
    *docker_compose,
    "up",
    "-d",
    "--wait",
    "--wait-timeout",
    "60",
    "--pull",
    "never",
    "--no-build",
]
```

Apply the same no-pull/no-build guarantee to readiness rechecks. An identity mismatch raises a bounded `ImageTemplateError("image_identity_mismatch")` before any service starts.

- [ ] **Step 6: Run GREEN Compose tests and quality checks**

```bash
.venv/bin/uv run pytest -q tests/unit/trial/test_image_template.py tests/unit/trial/test_boot.py
.venv/bin/uv run ruff check src/repotrial/trial tests/unit/trial
.venv/bin/uv run ruff format --check src/repotrial/trial tests/unit/trial
.venv/bin/uv run mypy src/repotrial/trial
```

- [ ] **Step 7: Commit Task 2**

```bash
git add src/repotrial/trial/image_template.py src/repotrial/trial/boot.py tests/unit/trial/test_image_template.py tests/unit/trial/test_boot.py
git commit -m "Prepare Compose images once per trial"
```

### Task 3: Graph preparation, evidence, and guaranteed finalization

**Files:**
- Modify: `src/repotrial/agent/graph.py`
- Modify: `src/repotrial/sandbox/lifecycle.py`
- Modify: `tests/unit/agent/test_graph.py`
- Modify: `tests/unit/sandbox/test_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 provider lifecycle and Task 2 preparation function.
- Produces: one warmup before baseline, `image-template.jsonl` evidence, and cancellation-safe finalization around `ainvoke_run()` and `aresume_run()`.

- [ ] **Step 1: Write graph/lifecycle RED tests**

Cover exactly-one preparation for a capable provider, no behavior change for the deterministic fake, warmup sandbox cleanup before baseline creation, compatibility/startup-input materialization in warmup, all warmup failures routing to an explainable stop reason, finalization on success/failure/cancellation/interrupt, and dual-failure preservation when graph execution and template cleanup both fail.

```python
result = await ainvoke_run(graph, state, context=context)
assert provider.events == [
    "warmup_create",
    "prepare",
    "activate",
    "warmup_destroy",
    "baseline_create",
    "candidate_create",
    "template_remove",
]
```

- [ ] **Step 2: Run graph/lifecycle RED tests**

```bash
.venv/bin/uv run pytest -q tests/unit/agent/test_graph.py tests/unit/sandbox/test_lifecycle.py -k 'image_template or runtime_template'
```

Expected: failures because invocation preparation/finalization is not wired.

- [ ] **Step 3: Implement one-time warmup and audit evidence**

In the baseline boot node, when `supports_runtime_templates` is true and no identity is active, create the run-unique warmup through `managed_sandbox`, materialize the same compatibility/startup inputs, prepare images, activate the template, and leave the context so warmup destruction completes before baseline creation. Record bounded JSONL events under the claimed baseline attempt directory.

Wrap both graph invocation entry points in a cancellation-safe finalization helper modeled on managed-sandbox cleanup. If finalization fails, do not return a successful run. Preserve the primary failure and report template cleanup as cleanup evidence.

- [ ] **Step 4: Run GREEN graph/lifecycle tests and the combined focused suite**

```bash
.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py tests/unit/sandbox/test_lifecycle.py tests/unit/trial/test_image_template.py tests/unit/trial/test_boot.py tests/unit/agent/test_graph.py
.venv/bin/uv run ruff check src/repotrial tests/unit
.venv/bin/uv run ruff format --check src/repotrial tests/unit
.venv/bin/uv run mypy src/repotrial
```

- [ ] **Step 5: Self-review and commit Task 3**

Inspect the complete diff for raw secret/env persistence, broad template deletion, host execution, deadline weakening, exception loss, and candidate state sharing.

```bash
git diff --check
git diff d641503d -- src/repotrial tests/unit
git add src/repotrial/agent/graph.py src/repotrial/sandbox/lifecycle.py tests/unit/agent/test_graph.py tests/unit/sandbox/test_lifecycle.py
git commit -m "Reuse exact Compose images across trial sandboxes"
```

### Task 4: Independent review and serial real verification

**Files:**
- Modify only if review or real evidence exposes a generic defect.
- Evidence: run artifact directories outside the target workspace.

**Interfaces:**
- Consumes: Tasks 1-3 final committed SHA.
- Produces: reviewer verdict, trusted smoke evidence, and two canary reports on one final HEAD.

- [ ] **Step 1: Independent Sol-high review**

Review spec compliance, template ownership, cancellation/cleanup behavior, immutable identity, no secret persistence, no host fallback, isolation, and tests. A finding must cite file/line, impact, and a concrete failing scenario. Resolve only evidenced generic defects through a new RED→GREEN fix round.

- [ ] **Step 2: Trusted real SBX smoke**

Run one local fixture serially through image acquisition, template save, baseline create/start/echo/destroy, candidate create/start/echo/destroy, and template removal. Verify matching image IDs, distinct sandbox/container/volume identities, `sbx list` empty, and no RepoTrial-owned template remains. Do not restart/reset the daemon.

- [ ] **Step 3: Rerun focused verification after smoke fixes**

```bash
.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py tests/unit/sandbox/test_lifecycle.py tests/unit/trial/test_image_template.py tests/unit/trial/test_boot.py tests/unit/agent/test_graph.py
.venv/bin/uv run ruff check .
.venv/bin/uv run ruff format --check .
.venv/bin/uv run mypy src/repotrial
```

- [ ] **Step 4: Run frozen Wakapi then Linkding canaries serially**

Use the existing exact repository URLs, frozen SHAs, model endpoint/name, inherited API key, and explicit proxy. Require complete JSON/HTML reports, cleanup PASS, matching requested/actual SHA, explainable terminal reason, and empty official sandbox/template inventory. If Wakapi exposes one generic issue, stop, fix only that issue with RED→GREEN, review, and rerun it once before Linkding.

- [ ] **Step 5: Run release gates only after both canaries pass on one HEAD**

```bash
.venv/bin/uv run pytest -q
.venv/bin/uv run coverage report --fail-under=85
.venv/bin/uv run pre-commit run --all-files
```

Update release-facing README/install/limitations/troubleshooting only from verified final behavior. Do not push, merge, or create a GitHub release without a new explicit Owner instruction.

### Task 5: Stream a bounded image bundle into each isolated sandbox

**Files:**
- Modify: `src/repotrial/sandbox/base.py`
- Modify: `src/repotrial/sandbox/docker_sbx.py`
- Modify: `src/repotrial/trial/image_template.py`
- Modify: `tests/unit/sandbox/test_docker_sbx_commands.py`
- Modify: `tests/unit/trial/test_image_template.py`
- Modify only when required for audit serialization: `tests/unit/agent/test_graph.py`

**Interfaces:**
- Consumes: Task 2 `ImageInventory`, Task 1 invocation-owned template state,
  the existing sanitized direct-argv subprocess boundary, per-sandbox trial
  deadline, and Task 3 guaranteed finalization.
- Produces: `SandboxProvider.stage_runtime_image_bundle(sandbox_id: str,
  image_references: tuple[str, ...], image_ids: tuple[str, ...]) -> None`, an
  invocation-owned private bundle identity in runtime-template audit evidence,
  automatic direct-stream import before a template-created sandbox is returned,
  and combined template/bundle cleanup confirmation.

- [ ] **Step 1: Write provider and preparation RED tests**

Add strict behavior tests that fail against the current template-only code and
name the concrete breaks they catch:

- preparation passes sorted unique immutable IDs plus every validated
  repository/tag reference needed by Compose, after inventory and before
  workspace clear/template activation;
- export uses direct argv `sbx exec <warmup> -- docker image save ...`, streams
  stdout to one exclusive mode-0600 temporary file outside the repository, and
  records a non-empty SHA-256/byte-size identity without buffering the archive;
- archive bytes are rejected and the process reaped when the Docker allocation
  byte bound is exceeded; malformed IDs/references, empty output, nonzero exit,
  excessive stderr, timeout, I/O failure, and cancellation fail closed;
- every template-created sandbox imports through stdin using exact argv `sbx
  exec <sandbox> -- docker image load` before `create()` returns, within the
  existing trial deadline; missing/replaced/changed/oversized bundles, write or
  read failure, nonzero exit, timeout, and cancellation trigger normal sandbox
  cleanup and never record a successful template use;
- finalization attempts both exact-tag template removal and exact-file bundle
  removal after success, primary failure, cancellation, or expired trial
  deadline; it preserves dual failures and reports cleanup failure unless both
  removals are confirmed;
- `begin_invocation()` rejects any leftover or unconfirmed bundle state, and
  unrelated files/templates are never deleted;
- public runtime-template audit evidence contains only bundle SHA-256 and byte
  size, never its path, bytes, environment, or credentials.

Run the smallest named tests and retain their expected failing output before
editing production code.

- [ ] **Step 2: Implement minimal streaming export/import and ownership**

Keep one provider-owned bundle per invocation. Derive its maximum bytes from the
existing Docker disk allocation, create it exclusively in the host temporary
directory with private permissions, and retain exact file identity. Stream
export chunks directly into the file while hashing and counting; stream the
same opened file directly into `docker image load` while hashing/counting again.
Reject a changed identity before accepting import. The host must never invoke
Docker on the bundle or deserialize it.

Use validated, option-safe image references to preserve Compose-resolvable tags
and validated full image IDs to preserve untagged images. No shell, pipeline,
temporary guest archive, host Docker socket, network fallback, second pull, or
second build is allowed. Insert import inside `DockerSbxProvider.create()` after
successful sandbox creation/clone verification and before returning or recording
template use. Keep the later full inventory comparison as the final authority.

Finalization must attempt cleanup of both resources even when either cleanup
fails and must retain enough exact state to retry safely when confirmation is
missing. `removal_confirmed=true` means both owned resources are absent.

- [ ] **Step 3: Run GREEN focused gates and commit**

```bash
.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py tests/unit/trial/test_image_template.py tests/unit/agent/test_graph.py -k 'runtime_template or image_template or image_bundle'
.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py tests/unit/sandbox/test_lifecycle.py tests/unit/trial/test_image_template.py tests/unit/trial/test_boot.py tests/unit/agent/test_graph.py
.venv/bin/uv run ruff check src/repotrial/sandbox src/repotrial/trial tests/unit/sandbox tests/unit/trial tests/unit/agent/test_graph.py
.venv/bin/uv run ruff format --check src/repotrial/sandbox src/repotrial/trial tests/unit/sandbox tests/unit/trial tests/unit/agent/test_graph.py
.venv/bin/uv run mypy src/repotrial
git diff --check
```

Commit only the scoped implementation, regression tests, and this approved
design/plan amendment using the configured Owner identity and no coauthor.

- [ ] **Step 4: Independent review, then resume Task 4 serial verification**

Obtain an independent Sol-high review of subprocess cleanup, deadline handling,
bundle ownership/integrity, audit secrecy, no host execution, and sandbox
isolation. Resolve only Critical/Important evidenced defects via a bounded
RED→GREEN fix round. After approval, rerun the trusted fixture serially and then
continue Task 4 from its canary step; do not run real sandboxes in parallel.
