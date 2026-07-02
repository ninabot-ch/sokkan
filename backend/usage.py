#!/usr/bin/env python3
"""usage.py — SOKKAN S2 : coûts & tokens par session/jour depuis les transcripts.

Source de vérité = les JSONL de Claude Code (chaque message assistant porte
`message.usage` : input/output/cache_read/cache_creation + model). Pas de
costUSD dans les transcripts → on ESTIME avec la grille tarifaire API
(l'abonnement Max rend le coût notionnel pour nous ; pour un client BYOK
c'est sa facture réelle). Cache sqlite par fichier (mtime+size) pour ne pas
re-parser 82 transcripts × 18 Mo à chaque poll.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(os.environ.get("SOKKAN_PROJECT_DIR", os.path.expanduser("~/.claude/projects")))
DB = Path(os.environ.get("SOKKAN_USAGE_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "usage.db")))
TZ = ZoneInfo(os.environ.get("SOKKAN_TZ", "Europe/Zurich"))

# USD / MTok (input, output) — grille API juin 2026. Match par préfixe, 1er gagnant.
# cache read = 0.1× input ; cache write = 1.25× (TTL 5m) / 2× (TTL 1h).
PRICES: list[tuple[str, tuple[float, float]]] = [
    ("claude-fable-5", (10.0, 50.0)),
    ("claude-mythos", (10.0, 50.0)),
    ("claude-opus-4-8", (5.0, 25.0)),
    ("claude-opus-4-7", (5.0, 25.0)),
    ("claude-opus-4-6", (5.0, 25.0)),
    ("claude-opus-4-5", (5.0, 25.0)),
    ("claude-opus", (15.0, 75.0)),   # 4.1 / 4.0 / 3 legacy
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku-4-5", (1.0, 5.0)),
    ("claude-haiku", (0.8, 4.0)),
    ("claude-3-5-haiku", (0.8, 4.0)),
]


def _price(model: str) -> tuple[float, float]:
    for prefix, p in PRICES:
        if model.startswith(prefix):
            return p
    return (5.0, 25.0)  # défaut opus courant


def _cost(model: str, usage: dict) -> float:
    pin, pout = _price(model)
    i = usage.get("input_tokens", 0) or 0
    o = usage.get("output_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cc = usage.get("cache_creation_input_tokens", 0) or 0
    # ventilation TTL du cache write si dispo (Claude Code utilise du 1h = 2×)
    det = usage.get("cache_creation") or {}
    c1h = det.get("ephemeral_1h_input_tokens", 0) or 0
    c5m = det.get("ephemeral_5m_input_tokens", 0) or 0
    if c1h + c5m == 0:
        c5m = cc
    return (
        i * pin + o * pout + cr * pin * 0.1 + c5m * pin * 1.25 + c1h * pin * 2.0
    ) / 1_000_000


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
            session_id TEXT, first_prompt TEXT, models TEXT,
            turns INTEGER, in_tokens INTEGER, out_tokens INTEGER,
            cache_read INTEGER, cache_write INTEGER, cost REAL,
            first_ts REAL, last_ts REAL
        );
        CREATE TABLE IF NOT EXISTS days (
            path TEXT, day TEXT, turns INTEGER,
            in_tokens INTEGER, out_tokens INTEGER, cost REAL,
            PRIMARY KEY (path, day)
        );
        """
    )
    return con


def _parse_file(path: Path) -> tuple[dict, dict[str, dict]]:
    """Agrège un transcript : totaux + ventilation par jour (Europe/Zurich)."""
    tot = {"turns": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0,
           "first_ts": None, "last_ts": None, "models": set(), "first_prompt": ""}
    days: dict[str, dict] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "user" and not tot["first_prompt"]:
                m = d.get("message", {})
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    # 1re ligne « humaine » (saute les caveats/reminders injectés)
                    for ln in c.strip().splitlines():
                        ln = ln.strip()
                        if ln and not ln.startswith("<") and not ln.startswith("Caveat:"):
                            tot["first_prompt"] = ln[:80]
                            break
            if t != "assistant":
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            if not u:
                continue
            model = msg.get("model", "")
            if model == "<synthetic>":  # placeholder d'erreur API, pas un tour facturé
                continue
            ts_raw = d.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                ts = path.stat().st_mtime
            cost = _cost(model, u)
            tot["turns"] += 1
            tot["in"] += u.get("input_tokens", 0) or 0
            tot["out"] += u.get("output_tokens", 0) or 0
            tot["cr"] += u.get("cache_read_input_tokens", 0) or 0
            tot["cw"] += u.get("cache_creation_input_tokens", 0) or 0
            tot["cost"] += cost
            tot["models"].add(model)
            tot["first_ts"] = ts if tot["first_ts"] is None else min(tot["first_ts"], ts)
            tot["last_ts"] = ts if tot["last_ts"] is None else max(tot["last_ts"], ts)
            day = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
            dd = days.setdefault(day, {"turns": 0, "in": 0, "out": 0, "cost": 0.0})
            dd["turns"] += 1
            dd["in"] += u.get("input_tokens", 0) or 0
            dd["out"] += u.get("output_tokens", 0) or 0
            dd["cost"] += cost
    return tot, days


