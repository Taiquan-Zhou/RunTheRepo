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

Real verification against SBX v0.39.0 proved that `sbx template save` does not
retain the nested Docker daemon image store. RepoTrial therefore records a
bounded, normalized inventory of immutable inner-Docker image IDs and digests,
then streams the exact images and their required Compose-resolvable references
to one invocation-owned host temporary bundle. The host never loads or executes
the archive. The bundle is size-bounded, mode-restricted, and content-hashed.

RepoTrial still removes the cloned target workspace from the warmup sandbox and
saves the remaining sandbox as a run-unique temporary SBX template. Baseline and
every candidate are created from that template with a fresh exact-SHA workspace
clone, then receive the verified bundle through stdin to their nested
`docker image load`. No second archive copy is written inside a sandbox.

Every Compose startup uses no-pull/no-build mode and verifies the full image
inventory before starting services. A mutable tag therefore resolves only once
for the trial; subsequent sandboxes use the same exact image IDs and required
tags. Missing, changed, oversized, or unverified bundles and inventories fail
closed instead of falling back to a remote pull.

Alternatives rejected:

- Increasing the deadline alone hides repeated work and leaves poor user
  experience unchanged.
- Reusing one running sandbox would leak containers, volumes, and application
  state between hardening candidates.
- A global cache could mix mutable tags or untrusted build output across
  repositories and runs.

## Lifecycle and safety contract

The Docker SBX provider owns at most one temporary template and one temporary
image bundle per invocation. The template name is generated from a strict
RepoTrial-owned prefix plus an unpredictable run token. The bundle is created
as a private, exclusive temporary file outside the repository and target
workspace. The provider records both identities and only removes those exact
owned resources; it never removes the installed `shell-docker` base template,
any pre-existing user template, or an unrelated host file.

Warmup creation, image preparation, bundle export/import, template save, and
every workload sandbox operation count against the existing host-side
total-duration deadline. Template and bundle removal follow the same guaranteed
finalization boundary as sandbox destroy, so both are still attempted after
that deadline expires. Removal is attempted on success, failure, cancellation,
and checkpoint interruption. An unconfirmed or failed removal is a cleanup
failure, even if sandbox inventory is empty.

The warmup sandbox receives no API key, real secret, host Docker socket, or host
execution fallback. Dockerfile build steps, when present, still execute only
inside disposable SBX isolation. Exact commit SHA, disk/memory/CPU bounds,
network policy, deterministic journey verification, and the documented PID
hard-bound limitation are unchanged.

## Interfaces and evidence

`SandboxProvider` exposes a typed per-invocation preparation/finalization
boundary, including staging the validated image set before template activation.
The deterministic fake used by unit tests may implement it as a no-op; a real
provider without the required capability reports `unsupported`, never silently
falls back to repeated or host-side execution. `DockerSbxProvider` implements
the boundary using direct argv subprocesses for `sbx exec ... docker image
save`, `sbx template save`, `sbx create --template`, `sbx exec ... docker image
load`, and `sbx template rm`. Export/import streams bounded chunks and never
buffers a complete image archive in memory. The graph prepares the runtime once
before baseline and finalizes it around every invocation.

A dedicated JSONL evidence artifact records warmup sandbox lifecycle, normalized
image identity, bundle hash and byte size, template save, template use, and
combined template/bundle removal confirmation without including environment
values, archive bytes, or credentials. Public failures use bounded reason codes
such as `image_prepare_failed`, `image_bundle_export_failed`,
`image_bundle_import_failed`, `image_identity_mismatch`, `template_save_failed`,
and `template_cleanup_failed`; raw SBX or untrusted Compose output remains in
bounded evidence only.

## Verification

Use RED-to-GREEN unit tests for command construction, unique ownership,
image-inventory normalization, no-pull/no-build startup, deadline propagation,
cancellation, and fail-closed cleanup. Run focused tests, ruff, formatting, and
mypy, then use one trusted local image-only/build fixture to prove:

1. remote image acquisition occurs once;
2. the private host bundle is bounded, hashed, streamed, and never host-loaded;
3. baseline and candidates report the same image IDs and required references;
4. containers and volumes are independent;
5. all sandboxes are destroyed and both the temporary template and bundle are
   absent.

Only after that smoke passes, rerun Wakapi and Linkding serially on the same
final HEAD. The two-repository release gate remains unchanged.
