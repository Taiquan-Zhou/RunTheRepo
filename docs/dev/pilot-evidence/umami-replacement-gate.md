# Umami replacement-attempt evidence gate

This record closes the audit chain for the two pre-workload Umami
infrastructure invalidations. It does not alter their original attempt artifacts,
stop reasons, or lifecycle events. Commands and observations below were produced
on the same Windows host and SBX v0.39.0 environment as the Pilot.

## Owner authorization record

The Owner instruction that authorized execution from unified RepoTrial HEAD
`31b8336d4890e761a13314d65f893ade39c0382b` explicitly required:

- preserve `4d487d62-e775-47f1-ac3d-2720309c3f6f` as `audit-only` for a verified
  pre-workload infrastructure invalidation;
- execute a replacement Umami attempt without deleting or overwriting history;
- bind the first attempt that actually starts target workload as the sole
  metric-bearing attempt, regardless of PASS/FAIL/UNSUPPORTED;
- when a new generic RepoTrial bug is found, retain the failed attempt and exact
  stop reason, apply a TDD minimal fix, review/commit it, and continue;
- never use a repository-specific workaround or change the frozen manifest/SHA.

This is a controller transcription of the Owner instruction in the active Codex
task. Runtime artifacts cannot encode authorization; this committed audit record
is the local authorization reference.

## Attempt 1: path-domain invalidation

- Attempt: `4d487d62-e775-47f1-ac3d-2720309c3f6f`.
- Original evidence:
  `artifacts/4d487d62-e775-47f1-ac3d-2720309c3f6f/attempt-result.json`.
- The artifact verifies the manifest SHA and ends with `internal:valueerror`.
- No lifecycle JSONL exists, so no sandbox/workload boundary was reached.
- Generic fix: commit `31b8336d4890e761a13314d65f893ade39c0382b`,
  `fix: normalize CLI sandbox workspace paths`.

That commit resolves the run layout once and passes absolute `workspace`,
`artifact_dir`, `overlay_dir`, and `accepted_compose_dir` paths into
`GraphContext`. Its regression test is
`test_relative_artifacts_root_uses_absolute_graph_context_paths`, which invokes
the real graph from a relative artifact root and verifies the full context path
domain and cleanup. The commit itself is the immutable root-cause/fix evidence:

```text
git show 31b8336d4890e761a13314d65f893ade39c0382b -- \
  src/repotrial/cli.py tests/integration/test_cli_e2e_fixture.py
```

The original post-attempt empty `sbx list` was observed by the controller but was
not machine-persisted in that attempt directory. The stronger boundary evidence
is the absence of any lifecycle file combined with the pre-provider ValueError
and the focused regression commit.

## Attempt 2: SBX integer-CPU invalidation

- Attempt: `53bd0dda-93c2-493b-bed1-43c37a585dbf`.
- Original evidence:
  `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/attempt-result.json`.
- Lifecycle:
  `artifacts/53bd0dda-93c2-493b-bed1-43c37a585dbf/evidence/baseline-d780b5f62220835d-0001-attempt-01/baseline-lifecycle.jsonl`.
- The lifecycle has no `create_success`; it records
  `create_cleanup_unsafe -> cleanup_retry_failure`. Target workload therefore did
  not start. The cleanup obligation remains `FAIL` under the frozen rules even
  though inventory was empty afterward.

Installed official help states:

```text
--cpus int    Number of CPUs to allocate to the sandbox (0 = auto: all host CPUs)
```

The failing parser condition was reproduced against the same pinned Umami
workspace before and after the command:

```text
> sbx list
No sandboxes found.

> sbx create --name repotrial-umami-cpu-evidence --clone \
    --cpus 1.5 --memory 1024m shell <pinned-umami-workspace>
ERROR: invalid argument "1.5" for "--cpus" flag: strconv.ParseInt: parsing "1.5": invalid syntax
exit code: 1

> sbx list
No sandboxes found.
```

A single-variable control with integer CPU against that same workspace passed
`DockerSbxProvider.create -> exec echo ok -> destroy`, followed by an empty
inventory. The generic TDD fix is commit
`cbc6eea0cb570f46929693a1f38cf7904cb2bf21`,
`fix: use supported integer sbx cpu defaults`. It rejects fractional policy input,
uses the stricter one-CPU default, and contains no rounding/truncation or
repository-specific behavior. Focused tests, ruff, mypy, a real provider smoke,
and an independent scoped review passed.

## Attribution ruling

Both failures occurred before `create_success`/target workload execution and have
generic, independently inspectable fix commits. The Owner authorization above
therefore permits continuation. Attempt
`70313f8c-9ea6-458a-aad0-89c133fc5ec4` is the first Umami attempt whose lifecycle
contains `create_success`; it is irrevocably the sole metric-bearing Umami attempt.
The two earlier attempts remain audit-only and continue to contribute their exact
stop reasons and cleanup obligations to all-attempt safety metrics.
