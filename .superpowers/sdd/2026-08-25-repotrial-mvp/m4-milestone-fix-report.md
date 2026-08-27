# M4 milestone bounded correction report

## Range

- Base: `080231037c9d9a35b549af9789b2a760c9178e7d`
- Head: this correction commit on `codex/m4.3-journey-planner` (its parent is the
  required base).

## Changed files

- `src/repotrial/trial/planner.py`
- `src/repotrial/journey/http_runner.py`
- `src/repotrial/journey/verifier.py`
- `tests/unit/trial/test_journey_planner.py`
- `tests/unit/journey/test_http_runner.py`
- This report.

No Playwright runner, SandboxProvider, dependency/lockfile, M5, or M6 files
were changed.

## Root cause and RED -> GREEN evidence

### I1 — current Runner consumability

Root cause: `_parse_journey()` parsed individual valid steps but did not apply a
Journey-level single-tool policy.

- RED:
  `uv run pytest -q tests/unit/trial/test_journey_planner.py -k 'mixed_tool or deterministic_success or browser_journey_uses'`
  produced four intended failures: declared mixed-tool Journey did not raise,
  model mixed-tool Journey materialized, and zero-step/assertion-free HTTP
  model Journeys materialized. The browser DSL preservation regression passed.
- GREEN: after adding the post-step single-tool and HTTP-success-condition
  checks in `_parse_journey()`, the same command reported `5 passed, 40
  deselected`.

The declaration regression verifies existing invalid-declaration behavior and
no model fallback. The model regression verifies an empty result.

### I2 — deterministic success condition

Root cause: Planner accepted empty or assertion-free HTTP Journeys, while the
HTTP runner had no Journey-level preflight and could return PASS for 0 steps or
an HTTP 500 with no assertion.

- RED:
  `uv run pytest -q tests/unit/journey/test_http_runner.py -k 'empty_journey_fails or assertion_free_http or evidence_directory_initialization or json_integer_exceeding'`
  produced the intended PASS-for-empty and PASS-for-assertion-free failures.
- GREEN:
  `uv run pytest -q tests/unit/journey/test_http_runner.py -k 'empty_journey_fails or assertion_free_http'`
  reported `2 passed, 66 deselected` after runner preflight began returning
  `journey:empty_steps` and `journey:missing_assertions` before evidence or
  network access.

Existing assertion-free fixtures that needed to reach a different pre-existing
validation, request, or evidence behavior were given a neutral status-200
assertion. Fixtures that deliberately test invalid trusted-origin precedence
and the multi-step later-invalid-assertion preflight remain unchanged.

### I3 — malformed bounded JSON

Root cause: `json.loads()` raises `ValueError` for an integer above Python's
digit-conversion limit, but `evaluate_assertion()` caught only
`JSONDecodeError`.

- RED: the focused command above raised the expected uncaught `ValueError` from
  `src/repotrial/journey/verifier.py` for a 5,000-digit integer (well below the
  65,536-byte acquisition bound).
- GREEN:
  `uv run pytest -q tests/unit/journey/test_http_runner.py -k 'json_integer_exceeding'`
  reported `1 passed, 67 deselected`; the real runner returns
  `step-0000:malformed_json`.

### I4 — evidence initialization separation

Root cause: `evidence_dir.mkdir()` was outside error handling, so an existing
regular file escaped as `FileExistsError` before the request loop.

- RED: the focused command above raised the expected `FileExistsError` at the
  evidence-directory creation line.
- GREEN:
  `uv run pytest -q tests/unit/journey/test_http_runner.py -k 'evidence_directory_initialization'`
  reported `1 passed, 67 deselected`. The runner returns FAIL with
  `failure_reason="journey:evidence_failure"`,
  `evidence_failure_reason="journey:evidence_initialization_failure"`, no
  evidence paths, and zero workload requests.

## Scoped and repository verification

- `uv run pytest -q tests/unit/trial/test_journey_planner.py tests/unit/journey/test_http_runner.py`
  → `113 passed in 1.15s`.
- `git diff --check` → no whitespace errors.
- `uv run ruff check .` → `All checks passed!`.
- `uv run ruff format --check .` → `46 files already formatted`.
- `uv run mypy src/repotrial` → `Success: no issues found in 21 source files`.
- `uv run pytest -q --cov=repotrial --cov-report=term-missing` → `606 passed,
  1 skipped in 68.02s`; total coverage `90.91%`, above the required `85%`.

## Self-review

- Confirmed the policy stays inside Planner parsing and HTTP runner preflight:
  no dispatcher, composite Journey, retries, shell/JS, or browser-isolation
  behavior was added.
- Confirmed Browser Journey steps remain validated by their frozen action DSL;
  the new browser regression has no `JourneyStep.assertions`.
- Confirmed only the documented verifier exception categories are normalized,
  and all existing assertion categories remain unchanged.
- Confirmed evidence initialization uses the existing `JourneyResult`
  workload/evidence failure-field separation and makes no request on failure.
- Confirmed test fixture updates only restore reachability of the original
  test's independent behavior under the new Journey-level contract.

## Concerns

None. The required separate reviewer was not dispatched because this brief
explicitly forbids spawning subagents; the implementer performed the recorded
scope and semantic self-review instead.
