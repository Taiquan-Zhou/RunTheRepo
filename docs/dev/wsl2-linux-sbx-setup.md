# WSL2 Ubuntu + Linux Docker Sandboxes setup

This guide reproduces RepoTrial's accepted Windows development topology. It is
an environment procedure for the existing `DockerSbxProvider`; it does not add
a Provider, change the frozen Pilot cohort, or authorize host Docker fallback.

## Support labels

- **Microsoft-official WSL behavior:** Microsoft documents [`wsl --install`,
  `--location`, and WSL management
  commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands). The
  calibrated host's Microsoft WSL 2.6.3.0 `wsl --help` also verified `--name`,
  `--version`, `--vhd-size`, and `--manage --set-default-user`; WSL owns the
  resulting ext4 VHD.
- **Docker-official behavior:** the Docker-published Linux SBX package, the
  installed `sbx` CLI, and Docker's documented [upstream proxy
  settings](https://docs.docker.com/ai/sandboxes/configuration/upstream-proxy/).
- **RepoTrial-supported runtime:** Windows 11 + WSL2 + a dedicated Ubuntu
  24.04 distro on D + official Linux SBX v0.42.0 is the current production
  runtime. This label does not turn the WSL topology into a Docker support
  claim. The older v0.39.0 calibration remains historical and is not an
  installation target.
- **Known unsupported:** Docker SBX v0.42.0 has no verified PID hard bound.
  RepoTrial does not emulate one with `ulimit`, Compose `pids_limit`, polling,
  or memory limits and does not claim fork-bomb/PID hard protection.

Historical calibration values and hashes are in the
[calibration report](pilot-evidence/wsl2-linux-sbx-calibration.md); the active
v0.42.0 package and CLI identity are pinned below.

## Verified topology and safety boundary

```text
Windows 11 desktop/control entry
  -> dedicated WSL2 distro: RepoTrial-Ubuntu
       -> RepoTrial ext4 checkout
       -> official Linux sbx CLI + sandboxd
            -> disposable SBX microVM
                 -> guest Docker + pinned untrusted Compose workload
```

CPU, memory, the three-part disk budget, host-side shared monotonic whole-trial
duration, fail-closed network policy, forced cleanup, and no-host-fallback were
calibrated. PID hard bound remains unsupported. The Windows host, dedicated WSL
distro, Linux `sbx` process, sandboxd, and SBX policy proxy are trusted control
infrastructure; target repositories remain untrusted and execute only inside a
disposable SBX microVM.

## Prerequisites

### Disk-space terminology

Do not equate the VHD capacity, actual allocated storage, free host-drive space,
or RepoTrial's per-sandbox disk policy. Microsoft documents that WSL VHDs
[grow as storage is used](https://learn.microsoft.com/en-us/windows/wsl/disk-space);
the capacity shown inside Linux is not proof that the Windows drive has that
much free space. Check both locations. Deleting Linux files is not evidence
that the Windows VHD has immediately returned the same amount of space.

The current CLI configures an 8192 MiB per-sandbox disk budget. This is not a
whole-installation footprint: OS/runtime assets, downloaded images, temporary
templates, Python dependencies and retained run artifacts also consume space.
Single-workload sampled peaks must be labelled as measurements, not universal
minimum requirements. No automatic deletion or VHD compaction is part of setup.

### Measured example, not a minimum configuration

At production HEAD `25a88527369779fab11221bfac5f7125e85d9bee`, one installed-wheel
changedetection run (`286023c6-1c9a-460a-ac10-652b63737fe3`, pinned source
`5d9c7c6da76340597243e8163c4f2439237fa0e8`) completed with exit 0,
`no_remaining_mutations`, JSON/HTML reports, four successful sandbox destroys,
confirmed template removal and empty official inventory.

| Measurement | Before | Sampled maximum | After cleanup |
| --- | ---: | ---: | ---: |
| SBX state directory, allocated storage (`du -s -B1`) | 2.228 GiB | 3.840 GiB | 2.232 GiB |
| Whole WSL ext4 filesystem, used blocks (`statvfs`) | 10.018 GiB | 12.001 GiB | 10.051 GiB |

There were 98 samples at a nominal 5-second interval; maximum monotonic gap
was 5.021 seconds, with no sampling errors. The sampled increases were
1.611 GiB for SBX state and 1.983 GiB for the whole WSL filesystem. These
overlap and must not be added together. The Windows D: used-space change was
only 380,928 bytes in this existing environment; reuse of previously allocated
VHD space means this is not a cold-install storage requirement. Host and
whole-filesystem metrics include other processes. Sampling can miss shorter
peaks, and existing runtime assets/caches were not cleared. This single workload
does not establish a universal minimum or a worst-case bound for other targets.

Raw samples and the measuring script are retained in the ignored directory
`artifacts/disk-peak-25a88527/`. No distro was created, no files were deleted,
and no runtime disk or duration limits were changed for this measurement.

Before provisioning, require:

- Windows 11 with current Microsoft WSL supporting the flags shown below;
- hardware virtualization and nested KVM visible in WSL2;
- sufficient free space on the host drive and inside the distro for the chosen
  workload, downloaded images, temporary templates and retained evidence;
  there is no validated universal 150 GB free-space minimum;
- network access to Ubuntu package archives, GitHub releases, Docker login,
  Docker Hub auth/registry/CDN, and the target public Git repositories;
- any existing `Ubuntu` or `docker-desktop` distributions, if present, recorded
  but not modified.

In PowerShell, capture the starting state:

```powershell
wsl --version
wsl --status
wsl --list --verbose
wsl --list --online
Get-PSDrive -Name D | Select-Object Name, Used, Free
Test-Path -LiteralPath 'D:\DockerData\WSL\RepoTrial-Ubuntu'
```

Stop if `RepoTrial-Ubuntu` or its target directory already exists; investigate
that exact state instead of overwriting it.

## Create the dedicated distro on D

This command records the historical development allocation, not a minimum user
configuration or a requirement to create a second distro for testing. The
120 GB VHD capacity below is not an installation-footprint measurement. Do not
derive a 150 GB free-space gate from it. Use an existing supported Linux/WSL
environment when available and measure the intended workload before setting
capacity. This command uses Microsoft WSL's placement interface. It does not
export, import, or manually move a VHD:

```powershell
wsl --install Ubuntu-24.04 --name RepoTrial-Ubuntu --location "D:\DockerData\WSL\RepoTrial-Ubuntu" --version 2 --vhd-size 120GB --no-launch
```

If Windows requests a reboot, reboot once and continue without rerunning the
install command. Create a non-root execution user as the distro root, grant only
the required KVM group membership, then make that user the distro default:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- useradd --create-home --shell /bin/bash repotrial
wsl -d RepoTrial-Ubuntu -u root -- usermod --append --groups adm,kvm repotrial
wsl --manage RepoTrial-Ubuntu --set-default-user repotrial
```

Do not grant passwordless sudo. Select `-u root` explicitly only for package
management and Playwright system dependencies. Verify:

```powershell
wsl -d RepoTrial-Ubuntu -- cat /etc/os-release
wsl -d RepoTrial-Ubuntu -- id
wsl -d RepoTrial-Ubuntu -- ls -l /dev/kvm
wsl -d RepoTrial-Ubuntu -- ps -p 1 -o comm=
wsl --list --verbose
```

Require Ubuntu 24.04, WSL version 2, default user `repotrial`, accessible
`/dev/kvm`, and `systemd` as PID 1.

## Install the supported official Linux SBX package

Install bounded OS prerequisites as root. Do not install Docker Engine or Docker
Desktop inside this distro:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- apt-get update
wsl -d RepoTrial-Ubuntu -u root -- apt-get install -y ca-certificates curl git python3.12 python3.12-venv
wsl -d RepoTrial-Ubuntu -u root -- install -d -o repotrial -g repotrial -m 0755 /home/repotrial/downloads
```

Download Docker's official Ubuntu 24.04 package as the non-root user:

```powershell
wsl -d RepoTrial-Ubuntu -- curl --fail --show-error --location --proto "=https" --tlsv1.2 --output /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb https://github.com/docker/sbx-releases/releases/download/v0.42.0/DockerSandboxes-linux-amd64-ubuntu2404.deb
wsl -d RepoTrial-Ubuntu -- sha256sum /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb
```

The required SHA-256 is:

```text
e8a769b50b0e662626b3d6f95b601211e827965406e600b74ee8cb0b06e180fe
```

Do not install a mismatched file. After the hash matches:

```powershell
wsl -d RepoTrial-Ubuntu -u root -- apt-get install -y /home/repotrial/downloads/DockerSandboxes-linux-amd64-ubuntu2404.deb
wsl -d RepoTrial-Ubuntu -- sbx version
```

The reviewed current CLI identity is
`v0.42.0 ca4a4bd42035628137d78c5a0bef5c0d3301a35a`. The package hash and
`sbx version` output are both required to match before running RepoTrial.

## Authentication, daemon, and deny-all policy

Use exactly one bootstrap path. With direct Docker and GitHub access, start only
the Linux daemon once with the frozen default-deny policy:

```powershell
wsl -d RepoTrial-Ubuntu -- sbx daemon start --detach --policy deny-all
wsl -d RepoTrial-Ubuntu -- sbx login
wsl -d RepoTrial-Ubuntu -- sbx diagnose --output json
wsl -d RepoTrial-Ubuntu -- sbx policy ls --type network --json
wsl -d RepoTrial-Ubuntu -- sbx list
```

If this network requires the approved LAN proxy, use the following PowerShell
block instead. Every `wsl` invocation receives the same explicit bootstrap
environment; no shell-local `export` is assumed to survive a later invocation:

```powershell
$repoTrialProxy = 'http://<windows-host-lan-ip>:<port>'
$repoTrialNoProxy = 'localhost,127.0.0.1'

wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx daemon start --detach --policy deny-all
wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx login
wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx settings set proxy.daemon $repoTrialProxy
wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx settings set proxy.sandbox $repoTrialProxy
wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx diagnose --output json
wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx policy ls --type network --json
wsl -d RepoTrial-Ubuntu -- env "HTTP_PROXY=$repoTrialProxy" "HTTPS_PROXY=$repoTrialProxy" "NO_PROXY=$repoTrialNoProxy" sbx list
```

This bootstrap use of standard variables can also feed Docker's daemon and
sandbox fallback routes; it is not client-only and is not a third Docker proxy
scope. The explicit scoped settings retain the intended routes for later
sessions.

Complete `sbx login` through Docker's official interactive flow. Never copy a
device code, cookie, token, or SBX secret into the repository, logs, shell
history, or this guide. Require all diagnose checks PASS, an immutable active
`default-deny-all` global network rule, and an empty inventory before running
RepoTrial.

## Proxy configuration: two Docker scopes plus the trusted client

Docker officially defines the first two outbound scopes below and documents
their precedence. RepoTrial separately observed the third client-side path on
this machine; it is an operational requirement here, not another Docker scope.

1. **Docker-official daemon upstream:** sandboxd's own external downloads.
2. **Docker-official sandbox upstream:** outbound traffic from disposable
   sandboxes through SBX's policy-enforcing proxy.
3. **RepoTrial-tested trusted client process:** the `sbx` CLI and RepoTrial
   control process, including Docker session/JWKS verification and GitHub
   access observed during calibration.

For an approved LAN proxy without embedded credentials, the proxied bootstrap
above sets these two official SBX overrides. Their equivalent commands from an
existing WSL shell are:

```text
sbx settings set proxy.daemon http://<windows-host-lan-ip>:<port>
sbx settings set proxy.sandbox http://<windows-host-lan-ip>:<port>
sbx settings get proxy.daemon
sbx settings get proxy.sandbox
```

Run those commands inside `RepoTrial-Ubuntu`. Docker documents that
`proxy.sandbox` is resolved for the next sandbox create or restart, while a
change to `proxy.daemon` requires one controlled daemon restart because the
daemon resolves it only at startup. Configure both during initial setup. Later
changes are explicit maintenance, not a retry loop. Keep proxy URLs free of
userinfo and never persist credentials in project files.

When the local network requires the same proxy for the trusted client layer,
launch RepoTrial and direct `sbx` commands from a shell with:

```bash
export HTTP_PROXY='http://<windows-host-lan-ip>:<port>'
export HTTPS_PROXY="$HTTP_PROXY"
export NO_PROXY='localhost,127.0.0.1'
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTPS_PROXY"
export no_proxy="$NO_PROXY"
```

On a proxied first setup, export these variables before the first `sbx` command
and keep using that shell for daemon startup, login, diagnose, and RepoTrial.
This bootstrap environment is not client-only: Docker documents the standard
variables as a fallback for daemon and sandbox traffic when no
higher-precedence setting is configured. Record the explicit scoped settings
above after startup to retain the intended daemon/sandbox routes across later
sessions. The calibrated setup used the same endpoint for bootstrap and both
scoped settings, so recording it did not require an additional restart to
change the effective route. If `proxy.daemon` is later changed to a different
value, one controlled daemon restart is required. Keep the client variables for
the separately observed JWKS/GitHub route.

`NO_PROXY` must retain loopback so published SBX application ports are checked
directly. The calibrated Windows proxy worked in Rule mode; Global mode is not
required. Enabling a Windows proxy's “Allow LAN” option broadens its listener:
restrict it with the Windows firewall to the intended WSL/host scope, do not
expose it to an untrusted LAN, and never pass that upstream endpoint or its
credentials directly to a target workload.

## Ext4 checkout and locked development environment

Keep the checkout under the distro's ext4 filesystem. `/mnt/c` and `/mnt/d`
have Windows filesystem semantics and are not accepted for calibration or Pilot
execution:

```bash
mkdir -p /home/repotrial/src
git clone <trusted-repository-source> /home/repotrial/src/RepoTrial
cd /home/repotrial/src/RepoTrial
git checkout --detach <approved-execution-head>
test "$(findmnt -T . -n -o FSTYPE)" = ext4
git status --porcelain=v1
```

Create an independent pinned uv bootstrap environment; do not share the Windows
virtual environment:

```bash
python3.12 -m venv /home/repotrial/.local/share/repotrial-uv
/home/repotrial/.local/share/repotrial-uv/bin/pip install 'uv==0.11.5'
export PATH="/home/repotrial/.local/share/repotrial-uv/bin:$HOME/.local/bin:$PATH"
uv lock --check
uv sync --locked --all-groups
uv run python --version
uv --version
```

Install Playwright's OS dependencies as the distro root and its locked browser
as `repotrial`; these are environment assets, not repository dependency edits:

```powershell
wsl -d RepoTrial-Ubuntu -u root --cd /home/repotrial/src/RepoTrial -- env PATH=/home/repotrial/.local/share/repotrial-uv/bin:/usr/local/bin:/usr/bin:/bin /home/repotrial/.local/share/repotrial-uv/bin/uv run playwright install-deps chromium
wsl -d RepoTrial-Ubuntu --cd /home/repotrial/src/RepoTrial -- env PATH=/home/repotrial/.local/share/repotrial-uv/bin:/home/repotrial/.local/bin:/usr/local/bin:/usr/bin:/bin /home/repotrial/.local/share/repotrial-uv/bin/uv run playwright install chromium
```

Install the user-scoped pre-commit runner without changing `pyproject.toml` or
`uv.lock`; hook revisions remain controlled by `.pre-commit-config.yaml`:

```bash
uv tool install 'pre-commit==4.6.2'
```

Retain the uv directory on `PATH` when invoking tests because packaging tests
spawn `uv` by command name.

## Verification before Pilot execution

From the ext4 checkout, with the required trusted-client proxy environment if
applicable, verify:

```bash
sbx version
sbx diagnose --output json
sbx policy ls --type network --json
sbx list
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src/repotrial
uv run pytest -q
uv run pytest --cov=repotrial --cov-branch --cov-report=term-missing
uv run pre-commit run --all-files
git diff --check
```

Real SBX smoke/calibration is opt-in and must follow the reviewed commands in
the [execution plan](../superpowers/plans/2026-08-29-wsl2-linux-sbx-execution.md).
Never reinterpret default skips as real-runtime PASS. Require an empty `sbx list`
after every lifecycle.

## Upgrade invalidation conditions

The accepted calibration applies only while these identities remain unchanged:

- runtime inputs under `src/repotrial`, `pyproject.toml`, `uv.lock`, runtime
  configuration, and the trusted calibration fixture;
- `eval/real_repos.yaml` hash for cohort execution;
- Ubuntu release, WSL kernel, KVM availability, SBX package/version/commit,
  guest Compose version, and global network policy hash.

Any change to one of those inputs requires fresh calibration and evidence review
before Pilot execution. A docs-only descendant does not invalidate calibration
when both runtime-input and environment fingerprints remain identical. Never
upgrade SBX, WSL, Ubuntu, guest components, or policy in place and silently reuse
the old report.

## Troubleshooting

- **Doctor rejects the SBX version:** install/select the reviewed Linux SBX
  `v0.42.0`; current doctor rejects the historical `v0.39.0` runtime.
- **Docker JWKS/auth/GitHub timeout from the CLI:** test the trusted client proxy
  layer. `proxy.daemon` and `proxy.sandbox` do not configure the `sbx` client.
  Keep `NO_PROXY=localhost,127.0.0.1`; no daemon restart is needed for client
  environment variables.
- **Daemon image pull/auth timeout:** verify the daemon upstream setting and the
  LAN proxy/firewall path. Do not pass the upstream proxy directly into target
  Compose services.
- **Diagnose failure or non-empty inventory:** stop Pilot work, retain exact
  output and logs, and identify owned sandbox IDs. Do not reset or restart in a
  loop and do not delete state manually.
- **`/dev/kvm` or systemd unavailable:** treat the environment as unsupported;
  do not patch RepoTrial to bypass isolation.
- **Checkout reports `drvfs`/`9p` instead of ext4:** reclone into
  `/home/repotrial/src`; do not calibrate from a Windows-mounted path.
- **Native Windows sandboxd self-connect error:** it is retained historical
  evidence, not a reason to switch to Docker Desktop or host Compose.

## Safe retirement

Retirement is destructive and requires a separate Owner authorization. First
export or copy the retained evidence, then independently verify the exact
registered name `RepoTrial-Ubuntu` and exact location
`D:\DockerData\WSL\RepoTrial-Ubuntu`. Unregister only that exact distro, then
resolve and verify the dedicated cleanup target before removing only that
directory. Never use a name pattern or recursively remove a parent directory.

## Forbidden operations

- do not terminate, unregister, move, import/export, or modify the existing
  `Ubuntu` distro;
- do not modify or use `docker-desktop` for target execution;
- do not run `wsl --shutdown` or change global `.wslconfig` as part of this
  setup;
- do not manually move or edit an SBX state directory, socket, or VHD;
- do not use reset/restart loops to hide a runtime failure;
- do not run target Docker Compose, target commands, or a target Docker socket
  on Windows or the WSL host;
- do not fall back to host Docker, Docker Desktop, host Compose, or ad-hoc host
  shell execution.
