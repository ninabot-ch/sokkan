"""Bridge between the SOKKAN 2.x MCP server and the 3.0 memory store (CortHeXis core).

``memory_search_server.py`` calls this module when ``CORTHEXIS_MEMORY_BACKEND=postgres``
(or ``SOKKAN_MEMORY_BACKEND``); otherwise nothing here is imported and the SQLite path
is unchanged. Wave 2 only has to fill the store and flip the flag.

Results keep the 2.x shape (note_name, description, score, cosine, snippet, path,
priority, degraded) and gain the 3.0 fields: age_days, date_source, modified,
generation, lexical, rerank.
"""
from __future__ import annotations

import os
import threading
from typing import Callable

_lock = threading.Lock()
_store = None


def enabled() -> bool:
    v = os.environ.get("CORTHEXIS_MEMORY_BACKEND") or os.environ.get("SOKKAN_MEMORY_BACKEND")
    return (v or "").strip().lower() in ("postgres", "postgresql", "pg")


def get_store():
    """One Store (connection pool) per process, opened on first use."""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                from core.store import SearchConfig, Store
                # SOKKAN 2.x boosted `priority: high` notes by 8 %: keep it unless configured
                boost = os.environ.get("CORTHEXIS_PRIORITY_BOOST") or \
                    os.environ.get("SOKKAN_PRIORITY_BOOST") or "0.08"
                _store = Store(config=SearchConfig.from_env(priority_boost=float(boost)),
                               max_size=int(os.environ.get("CORTHEXIS_DB_POOL", "4")))
    return _store


def _reranker() -> Callable | None:
    """The embed chantier's reranker when present and enabled (CORTHEXIS_SEARCH_RERANK=1)."""
    if (os.environ.get("CORTHEXIS_SEARCH_RERANK") or "0") not in ("1", "true", "yes"):
        return None
    try:
        from core import embed  # noqa: PLC0415 — optional module (embed chantier)
    except ImportError:
        return None
    return getattr(embed, "rerank", None)


def memory_search(query: str, top_k: int, embed_query: Callable[[str], list[float]],
                  backend_label: str = "embedding backend") -> list[dict]:
    from core.store import DimensionMismatch, StoreError
    from core.search import tokens

    st = get_store()
    degraded = None
    try:
        q = embed_query(query)
    except Exception as e:  # noqa: BLE001 — degrade to lexical-only instead of failing
        q = None
        degraded = f"{backend_label} unavailable — lexical-only scoring, degraded recall ({e})"
    if q is None and not tokens(query):
        return [{"error": f"{degraded}; the query has no usable keyword"}]
    try:
        hits = st.search(q, query, max(1, top_k), rerank=_reranker())
    except DimensionMismatch as e:
        # the query was embedded by another model than the active index generation
        degraded = f"query embedding does not match the index ({e}) — lexical-only scoring"
        if not tokens(query):
            return [{"error": degraded}]
        hits = st.search(None, query, max(1, top_k))
    except StoreError as e:
        return [{"error": str(e)}]
    if not hits:
        return [{"info": "No project memory yet (the 3.0 index is empty or has no active "
                 "generation).", "empty": True}]
    out = []
    for h in hits:
        d = h.as_dict()
        if degraded:
            d["degraded"] = degraded
        out.append(d)
    return out


def memory_get(note_name: str) -> str | None:
    """Full note body prefixed with its age and date provenance; None = not in the store."""
    from core.search import age

    st = get_store()
    name = note_name if st.get_note(note_name) else st.find_note_by_path(note_name)
    note = st.get_note(name) if name else None
    if note is None:
        return None
    mod, days, src = age(note.modified, note.modified_source)
    if days is None:
        head = f"[note {note.name} — last update date unknown]"
    else:
        head = f"[note {note.name} — updated {mod}, {days} d ago"
        if src != "frontmatter":
            head += f"; reconstructed date ({src}), exact day not guaranteed"
        head += "]"
    return head + "\n\n" + (note.body or "")
