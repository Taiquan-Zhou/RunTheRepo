# RepoTrial Codex Agentic Development Protocol

**Version:** 1.0  
**Applies to:** all Codex-driven implementation, refactoring, debugging, review, and release work in RepoTrial.  
**Goal:** make agentic development fast without trading away maintainability, correctness, security, or architectural clarity.

## 1. Operating principle

RepoTrial is not a “prompt-to-code” project. The repository is the system of record and every change must be reproducible from specification, tests, git history, and verification evidence.

The development hierarchy is:

**specification -> implementation plan -> failing test -> minimal implementation -> independent review -> fresh verification -> integration**

A high agent count is not a success metric. Use additional agents only when ownership is clear and their work can be independently verified.

## 2. Required Superpowers skills

The controller must route work through these skills rather than improvising an ad-hoc workflow:

| Situation | Required skill | Purpose |
|---|---|---|
| Start of a Codex session | `using-superpowers` | discover and apply the correct process skill before acting |
| Begin planned implementation | `using-git-worktrees` | isolate feature work and establish a clean baseline |
| Execute this implementation plan | `subagent-driven-development` | fresh implementer per task + task review + final review |
| 2+ truly independent domains | `dispatching-parallel-agents` | parallelize without shared-state conflicts |
| Feature / bugfix / refactor / behavior change | `test-driven-development` | enforce RED -> GREEN -> REFACTOR |
| Bug / failed test / unexpected behavior | `systematic-debugging` | root-cause investigation before fixes |
| After a task / before merge | `requesting-code-review` | independent review before defects cascade |
| Before any completion claim | `verification-before-completion` | evidence before assertions |
| Final branch integration | `finishing-a-development-branch` | verified, explicit integration/PR decision |

Superpowers is a Codex development capability, not a RepoTrial runtime dependency. Do not add it to `pyproject.toml`, Docker images, or application code.

## 3. Agent roles and separation of duties

### 3.1 Controller / main agent

The controller owns architecture, sequencing, context, rulings, review gates, and final integration. It should avoid bulk implementation when a subagent can execute the scoped task.

Controller responsibilities:
- Read the product spec and current plan before implementation begins.
- Inspect `git status`, recent history, baseline tests, and the current task contract.
- Use an isolated worktree for planned feature work.
- Record the task base SHA before delegation.
- Produce a precise task brief: scope, files, interfaces, constraints, tests, expected report.
- Select the subagent/model according to task complexity; do not use the strongest model for mechanical work by default.
- Keep implementer and reviewer roles separate.
- Independently inspect the final diff and verification evidence instead of trusting “done” reports.
- Rule on plan/code conflicts; record the ruling and consequence.
- Refuse scope creep and speculative refactors.
- Run or delegate a broad whole-branch review before release/integration.

The controller may make a tiny integration edit only when delegation would create more risk than value; such edits still require tests and review. “I can fix this quickly” is not a valid reason to bypass the process.

### 3.2 Implementer subagent

One fresh implementer owns one reviewable task or one deliberately batched set of identical mechanical changes.

The implementer must:
- Read only the provided task brief plus explicitly referenced files/interfaces.
- Confirm baseline state before editing.
- Write a failing test first and show that it fails for the intended missing behavior.
- Implement the smallest change that satisfies the test/spec.
- Refactor only after green, and only inside task scope.
- Run scoped tests and applicable quality gates.
- Self-review `git diff` for unintended files, dead code, duplication, unsafe shortcuts, and security-boundary violations.
- Commit a coherent change and return the commit SHA plus concise evidence.

The implementer must not:
- Implement future tasks.
- Rewrite interfaces without instruction.
- add “helper” abstractions for hypothetical future use.
- disable tests/lints/types to get green.
- silently weaken assertions.
- directly execute untrusted repos outside `SandboxProvider`.

### 3.3 Reviewer subagent

The reviewer is independent from the implementer and receives the task requirement plus `BASE_SHA`/`HEAD_SHA`, not the implementer’s entire reasoning transcript.

Reviewer checks, in this order:
1. **Specification compliance** — did the diff implement exactly the requested behavior, no more and no less?
2. **Correctness** — edge cases, error paths, state transitions, determinism, cleanup.
3. **Architecture** — module responsibility, dependency direction, interface stability, boundary violations.
4. **Test quality** — behavior-focused, meaningful assertions, RED/GREEN evidence, negative cases.
5. **Maintainability** — naming, duplication, complexity, dead code, accidental generalization.
6. **Security** — untrusted input, command execution, secrets, path traversal, sandbox escapes/fallbacks.
7. **Operational quality** — deterministic logs/artifacts, stop reasons, cleanup, reproducibility.

Severity:
- **Critical:** security/correctness/data-loss issue; task cannot proceed.
- **Important:** spec/architecture/test defect; fix before next task.
- **Minor:** non-blocking cleanup; fix now only if local and low-risk, otherwise record.

### 3.4 Main-agent final gate

