"""Bridge between SOKKAN and the 3.0 memory store (CortHeXis core).

``CORTHEXIS_MEMORY_BACKEND`` picks the index that serves ``memory_search`` /
``memory_get`` / ``memory_links``, the CortHeXis tab and the recall hooks:

* ``auto`` (the 3.0 default, also when unset and a database is configured): a 2.x
  install is migrated at the first start (``memory_migration.py``, ``core/migrate.py``)
  and its ``memory.db`` keeps serving, read-only, with the 2.x model until generation 1
  is built and checked; then the store, filled by ``core.indexer.IndexRunner``. A new
  install (no ``memory.db``) goes straight to the store.
* ``postgres``: the store only (no migration). ``sqlite``: the 2.x index.

Queries are embedded by ``core.embed`` (the configured profile); while the active index
generation is still the 2.x one (migration in progress) they are embedded by the 2.x
model instead, so the vectors always match the index they are compared with.

Reranker (``core.embed.rerank_policy()``): ``interactive`` (GPU) → top 10 of every
search; ``async`` (standard, ~4.5 s on CPU) → only for a deep search asked explicitly;
``off`` (light) → never. ``CORTHEXIS_SEARCH_RERANK=1|0`` forces it on or off.

Results keep the 2.x shape (note_name, description, score, cosine, snippet, path,
priority, degraded) and gain the 3.0 fields: age_days, date_source, modified,
generation, lexical, rerank.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

_lock = threading.Lock()
_store = None
_qemb: tuple[float, int | None, object] | None = None   # (checked at, generation, embedder)


def _env(name: str) -> str:
    return (os.environ.get("CORTHEXIS_" + name) or os.environ.get("SOKKAN_" + name) or "").strip()


def database_configured() -> bool:
    return bool(_env("DATABASE_URL"))


def configured() -> str:
    """postgres | sqlite | auto (see the module docstring)."""
    v = _env("MEMORY_BACKEND").lower()
    if v in ("postgres", "postgresql", "pg"):
        return "postgres"
    if v in ("sqlite", "legacy", "2.x"):
        return "sqlite"
    return "auto" if v == "auto" or database_configured() else "sqlite"


def legacy_db() -> Path:
    """The 2.x index (SOKKAN_MEMORY_DB, else $SOKKAN_DATA_DIR/memory.db)."""
    return Path(os.environ.get("SOKKAN_MEMORY_DB") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "memory.db"))


def migration_dir() -> str:
    return os.environ.get("CORTHEXIS_MIGRATION_DIR") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "memory-migration")


_state_cache: dict = {}


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


def migrating() -> bool:
    """auto mode, a 2.x memory.db, and the migration has not switched yet."""
    if configured() != "auto":
        return False
    status = migration_status()
    if status in ("done", "not-needed"):
        return False
    return status is not None or legacy_db().is_file()


def enabled() -> bool:
    """True = the 3.0 store serves; False = the 2.x memory.db (sqlite mode, or auto mode
    during the migration)."""
    mode = configured()
    if mode == "auto":
        return not migrating()
    return mode == "postgres"


def get_store():
    """One Store (connection pool) per process, opened on first use."""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                from core.store import SearchConfig, Store
                # SOKKAN 2.x boosted `priority: high` notes by 8 %: keep it unless configured
                boost = _env("PRIORITY_BOOST") or "0.08"
                _store = Store(config=SearchConfig.from_env(priority_boost=float(boost)),
                               max_size=int(os.environ.get("CORTHEXIS_DB_POOL", "4")))
    return _store


def reset() -> None:
    """Forget the store and the query embedder (tests, configuration change)."""
    global _store, _qemb
    with _lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:  # noqa: BLE001
                pass
        _store, _qemb = None, None


class NoServingEmbedder:
    """Stand-in when no embedder matches the active generation (model change pending):
    queries fail → lexical-only search, flagged — never vectors of another model."""
    backend = "none"
    rerank_policy = "off"
    rerank_url = None
    lexical_weight = None

    def __init__(self, identity: str):
        self._identity = identity

    def identity(self) -> str:
        return f"none (index built with {self._identity})"

    def embed_query(self, text: str, timeout: float = 30.0):
        raise RuntimeError(f"no embedding server serves the active index ({self._identity})")

    def rerank(self, query, docs, timeout: float = 3.0):
        return None


def serving_embedder(store=None):
    """Embedder whose identity matches the active generation, as the memory profile switch
    recorded it (``core.switch.serving_embedder``: the profile chosen in the UI, not a
    frozen configuration). Without the switch module: the configured embedder, or the 2.x
    one while a 2.x generation is active. None = no generation yet."""
    from core import embed

    store = store or get_store()
    g = store.active_generation()
    if g is None:
        return None
    try:
        from core import switch
    except ImportError:
        switch = None
    if switch is not None:
        e = switch.serving_embedder(store)
        if e is not None:
            return e
    else:
        e = embed.get()
        if e.identity() == g.embed_identity:
            return e
    leg = embed.legacy()
    if g.embed_identity in (leg.identity(), leg.identity_2x()):
        return leg
    return NoServingEmbedder(g.embed_identity)


def index_embedder(store=None):
    """What the indexer writes with: the serving embedder of the active generation, or the
    configured one for the very first generation (new install)."""
    from core import embed

    e = serving_embedder(store)
    return embed.get() if e is None else (None if isinstance(e, NoServingEmbedder) else e)


def embedder():
    """Query embedder (see ``serving_embedder``), re-checked every 10 s: a switch or an
    activation happens in another thread or process."""
    global _qemb
    from core import embed

    now = time.monotonic()
    if _qemb is not None and now - _qemb[0] < 10:
        return _qemb[2]
    gen = None
    try:
        st = get_store()
        g = st.active_generation()
        gen = g.id if g else None
        e = serving_embedder(st) or embed.get()
    except Exception:  # noqa: BLE001 — store down: the search reports it
        e = embed.get()
    _qemb = (now, gen, e)
    return e


def embed_query(text: str, timeout: float = 30.0) -> list[float]:
    return embedder().embed_query(text, timeout=timeout)


def rerank_policy() -> str:
    try:
        return getattr(embedder(), "rerank_policy", "off") or "off"
    except Exception:  # noqa: BLE001
        return "off"


def _reranker(deep: bool = False) -> Callable | None:
    forced = _env("SEARCH_RERANK").lower()
    if forced in ("0", "false", "no", "off"):
        return None
    policy = rerank_policy()
    if forced in ("1", "true", "yes", "on") or policy == "interactive" or (
            deep and policy == "async"):
        e = embedder()
        timeout = 3.0 if policy == "interactive" else 15.0
        return lambda q, docs: e.rerank(q, docs, timeout=timeout)
    return None


def memory_search(query: str, top_k: int,
                  embed_query_fn: Callable[[str], list[float]] | None = None,
                  backend_label: str = "embedding backend", *, deep: bool = False) -> list[dict]:
    from core.search import tokens
    from core.store import DimensionMismatch, StoreError

    try:
        st = get_store()
    except Exception as e:  # noqa: BLE001 — database down
        return [{"error": f"memory store unavailable: {e}"}]
    degraded = None
    try:
        q = (embed_query_fn or embed_query)(query)
    except Exception as e:  # noqa: BLE001 — degrade to lexical-only instead of failing
        q = None
        degraded = f"{backend_label} unavailable — lexical-only scoring, degraded recall ({e})"
    if q is None and not tokens(query):
        return [{"error": f"{degraded}; the query has no usable keyword"}]
    try:
        hits = st.search(q, query, max(1, top_k),
                         rerank=_reranker(deep) if q is not None else None)
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


def age_header(name: str, modified, source) -> str:
    """``[note x — updated 2026-09-01, 32 d ago]`` (+ provenance when reconstructed)."""
    from core.contract import MEASURED_SOURCES
    from core.search import age

    mod, days, src = age(modified, source)
    if days is None:
        return f"[note {name} — last update date unknown]"
    head = f"[note {name} — updated {mod}, {days} d ago"
    if src not in MEASURED_SOURCES:
        head += f"; reconstructed date ({src}), exact day not guaranteed"
    return head + "]"


def memory_get(note_name: str) -> str | None:
    """Full note body prefixed with its age and date provenance; None = not in the store."""
    st = get_store()
    name = note_name if st.get_note(note_name) else st.find_note_by_path(note_name)
    note = st.get_note(name) if name else None
    if note is None:
        return None
    return age_header(note.name, note.modified, note.modified_source) + "\n\n" + (note.body or "")


def memory_links(note_name: str) -> dict:
    st = get_store()
    if st.get_note(note_name) is None:
        return {"error": f"note not found: {note_name}"}
    out = st.links(note_name)
    out["links"] = [{"name": r["name"], "description": r["description"] or "",
                     "exists": bool(r["exists"])} for r in out["links"]]
    out["backlinks"] = [{"name": r["name"], "description": r["description"] or ""}
                        for r in out["backlinks"]]
    return out


def log_spawn_recall(session_id: str, hits: list[dict], query: str) -> None:
    """Record the notes pre-injected at spawn (channel ``spawn``): the per-turn recall
    then does not inject them again in the same session."""
    from core.search import Hit

    rows = [Hit(note_name=h["note_name"], score=float(h.get("score") or 0),
                cosine=h.get("cosine"), lexical=float(h.get("lexical") or 0),
                rerank=h.get("rerank"), snippet="", age_days=h.get("age_days"),
                date_source=h.get("date_source") or "", generation=h.get("generation"))
            for h in hits if h.get("note_name")]
    if rows:
        get_store().log_recall("spawn", rows, session_id=session_id, query=query[:2000])


# --------------------------------------------------------------------------- 2.x memory.db
# Serves searches and the per-turn recall while the migration re-encodes the corpus.
_legacy = None


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


class LegacyQueryEmbedder:
    """The 2.x embedder (core.embed.legacy, built once: fastembed keeps its model loaded),
    refused when it is not the model memory.db was built with — the caller then degrades
    to lexical-only instead of comparing vectors of two different spaces."""
    rerank_policy = "off"
    rerank_url = None

    def __init__(self, db=None):
        global _legacy
        from core import embed

        if _legacy is None:
            _legacy = embed.legacy()
        self.inner, self.db = _legacy, db or legacy_db()
        self.lexical_weight = getattr(_legacy, "lexical_weight", None)

    def identity(self) -> str:
        return self.inner.identity()

    def embed_query(self, text: str, timeout: float = 30.0) -> list[float]:
        built_with = _legacy_model(self.db)
        if built_with and built_with != self.inner.identity_2x():
            raise RuntimeError(f"memory.db was built with {built_with}, the 2.x embedder is "
                               f"{self.inner.identity_2x()}")
        return self.inner.embed_query(text, timeout)

    def rerank(self, query, docs, timeout: float = 3.0):
        return None


def legacy_embed_query(text: str, db=None) -> list[float]:
    return LegacyQueryEmbedder(db).embed_query(text)


class LegacyIndex:
    """memory.db seen through the few store methods core.recall uses (active_generation,
    search): the per-turn recall keeps working during the migration window. Ranking =
    the 2.x one (memory_search_server); generation id 0 = "the 2.x index"."""

    def __init__(self, db=None):
        self.db = db or legacy_db()

    def active_generation(self):
        from core.contract import Generation

        if not Path(self.db).is_file():
            return None
        return Generation(id=0, embed_identity=LegacyQueryEmbedder(self.db).identity(),
                          dim=0, created_at="", status="active")

    def search(self, query_vec, query_text, k=8, *, rerank=None, **_kw):
        from core.search import Hit

        import memory_search_server as mss  # lazy: it imports this module

        out = []
        for r in mss.rank_2x(query_text, query_vec, k):
            if "note_name" not in r:
                continue
            out.append(Hit(note_name=r["note_name"], score=r["score"], cosine=r.get("cosine"),
                           lexical=0.0, rerank=None, snippet=r.get("snippet") or "",
                           age_days=None, date_source="inconnue",
                           description=r.get("description") or "",
                           source_path=r.get("path"), priority=int(bool(r.get("priority"))),
                           generation=0, degraded=r.get("degraded")))
        return out
