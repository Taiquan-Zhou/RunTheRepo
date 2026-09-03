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
  `7c908f7d040fb838966532a9b280fc0759d7ab1d`, and
  `c0223b0ea758834febc13b49fd0af9e14d10bb10`.
- Scope: deterministic root-only Compose discovery, safe ruamel round-trip
  parsing, inert official tags only, typed canonical hash material, and
  resource/path-identity limits. No Compose execution, LLM, source mutation,
  or M1.3 behavior was implemented.
- TDD record: the original implementation report proved only two initial
  missing-module REDs and one mixed-key RED, not a behavior-focused RED for
  every original behavior. This historical gap is not repaired rhetorically.
  Fix round 1 added real RED/GREEN evidence for independently reproduced
  path, resource, quote, tag, and exception defects; fix round 2 added real
  RED/GREEN evidence for zero/unavailable-inode fail-closed behavior. Fix
  round 3 recorded a real RED where 129 collections reached the patched
  constructor before the fix, and NUL plus patched `lstat`/`resolve`/`iterdir`
  paths leaked `ValueError`; after the fix, 128 is accepted, 129 fails before
  `YAML.load`, active anchors use O(1) counts, and the typed discovery
  boundary is preserved.
- Review: the initial independent review reported two Critical and four
  Important findings. The first fix re-review closed C2/I1/I2/I3 and accepted
  the I4 ledger, but retained one C1 zero-ID Critical. The second fix
  re-review was APPROVED with Critical, Important, and Minor findings all
  None. An owner-requested fresh full audit after that earlier approval found
  one Critical (missing pre-construction collection-depth budget), one
  Important (raw discovery `ValueError` for invalid `pathlib` input), and no
  Minor findings. The original final auditor's two scoped follow-up attempts
  were blocked by the platform content filter and produced no verdict; a
  different fresh independent reviewer then completed scoped review and
  returned APPROVED with Critical, Important, and Minor findings all None.
- Controller fresh evidence at `c0223b0`: `uv lock --check`, 59 focused
  tests, 142 full tests, Ruff lint/format, strict mypy, 92.23% branch coverage
  (threshold `>=85%`), pre-commit, full diff/range/status checks, and a clean
  worktree all passed locally.
- Limitations/rulings: filesystems without a non-zero stable inode/file ID are
  unsupported and fail closed; no claim is made against mutation of the same
  inode's contents between checks. A maximum of 128 simultaneously open YAML
  mapping/sequence collections, including the root mapping, is enforced before
  `YAML.load`; this does not claim general parser resource immunity. Canonical
  JSON is typed hash material, not source reserialization, and only tested
  root-level discovery behavior is claimed.
- No push or remote GitHub Actions run was performed. In that M1.2 run, M1.3
  remained unstarted and unauthorized; the later M1.3 result is recorded below.

## M1.3 — deterministic baseline risk findings

- Base commit: `3ea153dc77b7b870faf59e1f4083ab6be3962935`.
- Implementation and bounded corrections: `79ceb66d0cbc38e8eddbb3a6bb192bd62cfcbdd5`,
  `ab02e3edb4e75503849f7f913cd7c60d14023190`,
  `3bc309cdd8d7c16ba350f898a1be76027a886ff2`,
  `815ab9fbdb7fb82a06be037163ffe8f9c426218d`,
  `ab1e7ccc8162bad71a4c8746535cf5b44c5656bc`, and the final bounded
  architecture closure `c88d86c6b8e76d06bcd4df6d712820277dd57183`.
- Scope: deterministic configuration-fact findings and unbounded aggregate
  ordering score only. M1.3 does not execute Compose, scan for vulnerabilities,
  invoke an LLM, mutate source input, or produce a safe/unsafe verdict.
- TDD record: the original implementation RED stopped at import collection and
  is retained as a disclosed process deviation. Later corrections recorded
  behavior-level RED/GREEN evidence. The final closure first reproduced the
  I2 ancestor-collapse cases and I1 `~\\` short/long mismatch; the I3 scanner
  regression used a disclosed controlled mutation because the production
  scanner behavior was already correct while the old test observed dead paths.
