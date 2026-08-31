# DeepSeek Portable Structured-Output Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing OpenAI-compatible adapter reach its already-valid portable JSON path when an otherwise accessible endpoint rejects strict `response_format=json_schema`, then prove the unchanged RepoTrial framework with DeepSeek before returning to broader model-schema work.

**Architecture:** Keep `ModelAdapter.structured()` and every domain/runtime contract unchanged. The adapter continues to try strict JSON Schema first, classifies only a bounded 400/422 `response_format` capability failure, and performs at most one existing JSON-prompt fallback whose output must pass strict Pydantic validation. Qwen and the private Journey transport-schema redesign are explicitly deferred to a separate plan.

**Tech Stack:** Python 3.12, Pydantic v2, httpx, pytest, existing `OpenAICompatibleModelAdapter`, DeepSeek OpenAI-compatible Chat Completions, Docker Sandboxes v0.39.0.

**Spec:** `docs/superpowers/specs/2026-08-31-model-boundary-portability-design.md`

## Global Constraints

- Worktree: `D:\A all code\RepoTrial-m7.5-compatibility-recovery`.
- Branch: `codex/m7.5-compatibility-recovery`.
- Plan base: `8941fbb` (`docs: tighten portable model boundary`).
- This phase implements only provider-neutral structured-output fallback and DeepSeek verification.
- Do not modify `src/repotrial/trial/planner.py` or Journey transport types in this phase.
- Do not run, tune, download, remove, or evaluate Qwen in this phase.
- Do not add provider names, endpoint names, or model names to production code.
- The complete `structured()` call may issue at most two HTTP requests.
- Do not fallback for authentication, authorization, invalid model, quota, rate limit, context limit, timeout, transport, redirect, response-size, or 5xx failures.
- Portable output must remain full-document JSON parsed by Pydantic. Do not strip fences, extract substrings, repair fields, coerce values, or add defaults.
- Keep credential-free errors and existing `ModelAdapterError.reason_code` values unchanged.
- Never print, persist, hash into repository evidence, or pass on a command line the value of `REPOTRIAL_MODEL_API_KEY`.
- Do not modify the frozen manifest, repository order, commit SHAs, SandboxProvider, Disk, PID, total-duration, LangGraph, RunState/checkpoint, report/API/CLI semantics, Journey verifier, experiments, mutations, or KEEP/ROLLBACK.
- Do not start, stop, restart, or reset the SBX daemon.
- Use `uv run --isolated ...` for Python quality gates; this avoids the incomplete cross-WSL `.venv` in the current worktree. The command was verified to import RepoTrial from this exact worktree.
- Implementation subagent: `gpt-5.6-luna`, reasoning `max`. Independent review subagent: `gpt-5.6-sol`, reasoning `high`.

---

### Task 1: Add a provider-neutral, two-request portable fallback

**Files:**

- Modify: `tests/unit/models/test_openai_compat.py`
- Modify: `src/repotrial/models/openai_compat.py`

**Interfaces:**

- Consumes: `OpenAICompatibleModelAdapter.structured(*, system: str, user: str, schema: type[ModelT]) -> ModelT`.
- Consumes: existing `_json_schema_payload()`, `_json_only_payload()`, `_error_details()`, and `_validated_result()` helpers.
- Produces: unchanged public adapter interface with a hard two-request maximum and broader provider-neutral capability classification.
- Preserves: existing strict-success path, strict invalid-content one-retry path, endpoint normalization, bounded responses, cancellation, redirect refusal, and credential-free errors.

- [ ] **Step 1: Add a RED test for a generic request error that names `response_format`**

Add this focused regression beside the existing explicit-schema fallback test:

```python
def test_generic_response_format_rejection_uses_one_json_only_fallback() -> None:
    with _ResponseServer(
        [
            (
                400,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                        "message": "response_format is unavailable for this request",
                    }
                },
            ),
            (200, _chat_completion('{"answer":"portable"}')),
        ]
    ) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")

        result = asyncio.run(
            adapter.structured(system="system", user="user", schema=_Answer)
        )

    assert result == _Answer(answer="portable")
    assert len(server.requests) == 2
    assert "response_format" in server.requests[0][1]
    assert "response_format" not in server.requests[1][1]
```

- [ ] **Step 2: Run the new test and observe the intended RED**

Run:

