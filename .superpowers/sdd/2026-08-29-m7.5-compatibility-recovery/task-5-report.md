# Task 5 Report — derive Journey input from pinned root README

## Identity and scope

- BASE_SHA: `50ea8d71e816ee47fe9845416eff16591ae59d58`
- HEAD before Task 5 commit: `50ea8d71e816ee47fe9845416eff16591ae59d58`
- Branch: `codex/m7.5-compatibility-recovery`
- No real SBX, daemon, or public repository was run.

Changed files:

- `src/repotrial/trial/journey_context.py`
- `src/repotrial/agent/graph.py`
- `tests/unit/trial/test_journey_context.py`
- `tests/unit/agent/test_graph.py`
- `tests/integration/test_cli_e2e_fixture.py`
- `.superpowers/sdd/2026-08-29-m7.5-compatibility-recovery/task-5-report.md`

No `GraphContext`, `RunState`, `GraphState`, provider, recovery, CLI/API, report,
or Journey/verifier contract was changed. The derived text is runtime-local and is
passed only to `plan_journeys()` in `_baseline` when the explicit excerpt is empty.

## RED evidence

Required command attempted before production edits:

```text
uv run pytest -q tests/unit/trial/test_journey_context.py tests/unit/agent/test_graph.py tests/integration/test_cli_e2e_fixture.py
```

It could not start pytest because the worktree's pre-existing `.venv\\lib64`
reparse point could not be removed:

```text
error: failed to remove file `D:\\A all code\\RepoTrial-m7.5-compatibility-recovery\\.venv\\lib64`: Access is denied. (os error 5)
```

After confirming the parent worktree virtual environment was installed editable
against the parent worktree, the same test selection was run with its interpreter
and `PYTHONPATH` set to this worktree's `src`. It failed during collection exactly
because the new private module did not exist:

```text
ImportError: cannot import name 'journey_context' from 'repotrial.trial'
1 error in 0.91s
```

This RED was observed before the final production implementation. A subsequent
test-first cycle for the post-open/final-identity swap check repeated the same
missing-module collection RED before restoring the minimal implementation.

## GREEN and gate evidence

Because `uv run` is blocked by the pre-existing worktree environment, the bundled
parent virtual environment was invoked directly with `PYTHONPATH` explicitly set
to this worktree's `src` for tests. This verified the current worktree source,
not the parent editable install.

```text
tests/unit/trial/test_journey_context.py                         10 passed in 0.18s
tests/unit/trial/test_journey_planner.py                         45 passed in 0.38s
tests/integration/test_cli_e2e_fixture.py                        31 passed in 17.54s
tests/unit/journey                                               68 passed in 0.82s
tests/unit/agent/test_graph.py -k "not intermediate_link and not replaced_by_link"
                                                                  30 passed, 2 deselected in 17.36s
```

The non-graph focused total is `154 passed in 17.30s` in one fresh final command.

The unfiltered graph command was also run. Its only failures are pre-existing
Windows test-environment limitations: both tests fail while creating a symbolic
link with `WinError 1314` (privilege not held), before reaching RepoTrial code:

- `test_keep_rejects_accepted_directory_reached_through_intermediate_link`
- `test_stop_rejects_selected_overlay_replaced_by_link`

Fresh static and scope gates:

```text
ruff check ...       All checks passed!
ruff format --check  5 files already formatted
mypy src/repotrial  Success: no issues found in 41 source files
git diff --check     exit 0, no output
```

## Self-review

- Scope: only brief-authorized source/tests plus this required report changed.
- Security: root-only candidate names; regular-file/link/reparse rejection;
  65,536-byte bounded read plus extra-byte rejection; UTF-8-only decode;
  pre/open/final file-identity checks; README remains inert text.
- Data flow: derivation occurs in `_baseline`, after graph intake; explicit
  non-empty context wins; the original explicit context value remains the sole
  README argument to `propose_recovery()`.
- Contracts: deterministic Journey DSL and existing verifier paths are reused;
  no persisted state or public schema receives README content.
- Dead code/hidden behavior: no new dependency, adapter, provider, or fallback
  behavior was added.

## Concerns

1. Required `uv run` gate commands cannot execute in this worktree because its
   pre-existing `.venv\\lib64` reparse point is access-denied. Direct execution
   against the bundled parent virtual environment with explicit `PYTHONPATH`
   supplied the recorded current-source verification instead.
2. Two existing graph tests requiring Windows symbolic-link creation cannot run
   on this host (`WinError 1314`). No tests were modified, skipped, or weakened;
   the remaining graph tests passed.
