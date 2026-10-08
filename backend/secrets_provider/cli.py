"""Operations on the secrets provider — migration, export/import, rotation, backup helpers.

Run with the instance's environment (same SOKKAN_DATA_DIR, same SOKKAN_OPENBAO_* as the api):

  compose:     docker compose exec api python3 -m secrets_provider.cli <command> …
               (PYTHONPATH=/app/backend is set by scripts/secrets-*.py wrappers)
  host / dev:  scripts/secrets-migrate.py …   scripts/secrets-rotate.py …

Commands (``--help`` on each):
  status                         selected provider, configuration, health
  migrate --from A --to B        copy data keys + project secrets, verify by reading back
  export --out FILE              passphrase-encrypted bundle (data keys + secrets)
  import --in FILE --to B        bundle → provider B
  rotate [--master] [--data-keys [CTX …]]
  backup-export --out FILE       (backup.sh, openbao) project secrets, each value
                                 transit-encrypted — no key in the file
  backup-import --in FILE        (restore.sh) put those secrets back in KV
  unwrap-check --dir DIR         (restore.sh) the wrapped data keys of DIR unwrap here
Every write is journaled: $SOKKAN_DATA_DIR/secrets-ops.log (JSON lines, 0600) + audit.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

if __package__ in (None, ""):  # run as a file: make `backend/` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_provider as sp  # noqa: E402
from secrets_provider import CONTEXTS, SecretsError, key_path, wrapped_path  # noqa: E402

EXPORT_FORMAT = "sokkan-secrets-export/1"
BACKUP_FORMAT = "sokkan-secrets-transit/1"
MARKER = "secrets-provider.json"


def _out(d: dict) -> None:
    print(json.dumps(d, indent=2, sort_keys=True))


def journal(event: str, **fields) -> None:
    """One JSON line per operation (never a value, never a key) + the audit journal."""
    rec = {"ts": time.time(), "event": event, **fields}
    p = os.path.join(sp.data_dir(), "secrets-ops.log")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as e:
        print(f"secrets: journal not written: {e}", file=sys.stderr)
    try:
        import audit
        audit.log(os.environ.get("SOKKAN_OPERATOR") or "secrets-cli", event,
                  fields.get("to") or fields.get("provider") or "",
                  json.dumps({k: v for k, v in fields.items() if k != "secrets"})[:1500],
                  project="")
    except Exception:  # noqa: BLE001 — the audit db may be elsewhere (backup host)
        pass


def _passphrase() -> bytes:
    v = os.environ.get("SOKKAN_SECRETS_EXPORT_PASSPHRASE") or ""
    f = os.environ.get("SOKKAN_SECRETS_EXPORT_PASSFILE") or ""
    if f:
        with open(f, "rb") as fh:
            v = fh.read().decode().strip()
    if len(v) < 12:
        raise SecretsError("set SOKKAN_SECRETS_EXPORT_PASSPHRASE (or _PASSFILE), 12 characters "
                           "or more")
    return v.encode()


def _kdf(pw: bytes, salt: bytes, n: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return base64.urlsafe_b64encode(Scrypt(salt=salt, length=32, n=n, r=8, p=1).derive(pw))


def _write_0600(path: str, body: str) -> None:
    if path == "-":
        sys.stdout.write(body)
        sys.stdout.flush()
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)


# ---- copy between providers ------------------------------------------------------------------
def copy(src: sp.SecretsProvider, dst: sp.SecretsProvider, overwrite: bool = False,
         dry_run: bool = False) -> dict:
    """Data keys first (every stored ciphertext stays readable), then project secrets.
    Idempotent: what is already there with the same value is skipped; a different value is
    reported and left alone unless ``overwrite``. Every write is verified by reading back."""
    rep: dict = {"from": src.name, "to": dst.name, "data_keys": {}, "copied": 0, "same": 0,
                 "differs": [], "overwritten": 0, "dry_run": dry_run}
    for ctx in CONTEXTS:
        keys = src.data_keys(ctx, create=False)
        if not keys:
            rep["data_keys"][ctx] = "none"
            continue
        have = dst.data_keys(ctx, create=False)
        if have == keys:
            rep["data_keys"][ctx] = "already there"
            continue
        if have:
            raise SecretsError(f"{dst.name} already holds a different {ctx} data key — refusing "
                               "to replace it (stored values would become unreadable)")
        if not dry_run:
            dst.import_data_keys(ctx, keys)
            back = dst.data_keys(ctx, create=False)
            if back != keys:
                raise SecretsError(f"{ctx}: data keys read back from {dst.name} differ")
        rep["data_keys"][ctx] = f"{len(keys)} key(s) imported"
    for p in src.projects():
        for n in src.list(p):
            v = src.get(p, n)
            if v is None:
                rep.setdefault("unreadable", []).append(f"{p}/{n}")
                continue
            cur = dst.get(p, n)
            if cur == v:
                rep["same"] += 1
                continue
            if cur is not None and not overwrite:
                rep["differs"].append(f"{p}/{n}")
                continue
            if not dry_run:
                dst.set(p, n, v)
                if dst.get(p, n) != v:
                    raise SecretsError(f"{p}/{n}: value read back from {dst.name} differs")
            rep["overwritten" if cur is not None else "copied"] += 1
    return rep


def _shred(path: str) -> bool:
    """Overwrite then unlink (best effort: copy-on-write / SSD may keep old blocks — the
    procedure says so)."""
    try:
        n = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(os.urandom(max(n, 64)))
            f.flush()
            os.fsync(f.fileno())
        os.unlink(path)
        return True
    except OSError:
        return False


def purge_clear(rep: dict) -> None:
    """After a verified migration to openbao/kubernetes: no clear key left on disk; vault.json
    kept as vault.json.pre-<provider> (ciphertext; its key is now wrapped) for a rollback."""
    gone = []
    for ctx in CONTEXTS:
        for p in (key_path(ctx), key_path(ctx) + ".next"):
            if os.path.exists(p) and _shred(p):
                gone.append(os.path.basename(p))
    import vault
    if os.path.exists(vault.STORE):
        os.replace(vault.STORE, vault.STORE + f".pre-{rep['to']}")
        gone.append("vault.json → vault.json.pre-" + rep["to"])
    rep["purged"] = gone


def cmd_migrate(a) -> int:
    src, dst = sp.make(a.src), sp.make(a.dst)
    if a.src == a.dst:
        raise SecretsError("--from and --to are the same provider")
    if dst.name != "file":
        h = dst.health()
        if not h.ok:
            raise SecretsError(f"{dst.name} is not ready: {h.detail}")
    rep = copy(src, dst, overwrite=a.overwrite, dry_run=a.dry_run or a.check)
    if a.check:
        rep["check"] = "no write (--check)"
    elif not a.dry_run:
        _write_0600(os.path.join(sp.data_dir(), MARKER), json.dumps(
            {"provider": dst.name, "migrated_from": src.name, "at": time.time(),
             "config": dst.describe()}, indent=2))
        if a.purge:
            if dst.name == "file":
                raise SecretsError("--purge removes CLEAR keys: only for --to openbao|kubernetes")
            if rep["differs"]:
                raise SecretsError("values differ between providers: not purging "
                                   f"({', '.join(rep['differs'])}) — re-run with --overwrite")
            again = copy(src, dst, dry_run=True)
            if again["copied"] or again["differs"]:
                raise SecretsError("verification before purge found missing values")
            purge_clear(rep)
        journal("secrets.migrate", **{k: v for k, v in rep.items() if k != "secrets"})
    _out(rep)
    if not a.dry_run and not a.check and dst.name != "file":
        print(f"\nnext: set SOKKAN_SECRETS_PROVIDER={dst.name} and restart the api"
              + ("" if a.purge else "; once checked, re-run with --purge (no clear key on disk)"),
              file=sys.stderr)
    return 1 if rep["differs"] and not a.overwrite else 0


def _bundle(src: sp.SecretsProvider) -> dict:
    return {"format": EXPORT_FORMAT, "provider": src.name, "created": time.time(),
            "data_keys": {c: [k.decode() for k in src.data_keys(c, create=False)]
                          for c in CONTEXTS},
            "secrets": {p: src.get_all(p) for p in src.projects()}}


def cmd_export(a) -> int:
    from cryptography.fernet import Fernet
    src = sp.make(a.src) if a.src else sp.active()
    b = _bundle(src)
    salt, n = os.urandom(16), 2 ** 15
    token = Fernet(_kdf(_passphrase(), salt, n)).encrypt(json.dumps(b).encode()).decode()
    _write_0600(a.out, json.dumps({"format": EXPORT_FORMAT, "kdf": "scrypt", "n": n, "r": 8,
                                   "p": 1, "salt": base64.b64encode(salt).decode(),
                                   "ciphertext": token}))
    summary = {"provider": src.name, "projects": len(b["secrets"]),
               "secrets": sum(len(v) for v in b["secrets"].values()), "out": a.out}
    journal("secrets.export", **summary)
    _out(summary)
    return 0


def _read_bundle(path: str) -> dict:
    from cryptography.fernet import Fernet, InvalidToken
    with open(path) as f:
        env = json.load(f)
    if env.get("format") != EXPORT_FORMAT:
        raise SecretsError(f"{path}: not a SOKKAN secrets export")
    try:
        raw = Fernet(_kdf(_passphrase(), base64.b64decode(env["salt"]), int(env["n"]))).decrypt(
            env["ciphertext"].encode())
    except InvalidToken:
        raise SecretsError("wrong passphrase (or the file was altered)") from None
    return json.loads(raw)


class _BundleProvider(sp.SecretsProvider):
    name = "export"

    def __init__(self, b: dict):
        self.b = b

    def data_keys(self, ctx, create=True):
        return [k.encode() for k in self.b["data_keys"].get(ctx) or []]

    def projects(self):
        return sorted(self.b["secrets"])

    def list(self, project):
        return sorted(self.b["secrets"].get(project) or {})

    def get(self, project, name):
        return (self.b["secrets"].get(project) or {}).get(name)


def cmd_import(a) -> int:
    src = _BundleProvider(_read_bundle(a.inp))
    dst = sp.make(a.dst)
    rep = copy(src, dst, overwrite=a.overwrite, dry_run=a.dry_run)
    if not a.dry_run:
        journal("secrets.import", **rep)
    _out(rep)
    return 1 if rep["differs"] and not a.overwrite else 0


# ---- rotation ----------------------------------------------------------------------------------
def reencrypt(ctx: str, prov: sp.SecretsProvider) -> dict:
    """Every value encrypted with a data key of ``ctx`` → re-encrypted with the primary."""
    done: dict = {}
    if ctx == "vault":
        if prov.name == "file":
            done["vault.json"] = prov.reencrypt_vault()
        import modelkeys
        done["modelkeys.json"] = modelkeys.reencrypt()
    elif ctx == "forge":
        from forge import links
        done["forge_links"] = links.reencrypt()
    elif ctx == "teams":
        p = os.environ.get("SOKKAN_TEAMS_DB") or os.path.join(sp.data_dir(), "teams.db")
        if os.path.exists(p):
            from teams import store
            done["teams token_cache"] = store.reencrypt()
    return done


def cmd_rotate(a) -> int:
    prov = sp.active()
    rep: dict = {"provider": prov.name, "dry_run": a.dry_run}
    if not a.master and a.data_keys is None:
        raise SecretsError("nothing to do: --master and/or --data-keys [vault forge teams]")
    if a.master:
        if a.dry_run:
            rep["master"] = "would rotate the transit key and rewrap the data keys"
        else:
            rep["master"] = prov.rotate_master()
    if a.data_keys is not None:
        ctxs = a.data_keys or list(CONTEXTS)
        for c in ctxs:
            if c not in CONTEXTS:
                raise SecretsError(f"unknown context {c!r} ({', '.join(CONTEXTS)})")
        rep["data_keys"] = {}
        for c in ctxs:
            if not prov.data_keys(c, create=False):
                rep["data_keys"][c] = "no key yet: nothing to rotate"
                continue
            if a.dry_run:
                rep["data_keys"][c] = "would add a key, re-encrypt, drop the old one"
                continue
            prov.add_data_key(c)                 # 1. new primary (old ones still decrypt)
            done = reencrypt(c, prov)            # 2. every value re-encrypted
            dropped = prov.drop_old_data_keys(c)  # 3. old key gone
            rep["data_keys"][c] = {"reencrypted": done, "dropped": dropped}
    if not a.dry_run:
        journal("secrets.rotate", **rep)
    _out(rep)
    return 0


# ---- backup helpers (openbao) -------------------------------------------------------------------
def cmd_backup_export(a) -> int:
    prov = sp.active()
    if prov.name != "openbao":
        raise SecretsError("backup-export is for the openbao provider")
    out = {"format": BACKUP_FORMAT, "transit": prov.describe()["transit"],
           "kv": prov.describe()["kv"], "created": time.time(), "secrets": {}}
    n = 0
    for p in prov.projects():
        for name, v in prov.get_all(p).items():
            out["secrets"].setdefault(p, {})[name] = prov.wrap(v.encode())
            n += 1
    _write_0600(a.out, json.dumps(out, indent=2))
    journal("secrets.backup_export", provider="openbao", secrets=n)
    print(f"secrets: {n} value(s) exported, transit-encrypted", file=sys.stderr)
    return 0


def cmd_backup_import(a) -> int:
    prov = sp.active()
    if prov.name != "openbao":
        raise SecretsError("backup-import is for the openbao provider")
    if a.inp == "-":
        d = json.load(sys.stdin)
    else:
        with open(a.inp) as f:
            d = json.load(f)
    if d.get("format") != BACKUP_FORMAT:
        raise SecretsError(f"{a.inp}: unknown format")
    rep = {"copied": 0, "same": 0, "differs": [], "overwritten": 0}
    for p, sec in d["secrets"].items():
        for name, ct in sec.items():
            v = prov.unwrap(ct).decode()
            cur = prov.get(p, name)
            if cur == v:
                rep["same"] += 1
            elif cur is not None and not a.overwrite:
                rep["differs"].append(f"{p}/{name}")
            else:
                prov.set(p, name, v)
                rep["overwritten" if cur is not None else "copied"] += 1
    journal("secrets.backup_import", **rep)
    _out(rep)
    return 0


def cmd_unwrap_check(a) -> int:
    """restore.sh, before touching anything: the set's wrapped keys open with THIS OpenBao."""
    prov = sp.make("openbao")
    bad = []
    n = 0
    for name in sorted(os.listdir(a.dir)):
        if not name.endswith(".key.wrapped"):
            continue
        with open(os.path.join(a.dir, name)) as f:
            d = json.load(f)
        for k in d.get("keys") or []:
            try:
                prov.unwrap(k["ciphertext"])
                n += 1
            except SecretsError as e:
                bad.append(f"{name}: {e}")
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    print(f"secrets: {n} wrapped data key(s) unwrap with {prov.describe()['transit']}",
          file=sys.stderr)
    return 0