```powershell
uv run --isolated pytest -q tests/unit/models/test_openai_compat.py::test_generic_response_format_rejection_uses_one_json_only_fallback
```

Expected: FAIL with `ModelAdapterError: model request failed`; exactly one request was made because the current classifier requires a capability-specific `code` or `param`.

- [ ] **Step 3: Add RED tests for forbidden generic classifications and the global request ceiling**

Add parametrized cases in which the status remains 400/422 and the message also mentions `response_format`, but message/type/code/param identify one of:

```python
@pytest.mark.parametrize(
    "error",
    [
        {
            "type": "invalid_request_error",
            "message": "invalid API key for response_format",
        },
        {"code": "model_not_found", "message": "model cannot use response_format"},
        {"code": "insufficient_quota", "message": "quota blocks response_format"},
        {"type": "rate_limit_error", "message": "rate limit for response_format"},
        {
            "code": "context_length_exceeded",
            "message": "context limit in response_format request",
        },
    ],
)
def test_generic_non_capability_classification_never_falls_back(
    error: dict[str, str],
) -> None:
    with _ResponseServer([(400, {"error": error})]) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
    assert len(server.requests) == 1
```

Add both request-ceiling branches:

```python
def test_invalid_portable_fallback_stops_after_two_requests() -> None:
    responses = [
        (
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "response_format unavailable",
                }
            },
        ),
        (200, _chat_completion('{"answer":1}')),
    ]
    with _ResponseServer(responses) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        with pytest.raises(ModelAdapterError, match="invalid structured response"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
    assert len(server.requests) == 2


def test_strict_retry_cannot_turn_into_a_third_fallback_request() -> None:
    responses = [
        (200, _chat_completion('{"answer":1}')),
        (
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "response_format unavailable",
                }
            },
        ),
    ]
    with _ResponseServer(responses) as server:
        adapter = OpenAICompatibleModelAdapter(server.endpoint, "local-model")
        with pytest.raises(ModelAdapterError, match="model request failed"):
            asyncio.run(
                adapter.structured(system="system", user="user", schema=_Answer)
            )
    assert len(server.requests) == 2
```

- [ ] **Step 4: Run all new fallback tests and observe RED only where behavior is missing**

Run:

```powershell
uv run --isolated pytest -q tests/unit/models/test_openai_compat.py -k "generic_response_format or generic_non_capability or invalid_portable or third_fallback"
```

Expected: the generic capability case fails before implementation; forbidden cases remain fail-closed; both request-ceiling tests prove or expose the current branch behavior without permitting a third request.

- [ ] **Step 5: Implement the minimal classifier correction**

Keep `structured()` control flow and `_json_only_payload()` unchanged. Replace the requirement that one of code/param repeat `response_format` with a closed classification that:

```python
_FORBIDDEN_FALLBACK_MARKERS: Final = frozenset(
    {
        "authentication",
        "authorization",
        "unauthorized",
        "forbidden",
        "api_key",
        "api key",
        "invalid_model",
        "model_not_found",
        "insufficient_quota",
        "quota",
        "rate_limit",
        "rate limit",
        "context_length",
        "context limit",
    }
)
```

Add these exact helpers and route `_explicitly_unsupported_schema()` through them:

```python
def _is_response_format_capability_rejection(
    message: str,
    error_type: str | None,
    error_code: str | None,
    error_param: str | None,
) -> bool:
    normalized = tuple(
        value.lower()
        for value in (message, error_type, error_code, error_param)
        if value is not None
    )
    if any(
        marker in value
        for value in normalized
        for marker in _FORBIDDEN_FALLBACK_MARKERS
    ):
        return False
    if "response_format" not in message.lower():
        return False
    if not _is_generic_or_schema_capability(error_type):
        return False
    if not _is_generic_or_schema_capability(error_code):
        return False
    return error_param is None or _is_schema_capability_discriminator(error_param)


def _is_generic_or_schema_capability(value: str | None) -> bool:
    return (
        value is None
        or value.lower() in _GENERIC_REQUEST_ERROR_TYPES
        or _is_schema_capability_discriminator(value)
    )
```

After `_error_details()` succeeds, `_explicitly_unsupported_schema()` returns `_is_response_format_capability_rejection(message, error_type, error_code, error_param)`. Remove `_has_exclusive_schema_capability_details()` once no caller remains; retain `_is_schema_capability_discriminator()` for the new helper.

