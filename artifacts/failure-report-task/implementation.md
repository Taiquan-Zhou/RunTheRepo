# Terminal exception report implementation

## Scope

- Updated only `src/repotrial/cli.py`, `src/repotrial/report/failure.py`,
  `tests/unit/test_cli.py`, `tests/unit/report/test_failure.py`, and this report.
- Existing dirty documentation files were preserved.
- No sandbox, network, API, LLM, local-Web runner, or domain interface was changed.

## RED

Added `test_docker_exec_timeout_emits_failure_report` before the implementation.
The focused run failed as expected because the existing exception path persisted
`report_paths={"html": None, "json": None}` and created no trial report, while
exit code 4 and `sandbox:exec:timeout` were already preserved.

## GREEN

Added a typed `FailureReportProjection` and exclusive JSON/HTML pair renderer.
The CLI now renders the failure pair after the exception path has unwound,
updates `attempt-result.json` only after the pair succeeds, emits the real
`report_json`/`report_html` paths, and keeps the original terminal evidence when
report writing fails. Reports are explicitly `execution_failure` with
`completed=false`; observations, journeys, and hardening are `unavailable`,
cleanup is `not_verified` unless structured cleanup failure is known, and only
bounded metadata is projected. Existing report files and symlink targets are
never overwritten.

## Verification

- `.venv/bin/pytest -q tests/unit/test_cli.py tests/unit/report/test_failure.py` — 55 passed.
- `.venv/bin/ruff check src/repotrial/cli.py src/repotrial/report/failure.py tests/unit/test_cli.py tests/unit/report/test_failure.py` — all checks passed.
- `.venv/bin/ruff format --check src/repotrial/cli.py src/repotrial/report/failure.py tests/unit/test_cli.py tests/unit/report/test_failure.py` — all formatted.
- `.venv/bin/mypy src/repotrial/cli.py src/repotrial/report/failure.py` — success.

No real Docker Sandbox or network/API execution was performed. Full repository
verification remains the controller's responsibility.

## Risks / follow-up

The failure report is execution evidence only and never a success result.
Cleanup and all unobserved run phases remain explicitly unverified/unavailable.

## Bounded Sol review correction

- RED: focused regressions failed because structured sandbox `details` were copied
  into attempt evidence and lifecycle cleanup errors remained unverified.
- GREEN: attempt evidence now projects only safe operation/reason and integer
  returncode; `CleanupError` and `RuntimeTemplateCleanupError` explicitly mark
  cleanup as failed. No arbitrary cleanup-string inference was added.
- Correction verification: focused CLI/report tests — 57 passed; Ruff check and
  format — passed; mypy `src/repotrial` — success.