def cmd_status(_a) -> int:
    st = sp.status()
    try:
        st["health"] = sp.active().health().as_dict()
    except SecretsError as e:
        st["health"] = {"ok": False, "detail": str(e)}
    st["wrapped"] = [os.path.basename(wrapped_path(c)) for c in CONTEXTS
                     if os.path.exists(wrapped_path(c))]
    _out(st)
    return 0 if st["health"].get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="secrets", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    m = sub.add_parser("migrate")
    m.add_argument("--from", dest="src", required=True, choices=sp.PROVIDERS)
    m.add_argument("--to", dest="dst", required=True, choices=sp.PROVIDERS)
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--check", action="store_true", help="compare only, write nothing")
    m.add_argument("--overwrite", action="store_true", help="replace values that differ")
    m.add_argument("--purge", action="store_true",
                   help="after verification: shred the clear key files (to openbao|kubernetes)")
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    e.add_argument("--from", dest="src", choices=sp.PROVIDERS)
    i = sub.add_parser("import")
    i.add_argument("--in", dest="inp", required=True)
    i.add_argument("--to", dest="dst", required=True, choices=sp.PROVIDERS)
    i.add_argument("--overwrite", action="store_true")
    i.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("rotate")
    r.add_argument("--master", action="store_true", help="openbao: rotate transit + rewrap")
    r.add_argument("--data-keys", nargs="*", metavar="CTX",
                   help="new data key(s) + re-encryption (default: vault forge teams)")
    r.add_argument("--dry-run", action="store_true")
    be = sub.add_parser("backup-export")
    be.add_argument("--out", required=True)
    bi = sub.add_parser("backup-import")
    bi.add_argument("--in", dest="inp", required=True)
    bi.add_argument("--overwrite", action="store_true")
    uc = sub.add_parser("unwrap-check")
    uc.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    fn = {"status": cmd_status, "migrate": cmd_migrate, "export": cmd_export,
          "import": cmd_import, "rotate": cmd_rotate, "backup-export": cmd_backup_export,
          "backup-import": cmd_backup_import, "unwrap-check": cmd_unwrap_check}[a.cmd]
    try:
        return fn(a)
    except SecretsError as e:
        print(f"secrets: ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