This requires status 400/422, a valid error envelope, `response_format` in the message, generic request or structured-output capability metadata in type/code, and either no param or a schema-capability param. If any normalized field contains a closed forbidden marker, return `False`. Do not store the message or include it in exceptions.

Do not broaden the classifier to every 400/422. Do not add provider/model names. Keep the strict-invalid-content retry as the only alternative second-request branch.

- [ ] **Step 6: Run focused GREEN tests**

Run:

```powershell
uv run --isolated pytest -q tests/unit/models/test_openai_compat.py
uv run --isolated pytest -q tests/unit/trial/test_journey_planner.py tests/unit/trial/test_recovery.py
```

Expected: all focused tests PASS; existing auth/model/quota/rate/timeout/redirect tests still make exactly one request; every retry/fallback branch makes at most two.

- [ ] **Step 7: Run scoped static gates**

Run:

```powershell
uv run --isolated ruff check src/repotrial/models/openai_compat.py tests/unit/models/test_openai_compat.py
uv run --isolated ruff format --check src/repotrial/models/openai_compat.py tests/unit/models/test_openai_compat.py
uv run --isolated mypy src/repotrial
git diff --check
rg -n -i "deepseek|qwen" src/repotrial/models/openai_compat.py
```

Expected: Ruff, mypy, and diff-check PASS. The final `rg` exits 1 with no production-brand matches.

- [ ] **Step 8: Independent focused review**

Give the reviewer the design, plan, Task 1 base/head diff, and focused test output. Require explicit review of:

- false fallback for auth/model/quota/rate/context errors;
- message secrecy;
- two-request ceiling on every control-flow branch;
- no provider-specific behavior;
- no change to `ModelAdapter` or planner semantics.

Any Critical or Important finding blocks the commit and receives one bounded TDD fix round.

- [ ] **Step 9: Commit the reviewed adapter correction**

```powershell
git add -- src/repotrial/models/openai_compat.py tests/unit/models/test_openai_compat.py
git commit -m "fix: support portable structured output fallback"
```

---

### Task 2: Verify DeepSeek through the public planner path

**Files:**

- Add: `docs/dev/pilot-evidence/m7.5-deepseek-portability-calibration.md`

**Interfaces:**

- Consumes: reviewed Task 1 commit and unchanged `plan_journeys()` public behavior.
- Consumes: user-scoped `REPOTRIAL_MODEL_API_KEY`; reads presence/value only into process environment and never prints it.
- Produces: sanitized evidence proving strict rejection -> one portable fallback -> non-empty policy-valid Journey.
- Does not produce a new provider, config field, model registry, or production code.

- [ ] **Step 1: Freeze execution identity and verify clean state**

Record:

```powershell
git rev-parse HEAD
git status --short
(Get-FileHash -Algorithm SHA256 eval/real_repos.yaml).Hash.ToLowerInvariant()
```

Require a clean worktree. Read the key from user scope into the current process without displaying it:

```powershell
$env:REPOTRIAL_MODEL_API_KEY = [Environment]::GetEnvironmentVariable(
    'REPOTRIAL_MODEL_API_KEY', 'User'
)
if ([string]::IsNullOrWhiteSpace($env:REPOTRIAL_MODEL_API_KEY)) {
    throw 'REPOTRIAL_MODEL_API_KEY is not configured'
}
```

- [ ] **Step 2: Run one sanitized public-path calibration**

Use `OpenAICompatibleModelAdapter("https://api.deepseek.com", "deepseek-v4-flash", api_key=...)` and `_plan_journeys_with_evidence()` with a temporary real directory and this trusted synthetic README excerpt:

```text
This application provides a browser-accessible dashboard after startup.
```

Run this file-less diagnostic from the repository root:

