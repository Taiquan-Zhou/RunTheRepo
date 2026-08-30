# M7.5 real-repository Pilot protocol and report

> **POST-FIX UNIFIED COHORT COMPLETE — MVP limitations identified**

This document freezes the M7.5 execution protocol and records the append-only Pilot
evidence. Infrastructure-invalidated attempts are retained below; no repository
metric is claimed until the target-workload boundary is actually crossed.

Sections 5–11 retain the first cohort as the append-only **pre-fix baseline**.
They were superseded after the generic network-policy fix
`d4c2a629b602fbd7128fc3ab6a4c2d5c81eb5d2d` and must not be used as the final
M7.5 metric set. The root-cause proof, fix verification, and complete final cohort
from that single unified HEAD are recorded in
[`pilot-evidence/m7.5-boot-root-cause.md`](pilot-evidence/m7.5-boot-root-cause.md).
The frozen manifest, repository order, and pinned SHAs were not changed.

## 1. Immutable cohort

- Manifest: `eval/real_repos.yaml`, schema version `1`, frozen `2026-08-29`.
- Cohort identity is the ordered tuple of each manifest `url` and `commit_sha`.
- Manifest list order is execution order and the only order used by the tables.
- All ten repositories stay in every denominator. A repository cannot be dropped,
  replaced, or reordered after any attempt or result is observed.
- Repo-specific workarounds are prohibited. A later generic product change must be
  reviewed and tested outside this preparation task before a new Pilot is frozen.
- Each repository receives exactly one primary attempt. Every command and attempt
  is retained in an append-only attempt ledger and is never erased or silently
  reclassified.

README remains unchanged until real Pilot metrics exist.

### Frozen attempt attribution

Attempt attribution is decided by observed execution boundaries, not by a later
interpretation of results:

1. Before invocation, label the first attempt `primary`. A retry authorized after
   a pre-workload infrastructure invalidation is `replacement`. Any attempt after
   a metric-bearing attempt exists is `diagnostic`.
2. An attempt invalidated by infrastructure before any target workload starts is
   `audit-only`. Retain all terminal evidence, but exclude it from repository
   outcome, convergence, experiment-count, failure-class, and timing attribution.
   It still contributes every failed terminal state to the exact-stop-reason gate
   and every owned sandbox obligation to the aggregate cleanup gate. A replacement
   requires explicit invalidation evidence and authorization.
3. The first attempt that starts any target workload is irrevocably the
   repository's sole metric-bearing attempt, whether its role is `primary` or
   `replacement`. Once assigned, the metric-bearing attempt ID never changes.
4. Every later attempt is `diagnostic-only`; retain its complete evidence. It
   cannot alter repository outcome, convergence, experiment-count, failure-class,
   or timing attribution, but its failed terminal state and owned sandbox cleanup
   obligations remain in the all-attempt safety gates.
5. A pre-workload terminal attempt without verified infrastructure invalidation is
   not replaceable under this protocol. If no metric-bearing attempt exists, the
   repository is an autonomous failure, its experiment count is `0`, it has no
   meaningful convergence, and its repository duration is missing. That missing
   duration makes P50 and P95 `UNKNOWN`.
6. Repository-level outcome, convergence, experiment, failure-class, and timing
   rows and aggregates derive mechanically from the sole metric-bearing attempt,
   or from the fixed no-metric-bearing defaults above. Exact-stop-reason coverage
   and aggregate cleanup instead derive from every retained attempt.

## 2. Fail-closed pre-execution gates

Execution is prohibited until every gate below is independently verified as
`PASS` against the exact build and environment selected for the Pilot. `TBD`,
`UNKNOWN`, `UNSUPPORTED`, or missing evidence keeps the gate closed.

| Gate | Required proof before any target workload | Preparation status |
|---|---|---|
| PID hard bound | SBX v0.39.0 cannot prove a PID hard bound; `pid_hard_bound_unsupported` is retained in every attempt result. | OWNER-ACCEPTED KNOWN UNSUPPORTED LIMITATION — not a Pilot blocker |
| Sandbox health | Current provider health and required isolation capabilities pass immediately before execution. | PASS — `sbx diagnose` reported `12/12` immediately before metric execution; daemon was not restarted/reset |
| Immutable repository identity | `--commit-sha` accepts only a lowercase full SHA, generic intake checks it out detached, verifies `HEAD^{commit}`, and persists expected/actual SHA in `attempt-result.json`. | IMPLEMENTED — verify against every exact Pilot attempt |
| Exact failure reason | Deterministic report JSON includes `RunState.stop_reason`; terminal `attempt-result.json` retains it for graph terminal results and a sanitized reason for exceptions. | IMPLEMENTED — verify against every exact Pilot attempt |
| Resource bounds | Effective CPU, memory, disk, and whole-duration limits are all verified; PID is the separately disclosed unsupported limitation. | PASS before execution — integer CPU compatibility, memory, official disk env allocation, and host monotonic deadline were verified; PID limitation remained disclosed |
| Forced cleanup | Cancellation, timeout, normal completion, and partial-create paths prove forced cleanup and auditable lifecycle evidence. | PASS before execution; observed all-attempt aggregate later finished at `90%`, below the Pilot safety target |
| No host fallback | Provider/tool routing proves that no target command or Compose workload can fall back to the host. | PASS — execution used only `DockerSbxProvider`; no host fallback route was enabled |

### Current preparation findings (execution gates, not Pilot outcomes)

1. SBX v0.39.0 PID hard-bound enforcement is an Owner-accepted known unsupported
   limitation. It is disclosed as `pid_hard_bound_unsupported`; this Pilot does
   not claim fork-bomb protection and PID is not an execution blocker.
2. `repotrial inspect --commit-sha <lowercase-full-sha>` is the generic immutable
   repository route. Generic intake performs a detached checkout and verifies
   `HEAD^{commit}` before baseline/workload; `attempt-result.json` records both
   expected and actual SHA. Each Pilot attempt must independently verify it.
3. `trial-report.json` projects the exact graph-terminal `RunState.stop_reason`.
   `attempt-result.json` is persisted for each non-dry-run terminal attempt and
   retains the raw graph stop reason or a deterministic sanitized exception reason.
   Each Pilot attempt must independently verify the evidence.

## 3. Execution protocol

For each manifest entry, in order:

1. Confirm all pre-execution gates are `PASS` and archive their evidence.
2. Allocate an attempt ID, assign its `primary`/`replacement`/`diagnostic` role,
   start an external monotonic timer, and record UTC start time before invocation.
   Record the exact command verbatim, including the approved generic
   `--commit-sha`, `--container-port`, and optional `--compose-path` inputs.
3. Through the approved generic intake route, verify the checked-out
   `HEAD^{commit}` equals the manifest SHA before any target workload executes.
   A mismatch terminates the attempt and is classified as `intake`.
4. Record `target_workload_started=false` until the controlled provider boundary
   first starts target workload execution. At that boundary, set it to `true` and,
   if no metric-bearing attempt exists, bind this attempt ID irrevocably as the
   repository's sole metric-bearing attempt.
5. Run only through `SandboxProvider`; never execute target Compose on the host and
   never fall back to a host command path.
6. Persist report paths, artifact references, deterministic baseline journey
   definitions/results, every experiment/mutation, and every lifecycle JSONL file.
