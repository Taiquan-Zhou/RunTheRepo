# Release closeout — 2026-09-08

Status: technical GO for the limited 0.1 source preview described below;
no push or publication performed. This is not a stable/full-product release.
Web expansion is paused. Preserve exact SHA, isolated target execution,
deterministic verification, fail-closed cleanup, resource bounds and the explicit
PID limitation. Keep the original frozen 10-repository denominator.
Technical checks passed for a limited 0.1 source preview on a pre-provisioned
supported WSL/SBX host; no push/publication performed. Full/stable product scope
is not complete.

## Current bounded acceptance evidence — HEAD `7e30606dc64db7d7c5ba2754eedfcf2fbdd944f2`

The 0.1 output remains a limited CLI and local Web preview, not a stable release
or a claim that the full product scope is complete. The following two real-SBX
canaries are bounded acceptance evidence at the current HEAD; they are not a
publication gate or a replacement for the frozen ten-repository cohort.

| Repository | Verified commit | Run | Duration | Bounded result |
| --- | --- | --- | ---: | --- |
| Umami | `ca661c7057984aa98ed4f7083d84dae2f65bfcb0` | `e751fd28-94f9-4d90-9a3c-7aaa8f791b47` | 878.047519223s | `exit_code=0`; `no_remaining_mutations`; complete JSON/HTML; 6-step operator-authored authenticated HTTP Journey (anonymous rejection, login, create/read/delete/list confirmation); baseline plus 2 KEEP candidates, each 6/6; 6/6 `destroy_success`; template removal confirmed; official inventory empty |
| changedetection.io | `5d9c7c6da76340597243e8163c4f2439237fa0e8` | `3eb07f3a-8e90-4424-984e-bc79d7c22aec` | 661.436816016s | `exit_code=0`; `no_remaining_mutations`; complete JSON/HTML; `GET /` plus 2 KEEP candidates; 4/4 `destroy_success`; template removal confirmed; official inventory empty |

These are bounded real-SBX acceptance facts only. Umami covers the declared
authenticated test-account CRUD path; changedetection covers only `GET /`.
Neither establishes LLM contribution, the complete frozen 10-repository cohort,
real-target browser execution, or a passed publication gate. The current
supported runtime is Docker Sandboxes `v0.42.0`; `repotrial doctor` rejects
`v0.39.0`.

Bounded current-HEAD packaging checks passed under
`artifacts/release-7e30606d/`: lock consistency check, wheel/sdist build, fresh-venv
install with `pip check`, outside-source CLI help/dry-run, installed-file diff,
and doctor JSON readiness. These checks are not fresh-machine OS/SBX proof
or publication approval, and a
dry-run is not a real-wheel canary. The supported installation path is a source
checkout using `uv sync --locked --all-groups`; arbitrary `pip` dependency
combinations are not claimed as validated.

## Historical frozen cohort execution — HEAD `25a88527369779fab11221bfac5f7125e85d9bee`

At the historical `25a88527` HEAD, all ten entries of `eval/real_repos.yaml`
were executed in their original order;
manifest SHA-256: `4a963f0ee730ddf4be7243ad31cc2899ce19cdbfed3c4570498ff7d1c617551a`.
No failed entry was replaced. Each attempt verified its pinned source SHA.

| Repository | Run ID | Exit / stop reason | CLI elapsed (s) |
| --- | --- | --- | ---: |
| umami | `2260092e-382b-4669-9db7-d5c69d64150d` | 0 / `no_remaining_mutations` | 709.378 |
| listmonk | `f78c7329-d195-4080-ba7e-32dd54fc4438` | 0 / `no_remaining_mutations` | 526.581 |
| changedetection | `f3bd35cb-0a3f-4967-bb9c-a0a8607a3ef9` | 0 / `no_remaining_mutations` | 495.626 |
| uptime-kuma | `3966fcca-edcb-40cc-9da3-1140dedd38ed` | 0 / `no_remaining_mutations` | 479.169 |
| wakapi | `9f3ba400-5197-4483-8d56-79f2b492ae9e` | 0 / `no_remaining_mutations` | 975.124 |
| linkding | `0332ade9-eae8-431e-9927-d5e70af18e46` | 0 / `no_remaining_mutations` | 572.640 |
| paperless-ngx | `85a98f3d-f76b-4f5a-8e6a-424509582405` | 3 / `boot_recovery_stopped` | 496.892 |
| n8n-hosting | `b4d0a115-cf82-4e4e-b719-fcaeb65dc7e6` | 4 / `sandbox:exec:timeout` | 437.787 |
| netbox-docker | `2a08cddf-e01c-44db-b4cf-26de845c47ea` | 3 / `boot_recovery_stopped` | 432.550 |
| dockge | `5328ce5c-1165-4fb5-b04c-d24851be4695` | 0 / `no_remaining_mutations` | 564.793 |

