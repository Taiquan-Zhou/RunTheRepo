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
