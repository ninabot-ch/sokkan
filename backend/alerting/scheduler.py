"""scheduler.py — the evaluation loop. One thread per API process; only the holder of the
lease (alerting.db, renewed every tick) evaluates, so a chart with several api replicas
rings once. A rule is evaluated when its `every` is due; a source that fails puts the rule in
`error` (visible, and itself an event of the « sokkan » source) and the loop goes on.

What happens when an alert fires, in order: notify the rule's channels (unless silenced or in
quiet hours), then the actions the rule asks for — open an incident, start a diagnosis session
that waits for a go-ahead, start the run of an alert agent (an agent approved in Crew whose
writes are held for approval). Nothing else executes on its own: human-gated.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import alerts, channels, engine, rules, sources, store
from .durations import seconds

TICK_S = float(os.environ.get("SOKKAN_ALERTING_TICK_S") or 15)
LEASE_S = max(TICK_S * 4, 60)
HOLDER = f"{socket.gethostname()}:{os.getpid()}"
hooks: dict = {}            # "spawn_diag": callable(rule, alert) -> session_id (set by app.py)
_stop = threading.Event()
_thread: threading.Thread | None = None
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert-notify")
_status = {"leader": False, "tick_s": TICK_S, "last_tick": None, "rules": 0, "errors": 0}


def status() -> dict:
    return dict(_status)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if os.environ.get("SOKKAN_ALERTING_EVALUATOR", "1").strip() in ("0", "false", "off"):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="alerting", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def _loop() -> None:
    while not _stop.wait(TICK_S if _status["last_tick"] else 2):
        try:
            tick()
        except Exception as e:  # noqa: BLE001 — the loop never dies
            print(f"[alerting] tick failed: {e}", file=sys.stderr)


def tick(now: float | None = None, force: bool = False) -> int:
    """Evaluate the rules that are due. Returns how many were evaluated."""
    import features
    if not features.enabled("alerting"):
        return 0
    t = now or time.time()
    _status["leader"] = store.take_lease(HOLDER, LEASE_S)
    _status["last_tick"] = t
    if not _status["leader"]:
        return 0
    n = errs = 0
    rs = rules.all_enabled()
    _status["rules"] = len(rs)
    for r in rs:
        st = r.get("state") or {}
        if not force and float(st.get("next_eval") or 0) > t:
            continue
        ok = evaluate_rule(r, t)
        n += 1
        errs += 0 if ok else 1
    _status["errors"] = errs
    return n


def _adapter(r: dict, src: dict):
    d = sources._raw(src["id"])
    d["_project"] = r["project"]
    return sources.adapter_for(d)


def evaluate_rule(r: dict, t: float) -> bool:
    """One live evaluation → state changes → notifications and actions. False on error."""
    every = seconds(r["every"])
    prev = r.get("state") or {}
    src = sources.get(r["source_id"])
    st = {"since": prev.get("since") or t, "last_eval": t, "next_eval": t + every, "error": None}
    try:
        if src is None:
            raise sources.SourceError("its source was deleted")
        ad = _adapter(r, src)
        typ = r["type"]
        known, last_vals = None, None
        last_eval = float(prev.get("last_eval") or 0)
        if typ == "new_term":
            known = _terms(r["id"])
            if known is None:          # first evaluation: learn what exists, ring for nothing
                _save_terms(r["id"], _seen_values(r, src, ad, t), t)
                obs = []
                ev = {"threshold": None, "series": []}
            else:
                start = max(last_eval, t - engine.lookback(r)) if last_eval else t - every
                ev = engine.evaluate(r, src, ad, start, t, max(t - start, 1), known_terms=known)
                _save_terms(r["id"], set(ev.get("new_terms") or {}), t)
                obs = _observe(ev, r, last_eval, window_mode=True)
        elif typ == "change":
            last_vals = _keyvals(r["id"])
            start = max(last_eval, t - engine.lookback(r)) if last_eval else t - every
            ev = engine.evaluate(r, src, ad, start, t, max(t - start, 1), last_values=last_vals)
            _save_keyvals(r["id"], ev.get("changes") or {}, t)
            obs = _observe(ev, r, last_eval, window_mode=True)
        elif typ == "any":
            start = last_eval if last_eval and t - last_eval < engine.lookback(r) else t - every
            ev = engine.evaluate(r, src, ad, start, t, max(t - start, 1))
            obs = _observe(ev, r, last_eval, window_mode=True)
        else:
            step = min(every, 60.0) if src["kind"] == "prometheus" else every
            ev = engine.evaluate(r, src, ad, t - engine.lookback(r), t, step)
            obs = _observe(ev, r, last_eval, window_mode=False)
    except (sources.SourceError, engine.RuleError, ValueError) as e:
        st.update({"state": "error", "error": str(e)[:300], "firing": prev.get("firing", 0),
                   "pending": prev.get("pending", 0), "spark": r.get("spark") or []})
        if prev.get("state") != "error":
            st["since"] = t
            _audit_sys("alerting.rule.error", r, str(e)[:300])
        rules.set_state(r["id"], st, t + every)
        return False
    r = {**r, "_threshold": ev.get("threshold")}
    events = alerts.apply(r, obs, t)
    act, _ = alerts.list_alerts(r["project"], "active", 500)
    mine = [a for a in act if a["rule_id"] == r["id"]]
    firing = sum(1 for a in mine if a["state"] == "firing")
    pending = sum(1 for a in mine if a["state"] == "pending")
    silenced = firing and all(a["silenced_until"] for a in mine if a["state"] == "firing")
    state = "silenced" if silenced else "firing" if firing else "pending" if pending else "ok"
    if state != prev.get("state"):
        st["since"] = t
    vals = [o.get("value") for o in obs if o.get("value") is not None]
    # the value that matters is the worst one toward the line: min for « below » (a target down = 0,
    # not the 1 of the 17 others), max otherwise
    below = str((ev.get("threshold") or {}).get("op") or "").startswith("<")
    last_value = (min(vals) if below else max(vals)) if vals else None
    st.update({"state": state, "firing": firing, "pending": pending, "last_value": last_value})
    spark = [p for p in r.get("spark") or [] if p[0] >= t - 86400]
    if last_value is not None and (not spark or t - spark[-1][0] >= 1800):
        spark.append([t, round(float(last_value), 6)])
    st["spark"] = spark[-48:]
    rules.set_state(r["id"], st, t + every)
    for kind, ref in events:
        _on_event(kind, ref["id"], r, t)
    return True


def _observe(ev: dict, r: dict, last_eval: float, window_mode: bool) -> list[dict]:
    """The evaluation's verdict per group, NOW (window_mode: anything since the last eval)."""
    out = []
    for s in ev.get("series") or []:
        pts, cond = s["points"], s.get("cond") or []
        if not pts:
            continue
        if window_mode:
            hits = [(p, c) for p, c in zip(pts, cond) if c]
            c = bool(hits)
            v = sum(p[1] for p, _ in hits) if hits else 0.0
        else:
            c, v = bool(cond[-1]) if cond else False, pts[-1][1]
        out.append({"group": s["group"], "group_key": s["group_key"], "cond": c, "value": v,
                    "samples": ev.get("sample_events") or []})
    return out


