"""`docker` runner: one container per session / agent run on the compose host.

The api talks to a Docker Engine API (`SOKKAN_DOCKER_HOST`: `unix:///var/run/docker.sock`
or `tcp://docker-proxy:2375`) — see docs/enterprise/KUBERNETES.md § docker runner for the
trade-off: whoever reaches that API can start any container. Each session container:

* runs the session image (`SOKKAN_SESSION_IMAGE`) as a non-root uid, read-only root fs,
  all capabilities dropped, no-new-privileges, pids/CPU/memory limits;
* mounts only its working directory and its transcript directory (runner/mounts.py);
* sits on an INTERNAL network (`SOKKAN_DOCKER_NETWORK`, no route out): it reaches the api's
  MCP relay and the egress gateway (model endpoint, allowed forges), nothing else.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import urllib.parse
from typing import Any

from . import common
from .base import Handle, SessionRunner, SessionSpec
from .mounts import SESSION_HOME, parse_mounts, plan


class DockerError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"docker API {status}: {body[:300]}")
        self.status = status


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 60):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


class DockerAPI:
    """Minimal Docker Engine API client (stdlib): the calls the runner needs, nothing else."""

    def __init__(self, host: str | None = None, version: str = "v1.45"):
        self.host = host or os.environ.get("SOKKAN_DOCKER_HOST") or os.environ.get(
            "DOCKER_HOST") or "unix:///var/run/docker.sock"
        self.version = version

    def _conn(self) -> http.client.HTTPConnection:
        u = urllib.parse.urlparse(self.host)
        if u.scheme == "unix":
            return _UnixConn(u.path)
        return http.client.HTTPConnection(u.hostname or "localhost", u.port or 2375, timeout=60)

    def request(self, method: str, path: str, body: Any = None,
                query: dict | None = None) -> Any:
        q = ("?" + urllib.parse.urlencode(query)) if query else ""
        c = self._conn()
        try:
            data = json.dumps(body).encode() if body is not None else None
            c.request(method, f"/{self.version}{path}{q}", body=data,
                      headers={"Content-Type": "application/json"} if data else {})
            r = c.getresponse()
            raw = r.read().decode("utf-8", "replace")
        finally:
            c.close()
        if r.status >= 400:
            raise DockerError(r.status, raw)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    # the calls ------------------------------------------------------------------------
    def create(self, name: str, body: dict) -> str:
        return self.request("POST", "/containers/create", body, {"name": name})["Id"]

    def start(self, cid: str) -> None:
        self.request("POST", f"/containers/{cid}/start")

    def inspect(self, cid: str) -> dict | None:
        try:
            return self.request("GET", f"/containers/{cid}/json")
        except DockerError as e:
            if e.status == 404:
                return None
            raise

    def remove(self, cid: str) -> None:
        try:
            self.request("DELETE", f"/containers/{cid}", query={"force": "true", "v": "true"})
        except DockerError as e:
            if e.status != 404:
                raise

    def list(self, labels: list[str]) -> list[dict]:
        return self.request("GET", "/containers/json",
                            query={"all": "true",
                                   "filters": json.dumps({"label": labels})}) or []

    def stats(self, cid: str) -> dict:
        return self.request("GET", f"/containers/{cid}/stats",
                            query={"stream": "false"}) or {}

    def logs(self, cid: str, tail: int = 200) -> str:
        out = self.request("GET", f"/containers/{cid}/logs",
                           query={"stdout": "1", "stderr": "1", "tail": str(tail)})
        return out if isinstance(out, str) else json.dumps(out)


def container_body(spec: SessionSpec, cfg: dict) -> dict:
    """The create body of a session container (pure: unit-tested)."""
    env = common.container_env(spec, cfg["relay_addr"], cfg.get("proxy", ""))
    mounts = []
    for m in plan(spec.cwd, cfg["mounts"], make_dirs=cfg.get("make_dirs", True)):
        if m.kind == "bind":
            mounts.append({"Type": "bind", "Source": m.source, "Target": m.target})
        else:
            v: dict[str, Any] = {"Type": "volume", "Source": m.source, "Target": m.target}
            if m.subpath:
                v["VolumeOptions"] = {"Subpath": m.subpath}  # Engine API ≥ 1.45
            mounts.append(v)
    r = spec.resources
    mem = common.mem_to_bytes(r.memory_limit)
    return {
        "Image": cfg["image"],
        "Cmd": ["sokkan-session-supervisor"],
        "Env": [f"{k}={v}" for k, v in sorted(env.items())],
        "User": cfg.get("user") or "1000:1000",
        "WorkingDir": spec.cwd,
        "Labels": {common.LABEL_SESSION: spec.sid, common.LABEL_PROJECT: spec.project,
                   common.LABEL_INSTANCE: common.instance_label(), common.LABEL_KIND: spec.kind},
        "ExposedPorts": {f"{common.SUPERVISOR_PORT}/tcp": {}},
        "HostConfig": {
            "NanoCpus": common.cpu_to_nano(r.cpu_limit),
            "Memory": mem, "MemorySwap": mem,           # no swap: the limit is the limit
            "MemoryReservation": common.mem_to_bytes(r.memory_request),
            "PidsLimit": int(cfg.get("pids", 1024)),
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,nosuid,size=1g,mode=1777",
                      SESSION_HOME: "rw,nosuid,size=1g,mode=1777"},
            "NetworkMode": cfg["network"],
            "Mounts": mounts,
            "RestartPolicy": {"Name": "no"},
            "Privileged": False,
        },
    }


class DockerRunner(SessionRunner):
    name = "docker"

    def __init__(self, api: DockerAPI | None = None, cfg: dict | None = None):
        self.api = api or DockerAPI()
        e = os.environ.get
        self.cfg = cfg or {
            "image": e("SOKKAN_SESSION_IMAGE") or "sokkan-session:latest",
            "network": e("SOKKAN_DOCKER_NETWORK") or "sokkan-sessions",
            "relay_addr": e("SOKKAN_RUNNER_RELAY_ADDR") or "api:8098",
            "proxy": e("SOKKAN_SESSION_HTTPS_PROXY") or "",
            "user": e("SOKKAN_DOCKER_SESSION_USER") or "1000:1000",
            "mounts": parse_mounts(e("SOKKAN_RUNNER_MOUNTS")
                                   or "/data=volume:sokkan-data,/workspace=bind:/workspace"),
        }

    def _labels(self) -> list[str]:
        return [f"{common.LABEL_INSTANCE}={common.instance_label()}", common.LABEL_SESSION]

    def _handle(self, sid: str, info: dict, adopted: bool) -> Handle:
        nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
        ip = (nets.get(self.cfg["network"]) or {}).get("IPAddress") or ""
        name = (info.get("Name") or "").lstrip("/") or common.LABEL_SESSION
        return Handle(sid, name, host=ip or name, port=common.SUPERVISOR_PORT,
                      token=common.token_for(sid, "supervisor"), adopted=adopted,
                      meta={"id": info.get("Id", "")})

    async def start(self, spec: SessionSpec) -> Handle:
        from .base import safe_name
        name = safe_name(spec.sid)
        info = await asyncio.to_thread(self.api.inspect, name)
        if info and (info.get("State") or {}).get("Running"):
            return self._handle(spec.sid, info, adopted=True)
        if info:  # finished: replace it
            await asyncio.to_thread(self.api.remove, name)
        spec.token = spec.token or common.token_for(spec.sid, "supervisor")
        body = container_body(spec, self.cfg)
        cid = await asyncio.to_thread(self.api.create, name, body)
        await asyncio.to_thread(self.api.start, cid)
        info = await asyncio.to_thread(self.api.inspect, cid) or {}
        return self._handle(spec.sid, info, adopted=False)

    async def stop(self, handle: Handle) -> None:
        await asyncio.to_thread(self.api.remove, handle.meta.get("id") or handle.name)

    async def status(self, handle: Handle) -> str:
        info = await asyncio.to_thread(self.api.inspect, handle.meta.get("id") or handle.name)
        if not info:
            return "gone"
        st = info.get("State") or {}
        if st.get("Running"):
            return "running"
        if st.get("Status") == "created":
            return "pending"
        return "succeeded" if st.get("ExitCode") == 0 else "failed"

    async def usage(self, handle: Handle) -> dict:
        s = await asyncio.to_thread(self.api.stats, handle.meta.get("id") or handle.name)
        cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
        d_cpu = ((cpu.get("cpu_usage") or {}).get("total_usage", 0)
                 - (pre.get("cpu_usage") or {}).get("total_usage", 0))
        d_sys = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or 1
        mem = s.get("memory_stats") or {}
        inactive = (mem.get("stats") or {}).get("inactive_file", 0)
        return {"cpu_millicores": round(1000 * ncpu * d_cpu / d_sys) if d_sys > 0 else 0,
                "memory_bytes": max(0, mem.get("usage", 0) - inactive)}

    async def logs(self, handle: Handle, tail: int = 200) -> str:
        return await asyncio.to_thread(self.api.logs, handle.meta.get("id") or handle.name, tail)

    async def list_live(self) -> list[Handle]:
        out = []
        for c in await asyncio.to_thread(self.api.list, self._labels()):
            if c.get("State") == "running":
                sid = (c.get("Labels") or {}).get(common.LABEL_SESSION, "")
                out.append(Handle(sid, (c.get("Names") or ["?"])[0].lstrip("/"),
                                  meta={"id": c.get("Id", "")}))
        return out

    async def reconcile(self) -> dict:
        kept, removed = [], []
        for c in await asyncio.to_thread(self.api.list, self._labels()):
            name = (c.get("Names") or ["?"])[0].lstrip("/")
            if c.get("State") == "running":
                kept.append(name)
            else:
                await asyncio.to_thread(self.api.remove, c.get("Id") or name)
                removed.append(name)
        return {"kept": kept, "removed": removed}
