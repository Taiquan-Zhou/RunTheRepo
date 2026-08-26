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
- A later Controller package-build gate found that the Hatch source distribution still included worktree-local `.git`, `.coverage`, and `.superpowers/sdd` content even though the wheel and installed `repotrial doctor` entry point were clean.
- The first bounded packaging remediation, `f785181d7a6d82a112a3111881ae22df033ff3bb` (`fix: exclude local state from source distribution`), added a target-specific exclusion blacklist and a real-archive regression test. Independent scoped re-review reproduced six unlisted paths in the sdist (`.env`, `.coverage.worker`, IDE state, and coverage reports), so its Important packaging finding remained open.
- The second bounded packaging remediation, `314098538a15cbd792b39b6603182e33bf2039de` (`fix: allowlist source distribution contents`), replaced the blacklist with a directory-level `only-include` allowlist for `src` and `tests`. Its expanded temporary-project regression excluded the known root-level local state while requiring every Python source/test plus `pyproject.toml`, `README.md`, and generated `PKG-INFO`. A second independent scoped re-review did not pass: it reproduced nested local-state leakage under those allowlisted trees (`src/repotrial.egg-info/PKG-INFO`, `src/repotrial/.env`, `src/repotrial/debug.log`, `tests/.coverage`, and `tests/.idea/workspace.xml`), so the Important packaging finding remained open.
- The final bounded packaging remediation, `7879de490cd9b5e84627f9b442cec8812bfd9cda` (`fix: restrict source distribution to Python files`), replaces the directory-level allowlist with file-level Git-style patterns that select only the current `src/**/*.py` and `tests/**/*.py` inputs. Its regression injects all five nested paths reproduced by the reviewer and requires them to remain absent while preserving all current Python source/tests and Hatch's mandatory `pyproject.toml`, `README.md`, and generated `PKG-INFO`. `uv.lock` and `.env.example` remain repository inputs rather than distribution inputs because neither is required to build or run the current package.
- Independent final scoped review at `7879de4` was APPROVED with no Critical, Important, or Minor findings: all 27 root/nested sentinel paths were absent from the sdist and none of the nine current Python source/test files was missing.
- Controller fresh final verification at `7879de4` used CPython 3.12.13 and passed `uv lock --check`, locked sync, Ruff lint/format, strict mypy, focused and full pytest runs with 36 tests passed, 98.09% branch coverage, pre-commit, git diff/fsck/ancestry checks, and secret/suppression/dependency/ignored-tracked hygiene checks. Default `uv build` explicitly built the wheel from the sdist; complete inspection found 13 sdist members and nine wheel members. An isolated wheel install ran `repotrial doctor` with output `ok` and created no `artifacts`; the tracked and untracked tree was clean afterward.
- Controller decision: the local M0 milestone gate is GO and accepted. Remote GitHub Actions remain intentionally unrun under the repository owner's decision, and no push was performed.
- `LICENSE` remains an owner decision required before public release, not an M0 implementation defect.
- Historical closure: after the M0 GO decision, M1.1 was authorized as the
  next bounded task.

## M1.1 — GitHub repository intake and immutable pinning

- Base commit: `9c360790812509edbe4998e45c82ab2634c5912c`.
- Implementation and bounded fixes: `24921720c29a3d798039f86d2eaf03944cc75639`,
  `a3380ac749046083df248bfbe6e6a78a76ff8581`, and
  `051a7a1c883a1672ada4f1cced71f1ad205aa848`.
- Scope: GitHub URL policy with CLI reuse; offline local Git clone and ref
  resolution into `PinnedRepo` with an immutable commit SHA. M1.2+ behavior
  was not implemented.
- TDD evidence: initial coverage recorded 32 RED cases followed by 32 GREEN
  cases. The implementation-review fixes added 13 RED cases and reached 74
  GREEN cases. The final communication `FileNotFoundError` regression also
  went RED then GREEN.
- Review: the independent revised design review was APPROVED. The implementation
  review required two bounded fix rounds and was then finally APPROVED with no
  Critical, Important, or Minor findings.
- Controller fresh evidence: `uv lock --check`, `uv sync --locked --all-groups`,
  focused tests (`75 passed`), Ruff lint/format, strict mypy, full tests
  (`83 passed`), branch coverage (`91.58%`, threshold `>=85%`), pre-commit,
  diff/range inspection, and clean status all passed.
- Git was available, so the local-Git integration tests ran without an
  `unsupported` skip. No real-GitHub E2E was run; the suite is deliberately
  offline. No push or remote GitHub Actions run was performed.
- Limitation/ruling: M1.1 guarantees direct Git-child kill/reap only, not
  descendant process-tree termination. On Windows, the standard library cannot
  make directory identity recheck and recursive path deletion atomic, so M1.1
  does not claim resistance to an adversarial same-host actor that swaps a
  destination directory between arbitrary Python instructions. Stronger host
  containment remains Sandbox-stage work.
- Historical transition: M1.2 was subsequently authorized as the next bounded task.

## M1.2 — Compose discovery and safe parsing

- Base commit: `a11b348ee1a3c1a1b0610c3865678641b979567b`.
- Implementation and bounded fixes: `a22b93f56da1f9b06fae69b0a10d270b4f140c4e`,
  `7395870d81d313de970e38e0402cbf55674198ff`, and
  `7c908f7d040fb838966532a9b280fc0759d7ab1d`.
- Scope: deterministic root-only Compose discovery, safe ruamel round-trip
  parsing, inert official tags only, typed canonical hash material, and
  resource/path-identity limits. No Compose execution, LLM, source mutation,
  or M1.3 behavior was implemented.
- TDD record: the original implementation report proved only two initial
  missing-module REDs and one mixed-key RED, not a behavior-focused RED for
  every original behavior. This historical gap is not repaired rhetorically.
  Fix round 1 added real RED/GREEN evidence for independently reproduced
  path, resource, quote, tag, and exception defects; fix round 2 added real
  RED/GREEN evidence for zero/unavailable-inode fail-closed behavior.
- Review: the initial independent review reported two Critical and four
  Important findings. The first fix re-review closed C2/I1/I2/I3 and accepted
  the I4 ledger, but retained one C1 zero-ID Critical. The second fix
  re-review was APPROVED with Critical, Important, and Minor findings all
  None.
- Controller fresh evidence at `7c908f7`: locked check/sync, 52 focused
  tests, 135 full tests, Ruff lint/format, strict mypy, 91.30% branch coverage
  (threshold `>=85%`), pre-commit, full diff/range/suppression checks, and a
  clean worktree all passed locally.
- Limitations/rulings: filesystems without a non-zero stable inode/file ID are
  unsupported and fail closed; no claim is made against mutation of the same
  inode's contents between checks. Canonical JSON is typed hash material, not
  source reserialization, and only tested root-level discovery behavior is
  claimed.
- No push or remote GitHub Actions run was performed. M1.3 remains unstarted
  and unauthorized in this run.