# ---- notifications and actions ---------------------------------------------------------------
def _on_event(kind: str, aid: int, r: dict, t: float) -> None:
    a = alerts.get(aid)
    if a is None:
        return
    muted = bool(a.get("silenced_until")) or alerts.in_quiet_hours(r.get("quiet_hours") or {}, t)
    if kind == "renotify" and a.get("acked_by"):
        return
    if not muted and r.get("channels"):
        alerts.mark_notified(aid, t)
        for cid in r["channels"]:
            _pool.submit(_deliver, cid, "renotify" if kind == "renotify" else kind, a, r)
    elif kind == "firing":
        alerts.mark_notified(aid, t)     # realert counts from the (muted) first ring
    if kind != "firing":
        return
    _audit_sys("alerting.alert.firing", r, a["summary"])
    acts = r.get("actions") or {}
    if acts.get("incident") and not a.get("incident_id"):
        try:
            open_incident(a, r, "rule")
        except Exception as e:  # noqa: BLE001
            print(f"[alerting] incident failed: {e}", file=sys.stderr)
    if acts.get("diag_session") and hooks.get("spawn_diag"):
        try:
            hooks["spawn_diag"](r, a)
        except Exception as e:  # noqa: BLE001
            print(f"[alerting] diagnosis session failed: {e}", file=sys.stderr)
    _fire_agents(r, a, acts.get("agent_id"))


