"""secrets_provider — where SOKKAN keeps its secrets, behind one interface (3.3).

Two things go through a provider:

1. **Project secrets** (the vault): ``get / set / delete / list`` by ``(project, NAME)``. The
   vault's policy (which project has a namespace, `named` vs `all`) stays in ``vault.py``; the
   provider only stores.
2. **Opaque values** encrypted by SOKKAN itself (forge OAuth tokens, Teams app tokens, BYOK model
   keys): ``encrypt(ctx, text)`` / ``decrypt(ctx, token)``. A *context* (``vault``, ``forge``,
   ``teams``) is one **data key** (a Fernet key). The data key is what changes with the provider:

   * ``file``        — the key files of 3.2 (``vault.key``, ``forge.key``, ``teams.key``, 0600),
                       vault values in ``vault.json``. Nothing changes on disk (default).
   * ``openbao``     — OpenBao / HashiCorp Vault: project secrets in **KV v2** under
                       ``<kv>/sokkan/<instance>/<project>/<NAME>``; each data key is **wrapped by
                       transit** (``<key file>.wrapped`` holds only the transit ciphertext) and
                       unwrapped in memory — the master key never leaves OpenBao, no clear key
                       on disk. Auth AppRole or Kubernetes (ServiceAccount JWT), token renewal,
                       TLS, optional namespace.
   * ``kubernetes``  — Kubernetes Secrets of the namespace (installs without OpenBao): one Secret
                       per project, data keys in one Secret. Encryption at rest = the cluster's.

Selection: ``SOKKAN_SECRETS_PROVIDER=file|openbao|kubernetes`` (feature ``secrets_provider``);
unset with the feature on → ``openbao`` when ``SOKKAN_OPENBAO_ADDR`` is configured, else
``file`` (and an enterprise instance shows a warning in Setup › Secrets).

Rotation: a context may hold several data keys, newest first (``vault.key.next`` in file mode,
the ``keys`` list of a ``.wrapped`` file): encryption uses the newest, decryption tries all —
``scripts/secrets-rotate.py`` adds a key, re-encrypts every value, then drops the old one.

NB: the package is not called ``secrets`` — ``backend/`` is on ``sys.path`` and that name would
shadow the standard library module every other file imports.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

PROVIDERS = ("file", "openbao", "kubernetes")
CONTEXTS = ("vault", "forge", "teams")


class SecretsError(RuntimeError):
    """The provider cannot serve (unreachable, sealed, denied, misconfigured, keys not migrated)."""


def data_dir() -> str:
    return os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))


def key_path(ctx: str) -> str:
    """Clear key file of a context in file mode (3.2 paths, env overrides kept)."""
    if ctx == "vault":
        import vault
        return vault.KEY_PATH
    if ctx == "forge":
        return os.environ.get("SOKKAN_FORGE_KEY_FILE") or os.path.join(data_dir(), "forge.key")
    if ctx == "teams":
        return os.environ.get("SOKKAN_TEAMS_KEY_FILE") or os.path.join(data_dir(), "teams.key")
    raise ValueError(f"unknown secrets context {ctx!r}")


def wrapped_path(ctx: str) -> str:
    return key_path(ctx) + ".wrapped"


@dataclass
class Health:
    ok: bool
    provider: str
    detail: str = ""
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "provider": self.provider, "detail": self.detail,
                "checks": self.checks, "checked_at": time.time()}


class SecretsProvider:
    """Interface. Subclasses implement storage (`_get/_set/_delete/_list/projects`) and the
    data keys (`data_keys`, `add_data_key`, `drop_old_data_keys`)."""

    name = "abstract"

    # ---- project secrets ---------------------------------------------------------------
    def get(self, project: str, name: str) -> str | None:
        raise NotImplementedError

    def set(self, project: str, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, project: str, name: str) -> None:
        raise NotImplementedError

    def list(self, project: str) -> list[str]:
        raise NotImplementedError

    def projects(self) -> list[str]:
        raise NotImplementedError

    def get_all(self, project: str, only: list[str] | None = None) -> dict[str, str]:
        out = {}
        for n in self.list(project):
            if only is not None and n not in only:
                continue
            v = self.get(project, n)
            if v is not None:
                out[n] = v
        return out

    # ---- data keys (envelope) -----------------------------------------------------------
    def data_keys(self, ctx: str, create: bool = True) -> list[bytes]:
        """Fernet keys of a context, newest first. ``create`` makes the first one if none."""
        raise NotImplementedError

    def add_data_key(self, ctx: str, key: bytes | None = None) -> bytes:
        """Rotation step 1: a new data key becomes the primary (the old ones still decrypt)."""
        raise NotImplementedError

    def drop_old_data_keys(self, ctx: str) -> int:
        """Rotation step 3 (after every value is re-encrypted): keep only the primary."""
        raise NotImplementedError

    def import_data_keys(self, ctx: str, keys: list[bytes]) -> None:
        """Migration: take over existing data keys (newest first) as they are."""
        raise NotImplementedError

    def fernet(self, ctx: str) -> MultiFernet:
        keys = self.data_keys(ctx)
        return MultiFernet([Fernet(k) for k in keys])

    def encrypt(self, ctx: str, plaintext: str) -> str:
        return self.fernet(ctx).encrypt(plaintext.encode()).decode()

    def decrypt(self, ctx: str, token: str) -> str:
        """Raises ``InvalidToken`` when no data key of the context opens it."""
        return self.fernet(ctx).decrypt(token.encode()).decode()

    # ---- operations ------------------------------------------------------------------------
    def health(self) -> Health:
        raise NotImplementedError

    def describe(self) -> dict:
        """Non-secret configuration, for Setup › Secrets and the backup MANIFEST."""
        return {"provider": self.name}

    def rotate_master(self) -> dict:
        raise SecretsError(f"the {self.name} provider has no master key to rotate "
                           "(rotate the data keys: secrets-rotate.py --data-keys)")


# ---- selection --------------------------------------------------------------------------
_lock = threading.RLock()
_active: SecretsProvider | None = None
_active_sig: tuple | None = None


def _openbao_configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool((env.get("SOKKAN_OPENBAO_ADDR") or "").strip())


def selected(env=None, feature_on: bool | None = None) -> tuple[str, str]:
    """(provider, why). Never raises. ``feature_on`` given = do not ask the registry (its
    readiness check calls this)."""
    env = os.environ if env is None else env
    raw = (env.get("SOKKAN_SECRETS_PROVIDER") or "").strip().lower()
    on = feature_on
    if on is None:
        try:
            import features
            on = features.enabled("secrets_provider", env=env)
        except Exception:  # noqa: BLE001 — registry unavailable: the 3.2 behaviour
            on = False
    if not on:
        if raw and raw != "file":
            return "file", (f"SOKKAN_SECRETS_PROVIDER={raw} ignored: feature secrets_provider "
                            "is off")
        return "file", "feature secrets_provider off (3.2 behaviour)"
    if raw:
        if raw not in PROVIDERS:
            return "file", f"SOKKAN_SECRETS_PROVIDER={raw} unknown — file used"
        return raw, f"SOKKAN_SECRETS_PROVIDER={raw}"
    if _openbao_configured(env):
        return "openbao", "SOKKAN_OPENBAO_ADDR is set"
    return "file", "no OpenBao configured (SOKKAN_OPENBAO_ADDR)"


def make(name: str, env=None) -> SecretsProvider:
    """A provider by name (the migration and rotation scripts build both ends)."""
    if name == "file":
        from secrets_provider.filep import FileProvider
        return FileProvider()
    if name == "openbao":
        from secrets_provider.openbao import OpenBaoProvider
        return OpenBaoProvider.from_env(env)
    if name == "kubernetes":
        from secrets_provider.kube import KubernetesProvider
        return KubernetesProvider.from_env(env)
    raise SecretsError(f"unknown secrets provider {name!r} (file | openbao | kubernetes)")


def _signature() -> tuple:
    keys = sorted(k for k in os.environ if k.startswith(("SOKKAN_OPENBAO_", "SOKKAN_K8S_")))
    return (selected()[0], data_dir(), tuple((k, os.environ[k]) for k in keys))


def active() -> SecretsProvider:
    """The provider of this process (rebuilt when its configuration changes — tests)."""
    global _active, _active_sig
    sig = _signature()
    with _lock:
        if _active is None or sig != _active_sig:
            _active, _active_sig = make(sig[0]), sig
        return _active


def reset() -> None:
    global _active, _active_sig
    with _lock:
        _active, _active_sig = None, None


def encrypt(ctx: str, plaintext: str) -> str:
    return active().encrypt(ctx, plaintext)


def decrypt(ctx: str, token: str) -> str:
    return active().decrypt(ctx, token)


def data_key(ctx: str) -> bytes:
    """Primary data key of a context (MAC derivations: forge tickets, Teams approvals)."""
    return active().data_keys(ctx)[0]


def all_data_keys(ctx: str) -> list[bytes]:
    return active().data_keys(ctx)


def warning(env=None, feature_on: bool | None = None) -> str | None:
    """Shown in Setup › Secrets: an enterprise instance whose secrets stay in files."""
    env = os.environ if env is None else env
    name, why = selected(env, feature_on)
    try:
        import features
        ed = features.edition(env)
    except Exception:  # noqa: BLE001
        ed = "community"
    if name == "file" and ed == "enterprise":
        return ("Secrets are stored in files on this server (vault.key, forge.key, teams.key next "
                "to the data): anyone with a copy of the data directory and its keys reads every "
                "secret. Configure OpenBao (docs/enterprise/SECRETS.md) — " + why + ".")
    return None


def status() -> dict:
    """Setup › Secrets: what is selected, why, its non-secret configuration, warnings."""
    name, why = selected()
    out = {"provider": name, "reason": why, "warning": warning(), "available": list(PROVIDERS),
           "config": {}, "error": ""}
    try:
        out["config"] = active().describe()
    except SecretsError as e:
        out["error"] = str(e)
    clear = [os.path.basename(key_path(c)) for c in CONTEXTS
             if name != "file" and os.path.exists(key_path(c))]
    if clear:
        out["clear_keys_on_disk"] = clear
    return out


__all__ = ["CONTEXTS", "PROVIDERS", "Health", "InvalidToken", "SecretsError", "SecretsProvider",
           "active", "all_data_keys", "data_key", "decrypt", "encrypt", "key_path", "make",
           "reset", "selected", "status", "warning", "wrapped_path"]