7. For every attempt, record actual SHA, UTC end, external monotonic duration,
   exact command, exit code, terminal outcome, exact raw `stop_reason`, failure
   class, lifecycle/cleanup evidence, and any invalidation evidence/authorization.
8. Derive cleanup only from lifecycle JSONL under the rules in section 6. Preserve
   raw unsupported/not-observed evidence; never convert it to success.
9. Stop the attempt timer only after required cleanup has reached a terminal state.
   Apply the frozen attribution rules without a free-form effect-on-metrics decision.

The exact command for every retained attempt is recorded in sections 5 and 10.
Metric-bearing execution used RepoTrial HEAD
`8766f818fe2d4920b7e3cdc95247fe903b6d0a83`; the manifest and pinned SHAs were
unchanged throughout the cohort.

## 4. Outcome definitions

### Autonomous success

Evaluate autonomous success only from the repository's sole metric-bearing
attempt. With no metric-bearing attempt, the repository is an autonomous failure.
A repository is an autonomous success only when all of the following are true:

- terminal outcome is `completed` and the captured process exit code is `0`;
- `trial-report.json` exists (with `trial-report.html` captured when produced);
- report/intake identity SHA exactly equals the manifest SHA;
- a non-empty deterministic baseline journey set exists and every required
  baseline journey passes;
- every cleanup obligation of the metric-bearing attempt derived from lifecycle
  JSONL succeeds.

Any missing, `UNKNOWN`, `UNSUPPORTED`, or failed condition means the repository is
not counted as an autonomous success.

### Meaningful regression-passing convergence

Evaluate convergence only from the repository's sole metric-bearing attempt. With
no metric-bearing attempt, the repository has no convergence. A repository
meaningfully converges only when at least one `KEEP` mutation is backed by replay
of the complete passing baseline journey set and evidence shows that it removes or
reduces privilege. The classification and evidence reference must be recorded per
mutation. `add_tmpfs` by itself is not meaningful.

`ROLLBACK`, `STOP`, unsupported collectors, and not-observed behavior remain raw
evidence and cannot be converted into a meaningful convergence or success.

## 5. Per-repository capture

`MISSING` marks a field that the retained evidence could not produce. URLs and
manifest SHAs below are frozen input, not observed results. Each row is populated
only from its sole metric-bearing
attempt. If none exists, use `MISSING — no metric-bearing attempt` for attempt
identity and duration, apply the fixed failure/zero/no-convergence defaults, and
keep attempt facts exclusively in section 10.

| # | Repository | Manifest SHA | Actual SHA | Metric-bearing attempt ID | UTC start | UTC end | Repository monotonic duration | Exact command | Exit | Terminal outcome | Exact raw `stop_reason` | Failure class |
|---:|---|---|---|---|---|---|---|---|---:|---|---|---|
| 1 | `https://github.com/umami-software/umami` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `70313f8c-9ea6-458a-aad0-89c133fc5ec4` | `2026-08-29T09:45:18.795779+00:00` | `2026-08-29T09:46:31.265957+00:00` | `72.46799999999348` | `uv run repotrial inspect https://github.com/umami-software/umami --provider docker-sbx --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 --container-port 3000 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 2 | `https://github.com/knadh/listmonk` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `4ea7097c-2228-4318-84a1-7c6a5e9b2327` | `2026-08-29T09:47:04.831788+00:00` | `2026-08-29T09:48:04.239354+00:00` | `59.40600000000268` | `uv run repotrial inspect https://github.com/knadh/listmonk --provider docker-sbx --commit-sha 670c01717d48647093335cc23a6be6f4b79c3b6b --container-port 9000 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 3 | `https://github.com/dgtlmoon/changedetection.io` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `04263916-9d1f-4f7c-81cc-4655bc20bb32` | `2026-08-29T09:48:17.682290+00:00` | `2026-08-29T09:49:32.985026+00:00` | `75.31200000000536` | `uv run repotrial inspect https://github.com/dgtlmoon/changedetection.io --provider docker-sbx --commit-sha 5d9c7c6da76340597243e8163c4f2439237fa0e8 --container-port 5000 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 4 | `https://github.com/louislam/uptime-kuma` | `a852e21eba4ecf339624b404518c5bc7fad6d45c` | `a852e21eba4ecf339624b404518c5bc7fad6d45c` | `3f00fc2d-48ed-4222-a184-657b8bace6f0` | `2026-08-29T09:49:47.939558+00:00` | `2026-08-29T09:51:00.853151+00:00` | `72.90600000000268` | `uv run repotrial inspect https://github.com/louislam/uptime-kuma --provider docker-sbx --commit-sha a852e21eba4ecf339624b404518c5bc7fad6d45c --container-port 3001 --compose-path compose.yaml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 5 | `https://github.com/muety/wakapi` | `8143ca13ade1c14959be8a79c1d736d9889013e4` | `8143ca13ade1c14959be8a79c1d736d9889013e4` | `061e3097-62f2-42cc-9dec-b8c7948738f2` | `2026-08-29T09:51:22.588789+00:00` | `2026-08-29T09:52:24.962992+00:00` | `62.36000000000058` | `uv run repotrial inspect https://github.com/muety/wakapi --provider docker-sbx --commit-sha 8143ca13ade1c14959be8a79c1d736d9889013e4 --container-port 3000 --compose-path compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 6 | `https://github.com/sissbruecker/linkding` | `65813a75404b1319aca8b09700fadc0b15adabaf` | `65813a75404b1319aca8b09700fadc0b15adabaf` | `8d508779-0002-417f-98da-8b51be739f9a` | `2026-08-29T09:52:40.803754+00:00` | `2026-08-29T09:53:40.374799+00:00` | `59.57799999999406` | `uv run repotrial inspect https://github.com/sissbruecker/linkding --provider docker-sbx --commit-sha 65813a75404b1319aca8b09700fadc0b15adabaf --container-port 9090 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 7 | `https://github.com/paperless-ngx/paperless-ngx` | `6f3945f11f1ff13ab90da76c26de37660ff1d497` | MISSING — clone failed before verification | MISSING — no metric-bearing attempt | MISSING | MISSING | MISSING | MISSING — retained in section 10 | N/A | Autonomous failure (derived) | MISSING — no metric-bearing attempt | `no metric-bearing attempt` (derived) |
| 8 | `https://github.com/n8n-io/n8n-hosting` | `6b78193475d84ae190622d8a8e9ba8598e89b7d1` | `6b78193475d84ae190622d8a8e9ba8598e89b7d1` | `01b0ed6c-6eeb-4fca-a428-128c8aff4339` | `2026-08-29T09:55:57.571272+00:00` | `2026-08-29T09:56:35.257852+00:00` | `37.687999999994645` | `uv run repotrial inspect https://github.com/n8n-io/n8n-hosting --provider docker-sbx --commit-sha 6b78193475d84ae190622d8a8e9ba8598e89b7d1 --container-port 5678 --compose-path docker-compose/withPostgres/docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 9 | `https://github.com/netbox-community/netbox-docker` | `5adc62fe3fa65163c4ef63733bdcbd3e59b5c544` | `5adc62fe3fa65163c4ef63733bdcbd3e59b5c544` | `155e28e2-877d-4d20-b5ab-cfb335ee2de5` | `2026-08-29T09:56:48.893949+00:00` | `2026-08-29T09:57:36.463230+00:00` | `47.5630000000092` | `uv run repotrial inspect https://github.com/netbox-community/netbox-docker --provider docker-sbx --commit-sha 5adc62fe3fa65163c4ef63733bdcbd3e59b5c544 --container-port 8080 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 10 | `https://github.com/louislam/dockge` | `f809ae192b571944ad773e9866d3e67064ae8043` | `f809ae192b571944ad773e9866d3e67064ae8043` | `b0e72a15-fa10-4863-9440-e98533407f09` | `2026-08-29T09:57:50.215862+00:00` | `2026-08-29T09:58:23.540343+00:00` | `33.312999999994645` | `uv run repotrial inspect https://github.com/louislam/dockge --provider docker-sbx --commit-sha f809ae192b571944ad773e9866d3e67064ae8043 --container-port 5001 --compose-path compose.yaml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |

