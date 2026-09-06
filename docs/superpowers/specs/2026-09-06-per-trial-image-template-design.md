# Per-Trial Image Template Design

**Status:** Owner-approved in chat on 2026-09-06.

## Problem and success criteria

Docker SBX gives every sandbox an independent inner Docker store. RepoTrial
currently creates a fresh store for baseline and every hardening candidate, so
the same remote images are repeatedly downloaded and unpacked. The Wakapi
canary spent about 212 seconds in baseline Compose startup and 303 seconds in
the first candidate startup; sandbox allocation and cleanup took only seconds.
Repeated cold pulls exhausted the existing 900-second host-side trial deadline.

The change succeeds when one trial pulls or builds its Compose images once,
then baseline and all candidates use that exact image set while retaining
independent containers, volumes, workspaces, and cleanup lifecycles. The design
must not introduce cross-run or cross-repository cache reuse.

## Chosen approach

Before baseline execution, RepoTrial creates one disposable warmup sandbox from
the normal trusted SBX base template. Inside that sandbox it materializes the
same generated startup inputs and compatibility overlay used by baseline,
resolves Compose, pulls declared images, and builds buildable services. It does
not start target services.

RepoTrial records a bounded, normalized inventory of immutable inner-Docker
image IDs and digests. It removes the cloned target workspace from the warmup
sandbox, saves the remaining sandbox as a run-unique temporary SBX template,
and destroys the warmup sandbox. Baseline and every candidate are then created
from that template with a fresh exact-SHA workspace clone.

Every Compose startup uses no-pull/no-build mode and verifies the image
inventory before starting services. A mutable tag therefore resolves only once
for the trial; subsequent sandboxes use the exact saved image ID. Missing or
different images fail closed instead of falling back to a remote pull.

Alternatives rejected:

- Increasing the deadline alone hides repeated work and leaves poor user
  experience unchanged.
- Reusing one running sandbox would leak containers, volumes, and application
  state between hardening candidates.
- A global cache could mix mutable tags or untrusted build output across
  repositories and runs.

## Lifecycle and safety contract

The Docker SBX provider owns at most one temporary template per invocation.
Its name is generated from a strict RepoTrial-owned prefix plus an unpredictable
run token. The provider records the template identity returned by SBX and only
removes that exact owned identity; it never removes the installed
`shell-docker` base template or any pre-existing user template.

Warmup creation, image preparation, template save, and every workload sandbox
operation count against the existing host-side total-duration deadline.
Template removal follows the same separately bounded cleanup path as sandbox
destroy, so it is still attempted after that deadline expires. Removal is
attempted on success, failure, cancellation, and checkpoint interruption. An
unconfirmed or failed removal is a cleanup failure, even if sandbox inventory
is empty.

The warmup sandbox receives no API key, real secret, host Docker socket, or host
execution fallback. Dockerfile build steps, when present, still execute only
inside disposable SBX isolation. Exact commit SHA, disk/memory/CPU bounds,
network policy, deterministic journey verification, and the documented PID
hard-bound limitation are unchanged.

## Interfaces and evidence

`SandboxProvider` gains a typed per-invocation preparation/finalization boundary.
The deterministic fake used by unit tests may implement it as a no-op; a real
provider without the required capability reports `unsupported`, never silently
falls back to repeated or host-side execution.
`DockerSbxProvider` implements the boundary using `sbx template save`,
`sbx create --template`, and `sbx template rm`. The graph prepares the runtime
once before baseline and finalizes it around every invocation.

A dedicated JSONL evidence artifact records warmup sandbox lifecycle, normalized
image identity, template save, template use, and template removal without
including environment values or credentials. Public failures use bounded reason
codes such as `image_prepare_failed`, `image_identity_mismatch`,
`template_save_failed`, and `template_cleanup_failed`; raw SBX or untrusted
Compose output remains in bounded evidence only.

## Verification

Use RED-to-GREEN unit tests for command construction, unique ownership,
image-inventory normalization, no-pull/no-build startup, deadline propagation,
cancellation, and fail-closed cleanup. Run focused tests, ruff, formatting, and
mypy, then use one trusted local image-only/build fixture to prove:

1. remote image acquisition occurs once;
2. baseline and candidates report the same image IDs;
3. containers and volumes are independent;
4. all sandboxes are destroyed and the temporary template is absent.

Only after that smoke passes, rerun Wakapi and Linkding serially on the same
final HEAD. The two-repository release gate remains unchanged.
