"""File provider — the 3.2 behaviour, byte for byte: Fernet key files (0600) next to the data,
vault values in ``vault.json`` (format 2). Rotation adds ``<key>.next`` (the primary while it
exists) and renames it over the key once every value is re-encrypted."""
from __future__ import annotations

import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from secrets_provider import Health, SecretsError, SecretsProvider, key_path, wrapped_path

_klock = threading.Lock()


def _read_key(p: str) -> bytes | None:
    try:
        with open(p, "rb") as f:
            k = f.read().strip()
        return k or None
    except OSError:
        return None


def write_key(p: str, key: bytes, exclusive: bool = True) -> None:
    """0600 from the first byte (O_EXCL unless replacing on purpose)."""
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    if exclusive:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.replace(tmp, p)


class FileProvider(SecretsProvider):
    name = "file"

    # ---- data keys -----------------------------------------------------------------------
    def data_keys(self, ctx: str, create: bool = True) -> list[bytes]:
        p = key_path(ctx)
        cur, nxt = _read_key(p), _read_key(p + ".next")
        if cur is None and nxt is None:
            if not create:
                return []
            if os.path.exists(wrapped_path(ctx)):
                raise SecretsError(
                    f"{os.path.basename(p)} is wrapped by OpenBao ({os.path.basename(wrapped_path(ctx))})"
                    " and this instance runs the file provider: select openbao "
                    "(SOKKAN_SECRETS_PROVIDER) or run scripts/secrets-migrate.py --from openbao "
                    "--to file — a new key would make every stored value unreadable")
            with _klock:
                cur = _read_key(p)
                if cur is None:
                    cur = Fernet.generate_key()
                    try:
                        write_key(p, cur)
                    except FileExistsError:
                        cur = _read_key(p)
        return [k for k in (nxt, cur) if k]

    def add_data_key(self, ctx: str, key: bytes | None = None) -> bytes:
        p = key_path(ctx) + ".next"
        have = _read_key(p)
        if have:
            return have  # a rotation was interrupted: resume with the same key
        self.data_keys(ctx)  # never rotate a context that has no key yet
        k = key or Fernet.generate_key()
        write_key(p, k)
        return k

    def drop_old_data_keys(self, ctx: str) -> int:
        p = key_path(ctx)
        if not os.path.exists(p + ".next"):
            return 0
        os.replace(p + ".next", p)
        return 1

    def import_data_keys(self, ctx: str, keys: list[bytes]) -> None:
        if not keys:
            return
        if len(keys) > 2:
            raise SecretsError(f"{ctx}: {len(keys)} data keys — finish the rotation first")
        p = key_path(ctx)
        write_key(p, keys[-1], exclusive=False)
        if len(keys) == 2:
            write_key(p + ".next", keys[0], exclusive=False)
        elif os.path.exists(p + ".next"):
            os.unlink(p + ".next")

    # ---- project secrets (vault.json, format 2) ---------------------------------------------
    def _vault(self):
        import vault
        return vault

    def get(self, project: str, name: str) -> str | None:
        tok = (self._vault()._load()["projects"].get(project) or {}).get(name)
        if tok is None:
            return None
        try:
            return self.decrypt("vault", tok)
        except InvalidToken:  # corrupted / key changed: skipped, as in 3.2
            return None

    def set(self, project: str, name: str, value: str) -> None:
        v = self._vault()
        tok = self.encrypt("vault", value)
        with v._lock:
            d = v._load()
            d["projects"].setdefault(project, {})[name] = tok
            v._save(d)

    def delete(self, project: str, name: str) -> None:
        v = self._vault()
        with v._lock:
            d = v._load()
            (d["projects"].get(project) or {}).pop(name, None)
            v._save(d)

    def list(self, project: str) -> list[str]:
        return sorted((self._vault()._load()["projects"].get(project) or {}).keys())

    def projects(self) -> list[str]:
        return sorted(p for p, s in self._vault()._load()["projects"].items() if s)

    def get_all(self, project: str, only: list[str] | None = None) -> dict[str, str]:
        f = self.fernet("vault")
        out = {}
        for k, tok in (self._vault()._load()["projects"].get(project) or {}).items():
            if only is not None and k not in only:
                continue
            try:
                out[k] = f.decrypt(tok.encode()).decode()
            except InvalidToken:
                continue
        return out

    def reencrypt_vault(self) -> int:
        """Rotation: every vault.json value re-encrypted with the primary key."""
        v = self._vault()
        f = self.fernet("vault")
        n = 0
        with v._lock:
            d = v._load()
            for _p, sec in d["projects"].items():
                for k, tok in list(sec.items()):
                    try:
                        sec[k] = f.rotate(tok.encode()).decode()
                        n += 1
                    except InvalidToken:
                        continue
            v._save(d)
        return n

    # ---- operations -------------------------------------------------------------------------
    def health(self) -> Health:
        checks = {}
        for ctx in ("vault", "forge", "teams"):
            p = key_path(ctx)
            checks[ctx] = ("present" if os.path.exists(p) else "not created yet")
            if os.path.exists(p):
                mode = os.stat(p).st_mode & 0o777
                if mode & 0o077:
                    return Health(False, self.name, f"{os.path.basename(p)} is readable by others "
                                  f"(mode {oct(mode)}): chmod 600", checks)
        try:
            self.data_keys("vault")
        except SecretsError as e:
            return Health(False, self.name, str(e), checks)
        return Health(True, self.name, "key files present, mode 0600", checks)

    def describe(self) -> dict:
        return {"provider": self.name, "key_files": [os.path.basename(key_path(c))
                                                     for c in ("vault", "forge", "teams")],
                "store": "vault.json"}