### Report, journey, experiment, and cleanup evidence

These fields also come only from the sole metric-bearing attempt. No
metric-bearing attempt means no report/baseline evidence, experiment count `0`,
meaningful convergence `NO`, and metric-bearing cleanup `N/A`; audit evidence
remains available in section 10. The cleanup value in this repository-level table
supports autonomous-success derivation only and is not the aggregate cleanup
denominator.

| # | Report/artifact references | Baseline journey definitions/results | Experiment count | Meaningful convergence | Lifecycle JSONL references | Metric-bearing cleanup result |
|---:|---|---|---:|---|---|---|
| 1 | `artifacts/70313f8c-9ea6-458a-aad0-89c133fc5ec4/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/70313f8c-9ea6-458a-aad0-89c133fc5ec4/evidence/baseline-5eb18a4c1f9b1ce0-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 2 | `artifacts/4ea7097c-2228-4318-84a1-7c6a5e9b2327/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/4ea7097c-2228-4318-84a1-7c6a5e9b2327/evidence/baseline-ef9a1d8f87acec76-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 3 | `artifacts/04263916-9d1f-4f7c-81cc-4655bc20bb32/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/04263916-9d1f-4f7c-81cc-4655bc20bb32/evidence/baseline-672819d073160390-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 4 | `artifacts/3f00fc2d-48ed-4222-a184-657b8bace6f0/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/3f00fc2d-48ed-4222-a184-657b8bace6f0/evidence/baseline-ed49c0ce2915c984-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 5 | `artifacts/061e3097-62f2-42cc-9dec-b8c7948738f2/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/061e3097-62f2-42cc-9dec-b8c7948738f2/evidence/baseline-d083a8d7052efce2-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 6 | `artifacts/8d508779-0002-417f-98da-8b51be739f9a/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/8d508779-0002-417f-98da-8b51be739f9a/evidence/baseline-f5bb9d8de959f92e-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 7 | None — no report | None — no metric-bearing attempt | `0` | NO | None — no sandbox created | N/A |
| 8 | `artifacts/01b0ed6c-6eeb-4fca-a428-128c8aff4339/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/01b0ed6c-6eeb-4fca-a428-128c8aff4339/evidence/baseline-cdd798ab4674a725-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 9 | `artifacts/155e28e2-877d-4d20-b5ab-cfb335ee2de5/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/155e28e2-877d-4d20-b5ab-cfb335ee2de5/evidence/baseline-d3d5db9191d73068-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |
| 10 | `artifacts/b0e72a15-fa10-4863-9440-e98533407f09/{attempt-result.json,report/}` | `0/0`; `baseline_observation=null` | `0` | NO | `artifacts/b0e72a15-fa10-4863-9440-e98533407f09/evidence/baseline-e3379201f8f0ff62-0001-attempt-01/baseline-lifecycle.jsonl` | PASS |

### Mutation evidence

Retain one row for every attempted experiment; do not summarize away `ROLLBACK` or
`STOP`. Only mutation rows whose `Attempt ID` equals the repository's sole
metric-bearing attempt ID participate in experiment count and meaningful
convergence. All other mutation rows are audit evidence only and cannot alter
either aggregate.

| Repo # | Attempt ID | Experiment ID | Mutation type/service/params | Parent hash | Candidate hash | Boot | Regression journeys | Verdict (`KEEP`/`ROLLBACK`/`STOP`) | Exact reason | Meaningful? | Evidence references |
|---:|---|---|---|---|---|---|---|---|---|---|---|
| N/A | N/A | N/A | None — all metric-bearing attempts stopped before experiments | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

## 6. Cleanup derivation

Derive each attempt's sandbox lifecycle from its JSONL events; do not infer cleanup
from a process exit or report file.

- `create_success` requires a later `destroy_success` for the same owned sandbox
  ID and no `destroy_failure`; otherwise cleanup is `FAIL` or `UNKNOWN`.
- `create_cleanup_unsafe` requires `cleanup_retry_success`. A
  `cleanup_retry_failure` is `FAIL`.
- `create_failure` before any owned sandbox ID is `N/A`.
- Missing, malformed, or incomplete lifecycle evidence is `UNKNOWN`.
- An attempt cleanup result is `FAIL` if any required lifecycle fails, `UNKNOWN`
  if none fails but required evidence is unknown, `PASS` only if all required
  cleanups succeed, and `N/A` only when no sandbox ID was ever owned.

The repository-level cleanup value is the cleanup result of its sole
metric-bearing attempt. With no metric-bearing attempt it is `N/A`; this value is
used for autonomous-success derivation but not as the aggregate cleanup
denominator.

Aggregate cleanup is successful cleanup obligations across all retained attempts
divided by all cleanup obligations across all retained attempts. Audit-only,
metric-bearing, and diagnostic-only attempts all contribute every owned sandbox
obligation. `N/A` has no obligation. Any `UNKNOWN` obligation makes aggregate
cleanup `UNKNOWN`. The M7.5 aggregate cleanup target is `100%`.