- Final architecture: pending evidence limits and materialized evidence values
  are disjoint result states; a materialized marker is retained as an ordinary
  child, while independent halt control stops only unvisited siblings after
  true node exhaustion. Preflight-wide subtrees remain local markers. The
  output evidence boundary is 48 actual containers including the evidence
  root, and nested traversal remains within the 10,000-work accounting bound.
- Independent final review at `c88d86c` returned `APPROVED`: I1, I2, and I3
  were each `CLOSED`, with no new load-bearing architecture finding.
  Independent reproductions covered short/long host-path parity, manual and
  parser-loaded depth preservation, nested/exhaustion accounting, and the live
  colon-rich scanner path.
- Controller fresh evidence at `c88d86c`: the I1 selector covering tilde
  backslash parity plus the short/long and source-target matrices (`40 passed`),
  focused I2 (`10 passed`), and focused I3 (`1 passed`) selections;
  `test_risk.py` (`79 passed`),
  Compose unit tests (`138 passed`), and full tests (`221 passed`); Ruff
  lint/format, strict mypy, 91.44% branch coverage (M1.3 risk module 90%),
  pre-commit, lock, diff, HEAD/scope, and clean-status checks passed locally.
- Evidence wording remains bounded: findings describe recognized configuration
  facts and truncated evidence explicitly; they do not establish that a target
  is safe, globally least-privileged, or free of unobserved risk.
- No push or remote GitHub Actions run was performed. M2.1 and later tasks were
  not started.

## M7.5 environment recovery — WSL2 Linux Docker Sandboxes

- Native Windows Docker Sandboxes remains historical evidence, not the active
  execution path. Stable v0.39.0 and nightly
  `v0.42.0-rc1-34-gc5fab2cd4` both retained the sandboxd AF_UNIX
  self-connect failure; neither result was rewritten or represented as fixed.
- The selected execution environment is the dedicated WSL2 distro
  `RepoTrial-Ubuntu`: Ubuntu `24.04.4 LTS`, kernel
  `6.6.87.2-microsoft-standard-WSL2`, systemd, accessible KVM, and an ext4
  checkout under `/home/repotrial/src/RepoTrial`. The distro is registered at
  `D:\DockerData\WSL\RepoTrial-Ubuntu`; the pre-existing `Ubuntu` and
  `docker-desktop` distributions were not used or modified.
- Calibrated execution HEAD:
  `760f73c15aba839a71ac036699b527334808b73f`. Calibration evidence commit:
  `6baf2e65e313c051e91d2062416262be7f7dd6f1`. This docs-only attestation does
  not and cannot identify its own future commit.
- Runtime-input fingerprint:
  `3598b61c3fca786bfcf9b81f9e5d030ea38613f13736ecb92bd5d14d7feabcff`;
  manifest SHA-256:
  `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`;
  environment fingerprint:
  `521b50be7c0ca064799f1f0f7e1cdd478fddc765d9ab6d5f8c49d2e26d13b005`.
- Official Linux SBX v0.39.0 diagnose completed `12 pass / 0 warn / 0 fail /
  0 skip`. Trusted real calibration passed `3 passed in 170.66s`; the opt-in
  Provider smoke passed `1 passed in 21.94s`; every lifecycle ended with exact
  empty inventory and no host Docker fallback.
- Linux quality gates at the calibrated HEAD: Ruff lint PASS; Ruff format
  `90 files already formatted`; mypy PASS for 39 source files; full pytest
  `1178 passed, 5 skipped, 1 warning`; branch coverage `88.78%` against the
  required `85%`; pre-commit and `git diff --check` PASS.
- A client-process proxy A/B first reproduced Docker JWKS timeout without
  `HTTP_PROXY`/`HTTPS_PROXY`, while the same Rule-mode route passed through the
  approved LAN proxy. With explicit trusted-client proxy variables and
  `NO_PROXY=localhost,127.0.0.1`, diagnose and the complete calibration passed.
  This client layer remains independent from Docker-official `proxy.daemon`
  and `proxy.sandbox` settings.
