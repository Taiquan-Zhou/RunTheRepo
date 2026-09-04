# Loopback Publication Compatibility Design

**Status:** Owner-approved on 2026-09-05 for RG3 implementation.

## Problem and success criteria

In the changedetection.io frozen canary, the application is reachable from its
container and through the sandbox guest's Compose-published loopback port, but
the same port resets when exposed by `SandboxProvider.publish_port()`. The
retained three-probe diagnostic is the RG3 entry-gate evidence. RepoTrial needs
a generic trial-only compatibility layer so this valid application topology is
reachable through the provider boundary without modifying the source Compose.

The change succeeds when:

- a unique exact port binding whose host IP is loopback is normalized to a
  sandbox-reachable host binding while preserving target port and protocol;
- the generated overlay is immutable, content-hashed, referenced by the run,
  and applied to baseline Boot, startup-input Compose validation, observation,
  and every hardening candidate;
- hardening parent/candidate hashes and KEEP/ROLLBACK decisions continue to use
  the source/accepted Compose model, never the compatibility overlay;
- a missing eligible binding is a no-op, while every ambiguous or unsupported
  case fails closed with an explainable `compatibility:<reason>` stop reason;
- source files remain byte-for-byte unchanged and all sandboxes are destroyed.

## Chosen approach

Generate one Compose override from the already-safe parsed Compose tree before
baseline Boot. The overlay uses `ports: !override`, copies every port entry for
the selected service, and changes only the unique matching entry's loopback
host IP to `0.0.0.0`. Compose commands receive files in this immutable order:

1. source or accepted Compose;
2. compatibility overlay, when present;
3. one hardening candidate overlay, when present.

The source and accepted Compose remain the configuration identity used by the
hardening engine. Therefore compatibility does not create an experiment, does
not count as convergence, and cannot produce KEEP.

Alternatives rejected:

- Modifying a copied source Compose conflates compatibility with accepted
  configuration and makes hashes/checkpoints misleading.
- Adding a proxy/helper container is broader, changes the service graph, and
  introduces another workload whose behavior must be trusted and audited.

## Accepted inputs and fail-closed behavior

The selector receives the parsed Compose mapping and RepoTrial's exact
`container_port`.

- Accept short syntax only when it is an exact `host_ip:published:target` entry,
  optionally with `/tcp` or `/udp`; bracketed IPv6 is accepted.
- Accept long syntax only when `target` and `published` are single numeric
  ports, `host_ip` is a numeric loopback address, and protocol is absent,
  `tcp`, or `udp`.
- Recognize numeric loopback addresses using the standard IP parser. Hostnames
  such as `localhost` are not accepted.
- Preserve all fields and ordering in long syntax and preserve target,
  published port, and protocol in short syntax.
- Return no artifact when no port entry matching `container_port` is bound to
  loopback.
- Reject port ranges, tagged port values, invalid port/protocol values,
  multiple matching loopback bindings, `network_mode: host`, and any existing
  binding that would collide on the selected published port/protocol after the
  host IP becomes `0.0.0.0`.

The compatibility artifact is created exclusively as a new regular file under
the verified workspace overlay directory. Its SHA-256 is computed from the
persisted bytes and stored in checkpointed `RunState` together with its relative
path. On checkpoint replay, RepoTrial verifies the same path, content, and hash;
replacement, deletion, symlink insertion, or a different planned overlay fails
closed.

## State, audit, and report contract

`RunState` gains optional `compatibility_overlay_path` and
`compatibility_overlay_sha256` fields. Both are absent together or present
together. The path is also included once in `artifacts`.

The JSON/HTML report exposes compatibility separately from hardening overlays:
the compatibility object includes its reference and SHA-256, and
`experiment_overlays` excludes it. This prevents an execution prerequisite from
being presented as a security hardening or meaningful convergence.

## Runtime threading

Boot, observation, and startup-input materialization accept an optional
`compatibility_overlay_path` in addition to their existing hardening
`overlay_path`; command construction always places compatibility first.
`ExperimentContext` carries the verified existing compatibility path into every
candidate sandbox. The baseline graph selects/verifies the artifact before
creating a sandbox. Journey runners need no new parameter because they consume
the provider-published host port after Boot.

If compatibility planning or replay validation fails, the graph routes directly
to reporting with `compatibility:<reason>` and never creates a sandbox. Cleanup,
exact-SHA intake, no-host-fallback, disk bounds, host-side total duration, and
the documented PID limitation are unchanged.

## Verification

Use RED-to-GREEN unit tests for selector/writer validation, checkpoint tamper
handling, command ordering across baseline/candidate/startup/observer, hash and
report separation, and unchanged hardening hashes/decisions. After focused
tests, ruff, formatting, and mypy pass, run the trusted loopback fixture through
create/Boot/publish/journey/observation/destroy and confirm official inventory
is empty. Only then rerun changedetection once at its frozen SHA.