| Repo # | Attempt ID | Lifecycle reference | Create state / owned ID | Required terminal event | Observed terminal event | Derived attempt cleanup | Notes |
|---:|---|---|---|---|---|---|---|
| 1 | `4d487d62-e775-47f1-ac3d-2720309c3f6f` | None | No owned sandbox ID | None | None | `N/A` | Failed before lifecycle boundary |
| 1 | `53bd0dda-93c2-493b-bed1-43c37a585dbf` | `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/evidence/baseline-d780b5f62220835d-0001-attempt-01/baseline-lifecycle.jsonl` | `create_cleanup_unsafe`; `repotrial-repotrial-baseline-d780b-797742fcf4c2` | `cleanup_retry_success` | `cleanup_retry_failure` | `FAIL` | Later `sbx list` empty does not override the frozen lifecycle rule |
| 1 | `70313f8c-9ea6-458a-aad0-89c133fc5ec4` | `artifacts/70313f8c-9ea6-458a-aad0-89c133fc5ec4/evidence/baseline-5eb18a4c1f9b1ce0-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-5eb18-f19c462fedb8` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 2 | `4ea7097c-2228-4318-84a1-7c6a5e9b2327` | `artifacts/4ea7097c-2228-4318-84a1-7c6a5e9b2327/evidence/baseline-ef9a1d8f87acec76-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-ef9a1-c610e20cd671` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 3 | `04263916-9d1f-4f7c-81cc-4655bc20bb32` | `artifacts/04263916-9d1f-4f7c-81cc-4655bc20bb32/evidence/baseline-672819d073160390-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-67281-f0f72b765017` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 4 | `3f00fc2d-48ed-4222-a184-657b8bace6f0` | `artifacts/3f00fc2d-48ed-4222-a184-657b8bace6f0/evidence/baseline-ed49c0ce2915c984-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-ed49c-c988dc996f24` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 5 | `061e3097-62f2-42cc-9dec-b8c7948738f2` | `artifacts/061e3097-62f2-42cc-9dec-b8c7948738f2/evidence/baseline-d083a8d7052efce2-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-d083a-b400050bdff6` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 6 | `8d508779-0002-417f-98da-8b51be739f9a` | `artifacts/8d508779-0002-417f-98da-8b51be739f9a/evidence/baseline-f5bb9d8de959f92e-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-f5bb9-e2c0c90d3b6f` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 7 | `b8907b7d-d983-4e54-874a-f81cc538f9f5` | None | No owned sandbox ID | None | None | `N/A` | Clone failed before sandbox lifecycle |
| 8 | `01b0ed6c-6eeb-4fca-a428-128c8aff4339` | `artifacts/01b0ed6c-6eeb-4fca-a428-128c8aff4339/evidence/baseline-cdd798ab4674a725-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-cdd79-737a3f265064` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 9 | `155e28e2-877d-4d20-b5ab-cfb335ee2de5` | `artifacts/155e28e2-877d-4d20-b5ab-cfb335ee2de5/evidence/baseline-d3d5db9191d73068-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-d3d5d-1e9084865c26` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |
| 10 | `b0e72a15-fa10-4863-9440-e98533407f09` | `artifacts/b0e72a15-fa10-4863-9440-e98533407f09/evidence/baseline-e3379201f8f0ff62-0001-attempt-01/baseline-lifecycle.jsonl` | `create_success`; `repotrial-repotrial-baseline-e3379-b8e827e55112` | `destroy_success` | `destroy_success` | `PASS` | Metric-bearing |

## 7. Failure taxonomy

Always preserve exact raw `stop_reason` separately from the normalized class for
every attempt. Attempt-level classes use the taxonomy below. Repository rows and
aggregate failure-class counts use only the sole metric-bearing attempt; a
repository with none has the derived state `no metric-bearing attempt` and is not
post-hoc assigned one of these attempt failure classes.

| Failure class | Classification boundary | Count | Run/attempt references |
|---|---|---:|---|
| intake | URL/ref/clone/pin/manifest-SHA identity failure before workload execution | `0` metric-bearing; one no-metric pre-workload terminal | `b8907b7d-d983-4e54-874a-f81cc538f9f5` is retained only in the all-attempt ledger |
| Compose discovery/parse | Compose path discovery, loading, or validation failure | `0` | None |
| sandbox unsupported | Required provider or observability capability is unsupported | `0` | None; PID remains a disclosed Owner-accepted known limitation |
| create/boot | Sandbox creation or workload boot failure | `9` | `70313f8c-...`, `4ea7097c-...`, `04263916-...`, `3f00fc2d-...`, `061e3097-...`, `8d508779-...`, `01b0ed6c-...`, `155e28e2-...`, `b0e72a15-...` |
| baseline journey | Non-empty deterministic baseline journey execution/verification failure | `0` | None; all metric attempts stopped before a baseline observation |
| experiment | Mutation application, candidate boot, replay, or decision failure | `0` | None |
| whole-trial timeout | Whole-duration deadline terminates the attempt | `0` | None |
| cleanup | Required destroy/retry cleanup fails | `0` metric-bearing | Audit-only attempt `53bd0dda-...` has a failed cleanup obligation and is counted in aggregate cleanup, not repository failure classes |
| internal | RepoTrial internal failure not classified above | `0` metric-bearing | Audit-only attempt `4d487d62-...` is retained in section 10 |
| unknown | Evidence cannot support a more specific class | `0` | None |

## 8. Aggregate metrics and thresholds

Repository outcome, convergence, experiment, failure-class, and timing aggregates
use the ten frozen repositories and derive from each sole metric-bearing attempt or
the frozen no-metric-bearing defaults. Failed metric-bearing attempts and
repositories with no metric-bearing attempt contribute `0` experiments;
survivor-only averages are prohibited. Exact-stop-reason coverage and aggregate
cleanup are all-attempt safety gates and use every retained attempt instead.

| Metric | Exact calculation | Result | M7.5 threshold/status |
|---|---|---|---|
| Autonomous successes | Count whose metric-bearing attempt satisfies every autonomous-success condition; none means failure; denominator 10 | `0/10` | **FAIL** — requires at least `7/10` |
| Meaningful convergences | Count whose metric-bearing attempt has at least one meaningful regression-passing `KEEP`; none means no convergence; denominator 10 | `0/10` | **FAIL** — requires at least `5/10` |
| Exact stop-reason coverage | All retained failed attempts with exact raw `stop_reason` / all retained failed attempts | `12/12 = 100%` | PASS |
| Average experiments | Sum of metric-bearing experiment counts for all ten repositories / `10`; no metric-bearing/no-experiment adds zero | `0 / 10 = 0.0` | Report only |
| Aggregate cleanup | Successful cleanup obligations across all retained attempts / all cleanup obligations across all retained attempts; `N/A` has no obligation and any `UNKNOWN` makes the result `UNKNOWN` | `9/10 = 90%` | **FAIL** — requires `100%` |
| P50 repository duration | Nearest-rank over metric-bearing repository durations, section 9 | `UNKNOWN` | Paperless-ngx has no metric-bearing duration |
| P95 repository duration | Nearest-rank over metric-bearing repository durations, section 9 | `UNKNOWN` | Paperless-ngx has no metric-bearing duration |

The exact-stop-reason and aggregate-cleanup rows are safety gates, not repository
outcome metrics. They include primary, replacement, audit-only, metric-bearing,
and diagnostic-only attempts according to their retained terminal and lifecycle
evidence.

The release-style Before/After demo threshold is met only if autonomous successes
are at least `7/10`, meaningful convergences are at least `5/10`, every failure has
an exact stop reason, and cleanup reaches `100%`. Otherwise report the unmet gate.

If fewer than `5/10` repositories run autonomously and failures are mainly caused
by project heterogeneity, perform the M7.5 Kill Criteria analysis. `5-6`
autonomous successes, or only `3-4` meaningful convergences, are continuation
zones that miss the release threshold without automatically triggering that kill
condition.

## 9. Timing

The repository duration is the external monotonic whole-attempt duration of its
sole metric-bearing attempt, including cleanup. Audit-only and diagnostic-only
attempt durations remain in section 10 but are excluded here. If no metric-bearing
attempt exists, the repository duration is missing. Sort all ten repository
durations ascending only when all are present. With `n = 10`, nearest-rank P50 is
rank `ceil(0.50 * 10) = 5`, and P95 is rank `ceil(0.95 * 10) = 10`. If any
repository duration is missing, both timing aggregates are `UNKNOWN`; do not
silently substitute another attempt or exclude that repository.