- Calibrated safety evidence covers CPU, memory, the exact three-part disk
  budget, shared host-side monotonic whole-trial duration, fail-closed network
  policy, cleanup, and no-host-fallback. PID hard bound remains explicitly
  unsupported and is not claimed or emulated.
- Environment recovery now hands execution to existing Task 2 of the M7.5
  compatibility-recovery plan. The frozen manifest, repository order, SHAs,
  thresholds, and Boot/Recovery/Journey/experiment semantics are unchanged.

## M7.5 final compatibility canary — FAILED

- Frozen execution HEAD:
  `bf62cc72ca3b77e305312f4cc54ff88d49e2bbff`; manifest SHA-256:
  `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`.
- Final pre-canary gates: full pytest `1256 passed, 5 skipped`; branch coverage
  `88.61%`; Ruff lint/format, mypy, pre-commit, and diff-check PASS; real SBX
  basic/primary/timeout/cancellation `4 passed`; independent review APPROVED.
- Umami `b08a3643-4390-4378-b378-a78686ba2ac2`: exact SHA; PostgreSQL disk
  exhaustion evidence; terminal
  `sandbox:clone_verification:total_duration_exhausted`; no completed report or
  passing required Journey set; cleanup PASS and final inventory empty.
- Listmonk `cc0cab03-8ca6-41ce-b359-9d5000bc15b5`: exact SHA; completed report
  with `journeys=[]` (`0/0`); terminal `boot_recovery_stopped`; cleanup PASS and
  final inventory empty.
- changedetection.io `655948c0-d46f-473a-b232-b400768954d5`: exact SHA; Boot
  PASS followed by `ObservationCollectionError`; terminal
  `internal:observationcollectionerror`; no completed report or passing
  required Journey set; cleanup PASS and final inventory empty.
- Canary result: `0/3` passed against the required `2/3`. Repositories #4-#10
  were not run. README, manifest, production code, frozen contracts, and the
  known `pid_hard_bound_unsupported` limitation were not changed.

**M7.5 COMPATIBILITY CANARY FAILED**

## M7.5 Task 2 - RG1 Docker-data capacity calibration

- Controller completed the bounded RG1 diagnostic at `4096 MiB` and
  `8192 MiB` for the exact Uptime Kuma and n8n pinned repositories, strictly
  serially, using the reviewed diagnostic runner.
- Uptime Kuma booted at both capacities but had insufficient Journey
  coverage. n8n retained Docker-data `no space left on device` during 4096 MiB
  extraction and timed out during the 8192 MiB bounded run. All four runs
  cleaned up successfully and ended with empty sandbox inventory.
- Evidence and runner commit: `032e4d9` (`docs: record M7.5 RG1 disk capacity
  calibration`).
- Gate: `RG1_CAPACITY_GATE=FAIL no_common_bounded_total_through_8192`.
  The default `2048 MiB` policy and public CLI/API remain unchanged; no RG1
  policy design was created. Implementer/reviewer relay failures are recorded
  as process deviations, not as task approval.

## M7.5 recovered metric-bearing canary — latest status

This section supersedes the older `M7.5 environment recovery — WSL2 Linux Docker
Sandboxes` wording that described WSL as active and native Windows as blocked, and
supersedes the old final-canary status only for current status reporting. All
preceding M7.5 recovery and canary records remain historical evidence and are
retained.

- Unified execution HEAD: `ddea7444f3f5ab8fbdcfc4ed93a41a433f6f48b3`.
- Manifest SHA-256: `b050f849802fcf5cb8d035f12d32a063b56b93d080cb580a4ebae852c7b075fd`.
- Current topology: Windows-hosted official `sbx.exe` CLI/daemon v0.39.0 at
  `C:\Users\zztq\AppData\Local\DockerSandboxes\bin\sbx.exe`, invoked as
  `sbx.exe daemon start`; `sbx diagnose` was `12/12` PASS and
  `WHvCapabilityCodeHypervisorPresent=true`. Target workloads ran in disposable
  Linux sandboxes. The WSL2 setup is historical/alternative evidence, not a
  current canary prerequisite.
- Model: `qwen3:4b-instruct`, digest
  `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`.
