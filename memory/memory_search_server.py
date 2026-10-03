#!/usr/bin/env python3
"""memory_search_server.py — SOKKAN P0 : MCP stdio server for semantic memory recall.

Exposes to any Claude Code session over MCP stdio:

  • memory_search(query, top_k=8) → ranked notes (best chunk per note) with score+snippet
  • memory_get(note_name)         → the full body of one note
  • memory_links(note_name)       → outgoing [[wikilinks]] + backlinks
  • memory_write(name, …)         → create/update a note (the only write path)

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
import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embeddings
import store_backend  # 3.0 store, behind CORTHEXIS_MEMORY_BACKEND=postgres

DB_PATH = Path(os.environ.get("SOKKAN_MEMORY_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "memory.db")))
# même défaut que memory/index_memory.py — les notes sont la source, la DB est dérivée
MEMORY_DIR = Path(os.environ.get("SOKKAN_MEMORY_DIR", os.path.expanduser("~/.sokkan/memory")))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

mcp = FastMCP("sokkan-memory")


def _embed_query(text: str) -> list[float]:
    # 2.x path: the 2.x model, checked against the one memory.db was built with
    return store_backend.legacy_embed_query(text, DB_PATH)


def _load_chunks() -> list[tuple[str, str, str, str, list[float], int]]:
    """(note_name, description, source_path, body, embedding, priority) for every chunk."""
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        try:
            rows = con.execute(
                "SELECT c.note_name, n.description, n.source_path, c.body, c.embedding, "
                "COALESCE(n.priority, 0) FROM chunks c JOIN notes n ON n.name = c.note_name"
            ).fetchall()
        except sqlite3.OperationalError:  # DB pré-migration sans colonne priority
            rows = [(*r, 0) for r in con.execute(
                "SELECT c.note_name, n.description, n.source_path, c.body, c.embedding "
                "FROM chunks c JOIN notes n ON n.name = c.note_name"
            ).fetchall()]
    finally:
        con.close()
    return [(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) for r in rows]


# weight of the lexical (keyword-overlap) signal in the final blend; the dense
# cosine carries the rest. Tuned so exact jargon hits ("promo", "jobup") surface
# without drowning the semantic signal on keyword-free queries.
LEXICAL_WEIGHT = 0.25
# relative boost for notes marked `priority: high` — MULTIPLICATIVE (score
# *= 1+boost): a weak match stays weak (no flat +0.08 dominating low-cosine
# queries), a strong match gets a real nudge. Tunable per install.
PRIORITY_BOOST = float(os.environ.get("SOKKAN_PRIORITY_BOOST", "0.08"))
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
    if store_backend.enabled():
        return store_backend.memory_search(query, top_k, None, "embedding backend")
    if not _load_chunks():
        return [{"info": "No project memory yet. Write notes as markdown files in the workspace "
                 "memory directory (one fact per file, with a description: frontmatter) and they "
                 "become searchable within ~2 minutes.", "empty": True}]
    qtok = _tokens(query)
    degraded = None
    try:
        q = _embed_query(query)
    except Exception as e:  # noqa: BLE001 — degrade to lexical-only instead of failing
        if not qtok:
            return [{"error": f"embedding backend unavailable ({embeddings.backend()}): {e}"}]
        q = None
        degraded = f"embedding backend unavailable ({embeddings.backend()}) — lexical-only scoring, degraded recall"
    return rank_2x(query, q, top_k, degraded)


def rank_2x(query: str, q: list[float] | None, top_k: int = 8,
            degraded: str | None = None) -> list[dict]:
    """The 2.x ranking over memory.db (also used by the recall during the migration)."""
    chunks = _load_chunks()
    qtok = _tokens(query)
    if q is None and not qtok:
        return []
    # aggregate per note: best chunk (for snippet) + full-note lexical haystack;
    # chunk relevance = cosine, or keyword overlap in degraded lexical-only mode
    agg: dict[str, dict] = {}
    for note_name, description, source_path, body, emb, priority in chunks:
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
                "priority": priority,
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
        if a["priority"]:
            score *= 1 + PRIORITY_BOOST
        snippet = a["snippet"] if len(a["snippet"]) <= 320 else a["snippet"][:317] + "…"
        results.append({
            "note_name": a["note_name"],
            "description": a["description"],
            "score": round(score, 4),
            "cosine": round(a["rel"], 4) if q is not None else None,
            "snippet": snippet,
            "path": a["path"],
            **({"priority": True} if a["priority"] else {}),
            **({"degraded": degraded} if degraded else {}),
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results[: max(1, top_k)]


@mcp.tool()
def memory_get(note_name: str) -> str:
    """Retourne le corps complet d'une note mémoire par son nom (sans .md)."""
    if store_backend.enabled():
        body = store_backend.memory_get(note_name)
        return body if body is not None else f"note not found: {note_name}"
    if not DB_PATH.exists():
        return f"memory index not found: {DB_PATH}"
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
        return f"note not found: {note_name}"
    return "\n\n".join(r[0] for r in rows)