| Repo # | Metric-bearing attempt ID | Repository monotonic duration | Included in all-ten ordering? | Evidence |
|---:|---|---|---|---|
| 1 | `70313f8c-9ea6-458a-aad0-89c133fc5ec4` | `72.46799999999348` | NO — all-ten ordering is incomplete | `artifacts/70313f8c-9ea6-458a-aad0-89c133fc5ec4/attempt-result.json` |
| 2 | `4ea7097c-2228-4318-84a1-7c6a5e9b2327` | `59.40600000000268` | NO — all-ten ordering is incomplete | `artifacts/4ea7097c-2228-4318-84a1-7c6a5e9b2327/attempt-result.json` |
| 3 | `04263916-9d1f-4f7c-81cc-4655bc20bb32` | `75.31200000000536` | NO — all-ten ordering is incomplete | `artifacts/04263916-9d1f-4f7c-81cc-4655bc20bb32/attempt-result.json` |
| 4 | `3f00fc2d-48ed-4222-a184-657b8bace6f0` | `72.90600000000268` | NO — all-ten ordering is incomplete | `artifacts/3f00fc2d-48ed-4222-a184-657b8bace6f0/attempt-result.json` |
| 5 | `061e3097-62f2-42cc-9dec-b8c7948738f2` | `62.36000000000058` | NO — all-ten ordering is incomplete | `artifacts/061e3097-62f2-42cc-9dec-b8c7948738f2/attempt-result.json` |
| 6 | `8d508779-0002-417f-98da-8b51be739f9a` | `59.57799999999406` | NO — all-ten ordering is incomplete | `artifacts/8d508779-0002-417f-98da-8b51be739f9a/attempt-result.json` |
| 7 | MISSING — no metric-bearing attempt | MISSING | NO | `artifacts/b8907b7d-d983-4e54-874a-f81cc538f9f5/attempt-result.json` (audit-only timing, excluded) |
| 8 | `01b0ed6c-6eeb-4fca-a428-128c8aff4339` | `37.687999999994645` | NO — all-ten ordering is incomplete | `artifacts/01b0ed6c-6eeb-4fca-a428-128c8aff4339/attempt-result.json` |
| 9 | `155e28e2-877d-4d20-b5ab-cfb335ee2de5` | `47.5630000000092` | NO — all-ten ordering is incomplete | `artifacts/155e28e2-877d-4d20-b5ab-cfb335ee2de5/attempt-result.json` |
| 10 | `b0e72a15-fa10-4863-9440-e98533407f09` | `33.312999999994645` | NO — all-ten ordering is incomplete | `artifacts/b0e72a15-fa10-4863-9440-e98533407f09/attempt-result.json` |

## 10. Attempt ledger and infrastructure invalidation

Every attempted command remains in the audit trail. Populate one row in each table
per attempt using the same attempt ID. Attempt role and metric attribution are
derived by the frozen rules in section 1; there is no free-form post-hoc field that
can change metric inclusion. The independently reviewable root-cause, reproduction,
fix-commit, inventory, and Owner-authorization chain for the two Umami
invalidations is retained in
[`pilot-evidence/umami-replacement-gate.md`](pilot-evidence/umami-replacement-gate.md).

| Repo # | Attempt ID | Attempt role (`primary`/`replacement`/`diagnostic`) | Metric attribution | Target workload started? | Actual SHA | UTC start | UTC end | Monotonic duration | Exact command | Exit | Terminal outcome | Exact raw `stop_reason` | Failure class |
|---:|---|---|---|---|---|---|---|---|---|---:|---|---|---|
| 1 | `4d487d62-e775-47f1-ac3d-2720309c3f6f` | `primary` | `audit-only` — verified pre-workload infrastructure invalidation | `false` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `2026-08-29T09:05:56.262880+00:00` | `2026-08-29T09:06:50.320499+00:00` | `54.04700000000594` | `uv run repotrial inspect https://github.com/umami-software/umami --provider docker-sbx --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 --container-port 3000 --compose-path docker-compose.yml` | `1` outer command; `4` logical evidence | `exception` | `internal:valueerror` | `internal` |
| 1 | `53bd0dda-93c2-493b-bed1-43c37a585dbf` | `replacement` | `audit-only` — verified pre-workload infrastructure invalidation | `false` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `2026-08-29T09:23:55.105099+00:00` | `2026-08-29T09:24:56.031849+00:00` | `60.921999999991385` | `uv run repotrial inspect https://github.com/umami-software/umami --provider docker-sbx --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 --container-port 3000 --compose-path docker-compose.yml` | `1` outer command; `4` logical evidence | `exception` | `internal:cleanuperror` | `cleanup` |
| 1 | `70313f8c-9ea6-458a-aad0-89c133fc5ec4` | `replacement` | `metric-bearing` | `true` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `2026-08-29T09:45:18.795779+00:00` | `2026-08-29T09:46:31.265957+00:00` | `72.46799999999348` | `uv run repotrial inspect https://github.com/umami-software/umami --provider docker-sbx --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 --container-port 3000 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 2 | `4ea7097c-2228-4318-84a1-7c6a5e9b2327` | `primary` | `metric-bearing` | `true` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `2026-08-29T09:47:04.831788+00:00` | `2026-08-29T09:48:04.239354+00:00` | `59.40600000000268` | `uv run repotrial inspect https://github.com/knadh/listmonk --provider docker-sbx --commit-sha 670c01717d48647093335cc23a6be6f4b79c3b6b --container-port 9000 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 3 | `04263916-9d1f-4f7c-81cc-4655bc20bb32` | `primary` | `metric-bearing` | `true` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `2026-08-29T09:48:17.682290+00:00` | `2026-08-29T09:49:32.985026+00:00` | `75.31200000000536` | `uv run repotrial inspect https://github.com/dgtlmoon/changedetection.io --provider docker-sbx --commit-sha 5d9c7c6da76340597243e8163c4f2439237fa0e8 --container-port 5000 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 4 | `3f00fc2d-48ed-4222-a184-657b8bace6f0` | `primary` | `metric-bearing` | `true` | `a852e21eba4ecf339624b404518c5bc7fad6d45c` | `2026-08-29T09:49:47.939558+00:00` | `2026-08-29T09:51:00.853151+00:00` | `72.90600000000268` | `uv run repotrial inspect https://github.com/louislam/uptime-kuma --provider docker-sbx --commit-sha a852e21eba4ecf339624b404518c5bc7fad6d45c --container-port 3001 --compose-path compose.yaml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 5 | `061e3097-62f2-42cc-9dec-b8c7948738f2` | `primary` | `metric-bearing` | `true` | `8143ca13ade1c14959be8a79c1d736d9889013e4` | `2026-08-29T09:51:22.588789+00:00` | `2026-08-29T09:52:24.962992+00:00` | `62.36000000000058` | `uv run repotrial inspect https://github.com/muety/wakapi --provider docker-sbx --commit-sha 8143ca13ade1c14959be8a79c1d736d9889013e4 --container-port 3000 --compose-path compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 6 | `8d508779-0002-417f-98da-8b51be739f9a` | `primary` | `metric-bearing` | `true` | `65813a75404b1319aca8b09700fadc0b15adabaf` | `2026-08-29T09:52:40.803754+00:00` | `2026-08-29T09:53:40.374799+00:00` | `59.57799999999406` | `uv run repotrial inspect https://github.com/sissbruecker/linkding --provider docker-sbx --commit-sha 65813a75404b1319aca8b09700fadc0b15adabaf --container-port 9090 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 7 | `b8907b7d-d983-4e54-874a-f81cc538f9f5` | `primary` | `no-metric pre-workload terminal` | `false` | MISSING — clone failed before verification | `2026-08-29T09:54:03.498649+00:00` | `2026-08-29T09:54:16.100172+00:00` | `12.593000000008033` (audit-only timing) | `uv run repotrial inspect https://github.com/paperless-ngx/paperless-ngx --provider docker-sbx --commit-sha 6f3945f11f1ff13ab90da76c26de37660ff1d497 --container-port 8000 --compose-path docker/compose/docker-compose.postgres.yml` | `1` outer; `4` evidence | `exception` | `intake:clone` | `intake` |
| 8 | `01b0ed6c-6eeb-4fca-a428-128c8aff4339` | `primary` | `metric-bearing` | `true` | `6b78193475d84ae190622d8a8e9ba8598e89b7d1` | `2026-08-29T09:55:57.571272+00:00` | `2026-08-29T09:56:35.257852+00:00` | `37.687999999994645` | `uv run repotrial inspect https://github.com/n8n-io/n8n-hosting --provider docker-sbx --commit-sha 6b78193475d84ae190622d8a8e9ba8598e89b7d1 --container-port 5678 --compose-path docker-compose/withPostgres/docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 9 | `155e28e2-877d-4d20-b5ab-cfb335ee2de5` | `primary` | `metric-bearing` | `true` | `5adc62fe3fa65163c4ef63733bdcbd3e59b5c544` | `2026-08-29T09:56:48.893949+00:00` | `2026-08-29T09:57:36.463230+00:00` | `47.5630000000092` | `uv run repotrial inspect https://github.com/netbox-community/netbox-docker --provider docker-sbx --commit-sha 5adc62fe3fa65163c4ef63733bdcbd3e59b5c544 --container-port 8080 --compose-path docker-compose.yml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |
| 10 | `b0e72a15-fa10-4863-9440-e98533407f09` | `primary` | `metric-bearing` | `true` | `f809ae192b571944ad773e9866d3e67064ae8043` | `2026-08-29T09:57:50.215862+00:00` | `2026-08-29T09:58:23.540343+00:00` | `33.312999999994645` | `uv run repotrial inspect https://github.com/louislam/dockge --provider docker-sbx --commit-sha f809ae192b571944ad773e9866d3e67064ae8043 --container-port 5001 --compose-path compose.yaml` | `1` outer; `3` evidence | `trial_failed` | `boot_recovery_stopped` | `create/boot` |

