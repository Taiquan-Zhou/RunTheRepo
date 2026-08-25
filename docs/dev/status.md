# Development status

## M0.1 — project bootstrap

- Base commit: `3dbd5f59fd3f1bc5296778d25d26f8587fdaebac`
- Scope: Python src-layout, development dependencies, quality gates, CI, and package version smoke test.
- Implementation and fixes: `4f23a550ad35d08fce1a9062c8013c4e6a194052` (`chore: bootstrap repotrial project`), `79f80570dc8236b55f2d976d2dcaa77ef01290df`, and `d1675c99ca9cc91bd87ffb4c2cef0c038db17472`.
- RED evidence: recorded in the task report.
- GREEN and quality-gate evidence: recorded in the task report; focused smoke test and full local gates passed.
- Review: initial task review plus two scoped re-reviews closed all Critical/Important findings.
- Controller fresh verification at `d1675c9` used CPython 3.12.13 and passed locked sync/install, ordinary import, Ruff lint/format, mypy, pytest, 100% branch coverage, pre-commit, diff check, and clean status.
- Remote CI: GitHub Actions has not run from this worktree.
- Deviations/rulings: none recorded.

## M0.2 — core domain contracts

- Base commit: `dece310f502ae1ea40028e48060bcbb7f3667020`.
- Scope: Pydantic v2 enums and models for the frozen M0 domain/JSON contracts; no database, LangGraph, sandbox, or orchestration implementation.
- Implementation: `4c32487b018b6bb3a9891c80f993a0c2a1d1e3e2` (`feat: define core domain contracts`).
- RED/GREEN evidence: model tests first failed because the domain modules were absent, then the focused domain suite and applicable local quality gates passed.
- Review: the independent task review and scoped second review closed all Critical/Important findings.
- Remote CI: GitHub Actions has not run from this worktree.
- Deviations/rulings: none recorded.

## M0.3 — CLI skeleton and run layout

- Base commit: `4c32487b018b6bb3a9891c80f993a0c2a1d1e3e2`.
- Scope: `repotrial doctor` and `repotrial inspect --dry-run URL`; URL validation and empty run-directory creation only, with no clone, Compose, Docker, sandbox, agent, or hardening behavior.
- Implementation and bounded fixes: `eb991381c8f84b2253a6438eea43240aede1b538` (`feat: add CLI skeleton and run artifacts`), `2d4e4602aeff3d29236632a4bb269d1905e9d196`, and `8710f334097ff6da8539f36d370a4d179647fd16`.
- RED/GREEN evidence: CLI tests first failed because the command module was absent; focused CLI tests and all applicable local gates passed after implementation and each bounded fix.
- Review: independent review identified URL-boundary defects; two scoped fix/re-review rounds closed the then-known Critical/Important findings.
- Controller verification at `8710f33` used CPython 3.12.13 and passed locked dependency checks, CLI/unit tests, Ruff lint/format, strict mypy, branch coverage, pre-commit, diff check, and clean status. `doctor` left the injected artifacts root absent.
- Remote CI: intentionally not run; the repository owner deferred GitHub Actions and push until local development is complete.

## M0 milestone review remediation

- Base commit: `8710f334097ff6da8539f36d370a4d179647fd16`.
- A fresh whole-M0 review identified remaining URL path-component, documentation, and repository-hygiene gaps.
- Bounded remediation: `2aaff31983572b57b41c8a5994641b44cf69b641` (`fix: close M0 milestone review gaps`) adds CLI-level regression cases for literal/encoded dot segments and encoded separators, tightens only that URL boundary, updates M0 documentation, ignores root-level generated artifacts/build outputs, strengthens existing CLI side-effect assertions, and clarifies the M0/M2.1 Provider-plan boundary.
- RED evidence: the focused malformed-URL test collected 22 cases; the eight new dot-segment/encoded-separator cases failed because the CLI returned exit code 0 and created a run.
- GREEN and quality-gate evidence: the focused CLI suite passed 28 tests; the full suite passed 35 tests; Ruff lint/format, strict mypy, branch coverage at 98.09%, pre-commit, diff check, and ignore-rule checks passed locally.
- Independent scoped re-review and Controller final verification are required before this remediation or the M0 milestone can be accepted.
- Remote CI: intentionally not run, and no push was performed.
- M1 has not started.