@mcp.tool()
def memory_links(note_name: str) -> dict:
    """Navigation du graphe memoire : liens sortants ([[wikilinks]] de la note)
    et entrants (notes qui la citent), avec leurs descriptions. Permet a une
    session de suivre le graphe sans relire les fichiers."""
    if store_backend.enabled():
        return store_backend.memory_links(note_name)
    if not DB_PATH.exists():
        return {"error": f"memory index not found: {DB_PATH}"}
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        try:
            out_rows = con.execute(
                "SELECT l.dst AS name, n.description FROM links l "
                "LEFT JOIN notes n ON n.name = l.dst WHERE l.src = ? ORDER BY l.dst",
                (note_name,)).fetchall()
            in_rows = con.execute(
                "SELECT l.src AS name, n.description FROM links l "
                "JOIN notes n ON n.name = l.src WHERE l.dst = ? ORDER BY l.src",
                (note_name,)).fetchall()
        except sqlite3.OperationalError:
            return {"note": note_name, "links": [], "backlinks": [],
                    "info": "links table absent - reindex the memory (pre-2.0 DB)"}
        exists = {r[0] for r in con.execute("SELECT name FROM notes")}
    finally:
        con.close()
    if note_name not in exists:
        return {"error": f"note not found: {note_name}"}
    return {
        "note": note_name,
        "links": [{"name": r["name"], "description": r["description"] or "",
                   "exists": r["name"] in exists} for r in out_rows],
        "backlinks": [{"name": r["name"], "description": r["description"] or ""}
                      for r in in_rows],
    }


@mcp.tool()
def memory_write(name: str, description: str, body: str,
                 priority: bool = False, type: str = "project",
                 overwrite: bool = False) -> dict:
    """Écrit une note mémoire — LE chemin d'écriture de la mémoire projet.

    Une note = UN fait durable. `name` = slug kebab-case sans .md (il devient le
    nom du fichier ET la cible des [[wikilinks]]). `description` = la ligne de
    l'index et le levier de recall : la soigner. `priority=True` épingle la note
    (★) et la remonte dans le rappel automatique. `overwrite=False` refuse
    d'écraser une note existante (relire d'abord avec memory_get).

    L'index et les embeddings suivent tout seuls (réindexation du backend).
    """
    name = (name or "").strip().removesuffix(".md")
    if not NAME_RE.match(name):
        return {"ok": False, "error": "invalid name: lowercase kebab-case slug, "
                                      "2-64 chars, no path separator (e.g. 'decision-delete-404')"}
    if not (description or "").strip():
        return {"ok": False, "error": "description is required — it is the index line "
                                      "and the main recall lever"}
    if not (body or "").strip():
        return {"ok": False, "error": "body is required — one durable fact, with [[links]] "
                                      "to the related notes"}
    path = MEMORY_DIR / f"{name}.md"
    if path.exists() and not overwrite:
        return {"ok": False, "error": f"note already exists: {name} — read it with memory_get, "
                                      "then call again with overwrite=true to replace it",
                "path": str(path)}
    fm = [
        "---",
        f"name: {name}",
        # json.dumps = scalaire YAML double-quoted valide (guillemets, ':' , '#'…) ;
        # ensure_ascii=False pour garder les accents lisibles dans le fichier
        f"description: {json.dumps(' '.join(description.split()), ensure_ascii=False)}",
    ]
    if priority:
        fm.append("priority: high")
    fm += ["metadata:", f"  type: {type or 'project'}", "---", ""]
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(fm) + body.strip() + "\n", encoding="utf-8")
        tmp.replace(path)  # atomique : jamais de note à moitié écrite pour l'indexeur
    except OSError as e:
        return {"ok": False, "error": f"cannot write {path}: {e}",
                "hint": f"the memory directory must be writable by uid {os.getuid()} "
                        "(SOKKAN_MEMORY_DIR)"}
    return {"ok": True, "note": name, "path": str(path), "updated": overwrite,
            "indexed": "the backend reindexes changed notes within ~2 min "
                       "(memory_search/memory_get see it after that)"}


if __name__ == "__main__":
    mcp.run()
