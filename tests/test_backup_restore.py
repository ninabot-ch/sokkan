"""scripts/backup.sh + scripts/restore.sh end to end (local mode, throw-away data dir).

Populates a SOKKAN_DATA_DIR under tmp_path (SQLite databases, one in WAL mode, a vault
key + an encrypted secret, project memory), backs it up, wipes it, restores it and
compares. With SOKKAN_TEST_PG_DSN (an admin DSN), a throw-away database is created,
dumped, emptied, restored and dropped. Never points at a real instance's data.
"""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"
PASS = "correct horse battery staple"

fernet = pytest.importorskip("cryptography.fernet")
if not shutil.which("openssl") or not shutil.which("tar") or not shutil.which("python3"):
    pytest.skip("openssl, tar and python3 are required", allow_module_level=True)


def _env(data_dir, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SOKKAN_", "CORTHEXIS_"))}
    env.update(SOKKAN_BACKUP_MODE="local", SOKKAN_DATA_DIR=str(data_dir))
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run(script, *args, env, check=True):
    r = subprocess.run(["sh", str(script), *map(str, args)], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise AssertionError(f"{script.name} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def _sets(out):
    return sorted(p for p in out.iterdir() if p.name.startswith("sokkan-backup-"))


def _populate(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(d / "board.db") as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, title TEXT, project TEXT)")
        c.executemany("INSERT INTO cards (title, project) VALUES (?, ?)",
                      [(f"card {i}", "default" if i % 2 else "radio") for i in range(50)])
    with sqlite3.connect(d / "agents.db") as c:
        c.execute("CREATE TABLE agents (name TEXT PRIMARY KEY, prompt TEXT)")
        c.execute("INSERT INTO agents VALUES ('nightly', 'check the backups')")
    with sqlite3.connect(d / "audit.db") as c:
        c.execute("CREATE TABLE audit (ts REAL, event TEXT)")
        c.execute("INSERT INTO audit VALUES (1.5, 'project.grant.self')")
    key = fernet.Fernet.generate_key()
    (d / "vault.key").write_bytes(key)
    os.chmod(d / "vault.key", 0o600)
    token = fernet.Fernet(key).encrypt(b"s3cr3t-value").decode()
    (d / "vault.json").write_text(json.dumps({"DB_PASSWORD": token}))
    (d / "session.key").write_bytes(secrets.token_bytes(32))
    (d / "llm.json").write_text('{"model": "x"}')
    mem = d / "projects" / "radio" / "memory"
    mem.mkdir(parents=True)
    (mem / "x.md").write_text("---\nname: x\n---\nradio note\n")
    (d / "memory-quarantine").mkdir()
    (d / "memory-quarantine" / "q.md").write_text("held\n")


def _sqlite_dump(p: Path) -> str:
    with sqlite3.connect(p) as c:
        return "\n".join(c.iterdump())


def _snapshot(d: Path) -> dict:
    """Comparable view of a data dir: SQL dumps for SQLite, hashes for the rest."""
    out = {}
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.name.endswith(("-wal", "-shm", "-journal")):
            continue
        rel = str(p.relative_to(d))
        if p.suffix == ".db":
            out[rel] = _sqlite_dump(p)
        else:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --- Postgres (optional) ------------------------------------------------------------

def _psql(dsn, sql):
    r = subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", dsn, "-c", sql],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture()
def pg_db():
    admin = os.environ.get("SOKKAN_TEST_PG_DSN")
    if not admin:
        yield None
        return
    if not all(shutil.which(t) for t in ("psql", "pg_dump", "pg_restore")):
        pytest.skip("psql / pg_dump / pg_restore not installed")
    name = f"lot4_backup_test_{secrets.token_hex(4)}"
    _psql(admin, f'CREATE DATABASE "{name}"')
    u = urlsplit(admin)
    dsn = urlunsplit((u.scheme, u.netloc, "/" + name, u.query, u.fragment))
    try:
        yield dsn
    finally:
        _psql(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _pg_rows(dsn):
    return _psql(dsn, "SELECT id, name, body FROM notes ORDER BY id")


# --- tests ---------------------------------------------------------------------------

def test_roundtrip_with_encrypted_key(tmp_path, pg_db):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    extra = {"SOKKAN_BACKUP_KEY_PASSPHRASE": PASS}
    if pg_db:
        _psql(pg_db, "CREATE TABLE notes (id int PRIMARY KEY, name text, body text);"
                     "INSERT INTO notes SELECT g, 'n' || g, repeat('x', g) FROM generate_series(1, 40) g")
        extra["SOKKAN_BACKUP_PG_DSN"] = pg_db
        rows_before = _pg_rows(pg_db)
    before = _snapshot(data)
    key_before = (data / "vault.key").read_bytes()

    _run(BACKUP, out, env=_env(data, **extra))
    (s,) = _sets(out)
    assert oct(s.stat().st_mode & 0o777) == "0o700"
    names = {p.name for p in s.iterdir()}
    assert {"MANIFEST", "data.tgz", "vault.key.enc"} <= names
    assert "vault.key" not in names
    assert ("pg.dump" in names) == bool(pg_db)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in s.iterdir())
    manifest = (s / "MANIFEST").read_text()
    assert f"sokkan_version: {(ROOT / 'VERSION').read_text().strip()}" in manifest
    for db in ("board.db", "agents.db", "audit.db"):
        assert db in manifest.split("sqlite:")[1].splitlines()[0]
    with tarfile.open(s / "data.tgz") as t:
        members = t.getnames()
    assert "vault.key" not in members and "./vault.key" not in members
    assert not any(m.endswith(("-wal", "-shm")) for m in members)

    # wipe everything, then restore
    shutil.rmtree(data)
    data.mkdir()
    (data / "stray.txt").write_text("must disappear")
    if pg_db:
        _psql(pg_db, "DROP TABLE notes")
    r = _run(RESTORE, s, env=_env(data, **extra), check=False)
    assert r.returncode != 0 and "--yes" in r.stderr  # destructive: needs --yes
    assert (data / "stray.txt").exists()
    _run(RESTORE, s, "--yes", env=_env(data, **extra))

    assert _snapshot(data) == before
    assert not (data / "stray.txt").exists()
    assert (data / "vault.key").read_bytes() == key_before
    assert (data / "vault.key").stat().st_mode & 0o777 == 0o600
    token = json.loads((data / "vault.json").read_text())["DB_PASSWORD"]
    assert fernet.Fernet((data / "vault.key").read_bytes()).decrypt(token.encode()) == b"s3cr3t-value"
    if pg_db:
        assert _pg_rows(pg_db) == rows_before
        assert rows_before.count("\n") == 40


def test_wrong_passphrase_touches_nothing(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    _run(BACKUP, out, env=_env(data, SOKKAN_BACKUP_KEY_PASSPHRASE=PASS))
    (s,) = _sets(out)
    (data / "new.txt").write_text("after the backup")
    r = _run(RESTORE, s, "--yes", env=_env(data, SOKKAN_BACKUP_KEY_PASSPHRASE="wrong"), check=False)
    assert r.returncode != 0
    assert (data / "new.txt").exists()


def test_without_passphrase_key_not_included(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    key = (data / "vault.key").read_bytes()
    r = _run(BACKUP, out, env=_env(data))
    assert "NOT included" in r.stderr
    (s,) = _sets(out)
    names = {p.name for p in s.iterdir()}
    assert "VAULT_KEY_NOT_INCLUDED.txt" in names
    assert not names & {"vault.key", "vault.key.enc"}
    assert "vault_key: not-included" in (s / "MANIFEST").read_text()
    with tarfile.open(s / "data.tgz") as t:
        for m in t.getmembers():
            assert Path(m.name).name != "vault.key"
            if m.isfile():
                assert key not in t.extractfile(m).read()

    # restore with the key kept aside (--vault-key)
    aside = tmp_path / "kept.key"
    aside.write_bytes(key)
    shutil.rmtree(data)
    _run(RESTORE, s, "--yes", "--vault-key", aside, env=_env(data))
    assert (data / "vault.key").read_bytes() == key


def test_plain_key_option(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    r = _run(BACKUP, out, "--include-plain-key", env=_env(data))
    assert "IN CLEAR" in r.stderr
    (s,) = _sets(out)
    assert (s / "vault.key").read_bytes() == (data / "vault.key").read_bytes()
    assert (s / "vault.key").stat().st_mode & 0o777 == 0o600


def test_retention_keeps_n_sets(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    (out / "unrelated").mkdir(parents=True)
    for _ in range(3):
        _run(BACKUP, out, env=_env(data, SOKKAN_BACKUP_KEEP=2))
    sets = _sets(out)
    assert len(sets) == 2
    assert (out / "unrelated").is_dir()


def test_corrupted_manifest_refused(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    _run(BACKUP, out, env=_env(data, SOKKAN_BACKUP_KEY_PASSPHRASE=PASS))
    (s,) = _sets(out)
    m = s / "MANIFEST"
    text = m.read_text()
    line = next(x for x in text.splitlines() if x.endswith("  data.tgz"))
    bad = ("0" if line[0] != "0" else "1") + line[1:]
    m.write_text(text.replace(line, bad))
    (data / "new.txt").write_text("after the backup")
    before = _snapshot(data)
    r = _run(RESTORE, s, "--yes", env=_env(data, SOKKAN_BACKUP_KEY_PASSPHRASE=PASS), check=False)
    assert r.returncode != 0
    assert "checksum mismatch: data.tgz" in r.stderr
    assert _snapshot(data) == before


def test_version_mismatch_refused(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    _populate(data)
    _run(BACKUP, out, env=_env(data))
    (s,) = _sets(out)
    m = s / "MANIFEST"
    m.write_text(m.read_text().replace("sokkan_version: ", "sokkan_version: 0.0.0-"))
    r = _run(RESTORE, s, "--yes", env=_env(data), check=False)
    assert r.returncode != 0 and "--force-version" in r.stderr
    _run(RESTORE, s, "--yes", "--force-version", env=_env(data))