def refresh() -> None:
    """Met à jour le cache pour les fichiers nouveaux/modifiés (incrémental)."""
    con = _con()
    known = {r["path"]: (r["mtime"], r["size"]) for r in con.execute("SELECT path, mtime, size FROM files")}
    live = set()
    for p in PROJECT_DIR.glob("*.jsonl"):
        st = p.stat()
        key = str(p)
        live.add(key)
        if known.get(key) == (st.st_mtime, st.st_size):
            continue
        tot, days = _parse_file(p)
        con.execute(
            "INSERT OR REPLACE INTO files(path, mtime, size, session_id, first_prompt,"
            " models, turns, in_tokens, out_tokens, cache_read, cache_write, cost,"
            " first_ts, last_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, st.st_mtime, st.st_size, p.stem, tot["first_prompt"],
             ",".join(sorted(tot["models"])), tot["turns"], tot["in"], tot["out"],
             tot["cr"], tot["cw"], tot["cost"], tot["first_ts"], tot["last_ts"]),
        )
        con.execute("DELETE FROM days WHERE path=?", (key,))
        con.executemany(
            "INSERT INTO days(path, day, turns, in_tokens, out_tokens, cost) VALUES(?,?,?,?,?,?)",
            [(key, day, v["turns"], v["in"], v["out"], v["cost"]) for day, v in days.items()],
        )
    # fichiers disparus
    for gone in set(known) - live:
        con.execute("DELETE FROM files WHERE path=?", (gone,))
        con.execute("DELETE FROM days WHERE path=?", (gone,))
    con.commit()
    con.close()


def summary(days_back: int = 30) -> dict:
    refresh()
    con = _con()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    start = (datetime.now(TZ) - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
    # série quotidienne (jours manquants remplis à 0 côté front)
    day_rows = [dict(r) for r in con.execute(
        "SELECT day, SUM(turns) turns, SUM(in_tokens) in_tokens,"
        " SUM(out_tokens) out_tokens, SUM(cost) cost"
        " FROM days WHERE day >= ? GROUP BY day ORDER BY day", (start,),
    )]
    def _tot(since: str) -> dict:
        r = con.execute(
            "SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(out_tokens),0) out,"
            " COALESCE(SUM(turns),0) turns FROM days WHERE day >= ?", (since,),
        ).fetchone()
        return {"cost": r["cost"], "out_tokens": r["out"], "turns": r["turns"]}
    week = (datetime.now(TZ) - timedelta(days=6)).strftime("%Y-%m-%d")
    totals = {
        "today": _tot(today), "7d": _tot(week), "30d": _tot(start),
        "all": dict(con.execute(
            "SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(out_tokens),0) out_tokens,"
            " COALESCE(SUM(turns),0) turns FROM files").fetchone()),
    }
    # top sessions (30j), titres résolus depuis le store SOKKAN si connu
    import board
    known = {}
    for s in board.list_sessions():
        if s.get("claude_session_id"):
            known[s["claude_session_id"]] = s
        known[s["session_id"]] = s
    cutoff = time.time() - days_back * 86400
    sessions = []
    for r in con.execute(
        "SELECT session_id, first_prompt, models, turns, in_tokens, out_tokens,"
        " cache_read, cost, last_ts FROM files WHERE last_ts >= ?"
        " ORDER BY cost DESC LIMIT 25", (cutoff,),
    ):
        s = known.get(r["session_id"])
        sessions.append({
            "session_id": r["session_id"],
            "title": (s or {}).get("title") or r["first_prompt"] or r["session_id"][:8],
            "tag": (s or {}).get("tag", ""),
            "models": r["models"], "turns": r["turns"],
            "in_tokens": r["in_tokens"], "out_tokens": r["out_tokens"],
            "cache_read": r["cache_read"], "cost": r["cost"], "last_ts": r["last_ts"],
        })
    # par modèle (approx. : coût total des fichiers mono-modèle + ventilation grossière sinon)
    by_model: dict[str, dict] = {}
    for r in con.execute("SELECT models, cost, out_tokens FROM files WHERE last_ts >= ?", (cutoff,)):
        key = r["models"] or "?"
        m = by_model.setdefault(key, {"cost": 0.0, "out_tokens": 0})
        m["cost"] += r["cost"]
        m["out_tokens"] += r["out_tokens"]
    con.close()
    return {
        "days": day_rows, "totals": totals, "sessions": sessions,
        "by_model": [{"model": k, **v} for k, v in sorted(by_model.items(), key=lambda x: -x[1]["cost"])],
        "note": "estimation grille API (input/output/cache read 0.1×/write 1.25–2×) — pas une facture",
    }
