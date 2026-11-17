"""The kernel engine's configuration for a REAL SOKKAN instance (4.0, docker-compose.kernel.yml).

Run once at every start by the compose service ``kernel-config`` (the api's image, the api's
uid, SOKKAN's data volume mounted), before the kernel: it reads the operator's file
(``deploy/kernel/config.yaml``, else the shipped ``config.example.yaml``), puts on top of it
everything SOKKAN decides for the kernel, and writes the result where the kernel reads it
(``SOKKAN_KERNEL_CONFIG_OUT``, a volume of its own). The operator's file keeps what is theirs
(model endpoints, tiers, web features); the block below is NOT theirs to change, it is
written again at every start:

* ``local.instance`` — the label of this kernel's sandboxes on the docker daemon (the kernel
  reaps the sandboxes of ITS instance at start-up). Derived from the instance key of SOKKAN
  (``harness.identity.instance_label``): stable across upgrades and restarts, different for
  two instances on one daemon. Left to the kernel, it is a hash of ``/opt/data`` as the
  container sees it — the same for every containerised kernel, which then remove each
  other's sandboxes (kernel gap n°23).
* ``local.code`` — the docker sandbox, never ``local`` (``require_sandbox``: in the local
  backend the model could read the kernel's token and approve its own cards). The daemon is
  reached through the socket proxy of the compose file (``DOCKER_HOST``), never the raw
  socket. The project folders are bind-mounted into a sandbox by their path ON THE HOST
  (``host_roots``): the proxy refuses any other mount, named volumes included.
* the sandbox's caps (memory, CPU, idle lifetime) and its network: ``none`` (default — no
  network at all), ``registries`` (package registries only, through the compose file's
  egress filter) or ``open`` (any public address; private ranges are always refused).
* the host flags SOKKAN ships with (kernel ``docs/HOST-EMBED.md``): surfaces_gate,
  strict_resume, mcp_session_headers, conclude_on_green, chain_max_calls; ``memory_direct``
  removed (a note goes through SOKKAN's own ``memory_write``, in quarantine).

Every value comes from the environment of the ``kernel-config`` service (the compose file
declares each one — tests/test_compose_env.py). A wrong value stops here, with its name.
"""
from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path
from typing import Mapping

import yaml

# the kernel's view of SOKKAN's files (docker-compose.kernel.yml mounts them there)
WORKSPACE = "/workspace"                      # the instance's default project
PROJECTS = "/data/projects"                   # the projects' spaces (sub-path of sokkan-data)
ENGINE_ROOTS = "/data/engine-roots"           # their opaque aliases (harness/identity.py)
STATE_DIR = "/opt/data"
TOKEN_PATH = "/run/kernel/local-token"
NETWORK_ENV = "NOKKER_SANDBOX_NETWORK"        # the sandbox network's name, set by the compose file

NETS = ("none", "registries", "open")
DEFAULTS = {
    "SOKKAN_KERNEL_SANDBOX_MEMORY": "2g",
    "SOKKAN_KERNEL_SANDBOX_CPUS": "2",
    "SOKKAN_KERNEL_SANDBOX_IDLE_S": "1800",
    "SOKKAN_KERNEL_SANDBOX_NET": "none",
    "SOKKAN_KERNEL_CHAIN_MAX_CALLS": "60",
}
# the host flags as SOKKAN ships them (the parity bench runs with exactly these)
HOST_FLAGS = {"surfaces_gate": True, "strict_resume": True, "mcp_session_headers": True,
              "conclude_on_green": True}
_MEM = re.compile(r"^(\d+)([mg])$")
_CPUS = re.compile(r"^\d+(\.\d{1,2})?$")


class ConfigError(ValueError):
    """A setting of the kernel engine is missing or wrong: the kernel does not start."""


def _get(env: Mapping[str, str], name: str) -> str:
    v = (env.get(name) or "").strip()
    return v or DEFAULTS.get(name, "")


def _host_path(env: Mapping[str, str], name: str) -> str:
    v = _get(env, name)
    if not v:
        raise ConfigError(f"{name} is not set: the path, ON THE DOCKER HOST, of the folder the "
                          "kernel's sandboxes mount (scripts/kernel.sh enable sets it)")
    if not v.startswith("/") or v.rstrip("/") == "" or any(p == ".." for p in v.split("/")):
        raise ConfigError(f"{name}={v!r}: an absolute path other than / is required")
    return v.rstrip("/")


def memory(env: Mapping[str, str]) -> str:
    v = _get(env, "SOKKAN_KERNEL_SANDBOX_MEMORY").lower()
    m = _MEM.match(v)
    mb = int(m.group(1)) * (1024 if m.group(2) == "g" else 1) if m else 0
    if not m or not 64 <= mb <= 1024 * 1024:
        raise ConfigError(f"SOKKAN_KERNEL_SANDBOX_MEMORY={v!r}: an integer and m or g, 64m to 1024g "
                          "(e.g. 2g)")
    return v


def cpus(env: Mapping[str, str]) -> float | int:
    v = _get(env, "SOKKAN_KERNEL_SANDBOX_CPUS")
    if not _CPUS.match(v) or not 0.1 <= float(v) <= 256:
        raise ConfigError(f"SOKKAN_KERNEL_SANDBOX_CPUS={v!r}: a number from 0.1 to 256, two "
                          "decimals at most (e.g. 2 or 1.5)")
    f = float(v)
    return int(f) if f.is_integer() else f


