#!/usr/bin/env python3
"""assistant.py — Nina, l'agente DevOps embarquée (S1).

Chat d'assistance produit dans le cockpit : persona DevOps qui connaît SOKKAN
par cœur (corpus `assistant_kb/*.md`, injecté ENTIER dans le prompt système —
à cette taille le RAG serait de l'overhead ; on indexera quand la KB grossira).

Frontière (spec docs/sokkan/spec-agente-devops.md, décision Nick 23-07) :
enforcement STRUCTUREL — ce module n'a aucun accès au workspace, aux fichiers,
à l'env des sessions ni au control plane. Ce qu'il ne reçoit pas, il ne peut
pas le divulguer. S1 = sans dossier client (S2 l'ajoutera via les endpoints
curés fleet/usage/llm.status).

LLM : SOKKAN_ASSISTANT_LLM_{URL,TOKEN,MODEL} si posés (service NINABOT, seedé
au provisioning cloud — le wallet du client n'est jamais débité), sinon
fallback sur la config LLM de l'instance (llm.py) pour le dev/test.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import httpx

import llm

KB_DIR = Path(__file__).parent / "assistant_kb"
DB = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))) / "assistant.db"
DAILY_LIMIT = int(os.environ.get("SOKKAN_ASSISTANT_DAILY_LIMIT", "50"))
MAX_TOKENS = 800
HISTORY_TURNS = 12  # messages (user+assistant confondus) rejoués au modèle
KB_CAP = 40_000     # garde-fou : au-delà on tronque (et il sera temps d'indexer)

PERSONA = """Tu es Nina, l'ingénieure DevOps embarquée dans le cockpit SOKKAN Cloud.
Tu connais le produit par cœur (la base de connaissance ci-dessous) et tu aides
l'utilisateur à réussir : importer son projet, semer sa mémoire, piloter ses
sessions, gérer sa flotte et ses coûts.

Règles absolues :
- Tu ne lis, ne cites et ne devines JAMAIS un secret : credentials, URI de
  connexion, variables d'environnement, clés. Si on te le demande, indique
  l'écran du cockpit où l'information se gère (ex. l'URI PostgreSQL se révèle
  dans Ma flotte, réservé admin).
- Tu n'as pas accès au code du client ni à ses machines — tu ne prétends
  jamais le contraire.
- Tu n'exécutes aucune action : tu expliques, tu guides, tu montres où cliquer.
- Ce que tu ne sais pas, tu ne l'inventes pas : tu proposes d'écrire à
  hello@sokkan.ch (le fondateur répond).
- Tu réponds dans la langue de l'utilisateur (FR/EN), ton direct et technique,
  chaleureux sans blabla. Réponses courtes d'abord, détail si on te le demande.
"""


# ---- base ----------------------------------------------------------------
def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT NOT NULL, ts REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_email, ts)")
    return con


def history(user_email: str, limit: int = 50) -> list[dict]:
    con = _con()
    rows = con.execute(
        "SELECT role, content, ts FROM messages WHERE user_email=? ORDER BY ts DESC LIMIT ?",
        (user_email, limit)).fetchall()
    con.close()
    return [{"role": r, "content": c, "ts": t} for r, c, t in reversed(rows)]


def _today_count(con: sqlite3.Connection, user_email: str) -> int:
    midnight = time.time() - (time.time() % 86400)
    return con.execute(
        "SELECT COUNT(*) FROM messages WHERE user_email=? AND role='user' AND ts>=?",
        (user_email, midnight)).fetchone()[0]


# ---- KB (corpus produit, cache par mtime) ---------------------------------
_kb_cache: tuple[float, str] | None = None


def _kb() -> str:
    global _kb_cache
    if not KB_DIR.is_dir():
        return ""
    mtime = max((p.stat().st_mtime for p in KB_DIR.glob("*.md")), default=0.0)
    if _kb_cache and _kb_cache[0] == mtime:
        return _kb_cache[1]
    parts = [p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(KB_DIR.glob("*.md"))]
    text = "\n\n---\n\n".join(parts)[:KB_CAP]
    _kb_cache = (mtime, text)
    return text


# ---- LLM -----------------------------------------------------------------
def _llm_config() -> dict | None:
    """Endpoint Anthropic-compatible : assistant dédié (NINABOT) sinon config
    instance. Retourne {url, token, model} ou None si rien de configurable."""
    url = os.environ.get("SOKKAN_ASSISTANT_LLM_URL", "")
    tok = os.environ.get("SOKKAN_ASSISTANT_LLM_TOKEN", "")
    if url and tok:
        return {"url": url.rstrip("/"), "token": tok,
                "model": os.environ.get("SOKKAN_ASSISTANT_LLM_MODEL", "qwen3-coder-plus")}
    c = llm.load()
    if c.get("mode") == "included" and c.get("base_url") and c.get("auth_token"):
        return {"url": c["base_url"].rstrip("/"), "token": c["auth_token"],
                "model": c.get("model") or "qwen3-coder-plus"}
    if c.get("mode") == "byok" and c.get("anthropic_api_key"):
        return {"url": "https://api.anthropic.com", "token": c["anthropic_api_key"],
                "model": "claude-haiku-4-5-20251001"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"url": "https://api.anthropic.com", "token": os.environ["ANTHROPIC_API_KEY"],
                "model": "claude-haiku-4-5-20251001"}
    return None


def configured() -> bool:
    return _llm_config() is not None and bool(_kb())


def chat(user_email: str, message: str) -> dict:
    """Un tour de chat. Retourne {reply} ou lève ValueError (limite/config)."""
    message = (message or "").strip()
    if not message:
        raise ValueError("message vide")
    if len(message) > 4000:
        raise ValueError("message trop long (4000 caractères max)")
    cfg = _llm_config()
    if not cfg:
        raise ValueError("assistant non configuré sur cette instance")

    con = _con()
    if _today_count(con, user_email) >= DAILY_LIMIT:
        con.close()
        raise ValueError("limite quotidienne atteinte — réessayez demain, "
                         "ou écrivez à hello@sokkan.ch")

    past = history(user_email, HISTORY_TURNS)
    msgs = [{"role": m["role"], "content": m["content"]} for m in past]
    msgs.append({"role": "user", "content": message})
    system = f"{PERSONA}\n\n=== BASE DE CONNAISSANCE PRODUIT ===\n\n{_kb()}"

    r = httpx.post(
        f"{cfg['url']}/v1/messages",
        headers={"x-api-key": cfg["token"], "anthropic-version": "2023-06-01",
                 "x-sokkan-user": f"assistant:{user_email}"},
        json={"model": cfg["model"], "max_tokens": MAX_TOKENS,
              "system": system, "messages": msgs},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    reply = "".join(b.get("text", "") for b in data.get("content", [])
                    if b.get("type") == "text").strip() or "(réponse vide)"

    now = time.time()
    con.execute("INSERT INTO messages(user_email, role, content, ts) VALUES(?,?,?,?)",
                (user_email, "user", message, now))
    con.execute("INSERT INTO messages(user_email, role, content, ts) VALUES(?,?,?,?)",
                (user_email, "assistant", reply, now + 0.001))
    con.commit()
    con.close()
    return {"reply": reply}