def _deliver(cid: int, kind: str, a: dict, r: dict) -> None:
    res = channels.deliver(cid, kind, a, r)
    if res != "ok":
        _audit_sys("alerting.notify.failed", r, f"channel #{cid}: {res}")


def open_incident(a: dict, r: dict, by: str) -> int:
    import observability
    iid = observability.record_incident(f"{r['name']}", a.get("summary") or r.get("sentence", ""),
                                         "critical" if a["severity"] == "critical" else "warning")
    alerts.set_incident(a["id"], iid)
    import audit
    audit.log(by if "@" in by else "alerting", "incident.open", f"#{iid}",
              f"from alert #{a['id']} ({r['name']})", project=r["project"])
    return iid


def _fire_agents(r: dict, a: dict, agent_id) -> None:
    """Agents approved in Crew with an « alert » trigger (their writes are held for approval):
    the rule's own agent, and every agent subscribed to alert:<rule name>."""
    try:
        import agents
        import agents_runtime
    except Exception:  # noqa: BLE001
        return
    ctx = {"alertname": r["name"], "severity": a["severity"], "summary": a["summary"],
           "labels": a.get("group") or {}, "rule": r["id"], "alert": a["id"],
           "incident": a.get("incident_id")}
    if agent_id:
        ag = agents.get(int(agent_id))
        if ag and (ag.get("project") or "default") == r["project"] and ag["status"] == "active" \
                and agents.alert_triggered(ag) and not agents.active_run(ag["id"]):
            agents.enqueue_run(ag["id"], "event", f"alert:{r['name']}", context=ctx)
            agents_runtime.poke()
    rt = agents_runtime.get_runtime()
    if rt:
        try:
            rt.fire_event("alert", r["name"], ctx)
        except Exception as e:  # noqa: BLE001
            print(f"[alerting] agents event failed: {e}", file=sys.stderr)


def _audit_sys(action: str, r: dict, detail: str) -> None:
    try:
        import audit
        audit.log("alerting", action, f"rule #{r['id']} {r['name']}"[:200], detail, project=r["project"])
    except Exception:  # noqa: BLE001
        pass


# ---- memory of new_term / change --------------------------------------------------------------
def _terms(rid: int) -> set | None:
    c = store.con()
    rows = c.execute("SELECT term FROM terms WHERE rule_id=?", (rid,)).fetchall()
    seeded = c.execute("SELECT 1 FROM keyvals WHERE rule_id=? AND k='__seeded__'", (rid,)).fetchone()
    c.close()
    if not seeded:
        return None
    return {r["term"] for r in rows}


def _seen_values(r: dict, src: dict, ad, t: float) -> set:
    f = r["params"]["field"]
    try:
        evts = ad.events(r["query_compiled"], t - seconds(r["params"]["lookback"]), t, limit=10000)
    except sources.SourceError:
        return set()
    return {str(e["fields"][f]) for e in evts if e["fields"].get(f) not in (None, "")}


def _save_terms(rid: int, terms: set, t: float) -> None:
    with store._lock:
        c = store.con()
        c.executemany("INSERT OR IGNORE INTO terms(rule_id, term, first_seen) VALUES(?,?,?)",
                      [(rid, x[:500], t) for x in terms])
        c.execute("INSERT OR REPLACE INTO keyvals(rule_id, k, v, ts) VALUES(?, '__seeded__', '1', ?)", (rid, t))
        c.commit()
        c.close()


def _keyvals(rid: int) -> dict:
    c = store.con()
    rows = c.execute("SELECT k, v FROM keyvals WHERE rule_id=? AND k != '__seeded__'", (rid,)).fetchall()
    c.close()
    return {r["k"]: r["v"] for r in rows}


def _save_keyvals(rid: int, vals: dict, t: float) -> None:
    with store._lock:
        c = store.con()
        c.executemany("INSERT OR REPLACE INTO keyvals(rule_id, k, v, ts) VALUES(?,?,?,?)",
                      [(rid, k[:300], str(v)[:500], t) for k, v in vals.items()])
        c.commit()
        c.close()


def dumps(v) -> str:
    return json.dumps(v)