Allowed terminal metric-attribution values are `audit-only` for a verified
pre-workload infrastructure invalidation, `metric-bearing` for the first attempt
that starts target workload, `diagnostic-only` after that binding, and
`no-metric pre-workload terminal` for a non-replaceable attempt that ends before
target workload starts.

| Repo # | Attempt ID | Report/artifact references | Lifecycle JSONL references | Derived attempt cleanup | Invalidation evidence and authorization | Notes |
|---:|---|---|---|---|---|---|
| 1 | `4d487d62-e775-47f1-ac3d-2720309c3f6f` | `artifacts/4d487d62-e775-47f1-ac3d-2720309c3f6f/attempt-result.json` | None — no lifecycle JSONL | `N/A` | Verified pre-workload infrastructure invalidation: relative artifact root produced mixed relative/absolute GraphContext paths; no sandbox/lifecycle was reached and `sbx list` was empty. Owner later authorized one replacement attempt. | `target_workload_started=false`; retain as audit-only and do not fill metric-bearing repository outcomes. The command runner observed process exit `1`; retained RepoTrial evidence records logical exit `4`, so both are preserved rather than silently reconciled. |
| 1 | `53bd0dda-93c2-493b-bed1-43c37a585dbf` | `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/attempt-result.json` | `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/evidence/baseline-d780b5f62220835d-0001-attempt-01/baseline-lifecycle.jsonl` | `FAIL` — `create_cleanup_unsafe` followed by `cleanup_retry_failure`; post-attempt `sbx list` was empty but does not override lifecycle evidence | Verified generic pre-workload compatibility invalidation: installed `sbx create --help` specifies `--cpus int`; the attempt passed `1.5` and failed before any daemon create request. A controlled provider reproduction retained exact stderr `invalid argument \"1.5\" for \"--cpus\" ... ParseInt`; integer CPU then passed `create -> exec echo ok -> destroy` against the same pinned workspace with no residue. Owner authorized generic TDD repair and continuation. | `target_workload_started=false`; retain as audit-only. Generic fix `cbc6eea0cb570f46929693a1f38cf7904cb2bf21` rejects fractional CPU policy and uses an integer default; scoped reviewer accepted it. The raw attempt stop reason remains exactly as originally captured. |
| 1 | `70313f8c-9ea6-458a-aad0-89c133fc5ec4` | `artifacts/70313f8c-9ea6-458a-aad0-89c133fc5ec4/{attempt-result.json,report/}` | `artifacts/70313f8c-9ea6-458a-aad0-89c133fc5ec4/evidence/baseline-5eb18a4c1f9b1ce0-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | Owner-authorized continuation after generic fix | First Umami attempt to cross workload boundary; irrevocably metric-bearing |
| 2 | `4ea7097c-2228-4318-84a1-7c6a5e9b2327` | `artifacts/4ea7097c-2228-4318-84a1-7c6a5e9b2327/{attempt-result.json,report/}` | `artifacts/4ea7097c-2228-4318-84a1-7c6a5e9b2327/evidence/baseline-ef9a1d8f87acec76-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 3 | `04263916-9d1f-4f7c-81cc-4655bc20bb32` | `artifacts/04263916-9d1f-4f7c-81cc-4655bc20bb32/{attempt-result.json,report/}` | `artifacts/04263916-9d1f-4f7c-81cc-4655bc20bb32/evidence/baseline-672819d073160390-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 4 | `3f00fc2d-48ed-4222-a184-657b8bace6f0` | `artifacts/3f00fc2d-48ed-4222-a184-657b8bace6f0/{attempt-result.json,report/}` | `artifacts/3f00fc2d-48ed-4222-a184-657b8bace6f0/evidence/baseline-ed49c0ce2915c984-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 5 | `061e3097-62f2-42cc-9dec-b8c7948738f2` | `artifacts/061e3097-62f2-42cc-9dec-b8c7948738f2/{attempt-result.json,report/}` | `artifacts/061e3097-62f2-42cc-9dec-b8c7948738f2/evidence/baseline-d083a8d7052efce2-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 6 | `8d508779-0002-417f-98da-8b51be739f9a` | `artifacts/8d508779-0002-417f-98da-8b51be739f9a/{attempt-result.json,report/}` | `artifacts/8d508779-0002-417f-98da-8b51be739f9a/evidence/baseline-f5bb9d8de959f92e-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 7 | `b8907b7d-d983-4e54-874a-f81cc538f9f5` | `artifacts/b8907b7d-d983-4e54-874a-f81cc538f9f5/attempt-result.json` | None | `N/A` | No verified infrastructure invalidation: a later diagnostic clone succeeded, but the retained exception does not preserve enough detail to prove the original transient cause | Frozen no-metric pre-workload autonomous failure; no replacement |
| 8 | `01b0ed6c-6eeb-4fca-a428-128c8aff4339` | `artifacts/01b0ed6c-6eeb-4fca-a428-128c8aff4339/{attempt-result.json,report/}` | `artifacts/01b0ed6c-6eeb-4fca-a428-128c8aff4339/evidence/baseline-cdd798ab4674a725-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 9 | `155e28e2-877d-4d20-b5ab-cfb335ee2de5` | `artifacts/155e28e2-877d-4d20-b5ab-cfb335ee2de5/{attempt-result.json,report/}` | `artifacts/155e28e2-877d-4d20-b5ab-cfb335ee2de5/evidence/baseline-d3d5db9191d73068-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |
| 10 | `b0e72a15-fa10-4863-9440-e98533407f09` | `artifacts/b0e72a15-fa10-4863-9440-e98533407f09/{attempt-result.json,report/}` | `artifacts/b0e72a15-fa10-4863-9440-e98533407f09/evidence/baseline-e3379201f8f0ff62-0001-attempt-01/baseline-lifecycle.jsonl` | `PASS` | N/A | Primary metric-bearing attempt |

