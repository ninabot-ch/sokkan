"""`kubernetes` runner: one Pod per session / agent run, created by the api through the
Kubernetes API with its own ServiceAccount (Role: pods, pods/log, secrets in the session
namespace — deploy/helm/sokkan/templates/runner-rbac.yaml).

A session pod:

* runs the session image as non-root under the `restricted` Pod Security Standard and
  OpenShift's restricted SCC: runAsNonRoot, no privilege escalation, ALL capabilities
  dropped, RuntimeDefault seccomp, read-only root fs, no fixed uid / fsGroup when
  SOKKAN_K8S_RUN_AS_USER is empty (OpenShift assigns them);
* gets its environment (model access, the session's vault values, its tokens) from a
  per-session Secret owned by the pod (garbage-collected with it), never from the pod spec;
* mounts only its working directory and transcript directory from the data PVC (subPath);
* has no ServiceAccount token, and NetworkPolicies let it reach the api's MCP relay, DNS and
  the egress gateway only; only the api reaches its supervisor port;
* has CPU / memory requests and limits and an activeDeadlineSeconds; the supervisor exits
  after SOKKAN_SESSION_IDLE_S without a client, and `reconcile` deletes finished pods.

api restart: pods are not owned by the api pod; `start` adopts the live pod of a session id
(same name, token derived from the instance key) instead of creating a second one.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import common
from .base import Handle, SessionRunner, SessionSpec, safe_name
from .mounts import SESSION_HOME, parse_mounts, plan

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"kubernetes API {status}: {body[:300]}")
        self.status = status


class KubeAPI:
    """Minimal Kubernetes REST client (stdlib): in-cluster ServiceAccount by default;
    SOKKAN_K8S_API_URL / _TOKEN_FILE / _CA_FILE / _NAMESPACE override it (dev, tests)."""

    def __init__(self) -> None:
        e = os.environ.get
        host, port = e("KUBERNETES_SERVICE_HOST"), e("KUBERNETES_SERVICE_PORT") or "443"
        self.base = (e("SOKKAN_K8S_API_URL") or (f"https://{host}:{port}" if host else "")
                     ).rstrip("/")
        self.token_file = e("SOKKAN_K8S_TOKEN_FILE") or f"{SA_DIR}/token"
        ca = e("SOKKAN_K8S_CA_FILE") or f"{SA_DIR}/ca.crt"
        ns_file = f"{SA_DIR}/namespace"
        self.namespace = e("SOKKAN_K8S_NAMESPACE") or (
            open(ns_file).read().strip() if os.path.exists(ns_file) else "default")
        self.ctx = ssl.create_default_context(cafile=ca if os.path.exists(ca) else None)

    def request(self, method: str, path: str, body: Any = None, query: dict | None = None,
                content_type: str = "application/json", raw: bool = False) -> Any:
        if not self.base:
            raise KubeError(0, "no Kubernetes API (not in a cluster, SOKKAN_K8S_API_URL unset)")
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        try:  # projected tokens rotate: read it at every call
            req.add_header("Authorization", "Bearer " + open(self.token_file).read().strip())
        except OSError:
            pass
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
                out = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise KubeError(e.code, e.read().decode("utf-8", "replace")) from None
        if raw:
            return out
        return json.loads(out) if out else None

    def _ns(self, kind: str, name: str = "", group: str = "/api/v1") -> str:
        return f"{group}/namespaces/{self.namespace}/{kind}" + (f"/{name}" if name else "")

    def create(self, kind: str, body: dict) -> dict:
        return self.request("POST", self._ns(kind), body)

    def get(self, kind: str, name: str) -> dict | None:
        try:
            return self.request("GET", self._ns(kind, name))
        except KubeError as e:
            if e.status == 404:
                return None
            raise

    def delete(self, kind: str, name: str) -> None:
        try:
            self.request("DELETE", self._ns(kind, name),
                         {"propagationPolicy": "Background", "gracePeriodSeconds": 5})
        except KubeError as e:
            if e.status != 404:
                raise

    def patch(self, kind: str, name: str, body: dict) -> dict:
        return self.request("PATCH", self._ns(kind, name), body,
                            content_type="application/merge-patch+json")

    def list(self, kind: str, selector: str) -> list[dict]:
        return (self.request("GET", self._ns(kind), query={"labelSelector": selector})
                or {}).get("items", [])

    def logs(self, name: str, tail: int = 200) -> str:
        return self.request("GET", self._ns("pods", name) + "/log",
                            query={"tailLines": str(tail)}, raw=True)

    def metrics(self, name: str) -> dict | None:
        try:
            return self.request("GET", self._ns("pods", name, "/apis/metrics.k8s.io/v1beta1"))
        except KubeError:
            return None


def config_from_env() -> dict:
    e = os.environ.get
    run_as = (e("SOKKAN_K8S_RUN_AS_USER") or "").strip()
    return {
        "image": e("SOKKAN_SESSION_IMAGE") or "ghcr.io/ninabot-ch/sokkan-session:latest",
        "pull_policy": e("SOKKAN_SESSION_IMAGE_PULL_POLICY") or "IfNotPresent",
        "pull_secret": e("SOKKAN_SESSION_IMAGE_PULL_SECRET") or "",
        "service_account": e("SOKKAN_K8S_SESSION_SERVICE_ACCOUNT") or "",
        "relay_addr": e("SOKKAN_RUNNER_RELAY_ADDR") or "sokkan-api-relay:8098",
        "proxy": e("SOKKAN_SESSION_HTTPS_PROXY") or "",
        "run_as_user": int(run_as) if run_as.isdigit() else None,
        "fs_group": (int(e("SOKKAN_K8S_FS_GROUP")) if (e("SOKKAN_K8S_FS_GROUP") or "").isdigit()
                     else None),
        "deadline_s": int(e("SOKKAN_SESSION_MAX_S") or 86400),
        "affinity_api": (e("SOKKAN_K8S_SESSION_AFFINITY") or "") == "api",
        "api_selector": json.loads(e("SOKKAN_K8S_API_POD_LABELS") or "{}"),
        "node_selector": json.loads(e("SOKKAN_K8S_SESSION_NODE_SELECTOR") or "{}"),
        "tolerations": json.loads(e("SOKKAN_K8S_SESSION_TOLERATIONS") or "[]"),
        "extra_labels": json.loads(e("SOKKAN_K8S_SESSION_LABELS") or "{}"),
        "mounts": parse_mounts(e("SOKKAN_RUNNER_MOUNTS") or "/data=pvc:sokkan-data"),
    }


def secret_manifest(spec: SessionSpec, cfg: dict, name: str) -> dict:
    env = common.container_env(spec, cfg["relay_addr"], cfg.get("proxy", ""))
    return {"apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": name, "labels": _labels(spec, cfg)},
            "type": "Opaque", "stringData": env}


def _labels(spec: SessionSpec, cfg: dict) -> dict:
    import re
    proj = re.sub(r"[^A-Za-z0-9_.-]", "-", spec.project or "default")[:63].strip("-_.") or "x"
    return {**cfg.get("extra_labels", {}),
            "app.kubernetes.io/name": "sokkan",
            common.LABEL_COMPONENT: "session",
            common.LABEL_INSTANCE: common.instance_label(),
            common.LABEL_SESSION: safe_name(spec.sid, "")[:63],
            common.LABEL_PROJECT: proj,
            common.LABEL_KIND: spec.kind}


def pod_manifest(spec: SessionSpec, cfg: dict, name: str, secret: str) -> dict:
    """The Pod of one session (pure: unit-tested for the restricted profile)."""
    volumes: list[dict] = [{"name": "home", "emptyDir": {"sizeLimit": "1Gi"}},
                           {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    mounts: list[dict] = [{"name": "home", "mountPath": SESSION_HOME},
                          {"name": "tmp", "mountPath": "/tmp"}]
    claims: dict[str, str] = {}
    for m in plan(spec.cwd, cfg["mounts"], make_dirs=cfg.get("make_dirs", True)):
        if m.kind != "pvc":
            raise ValueError(f"kubernetes runner: mount kind {m.kind} unsupported (pvc only)")
        vol = claims.setdefault(m.source, f"data-{len(claims)}")
        vm = {"name": vol, "mountPath": m.target, "readOnly": m.read_only}
        if m.subpath:
            vm["subPath"] = m.subpath
        mounts.append(vm)
    for claim, vol in claims.items():
        volumes.append({"name": vol, "persistentVolumeClaim": {"claimName": claim}})
    r = spec.resources
    psc: dict[str, Any] = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    if cfg.get("run_as_user") is not None:
        psc["runAsUser"] = cfg["run_as_user"]
        psc["runAsGroup"] = cfg["run_as_user"]
    if cfg.get("fs_group") is not None:
        psc["fsGroup"] = cfg["fs_group"]
    container = {
        "name": "session",
        "image": cfg["image"],
        "imagePullPolicy": cfg.get("pull_policy", "IfNotPresent"),
        "command": ["sokkan-session-supervisor"],
        "workingDir": spec.cwd,
        "envFrom": [{"secretRef": {"name": secret}}],
        "ports": [{"name": "supervisor", "containerPort": common.SUPERVISOR_PORT}],
        "resources": {"requests": {"cpu": r.cpu_request, "memory": r.memory_request},
                      "limits": {"cpu": r.cpu_limit, "memory": r.memory_limit}},
        "securityContext": {"allowPrivilegeEscalation": False, "privileged": False,
                            "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                            "capabilities": {"drop": ["ALL"]},
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "volumeMounts": mounts,
    }
    pod: dict[str, Any] = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "labels": _labels(spec, cfg),
                     "annotations": {"sokkan.ch/user": spec.user[:200]}},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "activeDeadlineSeconds": int(cfg.get("deadline_s", 86400)),
            "terminationGracePeriodSeconds": 15,
            "securityContext": psc,
            "containers": [container],
            "volumes": volumes,
        },
    }
    if cfg.get("service_account"):
        pod["spec"]["serviceAccountName"] = cfg["service_account"]
    if cfg.get("pull_secret"):
        pod["spec"]["imagePullSecrets"] = [{"name": cfg["pull_secret"]}]
    if cfg.get("node_selector"):
        pod["spec"]["nodeSelector"] = cfg["node_selector"]
    if cfg.get("tolerations"):
        pod["spec"]["tolerations"] = cfg["tolerations"]
    if cfg.get("affinity_api") and cfg.get("api_selector"):
        # ReadWriteOnce data volume: a session pod must land on the api's node
        pod["spec"]["affinity"] = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
            {"labelSelector": {"matchLabels": cfg["api_selector"]},
             "topologyKey": "kubernetes.io/hostname"}]}}
    return pod


def _phase(pod: dict | None) -> str:
    if not pod:
        return "gone"
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return "gone"
    ph = (pod.get("status") or {}).get("phase", "Pending")
    return {"Pending": "pending", "Running": "running", "Succeeded": "succeeded",
            "Failed": "failed"}.get(ph, "pending")


class KubernetesRunner(SessionRunner):
    name = "kubernetes"

    def __init__(self, api: KubeAPI | None = None, cfg: dict | None = None,
                 poll_s: float = 1.0, start_timeout_s: float | None = None):
        self.api = api or KubeAPI()
        self.cfg = cfg or config_from_env()
        self.poll_s = poll_s
        self.start_timeout_s = start_timeout_s or float(
            os.environ.get("SOKKAN_K8S_START_TIMEOUT_S") or 300)

    def _selector(self) -> str:
        return (f"{common.LABEL_COMPONENT}=session,"
                f"{common.LABEL_INSTANCE}={common.instance_label()}")

    def _handle(self, sid: str, pod: dict, adopted: bool) -> Handle:
        return Handle(sid, pod["metadata"]["name"],
                      host=(pod.get("status") or {}).get("podIP", ""),
                      port=common.SUPERVISOR_PORT,
                      token=common.token_for(sid, "supervisor"), adopted=adopted,
                      meta={"uid": pod["metadata"].get("uid", "")})

    async def _wait_running(self, name: str) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.start_timeout_s
        while True:
            pod = await asyncio.to_thread(self.api.get, "pods", name)
            ph = _phase(pod)
            if ph == "running" and (pod.get("status") or {}).get("podIP"):
                return pod  # type: ignore[return-value]
            if ph in ("failed", "succeeded", "gone"):
                raise RuntimeError(f"session pod {name} ended before it served ({ph}): "
                                   f"{_reason(pod)}")
            if loop.time() > deadline:
                raise TimeoutError(f"session pod {name} not running after "
                                   f"{self.start_timeout_s:.0f} s: {_reason(pod)}")
            await asyncio.sleep(self.poll_s)

    async def start(self, spec: SessionSpec) -> Handle:
        name = safe_name(spec.sid)
        secret = name + "-env"
        pod = await asyncio.to_thread(self.api.get, "pods", name)
        ph = _phase(pod)
        if ph in ("running", "pending") and pod:
            pod = await self._wait_running(name)
            return self._handle(spec.sid, pod, adopted=True)
        if pod:  # finished: replace it
            await self._delete(name, secret)
            await self._wait_gone(name)
        spec.token = spec.token or common.token_for(spec.sid, "supervisor")
        await asyncio.to_thread(self.api.delete, "secrets", secret)
        await asyncio.to_thread(self.api.create, "secrets",
                                secret_manifest(spec, self.cfg, secret))
        try:
            created = await asyncio.to_thread(
                self.api.create, "pods", pod_manifest(spec, self.cfg, name, secret))
        except Exception:
            await asyncio.to_thread(self.api.delete, "secrets", secret)
            raise
        # the Secret belongs to the pod: deleted with it by the garbage collector
        try:
            await asyncio.to_thread(self.api.patch, "secrets", secret, {"metadata": {
                "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": name,
                                     "uid": created["metadata"]["uid"]}]}})
        except Exception:  # noqa: BLE001 — stop() deletes it explicitly anyway
            pass
        pod = await self._wait_running(name)
        return self._handle(spec.sid, pod, adopted=False)

    async def _delete(self, name: str, secret: str) -> None:
        await asyncio.to_thread(self.api.delete, "pods", name)
        await asyncio.to_thread(self.api.delete, "secrets", secret)

    async def _wait_gone(self, name: str, timeout: float = 60) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await asyncio.to_thread(self.api.get, "pods", name) is None:
                return
            await asyncio.sleep(self.poll_s)

    async def stop(self, handle: Handle) -> None:
        await self._delete(handle.name, handle.name + "-env")

    async def status(self, handle: Handle) -> str:
        return _phase(await asyncio.to_thread(self.api.get, "pods", handle.name))

    async def usage(self, handle: Handle) -> dict:
        from .common import mem_to_bytes
        m = await asyncio.to_thread(self.api.metrics, handle.name)
        if not m:
            return {}
        cpu = mem = 0
        for c in m.get("containers") or []:
            u = c.get("usage") or {}
            v = str(u.get("cpu", "0"))
            cpu += (int(v[:-1]) // 1_000_000 if v.endswith("n") else
                    int(v[:-1]) if v.endswith("m") else int(float(v) * 1000))
            mem += mem_to_bytes(str(u.get("memory", "0")))
        return {"cpu_millicores": cpu, "memory_bytes": mem}

    async def logs(self, handle: Handle, tail: int = 200) -> str:
        return await asyncio.to_thread(self.api.logs, handle.name, tail)

    async def list_live(self) -> list[Handle]:
        out = []
        for p in await asyncio.to_thread(self.api.list, "pods", self._selector()):
            if _phase(p) in ("running", "pending"):
                sid = p["metadata"].get("labels", {}).get(common.LABEL_SESSION, "")
                out.append(Handle(sid, p["metadata"]["name"],
                                  host=(p.get("status") or {}).get("podIP", "")))
        return out

    async def reconcile(self) -> dict:
        kept, removed = [], []
        for p in await asyncio.to_thread(self.api.list, "pods", self._selector()):
            name = p["metadata"]["name"]
            if _phase(p) in ("running", "pending"):
                kept.append(name)
            else:
                await self._delete(name, name + "-env")
                removed.append(name)
        return {"kept": kept, "removed": removed}


def _reason(pod: dict | None) -> str:
    if not pod:
        return "deleted"
    st = pod.get("status") or {}
    for cs in st.get("containerStatuses") or []:
        for k in ("waiting", "terminated"):
            s = (cs.get("state") or {}).get(k)
            if s:
                return f"{s.get('reason', k)}: {s.get('message', '')}".strip(": ")
    for c in st.get("conditions") or []:
        if c.get("status") == "False" and c.get("message"):
            return c["message"]
    return st.get("phase", "?")