def idle_s(env: Mapping[str, str]) -> int:
    v = _get(env, "SOKKAN_KERNEL_SANDBOX_IDLE_S")
    if not v.isdigit() or (int(v) != 0 and not 60 <= int(v) <= 30 * 86400):
        raise ConfigError(f"SOKKAN_KERNEL_SANDBOX_IDLE_S={v!r}: 0 (never) or 60 to 2592000 seconds")
    return int(v)


def net(env: Mapping[str, str]) -> str:
    v = _get(env, "SOKKAN_KERNEL_SANDBOX_NET").lower()
    if v not in NETS:
        raise ConfigError(f"SOKKAN_KERNEL_SANDBOX_NET={v!r}: one of {', '.join(NETS)}")
    return v


def chain_max(env: Mapping[str, str]) -> int:
    v = _get(env, "SOKKAN_KERNEL_CHAIN_MAX_CALLS")
    if not v.isdigit() or not 1 <= int(v) <= 1000:
        raise ConfigError(f"SOKKAN_KERNEL_CHAIN_MAX_CALLS={v!r}: an integer from 1 to 1000")
    return int(v)


def render(base: dict, env: Mapping[str, str], instance: str) -> dict:
    """The kernel's configuration: the operator's ``base`` with SOKKAN's block on top."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{3,62}", instance or ""):
        raise ConfigError(f"instance label {instance!r} is not a valid label")
    image = _get(env, "SOKKAN_KERNEL_SANDBOX_IMAGE")
    if not image:
        raise ConfigError("SOKKAN_KERNEL_SANDBOX_IMAGE is not set: the image of the kernel's "
                          "sandboxes, pinned (the socket proxy only creates those)")
    data_host = _host_path(env, "SOKKAN_KERNEL_DATA_HOST")
    ws_host = _host_path(env, "SOKKAN_KERNEL_WORKSPACE_HOST")
    mode = net(env)
    cfg = copy.deepcopy(base) if isinstance(base, dict) else {}
    local = cfg.get("local") if isinstance(cfg.get("local"), dict) else {}
    cfg["local"] = local
    local["state_dir"] = STATE_DIR
    local["token_path"] = TOKEN_PATH
    local.setdefault("settings_path", f"{STATE_DIR}/settings.json")
    local["instance"] = instance
    local.update(HOST_FLAGS)
    local["chain_max_calls"] = chain_max(env)
    local.pop("memory_direct", None)       # off: absent, as the kernel's default
    code = local.get("code") if isinstance(local.get("code"), dict) else {}
    local["code"] = code
    for k in ("host_volumes", "network_name", "volume"):
        code.pop(k, None)                  # the proxy refuses a named volume; the name is the env's
    code.update({
        "backend": "docker",
        "require_sandbox": True,
        "image": image,
        "roots": [WORKSPACE, PROJECTS, ENGINE_ROOTS],
        # the folder as the kernel sees it → the same folder as the docker daemon sees it
        "host_roots": {WORKSPACE: ws_host, PROJECTS: f"{data_host}/projects",
                       ENGINE_ROOTS: f"{data_host}/engine-roots"},
        "memory": memory(env),
        "cpus": cpus(env),
        "idle_ttl_s": idle_s(env),
        "network": mode != "none",
        "network_name_env": NETWORK_ENV,
    })
    return cfg


def proxy_workspaces(env: Mapping[str, str]) -> str:
    """The host folders the socket proxy lets a sandbox mount (its ``NOKKER_HOST_WORKSPACE``)."""
    d = _host_path(env, "SOKKAN_KERNEL_DATA_HOST")
    return ":".join([_host_path(env, "SOKKAN_KERNEL_WORKSPACE_HOST"), f"{d}/projects",
                     f"{d}/engine-roots"])


def _base(src: Path) -> tuple[dict, Path]:
    for name in ("config.yaml", "config.example.yaml"):
        p = src / name
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ConfigError(f"{p}: not a YAML mapping")
            return data, p
    raise ConfigError(f"no config.yaml nor config.example.yaml in {src}")


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    src = Path(env.get("SOKKAN_KERNEL_CONFIG_SRC") or "/etc/kernel/src")
    out = Path(env.get("SOKKAN_KERNEL_CONFIG_OUT") or "/run/kernel-config/config.yaml")
    try:
        base, used = _base(src)
        from harness import identity
        instance = identity.instance_label("kernel")
        cfg = render(base, env, instance)
        # the folders the sandboxes mount must exist on the volume before the kernel mounts
        # them (compose `volume.subpath` refuses a missing one): created as the api's uid
        data = Path(env.get("SOKKAN_DATA_DIR") or "/data")
        for sub in ("projects", "engine-roots"):
            (data / sub).mkdir(parents=True, exist_ok=True)
    except (ConfigError, OSError) as e:
        print(f"[sokkan kernel-config] ✗ {e}", file=sys.stderr)
        return 2
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("# written by SOKKAN at every start (backend/harness/kernel_config.py) from "
                   f"{used.name}: edit that file, not this one\n"
                   + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, out)
    code = cfg["local"]["code"]
    print(f"[sokkan kernel-config] instance {instance} · sandbox {code['image']} · "
          f"memory {code['memory']} · cpus {code['cpus']} · network {net(env)} · from {used.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
