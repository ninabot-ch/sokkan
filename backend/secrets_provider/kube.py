"""Kubernetes provider — Secrets of the instance's namespace (installs without OpenBao).

* Project secrets: one Secret per project, ``<prefix>-vault-<project>`` (labels
  ``sokkan.ch/vault=true``, ``sokkan.ch/instance=<prefix>``, annotation ``sokkan.ch/project``),
  one data entry per NAME.
* Data keys: the Secret ``<prefix>-data-keys`` (entries ``vault``, ``forge``, ``teams``, and
  ``<ctx>.next`` during a rotation). Written with the resourceVersion (optimistic concurrency).
* Access: the pod's ServiceAccount (token + CA mounted by Kubernetes); RBAC = get/list/create/
  update/patch/delete on ``secrets`` in its namespace (the chart's Role). Encryption at rest is
  the cluster's (etcd encryption / KMS provider) — say so to the customer's security team.
"""
from __future__ import annotations

import base64
import os
import re
import threading
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from secrets_provider import Health, SecretsError, SecretsProvider, key_path

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _b64(s: str | bytes) -> str:
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


class KubernetesProvider(SecretsProvider):
    name = "kubernetes"

    def __init__(self, api: str, namespace: str, token_file: str, ca: str = "", prefix: str = "sokkan",
                 timeout: float = 10.0):
        u = urlsplit(api)
        if u.scheme == "http" and (u.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
            raise SecretsError("SOKKAN_K8S_API must be https (plain http only on loopback)")
        if not _DNS.match(prefix) or len(prefix) > 30:
            raise SecretsError(f"SOKKAN_K8S_SECRETS_PREFIX={prefix!r}: a DNS label, ≤ 30 characters")
        self.api, self.ns, self.token_file = api.rstrip("/"), namespace, token_file
        self.ca, self.prefix, self.timeout = ca, prefix, timeout
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls, env=None) -> KubernetesProvider:
        env = os.environ if env is None else env
        api = (env.get("SOKKAN_K8S_API") or "").strip()
        if not api:
            host, port = env.get("KUBERNETES_SERVICE_HOST"), env.get("KUBERNETES_SERVICE_PORT")
            if not host:
                raise SecretsError("not in a Kubernetes pod (KUBERNETES_SERVICE_HOST) and "
                                   "SOKKAN_K8S_API is not set")
            api = f"https://{host}:{port or 443}"
        ns = (env.get("SOKKAN_K8S_NAMESPACE") or "").strip()
        if not ns:
            try:
                with open(os.path.join(SA_DIR, "namespace")) as f:
                    ns = f.read().strip()
            except OSError:
                raise SecretsError("SOKKAN_K8S_NAMESPACE is not set and no ServiceAccount "
                                   "namespace is mounted") from None
        ca = (env.get("SOKKAN_K8S_CA") or "").strip()
        if not ca and os.path.exists(os.path.join(SA_DIR, "ca.crt")):
            ca = os.path.join(SA_DIR, "ca.crt")
        return cls(api, ns, (env.get("SOKKAN_K8S_TOKEN_FILE") or os.path.join(SA_DIR, "token")),
                   ca=ca, prefix=(env.get("SOKKAN_K8S_SECRETS_PREFIX") or "sokkan").strip())

    # ---- HTTP ---------------------------------------------------------------------------------
    def _req(self, method: str, path: str, body: dict | None = None, params: dict | None = None,
             content_type: str = "application/json"):
        import httpx
        try:
            with open(self.token_file) as f:
                tok = f.read().strip()  # re-read: projected tokens are rotated by the kubelet
        except OSError as e:
            raise SecretsError(f"ServiceAccount token unreadable ({self.token_file}): "
                               f"{e.strerror}") from None
        try:
            with httpx.Client(base_url=self.api, verify=self.ca or True, timeout=self.timeout) as h:
                import json as _json
                return h.request(method, path, params=params,
                                 content=None if body is None else _json.dumps(body),
                                 headers={"authorization": f"Bearer {tok}",
                                          "content-type": content_type})
        except Exception as e:  # noqa: BLE001
            raise SecretsError(f"Kubernetes API unreachable at {self.api}: {type(e).__name__}: {e}") from None

    def _check(self, r, what: str, ok=(200, 201)):
        if r.status_code not in ok:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = ""
            raise SecretsError(f"Kubernetes {what}: HTTP {r.status_code}{' — ' + msg if msg else ''}")
        return r.json() if r.content else {}

    def _base(self) -> str:
        return f"/api/v1/namespaces/{self.ns}/secrets"

    def _get_secret(self, name: str) -> dict | None:
        r = self._req("GET", f"{self._base()}/{name}")
        if r.status_code == 404:
            return None
        return self._check(r, f"get secret {name}")

    def _vault_name(self, project: str) -> str:
        n = f"{self.prefix}-vault-{project}"
        if not _DNS.match(n) or len(n) > 253:
            raise SecretsError(f"project {project!r} cannot name a Kubernetes Secret")
        return n

    # ---- project secrets ------------------------------------------------------------------------
    def get(self, project: str, name: str) -> str | None:
        s = self._get_secret(self._vault_name(project))
        v = ((s or {}).get("data") or {}).get(name)
        return _unb64(v).decode() if v is not None else None

    def set(self, project: str, name: str, value: str) -> None:
        sn = self._vault_name(project)
        with self._lock:
            if self._get_secret(sn) is None:
                body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": sn, "labels": {
                            "sokkan.ch/vault": "true", "sokkan.ch/instance": self.prefix,
                            "app.kubernetes.io/managed-by": "sokkan"},
                            "annotations": {"sokkan.ch/project": project}},
                        "data": {name: _b64(value)}}
                r = self._req("POST", self._base(), body)
                if r.status_code != 409:
                    self._check(r, f"create secret {sn}")
                    return
            r = self._req("PATCH", f"{self._base()}/{sn}", {"data": {name: _b64(value)}},
                          content_type="application/merge-patch+json")
            self._check(r, f"patch secret {sn}")

    def delete(self, project: str, name: str) -> None:
        sn = self._vault_name(project)
        if self._get_secret(sn) is None:
            return
        r = self._req("PATCH", f"{self._base()}/{sn}", {"data": {name: None}},
                      content_type="application/merge-patch+json")
        self._check(r, f"patch secret {sn}")

    def list(self, project: str) -> list[str]:
        s = self._get_secret(self._vault_name(project))
        return sorted(((s or {}).get("data") or {}).keys())

    def projects(self) -> list[str]:
        r = self._req("GET", self._base(), params={
            "labelSelector": f"sokkan.ch/vault=true,sokkan.ch/instance={self.prefix}"})
        items = self._check(r, "list secrets").get("items") or []
        return sorted(((i.get("metadata") or {}).get("annotations") or {}).get("sokkan.ch/project", "")
                      for i in items if i.get("data"))

    # ---- data keys --------------------------------------------------------------------------
    def _dk_name(self) -> str:
        return f"{self.prefix}-data-keys"

    def _dk_update(self, fn) -> dict:
        """Read-modify-write of the data-keys Secret with its resourceVersion (409 → retry)."""
        for _ in range(5):
            s = self._get_secret(self._dk_name())
            if s is None:
                data = fn({})
                body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": self._dk_name(), "labels": {
                            "sokkan.ch/data-keys": "true", "sokkan.ch/instance": self.prefix,
                            "app.kubernetes.io/managed-by": "sokkan"}},
                        "data": data}
                r = self._req("POST", self._base(), body)
                if r.status_code == 409:
                    continue
                return self._check(r, "create data-keys secret").get("data") or {}
            data = dict(s.get("data") or {})
            new = fn(dict(data))
            if new == data:
                return data
            s["data"] = new
            r = self._req("PUT", f"{self._base()}/{self._dk_name()}", s)
            if r.status_code == 409:
                continue
            return self._check(r, "update data-keys secret").get("data") or {}
        raise SecretsError("data-keys secret: too many concurrent updates")

    def data_keys(self, ctx: str, create: bool = True) -> list[bytes]:
        s = self._get_secret(self._dk_name())
        data = (s or {}).get("data") or {}
        if ctx not in data:
            if not create:
                return []
            if os.path.exists(key_path(ctx)):
                raise SecretsError(f"{os.path.basename(key_path(ctx))} is still a clear key file: "
                                   "run scripts/secrets-migrate.py --from file --to kubernetes")
            k = Fernet.generate_key()

            def add(d):
                d.setdefault(ctx, _b64(k))
                return d
            data = self._dk_update(add)
        out = [_unb64(data[f"{ctx}.next"])] if f"{ctx}.next" in data else []
        return out + [_unb64(data[ctx])]

    def add_data_key(self, ctx: str, key: bytes | None = None) -> bytes:
        self.data_keys(ctx)
        k = key or Fernet.generate_key()

        def add(d):
            d.setdefault(f"{ctx}.next", _b64(k))
            return d
        return _unb64(self._dk_update(add)[f"{ctx}.next"])

    def drop_old_data_keys(self, ctx: str) -> int:
        def promote(d):
            if f"{ctx}.next" in d:
                d[ctx] = d.pop(f"{ctx}.next")
            return d
        before = self.data_keys(ctx, create=False)
        self._dk_update(promote)
        return max(0, len(before) - 1)

    def import_data_keys(self, ctx: str, keys: list[bytes]) -> None:
        if not keys:
            return
        if len(keys) > 2:
            raise SecretsError(f"{ctx}: {len(keys)} data keys — finish the rotation first")
        have = self.data_keys(ctx, create=False)
        if have == list(keys):
            return
        if have:
            raise SecretsError(f"the data-keys Secret already holds a different {ctx} key")

        def put(d):
            d[ctx] = _b64(keys[-1])
            if len(keys) == 2:
                d[f"{ctx}.next"] = _b64(keys[0])
            return d
        self._dk_update(put)

    # ---- operations ---------------------------------------------------------------------------
    def health(self) -> Health:
        checks = {}
        try:
            checks["projects"] = len(self.projects())
            s = self._get_secret(self._dk_name())
            checks["data_keys"] = sorted(((s or {}).get("data") or {}).keys()) if s else "not created yet"
        except SecretsError as e:
            return Health(False, self.name, str(e), checks)
        return Health(True, self.name, f"Secrets of namespace {self.ns} readable", checks)

    def describe(self) -> dict:
        return {"provider": self.name, "api": self.api, "namespace": self.ns,
                "secrets": f"{self.prefix}-vault-<project>, {self.prefix}-data-keys"}
