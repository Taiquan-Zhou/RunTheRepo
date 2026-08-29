# M7.5 real-repository Pilot protocol and report

> **NOT RUN — cohort and protocol frozen; no pilot result exists yet**

This document freezes the M7.5 execution protocol and empty report structure. It
contains preparation findings and fail-closed gates, not Pilot outcomes. No
candidate repository has been started, and no pass, failure, or metric is claimed.

## 1. Immutable cohort

- Manifest: `eval/real_repos.yaml`, schema version `1`, frozen `2026-08-29`.
- Cohort identity is the ordered tuple of each manifest `url` and `commit_sha`.
- Manifest list order is execution order and the only order used by the tables.
- All ten repositories stay in every denominator. A repository cannot be dropped,
  replaced, or reordered after any attempt or result is observed.
- Repo-specific workarounds are prohibited. A later generic product change must be
  reviewed and tested outside this preparation task before a new Pilot is frozen.
- Each repository receives one primary run. An attempt invalidated by infrastructure
  is retained as evidence and linked to any authorized replacement attempt; it is
  never erased or silently reclassified.

README remains unchanged until real Pilot metrics exist.

## 2. Fail-closed pre-execution gates

Execution is prohibited until every gate below is independently verified as
`PASS` against the exact build and environment selected for the Pilot. `TBD`,
`UNKNOWN`, `UNSUPPORTED`, or missing evidence keeps the gate closed.

| Gate | Required proof before any target workload | Preparation status |
|---|---|---|
| PID hard bound | Effective sandbox PID limit is a verified hard bound, including failure behavior. | UNVERIFIED — gate closed |
| Sandbox health | Current provider health and required isolation capabilities pass immediately before execution. | UNVERIFIED — gate closed |
| Immutable repository identity | An official, generic route accepts/enforces the manifest SHA, and actual `HEAD^{commit}` equality is proven before target workload execution. | BLOCKED — current CLI has no immutable-ref input |
| Exact failure reason | Every failed attempt exposes and preserves the exact `RunState.stop_reason` in collected evidence. | BLOCKED — current report/CLI projection is insufficient |
| Resource bounds | Effective CPU, memory, PID, disk, and whole-duration limits are all verified. | UNVERIFIED — gate closed |
| Forced cleanup | Cancellation, timeout, normal completion, and partial-create paths prove forced cleanup and auditable lifecycle evidence. | UNVERIFIED — gate closed |
| No host fallback | Provider/tool routing proves that no target command or Compose workload can fall back to the host. | UNVERIFIED — gate closed |

### Current preparation findings (execution gates, not Pilot outcomes)

1. `repotrial inspect` currently accepts a repository URL but exposes no
   immutable-ref input. Repository intake can pin a `requested_ref` internally,
   but that does not provide an operator-visible, verified manifest-SHA route.
   Execution must not start until an official, generic route proves the exact
   manifest SHA before any target workload. This protocol invents no CLI flag.
2. `RunState.stop_reason` exists, but the current report projection omits it and
   the CLI does not provide the complete exact failure evidence required here.
   Execution must not start until exact stop-reason capture is proven.

Neither finding is fixed or bypassed by this preparation task.

## 3. Execution protocol

For each manifest entry, in order:

1. Confirm all pre-execution gates are `PASS` and archive their evidence.
2. Start an external monotonic timer and record UTC start time before the approved
   command is invoked. Record the exact command verbatim; do not add an invented
   immutable-ref option.
3. Through the approved generic intake route, verify the checked-out
   `HEAD^{commit}` equals the manifest SHA before any target workload executes.
   A mismatch terminates the attempt and is classified as `intake`.
4. Run exactly one primary trial through `SandboxProvider`; never execute target
   Compose on the host and never fall back to a host command path.
5. Persist report paths, artifact references, deterministic baseline journey
   definitions/results, every experiment/mutation, and every lifecycle JSONL file.
6. Record terminal outcome, process exit code, exact raw `stop_reason`, UTC end
   time, and external monotonic duration for every terminal path.
7. Derive cleanup only from lifecycle JSONL under the rules in section 6. Preserve
   raw unsupported/not-observed evidence; never convert it to success.
8. Stop the external timer only after required cleanup has reached a terminal
   state. Retain infrastructure-invalidated attempts and explain any authorized
   rerun in the deviations table.

The exact Pilot command is `TBD` until the immutable-identity gate has an approved
generic route. This preparation document does not authorize execution.

## 4. Outcome definitions

### Autonomous success

A repository is an autonomous success only when all of the following are true:

- terminal outcome is `completed` and the captured process exit code is `0`;
- `trial-report.json` exists (with `trial-report.html` captured when produced);
- report/intake identity SHA exactly equals the manifest SHA;
- a non-empty deterministic baseline journey set exists and every required
  baseline journey passes;
- every cleanup obligation derived from lifecycle JSONL succeeds.

Any missing, `UNKNOWN`, `UNSUPPORTED`, or failed condition means the repository is
not counted as an autonomous success.

### Meaningful regression-passing convergence

A repository meaningfully converges only when at least one `KEEP` mutation is
backed by replay of the complete passing baseline journey set and evidence shows
that it removes or reduces privilege. The classification and evidence reference
must be recorded per mutation. `add_tmpfs` by itself is not meaningful.

`ROLLBACK`, `STOP`, unsupported collectors, and not-observed behavior remain raw
evidence and cannot be converted into a meaningful convergence or success.

