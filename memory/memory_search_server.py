#!/usr/bin/env python3
"""memory_search_server.py — SOKKAN P0 : MCP stdio server for semantic memory recall.

Exposes two tools to any Claude Code session over MCP stdio:

  • memory_search(query, top_k=8) → ranked notes (best chunk per note) with score+snippet
  • memory_get(note_name)         → the full body of one note

Retrieval = cosine over unit-normalized embeddings (dot product) computed by
``index_memory.py`` and stored in the local SQLite DB. The query is embedded at
call time via the same multilingual model (POST {ML_SERVICE_URL}/api/v1/embed/text),
so recall is cross-lingual. The corpus is tiny (~hundreds of chunks) → brute-force
scoring in pure Python is sub-millisecond, no numpy / vector DB needed.

This is the foundation of SOKKAN's "memory moat" and powers the Mémoire/KB tab
later — but it's useful standalone TODAY in every Claude Code session: replaces
loading the oversized MEMORY.md index by retrieving only the relevant notes.

Run by Claude Code via .mcp.json; not meant to be launched by hand.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embeddings

DB_PATH = Path(os.environ.get("SOKKAN_MEMORY_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "memory.db")))

mcp = FastMCP("sokkan-memory")


def _embed_query(text: str) -> list[float]:
    return embeddings.embed_query(text)


def _load_chunks() -> list[tuple[str, str, str, str, list[float]]]:
    """(note_name, description, source_path, body, embedding) for every chunk."""
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT c.note_name, n.description, n.source_path, c.body, c.embedding "
            "FROM chunks c JOIN notes n ON n.name = c.note_name"
        ).fetchall()
    finally:
        con.close()
    return [(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in rows]


# weight of the lexical (keyword-overlap) signal in the final blend; the dense
# cosine carries the rest. Tuned so exact jargon hits ("promo", "jobup") surface
# without drowning the semantic signal on keyword-free queries.
LEXICAL_WEIGHT = 0.25
_STOP = {
    "les", "des", "sur", "de", "la", "le", "du", "un", "une", "pour", "dans",
    "avec", "et", "the", "to", "and", "of", "on", "in", "how", "que", "qui",
}


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    cur = ""
    for ch in text.lower():
        if ch.isalnum():
            cur += ch
        else:
            if len(cur) >= 3 and cur not in _STOP:
                out.add(cur)
            cur = ""
    if len(cur) >= 3 and cur not in _STOP:
        out.add(cur)
    return out


@mcp.tool()
def memory_search(query: str, top_k: int = 8) -> list[dict]:
    """Recherche sémantique dans la mémoire SOKKAN (notes Claude Code).

    Retourne les notes les plus pertinentes avec score [0..1] et un extrait.
    Score = blend cosine dense (cross-lingual) + recouvrement lexical des mots-clés
    de la requête. Utiliser au début d'une tâche pour charger le contexte pertinent
    au lieu de lire tout l'index MEMORY.md.

    Args:
        query: la question / le sujet de travail (n'importe quelle langue).
        top_k: nombre de notes à retourner (défaut 8).
    """
    chunks = _load_chunks()
    if not chunks:
        return [{"error": "index vide ou introuvable — lancer index_memory.py", "db": str(DB_PATH)}]
    qtok = _tokens(query)
    degraded = None
    try:
        q = _embed_query(query)
    except Exception as e:  # noqa: BLE001 — degrade to lexical-only instead of failing
        if not qtok:
            return [{"error": f"embedding backend unavailable ({embeddings.backend()}): {e}"}]
        q = None
        degraded = f"embedding backend unavailable ({embeddings.backend()}) — lexical-only scoring, degraded recall"
    # aggregate per note: best chunk (for snippet) + full-note lexical haystack;
    # chunk relevance = cosine, or keyword overlap in degraded lexical-only mode
    agg: dict[str, dict] = {}
    for note_name, description, source_path, body, emb in chunks:
        if q is not None:
            rel = sum(a * b for a, b in zip(q, emb))
        else:
            rel = len(qtok & _tokens(body)) / len(qtok)
        a = agg.get(note_name)
        if a is None:
            a = agg[note_name] = {
                "note_name": note_name,
                "description": description,
                "path": Path(source_path).name,
                "rel": rel,
                "snippet": body,
                "hay": f"{note_name} {description} {body}",
            }
        else:
            a["hay"] += " " + body
            if rel > a["rel"]:
                a["rel"], a["snippet"] = rel, body

    results = []
    for a in agg.values():
        lex = (len(qtok & _tokens(a["hay"])) / len(qtok)) if qtok else 0.0
        if q is None:
            # note-wide overlap + chunk concentration (breaks ties between notes
            # that all contain every keyword somewhere)
            score = 0.7 * lex + 0.3 * a["rel"]
        else:
            score = (1 - LEXICAL_WEIGHT) * a["rel"] + LEXICAL_WEIGHT * lex
        snippet = a["snippet"] if len(a["snippet"]) <= 320 else a["snippet"][:317] + "…"
        results.append({
            "note_name": a["note_name"],
            "description": a["description"],
            "score": round(score, 4),
            "cosine": round(a["rel"], 4) if q is not None else None,
            "snippet": snippet,
            "path": a["path"],
            **({"degraded": degraded} if degraded else {}),
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results[: max(1, top_k)]


@mcp.tool()
def memory_get(note_name: str) -> str:
    """Retourne le corps complet d'une note mémoire par son nom (sans .md)."""
    if not DB_PATH.exists():
        return f"index introuvable: {DB_PATH}"
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT body FROM chunks WHERE note_name = ? ORDER BY chunk_idx",
            (note_name,),
        ).fetchall()
        if not rows:
            # fallback: resolve by filename stem (frontmatter name may differ)
            rows = con.execute(
                "SELECT c.body FROM chunks c JOIN notes n ON n.name = c.note_name "
                "WHERE n.source_path LIKE ? ORDER BY c.chunk_idx",
                (f"%/{note_name.removesuffix('.md')}.md",),
            ).fetchall()
    finally:
        con.close()
    if not rows:
        return f"note introuvable: {note_name}"
    return "\n\n".join(r[0] for r in rows)


if __name__ == "__main__":
    mcp.run()
