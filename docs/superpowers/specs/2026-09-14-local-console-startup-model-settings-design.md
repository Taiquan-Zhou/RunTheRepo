# Local Console Startup and Model Settings Design

**Date:** 2026-09-14
**Status:** Approved by the owner in chat
**Supersedes:** The local-web rule that API keys must be configured only in the process environment.

## Goal

Make one `repotrial serve` launch sufficient after a reboot: the console starts and verifies its required SBX service before accepting work, reports the real dependency state, and lets a local user persist model settings from the Web UI.

## Scope

- On console startup, inspect the SBX daemon, start it with argv-only `sbx daemon start --detach` when it is stopped, then verify `sbx daemon status --json`.
- Keep the existing bounded `sbx diagnose --output json` and reviewed empty-inventory checks. A passing card means the named local dependencies are working; copy must not imply that a target repository or remote image registry is guaranteed to work.
- Report startup state and exact bounded remediation through the existing environment card. A failed required service blocks job submission.
- Move Compose path into the ordinary repository configuration because discovery normally supplies it.
- Keep only model configuration under Advanced settings.
- Add DeepSeek and Custom model providers. DeepSeek fills a supported endpoint and model default; Custom accepts an HTTP(S) endpoint without credentials and a model ID.
- Accept API keys only through a loopback, same-origin, CSRF-protected settings endpoint. Persist model settings under the local user's XDG config directory with owner-only permissions and atomic replacement.
- GET settings returns provider, endpoint, model name, and `api_key_configured`; it never returns the API key. Blank key on update retains the existing key; clearing requires a separate explicit action.
- Job submission resolves model settings on the server. The browser job payload, URL, localStorage, status response, reports, and logs never contain the key.
- Pass the key to the trusted CLI subprocess only for model construction, remove it from the CLI process environment immediately after reading, and never add it to `GraphContext.env`, target Compose, or SBX `--env`.
- New settings apply only to later jobs. Active jobs are unchanged.
- No database, account system, cloud secret store, public bind address, proxy credentials form, or automatic target-sandbox canary is added.

## Failure behavior

- If SBX start or status validation fails, the console remains available and shows the failed dependency plus a concrete next action; inspection submission returns a conflict instead of starting a doomed run.
- Invalid/corrupt settings fail closed as unconfigured and do not leak file contents.
- Settings writes reject symlinks/nonregular parents where applicable, use a bounded schema, and preserve the previous valid file on failure.
- Model configuration is optional. An unconfigured model is shown clearly and deterministic inspection remains available.

## Acceptance

- RED/GREEN tests cover stopped-to-started SBX, already-running SBX, start failure, malformed status, timeout, and job admission blocking.
- Tests prove API key non-disclosure, owner-only atomic persistence, retain/replace/clear semantics, server-side job resolution, argv exclusion, and environment removal after model construction.
- Browser tests prove Compose is outside Advanced, Advanced contains only model settings, DeepSeek preset behavior, configured-key status, and accessible error/success states.
- Existing security, job, report, progress, repository discovery, and responsive-layout tests remain green.
