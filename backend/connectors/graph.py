"""connectors.graph — Microsoft 365 through Microsoft Graph, DELEGATED (the person's consent).

* OAuth 2.0 authorization code + PKCE (S256) against the customer's tenant, a confidential
  client (the instance's application: client id + secret typed by the admin, encrypted).
  The refresh token is stored per person, encrypted; access tokens are cached encrypted and
  refreshed 2 minutes before they expire. Every call reads or writes AS the person: Exchange
  and SharePoint apply that person's own rights (a shared mailbox the person cannot open is
  not opened by SOKKAN either).
* Scopes (`scopes`): Mail.ReadWrite (drafts), Mail.Read.Shared, Calendars.ReadWrite,
  Files.ReadWrite.All, Sites.Read.All, User.Read, offline_access — Mail.Send only when the
  instance allows sending.
* ``SOKKAN_CONNECTORS_GRAPH_URL`` / ``SOKKAN_CONNECTORS_LOGIN_URL`` point at another Graph
  (tests: a simulated one; sovereign clouds). Defaults: graph.microsoft.com,
  login.microsoftonline.com.

Paths of files: ``/me/<path>`` is the person's OneDrive; ``/<alias>/<path>`` a SharePoint
document library the admin declared (`settings.graph_sites`: alias → site id).
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import time
from urllib.parse import quote, urlencode

from . import ConnectorError, http, store

BASE_SCOPES = ("offline_access", "User.Read", "Mail.ReadWrite", "Mail.Read.Shared",
               "Calendars.ReadWrite", "Files.ReadWrite.All", "Sites.Read.All")


def graph_url() -> str:
    return (os.environ.get("SOKKAN_CONNECTORS_GRAPH_URL") or "https://graph.microsoft.com/v1.0").rstrip("/")


def login_url() -> str:
    return (os.environ.get("SOKKAN_CONNECTORS_LOGIN_URL") or "https://login.microsoftonline.com").rstrip("/")


def scopes(allow_send: bool) -> str:
    return " ".join(BASE_SCOPES + (("Mail.Send",) if allow_send else ()))


# ---- OAuth ----------------------------------------------------------------------------------
def _cfg(settings: dict) -> dict:
    g = (settings.get("providers") or {}).get("graph") or {}
    tenant, client = (g.get("tenant") or "").strip(), (g.get("client_id") or "").strip()
    if not tenant or not client or not store.has_instance_secret("graph_client_secret"):
        raise ConnectorError("Microsoft 365 n'est pas configuré (Réglages › Connecteurs).", 409,
                             "not_configured")
    return {"tenant": tenant, "client_id": client}


def authorize_url(settings: dict, email: str, redirect_uri: str) -> str:
    c = _cfg(settings)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = store.oauth_begin(email, verifier)
    q = {"client_id": c["client_id"], "response_type": "code", "redirect_uri": redirect_uri,
         "response_mode": "query", "scope": scopes(bool(settings.get("allow_send", True))),
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
         "login_hint": email, "prompt": "select_account"}
    return f"{login_url()}/{quote(c['tenant'])}/oauth2/v2.0/authorize?{urlencode(q)}"


def _token(settings: dict, data: dict) -> dict:
    c = _cfg(settings)
    with http() as h:
        r = h.post(f"{login_url()}/{quote(c['tenant'])}/oauth2/v2.0/token",
                   data={"client_id": c["client_id"],
                         "client_secret": store.instance_secret("graph_client_secret"), **data})
    if r.status_code != 200:
        # never the body: it may echo the code or the request
        raise ConnectorError(f"Microsoft a refusé le jeton ({r.status_code}).", 502, "token")
    return r.json()


def finish(settings: dict, state: str, code: str, redirect_uri: str, expect: str) -> str:
    """The callback: exchange the code (PKCE), read who consented, store the refresh token.
    Returns the SOKKAN email the connection belongs to."""
    p = store.oauth_take(state)
    if not p:
        raise ConnectorError("Lien de connexion expiré ou déjà utilisé : recommencez.", 400, "state")
    if p["email"] != (expect or "").lower():
        # the browser that came back is not the person who started: the code is not redeemed
        raise ConnectorError("Ce consentement a été commencé par une autre personne.", 403, "mismatch")
    j = _token(settings, {"grant_type": "authorization_code", "code": code,
                          "redirect_uri": redirect_uri, "code_verifier": p["verifier"],
                          "scope": scopes(bool(settings.get("allow_send", True)))})
    if not j.get("refresh_token"):
        raise ConnectorError("Microsoft n'a pas donné d'accès durable (offline_access).", 502, "token")
    with http() as h:
        me = h.get(graph_url() + "/me", headers={"authorization": f"Bearer {j['access_token']}"})
    login = ""
    if me.status_code == 200:
        mj = me.json()
        login = (mj.get("mail") or mj.get("userPrincipalName") or "").lower()
    store.save_account(p["email"], "graph", login=login, secret=j["refresh_token"],
                       scopes=j.get("scope") or "")
    store.update_tokens(p["email"], "graph", access=j["access_token"],
                        expires_at=time.time() + float(j.get("expires_in", 3600)))
    return p["email"]


def access_token(settings: dict, acc: dict) -> str:
    if acc.get("access_enc") and float(acc.get("access_exp") or 0) - 120 > time.time():
        return store.dec(acc["access_enc"])
    j = _token(settings, {"grant_type": "refresh_token", "refresh_token": store.secret_of(acc),
                          "scope": scopes(bool(settings.get("allow_send", True)))})
    store.update_tokens(acc["email"], "graph", access=j["access_token"],
                        expires_at=time.time() + float(j.get("expires_in", 3600)),
                        refresh=j.get("refresh_token"))
    return j["access_token"]


class _Client:
    def __init__(self, settings: dict, acc: dict):
        self.settings, self.acc = settings, acc

    def call(self, method: str, path: str, *, params=None, json=None, content=None,
             headers=None, raw: bool = False):
        tok = access_token(self.settings, self.acc)
        hd = {"authorization": f"Bearer {tok}", **(headers or {})}
        with http() as h:
            r = h.request(method, graph_url() + path, params=params, json=json, content=content,
                          headers=hd)
        if r.status_code == 404:
            raise ConnectorError("Introuvable chez Microsoft 365.", 404, "not_found")
        if r.status_code in (401, 403):
            raise ConnectorError("Microsoft 365 refuse l'accès (droits de la personne).", 403, "denied")
        if r.status_code >= 300:
            raise ConnectorError(f"Microsoft 365 a répondu {r.status_code}.", 502, "upstream")
        if raw:
            return r.content
        return r.json() if r.content else {}


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _addr(a: dict | None) -> str:
    e = (a or {}).get("emailAddress") or {}
    return e.get("address") or ""


def _msg(m: dict, body: bool = False) -> dict:
    out = {"id": m.get("id", ""), "thread_id": m.get("conversationId", ""),
           "subject": m.get("subject") or "", "from": _addr(m.get("from")),
           "to": [_addr(x) for x in m.get("toRecipients") or []],
           "cc": [_addr(x) for x in m.get("ccRecipients") or []],
           "date": m.get("receivedDateTime") or m.get("sentDateTime") or "",
           "snippet": (m.get("bodyPreview") or "")[:300], "is_draft": bool(m.get("isDraft")),
           "has_attachments": bool(m.get("hasAttachments"))}
    if body:
        b = m.get("body") or {}
        out["body"] = _strip_html(b.get("content", "")) if b.get("contentType") == "html" else b.get("content", "")
    return out


def _box(mailbox: str) -> str:
    return f"/users/{quote(mailbox)}" if mailbox else "/me"


# ---- mail -------------------------------------------------------------------------------
class Mail:
    def __init__(self, settings: dict, acc: dict):
        self.c = _Client(settings, acc)

    def list(self, folder: str = "inbox", mailbox: str = "", query: str = "", limit: int = 20) -> list[dict]:
        params = {"$top": str(max(1, min(limit, 50))),
                  "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,"
                             "receivedDateTime,bodyPreview,isDraft,hasAttachments"}
        if query:
            params["$search"] = f'"{query.replace(chr(34), "")}"'
        else:
            params["$orderby"] = "receivedDateTime desc"
        j = self.c.call("GET", f"{_box(mailbox)}/mailFolders/{quote(folder or 'inbox')}/messages",
                        params=params)
        return [_msg(m) for m in j.get("value", [])]

    def thread(self, thread_id: str, mailbox: str = "") -> list[dict]:
        tid = thread_id.replace("'", "''")
        j = self.c.call("GET", f"{_box(mailbox)}/messages",
                        params={"$filter": f"conversationId eq '{tid}'", "$top": "50",
                                "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,"
                                           "receivedDateTime,sentDateTime,body,bodyPreview,isDraft,"
                                           "hasAttachments"})
        msgs = [_msg(m, body=True) for m in j.get("value", [])]
        return sorted(msgs, key=lambda m: m["date"])

    def draft_reply(self, message_id: str, body: str, mailbox: str = "", reply_all: bool = False) -> dict:
        verb = "createReplyAll" if reply_all else "createReply"
        d = self.c.call("POST", f"{_box(mailbox)}/messages/{quote(message_id)}/{verb}", json={})
        did = d.get("id", "")
        self.c.call("PATCH", f"{_box(mailbox)}/messages/{quote(did)}",
                    json={"body": {"contentType": "text", "content": body}})
        return {"draft_id": did, "subject": d.get("subject") or "",
                "to": [_addr(x) for x in d.get("toRecipients") or []],
                "cc": [_addr(x) for x in d.get("ccRecipients") or []], "mailbox": mailbox}

    def draft_new(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  mailbox: str = "") -> dict:
        rcpt = lambda xs: [{"emailAddress": {"address": a}} for a in xs]  # noqa: E731
        d = self.c.call("POST", f"{_box(mailbox)}/messages",
                        json={"subject": subject, "body": {"contentType": "text", "content": body},
                              "toRecipients": rcpt(to), "ccRecipients": rcpt(cc or [])})
        return {"draft_id": d.get("id", ""), "subject": subject, "to": to, "cc": cc or [],
                "mailbox": mailbox}

    def get_draft(self, draft_id: str, mailbox: str = "") -> dict:
        m = self.c.call("GET", f"{_box(mailbox)}/messages/{quote(draft_id)}")
        if not m.get("isDraft"):
            raise ConnectorError("Ce message n'est pas un brouillon.", 409, "not_draft")
        return _msg(m, body=True)

    def send_draft(self, draft_id: str, mailbox: str = "") -> dict:
        self.c.call("POST", f"{_box(mailbox)}/messages/{quote(draft_id)}/send")
        return {"sent": True}


# ---- calendar ----------------------------------------------------------------------------
def _when(v: dict | None) -> str:
    s = (v or {}).get("dateTime") or ""
    return s if not s or s.endswith("Z") or "+" in s[10:] else s.split(".")[0] + "Z"


def _event(e: dict, detail: bool = False) -> dict:
    out = {"id": e.get("id", ""), "subject": e.get("subject") or "", "start": _when(e.get("start")),
           "end": _when(e.get("end")), "location": ((e.get("location") or {}).get("displayName") or ""),
           "organizer": _addr(e.get("organizer")), "online": bool(e.get("isOnlineMeeting")),
           "show_as": e.get("showAs") or "",
           "attendees": [_addr(a) for a in e.get("attendees") or []]}
    if detail:
        b = e.get("body") or {}
        out["body"] = _strip_html(b.get("content", "")) if b.get("contentType") == "html" else b.get("content", "")
        out["attachments"] = [{"name": a.get("name", ""), "size": a.get("size", 0)}
                              for a in e.get("attachments") or []]
    return out


class Calendar:
    def __init__(self, settings: dict, acc: dict):
        self.c = _Client(settings, acc)

    def events(self, start: str, end: str) -> list[dict]:
        j = self.c.call("GET", "/me/calendarView",
                        params={"startDateTime": start, "endDateTime": end, "$top": "100",
                                "$orderby": "start/dateTime"},
                        headers={"Prefer": 'outlook.timezone="UTC"'})
        return [_event(e) for e in j.get("value", [])]

    def event(self, event_id: str) -> dict:
        e = self.c.call("GET", f"/me/events/{quote(event_id)}",
                        params={"$expand": "attachments($select=name,size)"},
                        headers={"Prefer": 'outlook.timezone="UTC"'})
        return _event(e, detail=True)

    def busy_of(self, attendees: list[str], start: str, end: str) -> dict[str, list[tuple[str, str]]]:
        """Busy intervals of colleagues (getSchedule: what their own calendar shares)."""
        if not attendees:
            return {}
        j = self.c.call("POST", "/me/calendar/getSchedule",
                        json={"schedules": attendees, "startTime": {"dateTime": start, "timeZone": "UTC"},
                              "endTime": {"dateTime": end, "timeZone": "UTC"},
                              "availabilityViewInterval": 15},
                        headers={"Prefer": 'outlook.timezone="UTC"'})
        out: dict[str, list[tuple[str, str]]] = {}
        for s in j.get("value", []):
            out[s.get("scheduleId", "")] = [(_when(i.get("start")), _when(i.get("end")))
                                            for i in s.get("scheduleItems") or []
                                            if (i.get("status") or "busy") != "free"]
        return out

    def create(self, subject: str, start: str, end: str, attendees: list[str], location: str = "",
               body: str = "", online: bool = False) -> dict:
        e = self.c.call("POST", "/me/events", json={
            "subject": subject, "body": {"contentType": "text", "content": body},
            "start": {"dateTime": start.rstrip("Z"), "timeZone": "UTC"},
            "end": {"dateTime": end.rstrip("Z"), "timeZone": "UTC"},
            "location": {"displayName": location},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees],
            "isOnlineMeeting": bool(online)})
        return {"id": e.get("id", ""), "web_link": e.get("webLink", "")}


# ---- files (OneDrive / SharePoint) ---------------------------------------------------------
class Files:
    def __init__(self, settings: dict, acc: dict):
        self.c = _Client(settings, acc)
        self.sites = settings.get("graph_sites") or {}

    def _drive(self, path: str) -> tuple[str, str]:
        parts = [p for p in (path or "/").split("/") if p]
        alias = parts[0] if parts else "me"
        rest = "/".join(parts[1:])
        if alias == "me":
            return "/me/drive", rest
        site = self.sites.get(alias)
        if not site:
            raise ConnectorError(f"Bibliothèque inconnue : « {alias} » (aliases : me, "
                                 f"{', '.join(self.sites) or 'aucun'}).", 404, "not_found")
        return f"/sites/{quote(site)}/drive", rest

    @staticmethod
    def _item(prefix: str, it: dict) -> dict:
        return {"path": f"{prefix}/{it.get('name', '')}".replace("//", "/"), "name": it.get("name", ""),
                "is_dir": "folder" in it, "size": it.get("size", 0),
                "modified": it.get("lastModifiedDateTime", ""), "id": it.get("id", "")}

    def list(self, path: str) -> list[dict]:
        drive, rest = self._drive(path)
        loc = f"{drive}/root:/{quote(rest)}:/children" if rest else f"{drive}/root/children"
        j = self.c.call("GET", loc, params={"$top": "200"})
        prefix = "/" + "/".join(p for p in (path or "/me").split("/") if p)
        return [self._item(prefix, it) for it in j.get("value", [])]

    def search(self, query: str, path: str = "/me") -> list[dict]:
        drive, _ = self._drive(path)
        alias = ([p for p in (path or "/me").split("/") if p] or ["me"])[0]
        j = self.c.call("GET", f"{drive}/root/search(q='{quote(query.replace(chr(39), ''))}')",
                        params={"$top": "50"})
        out = []
        for it in j.get("value", []):
            parent = ((it.get("parentReference") or {}).get("path") or "").split("root:", 1)[-1]
            out.append(self._item(f"/{alias}{parent}", it))
        return out

    def read(self, path: str) -> bytes:
        drive, rest = self._drive(path)
        if not rest:
            raise ConnectorError("Indiquez un fichier.", 400, "path")
        return self.c.call("GET", f"{drive}/root:/{quote(rest)}:/content", raw=True)

    def write(self, path: str, data: bytes) -> dict:
        drive, rest = self._drive(path)
        it = self.c.call("PUT", f"{drive}/root:/{quote(rest)}:/content", content=data,
                         headers={"content-type": "application/octet-stream"},
                         params={"@microsoft.graph.conflictBehavior": "rename"})
        return {"path": path, "web_url": it.get("webUrl", ""), "id": it.get("id", "")}
