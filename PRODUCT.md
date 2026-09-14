# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Developers and security-minded maintainers evaluating an unfamiliar public GitHub Docker Compose web application on their own machine. They need to configure one inspection, confirm the local runtime is usable, follow the active run, and inspect evidence without learning RepoTrial internals first.

## Product Purpose

RunTheRepo pins a public GitHub repository to an exact commit, runs it in disposable isolation, verifies real user journeys, and produces reviewable evidence. Success means the user can understand what was tested, what happened, why a run stopped, and which reports are available.

## Positioning

RunTheRepo is an evidence-driven repository inspection console. It connects repository intake, isolated execution, deterministic verification, and workload-conditioned least-privilege experiments in one reproducible run rather than presenting a static scan or an LLM opinion.

## Operating Context

The current product is a local, single-task web console. The primary flow is Repository → Run → Evidence. Environment diagnostics are a preflight aid. Inputs include a GitHub HTTPS URL, pinned commit SHA, application container port, optional Compose path, and optional model endpoint/name. Outputs include truthful run state, elapsed time, stop/failure evidence, and validated HTML/JSON report links.

## Capabilities and Constraints

- FastAPI, Jinja, and vanilla JavaScript remain the frontend stack.
- Existing repository auto-detection, bilingual UI, documentation links, CSRF protection, and single-flight behavior must remain intact.
- Untrusted workloads run only through SandboxProvider in disposable isolation.
- UI status may only reflect backend-confirmed states and evidence; no fake percentages, logs, stages, or safety claims.
- Unsupported or unobserved capability is stated explicitly.
- Only public GitHub HTTPS repositories are accepted by the current console.

## Brand Commitments

The product name is RunTheRepo. Its voice is direct, technical, calm, and evidence-led. Existing recognizable colors are warm neutral surfaces, deep green text, restrained semantic green, and RunTheRepo red reserved for identity and primary actions. The console should feel like a mature developer instrument rather than a generic admin dashboard or an AI-themed landing page.

## Evidence on Hand

- Local Web status API exposes running, completed, failed, and unknown states with elapsed time, exit code, stop reason, run ID, progress projection, and validated report availability.
- Doctor results expose real named checks with PASS, FAIL, or UNSUPPORTED plus detail/remediation where present.
- Existing Playwright browser fixtures cover idle, environment, running, completed, failed, unknown, submission-unknown, English, and narrow viewport states.
- No testimonials, customer logos, benchmarks, or global security proof exist and none may be fabricated.

## Product Principles

- Repository, run, and evidence form one continuous workspace.
- Truthful observability is more important than visual fullness.
- Errors name the failed boundary and the next useful action.
- Configuration remains approachable for a first-time user while advanced options stay available.
- Every result is scoped to the tested workload and journey.

## Accessibility & Inclusion

The console must remain keyboard-usable, retain associated labels and visible focus, expose state changes through aria-live where appropriate, avoid color-only status meaning, support reduced motion, preserve Chinese/English behavior, and avoid horizontal overflow at common desktop and 390px widths.
