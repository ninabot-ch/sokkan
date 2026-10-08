"""OpenBao / HashiCorp Vault provider (same HTTP API).

* Project secrets: KV v2, ``<kv mount>/data/<prefix>/<project>/<NAME>`` with ``{"value": …}``;
  ``<prefix>`` = ``SOKKAN_OPENBAO_PREFIX`` (default ``sokkan/<SOKKAN_INSTANCE_ID or default>``).
* Data keys (forge / Teams / BYOK encryption, vault.json in file mode): Fernet keys **wrapped by
  transit** — ``<key file>.wrapped`` holds only ``vault:vN:…`` ciphertexts; they are unwrapped
  in memory (cached while the file does not change). The transit key never leaves OpenBao.
* Auth: ``kubernetes`` (ServiceAccount JWT), ``approle`` (role_id + secret_id) or ``token``
  (dev / break-glass). Tokens are renewed at 2/3 of their TTL, re-login when renewal fails or a
  request is denied with a stale token.
* TLS: ``SOKKAN_OPENBAO_CACERT`` (PEM bundle). Plain ``http://`` is refused unless the address
  is loopback or ``SOKKAN_OPENBAO_ALLOW_HTTP=1`` (a test container).
* ``SOKKAN_OPENBAO_NAMESPACE`` → ``X-Vault-Namespace`` (Vault Enterprise / OpenBao namespaces).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from urllib.parse import quote, urlsplit

from cryptography.fernet import Fernet

from secrets_provider import Health, SecretsError, SecretsProvider, key_path, wrapped_path

SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
WRAPPED_FORMAT = "sokkan-wrapped/1"


def _read_file(p: str) -> str:
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError as e:
        raise SecretsError(f"cannot read {p}: {e.strerror}") from None


def _env_or_file(env, name: str) -> str:
    v = (env.get(name) or "").strip()
    if v:
        return v
    f = (env.get(name + "_FILE") or "").strip()
    return _read_file(f) if f else ""


class BaoClient:
    """Minimal OpenBao/Vault HTTP client (httpx), thread-safe token handling."""

    def __init__(self, addr: str, *, namespace: str = "", cacert: str = "", auth: str = "token",
                 token: str = "", role_id: str = "", secret_id: str = "", k8s_role: str = "",
                 k8s_jwt_file: str = SA_TOKEN, approle_mount: str = "approle",
                 k8s_mount: str = "kubernetes", timeout: float = 10.0, allow_http: bool = False):
        u = urlsplit(addr)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise SecretsError(f"SOKKAN_OPENBAO_ADDR={addr!r}: expected https://host:8200")
        if u.scheme == "http" and not allow_http and (u.hostname or "") not in (
                "127.0.0.1", "localhost", "::1"):
            raise SecretsError("SOKKAN_OPENBAO_ADDR is plain http on a non-loopback address: "
                               "use https (SOKKAN_OPENBAO_CACERT) or SOKKAN_OPENBAO_ALLOW_HTTP=1 "
                               "for a test server")
        self.addr = addr.rstrip("/")
        self.namespace, self.cacert, self.auth = namespace, cacert, auth
        self._static, self.role_id, self.secret_id = token, role_id, secret_id
        self.k8s_role, self.k8s_jwt_file = k8s_role, k8s_jwt_file
        self.approle_mount, self.k8s_mount, self.timeout = approle_mount, k8s_mount, timeout
        self._tok = ""
        self._issued = 0.0
        self._ttl = 0.0
        self._renewable = False
        self._lock = threading.RLock()

    # ---- transport ----------------------------------------------------------------------
    def _client(self):
        import httpx
        verify: bool | str = self.cacert or True
        return httpx.Client(base_url=self.addr + "/v1", verify=verify, timeout=self.timeout)

    def _raw(self, method: str, path: str, body: dict | None = None, token: str = "",
             params: dict | None = None):
        headers = {}
        if token:
            headers["X-Vault-Token"] = token
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        try:
            with self._client() as h:
                return h.request(method, "/" + path.lstrip("/"), json=body, headers=headers,
                                 params=params)
        except Exception as e:  # noqa: BLE001 — httpx errors, TLS, DNS
            raise SecretsError(f"OpenBao unreachable at {self.addr}: {type(e).__name__}: {e}") from None

    @staticmethod
    def _err(r, what: str) -> SecretsError:
        try:
            errs = "; ".join(r.json().get("errors") or [])
        except ValueError:
            errs = ""
        return SecretsError(f"OpenBao {what}: HTTP {r.status_code}{' — ' + errs if errs else ''}")

    # ---- auth ---------------------------------------------------------------------------
    def _set_auth(self, a: dict) -> None:
        self._tok = a.get("client_token") or ""
        self._ttl = float(a.get("lease_duration") or 0)
        self._renewable = bool(a.get("renewable"))
        self._issued = time.time()

    def login(self) -> None:
        with self._lock:
            if self.auth == "approle":
                if not self.role_id or not self.secret_id:
                    raise SecretsError("AppRole auth: SOKKAN_OPENBAO_ROLE_ID and "
                                       "SOKKAN_OPENBAO_SECRET_ID(_FILE) are required")
                r = self._raw("POST", f"auth/{self.approle_mount}/login",
                              {"role_id": self.role_id, "secret_id": self.secret_id})
            elif self.auth == "kubernetes":
                if not self.k8s_role:
                    raise SecretsError("Kubernetes auth: SOKKAN_OPENBAO_K8S_ROLE is required")
                jwt = _read_file(self.k8s_jwt_file)
                r = self._raw("POST", f"auth/{self.k8s_mount}/login",
                              {"role": self.k8s_role, "jwt": jwt})
            elif self.auth == "token":
                if not self._static:
                    raise SecretsError("token auth: SOKKAN_OPENBAO_TOKEN(_FILE) is required")
                r = self._raw("GET", "auth/token/lookup-self", token=self._static)
                if r.status_code != 200:
                    raise self._err(r, "token lookup")
                d = r.json()["data"]
                self._tok = self._static
                self._ttl = float(d.get("ttl") or 0)
                self._renewable = bool(d.get("renewable"))
                self._issued = time.time()
                return
            else:
                raise SecretsError(f"SOKKAN_OPENBAO_AUTH={self.auth!r}: approle | kubernetes | token")
            if r.status_code != 200:
                raise self._err(r, f"{self.auth} login")
            self._set_auth(r.json()["auth"])

    def token(self) -> str:
        with self._lock:
            if not self._tok:
                self.login()
            elif self._ttl and time.time() > self._issued + self._ttl * 2 / 3:
                self.renew()
            return self._tok

    def renew(self) -> None:
        """Renew at 2/3 of the TTL; a token that cannot be renewed is replaced by a new login."""
        with self._lock:
            if self._renewable:
                r = self._raw("POST", "auth/token/renew-self", {}, token=self._tok)
                if r.status_code == 200:
                    a = r.json()["auth"]
                    self._ttl = float(a.get("lease_duration") or self._ttl)
                    self._renewable = bool(a.get("renewable"))
                    self._issued = time.time()
                    return
            if self.auth == "token":
                if self._renewable:
                    raise SecretsError("the static OpenBao token could not be renewed")
                self._ttl = 0  # a non-renewable static token: used until it expires
                return
            self._tok = ""
            self.login()

    def token_info(self) -> dict:
        return {"auth": self.auth, "ttl_s": int(self._ttl), "renewable": self._renewable,
                "age_s": int(time.time() - self._issued) if self._issued else 0}

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None,
             ok=(200, 204), missing_ok: bool = False):
        r = self._raw(method, path, body, token=self.token(), params=params)
        if r.status_code == 403 and self.auth != "token":
            with self._lock:  # stale / revoked token: one fresh login, one retry
                self._tok = ""
            r = self._raw(method, path, body, token=self.token(), params=params)
        if missing_ok and r.status_code == 404:
            return None
        if r.status_code not in ok:
            raise self._err(r, f"{method} {path.split('/')[0]}/…")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    def health(self) -> dict:
        r = self._raw("GET", "sys/health", params={"standbyok": "true", "sealedcode": "200",
                                                   "uninitcode": "200"})
        try:
            return r.json()
        except ValueError:
            raise self._err(r, "sys/health") from None


class OpenBaoProvider(SecretsProvider):
    name = "openbao"

    def __init__(self, client: BaoClient, *, kv_mount: str = "secret", prefix: str = "sokkan/default",
                 transit_mount: str = "transit", transit_key: str = "sokkan-default"):
        self.c = client
        self.kv, self.prefix = kv_mount.strip("/"), prefix.strip("/")
        self.transit, self.tkey = transit_mount.strip("/"), transit_key
        self._cache: dict[str, tuple[str, list[bytes]]] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls, env=None) -> OpenBaoProvider:
        env = os.environ if env is None else env
        addr = (env.get("SOKKAN_OPENBAO_ADDR") or "").strip()
        if not addr:
            raise SecretsError("SOKKAN_OPENBAO_ADDR is not set")
        role_id = _env_or_file(env, "SOKKAN_OPENBAO_ROLE_ID")
        k8s_role = (env.get("SOKKAN_OPENBAO_K8S_ROLE") or "").strip()
        token = _env_or_file(env, "SOKKAN_OPENBAO_TOKEN")
        auth = (env.get("SOKKAN_OPENBAO_AUTH") or "").strip().lower() or (
            "approle" if role_id else "kubernetes" if k8s_role else "token")
        inst = (env.get("SOKKAN_INSTANCE_ID") or "default").strip() or "default"
        client = BaoClient(
            addr, namespace=(env.get("SOKKAN_OPENBAO_NAMESPACE") or "").strip(),
            cacert=(env.get("SOKKAN_OPENBAO_CACERT") or "").strip(), auth=auth, token=token,
            role_id=role_id,
            secret_id=_env_or_file(env, "SOKKAN_OPENBAO_SECRET_ID") if auth == "approle" else "",
            k8s_role=k8s_role,
            k8s_jwt_file=(env.get("SOKKAN_OPENBAO_K8S_JWT_FILE") or SA_TOKEN).strip(),
            approle_mount=(env.get("SOKKAN_OPENBAO_APPROLE_MOUNT") or "approle").strip("/ "),
            k8s_mount=(env.get("SOKKAN_OPENBAO_K8S_MOUNT") or "kubernetes").strip("/ "),
            timeout=float(env.get("SOKKAN_OPENBAO_TIMEOUT_S") or 10),
            allow_http=(env.get("SOKKAN_OPENBAO_ALLOW_HTTP") or "").strip() in ("1", "true", "yes"))
        return cls(client,
                   kv_mount=(env.get("SOKKAN_OPENBAO_KV_MOUNT") or "secret"),
                   prefix=(env.get("SOKKAN_OPENBAO_PREFIX") or f"sokkan/{inst}"),
                   transit_mount=(env.get("SOKKAN_OPENBAO_TRANSIT_MOUNT") or "transit"),
                   transit_key=(env.get("SOKKAN_OPENBAO_TRANSIT_KEY") or f"sokkan-{inst}"))

    # ---- KV v2 --------------------------------------------------------------------------------
    def _p(self, *parts: str) -> str:
        return "/".join([self.prefix, *(quote(p, safe="") for p in parts)])

    def get(self, project: str, name: str) -> str | None:
        j = self.c.call("GET", f"{self.kv}/data/{self._p(project, name)}", missing_ok=True)
        if not j:
            return None
        data = (j.get("data") or {}).get("data") or {}
        v = data.get("value")
        return v if isinstance(v, str) else None

    def set(self, project: str, name: str, value: str) -> None:
        self.c.call("POST", f"{self.kv}/data/{self._p(project, name)}", {"data": {"value": value}})

    def delete(self, project: str, name: str) -> None:
        self.c.call("DELETE", f"{self.kv}/metadata/{self._p(project, name)}", missing_ok=True)

    def _list(self, path: str) -> list[str]:
        j = self.c.call("GET", f"{self.kv}/metadata/{path}/", params={"list": "true"},
                        missing_ok=True)
        return list(((j or {}).get("data") or {}).get("keys") or [])

    def list(self, project: str) -> list[str]:
        return sorted(k for k in self._list(self._p(project)) if not k.endswith("/"))

    def projects(self) -> list[str]:
        return sorted(k.rstrip("/") for k in self._list(self.prefix) if k.endswith("/"))

    # ---- transit ------------------------------------------------------------------------------
    def wrap(self, plaintext: bytes) -> str:
        j = self.c.call("POST", f"{self.transit}/encrypt/{self.tkey}",
                        {"plaintext": base64.b64encode(plaintext).decode()})
        return j["data"]["ciphertext"]

    def unwrap(self, ciphertext: str) -> bytes:
        j = self.c.call("POST", f"{self.transit}/decrypt/{self.tkey}", {"ciphertext": ciphertext})
        return base64.b64decode(j["data"]["plaintext"])

    def ensure_transit_key(self) -> dict:
        j = self.c.call("GET", f"{self.transit}/keys/{self.tkey}", missing_ok=True)
        if j is None:
            self.c.call("POST", f"{self.transit}/keys/{self.tkey}", {"type": "aes256-gcm96"})
            j = self.c.call("GET", f"{self.transit}/keys/{self.tkey}")
        d = j.get("data") or {}
        return {"latest_version": d.get("latest_version"),
                "min_decryption_version": d.get("min_decryption_version"),
                "exportable": d.get("exportable"), "type": d.get("type")}

    # ---- wrapped data keys ---------------------------------------------------------------------
    def _read_wrapped(self, ctx: str) -> tuple[str, dict] | None:
        try:
            with open(wrapped_path(ctx)) as f:
                raw = f.read()
        except OSError:
            return None
        try:
            d = json.loads(raw)
        except ValueError:
            raise SecretsError(f"{wrapped_path(ctx)} is not valid JSON") from None
        if d.get("format") != WRAPPED_FORMAT or not d.get("keys"):
            raise SecretsError(f"{wrapped_path(ctx)}: unknown format")
        return raw, d

    def _write_wrapped(self, ctx: str, cts: list[str], exclusive: bool = False) -> None:
        p = wrapped_path(ctx)
        d = {"format": WRAPPED_FORMAT, "provider": "openbao", "transit_mount": self.transit,
             "transit_key": self.tkey, "context": ctx,
             "keys": [{"ciphertext": c, "created": time.time()} for c in cts]}
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        body = json.dumps(d, indent=2).encode()
        if exclusive:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
        else:
            tmp = p + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, p)
        with self._lock:
            self._cache.pop(ctx, None)

    def wrapped_ciphertexts(self, ctx: str) -> list[str]:
        w = self._read_wrapped(ctx)
        return [k["ciphertext"] for k in w[1]["keys"]] if w else []

    def data_keys(self, ctx: str, create: bool = True) -> list[bytes]:
        w = self._read_wrapped(ctx)
        if w is None:
            if not create:
                return []
            if os.path.exists(key_path(ctx)) or os.path.exists(key_path(ctx) + ".next"):
                raise SecretsError(
                    f"{os.path.basename(key_path(ctx))} is still a clear key file: run "
                    "scripts/secrets-migrate.py --from file --to openbao before selecting openbao")
            k = Fernet.generate_key()
            try:
                self._write_wrapped(ctx, [self.wrap(k)], exclusive=True)
            except FileExistsError:
                return self.data_keys(ctx, create=False)
            return [k]
        raw, d = w
        sig = hashlib.sha256(raw.encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(ctx)
            if hit and hit[0] == sig:
                return list(hit[1])
        keys = [self.unwrap(k["ciphertext"]) for k in d["keys"]]
        with self._lock:
            self._cache[ctx] = (sig, keys)
        return list(keys)

    def add_data_key(self, ctx: str, key: bytes | None = None) -> bytes:
        cts = self.wrapped_ciphertexts(ctx)
        if not cts:
            self.data_keys(ctx)
            cts = self.wrapped_ciphertexts(ctx)
        if len(cts) > 1:
            return self.data_keys(ctx)[0]  # a rotation was interrupted: resume with its key
        k = key or Fernet.generate_key()
        self._write_wrapped(ctx, [self.wrap(k), *cts])
        return k

    def drop_old_data_keys(self, ctx: str) -> int:
        cts = self.wrapped_ciphertexts(ctx)
        if len(cts) <= 1:
            return 0
        self._write_wrapped(ctx, cts[:1])
        return len(cts) - 1

    def import_data_keys(self, ctx: str, keys: list[bytes]) -> None:
        if not keys:
            return
        if self._read_wrapped(ctx) is not None:
            if self.data_keys(ctx, create=False) == list(keys):
                return  # idempotent: already migrated
            raise SecretsError(f"{wrapped_path(ctx)} already holds different data keys")
        self._write_wrapped(ctx, [self.wrap(k) for k in keys])

    def rotate_master(self) -> dict:
        """Rotate the transit key, then rewrap every wrapped data key with its latest version
        (the data keys, hence every stored value, do not change)."""
        self.ensure_transit_key()
        self.c.call("POST", f"{self.transit}/keys/{self.tkey}/rotate", {})
        info = self.ensure_transit_key()
        n = 0
        for ctx in ("vault", "forge", "teams"):
            cts = self.wrapped_ciphertexts(ctx)
            if not cts:
                continue
            new = [self.c.call("POST", f"{self.transit}/rewrap/{self.tkey}",
                               {"ciphertext": ct})["data"]["ciphertext"] for ct in cts]
            before = self.data_keys(ctx, create=False)
            self._write_wrapped(ctx, new)
            if self.data_keys(ctx, create=False) != before:  # never leave unreadable keys
                raise SecretsError(f"{ctx}: rewrapped keys do not match — restore the backup")
            n += len(new)
        return {"transit_key": self.tkey, "latest_version": info["latest_version"],
                "rewrapped": n}

    # ---- operations ----------------------------------------------------------------------------
    def health(self) -> Health:
        checks: dict = {}
        try:
            h = self.c.health()
            checks["server"] = {"version": h.get("version"), "initialized": h.get("initialized"),
                                "sealed": h.get("sealed")}
            if not h.get("initialized"):
                return Health(False, self.name, "OpenBao is not initialized (bao operator init)",
                              checks)
            if h.get("sealed"):
                return Health(False, self.name, "OpenBao is sealed (bao operator unseal)", checks)
            self.c.token()
            checks["auth"] = self.c.token_info()
            nonce = os.urandom(16)
            if self.unwrap(self.wrap(nonce)) != nonce:
                return Health(False, self.name, "transit round trip returned another value",
                              checks)
            checks["transit"] = f"{self.transit}/keys/{self.tkey}: encrypt/decrypt OK"
            checks["kv"] = f"{self.kv}/{self.prefix}: {len(self.projects())} project(s) listed"
            for ctx in ("vault", "forge", "teams"):
                if os.path.exists(wrapped_path(ctx)):
                    self.data_keys(ctx, create=False)
                    checks[f"data_key_{ctx}"] = "unwrapped"
        except SecretsError as e:
            return Health(False, self.name, str(e), checks)
        return Health(True, self.name, "reachable, unsealed, authenticated; transit and KV OK",
                      checks)

    def describe(self) -> dict:
        tls = ("plain HTTP — no TLS" if self.c.addr.startswith("http://")
               else "CA file " + os.path.basename(self.c.cacert) if self.c.cacert
               else "system CA store")
        d = {"provider": self.name, "address": self.c.addr, "namespace": self.c.namespace,
             "auth": self.c.auth + (f" (role {self.c.k8s_role})" if self.c.auth == "kubernetes"
                                    else ""),
             "tls": tls, "kv": f"{self.kv}/{self.prefix}",
             "transit": f"{self.transit}/keys/{self.tkey}"}
        return {k: v for k, v in d.items() if v}
