"""teams.connector — outbound calls: app tokens (client credentials, single tenant, cached
ENCRYPTED in teams.db) and messages to a Teams conversation (Bot Connector REST API)."""
from __future__ import annotations

import time

import teams
from teams import botauth, store

BOT_SCOPE = "https://api.botframework.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class Error(Exception):
    pass


def app_token(scope: str) -> str:
    """Client-credentials token of the app in the customer's tenant (cached encrypted)."""
    name = f"app:{scope}"
    tok = store.get_token(name)
    if tok:
        return tok
    if teams.configured():
        raise Error("Teams app not configured: " + ", ".join(teams.configured()))
    url = f"{teams.login_url()}/{teams.tenant_id()}/oauth2/v2.0/token"
    with teams.http() as h:
        r = h.post(url, data={"grant_type": "client_credentials", "client_id": teams.app_id(),
                              "client_secret": teams.cfg("APP_PASSWORD"), "scope": scope})
    if r.status_code != 200:
        raise Error(f"token endpoint answered {r.status_code}")
    j = r.json()
    store.put_token(name, j["access_token"], time.time() + float(j.get("expires_in", 3600)))
    return j["access_token"]


def _post(service_url: str, path: str, payload: dict) -> dict:
    if not botauth.service_url_ok(service_url):
        raise Error("refusing to send to a serviceUrl that is not a Microsoft host")
    url = service_url.rstrip("/") + path
    with teams.http() as h:
        r = h.post(url, json=payload,
                   headers={"authorization": f"Bearer {app_token(BOT_SCOPE)}"})
    if r.status_code >= 300:
        raise Error(f"Bot Connector answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return {}


def reply(activity: dict, payload: dict) -> dict:
    """Reply in the thread of ``activity``."""
    conv = (activity.get("conversation") or {}).get("id", "")
    out = {"type": "message", "replyToId": activity.get("id"),
           "conversation": activity.get("conversation"), "from": activity.get("recipient"),
           "recipient": activity.get("from"), **payload}
    return _post(activity.get("serviceUrl", ""),
                 f"/v3/conversations/{conv}/activities/{activity.get('id', '')}", out)


def send(service_url: str, conversation_id: str, payload: dict) -> dict:
    """Proactive message to a conversation the bot is part of."""
    return _post(service_url, f"/v3/conversations/{conversation_id}/activities",
                 {"type": "message", **payload})


def text(t: str) -> dict:
    return {"type": "message", "textFormat": "markdown", "text": t}


def card(c: dict, summary: str = "") -> dict:
    return {"type": "message", "summary": summary or "SOKKAN",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                             "content": c}]}
