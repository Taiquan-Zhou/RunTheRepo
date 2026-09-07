# Local Web technical-preview design

Owner approved a minimal local console after the CLI Alpha installation check.
This is a presentation and invocation layer, not a replacement trial engine.

## Scope and architecture

- `repotrial serve --port 8765` runs in the supported WSL environment and binds
  only `127.0.0.1`. No public host flag, account system, database, desktop shell,
  frontend framework, or new dependency is required.
- Reuse FastAPI/Jinja2 and plain browser JavaScript. A small local runner invokes
  the installed interpreter via argv: `python -m repotrial inspect ...`.
  Add the standard `repotrial.__main__` entry point; do not duplicate graph logic.
- The existing PostgreSQL API contracts are unchanged. The console has its own
  explicitly local factory and consumes the CLI's existing artifact contracts.
- Accept a public GitHub URL, required full lowercase SHA, container port,
  optional safe Compose path, and optional model endpoint/name pair. The API key
  is inherited from the trusted server environment, never accepted by the page.
- Permit one active invocation; reject additional submissions with HTTP 409.
  Return a job ID immediately. Poll actual running/terminal state and elapsed
  time; never invent percentages or claim detailed graph-stage observability.
- A browser page closing does not cancel its run. Server shutdown signals the
  owned CLI process with SIGINT and awaits exit/CLI cleanup without a premature
  kill timer. Never kill unrelated processes or restart/reset the daemon.

## Local security and evidence

- Restrict Host to loopback names, reject cross-origin mutation requests, and
  require a per-server unpredictable CSRF token for execution/doctor requests.
  No CORS wildcard, arbitrary shell, upload, or arbitrary file-download route.
- Do not display or retain raw child stdout/stderr. Drain both with a bounded
  capture sufficient for the CLI's generated run ID; discard overflow.
- Only validated server-owned job IDs may reach fixed JSON/HTML report names
  under their known run directory. Reject traversal and symlink/nonregular path
  components; bound file reads. Escape UI text; isolate rendered reports and
  forbid report scripts. Never serve the cloned workspace or model credentials.
- Run doctor only while idle using its existing bounded implementation. Show
  its real checks and unsupported PID limitation; do not reinterpret it as a
  full repository or browser-functionality guarantee.
- Terminal success requires exit 0 and matching attempt identity plus available
  reports. Missing/invalid terminal evidence remains failed/unknown, not success.
- Keep in-memory job history bounded. Cross-server resume and durable job history
  are out of scope; on-disk CLI evidence remains the durable record.
- Exact SHA, disposable SBX, no host fallback, resource limits, fail-closed
  cleanup, per-trial image ownership, and real-browser unsupported remain intact.

## Acceptance

Trusted tests prove exact argv, single-flight admission, bounded pipe draining,
terminal evidence projection, origin/token/path guards, and shutdown cleanup.
A real browser may open this trusted local UI with an injected trusted runner;
it must show an environment result, submit parameters, poll a job and expose
its report. This is not a real-target browser Journey or a new repository canary.
Focused tests, lint, format, types, one module review and packaging smoke precede
completion. No push, merge, public exposure, or release is authorized here.
