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

3.2 (board piloté depuis les sessions, POC RTS) : assigné, clôture (terminé ≠
supprimé : `closed_at`/`closed_by`), commentaires signés (`card_comments`),
liens vers session / agent / run / incident (`card_links`), recherche, et chaque
événement porte la session d'où il vient (`session_id`, `session_tag`, `via`).
"""
from __future__ import annotations

import json
import os
import sqlite3
import shlex
import subprocess
import threading
import time
import uuid as uuidlib
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_BOARD_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "board.db")))
TMUX = os.environ.get("SOKKAN_SPAWN_TMUX", "sokkan")
WD = os.environ.get("SOKKAN_PROJECT_WD", os.getcwd())
BUCKETS = ["Backlog", "Doing", "Review", "Done"]
PRIORITIES = {0: "urgent", 1: "high", 2: "normal", 3: "low"}

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
    "assignee": "TEXT DEFAULT ''",
    "closed_at": "REAL",
    "closed_by": "TEXT DEFAULT ''",
    # 3.2 multi-user : chaque carte appartient à un projet ; l'existant → 'default'
    "project": "TEXT NOT NULL DEFAULT 'default'",
}
# 3.2 : d'où vient un événement (session SOKKAN, canal web / mcp / run d'agent)
_EVENT_MIGRATIONS = {
    "session_id": "TEXT DEFAULT ''",
    "session_tag": "TEXT DEFAULT ''",
    "via": "TEXT DEFAULT ''",
}
LINK_KINDS = ("session", "agent", "run", "incident")


_init_lock = threading.Lock()
_initialized = False


def init(force: bool = False) -> None:
    """DDL + migrations, exécutés une seule fois par process (force=True pour
    ré-initialiser après un changement de DB, p.ex. dans les tests)."""
    global _initialized
    with _init_lock:
        if _initialized and not force:
            return
        DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB)
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
            CREATE TABLE IF NOT EXISTS card_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL, ts REAL NOT NULL, author TEXT DEFAULT '',
                session_id TEXT DEFAULT '', session_tag TEXT DEFAULT '', via TEXT DEFAULT '',
                body TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_card_comments_card ON card_comments(card_id, ts);
            CREATE TABLE IF NOT EXISTS card_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
                ts REAL NOT NULL, created_by TEXT DEFAULT '',
                UNIQUE(card_id, kind, ref)
            );
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
        for col, ddl in _CARD_MIGRATIONS.items():
            if col not in cols:
                con.execute(f"ALTER TABLE cards ADD COLUMN {col} {ddl}")
        ecols = {r[1] for r in con.execute("PRAGMA table_info(card_events)")}
        for col, ddl in _EVENT_MIGRATIONS.items():
            if col not in ecols:
                con.execute(f"ALTER TABLE card_events ADD COLUMN {col} {ddl}")
        scols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        if "kind" not in scols:
            con.execute("ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'tmux'")
            con.execute("ALTER TABLE sessions ADD COLUMN claude_session_id TEXT DEFAULT ''")
        if "secrets" not in scols:  # 3.1 : secrets nommés à l'ouverture (JSON, NULL = non choisi)
            con.execute("ALTER TABLE sessions ADD COLUMN secrets TEXT DEFAULT NULL")
        if "project" not in scols:  # 3.2 : projet de la session (périmètre du rappel mémoire)
            con.execute("ALTER TABLE sessions ADD COLUMN project TEXT NOT NULL DEFAULT 'default'")
        con.commit()
        con.close()
        _initialized = True


def _con() -> sqlite3.Connection:
    init()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _event(con: sqlite3.Connection, card_id: int, user: str, action: str, detail: str = "",
           origin: dict | None = None) -> None:
    """`origin` = d'où vient l'action : {session_id, session_tag, via} — posé par
    l'appelant (API : via=web ; MCP : la session de l'env), jamais par le modèle."""
    o = origin or {}
    con.execute(
        "INSERT INTO card_events(card_id, ts, user, action, detail, session_id, session_tag, via)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (card_id, time.time(), user or "", action, detail, o.get("session_id") or "",
         o.get("session_tag") or "", o.get("via") or ""),
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


# Où vivent les notes — même défaut que memory/index_memory.py. Une session
# libre l'ignorait totalement : seuls les playbooks `onboard-memory`/`digest`
# injectaient le chemin, donc un agent à qui on demandait « écris une note »
# partait en `find /` (constaté sur les 2 missions blanches, 09.2026).
MEMORY_DIR = os.environ.get(
    "SOKKAN_MEMORY_DIR", os.path.expanduser("~/.sokkan/memory"))


def _memory_howto() -> str:
    """Une ligne, dans CHAQUE seed : comment écrire dans la mémoire. Le rappel
    (lecture) est déjà pré-injecté ; c'est l'écriture qui manquait."""
    return (
        "To record a decision or a lesson, call the mcp__sokkan-memory__memory_write MCP "
        f"tool (one durable fact per note); the notes themselves live in {MEMORY_DIR}."
    )


def _seed_text(prompt: str, recall: str = "") -> str:
    """Seed d'une session. `recall` = bloc mémoire PRÉ-INJECTÉ (recherche faite
    côté serveur au spawn — déterministe, n'attend pas que le modèle obéisse).
    Sans recall (mémoire vide, backend down, spawn tmux), on retombe sur le
    rituel textuel historique."""
    if recall:
        return (
            f"{prompt.strip()}\n\n{recall}\n"
            "The notes above were auto-recalled from project memory for this task. "
            "Call the mcp__sokkan-memory__memory_get MCP tool on any note you need in full, and "
            "mcp__sokkan-memory__memory_search for other angles (real MCP tool calls, not shell "
            f"commands). {_memory_howto()} "
            "Then propose a short plan — don't execute anything without my go-ahead."
        ).strip()
    return (
        f"{prompt.strip()} "
        "Start by calling the mcp__sokkan-memory__memory_search MCP tool on this topic to load "
        "any relevant project context (then mcp__sokkan-memory__memory_get on the useful notes — "
        "real MCP tool calls, not shell commands). If the project memory is still empty, just note "
        f"that and carry on. {_memory_howto()} "
        "Then propose a short plan — don't execute anything without my go-ahead."
    ).strip()


def _delayed_seed(target: str, seed: str, delay: float = 5.0) -> None:
    time.sleep(delay)
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", seed], timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], timeout=5)


def _recall_settings_arg() -> str:
    """`--settings <file>`: the memory recall hooks (3.0), or nothing when they are off."""
    try:
        import memrecall
        path = memrecall.cli_settings_path()
    except Exception:  # noqa: BLE001 — a session always starts, with or without recall
        return ""
    return f" --settings {shlex.quote(path)}" if path else ""


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
        ["tmux", "send-keys", "-t", target, f"claude --name {wname} --session-id {u}"
         + _recall_settings_arg(), "Enter"],
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


def add_sdk_session(sid: str, tag: str, title: str = "", prompt: str = "",
                    secrets: list[str] | None = None, project: str = "default") -> dict:
    """Enregistre une session SDK possédée par SOKKAN (le chat vit dans l'API,
    l'historique dans le transcript du claude_session_id, persisté plus tard)."""
    tag = (tag or "session").strip().replace(" ", "-")[:24]
    name = _uniquify_sdk(tag)
    title = (title or prompt or tag).strip().splitlines()[0][:60] or tag
    con = _con()
    con.execute(
        "INSERT INTO sessions(session_id, tag, window, title, prompt, created_at, kind, secrets,"
        " project) VALUES(?,?,?,?,?,?, 'sdk', ?, ?)",
        (sid, name, "", title, prompt, time.time(),
         None if secrets is None else json.dumps(list(secrets)), project or "default"),
    )
    con.commit()
    con.close()
    return {"session_id": sid, "tag": name, "window": "", "title": title, "kind": "sdk",
            "project": project or "default"}


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


def get_session_project(sid: str) -> str | None:
    """Projet d'une session SOKKAN (3.2) ; None = session inconnue de SOKKAN."""
    con = _con()
    r = con.execute("SELECT project FROM sessions WHERE session_id=?", (sid,)).fetchone()
    con.close()
    return (r["project"] or "default") if r else None


def get_session_secrets(sid: str) -> list[str] | None:
    """Secrets choisis à l'ouverture de la session (None = aucun choix enregistré)."""
    con = _con()
    r = con.execute("SELECT secrets FROM sessions WHERE session_id=?", (sid,)).fetchone()
    con.close()
    if not r or r["secrets"] is None:
        return None
    try:
        v = json.loads(r["secrets"])
        return [x for x in v if isinstance(x, str)]
    except ValueError:
        return []


def seed_text(prompt: str, recall: str = "") -> str:
    return _seed_text(prompt, recall)


# ---------- cartes ----------

def list_cards(include_archived: bool = False, project: str | None = None) -> dict:
    """Cards per column; ``project`` (3.2) = that project's board only."""
    con = _con()
    conds, args = ([] if include_archived else ["archived=0"]), []
    if project is not None:
        conds.append("project=?")
        args.append(project)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = [_card_out(r) for r in con.execute(f"SELECT * FROM cards {where} ORDER BY sort, id",
                                              args)]
    con.close()
    return {b: [c for c in rows if c["bucket"] == b] for b in BUCKETS}


def add_card(title: str, description: str = "", tag: str = "backend",
             bucket: str = "Backlog", priority: int = 2, due: str = "",
             user: str = "", origin: dict | None = None, project: str = "default") -> dict:
    if bucket not in BUCKETS:
        bucket = "Backlog"
    title = (title.strip() or description.strip()[:60] or "tâche")
    now = time.time()
    con = _con()
    cur = con.execute(
        "INSERT INTO cards(title, description, tag, bucket, created_at, sort, priority, due, updated_at,"
        " closed_at, closed_by, project) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (title, description.strip(), tag, bucket, now, now, int(priority), due, now,
         now if bucket == "Done" else None, (user or "") if bucket == "Done" else "",
         project or "default"),
    )
    _event(con, cur.lastrowid, user, "created", f"\u201c{title}\u201d in {bucket}", origin)
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
        "SELECT ts, user, action, detail, session_id, session_tag, via FROM card_events"
        " WHERE card_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (card_id, limit),
    )]
    con.close()
    return rows