This table is historical evidence, not current-HEAD acceptance. Seven attempts
completed with JSON/HTML reports and at least one KEEP. Their
observed journey coverage is only `GET /`; this does not prove application
business workflows. In particular, Dockge's socket-removal KEEP does not prove
that its container-management functionality survives. All ten attempts ended
with successful sandbox destruction, confirmed template removal and empty
official inventory. The historical n8n attempt had terminal exception evidence
but no JSON/HTML report. The later CLI handled-failure path now emits
terminal-failure reports for new failures; it does not backfill that historical
run or claim that n8n's timeout was fixed.
One independent evidence review confirmed autonomous completion **7/10** and
meaningful regression-passing hardening **7/10**, meeting the frozen 7/10 and
5/10 thresholds. Each hardening counted a principal privilege reduction (not
`add_tmpfs` alone), distinct parent/candidate hashes, full recorded baseline
replay and journey evidence references. This is acceptance under the frozen
single-GET journey contract, not a business-workflow preservation claim.

Failure evidence identifies Paperless's default application secret, n8n's
30-second `docker diff` observation timeout after a successful boot/GET, and
NetBox's dependency/readiness timeout while migrations were still running.
No global timeout was increased. CLI elapsed time includes intake and finalization;
the provider's shared 900-second sandbox budget starts at first sandbox creation,
so these are not whole-CLI 900-second guarantees.

## Completed: cumulative hardened output

- Persist baseline reference and typed baseline/final/artifact identity.
- Replay the recorded KEEP/ROLLBACK chain and export a multi-service diff;
  never relabel a full accepted Compose file as an overlay.
- Pure report projection; invalid or legacy provenance remains unavailable.
- Identity-qualified filenames, exclusive writes, partial-write cleanup and
  write-back verification; compatibility and runtime inputs remain separate.
- Luna implementation, Sol module review and scoped findings closure completed.
- Earlier module-stage regression: 2155 passed, 12 skipped, 1 warning, followed
  by focused write-integrity checks; this is historical evidence.
- Current-HEAD fresh full regression: 2238 passed, 12 skipped, 1 warning in
  194.85 seconds; branch coverage 85.31%. All-file pre-commit, Ruff/format
  (163 files), mypy (55 source files), and diff checks passed.
- Trusted real SBX Compose-config equivalence smoke passed across services,
  reset fields and overridden lists. Sandbox
  `repotrial-overlay-merge-smoke-0fc3c18d4477` was destroyed successfully and
  official inventory was empty. No application image was pulled or started.

## Completed: operator-authored business Journey input

- `inspect --journeys-file PATH` reads a bounded, validated snapshot before
  intake and preserves the pinned repository. Existing baseline/candidate
  execution replays that snapshot; no graph or sandbox bypass was introduced.
- Reports identify operator-authored input with raw/canonical SHA-256 hashes;
  it is not autonomous or LLM-generated coverage. Legacy report shape is retained.
- A trusted loopback HTTP CRUD fixture verifies POST/GET/DELETE/GET assertions,
  precedence over repository declarations and baseline/candidate replay.
  This is not a public-repository or real-SBX canary.
- Luna implementation and one Sol module review completed without blockers.
- Final-source full regression: 2174 passed, 12 skipped, 1 dependency
  deprecation warning in 141.99 seconds; branch coverage 85.15% (85% gate).
  Ruff, format (160 files), mypy (53 source files), scoped pre-commit and
  diff checks passed.
- Wheel/sdist built; installed CLI valid dry-run and invalid-input refusal
  checked in an isolated Python environment. This is not clean-machine proof.

## Completed: bounded bearer Journey support and canaries

- Operator-authored HTTP steps may use exactly one bounded declaration:
  `{"auth":{"capture_bearer":"token"}}` on a successful login response or
  `{"auth":{"use_bearer":true}}` on a later explicit step. The token is
  ephemeral, per-Journey, and absent from reports, evidence, stdout, and the
  Journey payload; arbitrary headers, cookies and environment interpolation
  remain unsupported. The real proof is operator-authored; model proposals
  retain their GET-only policy.
- The Umami canary above used only disposable test-account credentials. Its
  six steps were anonymous rejection, login, create, read, delete, and list
  confirmation; baseline and both KEEP candidates passed all six steps.

## Remaining, in order

1. Preserve the historical frozen cohort denominator and its 7/10 autonomous
   completion plus 7/10 meaningful regression-passing hardening as historical
   single-`GET /` evidence; do not present it as current business-workflow
   coverage.
2. Owner controls publication and the choice of any open-source license.
   Keep source `uv sync --locked --all-groups` as the supported setup; full
   fresh-machine OS/SBX prerequisite installation remains unverified.
3. Real-target browser execution and accepted LLM contribution remain unproven
   product capabilities; explicitly resolve their release scope, never claim
   trusted UI/fixture smoke demonstrates them.

The current Umami and changedetection canaries are at HEAD `7e30606d`; older
`25a88527` and `7808b9b5` runs remain historical evidence. No release readiness
claim follows from these bounded checks alone.
