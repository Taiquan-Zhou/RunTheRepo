# M7.5 real-repository Pilot protocol and report

> **EXECUTION IN PROGRESS — cohort and protocol remain frozen**

This document freezes the M7.5 execution protocol and records the append-only Pilot
evidence. Infrastructure-invalidated attempts are retained below; no repository
metric is claimed until the target-workload boundary is actually crossed.

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
| Sandbox health | Current provider health and required isolation capabilities pass immediately before execution. | UNVERIFIED — gate closed |
| Immutable repository identity | `--commit-sha` accepts only a lowercase full SHA, generic intake checks it out detached, verifies `HEAD^{commit}`, and persists expected/actual SHA in `attempt-result.json`. | IMPLEMENTED — verify against every exact Pilot attempt |
| Exact failure reason | Deterministic report JSON includes `RunState.stop_reason`; terminal `attempt-result.json` retains it for graph terminal results and a sanitized reason for exceptions. | IMPLEMENTED — verify against every exact Pilot attempt |
| Resource bounds | Effective CPU, memory, disk, and whole-duration limits are all verified; PID is the separately disclosed unsupported limitation. | UNVERIFIED — gate closed |
| Forced cleanup | Cancellation, timeout, normal completion, and partial-create paths prove forced cleanup and auditable lifecycle evidence. | UNVERIFIED — gate closed |
| No host fallback | Provider/tool routing proves that no target command or Compose workload can fall back to the host. | UNVERIFIED — gate closed |

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

The exact Pilot command remains `TBD`. This preparation document does not
authorize execution.

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

`TBD` means no run evidence exists. URLs and manifest SHAs below are frozen input,
not observed results. Each row is populated only from its sole metric-bearing
attempt. If none exists, use `MISSING — no metric-bearing attempt` for attempt
identity and duration, apply the fixed failure/zero/no-convergence defaults, and
keep attempt facts exclusively in section 10.

| # | Repository | Manifest SHA | Actual SHA | Metric-bearing attempt ID | UTC start | UTC end | Repository monotonic duration | Exact command | Exit | Terminal outcome | Exact raw `stop_reason` | Failure class |
|---:|---|---|---|---|---|---|---|---|---:|---|---|---|
| 1 | `https://github.com/umami-software/umami` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 2 | `https://github.com/knadh/listmonk` | `670c01717d48647093335cc23a6be6f4b79c3b6b` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 3 | `https://github.com/dgtlmoon/changedetection.io` | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 4 | `https://github.com/louislam/uptime-kuma` | `a852e21eba4ecf339624b404518c5bc7fad6d45c` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 5 | `https://github.com/muety/wakapi` | `8143ca13ade1c14959be8a79c1d736d9889013e4` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 6 | `https://github.com/sissbruecker/linkding` | `65813a75404b1319aca8b09700fadc0b15adabaf` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 7 | `https://github.com/paperless-ngx/paperless-ngx` | `6f3945f11f1ff13ab90da76c26de37660ff1d497` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 8 | `https://github.com/n8n-io/n8n-hosting` | `6b78193475d84ae190622d8a8e9ba8598e89b7d1` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 9 | `https://github.com/netbox-community/netbox-docker` | `5adc62fe3fa65163c4ef63733bdcbd3e59b5c544` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| 10 | `https://github.com/louislam/dockge` | `f809ae192b571944ad773e9866d3e67064ae8043` | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

### Report, journey, experiment, and cleanup evidence

These fields also come only from the sole metric-bearing attempt. No
metric-bearing attempt means no report/baseline evidence, experiment count `0`,
meaningful convergence `NO`, and metric-bearing cleanup `N/A`; audit evidence
remains available in section 10. The cleanup value in this repository-level table
supports autonomous-success derivation only and is not the aggregate cleanup
denominator.

| # | Report/artifact references | Baseline journey definitions/results | Experiment count | Meaningful convergence | Lifecycle JSONL references | Metric-bearing cleanup result |
|---:|---|---|---:|---|---|---|
| 1 | TBD | TBD | TBD | TBD | TBD | TBD |
| 2 | TBD | TBD | TBD | TBD | TBD | TBD |
| 3 | TBD | TBD | TBD | TBD | TBD | TBD |
| 4 | TBD | TBD | TBD | TBD | TBD | TBD |
| 5 | TBD | TBD | TBD | TBD | TBD | TBD |
| 6 | TBD | TBD | TBD | TBD | TBD | TBD |
| 7 | TBD | TBD | TBD | TBD | TBD | TBD |
| 8 | TBD | TBD | TBD | TBD | TBD | TBD |
| 9 | TBD | TBD | TBD | TBD | TBD | TBD |
| 10 | TBD | TBD | TBD | TBD | TBD | TBD |

