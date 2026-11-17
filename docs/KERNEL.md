# The kernel engine (4.0 « Seven seas »)

SOKKAN 4.0 can run its sessions on a second engine: Nokker's agent kernel, a container image
next to the api (`docker-compose.kernel.yml`, profile `kernel`). The default engine stays the
Claude Code CLI (`legacy`); the kernel engine is **off unless you switch it on**.

What changes for the people: nothing on screen. What changes for the operator: two more kinds
of containers on the machine — the kernel's own services, and one short-lived **sandbox**
container per working session, where the model's commands and file edits run.

## Prerequisites

- **Docker 26 or newer** with Compose v2.24 or newer (the kernel mounts sub-folders of
  SOKKAN's data volume, `volume.subpath`). `scripts/kernel.sh enable` checks the version.
- **cgroup controllers delegated to Docker** (memory and cpu). Each sandbox is created with a
  memory and a CPU cap; when Docker cannot apply them the kernel refuses the session rather
  than run it without caps. On a current Linux with cgroup v2 and the rootful Docker of the
  distribution, nothing to do. Check: `docker info --format '{{.CgroupVersion}} {{.CgroupDriver}}'`
  → `2 systemd`, and no « No memory limit support » warning in `docker info`. Rootless Docker:
  the user's systemd unit needs `Delegate=cpu cpuset io memory pids`
  (`/etc/systemd/system/user@.service.d/delegate.conf`).
- A **local volume driver** for `sokkan-data` (the default): the sandboxes mount project folders
  by their path on the host.
- The kernel image, **pinned** (a version tag or a digest, never `latest`).

## Switch it on

```sh
cd /opt/sokkan                       # your install folder
sh scripts/kernel.sh enable --image <registry>/<kernel>:<version>
docker compose up -d --remove-orphans
```

At install time: `install.sh --kernel <image>` (or `SOKKAN_KERNEL_IMAGE=<image>`) does the same.
`upgrade.sh` keeps it on and refreshes its host paths at every upgrade.

`enable` writes to `.env` only: `docker-compose.kernel.yml` in `COMPOSE_FILE`, `kernel` in
`COMPOSE_PROFILES`, `SOKKAN_ENGINE_RUNTIME=kernel` (skip it with `--keep-engine` to run the
service without moving the sessions), the two passwords of the kernel's database roles, the
name of the sandbox network (one per install), and two host paths:

| Variable | What |
|---|---|
| `SOKKAN_KERNEL_DATA_HOST` | where Docker keeps the volume `sokkan-data` on the host (`docker volume inspect`) |
| `SOKKAN_KERNEL_WORKSPACE_HOST` | the default project folder (`SOKKAN_WORKSPACE`), absolute |

Then the model endpoints: copy `deploy/kernel/config.example.yaml` to
`deploy/kernel/config.yaml` and set `tiers` (the appliance's router is the default). The key, if
any, goes in `.env` as `SOKKAN_KERNEL_MODEL_KEY`.

## What runs

| Service | Role |
|---|---|
| `kernel` | the engine. uid 1000, read-only, no capability, no published port. Sees the default project, the projects' folders and nothing else of SOKKAN's data (no key, no database file) |
| `kernel-dockerproxy` | the **only** way from the kernel to Docker. The raw socket is mounted here and nowhere else |
| `kernel-egress-filter` | the only thing a sandbox can reach: decides which names may be reached |
| `kernel-egress` | behind the filter: resolves the name, refuses every private, loopback, link-local and Docker address |
| `kernel-init`, `kernel-db`, `kernel-migrate` | one-shot: volumes, the schema `kernel` in SOKKAN's Postgres (roles `kernel_owner`, `kernel_app`, no right on SOKKAN's tables), its migrations |

The kernel's configuration is **rendered by the api at every start**
(`backend/harness/kernel_config.py`): your `deploy/kernel/config.yaml` with SOKKAN's block on
top. The block is not yours to change in that file — it is written again at every start:

- the sandbox (`local.code.backend: docker`, `require_sandbox: true`): with the `local`
  backend the model's commands would run in the kernel's own container, where the model could
  read the kernel's token and approve its own cards. The kernel refuses to start otherwise;
- `local.instance`: the label of this install's sandboxes on the Docker daemon, derived from
  SOKKAN's instance key (`engine-identity.key`, in the backups). Stable across upgrades and
  restarts, different for two installs on one daemon: at start-up the kernel removes the
  leftover sandboxes of ITS instance only;
- the host flags SOKKAN runs with (`surfaces_gate`, `strict_resume`, `mcp_session_headers`,
  `conclude_on_green`, `chain_max_calls`), the kernel's own memory tool off.

A wrong setting stops the render with the variable's name in the api's log
(`docker compose logs api | grep kernel-config`); the kernel then does not start, the api does.

## The socket proxy

The kernel starts sandboxes with the Docker CLI against `kernel-dockerproxy`
(`DOCKER_HOST=tcp://kernel-dockerproxy:2375`, a network only the two share). The proxy is the
kernel's own (`dockerproxy.py` of its `sandboxd` image) and answers 403 to anything but:

- probes (`_ping`, `version`, `info`), image inspect and pull;
- create / start / exec / inspect / stop / remove of containers **labelled `nokker.sandbox`**,
  and the listing of those only;
- a create is refused if it asks for: privileged mode, a capability other than DAC_OVERRIDE,
  CHOWN, FOWNER, a host namespace, a device, a published port, a network other than the sandbox
  network or none, a missing `no-new-privileges`, or a bind mount outside the folders of
  `NOKKER_HOST_WORKSPACE` (the default project, `projects/` and `engine-roots/` of the data
  volume). Named volumes are refused.

## The sandbox's network

`SOKKAN_KERNEL_SANDBOX_NET` (`.env`, or `kernel.sh enable --net`):

| Value | A sandbox reaches |
|---|---|
| `none` (default) | nothing: started without any network. An Atelier session needs none (it has no shell) |
| `registries` | the package registries of `SOKKAN_KERNEL_EGRESS_ALLOW` (default: PyPI, npm, Go, crates.io, Maven Central, RubyGems, Debian), ports 80/443, by name only |
| `open` | any public address. Private ranges (your LAN, the box, Postgres, cloud metadata) stay refused |

Through `kernel-egress-filter` → `kernel-egress` in both cases; the sandbox network is internal.

## Caps

| Variable | Default | |
|---|---|---|
| `SOKKAN_KERNEL_SANDBOX_MEMORY` | `2g` | memory of each sandbox (`512m`…) |
| `SOKKAN_KERNEL_SANDBOX_CPUS` | `2` | CPUs of each sandbox (`1.5`…) |
| `SOKKAN_KERNEL_SANDBOX_IDLE_S` | `1800` | a sandbox without a tool call for this long is removed (the session stays; 0 = never) |
| `SOKKAN_KERNEL_CHAIN_MAX_CALLS` | `60` | model calls of one request, approval continuations included |

A sandbox is removed when its session ends (`DELETE` of the session), when it idles, when the
kernel stops; one left by a crash is removed at the kernel's next start.

## `sandbox_unavailable`

« The agent's isolated workspace could not start » in the simple profile, an error
`sandbox_unavailable` in the technical pane: the kernel could not create the session's sandbox
with its caps. Nothing is lost; the next message tries again. Check, in this order:

1. `docker compose logs kernel-dockerproxy | grep refus` — a create refused by the proxy (a
   host path that moved: `sh scripts/kernel.sh refresh`, then `docker compose up -d`);
2. `docker info` warnings about memory / CPU limits — the cgroup delegation above;
3. `docker compose logs kernel | grep -i sandbox` — the kernel's own reason.

## Switch it off

```sh
sh scripts/kernel.sh disable
docker compose up -d --remove-orphans
```

New sessions run on the legacy engine again. The kernel's volumes, its schema `kernel` and its
settings in `.env` are kept; `enable` brings them back. `scripts/rollback.sh` to a release
without the kernel engine removes `docker-compose.kernel.yml` with the rest of the newer files.

## Known limits

- The socket proxy scopes to `nokker.sandbox`, not to one install: two SOKKAN installs on one
  Docker daemon do not reap each other's sandboxes (different `local.instance`), but a
  compromised kernel of one could reach the sandboxes of the other through its proxy. One
  install per Docker daemon is the recommended setup.
- Two installs restored from the SAME backup share the instance key, hence the label: give one
  of them a new key (`engine-identity.key`) before running both on one daemon.
- The kernel image is pulled by Compose; its signature is not yet verified by `upgrade.sh`.
