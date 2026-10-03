"""Bridge between the SOKKAN 2.x MCP server and the 3.0 memory store (CortHeXis core).

``memory_search_server.py`` asks ``enabled()`` which index serves: the store when
``CORTHEXIS_MEMORY_BACKEND=postgres``; with ``auto`` (the 3.0 default) the 2.x
``memory.db`` keeps serving, read-only, until the migration (``memory_migration.py``,
``core/migrate.py``) has built and checked generation 1, then the store; ``sqlite`` = 2.x.

Results keep the 2.x shape (note_name, description, score, cosine, snippet, path,
priority, degraded) and gain the 3.0 fields: age_days, date_source, modified,
generation, lexical, rerank.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Callable

_lock = threading.Lock()
_store = None


def configured() -> str:
    """sqlite (2.x), postgres (3.0 store only) or auto (3.0: the 2.x index serves until
    the migration to the store is complete, then the store — no gap in between)."""
    v = (os.environ.get("CORTHEXIS_MEMORY_BACKEND") or os.environ.get("SOKKAN_MEMORY_BACKEND")
         or "").strip().lower()
    if v in ("postgres", "postgresql", "pg"):
        return "postgres"
    return "auto" if v == "auto" else "sqlite"


def migration_dir() -> str:
    return os.environ.get("CORTHEXIS_MIGRATION_DIR") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "memory-migration")


_state_cache: dict = {}
_legacy = None


def migration_status() -> str | None:
    """Status in the migration state file (core.migrate), cached on its mtime."""
    path = os.path.join(migration_dir(), "state.json")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    if _state_cache.get("mtime") != mtime:
        try:
            with open(path, encoding="utf-8") as fh:
                _state_cache.update(mtime=mtime, status=json.load(fh).get("status"))
        except (OSError, ValueError):
            return None
    return _state_cache.get("status")


def enabled() -> bool:
    """True = searches read the 3.0 store; False = the SQLite memory.db (2.x path)."""
    mode = configured()
    if mode == "auto":
        return migration_status() in ("done", "not-needed")
    return mode == "postgres"


def embed_query(text: str, legacy_db: str | os.PathLike | None = None) -> list[float]:
    """Query vector for the index that serves: the 3.0 engine for the store, the 2.x
    embedder (core.embed.legacy) for memory.db. A model that is not the one the index was
    built with raises, and the caller degrades to lexical-only instead of comparing
    vectors from two different spaces."""
    from core import embed

    if enabled():
        e = embed.get()
        active = get_store().active_generation()
        if active is not None and active.embed_identity != e.identity():
            raise RuntimeError(f"the index was built with {active.embed_identity}, the "
                               f"configured model is {e.identity()} (re-index pending)")
        return e.embed_query(text)
    global _legacy
    if _legacy is None:
        _legacy = embed.legacy()     # fastembed keeps its model loaded: build it once
    e = _legacy
    built_with = _legacy_model(legacy_db)
    if built_with and built_with != e.identity_2x():
        raise RuntimeError(f"memory.db was built with {built_with}, the 2.x embedder is "
                           f"{e.identity_2x()}")
    return e.embed_query(text)


def _legacy_model(db) -> str | None:
    if not db or not os.path.exists(db):
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


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