- Pre-fix Umami run: `d8afc2ad-8c22-43eb-b25f-8d502baa5749` was a
  `superseded metric-bearing attempt on the previous HEAD, retained as audit
  evidence`. It completed Boot and entered observer before the generic observer
  fix `ddea7444f3f5ab8fbdcfc4ed93a41a433f6f48b3`, which preserves multi-word
  `docker top` command fields. It remains history and is not included in the
  current metric-bearing set.
- Current metric runs: Umami
  `1bb45765-2501-4091-be35-16f1a4fd2aa0`, Listmonk
  `ed8d3db3-950b-4321-96c2-ade4e3eef241`, and changedetection.io
  `54b05ce1-1e86-4726-8a0a-4a93c755cead`.
- Umami: exact SHA, Boot, observer, JSON/HTML report, and cleanup PASS; model
  `policy_rejected`; `0/0` Journeys; final `insufficient_coverage`.
- Listmonk: exact SHA, Boot, observer, JSON/HTML report, and cleanup PASS;
  observer recorded 9 processes; model `policy_rejected`; `0/0` Journeys; final
  `insufficient_coverage`.
- changedetection.io: exact SHA, create, and cleanup PASS; model call succeeded
  with an accepted empty Journey set (`0` Journeys); Compose `up` timed out, Boot
  final verdict was `null`, and observer/report did not run; stop reason
  `sandbox:exec:timeout`.
- Canary result: `0/3 < 2/3`; repositories #4–#10 were not run. Runtime progress
  was `2/3` healthy Boot + observer + report and `3/3` exact SHA + cleanup.
  Product functionality remains below the M7.5 release gate.
- Gates: observer WSL `89 passed`; full coverage run `1362 passed, 2
  coverage-instrumentation timeout failures, 6 skipped`, branch coverage
  `86.97%`; the exact two timeout tests passed without coverage instrumentation;
  pre-commit PASS; independent review APPROVED. The full coverage run is not
  described as fully green.
- Docker Sandboxes v0.39.0 remains `pid_hard_bound_unsupported`; no PID hard
  protection is claimed. No host fallback was used.

**M7.5 PILOT COMPLETE — MVP LIMITATIONS IDENTIFIED**

## DeepSeek portable fallback canary — latest status

This section supersedes only the current status. Historical Qwen and other
canary records remain retained and are not deleted or overwritten.

- Unified execution HEAD: `5f7faa3949c74d3458e8651b9f399d5741e556c0`.
- Manifest SHA-256: `b050f849802fcf5cb8d035f12d32a063b56b93d080cb580a4ebae852c7b075fd`.
- DeepSeek endpoint class/model: public OpenAI-compatible / `deepseek-v4-flash`.
  No account or API key is recorded in this evidence.
- Strict/portable calibration evidence remains in
  [`pilot-evidence/m7.5-deepseek-portability-calibration.md`](pilot-evidence/m7.5-deepseek-portability-calibration.md).
- Qwen and private Journey schema work remain deferred. Docker Sandboxes PID
  hard bound remains unsupported. No host fallback was used.

| Repository | Run ID | Exact SHA | Duration | Outcome | Stop reason | Journeys | Experiments | Report | Cleanup / inventory |
|---|---|---|---:|---|---|---|---|---|---|
| Umami | `3467cef1-fdbf-4897-bf67-acd2d8e95fc8` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` exact | `404.375s` | `execution_unsupported` | `experiment:sandbox_failed` | `4` (`3 PASS` / `1 UNSUPPORTED`) | `3` (`2 boot_regression` rollback, `1 sandbox_failed` stop) | JSON/HTML present | all created sandboxes `destroy_success`; final inventory empty |
| Listmonk | `26f65e30-74db-446e-a92f-e9d81aacea4b` | `670c01717d48647093335cc23a6be6f4b79c3b6b` exact | `207.484s` | `trial_failed` | `insufficient_coverage` | `0/0` | `0` | JSON/HTML present | `destroy_success`; inventory empty |
| changedetection.io | `5055f71a-af13-4b29-9d3c-caaaa76f4734` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` exact | `218.125s` | `execution_unsupported` | `insufficient_coverage` | `5` (`0 PASS` / `3 FAIL` / `2 UNSUPPORTED`) | `0` | JSON/HTML present | `destroy_success`; inventory empty |

