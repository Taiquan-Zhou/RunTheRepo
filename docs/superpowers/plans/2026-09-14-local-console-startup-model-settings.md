# Local Console Startup and Model Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one console launch start and verify SBX, and let users persist model configuration safely in the loopback Web UI.

**Architecture:** Add a bounded local service manager and an owner-only model settings store, compose them in the FastAPI factory, and keep the API key out of job HTTP contracts and evidence. Preserve the existing CLI/provider boundaries.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, Jinja2, browser JavaScript, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-14-local-console-startup-model-settings-design.md`

## Global Constraints

- Bind only to loopback and preserve same-origin plus CSRF checks.
- Use argv subprocesses only; no shell execution.
- Never expose the API key in GET responses, job payloads, argv, localStorage, logs, reports, or target workloads.
- Do not weaken SandboxProvider isolation, exact SHA pinning, report identity, or terminal evidence.
- Keep changes scoped to startup dependency handling, model settings, configuration grouping, documentation, and their tests.
- TDD is mandatory and all repository quality gates must pass.

---

### Task 1: Startup dependency manager, persistent model settings, and Web integration

**Files:**
- Create: `src/repotrial/local_web/services.py`
- Create: `src/repotrial/local_web/settings.py`
- Modify: `src/repotrial/local_web/app.py`
- Modify: `src/repotrial/local_web/runner.py`
- Modify: `src/repotrial/cli.py`
- Modify: `src/repotrial/local_web/templates/index.html.j2`
- Modify: `src/repotrial/local_web/templates/usage-guide.html.j2`
- Test: `tests/unit/local_web/test_services.py`
- Test: `tests/unit/local_web/test_settings.py`
- Modify: `tests/unit/local_web/test_local_web_app.py`
- Modify: `tests/unit/local_web/test_local_web_runner.py`
- Modify: `tests/unit/local_web/test_console_browser.py`
- Modify: `tests/unit/local_web/test_console_browser_extra.py`
- Modify when required: `tests/unit/test_cli.py`

**Interfaces:**
- `RequiredServices.ensure_ready() -> DoctorReport` starts a stopped SBX daemon with `sbx daemon start --detach`, validates daemon status, and returns bounded checks.
- `ModelSettingsStore.load_public() -> PublicModelSettings`, `load_resolved() -> ResolvedModelSettings | None`, `save(update) -> PublicModelSettings`, and `clear_key() -> PublicModelSettings`.
- `GET /api/settings/model`, `PUT /api/settings/model`, and `DELETE /api/settings/model/key` expose only the public settings shape.
- Job submission reads resolved settings server-side; the public `JobInput` no longer needs model secrets.
- `CliRunner` may carry a secret only in memory and in the trusted child environment; `argv()` must never include it.
- CLI model construction consumes and removes `REPOTRIAL_MODEL_API_KEY` before sandbox/provider subprocesses can inherit it.

- [ ] **Step 1: Write failing service-manager tests**

Cover already-running, stopped then successfully started, nonzero start, malformed JSON status, timeout, and the exact argv/timeout bounds. Assert no shell use and no daemon reset/restart.

- [ ] **Step 2: Run the focused service tests and record the expected RED**

Run: `uv run pytest -q tests/unit/local_web/test_services.py`
Expected: failure because the service manager does not exist.

- [ ] **Step 3: Implement the minimal service manager**

Reuse the bounded command adapter. Parse only the documented JSON shape required to prove the daemon is running. Return stable check names, details, and remediation without copying raw command output.

- [ ] **Step 4: Write failing settings-store and API tests**

Cover provider validation, endpoint credential rejection, DeepSeek defaults, file mode `0600`, atomic replace, corrupt/symlink failure, retain/replace/clear key semantics, GET non-disclosure, CSRF, and active-job behavior.

- [ ] **Step 5: Run the focused settings/API tests and record the expected RED**

Run: `uv run pytest -q tests/unit/local_web/test_settings.py tests/unit/local_web/test_local_web_app.py`
Expected: failures for missing settings interfaces and routes.

- [ ] **Step 6: Implement settings persistence and server-side resolution**

Use a configurable store path for tests and the XDG user config path by default. Keep the secret out of Pydantic repr where practical, public projections, job status, and exception text. Fail closed without destroying a previous valid file.

- [ ] **Step 7: Write failing runner/CLI secret-boundary tests**

Prove the key is absent from argv and status, present only in the trusted CLI child environment, consumed before downstream subprocess work, and never copied to the graph target environment.

- [ ] **Step 8: Implement the minimal trusted secret handoff**

Do not change target-workload environment behavior. Preserve endpoint/name CLI compatibility for direct CLI users.

- [ ] **Step 9: Write failing browser layout and interaction tests**

Assert Compose appears with repository fields outside `details`; Advanced contains only the model provider, endpoint, name, password input, configured status, save, and clear controls. Cover Chinese and English text, DeepSeek defaults, keyboard labels, save errors, and mobile layout.

- [ ] **Step 10: Implement the UI and startup flow**

Run service readiness during FastAPI lifespan before accepting jobs. Render precise dependency status. Update the usage guide so users start the Web once and configure the model there.

- [ ] **Step 11: Run focused GREEN verification**

Run:
`uv run pytest -q tests/unit/local_web tests/unit/test_cli.py tests/unit/test_doctor.py`
Expected: all pass.

- [ ] **Step 12: Self-review the complete diff**

Inspect secret handling, filesystem ownership checks, subprocess environments, lifecycle ordering, stale copy, accessibility, i18n, responsive layout, and unrelated changes. Do not modify unrelated dirty files.

- [ ] **Step 13: Run all repository gates**

Run:
`uv run ruff check .`
`uv run ruff format --check .`
`uv run mypy src/repotrial`
`uv run pytest -q --cov=repotrial --cov-report=term`
Expected: all applicable gates pass with coverage at or above the configured threshold.
