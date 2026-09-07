# Task 5A report: provider-owned bounded image-bundle streaming

## Status

Implemented provider boundary and focused unit coverage. Existing uncommitted
design/plan docs and Task 5B trial test were left untouched.

## RED

The first retained RED command was
`.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py -k
stage_runtime_image_bundle`; it produced **1 failed, 247 deselected** with
`RuntimeError: runtime image bundles are unsupported`. After adding the
focused tests, `.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py
-k 'runtime_template or image_bundle'` produced **10 failed, 26 passed,
221 deselected**, all from the missing provider implementation/residue guard.

## GREEN and implementation

- Added the typed default `SandboxProvider.stage_runtime_image_bundle` contract.
- Added Docker provider validation for canonical option-safe references,
  sorted/unique inputs, and full lowercase `sha256:<64 hex>` IDs.
- Added one-shot provider-owned 0600 temporary bundle files, bounded chunked
  export, SHA-256/byte accounting, stderr overflow handling, and process
  cleanup.
- Added exact direct-argv `docker image load` streaming into each template
  workload sandbox, with deadline, hash, size, replacement, and cleanup checks.
- Added bounded bundle hash/size audit fields and dual template/bundle
  finalization behavior that retains unconfirmed ownership state.

## Verification

- `pytest ... -k 'runtime_template or image_bundle'`: **39 passed, 232 deselected**
- `ruff check src/repotrial/sandbox tests/unit/sandbox`: **passed**
- `ruff format --check src/repotrial/sandbox tests/unit/sandbox`: **passed**
- `mypy src/repotrial/sandbox`: **passed**
- `git diff --check`: **passed**
- Additional scoped regression: both sandbox unit files **271 passed**.

## Concerns

No real sandbox, daemon, network, API key, full pytest, coverage, or
pre-commit command was used. Task 5B still owns trial/image-template
integration and must verify its caller ordering against this provider boundary.

## Fix round 1

Reviewer findings were reproduced with tests added before production changes.
At base `1e4aca6f`, `pytest -q tests/unit/sandbox/test_docker_sbx_commands.py
-k 'runtime_template or bundle'` produced **4 failed, 36 passed, 222
deselected** (active template without bundle, cleanup retry, audit retention,
and the adjusted create/finalize test). The temp-root regression command
`pytest -q tests/unit/sandbox/test_docker_sbx_commands.py -k temp_root`
produced **1 failed, 261 deselected**. The failures matched the intended
production gaps.

The fix models template cleanup confirmation separately from bundle ownership,
requires a complete bundle for active templates, retains bundle audit identity
after successful finalization, validates the temporary root, and takes
ownership of the path/fd immediately after creation. The focused fix suite
then passed: **41 passed, 221 deselected** for
`-k 'runtime_template or bundle or temp_root'`.

Reviewer-reported `/tmp/repotrial-image-bundle-*` residues were first listed
and verified as 23 regular mode-0600 non-symlink files directly under `/tmp`,
then those exact files were deleted with max-depth-one matching; no other
paths were removed.

Post-gate status also exposed one exact workspace-level
`repotrial-image-bundle-umidrhut` residue; it was verified as a regular
mode-0600 non-symlink file and removed by exact pathname.

## Fix round 2

At fix-round-2 HEAD, the required minimal regression command was
`.venv/bin/uv run pytest -q tests/unit/sandbox/test_docker_sbx_commands.py
-k 'bundle or temp_root'`: **15 passed, 1 failed, 249 deselected**. The
failure was the mixed template-failure/bundle-success audit regression; the
close-success/unlink-failure retry and pre-identity fstat tests were already
passing at that HEAD, so no RED is claimed for those two cases.

The minimal fix preserves independent bundle ownership after close, retries
only the exact owned path, retains bundle hash/size through mixed cleanup,
closes and unlinks a newly-owned path even when fstat fails, binds the temp
root check to the resolved warmup workspace, and rejects short image aliases
such as `alpine:latest`. The focused gate is now **41 passed, 238
deselected**; both sandbox unit files are **278 passed**.

Round-2 verification: `ruff check src/repotrial/sandbox tests/unit/sandbox`,
`ruff format --check src/repotrial/sandbox tests/unit/sandbox`, `mypy
src/repotrial/sandbox`, and `git diff --check` all passed. Before the final
gate, 18 `/tmp/repotrial-image-bundle-*` paths were individually verified by
`stat` as direct regular mode-0600 non-symlink files (sizes 17 or 22), then
removed by exact max-depth-one path; the subsequent matching-path check was
empty (0 matches). No recursive deletion was used.

After the fix commit, the fresh provider focused gate remained **41 passed,
238 deselected**, and the two sandbox unit files passed **279 tests**. Fresh
post-commit ruff check, format check, mypy sandbox, and diff-check all passed;
the post-test `/tmp` and repository-root residue scans both returned 0 paths.
