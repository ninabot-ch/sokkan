#!/usr/bin/env python3
"""board.py — SOKKAN : sessions POSSÉDÉES par SOKKAN + kanban natif.

Modèle (refonte 2026-06-25, feedback Nick) : SOKKAN ne s'appuie PLUS sur les
fenêtres tmux existantes de Nick. Ouvrir une session = CRÉER une fenêtre tmux
(dans la session tmux `sokkan`), lancer `claude --session-id <uuid>`, et lui
apposer un TAG choisi dans une liste fixe. Le tag = nom de fenêtre ; réutiliser
le même tag incrémente (`backend`, `backend-1`, `backend-2`…). Le binding
fenêtre↔transcript est donc TOUJOURS connu → terminal + envoi marchent sans
relaunch. Le rail liste ces sessions par tag.

v2 (2026-07-02, consolidation produit) : cartes enrichies — priorité (0 urgente
→ 3 basse), échéance, checklist JSON, archivage soft (revert possible), et
timeline d'événements par carte (`card_events`) : qui a fait quoi, quand.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid as uuidlib
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_BOARD_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "board.db")))
TMUX = os.environ.get("SOKKAN_SPAWN_TMUX", "sokkan")
WD = os.environ.get("SOKKAN_PROJECT_WD", os.getcwd())
BUCKETS = ["Backlog", "Doing", "Review", "Done"]
PRIORITIES = {0: "urgente", 1: "haute", 2: "normale", 3: "basse"}

# 20 tags qui ont du sens pour le studio (domaines + types de travail)
TAGS = [
    "backend", "frontend", "mobile", "messaging", "email", "marketing", "seo",
    "infra", "devops", "database", "llm", "scraping", "matching", "billing",
    "auth", "docs", "legal", "design", "bugfix", "research",
]

# colonnes ajoutées après la v1 → migrées à la volée dans _con()
_CARD_MIGRATIONS = {
    "tag": "TEXT DEFAULT 'backend'",
    "priority": "INTEGER DEFAULT 2",
    "due": "TEXT DEFAULT ''",
    "checklist": "TEXT DEFAULT '[]'",
    "updated_at": "REAL",
    "archived": "INTEGER DEFAULT 0",
}


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, description TEXT DEFAULT '',
            tag TEXT DEFAULT 'backend', bucket TEXT NOT NULL DEFAULT 'Backlog',
            session_id TEXT, window TEXT, created_at REAL, sort REAL DEFAULT 0,
            priority INTEGER DEFAULT 2, due TEXT DEFAULT '',
            checklist TEXT DEFAULT '[]', updated_at REAL, archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, tag TEXT, window TEXT,
            title TEXT, prompt TEXT, created_at REAL,
            kind TEXT DEFAULT 'tmux', claude_session_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS card_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL, ts REAL NOT NULL,
            user TEXT DEFAULT '', action TEXT NOT NULL, detail TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_card_events_card ON card_events(card_id, ts);
        """
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
    for col, ddl in _CARD_MIGRATIONS.items():
        if col not in cols:
            con.execute(f"ALTER TABLE cards ADD COLUMN {col} {ddl}")
    scols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    if "kind" not in scols:
        con.execute("ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'tmux'")
        con.execute("ALTER TABLE sessions ADD COLUMN claude_session_id TEXT DEFAULT ''")
    con.commit()
    return con


def _event(con: sqlite3.Connection, card_id: int, user: str, action: str, detail: str = "") -> None:
    con.execute(
        "INSERT INTO card_events(card_id, ts, user, action, detail) VALUES(?,?,?,?,?)",
        (card_id, time.time(), user or "", action, detail),
    )


def _card_out(row: sqlite3.Row | dict) -> dict:
    c = dict(row)
    try:
        c["checklist"] = json.loads(c.get("checklist") or "[]")
    except (TypeError, json.JSONDecodeError):
        c["checklist"] = []
    return c


# ---------- sessions (possédées par SOKKAN) ----------

def list_sessions() -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute("SELECT * FROM sessions ORDER BY created_at DESC")]
    con.close()
    return rows


