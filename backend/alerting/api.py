"""api.py — /api/alerting/* (Operate › Alerts). Mounted by app.py like uiroutes.

Every route is about the project of the request (projectgate: `/api/alerting` is a project
prefix — header x-sokkan-project or ?project=), with the person's role IN that project:
viewer reads; dev writes the project's rules, acks and silences; maintainer/admin manage
sources, channels and project-wide silences. A rule above the reader's clearance (3.4
classification) does not exist for them. 404 everywhere when the feature is off. Every write
is in the audit journal.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import audit
import auth
import features
import iam

from . import alerts, channels, engine, rules, scheduler, sources, templates
from .durations import BadDuration, seconds

router = APIRouter(prefix="/api/alerting")


def _on() -> None:
    if not features.enabled("alerting"):
        raise HTTPException(404, "feature disabled on this instance")


def _project(user: dict) -> str:
    return user.get("project") or "default"


def _need(user: dict, role: str) -> None:
    if iam.rank(user.get("role") or "") < iam.rank(role):
        label = {"dev": "developer", "admin": "maintainer"}.get(role, role)
        raise HTTPException(403, f"you need the {label} role in this project")


def _visible(user: dict, r: dict) -> bool:
    cap = user.get("clearance")
    lvl = r.get("level")
    return cap is None or lvl is None or int(lvl) <= int(cap)


def _rule(user: dict, rid: int) -> dict:
    r = rules.get(rid, _project(user))
    if r is None or not _visible(user, r):
        raise HTTPException(404, "rule not found")
    return r


def _alert(user: dict, aid: int) -> dict:
    a = alerts.get(aid, _project(user))
    if a is None:
        raise HTTPException(404, "alert not found")
    r = rules.get(a["rule_id"])
    if r is not None and not _visible(user, r):
        raise HTTPException(404, "alert not found")
    return a


Dep = Depends(auth.current_user)
On = Depends(_on)


# ---- status -------------------------------------------------------------------------------------
@router.get("/status")
def status(u: dict = Dep, _f: None = On) -> dict:
    srcs = sources.list_sources(_project(u))
    return {"enabled": True, "evaluator": scheduler.status(),
            "defaults": {"prometheus": any(s["builtin"] and s["kind"] == "prometheus" for s in srcs),
                         "loki": any(s["builtin"] and s["kind"] == "loki" for s in srcs)}}


# ---- sources ------------------------------------------------------------------------------------
class SourceIn(BaseModel):
    name: str = ""
    kind: str
    url: str
    auth: dict = {}
    options: dict = {}


@router.get("/sources")
def sources_list(u: dict = Dep, _f: None = On) -> dict:
    return {"sources": sources.list_sources(_project(u))}


@router.post("/sources", status_code=201)
def sources_create(body: SourceIn, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    try:
        s = sources.create(_project(u), body.model_dump(), u["email"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.source.create", f"#{s['id']} {s['name']}", s["kind"])
    return s


def _own_source(u: dict, sid: int) -> dict:
    s = sources.get(sid, _project(u))
    if s is None:
        raise HTTPException(404, "source not found")
    return s


@router.put("/sources/{sid}")
def sources_update(sid: int, body: SourceIn, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    s = _own_source(u, sid)
    if s["builtin"] or s["scope"] == "instance":
        raise HTTPException(409, "this source comes from the instance configuration")
    try:
        s = sources.update(sid, body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.source.update", f"#{sid} {s['name']}", s["kind"])
    return s


@router.delete("/sources/{sid}")
def sources_delete(sid: int, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    s = _own_source(u, sid)
    if s["builtin"] or s["scope"] == "instance":
        raise HTTPException(409, "this source comes from the instance configuration")
    used = [r["name"] for r in rules.list_rules(_project(u)) if r["source_id"] == sid]
    if used:
        raise HTTPException(409, f"used by {len(used)} rule(s): {', '.join(used[:3])}")
    sources.delete(sid)
    audit.log(u["email"], "alerting.source.delete", f"#{sid} {s['name']}", "")
    return {"ok": True}


@router.post("/sources/test")
def sources_test_new(body: SourceIn, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    try:
        v = sources.validate(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return sources.test({**v, "secret": v["secret"] or ""})


@router.post("/sources/{sid}/test")
def sources_test(sid: int, u: dict = Dep, _f: None = On) -> dict:
    _own_source(u, sid)
    d = sources._raw(sid)
    d["_project"] = _project(u)
    res = sources.test(d)
    sources.set_status(sid, {"ok": res["ok"], "checked_at": time.time(), "latency_ms": res["latency_ms"],
                             "error": res["error"]})
    return res


@router.get("/sources/{sid}/suggest")
def sources_suggest(sid: int, kind: str = "metrics", metric: str = "", label: str = "", field: str = "",
                    q: str = "", u: dict = Dep, _f: None = On) -> dict:
    _own_source(u, sid)
    d = sources._raw(sid)
    d["_project"] = _project(u)
    try:
        items = sources.adapter_for(d).suggest(kind, metric=metric, label=label, field=field, q=q)
    except sources.SourceError as e:
        raise HTTPException(502, str(e)) from None
    return {"items": items}


# ---- templates ----------------------------------------------------------------------------------
@router.get("/templates")
def templates_list(u: dict = Dep, _f: None = On) -> dict:
    return {"templates": templates.build(_project(u))}


class TemplateVars(BaseModel):
    values: dict = {}


@router.post("/templates/{tid}/apply")
def templates_apply(tid: str, body: TemplateVars, u: dict = Dep, _f: None = On) -> dict:
    t = next((x for x in templates.build(_project(u)) if x["id"] == tid), None)
    if t is None:
        raise HTTPException(404, "template not found")
    if not t["available"]:
        raise HTTPException(409, t["missing"] or "not available on this instance")
    return {"rule": templates.apply_variables(t, body.values, _project(u))}


# ---- rules --------------------------------------------------------------------------------------
def _with_spark(r: dict) -> dict:
    return {**r, "spark": _spark(r)}


def _spark(r: dict) -> list:
    """≤ 48 points over 24 h, kept by the evaluator (no source query on a list)."""
    cut = time.time() - 86400
    return [p for p in r.get("spark") or [] if p[0] >= cut][-48:]


@router.get("/rules")
def rules_list(u: dict = Dep, _f: None = On) -> dict:
    rs = [r for r in rules.list_rules(_project(u)) if _visible(u, r)]
    counts = {"firing": 0, "pending": 0, "silenced": 0, "ok": 0, "error": 0, "disabled": 0}
    for r in rs:
        counts[r["state"]["state"]] = counts.get(r["state"]["state"], 0) + 1
    return {"rules": [_with_spark(r) for r in rs], "counts": counts}


@router.post("/rules", status_code=201)
def rules_create(body: dict, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _check_agent(u, body)
    try:
        r = rules.create(_project(u), body, u["email"])
    except engine.RuleError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.rule.create", f"#{r['id']} {r['name']}", r["sentence"][:300])
    return r


def _check_agent(u: dict, body: dict) -> None:
    aid = ((body or {}).get("actions") or {}).get("agent_id")
    if not aid:
        return
    import agents
    a = agents.get(int(aid))
    if a is None or (a.get("project") or "default") != _project(u):
        raise HTTPException(422, "this agent does not exist in this project")
    if not agents.alert_triggered(a):
        raise HTTPException(422, f"the agent {a['name']} is not an alert agent: in Crew, set its "
                                 "trigger to « alert » (its writes are then held for approval)")


def _may_write(u: dict, r: dict) -> None:
    _need(u, "dev")
    if iam.rank(u.get("role") or "") < iam.rank("admin") and r["created_by"] not in (u["email"], "external"):
        raise HTTPException(403, "a developer edits the rules they wrote; ask a maintainer")


@router.get("/rules/{rid}")
def rules_get(rid: int, u: dict = Dep, _f: None = On) -> dict:
    return _rule(u, rid)


@router.put("/rules/{rid}")
def rules_update(rid: int, body: dict, u: dict = Dep, _f: None = On) -> dict:
    r = _rule(u, rid)
    _may_write(u, r)
    if r.get("external"):
        raise HTTPException(409, "an external alert's rule is managed by its source")
    _check_agent(u, body)
    try:
        r = rules.update(rid, _project(u), body)
    except engine.RuleError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.rule.update", f"#{rid} {r['name']}", r["sentence"][:300])
    return r


@router.delete("/rules/{rid}")
def rules_delete(rid: int, u: dict = Dep, _f: None = On) -> dict:
    r = _rule(u, rid)
    _may_write(u, r)
    scheduler.close_rule(r, f"rule deleted by {u['email']}")   # 3.5.1: they resolve, channels hear it
    rules.delete(rid)
    audit.log(u["email"], "alerting.rule.delete", f"#{rid} {r['name']}", "")
    return {"ok": True}


@router.post("/rules/{rid}/enable")
def rules_enable(rid: int, u: dict = Dep, _f: None = On) -> dict:
    r = _rule(u, rid)
    _may_write(u, r)
    try:
        rules.require_someone({**r, "enabled": True})
    except engine.RuleError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.rule.enable", f"#{rid} {r['name']}", "")
    return rules.set_enabled(rid, True)


@router.post("/rules/{rid}/disable")
def rules_disable(rid: int, u: dict = Dep, _f: None = On) -> dict:
    r = _rule(u, rid)
    _may_write(u, r)
    scheduler.close_rule(r, f"rule turned off by {u['email']}")
    audit.log(u["email"], "alerting.rule.disable", f"#{rid} {r['name']}", "")
    return rules.set_enabled(rid, False)


@router.post("/rules/{rid}/test-notify")
def rules_test_notify(rid: int, u: dict = Dep, _f: None = On) -> dict:
    r = _rule(u, rid)
    _need(u, "dev")
    a = {"id": 0, "severity": r["severity"], "group": {}, "link": f"/?plane=operate&tab=alerts&rule={rid}",
         "summary": f"Test of the rule « {r['name']} » — nothing is wrong."}
    out = {str(cid): channels.deliver(cid, "test", a, r) for cid in r["channels"]}
    audit.log(u["email"], "alerting.rule.test_notify", f"#{rid} {r['name']}", str(out)[:300])
    return {"channels": out}


@router.get("/rules/{rid}/history")
def rules_history(rid: int, limit: int = 100, u: dict = Dep, _f: None = On) -> dict:
    r = _rule(u, rid)
    return {"transitions": alerts.history(rid, limit, r)}


@router.post("/rules/{rid}/evaluate")
def rules_evaluate(rid: int, u: dict = Dep, _f: None = On) -> dict:
    """Evaluate now (the « Run now » button) — same path as the loop."""
    r = _rule(u, rid)
    _need(u, "dev")
    if not r["enabled"] or r.get("external"):
        raise HTTPException(409, "enable the rule first")
    scheduler.evaluate_rule(r, time.time())
    return rules.get(rid)


# ---- preview / backtest -------------------------------------------------------------------------
class PreviewIn(BaseModel):
    rule: dict
    range: str = "24h"


@router.post("/preview")
def preview(body: PreviewIn, u: dict = Dep, _f: None = On) -> dict:
    p = _project(u)
    try:
        sid = int((body.rule or {}).get("source_id") or 0)
    except (TypeError, ValueError):
        sid = 0
    src = sources.get(sid, p) if sid else None
    try:
        r = engine.normalize({**body.rule, "name": (body.rule or {}).get("name") or "preview"}, src)
        if src["kind"] == "external":
            raise engine.RuleError("external alerts have no query to preview")
    except engine.RuleError as e:
        return {"kind": "metric", "step_s": 0, "query_compiled": "", "sentence": "", "series": [],
                "threshold": None, "reference": None, "fired_intervals": [], "fires": 0,
                "sample_events": [], "error": str(e), "warnings": []}
    d = sources._raw(src["id"])
    d["_project"] = p
    return engine.preview(r, src, sources.adapter_for(d), body.range, time.time())


# ---- alerts -------------------------------------------------------------------------------------
@router.get("/alerts")
def alerts_list(state: str = "active", limit: int = 100, u: dict = Dep, _f: None = On) -> dict:
    out, counts = alerts.list_alerts(_project(u), state, limit)
    hidden = {r["id"] for r in rules.list_rules(_project(u)) if not _visible(u, r)}
    return {"alerts": [a for a in out if a["rule_id"] not in hidden], "counts": counts}


@router.get("/alerts/{aid}")
def alerts_get(aid: int, u: dict = Dep, _f: None = On) -> dict:
    return _alert(u, aid)


@router.post("/alerts/{aid}/ack")
def alerts_ack(aid: int, u: dict = Dep, _f: None = On) -> dict:
    a = _alert(u, aid)
    _need(u, "dev")
    alerts.ack(aid, u["email"])
    audit.log(u["email"], "alerting.alert.ack", f"#{aid}", a["summary"][:200])
    _cards_follow(aid, f"Acknowledged by {u['email']} — no more reminders until it resolves.")
    return alerts.get(aid)


def _cards_follow(aid: int, note: str) -> None:
    """The Teams cards of the alert say what was decided in the cockpit (best-effort, off-thread)."""
    a, r = alerts.get(aid), None
    if a:
        r = rules.get(a["rule_id"])
    if a and r:
        scheduler._pool.submit(channels.follow_up, a, r, note)


class SilenceFor(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    for_: str = Field("1h", alias="for")
    reason: str = ""


@router.post("/alerts/{aid}/silence")
def alerts_silence(aid: int, body: SilenceFor, u: dict = Dep, _f: None = On) -> dict:
    a = _alert(u, aid)
    _need(u, "dev")
    try:
        dur = seconds(body.for_)
    except BadDuration as e:
        raise HTTPException(422, str(e)) from None
    t = time.time()
    alerts.add_silence(_project(u), a["rule_id"], a["group"], t, t + dur, body.reason, u["email"])
    audit.log(u["email"], "alerting.alert.silence", f"#{aid}", f"{body.for_} {body.reason}"[:200])
    return alerts.get(aid)


@router.post("/alerts/{aid}/open-incident")
def alerts_incident(aid: int, u: dict = Dep, _f: None = On) -> dict:
    a = _alert(u, aid)
    _need(u, "dev")
    if a.get("incident_id"):
        return {"alert": a, "incident_id": a["incident_id"]}
    r = rules.get(a["rule_id"]) or {"name": a["rule_name"], "sentence": "", "project": _project(u)}
    iid = scheduler.open_incident(a, r, u["email"])
    return {"alert": alerts.get(aid), "incident_id": iid}


class ProposeAgent(BaseModel):
    agent_id: int


@router.post("/alerts/{aid}/propose-agent")
def alerts_propose_agent(aid: int, body: ProposeAgent, u: dict = Dep, _f: None = On) -> dict:
    """Ask an approved agent to look at this alert: a run request, like « Run now » in Crew."""
    a = _alert(u, aid)
    _need(u, "dev")
    import agents
    ag = agents.get(body.agent_id)
    if ag is None or (ag.get("project") or "default") != _project(u):
        raise HTTPException(404, "agent not found")
    try:
        run = agents.request_run(u, ag["id"], trigger="alert", requested_by=u["email"],
                                 context={"alertname": a["rule_name"], "summary": a["summary"],
                                          "labels": a["group"], "alert": aid,
                                          "incident": a.get("incident_id")})
    except (agents.AgentError, agents.Forbidden, agents.NotFound) as e:
        raise HTTPException(409, str(e)) from None
    audit.log(u["email"], "agent.run.request", ag["name"], f"run #{run['id']} for alert #{aid}")
    return {"proposal": f"run #{run['id']} of {ag['name']}", "status": run["status"]}


# ---- silences -----------------------------------------------------------------------------------
class SilenceIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    rule_id: int | None = None
    matchers: dict = {}
    duration: str = Field("", alias="for")
    ends_at: float | None = None
    starts_at: float | None = None
    reason: str = ""


@router.get("/silences")
def silences_list(u: dict = Dep, _f: None = On) -> dict:
    return {"silences": alerts.silences(_project(u))}


@router.post("/silences", status_code=201)
def silences_create(body: SilenceIn, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    if body.rule_id is None:
        _need(u, "admin")
    else:
        r = _rule(u, body.rule_id)
        if iam.rank(u.get("role") or "") < iam.rank("admin") and r["created_by"] != u["email"]:
            raise HTTPException(403, "a developer silences the rules they wrote; ask a maintainer")
    t = time.time()
    start = body.starts_at or t
    try:
        end = body.ends_at or (start + seconds(body.duration or "1h"))
        s = alerts.add_silence(_project(u), body.rule_id, body.matchers, start, end, body.reason, u["email"])
    except (BadDuration, ValueError) as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.silence.create", f"#{s['id']}",
              f"rule {body.rule_id} until {int(end)} {body.reason}"[:200])
    return s


@router.delete("/silences/{sid}")
def silences_delete(sid: int, u: dict = Dep, _f: None = On) -> dict:
    s = alerts.get_silence(sid)
    if s is None or s["project"] != _project(u):
        raise HTTPException(404, "silence not found")
    _need(u, "dev")
    if iam.rank(u.get("role") or "") < iam.rank("admin") and s["created_by"] != u["email"]:
        raise HTTPException(403, "only its author or a maintainer lifts a silence")
    alerts.delete_silence(sid)
    audit.log(u["email"], "alerting.silence.delete", f"#{sid}", "")
    return {"ok": True}


# ---- channels -----------------------------------------------------------------------------------
class ChannelIn(BaseModel):
    name: str = ""
    kind: str
    config: dict = {}
    enabled: bool = True


@router.get("/channels")
def channels_list(u: dict = Dep, _f: None = On) -> dict:
    return {"channels": channels.list_channels(_project(u)), "kinds": channels.kinds()}


@router.get("/teams-channels")
def teams_channels(u: dict = Dep, _f: None = On) -> dict:
    """3.5.1 — the Teams channels mapped to THIS project (Setup › Organization › Teams), to pick
    from instead of pasting `19:…@thread.tacv2`."""
    try:
        import features
        if not features.enabled("teams"):
            return {"channels": [], "teams": False}
        from teams import store as tstore
        rows = [c for c in tstore.channels() if c.get("project") == _project(u)]
    except Exception:  # noqa: BLE001
        return {"channels": [], "teams": False}
    return {"teams": True, "channels": [{"id": c["channel_id"], "name": c.get("name") or "",
                                         "level": c.get("level")} for c in rows]}


def _own_channel(u: dict, cid: int) -> dict:
    c = channels.get(cid, _project(u))
    if c is None:
        raise HTTPException(404, "channel not found")
    return c


@router.post("/channels", status_code=201)
def channels_create(body: ChannelIn, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    _teams_channel(u, body)
    try:
        c = channels.create(_project(u), body.model_dump(), u["email"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.channel.create", f"#{c['id']} {c['name']}", c["kind"])
    return c


def _teams_channel(u: dict, body: ChannelIn) -> None:
    """A Teams channel must be one mapped to THIS project (Setup › Organization › Teams)."""
    if body.kind != "teams":
        return
    try:
        from teams import store as tstore
        ch = tstore.channel((body.config or {}).get("channel_id", ""))
    except Exception:  # noqa: BLE001
        ch = None
    if not ch or ch.get("project") != _project(u):
        raise HTTPException(422, "this Teams channel is not mapped to this project")


@router.put("/channels/{cid}")
def channels_update(cid: int, body: ChannelIn, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    c = _own_channel(u, cid)
    if c["builtin"] or c["scope"] == "instance":
        raise HTTPException(409, "the instance notifications are set in Setup › Notifications")
    _teams_channel(u, body)
    try:
        c = channels.update(cid, body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    audit.log(u["email"], "alerting.channel.update", f"#{cid} {c['name']}", c["kind"])
    return c


@router.delete("/channels/{cid}")
def channels_delete(cid: int, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    c = _own_channel(u, cid)
    if c["builtin"] or c["scope"] == "instance":
        raise HTTPException(409, "the instance notifications are set in Setup › Notifications")
    used = [r["name"] for r in rules.list_rules(_project(u)) if cid in r["channels"]]
    if used:
        raise HTTPException(409, f"used by {len(used)} rule(s): {', '.join(used[:3])}")
    channels.delete(cid)
    audit.log(u["email"], "alerting.channel.delete", f"#{cid} {c['name']}", "")
    return {"ok": True}


@router.post("/channels/{cid}/test")
def channels_test(cid: int, u: dict = Dep, _f: None = On) -> dict:
    _own_channel(u, cid)
    _need(u, "dev")
    res = channels.test(cid, _project(u))
    audit.log(u["email"], "alerting.channel.test", f"#{cid}", res["detail"][:200])
    return res