## 11. Pilot decision

**M7.5 RELEASE GATE — FAIL.** Autonomous success is `0/10` (required `>=7/10`),
meaningful convergence is `0/10` (required `>=5/10`), and aggregate cleanup is
`9/10 = 90%` (required `100%`). Exact stop-reason coverage is `12/12 = 100%`,
all nine verified repository SHAs match their manifest pins, and the final SBX
inventory is empty. No release-style Before/After Demo is authorized.

Autonomous success below `5/10` triggers Kill Criteria analysis. The dominant
observed metric-bearing failure class is create/boot: all nine metric-bearing
attempts ended with the exact graph stop reason `boot_recovery_stopped` before a
baseline observation or experiment. The retained evidence does not include the
underlying boot-command failure detail, so it cannot prove that project
heterogeneity, rather than a common generic boot path, is the main cause. It would
be unsound to add repository-specific setup scripts or to claim the product thesis
is disproven from this evidence alone.

Owner decision is required between a bounded, generic boot-evidence/root-cause
task and stopping the MVP line. Any future production change after these
metric-bearing results requires marking them superseded and rerunning the complete
frozen cohort on one final unified HEAD. No repository may be replaced.

## 12. WSL2 Linux environment and cohort handoff

The accepted execution environment for the next release decision is the
dedicated WSL2 Ubuntu 24.04 topology documented in the
[setup guide](wsl2-linux-sbx-setup.md) and
[calibration evidence](pilot-evidence/wsl2-linux-sbx-calibration.md).
Calibration ran at
`760f73c15aba839a71ac036699b527334808b73f`; the report was persisted by
calibration evidence commit
`6baf2e65e313c051e91d2062416262be7f7dd6f1`.

All Windows attempts and rows above remain immutable append-only evidence. The
Windows execution cohort is superseded only at cohort level for the next release
decision; no attempt ID, raw stop reason, role, SHA, artifact reference, or
historical metric is deleted, overwritten, or reclassified.

Execution resumes at existing Task 2 of the M7.5 compatibility-recovery plan:

1. Linux attempts for frozen repositories #1 Umami, #2 Listmonk, and #3
   changedetection.io are initially `diagnostic-only`. They diagnose the
   dominant generic cause and are not current release metrics.
2. If Task 2 authorizes one or more generic corrections under the existing
   evidence and review gates, every correction is completed before the final
   Linux cohort is frozen.
3. On the final unified Linux execution HEAD, repositories #1–#3 are
   simultaneously the 2/3 canary and the first three `metric-bearing` entries.
   If the canary passes, execution continues with frozen repositories #4–#10;
   #1–#3 are not rerun between those two roles.
4. The frozen manifest, order, URLs, exact SHAs, thresholds, and
   Boot/Recovery/Journey/experiment/KEEP/ROLLBACK semantics remain unchanged.

Current release metrics aggregate only attempts from that final Linux cohort and
one execution HEAD/environment fingerprint. Cross-cohort reporting is separate:
it retains Windows history, Linux diagnostic attempts, failed/infrastructure
attempts, and explicit supersession links without mixing them into the current
cohort numerator, denominator, latency, convergence, or cleanup result.

## 13. M7.5 WSL2 compatibility diagnostic canary

Task 2 resumed on the calibrated dedicated WSL2 Ubuntu 24.04 environment at
execution HEAD `4565301f1307cfec3de8c97549c978244bce793d`. The frozen manifest
hash matched, SBX was v0.39.0, diagnose was `12/12` PASS, and initial inventory
was empty. The first three frozen repositories were each executed exactly once
in order. Detailed bounded command, recovery, lifecycle, and inventory evidence
is retained in
[`pilot-evidence/m7.5-compatibility-diagnostic.md`](pilot-evidence/m7.5-compatibility-diagnostic.md).

| Repo | Run ID | Role / metric attribution | Expected = actual SHA | Outer / evidence exit | Target workload started? | First failed command / phase | Recovery / exact stop reason | Cleanup / post-list |
|---|---|---|---|---|---|---|---|---|
| Umami | `c4c95907-43a2-465c-b14e-40519ab85134` | diagnostic / `diagnostic-only` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `1` / `3` | `true` | `up`, baseline Boot, exit `1`: host artifact Compose path absent in guest | `stop` / `stopped` / `boot_recovery_stopped` | PASS / exact empty |
| Listmonk | `efd48f46-5c24-4f3d-9c83-4dec02c61003` | diagnostic / `diagnostic-only` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `1` / `3` | `true` | `up`, baseline Boot, exit `1`: host artifact Compose path absent in guest | `stop` / `stopped` / `boot_recovery_stopped` | PASS / exact empty |
| changedetection.io | `e8c3a9df-44c8-4e44-8857-d429ea80e9a3` | diagnostic / `diagnostic-only` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `1` / `3` | `true` | `up`, baseline Boot, exit `1`: host artifact Compose path absent in guest | `stop` / `stopped` / `boot_recovery_stopped` | PASS / exact empty |

**Dominant-cause ruling: 3/3 qualifying, Category A — same RepoTrial generic
bug.** Each non-truncated retained Boot stream has the same command and phase,
compatible exit, and normalized causal signature:
`open <host-ext4-checkout>/artifacts/<run_id>/workspace/docker-compose.yml: no
such file or directory`. This proves a generic host-to-guest Compose path
mapping/command-construction defect after the target-workload execution boundary
but before any target service started. The ruling does not
come from `boot_recovery_stopped`, has no dissenting repository, and does not
establish Task 3's bounded-recovery or missing-environment entry gate. No later
task was started. All three lifecycle cleanups succeeded and all three
post-attempt inventories were exactly empty.

## 14. Correction: cloned-workspace allocation and clone postcondition

The original path-mapping attribution in section 13 is retained as historical
symptom evidence, but is superseded by later direct 5/64 MiB A/B probes. At a
5 MiB cloned-workspace allocation, guest `pwd` was the correct target path but
the directory was empty, with neither Git metadata nor a Compose file, even
though `sbx create` succeeded. With only cloned-workspace allocation raised to
64 MiB under the same 512 MiB total policy, Umami, Listmonk, and
changedetection.io each had the exact frozen guest SHA, their selected Compose
file, and successful `docker compose config`.