## 5. Per-repository capture

`TBD` means no run evidence exists. URLs and manifest SHAs below are frozen input,
not observed results.

| # | Repository | Manifest SHA | Actual SHA | Run ID | UTC start | UTC end | Monotonic duration | Exact command | Exit | Terminal outcome | Exact raw `stop_reason` | Failure class |
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

| # | Report/artifact references | Baseline journey definitions/results | Experiment count | Meaningful convergence | Lifecycle JSONL references | Cleanup result |
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

Add one row per attempted experiment; do not summarize away `ROLLBACK` or `STOP`.

| Repo # | Experiment ID | Mutation type/service/params | Parent hash | Candidate hash | Boot | Regression journeys | Verdict (`KEEP`/`ROLLBACK`/`STOP`) | Exact reason | Meaningful? | Evidence references |
|---:|---|---|---|---|---|---|---|---|---|---|
| TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

## 6. Cleanup derivation

Derive each sandbox lifecycle from its JSONL events; do not infer cleanup from a
process exit or report file.

- `create_success` requires a later `destroy_success` for the same owned sandbox
  ID and no `destroy_failure`; otherwise cleanup is `FAIL` or `UNKNOWN`.
- `create_cleanup_unsafe` requires `cleanup_retry_success`. A
  `cleanup_retry_failure` is `FAIL`.
- `create_failure` before any owned sandbox ID is `N/A`.
- Missing, malformed, or incomplete lifecycle evidence is `UNKNOWN`.
- A repository cleanup result is `FAIL` if any required lifecycle fails,
  `UNKNOWN` if none fails but required evidence is unknown, `PASS` only if all
  required cleanups succeed, and `N/A` only when no sandbox ID was ever owned.

Cleanup percentage is successful required cleanups divided by all terminal
cleanup obligations; `N/A` has no obligation. Any `UNKNOWN` makes the aggregate
cleanup metric `UNKNOWN`. The M7.5 cleanup target is `100%`.

| Repo # | Lifecycle reference | Create state / owned ID | Required terminal event | Observed terminal event | Derived cleanup | Notes |
|---:|---|---|---|---|---|---|
| TBD | TBD | TBD | TBD | TBD | TBD | TBD |

## 7. Failure taxonomy

Always preserve exact raw `stop_reason` separately from the normalized class.

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

All aggregate denominators use the ten frozen repositories. Failed runs and runs
with no experiments contribute `0` experiments to the average; survivor-only
averages are prohibited.

| Metric | Exact calculation | Result | M7.5 threshold/status |
|---|---|---|---|
| Autonomous successes | Count satisfying every autonomous-success condition, denominator 10 | TBD | At least `7/10` |
| Meaningful convergences | Count with at least one meaningful regression-passing `KEEP`, denominator 10 | TBD | At least `5/10` |
| Exact stop-reason coverage | Failed attempts with exact raw `stop_reason` / all failed attempts | TBD | `100%` |
| Average experiments | Sum of experiment counts for all ten repositories / `10`; failed/no-experiment runs add zero | TBD | Report only |
| Cleanup | Section 6 derivation across all cleanup obligations | TBD | `100%` |
| P50 monotonic duration | Nearest-rank, section 9 | TBD | Report only |
| P95 monotonic duration | Nearest-rank, section 9 | TBD | Report only |

The release-style Before/After demo threshold is met only if autonomous successes
are at least `7/10`, meaningful convergences are at least `5/10`, every failure has
an exact stop reason, and cleanup reaches `100%`. Otherwise report the unmet gate.

If fewer than `5/10` repositories run autonomously and failures are mainly caused
by project heterogeneity, perform the M7.5 Kill Criteria analysis. `5-6`
autonomous successes, or only `3-4` meaningful convergences, are continuation
zones that miss the release threshold without automatically triggering that kill
condition.

## 9. Timing

Use external monotonic whole-attempt durations for all ten repositories, including
failed attempts and cleanup. Sort all ten durations ascending. With `n = 10`,
nearest-rank P50 is rank `ceil(0.50 * 10) = 5`, and P95 is rank
`ceil(0.95 * 10) = 10`. If any duration is missing, both timing aggregates are
`UNKNOWN`; do not silently exclude that repository.

| Repo # | External monotonic duration | Included in all-ten ordering? | Evidence |
|---:|---|---|---|
| 1 | TBD | TBD | TBD |
| 2 | TBD | TBD | TBD |
| 3 | TBD | TBD | TBD |
| 4 | TBD | TBD | TBD |
| 5 | TBD | TBD | TBD |
| 6 | TBD | TBD | TBD |
| 7 | TBD | TBD | TBD |
| 8 | TBD | TBD | TBD |
| 9 | TBD | TBD | TBD |
| 10 | TBD | TBD | TBD |

## 10. Deviations and infrastructure-invalidated attempts

Every attempted command remains in the audit trail. A replacement attempt requires
explicit authorization and a reason; it does not overwrite the primary record or
change the frozen cohort denominator.

| Repo # | Attempt/run ID | Primary or replacement | Infrastructure invalidation evidence | Authorization/reason | Effect on metrics |
|---:|---|---|---|---|---|
| TBD | TBD | TBD | TBD | TBD | TBD |

## 11. Pilot decision

**TBD — NOT RUN.** No release, continuation, or Kill Criteria conclusion exists.
Populate this section only from the complete retained evidence and calculations
defined above.
