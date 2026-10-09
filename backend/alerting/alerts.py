"""alerts.py — alert instances (one per rule × group) and their state machine:

    ok ──cond──▶ pending ──held `for`──▶ firing ──cond false──▶ resolved
                    └──cond false──▶ (dropped, never notified)

A firing alert notifies its rule's channels once, then again every `realert` while it lasts
(0 = never again), and once when it resolves. Silences (rule and/or label matchers, time
window) and quiet hours mute the notifications, never the state: the cockpit always shows
what is true. Ack = « someone is on it »: no more re-notifications until it resolves.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import humanize, store
from .durations import seconds


def _public(r) -> dict:
    grp = store.j(r["grp"], {})
    th = store.j(r["threshold"], None)
    return {**_raw_public(r, grp, th),
            # 3.5: what a person reads — « rpi1 » for 100.76.30.90:9100, « 77.8 % > 50 % », « down »
            "group_title": humanize.group_title(grp), "group_display": humanize.group_display(grp),
            "unit": (th or {}).get("unit", ""),
            "value_text": humanize.value_text(r["value"], th, (th or {}).get("unit", ""),
                                              (th or {}).get("type", ""), (th or {}).get("window", ""))}


def _raw_public(r, grp, th) -> dict:
    return {"id": r["id"], "rule_id": r["rule_id"], "project": r["project"],
            "severity": r["severity"], "group": grp, "group_key": r["group_key"],
            "state": r["state"], "started_at": r["started_at"], "fired_at": r["fired_at"],
            "resolved_at": r["resolved_at"], "last_notified_at": r["last_notified_at"],
            "value": r["value"], "threshold": th, "summary": r["summary"],
            "acked_by": r["acked_by"], "acked_at": r["acked_at"],
            "silenced_until": r["silenced_until"], "incident_id": r["incident_id"],
            "sample_events": store.j(r["sample_events"], []),
            "link": f"/?plane=operate&tab=alerts&alert={r['id']}"}


def get(aid: int, project: str | None = None) -> dict | None:
    c = store.con()
    r = c.execute("SELECT a.*, r.body AS rbody FROM alerts a LEFT JOIN rules r ON r.id=a.rule_id "
                  "WHERE a.id=?", (aid,)).fetchone()
    c.close()
    if r is None or (project is not None and r["project"] != project):
        return None
    d = _public(r)
    d["rule_name"] = store.j(r["rbody"], {}).get("name", "")
    d["silenced_until"] = _silenced_until(d) or d["silenced_until"]
    return d


def list_alerts(project: str, state: str = "active", limit: int = 100) -> tuple[list[dict], dict]:
    c = store.con()
    if state == "active":
        where, args = "a.state IN ('pending','firing')", ()
    elif state in ("pending", "firing", "resolved"):
        where, args = "a.state=?", (state,)
    else:
        where, args = "1=1", ()
    rows = c.execute(f"SELECT a.*, r.body AS rbody FROM alerts a LEFT JOIN rules r ON r.id=a.rule_id "
                     f"WHERE a.project=? AND {where} ORDER BY CASE a.state WHEN 'firing' THEN 0 WHEN "
                     f"'pending' THEN 1 ELSE 2 END, CASE a.severity WHEN 'critical' THEN 0 WHEN "
                     f"'warning' THEN 1 ELSE 2 END, COALESCE(a.fired_at, a.started_at) DESC LIMIT ?",
                     (project, *args, min(int(limit), 500))).fetchall()
    cnt = c.execute("SELECT state, COUNT(*) n, SUM(acked_by IS NOT NULL) acked FROM alerts WHERE project=? "
                    "AND state IN ('pending','firing') GROUP BY state", (project,)).fetchall()
    c.close()
    out = []
    for r in rows:
        d = _public(r)
        d["rule_name"] = store.j(r["rbody"], {}).get("name", "")
        su = _silenced_until(d)
        if su:
            d["silenced_until"] = su
        out.append(d)
    counts = {"firing": 0, "pending": 0, "silenced": 0, "acked": 0}
    for r in cnt:
        counts[r["state"]] = r["n"]
        counts["acked"] += r["acked"] or 0
    counts["silenced"] = sum(1 for a in out if a["state"] in ("pending", "firing") and a["silenced_until"])
    return out, counts


def history(rid: int, limit: int = 100) -> list[dict]:
    c = store.con()
    rows = c.execute("SELECT * FROM transitions WHERE rule_id=? ORDER BY ts DESC LIMIT ?",
                     (rid, min(int(limit), 1000))).fetchall()
    c.close()
    return [_transition_public(r) for r in rows]


def _transition_public(r) -> dict:
    th = store.j(r["threshold"], None)
    d = th if isinstance(th, dict) else {}
    return {"ts": r["ts"], "group": store.j(r["grp"], {}), "group_key": r["group_key"],
            "group_title": humanize.group_title(store.j(r["grp"], {})),
            "from": r["from_state"], "to": r["to_state"], "value": r["value"],
            # the contract's number (the dict the evaluator keeps carries the unit and the type)
            "threshold": d.get("value") if isinstance(th, dict) else th,
            "value_text": humanize.value_text(r["value"], d, d.get("unit", ""), d.get("type", ""),
                                              d.get("window", "")) if d else None,
            "note": r["note"]}


# ---- silences --------------------------------------------------------------------------------
def silences(project: str, active_only: bool = False) -> list[dict]:
    t = store.now()
    c = store.con()
    rows = c.execute("SELECT * FROM silences WHERE project=? ORDER BY ends_at DESC", (project,)).fetchall()
    c.close()
    out = [{"id": r["id"], "rule_id": r["rule_id"], "matchers": store.j(r["matchers"], {}),
            "starts_at": r["starts_at"], "ends_at": r["ends_at"], "reason": r["reason"],
            "created_by": r["created_by"], "active": r["starts_at"] <= t < r["ends_at"]} for r in rows]
    return [s for s in out if s["active"]] if active_only else out


def add_silence(project: str, rule_id: int | None, matchers: dict, starts_at: float, ends_at: float,
                reason: str, by: str) -> dict:
    if ends_at <= starts_at:
        raise ValueError("a silence must end after it starts")
    with store._lock:
        c = store.con()
        cur = c.execute("INSERT INTO silences(project, rule_id, matchers, starts_at, ends_at, reason, "
                        "created_by) VALUES(?,?,?,?,?,?,?)",
                        (project, rule_id, json.dumps({str(k): str(v) for k, v in (matchers or {}).items()}),
                         starts_at, ends_at, (reason or "")[:300], by))
        c.commit()
        sid = cur.lastrowid
        c.close()
    return next(s for s in silences(project) if s["id"] == sid)


def get_silence(sid: int) -> dict | None:
    c = store.con()
    r = c.execute("SELECT * FROM silences WHERE id=?", (sid,)).fetchone()
    c.close()
    return dict(r) if r else None


def delete_silence(sid: int) -> None:
    with store._lock:
        c = store.con()
        c.execute("DELETE FROM silences WHERE id=?", (sid,))
        c.commit()
        c.close()


def _silenced_until(a: dict) -> float | None:
    best = None
    for s in silences(a["project"], active_only=True):
        if s["rule_id"] not in (None, a["rule_id"]):
            continue
        if all(str(a["group"].get(k, "")) == v for k, v in s["matchers"].items()):
            best = max(best or 0, s["ends_at"])
    return best


def in_quiet_hours(qh: dict, t: float) -> bool:
    if not (qh or {}).get("enabled"):
        return False
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(t, ZoneInfo(qh.get("tz") or "Europe/Zurich"))
    except Exception:  # noqa: BLE001 — an unknown zone = UTC
        dt = datetime.utcfromtimestamp(t)
    if dt.weekday() not in (qh.get("days") or list(range(7))):
        return False

    def mins(s: str) -> int:
        h, m = (s or "0:0").split(":")
        return int(h) * 60 + int(m)
    a, b, x = mins(qh.get("start", "22:00")), mins(qh.get("end", "07:00")), dt.hour * 60 + dt.minute
    return (a <= x < b) if a <= b else (x >= a or x < b)


# ---- the state machine -----------------------------------------------------------------------
def _open(c, rid: int, gk: str):
    return c.execute("SELECT * FROM alerts WHERE rule_id=? AND group_key=? AND state IN "
                     "('pending','firing') ORDER BY id DESC LIMIT 1", (rid, gk)).fetchone()


def _transition(c, rule: dict, gk: str, grp: dict, frm: str, to: str, value, note: str = "") -> None:
    c.execute("INSERT INTO transitions(rule_id, project, ts, group_key, grp, from_state, to_state, value,"
              " threshold, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (rule["id"], rule["project"], store.now(), gk, json.dumps(grp), frm, to, value,
               json.dumps(rule.get("_threshold")), note[:300]))


def _num(x: float) -> str:
    """A value a person reads in a message: 77.78, 0.0123, 12345 (not 77.7834 or 1.2345e+04)."""
    if abs(x) >= 1:
        return f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{x:.3g}"


def summary(rule: dict, grp: dict, value) -> str:
    """« Host memory almost full: 77.8 % > 50 % (rog1) » · « Service down: down (rpi1 · node) »."""
    th = rule.get("_threshold") or {}
    s = rule["name"]
    v = humanize.value_text(value, th, th.get("unit") or rule.get("unit", ""), th.get("type") or rule.get("type", ""),
                            th.get("window", ""))
    if v:
        s += f": {v}"
    title = humanize.group_title(grp)
    if title:
        s += f" ({title})"
    return s


def apply(rule: dict, observations: list[dict], t: float) -> list[tuple[str, dict]]:
    """One evaluation's result → state changes. observations = [{group, group_key, cond, value,
    samples}] for EVERY group the evaluation saw. Returns the events to notify:
    [("firing"|"resolved"|"renotify", alert)]. Groups not observed resolve when open."""
    out: list[tuple[str, dict]] = []
    for_s = seconds(rule["for"])
    realert = seconds(rule["realert"])
    seen = set()
    with store._lock:
        c = store.con()
        for ob in observations:
            gk, grp = ob["group_key"], ob["group"]
            seen.add(gk)
            row = _open(c, rule["id"], gk)
            samples = json.dumps(ob.get("samples") or [])
            if ob["cond"]:
                if row is None:
                    state = "firing" if for_s <= 0 else "pending"
                    cur = c.execute(
                        "INSERT INTO alerts(rule_id, project, group_key, grp, state, severity, started_at,"
                        " fired_at, value, threshold, summary, sample_events) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rule["id"], rule["project"], gk, json.dumps(grp), state, rule["severity"], t,
                         t if state == "firing" else None, ob.get("value"),
                         json.dumps(rule.get("_threshold")), summary(rule, grp, ob.get("value")), samples))
                    _transition(c, rule, gk, grp, "ok", state, ob.get("value"))
                    if state == "firing":
                        out.append(("firing", {"id": cur.lastrowid}))
                else:
                    c.execute("UPDATE alerts SET value=?, summary=?, sample_events=? WHERE id=?",
                              (ob.get("value"), summary(rule, grp, ob.get("value")), samples, row["id"]))
                    if row["state"] == "pending" and t - row["started_at"] >= for_s:
                        c.execute("UPDATE alerts SET state='firing', fired_at=? WHERE id=?", (t, row["id"]))
                        _transition(c, rule, gk, grp, "pending", "firing", ob.get("value"))
                        out.append(("firing", {"id": row["id"]}))
                    elif row["state"] == "firing" and realert > 0 and not row["acked_by"] and \
                            row["last_notified_at"] and t - row["last_notified_at"] >= realert:
                        out.append(("renotify", {"id": row["id"]}))
            elif row is not None:
                _close(c, rule, row, t, ob.get("value"), out)
        for row in c.execute("SELECT * FROM alerts WHERE rule_id=? AND state IN ('pending','firing')",
                             (rule["id"],)).fetchall():
            if row["group_key"] not in seen:
                _close(c, rule, row, t, None, out, note="group no longer reported")
        c.commit()
        c.close()
    return out


def _close(c, rule, row, t, value, out, note=""):
    grp = store.j(row["grp"], {})
    if row["state"] == "pending":       # never rang: it just goes away
        c.execute("UPDATE alerts SET state='resolved', resolved_at=? WHERE id=?", (t, row["id"]))
        _transition(c, rule, row["group_key"], grp, "pending", "ok", value, note)
        return
    c.execute("UPDATE alerts SET state='resolved', resolved_at=?, value=COALESCE(?, value) WHERE id=?",
              (t, value, row["id"]))
    _transition(c, rule, row["group_key"], grp, "firing", "resolved", value, note)
    out.append(("resolved", {"id": row["id"]}))


def mark_notified(aid: int, t: float) -> None:
    with store._lock:
        c = store.con()
        c.execute("UPDATE alerts SET last_notified_at=? WHERE id=?", (t, aid))
        c.commit()
        c.close()


def ack(aid: int, by: str) -> None:
    with store._lock:
        c = store.con()
        c.execute("UPDATE alerts SET acked_by=?, acked_at=? WHERE id=?", (by, store.now(), aid))
        c.commit()
        c.close()


def set_incident(aid: int, incident_id: int) -> None:
    with store._lock:
        c = store.con()
        c.execute("UPDATE alerts SET incident_id=? WHERE id=?", (incident_id, aid))
        c.commit()
        c.close()


def record_external(rule: dict, labels: dict, summary_text: str, severity: str, resolved: bool) -> dict | None:
    """An alert POSTed by Grafana / a webhook → firing (or resolved) alert of its external rule."""
    gk = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("alertname",))[:300]
    t = store.now()
    with store._lock:
        c = store.con()
        row = _open(c, rule["id"], gk)
        aid = None
        if resolved:
            if row:
                c.execute("UPDATE alerts SET state='resolved', resolved_at=? WHERE id=?", (t, row["id"]))
                _transition(c, rule, gk, labels, row["state"], "resolved", None, "resolved by the source")
                aid = row["id"]
        elif row:
            c.execute("UPDATE alerts SET summary=?, last_notified_at=? WHERE id=?", (summary_text[:500], t, row["id"]))
            aid = row["id"]
        else:
            cur = c.execute(
                "INSERT INTO alerts(rule_id, project, group_key, grp, state, severity, started_at, fired_at,"
                " last_notified_at, summary) VALUES(?,?,?,?, 'firing', ?,?,?,?,?)",
                (rule["id"], rule["project"], gk, json.dumps(labels), severity if severity in
                 ("info", "warning", "critical") else "warning", t, t, t, summary_text[:500]))
            _transition(c, rule, gk, labels, "ok", "firing", None, "sent by the source")
            aid = cur.lastrowid
        c.commit()
        c.close()
    return get(aid) if aid else None