Every probe completed `create_attempt -> create_success -> destroy_attempt ->
destroy_success`, with exact empty final inventory. The bounded provider
correction allocates cloned workspace proportionally at 1:8 (subject to the
existing floors) and requires guest top-level/work-tree/HEAD/clean-status
postconditions before allowing public network or recording ACTIVE. This is a
tested-workload compatibility decision, not a global compatibility or
least-privilege claim.

## 15. Post-fix Task 2A compatibility diagnostics

This append-only section records the three post-fix diagnostic-only reruns at
code/evidence HEAD `0ae81d8eec376c4af7f89a60c5351d841b28b41c`. It supersedes
only the prior correct-path-but-empty-clone symptom. Sections 13 and 14, their
attempts, and their original path-mapping attribution remain unchanged. Every
rerun used the frozen repository URL, SHA, Compose path, and port; all actual
SHAs matched their pins. These runs are not metric-bearing and no repository was
replaced. Full retained evidence is appended to
[`pilot-evidence/m7.5-compatibility-diagnostic.md`](pilot-evidence/m7.5-compatibility-diagnostic.md).

| Repo | Run ID | Exact SHA | Duration | Command results | Boot / retained outcome | Recovery / lifecycle |
|---|---|---|---:|---|---|---|
| Umami | `f4167de9-c652-47be-bcc8-db7190d66bba` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `102.2119506329982 s` | `up=1` (stderr truncated; causal tail retained), `ps=0`, `logs=0` | Compose present; images pulled; containers created and started; `db=restarting`, `umami=created`; Boot fail; PostgreSQL `initdb` could not create `/var/lib/postgresql/data/pg_wal`: `No space left on device` | `stop` / `stopped` / `invalid recovery evidence` / `boot_recovery_stopped`; `create_success -> destroy_attempt -> destroy_success` |
| Listmonk | `9aca7344-e60a-4d84-ac9a-34c8dfc5b8b9` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `60.388527538001654 s` | `up=0`, `ps=0`, `logs=0`; no truncation | Compose present; images pulled; containers created and started; `app=restarting`, `db=running/starting`; Boot fail. Later logs show PostgreSQL ready and Listmonk `http server started on [::]:9000`; bounded immediate post-up startup/readiness transition | `stop` / `stopped` / `invalid recovery evidence` / `boot_recovery_stopped`; `create_success -> destroy_attempt -> destroy_success` |
| changedetection.io | `e5211ae6-a2e5-411b-abf6-9038b7b354ba` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `113.89821543700236 s` | `up=0`, `ps=0`, `logs=0`; no truncation | Compose present; `changedetection=running`; Boot pass; Flask listened on port 5000. Later Observation collection failed with `ObservationCollectionError`, exit `4`, `internal:observationcollectionerror`; no more specific collector reason retained | No Recovery ruling; `create_success -> destroy_attempt -> destroy_success` |

The Task 2A cloned-workspace correction is empirically validated `3/3`: every
rerun reached its exact Compose workload and the old correct-path-but-empty
workspace failure did not recur. The controller's final inventory check after
the third rerun was exactly `No sandboxes found.` It is not evidence of a
separate immediate post-attempt inventory check after each individual rerun.

The primary workload outcomes are heterogeneous: Umami has a hard
Docker/workload disk-exhaustion signature, Listmonk has a bounded
startup/readiness transition, and changedetection.io passes Boot before a later
`ObservationCollectionError`.

Independently of those workload causes, Umami and Listmonk share one exact
Recovery-phase failure: `action=stop`, `disposition=stopped`, reason
`invalid recovery evidence`. Current code accepts empty `allowed_env_keys` and
an empty README as valid; `_combined_logs()` rejects fields above `4,096`
characters or aggregate logs above `16,384` characters. Both retained attempts
have sanitized Boot evidence beyond those bounds, yielding the same Recovery
phase and normalized planner-input rejection signature in `2/3`.

The frozen Task 3 entry gate is therefore met for bounded recovery projection.
This authorizes only the repair that enables the deterministic planner to receive
valid bounded evidence; it does not claim to fix Umami's disk exhaustion or
Listmonk's startup transition. Task 4's readiness entry gate is not met: only
Listmonk shows the bounded startup/readiness transition, while Umami has a hard
disk error and changedetection.io passes Boot. Task 5 is not ruled on by this
documentation task, and Task 3 is not implemented here.

**TASK 3 ENTRY GATE MET — BOUNDED RECOVERY PROJECTION AUTHORIZED**

## 16. Post-Task-3 diagnostics and Task 4 gate ruling

Three post-Task-3 reruns were completed at reviewed HEAD
`922d6357dcb4d782bcaa7a84d5c29fc0f3be4a50`. They are all
`diagnostic-only`, are not metric-bearing, and do not replace any retained
attempt history. Focused Task 3 tests passed `117`; `recovery_context` coverage
was `86.27%`. The full suite passed `1233`, with `5` skipped; total coverage was
`88.71%`. Ruff check, Ruff format check, mypy, pre-commit, and `git diff --check`
passed. The real SBX lifecycle smoke passed `1` test in `25.16s`.

| Repo | Run | Exact expected/actual SHA | Duration | Retained outcome and ruling |
|---|---|---|---:|---|
| Umami | `a6f0a1dc-e530-45e8-bec9-4addd307ecc5` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `307.7671560730014 s` | Attempts 1 and 2: `up=1`; `ps` showed `db=restarting`, `umami=created`; logs repeatedly contained PostgreSQL `No space left on device`. Recovery was schema-valid and applied: `action=wait`, reason `recognizable startup delay`; the old `invalid recovery evidence` outcome did not recur. Attempt 3 was not started as a workload subprocess because the trusted host-side deadline was exhausted. Terminal stop reason: `sandbox:exec:total_duration_exhausted`. All three lifecycle records ended in `destroy_success`. Task 3 made bounded evidence consumable, but this repository did not transition to ready; disk exhaustion and recovery classification remain a separate generic issue outside this gate task. |
| Listmonk | `61aa53fd-ca6b-47af-94c5-e0128bf6d8c5` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | `150.46334362200287 s` | First `compose up` ended as `DockerSbxError` before `ps`/`logs`/recovery; terminal stop reason `sandbox:exec:timeout`. Lifecycle ended in `destroy_success`. This run is inconclusive for Task 3/Task 4 recovery behavior because it never reached a retained startup-transition evidence set. |
| changedetection.io | `4f3a621e-675d-4c77-ad01-b2618e152403` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `157.56752266000694 s` | Boot: `up=0`, final service state `changedetection=running`, verdict PASS. Later terminal exception remains `ObservationCollectionError`; stop reason `internal:observationcollectionerror`. Lifecycle ended in `destroy_success`. This is not a recovery-produced starting/health transition and does not satisfy the Task 4 entry-gate signature. |

The known limitation `pid_hard_bound_unsupported` remains unchanged. The final
`sbx list` after all reruns was `No sandboxes found.` Windows and WSL checkouts
were clean at exact HEAD `922d635...` before this documentation edit.

Task 4 entry gate requires at least `2/3` latest diagnostics with Compose still
in a bounded starting/health transition after `up`. The observed qualifying
count is `0/3`: Umami is a hard disk-exhaustion failure; Listmonk timed out
before evidence; changedetection passed Boot directly.

**Task 4 — NOT APPLICABLE (ENTRY GATE NOT MET)**. No readiness implementation
or calibration is authorized.