def _describe_change(field: str, old, new) -> str:
    if field == "description":
        return "description updated"
    if field == "checklist":
        return "checklist updated"
    if field == "priority":
        return f"priority: {PRIORITIES.get(old, old)} \u2192 {PRIORITIES.get(int(new), new)}"
    if field == "sort":
        return "reordered"
    return f"{field}: {old or '\u2014'} \u2192 {new or '\u2014'}"


def update_card(card_id: int, user: str = "", origin: dict | None = None,
                **fields) -> dict | None:
    allowed = {"title", "description", "tag", "bucket", "session_id", "window",
               "sort", "priority", "due", "checklist", "archived", "assignee"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_card(card_id)
    old = get_card(card_id)
    if old is None:
        return None
    if "checklist" in sets and not isinstance(sets["checklist"], str):
        sets["checklist"] = json.dumps(sets["checklist"], ensure_ascii=False)
    now = time.time()
    sets["updated_at"] = now
    # Done = clos : la clôture suit la colonne, quel que soit le chemin (web, MCP)
    if "bucket" in sets and sets["bucket"] != old["bucket"]:
        if sets["bucket"] == "Done" and not old.get("closed_at"):
            sets["closed_at"], sets["closed_by"] = now, user or ""
        elif sets["bucket"] != "Done":
            sets["closed_at"], sets["closed_by"] = None, ""
    con = _con()
    con.execute(
        f"UPDATE cards SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), card_id),
    )
    for k, v in sets.items():
        if k in ("updated_at", "session_id", "window", "closed_at", "closed_by"):
            continue
        ov = json.dumps(old.get(k), ensure_ascii=False) if k == "checklist" else old.get(k)
        if ov != v:
            if k == "bucket":
                _event(con, card_id, user, "moved", f"{old['bucket']} \u2192 {v}", origin)
            elif k == "archived":
                _event(con, card_id, user, "archived" if v else "restored", "", origin)
            elif k == "assignee":
                _event(con, card_id, user, "assigned", f"{old.get(k) or '\u2014'} \u2192 {v or '\u2014'}",
                       origin)
            else:
                _event(con, card_id, user, "edited", _describe_change(k, old.get(k), v), origin)
    con.commit()
    con.close()
    return get_card(card_id)


def delete_card(card_id: int, user: str = "") -> None:
    con = _con()
    con.execute("DELETE FROM cards WHERE id=?", (card_id,))
    con.execute("DELETE FROM card_events WHERE card_id=?", (card_id,))
    con.execute("DELETE FROM card_comments WHERE card_id=?", (card_id,))
    con.execute("DELETE FROM card_links WHERE card_id=?", (card_id,))
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


# ---------- 3.2 : clôture, commentaires, liens, recherche ----------

def close_card(card_id: int, user: str = "", resolution: str = "",
               origin: dict | None = None) -> dict | None:
    """Terminé ≠ supprimé : la carte passe en Done, garde tout son historique,
    et `reopen_card` la rouvre. Clore une carte déjà close ne change rien."""
    old = get_card(card_id)
    if old is None:
        return None
    if old["bucket"] == "Done" and old.get("closed_at"):
        return old
    now = time.time()
    con = _con()
    con.execute("UPDATE cards SET bucket='Done', closed_at=?, closed_by=?, updated_at=? WHERE id=?",
                (now, user or "", now, card_id))
    detail = f"{old['bucket']} \u2192 Done" + (f" \u2014 {resolution.strip()[:300]}" if resolution.strip() else "")
    _event(con, card_id, user, "closed", detail, origin)
    con.commit()
    con.close()
    return get_card(card_id)


def reopen_card(card_id: int, user: str = "", bucket: str = "Backlog", reason: str = "",
                origin: dict | None = None) -> dict | None:
    """Rouvre une carte close (et/ou archivée) dans `bucket` (pas Done)."""
    if bucket not in BUCKETS or bucket == "Done":
        raise ValueError(f"reopen into one of {[b for b in BUCKETS if b != 'Done']}")
    old = get_card(card_id)
    if old is None:
        return None
    if old["bucket"] != "Done" and not old.get("archived") and not old.get("closed_at"):
        return old
    now = time.time()
    con = _con()
    con.execute("UPDATE cards SET bucket=?, closed_at=NULL, closed_by='', archived=0, updated_at=?"
                " WHERE id=?", (bucket, now, card_id))
    detail = f"{old['bucket']} \u2192 {bucket}" + (f" \u2014 {reason.strip()[:300]}" if reason.strip() else "")
    _event(con, card_id, user, "reopened", detail, origin)
    con.commit()
    con.close()
    return get_card(card_id)


def archive_card(card_id: int, user: str = "", reason: str = "",
                 origin: dict | None = None) -> dict | None:
    """Archivage soft : la carte quitte le board, reste lisible (get_card,
    search_cards include_archived) et se restaure (reopen_card / UI)."""
    old = get_card(card_id)
    if old is None:
        return None
    if old.get("archived"):
        return old
    con = _con()
    con.execute("UPDATE cards SET archived=1, updated_at=? WHERE id=?", (time.time(), card_id))
    _event(con, card_id, user, "archived", reason.strip()[:300], origin)
    con.commit()
    con.close()
    return get_card(card_id)


COMMENT_MAX = 8000


def add_comment(card_id: int, body: str, author: str = "",
                origin: dict | None = None) -> dict | None:
    body = (body or "").strip()
    if not body:
        raise ValueError("empty comment")
    if get_card(card_id) is None:
        return None
    o = origin or {}
    con = _con()
    cur = con.execute(
        "INSERT INTO card_comments(card_id, ts, author, session_id, session_tag, via, body)"
        " VALUES(?,?,?,?,?,?,?)",
        (card_id, time.time(), author or "", o.get("session_id") or "",
         o.get("session_tag") or "", o.get("via") or "", body[:COMMENT_MAX]),
    )
    first = body.splitlines()[0]
    _event(con, card_id, author, "commented", first[:120] + ("\u2026" if len(first) > 120 else ""), origin)
    con.execute("UPDATE cards SET updated_at=? WHERE id=?", (time.time(), card_id))
    con.commit()
    row = dict(con.execute("SELECT * FROM card_comments WHERE id=?", (cur.lastrowid,)).fetchone())
    con.close()
    return row


def card_comments(card_id: int, limit: int = 200) -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM card_comments WHERE card_id=? ORDER BY ts, id LIMIT ?", (card_id, limit))]
    con.close()
    return rows


def resolve_link(kind: str, ref, project: str | None = None) -> dict | None:
    """Le lien pointe-t-il vers un objet qui EXISTE ? → {kind, ref, label, status,
    href} ; None sinon. `href` = lien profond du cockpit (session : ouverte par l'UI).
    `project` (3.2) : une session / un agent / un run d'un AUTRE projet n'existe pas."""
    ref = str(ref).strip()

    def other(obj_project) -> bool:
        return project is not None and (obj_project or "default") != project
    if kind not in LINK_KINDS or not ref:
        return None
    try:
        if kind == "session":
            s = next((x for x in list_sessions() if x["session_id"] == ref), None)
            if not s or other(s.get("project")):
                return None
            return {"kind": kind, "ref": ref, "label": f"{s.get('tag') or ''} \u00b7 {s.get('title') or ''}".strip(" \u00b7"),
                    "status": s.get("kind") or "", "href": ""}
        if kind == "agent":
            import agents
            a = agents.resolve(int(ref) if ref.isdigit() else ref)
            if not a or other(a.get("project")):
                return None
            return {"kind": kind, "ref": str(a["id"]), "label": a["name"], "status": a["status"],
                    "href": f"/?tab=crew&agent={a['id']}"}
        if kind == "run":
            import agents
            if not ref.lstrip("#").isdigit():
                return None
            r = agents.get_run(int(ref.lstrip("#")))
            if not r:
                return None
            a = agents.get(r["agent_id"]) or {}
            if other(a.get("project")):
                return None
            return {"kind": kind, "ref": str(r["id"]),
                    "label": f"run #{r['id']} of {a.get('name') or 'agent #' + str(r['agent_id'])}",
                    "status": r["status"], "agent_id": r["agent_id"],
                    "href": f"/?tab=crew&agent={r['agent_id']}&run={r['id']}"}
        if kind == "incident":
            import observability
            if not ref.lstrip("#").isdigit():
                return None
            iid = int(ref.lstrip("#"))
            con = observability._con()
            row = con.execute("SELECT id, title, status FROM incidents WHERE id=?", (iid,)).fetchone()
            con.close()
            if not row:
                return None
            return {"kind": kind, "ref": str(row["id"]), "label": row["title"] or f"incident #{iid}",
                    "status": row["status"], "href": f"/?tab=operate&incident={row['id']}"}
    except (ValueError, sqlite3.Error):
        return None
    return None


def link_card(card_id: int, kind: str, ref, user: str = "", remove: bool = False,
              origin: dict | None = None) -> dict | None:
    """Relie (ou délie) une carte à une session / un agent / un run / un incident.
    Refuse un objet qui n'existe pas (ValueError) ; None si la carte n'existe pas."""
    if kind not in LINK_KINDS:
        raise ValueError(f"unknown link kind: {kind} (valid: {list(LINK_KINDS)})")
    card = get_card(card_id)
    if card is None:
        return None
    project = card.get("project") or "default"
    con = _con()
    if remove:
        t = resolve_link(kind, ref)
        rref = t["ref"] if t else str(ref).strip().lstrip("#")
        cur = con.execute("DELETE FROM card_links WHERE card_id=? AND kind=? AND ref=?",
                          (card_id, kind, rref))
        if cur.rowcount:
            _event(con, card_id, user, "unlinked", f"{kind} {ref}", origin)
        con.commit()
        con.close()
        return {"card_id": card_id, "removed": bool(cur.rowcount)}
    target = resolve_link(kind, ref, project)
    if target is None:
        con.close()
        raise ValueError(f"{kind} {ref} not found")
    cur = con.execute("INSERT OR IGNORE INTO card_links(card_id, kind, ref, ts, created_by)"
                      " VALUES(?,?,?,?,?)", (card_id, kind, target["ref"], time.time(), user or ""))
    if cur.rowcount:
        _event(con, card_id, user, "linked", f"{kind} {target['label']}", origin)
    con.commit()
    con.close()
    return {"card_id": card_id, **target, "added": bool(cur.rowcount)}


def card_links(card_id: int) -> list[dict]:
    """Liens de la carte, résolus (un objet disparu reste listé, `missing`).
    La session spawnée depuis la carte (`cards.session_id`) compte comme un lien."""
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT kind, ref, ts, created_by FROM card_links WHERE card_id=? ORDER BY ts, id", (card_id,))]
    c = con.execute("SELECT session_id FROM cards WHERE id=?", (card_id,)).fetchone()
    con.close()
    if c and c["session_id"] and not any(r["kind"] == "session" and r["ref"] == c["session_id"]
                                         for r in rows):
        rows.insert(0, {"kind": "session", "ref": c["session_id"], "ts": None, "created_by": "spawn"})
    out = []
    for r in rows:
        t = resolve_link(r["kind"], r["ref"])
        out.append({**r, **(t or {"label": f"{r['kind']} {r['ref']}", "status": "", "href": "",
                                   "missing": True})})
    return out


def card_detail(card_id: int) -> dict | None:
    c = get_card(card_id)
    if not c:
        return None
    return {**c, "events": card_events(card_id), "comments": card_comments(card_id),
            "links": card_links(card_id)}


def search_cards(query: str = "", tag: str = "", bucket: str = "", assignee: str = "",
                 include_archived: bool = False, limit: int = 50,
                 project: str | None = None) -> list[dict]:
    """Recherche plein texte simple (titre, description, commentaires) + filtres ;
    `project` (3.2) = le board de ce projet seulement."""
    where, args = [], []
    if project is not None:
        where.append("c.project=?")
        args.append(project)
    if not include_archived:
        where.append("c.archived=0")
    if tag:
        where.append("c.tag=?")
        args.append(tag)
    if bucket:
        where.append("c.bucket=?")
        args.append(bucket)
    if assignee:
        if assignee in ("none", "-"):
            where.append("COALESCE(c.assignee,'')=''")
        else:
            where.append("LOWER(c.assignee)=LOWER(?)")
            args.append(assignee.strip())
    for word in (query or "").split()[:8]:
        like = f"%{word.replace('%', '').replace('_', '')}%"
        where.append("(c.title LIKE ? OR c.description LIKE ? OR EXISTS (SELECT 1 FROM card_comments k"
                     " WHERE k.card_id=c.id AND k.body LIKE ?))")
        args += [like, like, like]
    sql = "SELECT c.* FROM cards c" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY c.archived, CASE c.bucket WHEN 'Done' THEN 1 ELSE 0 END, c.priority," \
           " COALESCE(c.updated_at, c.created_at) DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    con = _con()
    rows = [_card_out(r) for r in con.execute(sql, args)]
    con.close()
    return rows


def validate_assignee(value: str) -> str:
    """'' (personne), l'email d'un utilisateur IAM connu, ou `agent:<nom>` d'un
    agent existant. Renvoie la valeur normalisée ; ValueError sinon."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.lower().startswith("agent:"):
        import agents
        name = v.split(":", 1)[1].strip()
        a = agents.get_by_name(name)
        if not a:
            raise ValueError(f"unknown agent: {name}")
        return f"agent:{a['name']}"
    import iam
    email = v.lower()
    if not any(u["email"] == email for u in iam.list_users()):
        raise ValueError(f"unknown user: {v} (an IAM user email, or agent:<name>)")
    return email