Listmonk model terminal outcome was `planner_timeout`; changedetection.io model
terminal outcome was `success`. Production code, tests, manifest, and frozen
contracts are unchanged. The canary result is `0/3 < 2/3`; repositories #4–#10
were not authorized.

**M7.5 DEEPSEEK CANARY — FAIL**

## M7.5 DeepSeek canary at frozen HEAD `e2ce00222380d06834075481e7c67f026b91af55`

- Latest metric-bearing canary HEAD: `e2ce00222380d06834075481e7c67f026b91af55`.
- Manifest SHA-256: `b050f849802fcf5cb8d035f12d32a063b56b93d080cb580a4ebae852c7b075fd`.
- Model: `deepseek-v4-flash`.
- Earlier attempts remain retained as superseded, audit-only evidence; the
  three runs below are the current metric-bearing set.
- Umami `1b05caa6-c7df-48c3-9a8c-510cd1dd8a00`: exact SHA
  `ca661c7057984aa98ed4f7083d84dae2f65bfcb0`; `339.984s`; exit `4`,
  `exception` (`DockerSbxError`); model success in `129.313s`; 5 persisted
  HTTP Journeys; Boot `up` timed out; no report; stop reason
  `sandbox:exec:timeout`; `destroy_success`; final inventory empty.
- Listmonk `697ebd1f-303b-47e2-9f3e-0318886454ee`: exact SHA
  `670c01717d48647093335cc23a6be6f4b79c3b6b`; `265.641s`; exit `4`,
  `exception` (`DockerSbxError`); model success in `89.657s`; 1 persisted
  HTTP Journey; Boot `up` timed out; no report; stop reason
  `sandbox:exec:timeout`; `destroy_success`; final inventory empty.
- changedetection.io `ed91fab9-53d9-4d48-b477-78c979385236`: exact SHA
  `5d9c7c6da76340597243e8163c4f2439237fa0e8`; `284.391s`; exit `3`,
  `trial_failed`; model success in `108.172s`; 5 persisted HTTP Journeys, all
  failed with `network_error`; Boot PASS and service running; JSON/HTML report
  produced; stop reason `insufficient_coverage`; `destroy_success`; final
  inventory empty.
- The changedetection.io Compose file binds `127.0.0.1:5000:5000`, and the
  current SBX publish path could not reach that loopback-bound service. This is
  an evidence-supported compatibility-topology finding for this run, not a
  general Compose/SBX theorem.
- Canary result: `0/3 < 2/3`; repositories #4–#10 were not authorized or run.
- PID hard bound remains unsupported; no host fallback was used. HEAD and
  manifest remained unchanged, and the source/tests/manifest were clean before
  this documentation-only append; no sandbox remained. The current
  uncommitted changes are limited to these two documents. README was not
  updated.
- Pre-canary gates: 60 focused passed; Linux full suite `1380 passed, 6
  skipped`; coverage `86.92%`; Ruff/format, mypy, pre-commit, diff-check, and
  independent review PASS.

**M7.5 DEEPSEEK CANARY — FAIL (`0/3 < 2/3`)**

## M7.5 final unified DeepSeek cohort — current status

- Final execution HEAD:
  `7f1f401bc749f9bcce8e4bbc9287071ccc3a758c`; Linux manifest SHA-256:
  `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`.
  The Windows checkout byte hash `b050f849...` differs only by CRLF/LF checkout
  normalization.
- Model: public OpenAI-compatible `deepseek-v4-flash`; no credential is recorded.
- Fresh gates: planner focused `103 passed`; Linux full suite `1423 passed, 6
  skipped`; coverage `87.05%`; Ruff lint/format, mypy, pre-commit, diff-check,
  and scoped independent review PASS; SBX diagnose `12/12` PASS.
- Audit-only pre-workload Umami run
  `66ea294d-00b9-4713-b4c1-733cbb41db85` retained a client-proxy/JWKS
  infrastructure invalidation and `internal:cleanuperror`. Explicit client
  proxy probes and the trusted real SBX smoke then passed before the authorized
  replacement.