def _existing_windows() -> set[str]:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", TMUX, "-F", "#{window_name}"],
        capture_output=True, text=True, timeout=5,
    )
    return set(r.stdout.split()) if r.returncode == 0 else set()


def _uniquify(tag: str) -> str:
    """tag libre → 'tag' ; sinon 'tag-1', 'tag-2'…"""
    existing = _existing_windows()
    if tag not in existing:
        return tag
    n = 1
    while f"{tag}-{n}" in existing:
        n += 1
    return f"{tag}-{n}"


def _seed_text(prompt: str) -> str:
    return (
        f"{prompt.strip()} "
        "Commence par appeler l'outil MCP memory_search avec le sujet pour charger le "
        "contexte pertinent (puis memory_get sur les notes utiles). "
        "Ensuite propose un plan court — n'exécute rien sans mon accord."
    ).strip()


def _delayed_seed(target: str, seed: str, delay: float = 5.0) -> None:
    time.sleep(delay)
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", seed], timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], timeout=5)


def spawn(tag: str, prompt: str = "", title: str = "") -> dict:
    """Crée une fenêtre tmux taguée + lance claude --session-id + seed optionnel."""
    tag = (tag or "session").strip().replace(" ", "-")[:24]
    wname = _uniquify(tag)
    target = f"{TMUX}:{wname}"
    u = str(uuidlib.uuid4())

    exists = subprocess.run(["tmux", "has-session", "-t", TMUX], capture_output=True).returncode == 0
    if not exists:
        subprocess.run(["tmux", "new-session", "-d", "-s", TMUX, "-c", WD, "-n", wname], timeout=5)
    else:
        subprocess.run(["tmux", "new-window", "-t", f"{TMUX}:", "-c", WD, "-n", wname], timeout=5)

    subprocess.run(
        ["tmux", "send-keys", "-t", target, f"claude --name {wname} --session-id {u}", "Enter"],
        timeout=5,
    )
    if prompt.strip():
        threading.Thread(target=_delayed_seed, args=(target, _seed_text(prompt)), daemon=True).start()

    title = (title or prompt or tag).strip().splitlines()[0][:60] or tag
    con = _con()
    con.execute(
        "INSERT INTO sessions(session_id, tag, window, title, prompt, created_at) VALUES(?,?,?,?,?,?)",
        (u, tag, target, title, prompt, time.time()),
    )
    con.commit()
    con.close()
    return {"session_id": u, "tag": tag, "window": target, "title": title}