After reviewer approval, the controller still performs a final gate:
- Compare plan requirements with the actual diff.
- Confirm only expected files changed.
- Read tests, not just test summaries.
- Run fresh verification commands or inspect fresh machine evidence from a delegated test runner.
- Confirm no Critical/Important findings remain.
- Mark the task complete in the progress ledger/status only after evidence exists.

Subagent “success” text is never proof of completion.

## 4. Safe use of parallel agents

Parallelism is encouraged only when tasks are independent.

Good parallel work:
- reviewer + documentation consistency check after an implementation diff is frozen;
- independent fixture creation in separate files with fixed interfaces;
- unrelated failing test groups with different root causes;
- benchmark analysis and README copy after metrics are immutable.

Bad parallel work:
- two agents changing the same module/interface;
- one task consumes an API another agent is still designing;
- simultaneous migrations of shared schemas;
- parallel fixes for failures that may share one root cause;
- multiple agents operating on one mutable sandbox/run state.

Rule: **one writer per file/interface/worktree at a time**. If dependency exists, execute sequentially.

## 5. Worktree, branch, and commit policy

- Never start planned feature work directly on `main`/`master` without explicit human approval.
- Detect whether Codex already placed the task in an isolated worktree before creating another.
- Prefer Codex-native worktree support; use git worktrees only as fallback.
- Establish a clean baseline by running setup and current tests before task implementation.
- One task -> one coherent commit where practical.
- Use Conventional Commit style: `feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`.
- Never mix unrelated formatting/refactors into a feature commit.
- Do not push, merge, publish, or delete branches/worktrees without the appropriate human/integration gate.

## 6. TDD contract

For production behavior changes:

1. **RED** — write the smallest behavior-focused test.
2. **Verify RED** — run it and confirm it fails for the intended missing behavior, not import/setup mistakes.
3. **GREEN** — implement the minimum behavior.
4. **Verify GREEN** — run the focused test and the relevant existing tests.
5. **REFACTOR** — clean duplication/naming while remaining green.
6. **Regression** — run applicable project quality gates.

A test that passed before implementation does not prove the new behavior. A test that only asserts mocks were called is insufficient when real behavior can be asserted.

Exceptions such as generated code/config-only changes require controller approval and must still have deterministic validation.

## 7. Debugging contract

When anything unexpectedly fails, do not stack speculative patches.

Required sequence:
- read the complete error/trace;
- reproduce consistently;
- inspect recent git changes;
- gather evidence at component boundaries;
- trace bad data/state back to its source;
- compare with a working pattern/reference;
- form one explicit hypothesis;
- test the hypothesis with the smallest change;
- write a regression test reproducing the root cause;
- implement one root-cause fix;
- verify locally and broadly.

After three failed fix hypotheses, stop adding patches. Escalate to a fresh debugging subagent/reviewer with the evidence collected so far.

## 8. Code design guardrails

The codebase should remain legible to both humans and agents.

- Each module should have one clear reason to change.
- Prefer explicit domain types over nested unvalidated dictionaries at boundaries.
- Keep orchestration separate from deterministic tools and domain rules.
- Agent graph nodes coordinate typed tools; they do not embed hidden shell scripts or duplicate provider logic.
- Preserve dependency direction: domain -> application logic -> adapters/providers; outer layers may depend inward, not vice versa.
- Avoid cyclic imports; treat them as an architectural defect, not something to patch with local imports.
- Prefer pure functions for parsing, scoring, mutation generation, and deterministic verification where possible.
- I/O boundaries must be injectable/testable.
- No global mutable state for run/session data.
- No magic fallback from a failed sandbox to host execution.
- No silent exception swallowing.
- No “temporary” duplicate implementations left behind after migration.
- No dead compatibility path without an active compatibility requirement.

Soft complexity triggers for mandatory reviewer attention (not automatic failure):
- function > ~50 logical lines;
- module > ~400 logical lines;
- deeply nested control flow (>3 levels);
- a function with >5 meaningful parameters when a domain model would be clearer;
- repeated logic appearing in three or more places.

If one of these is justified, document why rather than splitting mechanically.

## 9. Type, lint, and dependency discipline

Baseline gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q
```

Policy:
- Keep `ruff`/formatter output clean; do not blanket-ignore directories to hide failures.
- Use strict typing for core/domain/application modules; narrow third-party escape hatches at adapter boundaries.
- `# type: ignore[...]` must specify the code and include a reason when non-obvious.
- Add a runtime dependency only when a current task needs it.
- Prefer mature, actively maintained libraries and standard library solutions where adequate.
- Commit `uv.lock`; CI uses the lockfile/frozen environment.
- Do not add two libraries that solve the same problem without a written tradeoff.
- Remove unused dependencies immediately.

## 10. Test strategy

Test pyramid for RepoTrial:

- **Unit tests:** deterministic domain logic, parsing, risk findings, overlays, policy decisions, verifiers.
- **Contract tests:** every `SandboxProvider` implementation must satisfy the same provider contract suite.
- **Integration tests:** fixture apps + fake/real sandbox runner, lifecycle, cleanup, boot recovery, journey replay.
- **Security regression tests:** prompt injection fixture, host-path/socket protection, shell boundary, missing-observer semantics.
- **Eval tests:** RepoTrial-Eval fixtures and stable metrics; do not cherry-pick failed real repos.

