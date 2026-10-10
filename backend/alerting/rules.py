"""rules.py — rules as stored (one JSON body, validated by engine.normalize) and their state."""
from __future__ import annotations

import json

from . import engine, humanize, sources, store


def _readable(body: dict) -> dict:
    """Rules saved before 3.5 polish said « the query (Prometheus) » and had no unit: say what the
    query measures and with which unit, from the definition itself (nothing to migrate on disk)."""
    if body.get("unit") is not None and "the query (" not in (body.get("sentence") or ""):
        return body
    src = sources.get(int(body.get("source_id") or 0)) if body.get("source_id") else None
    if not src:
        return body
    body = dict(body)
    if body.get("unit") is None:
        body["unit"] = humanize.infer_unit(body, src["kind"])
    try:
        body["sentence"] = engine.sentence(body, src)
    except (KeyError, TypeError, ValueError):
        pass
    return body


def _public(r) -> dict:
    body = _readable(store.j(r["body"], {}))
    st = store.j(r["state"], {})
    if not r["enabled"]:
        st = {**st, "state": "disabled"}
    st.setdefault("state", "ok")
    spark = st.pop("spark", None) or []
    return {**body, "spark": spark, "id": r["id"], "project": r["project"], "enabled": bool(r["enabled"]),
            "created_by": r["created_by"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "external": r["external"] or None, "state": st}


def list_rules(project: str) -> list[dict]:
    c = store.con()
    rows = c.execute("SELECT * FROM rules WHERE project=? ORDER BY external != '', id",
                     (project,)).fetchall()
    c.close()
    return [_public(r) for r in rows]


def all_enabled() -> list[dict]:
    c = store.con()
    rows = c.execute("SELECT * FROM rules WHERE enabled=1 AND external='' ORDER BY next_eval").fetchall()
    c.close()
    return [_public(r) for r in rows]


def get(rid: int, project: str | None = None) -> dict | None:
    c = store.con()
    r = c.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
    c.close()
    if r is None or (project is not None and r["project"] != project):
        return None
    return _public(r)


def check(project: str, body: dict) -> dict:
    """Normalized body, its source checked to be visible from the project."""
    try:
        sid = int((body or {}).get("source_id") or 0)
    except (TypeError, ValueError):
        sid = 0
    src = sources.get(sid, project) if sid else None
    n = engine.normalize(body, src)
    if src["kind"] == "external":
        raise engine.RuleError("external alerts arrive by themselves: no rule to write for them")
    for cid in n["channels"]:
        from . import channels
        if channels.get(cid, project) is None:
            raise engine.RuleError(f"channel #{cid} does not exist in this project")
    require_someone(n)
    return n


NOBODY = ("nobody would be told: pick at least one channel in « Who to tell », or tick "
          "« no notification » if this rule only feeds the cockpit")


def require_someone(n: dict) -> None:
    """3.5.1 — in prod a 2nd rule was saved with no channel (none was preselected): it would ring
    in silence. An ACTIVE rule tells someone, or says on purpose that it tells nobody. A rule
    proposed by a session is saved off, and checked when a person turns it on."""
    if n.get("enabled", True) and not n.get("channels") and not n.get("no_notification"):
        raise engine.RuleError(NOBODY)


def create(project: str, body: dict, by: str) -> dict:
    n = check(project, body)
    t = store.now()
    with store._lock:
        c = store.con()
        cur = c.execute("INSERT INTO rules(project, body, enabled, created_by, created_at, updated_at,"
                        " state, next_eval) VALUES(?,?,?,?,?,?,?,?)",
                        (project, json.dumps(n), int(n["enabled"]), by, t, t,
                         json.dumps({"state": "ok", "since": t}), 0))
        c.commit()
        rid = cur.lastrowid
        c.close()
    return get(rid)


def update(rid: int, project: str, body: dict) -> dict:
    n = check(project, body)
    with store._lock:
        c = store.con()
        c.execute("UPDATE rules SET body=?, enabled=?, updated_at=?, next_eval=0 WHERE id=? AND project=?",
                  (json.dumps(n), int(n["enabled"]), store.now(), rid, project))
        c.commit()
        c.close()
    return get(rid)


def set_enabled(rid: int, on: bool) -> dict:
    with store._lock:
        c = store.con()
        body = store.j(c.execute("SELECT body FROM rules WHERE id=?", (rid,)).fetchone()["body"], {})
        body["enabled"] = on
        c.execute("UPDATE rules SET enabled=?, body=?, updated_at=?, next_eval=0 WHERE id=?",
                  (int(on), json.dumps(body), store.now(), rid))
        if not on:   # a disabled rule rings no more: its open alerts resolve
            c.execute("UPDATE alerts SET state='resolved', resolved_at=? WHERE rule_id=? AND state IN "
                      "('pending','firing')", (store.now(), rid))
        c.commit()
        c.close()
    return get(rid)


def delete(rid: int) -> None:
    with store._lock:
        c = store.con()
        for t in ("rules",):
            c.execute(f"DELETE FROM {t} WHERE id=?", (rid,))
        for t in ("alerts", "transitions", "terms", "keyvals"):
            c.execute(f"DELETE FROM {t} WHERE rule_id=?", (rid,))
        c.execute("DELETE FROM silences WHERE rule_id=?", (rid,))
        c.commit()
        c.close()


def set_state(rid: int, st: dict, next_eval: float) -> None:
    with store._lock:
        c = store.con()
        c.execute("UPDATE rules SET state=?, next_eval=? WHERE id=?", (json.dumps(st), next_eval, rid))
        c.commit()
        c.close()


def external_rule(project: str, alertname: str) -> int:
    """The read-only rule an external alert (Grafana…) is filed under — one per alertname."""
    src = next(s for s in sources.list_sources(project) if s["kind"] == "external")
    key = f"external:{alertname}"[:200]
    with store._lock:
        c = store.con()
        r = c.execute("SELECT id FROM rules WHERE project=? AND external=?", (project, key)).fetchone()
        if r:
            c.close()
            return r["id"]
        t = store.now()
        body = {"name": alertname[:120], "description": "Sent by an external system to "
                "/api/observability/alert.", "severity": "warning", "enabled": True,
                "source_id": src["id"], "query": {"mode": "raw", "raw": "*", "builder": {}},
                "type": "any", "params": {}, "every": "1m", "for": "0s", "group_by": [],
                "realert": "0s", "quiet_hours": {"enabled": False}, "channels": [],
                "actions": {"incident": False, "diag_session": False, "agent_id": None, "runbook": None},
                "labels": {}, "runbook_url": "", "level": None, "query_compiled": "*",
                "sentence": f"External alert « {alertname} » (Grafana or a webhook)"}
        cur = c.execute("INSERT INTO rules(project, body, enabled, created_by, created_at, updated_at,"
                        " state, external) VALUES(?,?,1,'external',?,?,?,?)",
                        (project, json.dumps(body), t, t, json.dumps({"state": "ok", "since": t}), key))
        c.commit()
        rid = cur.lastrowid
        c.close()
        return rid