def close_session(session_id: str, kill: bool = False) -> None:
    con = _con()
    row = con.execute("SELECT window FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    con.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    con.commit()
    con.close()
    if kill and row and row["window"]:
        subprocess.run(["tmux", "kill-window", "-t", row["window"]], capture_output=True)


# ---------- sessions SDK (chat piloté par le Claude Agent SDK, sans tmux) ----------

def _uniquify_sdk(tag: str) -> str:
    """Comme _uniquify mais contre les noms du store (pas de fenêtre tmux)."""
    con = _con()
    existing = {r["tag"] for r in con.execute("SELECT tag FROM sessions")}
    con.close()
    if tag not in existing:
        return tag
    n = 1
    while f"{tag}-{n}" in existing:
        n += 1
    return f"{tag}-{n}"


def add_sdk_session(sid: str, tag: str, title: str = "", prompt: str = "") -> dict:
    """Enregistre une session SDK possédée par SOKKAN (le chat vit dans l'API,
    l'historique dans le transcript du claude_session_id, persisté plus tard)."""
    tag = (tag or "session").strip().replace(" ", "-")[:24]
    name = _uniquify_sdk(tag)
    title = (title or prompt or tag).strip().splitlines()[0][:60] or tag
    con = _con()
    con.execute(
        "INSERT INTO sessions(session_id, tag, window, title, prompt, created_at, kind)"
        " VALUES(?,?,?,?,?,?, 'sdk')",
        (sid, name, "", title, prompt, time.time()),
    )
    con.commit()
    con.close()
    return {"session_id": sid, "tag": name, "window": "", "title": title, "kind": "sdk"}


def set_claude_session_id(sid: str, csid: str) -> None:
    con = _con()
    con.execute("UPDATE sessions SET claude_session_id=? WHERE session_id=?", (csid, sid))
    con.commit()
    con.close()


def get_claude_session_id(sid: str) -> str:
    con = _con()
    r = con.execute("SELECT claude_session_id FROM sessions WHERE session_id=?", (sid,)).fetchone()
    con.close()
    return (r["claude_session_id"] if r else "") or ""


def seed_text(prompt: str) -> str:
    return _seed_text(prompt)


# ---------- cartes ----------

def list_cards(include_archived: bool = False) -> dict:
    con = _con()
    where = "" if include_archived else "WHERE archived=0"
    rows = [_card_out(r) for r in con.execute(f"SELECT * FROM cards {where} ORDER BY sort, id")]
    con.close()
    return {b: [c for c in rows if c["bucket"] == b] for b in BUCKETS}


def add_card(title: str, description: str = "", tag: str = "backend",
             bucket: str = "Backlog", priority: int = 2, due: str = "",
             user: str = "") -> dict:
    if bucket not in BUCKETS:
        bucket = "Backlog"
    title = (title.strip() or description.strip()[:60] or "tâche")
    now = time.time()
    con = _con()
    cur = con.execute(
        "INSERT INTO cards(title, description, tag, bucket, created_at, sort, priority, due, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (title, description.strip(), tag, bucket, now, now, int(priority), due, now),
    )
    _event(con, cur.lastrowid, user, "création", f"« {title} » dans {bucket}")
    con.commit()
    row = _card_out(con.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone())
    con.close()
    return row


def get_card(card_id: int) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    con.close()
    return _card_out(r) if r else None


def card_events(card_id: int, limit: int = 50) -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT ts, user, action, detail FROM card_events WHERE card_id=? ORDER BY ts DESC LIMIT ?",
        (card_id, limit),
    )]
    con.close()
    return rows


def _describe_change(field: str, old, new) -> str:
    if field == "description":
        return "description modifiée"
    if field == "checklist":
        return "checklist mise à jour"
    if field == "priority":
        return f"priorité : {PRIORITIES.get(old, old)} → {PRIORITIES.get(int(new), new)}"
    if field == "sort":
        return "réordonnée"
    return f"{field} : {old or '∅'} → {new or '∅'}"


def update_card(card_id: int, user: str = "", **fields) -> dict | None:
    allowed = {"title", "description", "tag", "bucket", "session_id", "window",
               "sort", "priority", "due", "checklist", "archived"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_card(card_id)
    old = get_card(card_id)
    if old is None:
        return None
    if "checklist" in sets and not isinstance(sets["checklist"], str):
        sets["checklist"] = json.dumps(sets["checklist"], ensure_ascii=False)
    sets["updated_at"] = time.time()
    con = _con()
    con.execute(
        f"UPDATE cards SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), card_id),
    )
    for k, v in sets.items():
        if k in ("updated_at", "session_id", "window"):
            continue
        ov = json.dumps(old.get(k), ensure_ascii=False) if k == "checklist" else old.get(k)
        if ov != v:
            if k == "bucket":
                _event(con, card_id, user, "déplacement", f"{old['bucket']} → {v}")
            elif k == "archived":
                _event(con, card_id, user, "archivage" if v else "restauration", "")
            else:
                _event(con, card_id, user, "édition", _describe_change(k, old.get(k), v))
    con.commit()
    con.close()
    return get_card(card_id)


def delete_card(card_id: int, user: str = "") -> None:
    con = _con()
    con.execute("DELETE FROM cards WHERE id=?", (card_id,))
    con.execute("DELETE FROM card_events WHERE card_id=?", (card_id,))
    con.commit()
    con.close()


def spawn_card(card_id: int, user: str = "") -> dict:
    card = get_card(card_id)
    if not card:
        raise ValueError("card not found")
    s = spawn(card["tag"], prompt=card["description"], title=card["title"])
    update_card(card_id, user=user, session_id=s["session_id"], window=s["window"], bucket="Doing")
    con = _con()
    _event(con, card_id, user, "spawn", f"session {s['window']}")
    con.commit()
    con.close()
    return {**s, "card_id": card_id}
