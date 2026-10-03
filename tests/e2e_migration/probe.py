#!/usr/bin/env python3
"""Runs INSIDE a SOKKAN api container (2.x or 3.0) for the migration end-to-end test.

    probe.py bench  < questions.json   -> hit@1 / MRR@8 through memory_search (MCP tool)
    probe.py search "<query>"          -> top 3 with the fields that tell who served it
    probe.py notes                     -> {name: {modified, source}} of the index that serves
    probe.py recall "<prompt>"         -> what the UserPromptSubmit hook injects (through the
                                          backend, like a session's command hook)
    probe.py watch <seconds> "<query>" -> one JSON line per second: who serves, the
                                          migration step, the top hit (service continuity)
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/app/memory")
import memory_search_server as mem  # noqa: E402


def slug(name: str | None) -> str:
    """2.x keys a note whose YAML is broken by its file stem (flour_supplier), 3.0 by
    its repaired name (flour-supplier): compare both on the same footing."""
    return "-".join((name or "").lower().replace("_", "-").split())


def bench() -> dict:
    qs = json.load(sys.stdin)
    hit1, rr, lat = 0, 0.0, []
    misses = []
    for q in qs:
        t0 = time.monotonic()
        res = mem.memory_search(q["q"], top_k=8)
        lat.append(time.monotonic() - t0)
        names = [slug(r.get("note_name")) for r in res]
        if names and names[0] == q["expected"]:
            hit1 += 1
        if q["expected"] in names:
            rr += 1 / (names.index(q["expected"]) + 1)
        else:
            misses.append(q["q"])
    lat.sort()
    return {"questions": len(qs), "hit@1": hit1, "mrr@8": round(rr / len(qs), 4),
            "p50_ms": round(1000 * lat[len(lat) // 2]), "misses": misses,
            "degraded": any("degraded" in r for r in res)}


def search(q: str) -> list:
    keep = ("note_name", "score", "generation", "date_source", "age_days", "degraded")
    return [{k: r[k] for k in keep if k in r} for r in mem.memory_search(q, top_k=3)]


def notes() -> dict:
    try:
        import store_backend as sb
    except ImportError:          # SOKKAN 2.x
        sb = None
    if sb is not None and sb.enabled():
        st = sb.get_store()
        g = st.active_generation()
        return {"serving": "store", "generation": g.id, "identity": g.embed_identity,
                "notes": {n: {"modified": st.get_note(n).modified,
                              "source": st.get_note(n).modified_source,
                              "file": os.path.basename(st.get_note(n).source_path)}
                          for n in sorted(st.note_names(g.id))}}
    con = sqlite3.connect(f"file:{mem.DB_PATH}?mode=ro", uri=True)
    rows = con.execute("SELECT name, mtime, source_path FROM notes").fetchall()
    model = con.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {"serving": "memory.db", "model": model[0] if model else None, "chunks": chunks,
            "notes": {n: {"mtime": m, "file": os.path.basename(p)} for n, m, p in rows}}


def watch(seconds: float, q: str) -> None:
    import store_backend as sb
    end = time.monotonic() + seconds
    state = os.path.join(sb.migration_dir(), "state.json")
    while time.monotonic() < end:
        t0 = time.monotonic()
        try:
            res = mem.memory_search(q, top_k=3)
            top = res[0] if res else {}
            err = top.get("error") or top.get("info")
        except Exception as e:  # noqa: BLE001
            top, err = {}, f"{type(e).__name__}: {e}"
        try:
            with open(state) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        print(json.dumps({"t": round(time.time(), 1), "serving": "store" if sb.enabled()
                          else "memory.db", "status": st.get("status"), "step": st.get("step"),
                          "top": top.get("note_name"), "generation": top.get("generation"),
                          "degraded": bool(top.get("degraded")), "error": err,
                          "ms": round(1000 * (time.monotonic() - t0))}), flush=True)
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))


def recall(prompt: str) -> dict:
    import urllib.request
    data = os.environ.get("SOKKAN_DATA_DIR", "/data")
    path = os.path.join(data, "claude-hooks", "recall-token")
    if not os.path.exists(path):        # the backend creates it at the first hook call
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8097/api/memory/hook", data=b"{}",
                headers={"x-sokkan-hook-token": "x"}), timeout=10)
        except Exception:  # noqa: BLE001 — 401/403 expected
            pass
    tok = open(path).read().strip()
    body = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt,
                       "session_id": f"e2e-{time.time():.0f}"}).encode()
    req = urllib.request.Request("http://127.0.0.1:8097/api/memory/hook", data=body,
                                 headers={"content-type": "application/json",
                                          "x-sokkan-hook-token": tok})
    out = json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    return {"injected": bool(ctx), "context": ctx[:400]}


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "recall":
        print(json.dumps(recall(sys.argv[2]), ensure_ascii=False))
        raise SystemExit(0)
    if cmd == "watch":
        watch(float(sys.argv[2]), sys.argv[3])
        raise SystemExit(0)
    out = bench() if cmd == "bench" else search(sys.argv[2]) if cmd == "search" else notes()
    print(json.dumps(out, ensure_ascii=False))