Rules:
- Test externally observable behavior, not private implementation shape unless enforcing an architecture boundary.
- Every bug fix gets a regression test.
- Critical cleanup/fallback/safety paths need negative tests.
- Flaky tests are defects: root-cause them; do not paper over with larger sleeps/retry counts.
- Keep slow/external tests explicitly marked so CI semantics are clear.

Coverage is a guardrail, not a game. Maintain the configured branch-coverage floor while requiring stronger semantic tests for safety-critical code. Never add meaningless tests solely to raise percentage.

## 11. CI and open-source repository standard

At project bootstrap, CI should run on pull requests and pushes to the main development branch:
- dependency sync from lockfile;
- Ruff lint;
- Ruff format check;
- mypy;
- unit tests + coverage gate;
- deterministic architecture/safety guard tests.

Before public release also require:
- integration suite in a supported sandbox environment;
- dependency vulnerability audit/report;
- README quickstart verified from a clean checkout;
- `SECURITY.md`, `CONTRIBUTING.md`, license, issue/PR templates as applicable;
- no secrets or large generated artifacts committed;
- documented supported/unsupported platforms;
- honest pilot metrics, including failures.

Do not add CI badges or “passing” claims until the workflow actually exists and passes.

## 12. RepoTrial-specific architecture gates

These constraints deserve automated tests where practical:

- `repotrial.agent` must not import host-execution APIs to bypass `SandboxProvider`.
- target repository execution must route through a provider implementation.
- host Docker socket / privileged host mounts are never made available to target fixtures by fallback.
- original Compose inputs are immutable; overlays are separate artifacts.
- hardening KEEP decisions require deterministic journey evidence.
- missing collector capability remains explicitly `UNSUPPORTED/NOT_OBSERVED`.
- LLM outputs are parsed/validated into bounded schemas before tool execution.
- generated argv/DSL values are validated; avoid arbitrary LLM-generated shell/Playwright source code.

## 13. Per-task lifecycle

For task `Mx.y`:

1. Controller invokes required process skills.
2. Verify isolated workspace and clean baseline.
3. Read task + dependency interfaces + relevant code/tests only.
4. Record base SHA.
5. Dispatch fresh implementer.
6. Implementer RED -> GREEN -> REFACTOR -> scoped verification -> commit -> self-review.
7. Dispatch independent reviewer against base/head SHAs.
8. Fix Critical/Important issues; re-review the scoped fix.
9. Controller performs final diff + requirements + verification gate.
10. Record task, commit, commands, outcomes, deviations/rulings in status/ledger.
11. Advance only after the gate passes.

For high-risk tasks (sandbox execution, command construction, cleanup, secret isolation, generated tool actions), add a dedicated security review pass even if normal review is clean.

## 14. Completion evidence contract

Never write “done”, “fixed”, “all tests pass”, “ready”, or equivalent without fresh evidence.

A task completion report must include:
- task ID;
- `BASE_SHA` and `HEAD_SHA`/commit;
- changed files;
- short design summary;
- RED test command and observed failure reason;
- GREEN/scoped test command and result;
- full applicable quality-gate commands and exit status;
- reviewer findings and disposition;
- known limitations / unsupported paths;
- deviations from plan and controller rulings.

## 15. Anti-slop rules

Reject changes that exhibit these patterns unless explicitly required:
- unused classes/interfaces “for future expansion”;
- five wrappers around a one-line deterministic operation;
- one mega service/controller containing parsing, I/O, policy, and rendering;
- duplicate code paths kept because deleting one “might break something” without a test;
- broad catch-and-continue behavior;
- silent fallback values that turn errors into false success;
- tests that assert only `is not None`, HTTP 200 without semantics, or mock call counts when behavior matters;
- giant prompt strings containing business logic that belongs in typed code;
- comments narrating obvious code instead of explaining constraints/tradeoffs;
- autogenerated docs claiming unimplemented features;
- magic sleeps for concurrency/readiness;
- direct `subprocess(..., shell=True)` from agent/planner code;
- generic `utils.py` dumping ground.

## 16. First Codex session bootstrap

Before M0.1, the controller should do only preflight/governance work:

```text
You are the controller for RepoTrial. Do not implement product code yet.

1. Read AGENTS.md, the project spec, the current implementation plan, and docs/dev/AGENTIC_DEVELOPMENT_PROTOCOL.md.
2. Invoke/use superpowers:using-superpowers first.
3. Verify the Superpowers skills required by AGENTS.md are available. Do not copy/install plugin source into this repo and do not add it as an application dependency.
4. Inspect git status, branch/worktree state, and repository files.
5. Verify the implementation plan is internally consistent with the spec and the repository is ready for M0.1.
6. Return only: detected constraints, missing prerequisites, conflicts/rulings needed, and the exact next task. Do not write production code.
```

After preflight is clean, start M0.1 using `using-git-worktrees` and `subagent-driven-development`.