- Final canary metric runs: Umami
  `d2318902-3810-4f13-aca1-1faa2e099615` (`1/1` Journey PASS), Listmonk
  `974b0f7a-b4da-4f2b-8fe0-7f9ccc4b6ecd` (`1/1` PASS), and
  changedetection.io `05ea56c3-3ebf-4127-800f-287e60787def` (`0/3` PASS).
  These frozen repositories #1-#3 were the entire canary denominator; Dockge
  and repositories #4-#10 were not canary entries. The compatibility canary
  was `2/3 PASS`, authorizing repositories #4-#10 on the unchanged HEAD.
- Complete-cohort autonomous success is `1/10`: only Dockge
  `f60e15f4-7171-4edb-b641-33ad209ff9c0` completed with exit `0`, a non-empty
  all-PASS Journey set, report, exact SHA, and successful cleanup.
- Meaningful convergence is `0/10`; no experiment produced KEEP. P50/P95 are
  `UNKNOWN` because Wakapi ended at `intake:clone` before a metric-bearing
  target workload and therefore has no repository duration under the frozen
  rule.
- Dominant observed limitations: three frozen Docker-disk exhaustion failures
  (Uptime Kuma, n8n-hosting, NetBox Docker); Linkding's documented but unapplied
  `.env` setup; changedetection.io guest-loopback publication; Paperless-ngx
  guest clone status mismatch; Wakapi clone intake; and later experiment
  sandbox failures for Umami/Listmonk.
- Metric-attempt sandbox cleanup obligations were `13/13` successful and final
  inventory was empty after every metric attempt. The final-HEAD chain aggregate
  is nevertheless `13/14 = 92.86%`, below the required `100%`, because the
  audit-only pre-workload lifecycle contains `cleanup_retry_failure`; later
  empty inventory does not override the frozen lifecycle rule. Paperless-ngx
  is `N/A` under that same rule because its lifecycle ends at `create_failure`
  without an owned sandbox ID; it is not omitted from the denominator.
- Autonomous success `1/10 < 7/10`, convergence `0/10 < 5/10`, and aggregate
  cleanup `92.86% < 100%`; the M7.5 release gate fails. README was not changed.
  PID hard bound remains unsupported and disclosed; no host fallback was used.

**M7.5 PILOT COMPLETE — MVP LIMITATIONS IDENTIFIED**

## M7.5 post-pilot failure diagnostic — current recovery status

- Diagnostic execution HEAD:
  `8d6982f45a0ddcb4dbe68c89e51ba3d77eb1c6a0`; manifest SHA-256:
  `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`.
- Four fixed, strictly serial diagnostic attempts completed: Umami
  `34c99596-e85b-468e-83ff-cebe5c47ba2d`, Listmonk
  `d080dc08-6eb9-4ddf-b9ce-941d8777db4d`, Paperless-ngx
  `da95f905-5019-4c04-a1de-f7a534343fbf`, and Wakapi
  `6f5c6a71-5740-4e35-aa00-23b4af821a24`.
- Umami and Listmonk independently exhausted the shared monotonic 300-second
  total-duration budget. Enforcement passed. Provider-internal,
  deadline-independent force-remove semantics plus the queue controller's exact
  post-run `sbx list` captures prove cleanup and empty inventory; total-duration
  resource calibration is the next bounded task.
- Fixed-UID correction is not yet proven across two repositories. Paperless-ngx
  clone diagnostics were truncated/malformed, so a generic clone correction is
  also not yet proven. Wakapi ended at a separate per-command Boot exec timeout.
- All four attempts used their exact manifest SHA, retained their failure
  outcomes, and ended with exact-empty official sandbox inventory. Every command
  explicitly selected `docker-sbx`; the CLI has no host Docker provider/fallback
  branch. PID hard bound remains unsupported and disclosed.
- Full evidence and gate rulings are recorded in
  [`pilot-evidence/m7.5-post-pilot-failure-diagnostic.md`](pilot-evidence/m7.5-post-pilot-failure-diagnostic.md).
- These attempts are audit diagnostics, not replacement metric-bearing cohort
  results. Existing M7.5 history remains retained.

