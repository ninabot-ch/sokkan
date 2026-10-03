"""SOKKAN side of the 2.x → 3.0 memory migration (the engine is ``core/migrate.py``).

At the first start of 3.0 (``CORTHEXIS_MEMORY_BACKEND=auto``, the compose default) the
backend runs the migration in a background thread: the notes are archived in the data
volume, normalized, their dates imported, and generation 1 of the store is built with
the configured memory profile. Meanwhile the 2.x ``memory.db`` keeps serving searches,
read-only (``store_backend.enabled()`` is False until the state says ``done``). Then
the backend indexes the store incrementally instead of ``memory.db``.

The state (plan, progress, checks, log) is served by ``GET /api/memory/migration`` for
the CortHeXis tab; ``POST /api/memory/migration/approve`` gives the explicit go when the
policy is ``ask`` or a check failed.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import store_backend

DATA_DIR = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))
MEMORY_DIR = Path(os.environ.get("CORTHEXIS_MEMORY_DIR") or os.environ.get(
    "SOKKAN_MEMORY_DIR", os.path.expanduser("~/.sokkan/memory")))
LEGACY_DB = Path(os.environ.get("SOKKAN_MEMORY_DB", DATA_DIR / "memory.db"))
RETRY_WAITING_S = float(os.environ.get("SOKKAN_MIGRATION_RETRY_S", "60"))
RETRY_BLOCKED_S = 600.0

_thread: threading.Thread | None = None
_wake = threading.Event()
_indexer = None
_index_lock = threading.Lock()


def active() -> bool:
    """The 3.0 store is in use (auto or postgres): migrate, then index the store."""
    return store_backend.configured() in ("auto", "postgres")


def work_dir() -> Path:
    return Path(store_backend.migration_dir())


def config_translation() -> dict:
    """How the 2.x embedding configuration maps to a 3.0 memory profile (for the UI and
    the log). The rule itself lives in core.embed.current_profile()."""
    from core import embed

    ml = os.environ.get("CORTHEXIS_ML_SERVICE_URL") or os.environ.get("ML_SERVICE_URL") or ""
    model = os.environ.get("CORTHEXIS_LEGACY_MODEL") or os.environ.get("SOKKAN_EMBED_MODEL")
    explicit = os.environ.get("CORTHEXIS_MEMORY_PROFILE") or \
        os.environ.get("SOKKAN_MEMORY_PROFILE") or ""
    try:
        profile = embed.current_profile()
    except ValueError as e:
        return {"profile": None, "error": str(e)}
    if explicit:
        why = "memory profile set explicitly (SOKKAN_MEMORY_PROFILE)"
    elif ml:
        why = ("2.x ML_SERVICE_URL kept: profile remote, same vectors as before; set "
               "SOKKAN_MEMORY_PROFILE to move to the 3.0 model")
    elif profile == "legacy":
        why = "2.x SOKKAN_EMBED_MODEL kept: profile legacy (in-process fastembed model)"
    else:
        why = "no 2.x embedding setting: default 3.0 profile"
    return {"profile": profile, "reason": why, "ml_service_url": bool(ml),
            "embed_model": model or None}


def run_once() -> dict:
    from core import embed, migrate
    from store_backend import get_store

    cfg = config_translation()
    return migrate.Migration(
        MEMORY_DIR, work_dir(), get_store(), embed.get, legacy_db=LEGACY_DB,
        context={"profile": cfg.get("profile"), "reason": cfg.get("reason")},
        log=lambda m: print(f"[sokkan] memory migration: {m}", file=sys.stderr)).run()


def _loop() -> None:
    while True:
        try:
            st = run_once()
        except Exception as e:  # noqa: BLE001 — the db may still be starting
            print(f"[sokkan] memory migration: {e}", file=sys.stderr)
            st = {"status": "waiting"}
        status = st.get("status")
        if status in ("done", "not-needed"):
            return
        _wake.wait(RETRY_BLOCKED_S if status in ("blocked", "failed") else RETRY_WAITING_S)
        _wake.clear()


def start() -> None:
    """Background migration thread (no-op in 2.x mode)."""
    global _thread
    if not active() or (_thread and _thread.is_alive()):
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="sokkan-memory-migration")
    _thread.start()


def status() -> dict:
    """State for the UI: steps, plan, progress, checks, log, and what serves searches."""
    import json

    from core import migrate

    out: dict = {"backend": store_backend.configured(),
                 "serving": "store" if store_backend.enabled() else "memory.db (2.x)"}
    if not active():
        return {**out, "status": "off"}
    p = work_dir() / "state.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {"status": "pending", "steps": {}}
    plan = work_dir() / "normalize-plan.txt"
    legacy = migrate.legacy_index(LEGACY_DB)
    return {**out, **st, "steps_order": list(migrate.STEPS),
            "plan": plan.read_text(encoding="utf-8")[:20000] if plan.exists() else None,
            "approved": _migration_approvals(),
            "legacy_index": {k: (len(v) if k == "notes" else v)
                             for k, v in legacy.items()} if legacy else None,
            "config": config_translation()}


def _migration_approvals() -> dict:
    import json

    try:
        return json.loads((work_dir() / "approved.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def approve(what: str, who: str) -> dict:
    """Explicit go (normalize | override), then wake the migration thread."""
    from core import migrate

    doc = migrate.Migration(MEMORY_DIR, work_dir(), None, None,
                            log=lambda m: None).approve(what, who)
    _wake.set()
    start()
    return doc


def index_tick() -> dict | None:
    """Incremental indexing of the store once it serves (replaces index_memory.run_index,
    which keeps memory.db as the 2.x left it). One pass at a time."""
    global _indexer
    if not store_backend.enabled():
        return None
    from core import embed, indexer
    from store_backend import get_store

    with _index_lock:
        if _indexer is None:
            cfg = indexer.IndexConfig.from_env(memory_dir=MEMORY_DIR)
            _indexer = indexer.Indexer(get_store(), embed.get(), cfg,
                                       log=lambda m: print(f"[sokkan] memory index: {m}",
                                                           file=sys.stderr))
        t0 = time.monotonic()
        rep = _indexer.run()
        if rep.reindexed or rep.pruned:
            print(f"[sokkan] memory index: {rep.reindexed} notes indexed, {rep.pruned} pruned "
                  f"({time.monotonic() - t0:.1f} s)", file=sys.stderr)
        return {"reindexed": rep.reindexed, "pruned": rep.pruned, "notes": rep.notes}