```powershell
$env:REPOTRIAL_MODEL_API_KEY = [Environment]::GetEnvironmentVariable(
    'REPOTRIAL_MODEL_API_KEY', 'User'
)
$probe = @'
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from repotrial.models.openai_compat import OpenAICompatibleModelAdapter
from repotrial.trial.planner import _plan_journeys_with_evidence


class CountingAdapter(OpenAICompatibleModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            "https://api.deepseek.com",
            "deepseek-v4-flash",
            api_key=os.environ["REPOTRIAL_MODEL_API_KEY"],
        )
        self.modes: list[str] = []

    async def _request(self, payload: dict[str, object]):
        mode = "strict" if "response_format" in payload else "portable"
        self.modes.append(mode)
        return await super()._request(payload)


async def main() -> None:
    adapter = CountingAdapter()
    with TemporaryDirectory(prefix="repotrial-deepseek-calibration-") as temp:
        evidence_dir = Path(temp).resolve(strict=True)
        journeys = await _plan_journeys_with_evidence(
            evidence_dir,
            "This application provides a browser-accessible dashboard after startup.",
            adapter,
            evidence_dir=evidence_dir,
        )
        evidence_file = next(
            evidence_dir.glob("baseline-model-attempt-*.jsonl")
        )
        terminal = json.loads(
            evidence_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        result = {
            "strict_requests": adapter.modes.count("strict"),
            "portable_requests": adapter.modes.count("portable"),
            "journey_count": len(journeys),
            "terminal_outcome": terminal.get("outcome"),
        }
        print(json.dumps(result, sort_keys=True))
        if (
            result["strict_requests"] != 1
            or result["portable_requests"] not in {0, 1}
            or not 1 <= result["journey_count"] <= 5
            or result["terminal_outcome"] != "success"
        ):
            raise SystemExit(1)


asyncio.run(main())
'@
$probe | uv run --isolated python -
```

The runner must output only one record shaped like:

```json
{
  "strict_requests": 1,
  "portable_requests": 1,
  "journey_count": 1,
  "terminal_outcome": "success"
}
```

The displayed `journey_count` is an example; any integer from 1 through 5 is valid. The runner may inspect request mode in memory but must not output prompt text, response content, provider error message, token counts, headers, or credentials. Require one strict request, zero or one portable request, a non-empty public Journey set, terminal evidence outcome `success`, and process exit 0.

If strict unexpectedly succeeds, record `strict_requests=1`, `portable_requests=0`; this is also valid provided the public Journey is non-empty and policy-valid. If the result is empty, malformed, policy-rejected, auth/quota/rate limited, or exceeds the planner deadline, stop before any sandbox execution.

- [ ] **Step 3: Run complete quality gates on the reviewed code HEAD**

```powershell
uv run --isolated ruff check .
uv run --isolated ruff format --check .
uv run --isolated mypy src/repotrial
uv run --isolated pytest -q
uv run --isolated pytest --cov=src/repotrial --cov-branch --cov-report=term-missing --cov-fail-under=85
uv run --isolated pre-commit run --all-files
git diff --check
```

Require fresh PASS output. Do not add skips/xfails or weaken tests. If the known Windows baseline reappears, compare exact node IDs/classes against the retained baseline before ruling; any new regression blocks execution.

- [ ] **Step 4: Write sanitized calibration evidence**

Create `docs/dev/pilot-evidence/m7.5-deepseek-portability-calibration.md` with:

- code HEAD and manifest hash;
- endpoint class and model name, but no credential/account identity;
- strict/portable request counts;
- elapsed monotonic time;
- public Journey count and terminal evidence outcome;
- confirmation that raw prompts/responses/errors were not retained;
- confirmation that Qwen and private Journey schema work were not performed;
- quality-gate results.

Do not copy raw model output or provider error messages into the document.

- [ ] **Step 5: Commit calibration evidence**

```powershell
git add -- docs/dev/pilot-evidence/m7.5-deepseek-portability-calibration.md
git commit -m "docs: record DeepSeek portability calibration"
```

---

### Task 3: Run the frozen three-repository canary with DeepSeek

**Files:**

- Modify after all attempts: `docs/dev/pilot-report.md`
- Modify after all attempts: `docs/dev/status.md`

**Interfaces:**

- Consumes: unchanged Task 2 evidence commit, `eval/real_repos.yaml`, current healthy SBX daemon, and `REPOTRIAL_MODEL_API_KEY`.
- Produces: three immutable metric-bearing attempts in frozen order and an honest release-gate result.
- Preserves: exact SHA, Disk Bound, whole-trial duration, cleanup, no-host-fallback, and known `pid_hard_bound_unsupported` disclosure.

- [ ] **Step 1: Verify the execution gate without daemon lifecycle commands**

Run read-only checks:

```powershell
$env:REPOTRIAL_MODEL_API_KEY = [Environment]::GetEnvironmentVariable(
    'REPOTRIAL_MODEL_API_KEY', 'User'
)
if ([string]::IsNullOrWhiteSpace($env:REPOTRIAL_MODEL_API_KEY)) {
    throw 'REPOTRIAL_MODEL_API_KEY is not configured'
}
git status --short
git rev-parse HEAD
sbx version
sbx diagnose --output json
sbx list
```

Require clean Git state, healthy required diagnose checks, and exact empty sandbox inventory. If daemon health is not already PASS, stop; do not start/restart/reset it.

- [ ] **Step 2: Run Umami with the frozen identity**

```powershell
uv run --isolated repotrial inspect https://github.com/umami-software/umami `
  --provider docker-sbx `
  --commit-sha ca661c7057984aa98ed4f7083d84dae2f65bfcb0 `
  --container-port 3000 `
  --compose-path docker-compose.yml `
  --model-endpoint https://api.deepseek.com `
  --model-name deepseek-v4-flash
```

Preserve the result regardless of PASS/FAIL/UNSUPPORTED. Record run ID, exact SHA evidence, Journey count/results, report presence, stop reason, duration, experiment count, cleanup, and final inventory. Do not retry a metric-bearing target-workload attempt.

- [ ] **Step 3: Run Listmonk with the frozen identity**

```powershell
uv run --isolated repotrial inspect https://github.com/knadh/listmonk `
  --provider docker-sbx `
  --commit-sha 670c01717d48647093335cc23a6be6f4b79c3b6b `
  --container-port 9000 `
  --compose-path docker-compose.yml `
  --model-endpoint https://api.deepseek.com `
  --model-name deepseek-v4-flash
```

Apply the same immutable evidence and no-retry rules.

- [ ] **Step 4: Run changedetection.io with the frozen identity**

```powershell
uv run --isolated repotrial inspect https://github.com/dgtlmoon/changedetection.io `
  --provider docker-sbx `
  --commit-sha 5d9c7c6da76340597243e8163c4f2439237fa0e8 `
  --container-port 5000 `
  --compose-path docker-compose.yml `
  --model-endpoint https://api.deepseek.com `
  --model-name deepseek-v4-flash
```

Apply the same immutable evidence and no-retry rules.

- [ ] **Step 5: Verify cleanup after every attempt and classify the canary**

After each attempt, run `sbx list` and require no residual sandbox. A repository passes only when it has:

- exact frozen SHA;
- a non-empty persisted required Journey set;
- every required Journey deterministically PASS;
- completed report evidence;
- exact stop reason evidence;
- successful cleanup and empty final inventory.

Canary passes only at `>=2/3`. Do not replace repositories or reinterpret an empty Journey set as success. If canary fails, retain all evidence and report the exact layer: intake, sandbox, boot, model transport, model policy, Journey execution, observation, experiment, report, or cleanup.

- [ ] **Step 6: Update the Pilot report and status honestly**

Add one bounded section to `docs/dev/pilot-report.md` and one current-status entry to `docs/dev/status.md` containing:

- execution HEAD and manifest hash;
- model endpoint class/name without account or credential data;
- all three run IDs and immutable outcomes;
- strict/fallback evidence disposition;
- Journey success, report, stop reason, duration, experiments, cleanup;
- canary result and whether repositories 4-10 are authorized by the frozen gate;
- explicit statement that Qwen/private typed-schema work remains deferred.

- [ ] **Step 7: Verify documentation diff and commit the canary record**

```powershell
git diff --check
git diff -- docs/dev/pilot-report.md docs/dev/status.md
git add -- docs/dev/pilot-report.md docs/dev/status.md
git commit -m "docs: record DeepSeek M7.5 canary"
git status --short --branch
```

Require a clean working tree. Do not push or merge.

## Completion outcomes

- Adapter focused tests and quality gates PASS.
- Real DeepSeek public planner calibration returns a non-empty policy-valid Journey.
- Frozen 3-repository canary is recorded without replacement or hidden retry.
- If canary is `>=2/3`, report `M7.5 DEEPSEEK CANARY — PASS` and proceed to the existing repositories 4-10 plan only after confirming the unchanged HEAD.
- If canary is `<2/3`, report `M7.5 DEEPSEEK CANARY — FAIL` with exact retained evidence; do not start Qwen/schema work automatically.