**M7.5 RECOVERY — TOTAL-DURATION CALIBRATION PROVEN**

## M7.5 final 900-second mutation-grounding cohort — current status

- Current execution HEAD:
  `9f5d503b83da4818335228539c5de1fe40b759df`; manifest SHA-256:
  `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`.
- The build includes the verified 900-second host monotonic whole-trial default
  and generic `root_user_possible -> DROP_ALL_CAPS` mutation grounding. Frozen
  architecture, manifest, Disk Bound, PID disclosure, and no-host-fallback
  semantics were unchanged.
- Fresh gates before execution: Linux full suite `1519 passed, 6 skipped`;
  coverage `87.30%`; Ruff lint/format, strict mypy, pre-commit, diff-check,
  focused mutation tests, and independent review PASS; SBX diagnose `12/12`
  PASS with exact empty inventory.
- The frozen three-repository canary passed `2/3`: Umami
  `dfc8e110-fdd0-4cc3-a3fa-d37a2e241954` and Listmonk
  `3c564561-5acf-428a-b5b8-76a0ddad40b6` had non-empty all-PASS required
  Journeys; changedetection.io
  `2630f55c-bf7a-4ce5-adfe-f559eadf5d20` retained five `network_error`
  Journey failures. The failed result remained in the denominator.
- Repositories #4-#10 then ran serially on the same unchanged HEAD. The final
  cohort autonomous-success result is `3/10`: Umami, Listmonk, and Dockge
  `87bd771f-0c80-418e-b224-1048650d466c`. Meaningful convergence is `0/10`;
  all nine experiments rolled back and none produced KEEP. Each of the three
  autonomous successes retained process exit code `0`, terminal `completed`,
  and an existing completed JSON report.
- Other exact terminal causes: Uptime Kuma and n8n-hosting Docker-data disk
  exhaustion; Linkding missing documented `.env`; NetBox PostgreSQL recovery
  timing; Wakapi per-command `sandbox:exec:timeout`; Paperless-ngx fail-closed
  malformed/truncated guest clone-status verification.
- Exact SHA verification passed `10/10`. Every attempt retained an exact stop
  reason; failed-attempt coverage is `7/7 = 100%`. Final-epoch cleanup was
  `19/19 = 100%`; every final inventory was exactly empty with empty stderr.
- P50/P95 remain `UNKNOWN` because Paperless-ngx did not cross the target
  workload boundary. No run exhausted the 900-second whole-trial deadline.
- Release thresholds fail: autonomous success `3/10 < 7/10` and meaningful
  convergence `0/10 < 5/10`. README was not changed. PID hard bound remains
  unsupported and disclosed; no host fallback was used.

Full current-epoch evidence and run IDs are recorded in
[`pilot-report.md`](pilot-report.md) and
[`pilot-evidence/m7.5-compatibility-diagnostic.md`](pilot-evidence/m7.5-compatibility-diagnostic.md).

## M7.5 release-gap closure - Task 1 status

- Implementation base: `b19dd13f632255caeee7338d773f84a4b3eef97e`; clean ext4 worktree established at `/home/repotrial/src/RepoTrial-m7.5-release-gap`.
- Result: `ENVIRONMENT_BLOCKED`. Ubuntu/WSL/ext4/SBX versions and frozen manifest hash match, and official SBX inventory is empty; locked quality gates pass except Ruff format on the approved plan Markdown, and trusted calibration returned `2 passed / 1 failed`.
- `uv 0.11.5` was taken from the task-local Linux tool path; both model variables were SET. No real repositories were run, and no Windows `.venv`, target Compose, or production code was touched.
- Exact non-secret fingerprints and the blocking command matrix are recorded in [`pilot-evidence/m7.5-release-gap-closure.md`](pilot-evidence/m7.5-release-gap-closure.md).

**M7.5 PILOT COMPLETE — MVP LIMITATIONS IDENTIFIED**

## M7.5 release-gap closure - Task 1 corrected rerun/ruling (append-only)

- task_id: `Task 1`; BASE_SHA:
  `b19dd13f632255caeee7338d773f84a4b3eef97e`; HEAD_SHA:
  `ece0b206dd42151b1c84c07a7177c3cfa92bb78b`; corrected rerun parent:
  `ece0b206dd42151b1c84c07a7177c3cfa92bb78b`.