### Mutation evidence

Retain one row for every attempted experiment; do not summarize away `ROLLBACK` or
`STOP`. Only mutation rows whose `Attempt ID` equals the repository's sole
metric-bearing attempt ID participate in experiment count and meaningful
convergence. All other mutation rows are audit evidence only and cannot alter
either aggregate.

| Repo # | Attempt ID | Experiment ID | Mutation type/service/params | Parent hash | Candidate hash | Boot | Regression journeys | Verdict (`KEEP`/`ROLLBACK`/`STOP`) | Exact reason | Meaningful? | Evidence references |
|---:|---|---|---|---|---|---|---|---|---|---|---|
| TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

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
| TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

## 7. Failure taxonomy

Always preserve exact raw `stop_reason` separately from the normalized class for
every attempt. Attempt-level classes use the taxonomy below. Repository rows and
aggregate failure-class counts use only the sole metric-bearing attempt; a
repository with none has the derived state `no metric-bearing attempt` and is not
post-hoc assigned one of these attempt failure classes.

| Failure class | Classification boundary | Count | Run/attempt references |
|---|---|---:|---|
| intake | URL/ref/clone/pin/manifest-SHA identity failure before workload execution | TBD | TBD |
| Compose discovery/parse | Compose path discovery, loading, or validation failure | TBD | TBD |
| sandbox unsupported | Required provider or observability capability is unsupported | TBD | TBD |
| create/boot | Sandbox creation or workload boot failure | TBD | TBD |
| baseline journey | Non-empty deterministic baseline journey execution/verification failure | TBD | TBD |
| experiment | Mutation application, candidate boot, replay, or decision failure | TBD | TBD |
| whole-trial timeout | Whole-duration deadline terminates the attempt | TBD | TBD |
| cleanup | Required destroy/retry cleanup fails | TBD | TBD |
| internal | RepoTrial internal failure not classified above | TBD | TBD |
| unknown | Evidence cannot support a more specific class | TBD | TBD |

## 8. Aggregate metrics and thresholds

Repository outcome, convergence, experiment, failure-class, and timing aggregates
use the ten frozen repositories and derive from each sole metric-bearing attempt or
the frozen no-metric-bearing defaults. Failed metric-bearing attempts and
repositories with no metric-bearing attempt contribute `0` experiments;
survivor-only averages are prohibited. Exact-stop-reason coverage and aggregate
cleanup are all-attempt safety gates and use every retained attempt instead.

| Metric | Exact calculation | Result | M7.5 threshold/status |
|---|---|---|---|
| Autonomous successes | Count whose metric-bearing attempt satisfies every autonomous-success condition; none means failure; denominator 10 | TBD | At least `7/10` |
| Meaningful convergences | Count whose metric-bearing attempt has at least one meaningful regression-passing `KEEP`; none means no convergence; denominator 10 | TBD | At least `5/10` |
| Exact stop-reason coverage | All retained failed attempts with exact raw `stop_reason` / all retained failed attempts | TBD | `100%` |
| Average experiments | Sum of metric-bearing experiment counts for all ten repositories / `10`; no metric-bearing/no-experiment adds zero | TBD | Report only |
| Aggregate cleanup | Successful cleanup obligations across all retained attempts / all cleanup obligations across all retained attempts; `N/A` has no obligation and any `UNKNOWN` makes the result `UNKNOWN` | TBD | `100%` |
| P50 repository duration | Nearest-rank over metric-bearing repository durations, section 9 | TBD | Report only |
| P95 repository duration | Nearest-rank over metric-bearing repository durations, section 9 | TBD | Report only |

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
| 1 | TBD | TBD | TBD | TBD |
| 2 | TBD | TBD | TBD | TBD |
| 3 | TBD | TBD | TBD | TBD |
| 4 | TBD | TBD | TBD | TBD |
| 5 | TBD | TBD | TBD | TBD |
| 6 | TBD | TBD | TBD | TBD |
| 7 | TBD | TBD | TBD | TBD |
| 8 | TBD | TBD | TBD | TBD |
| 9 | TBD | TBD | TBD | TBD |
| 10 | TBD | TBD | TBD | TBD |

