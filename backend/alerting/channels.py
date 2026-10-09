"""channels.py — where an alert is delivered: Telegram, Microsoft Teams (adaptive card with
Ack / Silence 1 h / Open incident), Slack (incoming webhook), e-mail (the instance's SMTP),
a generic webhook (JSON, optional HMAC signature) and PagerDuty (Events API v2).

Secret fields (bot token, webhook URLs, routing key…) are sealed in alerting.db and shown as
`<field>_set: true`. Delivery is best-effort with a short timeout: a channel that fails is
reported (test button, alert history), never blocks the evaluation loop.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from . import store

TIMEOUT = 10.0
KINDS = [
    {"kind": "telegram", "label": "Telegram", "fields": [
        {"key": "bot_token", "label": "Bot token", "secret": True, "required": True},
        {"key": "chat_id", "label": "Chat id", "required": True}]},
    {"kind": "teams", "label": "Microsoft Teams", "fields": [
        {"key": "channel_id", "label": "Teams channel (mapped to this project)", "required": True}]},
    {"kind": "slack", "label": "Slack", "fields": [
        {"key": "webhook_url", "label": "Incoming webhook URL", "secret": True, "required": True}]},
    {"kind": "email", "label": "E-mail", "fields": [
        {"key": "to", "label": "Recipients (comma-separated)", "required": True}]},
    {"kind": "webhook", "label": "Webhook", "fields": [
        {"key": "url", "label": "URL", "secret": True, "required": True},
        {"key": "secret", "label": "Signing secret (HMAC-SHA256, optional)", "secret": True}]},
    {"kind": "pagerduty", "label": "PagerDuty", "fields": [
        {"key": "routing_key", "label": "Integration (routing) key", "secret": True, "required": True}]},
]
BY_KIND = {k["kind"]: k for k in KINDS}
SEV_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def public_url() -> str:
    return (os.environ.get("SOKKAN_PUBLIC_URL") or "http://localhost:3009").rstrip("/")


def smtp_configured() -> bool:
    return bool(os.environ.get("SOKKAN_SMTP_HOST"))


def kinds() -> list[dict]:
    out = []
    for k in KINDS:
        d = dict(k)
        if k["kind"] == "email" and not smtp_configured():
            d["unavailable"] = "the instance has no SMTP server (SOKKAN_SMTP_HOST)"
        if k["kind"] == "teams":
            try:
                import teams
                if not teams.enabled():
                    d["unavailable"] = "the Teams feature is off on this instance"
            except Exception:  # noqa: BLE001
                d["unavailable"] = "the Teams feature is off on this instance"
        out.append(d)
    return out


# ---- rows --------------------------------------------------------------------------------------
def _row(r) -> dict:
    cfg = store.j(r["config"], {})
    sec = store.unseal(r["secrets_ct"])
    for f in BY_KIND.get(r["kind"], {"fields": []})["fields"]:
        if f.get("secret"):
            cfg[f"{f['key']}_set"] = bool(sec.get(f["key"]))
    return {"id": r["id"], "name": r["name"], "kind": r["kind"],
            "scope": "instance" if r["project"] == "*" else "project", "project": r["project"],
            "enabled": bool(r["enabled"]), "config": cfg, "builtin": False,
            "last_test": store.j(r["last_test"], None)}


def _builtin() -> dict | None:
    """The instance notifications (Setup › Notifications, notify.json) as a channel, id 0."""
    try:
        import notify
        st = notify.status()
    except Exception:  # noqa: BLE001
        return None
    if not (st.get("telegram") or st.get("webhook") or st.get("enabled")):
        return None
    return {"id": 0, "name": "Instance notifications", "kind": "instance", "scope": "instance",
            "project": "*", "enabled": True, "builtin": True,
            "config": {"detail": "Setup › Notifications (Telegram / webhook of the instance)"},
            "last_test": None}


def list_channels(project: str) -> list[dict]:
    c = store.con()
    rows = c.execute("SELECT * FROM channels WHERE project IN ('*', ?) ORDER BY id", (project,)).fetchall()
    c.close()
    out = [_row(r) for r in rows]
    b = _builtin()
    return ([b] if b else []) + out


def get(cid: int, project: str | None = None) -> dict | None:
    if int(cid) == 0:
        return _builtin()
    c = store.con()
    r = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    c.close()
    if r is None or (project is not None and r["project"] not in ("*", project)):
        return None
    return _row(r)


def _split(kind: str, config: dict, old_secrets: dict | None = None) -> tuple[dict, dict]:
    spec = BY_KIND.get(kind)
    if spec is None:
        raise ValueError(f"unknown channel kind {kind!r}")
    pub, sec = {}, dict(old_secrets or {})
    for f in spec["fields"]:
        v = (config or {}).get(f["key"])
        v = v.strip() if isinstance(v, str) else v
        if f.get("secret"):
            if v:
                sec[f["key"]] = v
        elif v is not None:
            pub[f["key"]] = v
        have = sec.get(f["key"]) if f.get("secret") else pub.get(f["key"])
        if f.get("required") and not have:
            raise ValueError(f"{spec['label']}: « {f['label']} » is required")
    if kind in ("slack",) and not str(sec.get("webhook_url", "")).startswith("https://"):
        raise ValueError("the Slack webhook URL starts with https://")
    if kind == "webhook" and not str(sec.get("url", "")).startswith(("https://", "http://")):
        raise ValueError("the webhook URL starts with https:// or http://")
    return pub, sec


def create(project: str, body: dict, by: str) -> dict:
    kind = (body.get("kind") or "").strip()
    pub, sec = _split(kind, body.get("config") or {})
    name = (body.get("name") or "").strip() or BY_KIND[kind]["label"]
    t = store.now()
    with store._lock:
        c = store.con()
        cur = c.execute("INSERT INTO channels(project, name, kind, config, secrets_ct, enabled, created_by,"
                        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (project, name[:80], kind, json.dumps(pub), store.seal(sec),
                         int(body.get("enabled", True)), by, t, t))
        c.commit()
        cid = cur.lastrowid
        c.close()
    return get(cid)


def update(cid: int, body: dict) -> dict:
    c = store.con()
    r = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    c.close()
    kind = (body.get("kind") or r["kind"]).strip()
    pub, sec = _split(kind, body.get("config") or {}, store.unseal(r["secrets_ct"]) if kind == r["kind"] else {})
    with store._lock:
        c = store.con()
        c.execute("UPDATE channels SET name=?, kind=?, config=?, secrets_ct=?, enabled=?, updated_at=? WHERE id=?",
                  ((body.get("name") or r["name"]).strip()[:80], kind, json.dumps(pub), store.seal(sec),
                   int(body.get("enabled", True)), store.now(), cid))
        c.commit()
        c.close()
    return get(cid)


def delete(cid: int) -> None:
    with store._lock:
        c = store.con()
        c.execute("DELETE FROM channels WHERE id=?", (cid,))
        c.commit()
        c.close()


def _set_test(cid: int, res: dict) -> None:
    if not cid:
        return
    with store._lock:
        c = store.con()
        c.execute("UPDATE channels SET last_test=? WHERE id=?",
                  (json.dumps({**res, "at": store.now()}), cid))
        c.commit()
        c.close()


# ---- delivery ----------------------------------------------------------------------------------
def message(event: str, alert: dict, rule: dict) -> dict:
    """The words of a notification, shared by every channel."""
    sev = alert.get("severity") or rule.get("severity") or "warning"
    icon = "✅" if event == "resolved" else SEV_ICON.get(sev, "🟠")
    head = {"firing": "Alert", "renotify": "Still firing", "resolved": "Resolved", "test": "[TEST] Alert"}[event]
    title = f"{icon} {head} — {rule.get('name', '')}"
    lines = [alert.get("summary") or rule.get("sentence") or ""]
    if event != "resolved" and rule.get("sentence"):
        lines.append(f"Rule: {rule['sentence']}")
    if alert.get("group"):
        lines.append("Where: " + ", ".join(f"{k}={v}" for k, v in alert["group"].items()))
    if rule.get("runbook_url"):
        lines.append(f"Runbook: {rule['runbook_url']}")
    link = public_url() + (alert.get("link") or f"/?plane=operate&tab=alerts&rule={rule.get('id', '')}")
    return {"title": title, "text": "\n".join(x for x in lines if x), "link": link, "severity": sev,
            "event": event}


def deliver(cid: int, event: str, alert: dict, rule: dict) -> str:
    """Send to one channel → "ok" or a short error."""
    m = message(event, alert, rule)
    try:
        if int(cid) == 0:
            import notify
            res = notify.send(m["title"], m["text"], m["link"], "alert")
            return "ok" if res and all(v == "ok" for v in res.values()) else (json.dumps(res) or "no channel")
        c = store.con()
        r = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        c.close()
        if r is None:
            return "channel deleted"
        if not r["enabled"]:
            return "channel disabled"
        cfg, sec = store.j(r["config"], {}), store.unseal(r["secrets_ct"])
        fn = {"telegram": _telegram, "teams": _teams, "slack": _slack, "email": _email,
              "webhook": _webhook, "pagerduty": _pagerduty}[r["kind"]]
        return fn(cfg, sec, m, alert, rule)
    except Exception as e:  # noqa: BLE001 — a channel never breaks the loop
        return f"error: {e.__class__.__name__}: {str(e)[:160]}"


def test(cid: int, project: str) -> dict:
    alert = {"id": 0, "severity": "info", "summary": "This is a test of the channel — nothing is wrong.",
             "group": {}, "link": "/?plane=operate&tab=alerts"}
    rule = {"id": 0, "name": "Channel test", "sentence": "Sent from Operate › Alerts › Channels",
            "severity": "info"}
    res = deliver(cid, "test", alert, rule)
    out = {"ok": res == "ok", "detail": res}
    _set_test(cid, out)
    return out


def _http_ok(r: httpx.Response) -> str:
    return "ok" if r.status_code < 300 else f"http {r.status_code}"


def _telegram(cfg, sec, m, alert, rule) -> str:
    text = f"{m['title']}\n{m['text']}\n{m['link']}"
    r = httpx.post(f"https://api.telegram.org/bot{sec.get('bot_token', '')}/sendMessage",
                   json={"chat_id": cfg.get("chat_id"), "text": text[:4000],
                         "disable_web_page_preview": True}, timeout=TIMEOUT)
    return _http_ok(r)


def _slack(cfg, sec, m, alert, rule) -> str:
    color = {"critical": "#d1242f", "warning": "#d4a72c", "info": "#0969da"}.get(m["severity"], "#d4a72c")
    if m["event"] == "resolved":
        color = "#1a7f37"
    r = httpx.post(sec["webhook_url"], json={
        "text": m["title"],
        "attachments": [{"color": color, "text": m["text"],
                         "actions": [{"type": "button", "text": "Open in SOKKAN", "url": m["link"]}]}]},
        timeout=TIMEOUT)
    return _http_ok(r)


def _webhook(cfg, sec, m, alert, rule) -> str:
    body = json.dumps({"event": m["event"], "alert": alert,
                       "rule": {"id": rule.get("id"), "name": rule.get("name"),
                                "severity": rule.get("severity"), "sentence": rule.get("sentence")},
                       "link": m["link"]}, default=str).encode()
    headers = {"content-type": "application/json", "user-agent": "SOKKAN-alerting"}
    if sec.get("secret"):
        headers["x-sokkan-signature"] = "sha256=" + hmac.new(sec["secret"].encode(), body,
                                                             hashlib.sha256).hexdigest()
    r = httpx.post(sec["url"], content=body, headers=headers, timeout=TIMEOUT)
    return _http_ok(r)


def _pagerduty(cfg, sec, m, alert, rule) -> str:
    action = "resolve" if m["event"] == "resolved" else "trigger"
    sev = {"critical": "critical", "warning": "warning", "info": "info"}.get(m["severity"], "warning")
    r = httpx.post("https://events.pagerduty.com/v2/enqueue", json={
        "routing_key": sec["routing_key"], "event_action": action,
        "dedup_key": f"sokkan-{rule.get('id')}-{alert.get('group_key', '')}"[:255],
        "payload": {"summary": (alert.get("summary") or m["title"])[:1000], "severity": sev,
                    "source": "SOKKAN", "custom_details": {"rule": rule.get("sentence"),
                                                           "group": alert.get("group")}},
        "links": [{"href": m["link"], "text": "Open in SOKKAN"}]}, timeout=TIMEOUT)
    return _http_ok(r) if r.status_code != 202 else "ok"


def _email(cfg, sec, m, alert, rule) -> str:
    host = os.environ.get("SOKKAN_SMTP_HOST")
    if not host:
        return "no SMTP server on this instance (SOKKAN_SMTP_HOST)"
    port = int(os.environ.get("SOKKAN_SMTP_PORT") or 587)
    msg = EmailMessage()
    msg["Subject"] = m["title"]
    msg["From"] = os.environ.get("SOKKAN_SMTP_FROM") or os.environ.get("SOKKAN_SMTP_USER") or "sokkan@localhost"
    msg["To"] = cfg.get("to", "")
    msg.set_content(f"{m['text']}\n\n{m['link']}\n")
    with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
        if port != 25:
            s.starttls(context=ssl.create_default_context())
        if os.environ.get("SOKKAN_SMTP_USER"):
            s.login(os.environ["SOKKAN_SMTP_USER"], os.environ.get("SOKKAN_SMTP_PASSWORD", ""))
        s.send_message(msg)
    return "ok"


def teams_card(m: dict, alert: dict) -> dict:
    sev_style = {"critical": "attention", "warning": "warning", "info": "accent"}.get(m["severity"], "warning")
    if m["event"] == "resolved":
        sev_style = "good"
    body = [{"type": "TextBlock", "text": m["title"], "weight": "Bolder", "wrap": True,
             "style": "heading", "color": sev_style},
            {"type": "TextBlock", "text": m["text"], "wrap": True}]
    actions = []
    if m["event"] in ("firing", "renotify") and alert.get("id"):
        data = {"sokkan": "alert", "alert": alert["id"]}
        actions = [{"type": "Action.Execute", "title": t, "verb": v, "data": data}
                   for t, v in (("Ack", "alert.ack"), ("Silence 1 h", "alert.silence"),
                                ("Open incident", "alert.incident"))]
    actions.append({"type": "Action.OpenUrl", "title": "Open in SOKKAN", "url": m["link"]})
    body.append({"type": "ActionSet", "actions": actions})
    return {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5", "body": body, "fallbackText": f"{m['title']} — {m['text']}"}


def _teams(cfg, sec, m, alert, rule) -> str:
    import teams
    from teams import connector, proactive
    if not teams.enabled():
        return "Teams is off on this instance"
    su, conv = proactive._reach(cfg.get("channel_id", ""))
    connector.send(su, conv, connector.card(teams_card(m, alert), m["title"]))
    return "ok"