- changed_files: evidence ledger, this status append, and ignored Task 1 report
  artifact only; production source, tests, manifest, README, target Compose,
  and real repositories unchanged.
- Result remains `ENVIRONMENT_BLOCKED`. Python `/usr/bin/python3.12` is
  `3.12.3`; guest Compose fingerprint is `v5.5.0`; immutable global network
  policy fingerprint is
  `e745ff8da1066eba64b0bbf7865ff91a308b0b216f81c5c375091e0a13b46d8b`.
  Current `free -m` is Mem `7804/724/6794/3/480/7079`, Swap
  `2048/0/2048`; current state-volume `df -Pm` is `/dev/sdd`, total `120380`,
  used `7430`, available `106791`, `7%` for `/`, `/home/repotrial`, and
  `/home/repotrial/.local/state/sandboxes`.
- Full gates: locked sync, Ruff lint, mypy, adjusted-PATH pytest
  (`1519 passed, 6 skipped, 1 warning`), and adjusted-PATH branch coverage
  (`87.29%`) passed; Ruff format failed on approved plan lines `274-307`;
  initial default-PATH pytest failed to spawn `uv` (`1518 passed, 6 skipped,
  1 failed`, exit `1`).
- SBX/model: `sbx version` and current read-only diagnose (`12 passed`) passed;
  `sbx list` was exactly empty; model variables were SET with
  `public OpenAI-compatible` / `deepseek-v4-flash`; no secrets or daemon
  lifecycle mutation.
- Marker ruling: brief `REPOTRIAL_RUN_REAL_SBX=1` produced `3 skipped in
  0.03s` (exit `0`), while code marker
  `REPOTRIAL_RUN_WSL2_SBX_CALIBRATION=1` produced `1 failed, 2 passed in
  397.58s` (exit `1`), failing
  `test_wsl2_linux_sbx_timeout_calibration` with
  `clone_verification failed: total_duration_exhausted` and a Docker Hub
  refresh-lock deadline warning.
- Review disposition: Important evidence omissions are addressed in the
  append-only corrected report and ledger; historical attempts remain intact.
  Known limitations and exact controller ruling are recorded in the Task 1
  artifacts. Controller ruling: preserve evidence and stop before real
  repositories; no release-readiness claim.

## M7.5 release-gap closure - Task 1 proxy-causal rerun (append-only)

- Parent HEAD `5c8265987fc85613f0247755b32ede3a5371570f`; production files
  unchanged.
- Root cause confirmed: host `sbx` Docker Hub refresh/network calls were
  launched without the configured WSL proxy, causing refresh-lock waits and
  consuming the calibration total-duration budget. Direct Docker Hub access
  timed out without proxy; `172.24.64.1:7890` returned expected responses.
- With `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` set to
  `http://172.24.64.1:7890`, primary calibration passed (`1 passed, 2
  deselected, 115.61s`) and the complete calibration passed (`3 passed,
  270.40s`). SBX daemon remained healthy and inventory empty.
- Controller ruling: retain the earlier no-proxy failure as historical
  evidence; supported WSL calibration/canary execution must export the
  configured proxy. No production retry/proxy behavior was added.

## M7.5 release-gap closure - Task 1 final gate ruling (append-only)

- Final reviewed controller HEAD: `ebaa984418addeeea55875308f9913d57fd293bb`.
- Fresh Linux quality gates passed: locked sync, Ruff lint/format, mypy, and
  full pytest (`1519 passed, 6 skipped, 1 warning`). SBX diagnose passed and
  inventory is empty.
- Proxy-backed trusted calibration passed all three tests (`3 passed in
  270.40s`).
- Independent review service returned HTTP `502/503` three times before a
  report was produced; no reviewer success is claimed. Controller diff review
  found no Critical/Important issue in the evidence-only append.
- Ruling: Task 1 is accepted for execution under the explicit proxy-backed
  WSL environment; the review-service outage remains a process deviation and
  must be revisited before final integration. Continue to Task 2 without
  production code changes.