## 10. Attempt ledger and infrastructure invalidation

Every attempted command remains in the audit trail. Populate one row in each table
per attempt using the same attempt ID. Attempt role and metric attribution are
derived by the frozen rules in section 1; there is no free-form post-hoc field that
can change metric inclusion.

| Repo # | Attempt ID | Attempt role (`primary`/`replacement`/`diagnostic`) | Metric attribution | Target workload started? | Actual SHA | UTC start | UTC end | Monotonic duration | Exact command | Exit | Terminal outcome | Exact raw `stop_reason` | Failure class |
|---:|---|---|---|---|---|---|---|---|---|---:|---|---|---|
| 1 | `4d487d62-e775-47f1-ac3d-2720309c3f6f` | `primary` | `audit-only` — verified pre-workload infrastructure invalidation | `false` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `2026-08-29T09:05:56.262880+00:00` | `2026-08-29T09:06:50.320499+00:00` | `54.04700000000594` | `uv run repotrial inspect https://github.com/umami-software/umami --provider docker-sbx --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 --container-port 3000 --compose-path docker-compose.yml` | `1` outer command; `4` logical evidence | `exception` | `internal:valueerror` | `internal` |
| 1 | `53bd0dda-93c2-493b-bed1-43c37a585dbf` | `replacement` | `audit-only` — verified pre-workload infrastructure invalidation | `false` | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `2026-08-29T09:23:55.105099+00:00` | `2026-08-29T09:24:56.031849+00:00` | `60.921999999991385` | `uv run repotrial inspect https://github.com/umami-software/umami --provider docker-sbx --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 --container-port 3000 --compose-path docker-compose.yml` | `1` outer command; `4` logical evidence | `exception` | `internal:cleanuperror` | `cleanup` |
| TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

Allowed terminal metric-attribution values are `audit-only` for a verified
pre-workload infrastructure invalidation, `metric-bearing` for the first attempt
that starts target workload, `diagnostic-only` after that binding, and
`no-metric pre-workload terminal` for a non-replaceable attempt that ends before
target workload starts.

| Repo # | Attempt ID | Report/artifact references | Lifecycle JSONL references | Derived attempt cleanup | Invalidation evidence and authorization | Notes |
|---:|---|---|---|---|---|---|
| 1 | `4d487d62-e775-47f1-ac3d-2720309c3f6f` | `artifacts/4d487d62-e775-47f1-ac3d-2720309c3f6f/attempt-result.json` | None — no lifecycle JSONL | `N/A` | Verified pre-workload infrastructure invalidation: relative artifact root produced mixed relative/absolute GraphContext paths; no sandbox/lifecycle was reached and `sbx list` was empty. Owner later authorized one replacement attempt. | `target_workload_started=false`; retain as audit-only and do not fill metric-bearing repository outcomes. The command runner observed process exit `1`; retained RepoTrial evidence records logical exit `4`, so both are preserved rather than silently reconciled. |
| 1 | `53bd0dda-93c2-493b-bed1-43c37a585dbf` | `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/attempt-result.json` | `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/evidence/baseline-d780b5f62220835d-0001-attempt-01/baseline-lifecycle.jsonl` | `FAIL` — `create_cleanup_unsafe` followed by `cleanup_retry_failure`; post-attempt `sbx list` was empty but does not override lifecycle evidence | Verified generic pre-workload compatibility invalidation: installed `sbx create --help` specifies `--cpus int`; the attempt passed `1.5` and failed before any daemon create request. A controlled provider reproduction retained exact stderr `invalid argument \"1.5\" for \"--cpus\" ... ParseInt`; integer CPU then passed `create -> exec echo ok -> destroy` against the same pinned workspace with no residue. Owner authorized generic TDD repair and continuation. | `target_workload_started=false`; retain as audit-only. Generic fix `cbc6eea0cb570f46929693a1f38cf7904cb2bf21` rejects fractional CPU policy and uses an integer default; scoped reviewer accepted it. The raw attempt stop reason remains exactly as originally captured. |
| TBD | TBD | TBD | TBD | TBD | TBD | TBD |

## 11. Pilot decision

**TBD — NOT RUN.** No release, continuation, or Kill Criteria conclusion exists.
Populate this section only from the complete retained evidence and calculations
defined above.
