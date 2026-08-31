# RepoTrial Model-Boundary Portability Design

## Status

- Owner-approved design: 2026-08-31
- Design base: `9525402459a1496b778aa10c2834540651eebef8`
- Scope: private model transport and OpenAI-compatible request negotiation only

## Goal

Make RepoTrial's existing structured model boundary usable with a broad range
of OpenAI-compatible text models without binding behavior to provider or model
names. Preserve strict local validation, deterministic Journey verification,
and fail-closed behavior.

This design does not promise that every model is capable of producing useful
Journeys. It separates provider protocol compatibility from model quality so
that each failure is classified at the correct boundary.

## Reproduced evidence

Two independent failures expose different defects in the current boundary:

1. A local Qwen model returned a schema-valid `_JourneyProposal` whose
   `http/request` params omitted the required `method`. The private transport
   schema accepted the generic params mapping, and the later domain policy
   rejected the proposal.
2. `deepseek-v4-flash` was accessible through the authenticated `/models`
   endpoint, but the current strict `response_format=json_schema` request
   returned HTTP 400. The same model, prompts, private schema, and domain policy
   succeeded through the adapter's existing portable JSON-prompt payload:
   HTTP 200, schema-valid, one non-empty policy-valid Journey.

The public Journey DSL and deterministic verifier were not implicated by
either reproduction.

## Root causes

### Under-specified private Journey transport

`_JourneyStepTransport` currently exposes `tool`, `action`, and a generic
`dict[str, object]` params value. Its JSON Schema limits collection size but
does not express action-specific required fields or legal field combinations.
The model must recover those rules from prose, while Pydantic accepts outputs
that the domain materializer must later reject.

### Overly narrow structured-output negotiation

`OpenAICompatibleModelAdapter` correctly prefers strict JSON Schema and already
has a portable JSON-prompt fallback. The fallback is currently gated on error
metadata that some otherwise compatible endpoints do not provide. A response
can identify `response_format` as the invalid request component while using
only a generic request-error type/code and no capability-specific param.

## Design

### 1. Action-specific private Journey schema

Replace the generic model-facing step schema with private strict variants for
the existing, frozen action set:

- `http/request`: required `method` (`GET`, `POST`, or `DELETE`) and `path`;
  optional JSON body; typed assertion variants.
- `browser/goto`: exactly `path`.
- `browser/fill_by_label`: exactly `label` and `value`.
- `browser/click_by_role`: exactly an allowed `role` and `name`.
- `browser/assert_text_visible`: exactly `text`.

Assertion variants encode the existing combinations:

- `status_code` / `response.status` / integer expected;
- `text_contains` / `response.text` / text expected;
- `json_path_equals` / bounded dotted target / bounded JSON expected.

These types are private model-transport types. They materialize into the
unchanged public `Journey`, `JourneyStep`, and `JourneyAssertion` models. The
existing domain parser and policy remain a second, authoritative validation
layer; the transport schema does not replace them.

The private schema must encode field presence, literal action/method/role
values, exact parameter shapes, and directly portable string constraints. The
existing domain validator remains authoritative for custom path safety and JSON
depth, node-count, collection-size, and aggregate-content limits that are not
reliably portable across provider JSON Schema implementations.

Cross-step rules that are unsuitable for portable JSON Schema remain in the
domain validator, including non-empty Journeys, one tool type per Journey, and
at least one assertion for an HTTP Journey.

### 2. Provider-neutral bounded fallback

Keep strict JSON Schema as the first request. Permit exactly one fallback to
the existing portable JSON-prompt payload only when all of these hold:

- HTTP status is 400 or 422;
- the parsed response has the recognized error-envelope shape;
- the message contains the case-insensitive token `response_format`;
- available type/code/param fields are generic request metadata or structured
  output capability metadata;
- neither the message nor type/code/param fields classify authentication,
  authorization, model access, quota, rate limit, context limit, or another
  non-capability failure. The message may be inspected in memory for this
  classification but must never be logged or persisted.

Never fallback for 401/403/404/408/409/429, transport failures, timeouts,
response-size failures, redirects, or 5xx responses. Do not branch on endpoint,
provider, or model name.

The fallback request embeds the canonical Pydantic JSON Schema in the bounded
system prompt. Its response must be a complete JSON object and pass the same
strict Pydantic validation. Do not strip Markdown fences, extract JSON
substrings, repair fields, insert defaults, or convert values. Invalid output
fails closed.

The complete `structured()` call has a hard maximum of two HTTP requests:

- first strict response is a qualifying capability rejection -> make one
  portable fallback; invalid fallback output terminates without retry;
- first strict response is successful but structurally invalid -> make one
  same-mode strict retry; the retry cannot trigger a portable fallback;
- every other first response terminates immediately.

No path may make a third request.

### 3. Error and evidence semantics

Existing credential-free `ModelAdapterError` messages and closed reason codes
remain unchanged. Raw provider errors, prompts, responses, credentials, and
model rationale must not enter logs or model-attempt evidence.

Planner outcomes remain unchanged:

- adapter/schema failure -> empty Journey set;
- policy failure -> empty Journey set;
- valid proposal -> existing materialization and deterministic execution.

## Frozen architecture impact

Allowed implementation scope:

- `src/repotrial/models/openai_compat.py`
- `src/repotrial/trial/planner.py`
- focused unit tests for those modules
- bounded calibration evidence and status documentation after verification

The implementation must not modify:

- `ModelAdapter` protocol;
- public Journey DSL or deterministic verifier;
- LangGraph topology;
- RunState or checkpoint semantics;
- SandboxProvider, Disk, PID, or total-duration behavior;
- report, API, or CLI semantics;
- Experiment, mutation, Journey replay, or KEEP/ROLLBACK semantics;
- the frozen real-repository manifest or commit SHAs.

If implementation requires one of those changes, stop for Owner review.

## TDD and verification

RED tests must prove at least:

1. The generated private schema requires HTTP `method` and `path` and rejects
   invalid action/params combinations before domain materialization.
2. Typed assertion combinations are represented and invalid combinations fail
   transport validation.
3. A DeepSeek-shaped generic 400 identifying `response_format` performs one
   portable fallback and accepts only a fully schema-valid response.
4. Authentication, authorization, invalid model, quota, rate limit, context,
   timeout, transport, redirect, and 5xx failures never fallback.
5. Fallback output that is malformed, schema-invalid, or policy-invalid remains
   fail-closed, and every branch observes the two-request hard ceiling.
6. No provider or model-name conditional is introduced.

After GREEN:

- run focused planner and adapter tests;
- run Ruff, mypy, full pytest, branch coverage, pre-commit, and diff checks;
- independently review only this model-boundary change;
- calibrate the real local Qwen path and the DeepSeek portable path without
  retaining raw model output;
- require DeepSeek to return a non-empty policy-valid Journey through the
  public planner path;
- require Qwen either to return a policy-valid Journey or fail at the typed
  transport boundary, never repeat the prior schema-valid/missing-method gap;
- only after these gates, run the frozen three-repository canary from one
  unchanged HEAD.

## Success criteria

The correction is complete when RepoTrial can use both strict-schema-capable
and portable-JSON OpenAI-compatible endpoints through the unchanged public
model interface, while every accepted Journey is validated by both the private
typed transport and the existing domain policy. Compatibility failure and
model-quality failure remain explicit, bounded, and fail-closed.
