"""scheduler.py — when passes run. Ticked by the agents scheduler (agents_runtime.Runtime.tick).

* a source with a `schedule` (cron, e.g. "30 1 * * *") gets its incremental pass at night;
* a pass that was interrupted by a restart or a crash is taken up again (same pass, same
  batch) — unless a person stopped it;
* one pass per source at a time; a missed occurrence older than MISFIRE_S is not caught up
  in the middle of the day (the model also serves people).

A pass runs in a thread of the API process: it only does I/O (files, the extraction child
process, HTTP to the local model).
"""
from __future__ import annotations

import os
import threading
import time

from . import config as cfgmod
from . import runner, state

MISFIRE_S = 6 * 3600
_lock = threading.Lock()
_live: dict[str, dict] = {}          # source id -> {"thread", "stop", "pass": Pass}


def auto_resume() -> bool:
    return (os.environ.get("SOKKAN_DOC_INGEST_AUTO_RESUME") or "1").strip() != "0"


def running(source_id: str) -> bool:
    with _lock:
        e = _live.get(source_id)
        return bool(e and e["thread"].is_alive())


def start_pass(source_id: str, kind: str = "manual", requested_by: str = "", **kw) -> dict:
    src = cfgmod.get(source_id)
    if src is None:
        raise KeyError(source_id)
    if not src.enabled:
        raise RuntimeError(f"source {source_id} is disabled")
    with _lock:
        e = _live.get(source_id)
        if e and e["thread"].is_alive():
            raise RuntimeError(f"a pass of {source_id} is already running")
        stop = threading.Event()
        p = runner.Pass(src, kind, requested_by, stop=stop, **kw)

        def _run() -> None:
            try:
                p.run()
            except Exception as ex:  # noqa: BLE001 — e.g. no local model: said, not raised
                print(f"[doc-ingest] pass of {source_id} did not start: {ex}", flush=True)

        t = threading.Thread(target=_run, daemon=True, name=f"docingest-pass-{source_id}")
        _live[source_id] = {"thread": t, "stop": stop, "pass": p}
        t.start()
    return {"started": True, "source": source_id, "kind": kind}


def stop_pass(source_id: str, by: str = "") -> bool:
    with _lock:
        e = _live.get(source_id)
        if not e or not e["thread"].is_alive():
            return False
        e["pass"].stop_reason = f"stopped by {by or 'an operator'}"
        e["stop"].set()
        return True


def stop_all() -> None:
    """API shutdown: running passes finish their current document and are left `interrupted`
    (resumed at the next start)."""
    with _lock:
        for e in _live.values():
            e["stop"].set()


def tick(now: float | None = None) -> list[str]:
    """Called every scheduler tick. Returns the sources a pass was started for."""
    import cronexpr
    now = time.time() if now is None else now
    started: list[str] = []
    try:
        srcs = cfgmod.load()
    except cfgmod.ConfigError as e:
        print(f"[doc-ingest] sources not loaded: {e}", flush=True)
        return started
    for src in srcs:
        if not src.enabled or running(src.id):
            continue
        if auto_resume() and _resumable(src.id, now):
            try:
                start_pass(src.id, "incremental", "scheduler (resume)")
                started.append(src.id)
            except RuntimeError:
                pass
            continue
        if not src.schedule:
            continue
        tz = src.timezone or cronexpr.DEFAULT_TZ
        cron = cronexpr.parse(src.schedule)
        st = state.schedule_get(src.id)
        if st is None or st["next_run_at"] is None:
            state.schedule_set(src.id, cron.next_after(now, tz))
            continue
        if st["next_run_at"] > now:
            continue
        due = st["next_run_at"]
        state.schedule_set(src.id, cron.next_after(now, tz), last_run_at=now)
        if now - due > MISFIRE_S:
            continue                      # missed while the API was down: next night
        try:
            start_pass(src.id, "incremental", "scheduler")
            started.append(src.id)
        except RuntimeError as e:
            print(f"[doc-ingest] {src.id}: {e}", flush=True)
    return started


def _resumable(source_id: str, now: float) -> bool:
    rows = state.pass_list(source=source_id, limit=1)
    if not rows:
        return False
    p = rows[0]
    if p["status"] == "interrupted":
        return not (p.get("error") or "").startswith("stopped by")
    return p["status"] == "running" and now - p["heartbeat"] >= state.STALE_S


def status() -> dict:
    out = {}
    for src in cfgmod.load():
        st = state.schedule_get(src.id) or {}
        out[src.id] = {"running": running(src.id), "schedule": src.schedule,
                       "next_run_at": st.get("next_run_at"), "last_run_at": st.get("last_run_at")}
    return out
