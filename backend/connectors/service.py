"""connectors.service — the operations, decided once for the MCP server and the API.

Every read goes: feature on → a person behind the session → the provider is enabled by the
admin and connected by the person → the project may reach that mailbox / folder → the
project's classification allows the zone of the session's model (policy) → the provider is
called. Every write that leaves the person's own drafts becomes an ACTION: `propose` stores
it and journals the request; only `decide` (the API, on the person's click) runs it."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from . import FAMILIES, PROVIDERS, SERVER, ConnectorError, enabled, policy, store

TEXT_MAX = 60_000
WRITE_MAX = 5 * 1024 * 1024
DEFAULT = {
    "providers": {
        "graph": {"enabled": False, "tenant": "", "client_id": ""},
        "imap": {"enabled": False, "imap_host": "", "imap_port": 993, "smtp_host": "", "smtp_port": 465,
                 "security": "ssl"},
        "caldav": {"enabled": False, "url": ""},
        "webdav": {"enabled": False, "url": ""},
        "folder": {"enabled": False, "root": "", "write_root": ""},
        "ics": {"enabled": True},
        "demo": {"enabled": False},
    },
    "allow_send": True,        # off: no mail is ever sent from SOKKAN (drafts only)
    "scopes": {},              # {project | "*": {shared_mailboxes, sites, folders}}
    "graph_sites": {},         # alias → SharePoint site id
}
PROVIDER_KEYS = {
    "graph": ("enabled", "tenant", "client_id"),
    "imap": ("enabled", "imap_host", "imap_port", "smtp_host", "smtp_port", "security", "smtp_security",
             "smtp_auth"),
    "caldav": ("enabled", "url"), "webdav": ("enabled", "url"),
    "folder": ("enabled", "root", "write_root"), "ics": ("enabled",), "demo": ("enabled",),
}


# ---- settings -----------------------------------------------------------------------------
def settings() -> dict:
    s = json.loads(json.dumps(DEFAULT))
    got = store.get_setting("settings", {}) or {}
    for k, v in (got.get("providers") or {}).items():
        if k in s["providers"] and isinstance(v, dict):
            s["providers"][k].update({kk: vv for kk, vv in v.items() if kk in PROVIDER_KEYS[k]})
    for k in ("allow_send", "scopes", "graph_sites"):
        if k in got:
            s[k] = got[k]
    if os.environ.get("SOKKAN_CONNECTORS_DEMO") == "1":
        s["providers"]["demo"]["enabled"] = True
    return s


def save_settings(new: dict, by: str) -> dict:
    cur = settings()
    out = {"providers": {}, "allow_send": cur["allow_send"], "scopes": cur["scopes"],
           "graph_sites": cur["graph_sites"]}
    for k, v in cur["providers"].items():
        upd = ((new.get("providers") or {}).get(k) or {})
        out["providers"][k] = {**v, **{kk: vv for kk, vv in upd.items() if kk in PROVIDER_KEYS[k]}}
    if "allow_send" in new:
        out["allow_send"] = bool(new["allow_send"])
    if "scopes" in new:
        sc = new["scopes"] or {}
        if not isinstance(sc, dict):
            raise ConnectorError("scopes: un objet {projet: {shared_mailboxes, sites, folders}}")
        out["scopes"] = {str(p): {k: [str(x).strip() for x in (v or {}).get(k) or [] if str(x).strip()]
                                  for k in ("shared_mailboxes", "sites", "folders")}
                         for p, v in sc.items()}
    if "graph_sites" in new:
        gs = new["graph_sites"] or {}
        if not all(re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", a) and a != "me" for a in gs):
            raise ConnectorError("graph_sites : des alias courts en minuscules (pas « me »)")
        out["graph_sites"] = {a: str(v).strip() for a, v in gs.items()}
    for k in ("folder",):
        for f in ("root", "write_root"):
            v = out["providers"][k].get(f) or ""
            if v and not os.path.isabs(v):
                raise ConnectorError(f"{k}.{f} : un chemin absolu sur la machine")
    for k in ("caldav", "webdav"):
        u = out["providers"][k].get("url") or ""
        if u and not re.match(r"^https?://", u):
            raise ConnectorError(f"{k}.url : une adresse http(s)")
    if "graph_client_secret" in new:
        store.put_instance_secret("graph_client_secret", str(new["graph_client_secret"] or ""), by)
    # a provider turned off: every connection to it is dropped (revocation is not optional)
    dropped = {}
    for k, v in out["providers"].items():
        if cur["providers"][k].get("enabled") and not v.get("enabled") and k in ("graph", "imap", "caldav",
                                                                                  "webdav", "demo"):
            dropped[k] = store.delete_provider(k)
    store.put_setting("settings", out, by)
    import audit
    audit.log(by, "connectors.settings", "connectors",
              json.dumps({"enabled": [k for k, v in out["providers"].items() if v.get("enabled")],
                          "allow_send": out["allow_send"], "dropped": dropped})[:500])
    return settings()


def public_settings() -> dict:
    s = settings()
    s["graph_client_secret_set"] = store.has_instance_secret("graph_client_secret")
    return s


# ---- the caller ---------------------------------------------------------------------------
class Ctx:
    """Who asks, from where: the person (never chosen by the model), the session, the project."""

    def __init__(self, email: str, session_id: str = "", project: str = "default", zone: str | None = None,
                 agent: str = ""):
        if "@" not in (email or ""):
            raise ConnectorError("Cette session n'a pas de personne identifiée : les connecteurs "
                                 "agissent toujours au nom de quelqu'un.", 403, "no_person")
        self.email, self.session_id = email.lower().strip(), session_id
        self.project = project or "default"
        self.agent = agent
        self._zone = zone

    @property
    def zone(self) -> str:
        if self._zone is None:
            self._zone = policy.session_zone(self.session_id, self.project)
        return self._zone

    @classmethod
    def from_env(cls) -> "Ctx":
        return cls(os.environ.get("SOKKAN_SESSION_USER") or "", os.environ.get("SOKKAN_SESSION_ID") or "",
                   os.environ.get("SOKKAN_SESSION_PROJECT") or "default",
                   agent=os.environ.get("SOKKAN_AGENT_NAME") or "")


def _on() -> None:
    if not enabled():
        raise ConnectorError("Les connecteurs ne sont pas activés sur cette instance.", 404, "off")


def status(ctx: Ctx) -> dict:
    _on()
    s = settings()
    mine = {a["provider"]: a for a in store.accounts_of(ctx.email)}
    fam = {}
    for f in FAMILIES:
        fam[f] = [{"provider": p, "enabled": bool(s["providers"].get(p, {}).get("enabled")),
                   "connected": p in mine or p in ("folder", "ics"),
                   "login": (mine.get(p) or {}).get("login", "")}
                  for p in PROVIDERS[f] if s["providers"].get(p, {}).get("enabled")]
    ok, why = policy.content_allowed(ctx.project, ctx.zone)
    return {"families": fam, "content_allowed": ok, "reason": why, "zone": ctx.zone or "unknown",
            "allow_send": bool(s["allow_send"]), "pending_actions": store.pending_count(ctx.email),
            "scopes": policy.scopes(s, ctx.project)}


# ---- provider resolution ------------------------------------------------------------------
_ORDER = {"mail": ("graph", "imap", "demo"), "calendar": ("graph", "caldav", "ics", "demo"),
          "files": ("graph", "webdav", "folder", "demo")}


def client(ctx: Ctx, family: str, provider: str = ""):
    """(provider name, client) for the person: the one asked for, else the first enabled and
    connected in the family's order."""
    s = settings()
    want = [provider] if provider else list(_ORDER[family])
    if provider and provider not in PROVIDERS[family]:
        raise ConnectorError(f"{family} : fournisseurs possibles {', '.join(PROVIDERS[family])}", 400, "provider")
    last = None
    for p in want:
        if not s["providers"].get(p, {}).get("enabled"):
            last = ConnectorError(f"« {p} » n'est pas activé sur cette instance.", 409, "disabled")
            continue
        try:
            return p, _build(s, ctx, family, p)
        except ConnectorError as e:
            last = e
            if provider:
                raise
    raise last or ConnectorError(f"Aucun connecteur {family} n'est activé.", 409, "disabled")


def _build(s: dict, ctx: Ctx, family: str, p: str):
    from . import caldav, demo, graph, imapmail, webdav
    if p == "folder":
        return webdav.Folder(s)
    if p == "ics":
        return caldav.ICS(ctx.email, ctx.project)
    acc = store.account(ctx.email, p)
    if acc is None:
        raise ConnectorError(f"Vous n'avez pas connecté « {p} » (Réglages › Connecteurs).", 409, "not_connected")
    store.touch(ctx.email, p)
    if p == "demo":
        return {"mail": demo.Mail, "calendar": demo.Calendar, "files": demo.Files}[family](ctx.email, acc["login"])
    if p == "graph":
        return {"mail": graph.Mail, "calendar": graph.Calendar, "files": graph.Files}[family](s, acc)
    pw = store.secret_of(acc)
    if p == "imap" and family == "mail":
        return imapmail.Mail(s, acc, pw)
    if p == "caldav" and family == "calendar":
        return caldav.Calendar(s, acc, pw)
    if p == "webdav" and family == "files":
        return webdav.WebDAV(s, acc, pw)
    raise ConnectorError(f"« {p} » ne sert pas « {family} ».", 400, "provider")


def _login(ctx: Ctx, provider: str) -> str:
    acc = store.account(ctx.email, provider) or {}
    return acc.get("login") or ctx.email


def _read(ctx: Ctx) -> None:
    _on()
    policy.check_content(ctx.project, ctx.zone)


# ---- mail -----------------------------------------------------------------------------------
def mail_list(ctx: Ctx, folder: str = "inbox", mailbox: str = "", query: str = "", limit: int = 20,
              provider: str = "") -> dict:
    _read(ctx)
    p, c = client(ctx, "mail", provider)
    policy.check_mailbox(settings(), ctx.project, _login(ctx, p), mailbox)
    return {"provider": p, "messages": c.list(folder, mailbox, query, limit)}


def mail_thread(ctx: Ctx, thread_id: str, mailbox: str = "", provider: str = "") -> dict:
    _read(ctx)
    p, c = client(ctx, "mail", provider)
    policy.check_mailbox(settings(), ctx.project, _login(ctx, p), mailbox)
    msgs = c.thread(thread_id, mailbox)
    for m in msgs:
        if len(m.get("body") or "") > TEXT_MAX // 4:
            m["body"] = m["body"][:TEXT_MAX // 4] + "\n[… coupé]"
    return {"provider": p, "messages": msgs}


def mail_draft(ctx: Ctx, body: str, reply_to: str = "", to: list[str] | None = None, subject: str = "",
               cc: list[str] | None = None, mailbox: str = "", reply_all: bool = False, provider: str = "") -> dict:
    """A draft in the person's own mailbox (or a shared one they may write in): never sent."""
    _read(ctx)
    p, c = client(ctx, "mail", provider)
    policy.check_mailbox(settings(), ctx.project, _login(ctx, p), mailbox)
    if not (body or "").strip():
        raise ConnectorError("Le brouillon est vide.", 400, "empty")
    if reply_to:
        d = c.draft_reply(reply_to, body, mailbox, reply_all)
    else:
        if not to or not subject:
            raise ConnectorError("Un nouveau message demande des destinataires et un objet.", 400, "fields")
        d = c.draft_new(list(to), subject, body, list(cc or []), mailbox)
    import audit
    audit.log(ctx.email, "connectors.mail.draft", f"{p}:{mailbox or 'me'}", d.get("subject", "")[:200],
              project=ctx.project)
    d.update(provider=p, note="Brouillon enregistré dans la boîte de la personne. Rien n'est envoyé : "
                              "pour l'envoyer, proposez l'envoi (mail_propose_send) — la personne valide.")
    return d


def _digest(d: dict) -> str:
    return hashlib.sha256(json.dumps({k: d.get(k) for k in ("subject", "to", "cc", "body")},
                                     sort_keys=True).encode()).hexdigest()


# ---- calendar -------------------------------------------------------------------------------
def _range(start: str, end: str, days: int = 1) -> tuple[str, str]:
    from .caldav import _iso, parse_iso
    s = parse_iso(start) if start else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    e = parse_iso(end) if end else s + timedelta(days=days)
    if e <= s:
        raise ConnectorError("La fin doit suivre le début.", 400, "date")
    if e - s > timedelta(days=62):
        raise ConnectorError("Deux mois au plus par lecture.", 400, "date")
    return _iso(s), _iso(e)


def calendar_events(ctx: Ctx, start: str = "", end: str = "", provider: str = "") -> dict:
    _read(ctx)
    p, c = client(ctx, "calendar", provider)
    s, e = _range(start, end)
    return {"provider": p, "start": s, "end": e, "events": c.events(s, e)}


def calendar_free_slots(ctx: Ctx, duration_min: int = 30, start: str = "", end: str = "",
                        attendees: list[str] | None = None, day_start: int = 8, day_end: int = 18,
                        provider: str = "", max_slots: int = 5) -> dict:
    """Free slots of the person (and of colleagues where the provider shares their busy times),
    on working days, between day_start and day_end (Europe/Zurich)."""
    from zoneinfo import ZoneInfo

    from .caldav import _iso, parse_iso
    _read(ctx)
    p, c = client(ctx, "calendar", provider)
    s, e = _range(start, end, days=5)
    busy = [(parse_iso(x["start"]), parse_iso(x["end"])) for x in c.events(s, e)
            if (x.get("show_as") or "busy") not in ("free",)]
    others = c.busy_of(list(attendees or []), s, e) if attendees else {}
    for v in others.values():
        busy += [(parse_iso(a), parse_iso(b)) for a, b in v]
    tz, dur = ZoneInfo("Europe/Zurich"), timedelta(minutes=max(15, min(int(duration_min), 480)))
    cur = parse_iso(s).astimezone(tz)
    cur = cur.replace(minute=(cur.minute // 15) * 15, second=0, microsecond=0)
    stop, out = parse_iso(e).astimezone(tz), []
    while cur + dur <= stop and len(out) < max_slots:
        if cur.weekday() < 5 and cur.hour >= day_start and (cur + dur).hour * 60 + (cur + dur).minute <= day_end * 60 \
                and (cur + dur).date() == cur.date() and cur >= datetime.now(tz) - timedelta(minutes=1):
            if not any(a < cur + dur and b > cur for a, b in busy):
                out.append({"start": _iso(cur), "end": _iso(cur + dur), "local": cur.strftime("%a %d.%m %H:%M")})
                cur += dur
                continue
        cur += timedelta(minutes=15)
    return {"provider": p, "slots": out, "attendees_checked": sorted(others),
            "attendees_unknown": sorted(set(attendees or []) - set(others))}


def calendar_prepare(ctx: Ctx, event_id: str, provider: str = "") -> dict:
    """A meeting and what goes with it: its detail, its attachments, the recent mails that
    mention it (best effort, the person's own mailbox)."""
    _read(ctx)
    p, c = client(ctx, "calendar", provider)
    ev = c.event(event_id)
    related = []
    words = [w for w in re.findall(r"[^\W\d_]{4,}", ev.get("subject") or "")][:4]
    if words:
        try:
            mp, mc = client(ctx, "mail")
            seen = set()
            for w in sorted(words, key=len, reverse=True)[:2]:
                for m in mc.list("inbox", "", w, 5):
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        related.append(m)
        except ConnectorError:
            pass
    return {"provider": p, "event": ev, "related_mail": related}


# ---- files ----------------------------------------------------------------------------------
def files_list(ctx: Ctx, path: str = "/", provider: str = "") -> dict:
    _read(ctx)
    p, c = client(ctx, "files", provider)
    policy.check_path(settings(), ctx.project, p, path)
    return {"provider": p, "path": path, "items": c.list(path)}


def files_search(ctx: Ctx, query: str, path: str = "/", provider: str = "") -> dict:
    _read(ctx)
    p, c = client(ctx, "files", provider)
    policy.check_path(settings(), ctx.project, p, path)
    s = settings()
    items = [i for i in c.search(query, path) if _path_ok(s, ctx.project, p, i["path"])]
    return {"provider": p, "items": items}


def _path_ok(s, project, p, path) -> bool:
    try:
        policy.check_path(s, project, p, path)
        return True
    except ConnectorError:
        return False


def to_text(name: str, data: bytes) -> tuple[str, str]:
    ext = os.path.splitext(name)[1].lower()
    if ext in (".docx", ".pptx", ".xlsx", ".pdf"):
        import doctext
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "f" + ext)
            with open(fp, "wb") as f:
                f.write(data)
            return doctext.extract(fp, ext)
    if b"\x00" in data[:4096]:
        return "", "unavailable"
    import doctext
    return doctext.decode_text(data), "ok"


def files_read(ctx: Ctx, path: str, provider: str = "") -> dict:
    _read(ctx)
    p, c = client(ctx, "files", provider)
    policy.check_path(settings(), ctx.project, p, path)
    data = c.read(path)
    text, state = to_text(path, data)
    cut = len(text) > TEXT_MAX
    return {"provider": p, "path": path, "size": len(data), "state": state,
            "text": text[:TEXT_MAX] + ("\n[… coupé]" if cut else ""), "truncated": cut}


# ---- actions (cards) ------------------------------------------------------------------------
def _hitl_requested(a: dict, ctx: Ctx) -> None:
    import hitl_journal
    hitl_journal.requested(f"conn:{a['id']}", "connector", session_id=ctx.session_id,
                           tool=f"{SERVER}__{a['kind']}", title=a["title"], input_data=a["preview"],
                           owner=ctx.email, agent_run=ctx.agent)
    import audit
    audit.log(ctx.email, f"connectors.{a['kind']}.proposed", f"action:{a['id']}", a["title"][:300],
              project=ctx.project)


def _card(a: dict) -> dict:
    return {"action_id": a["id"], "status": "pending", "title": a["title"],
            "expires_at": a["expires_at"],
            "note": "Carte d'accord créée : rien n'est fait tant que la personne n'a pas validé dans "
                    "SOKKAN (Connecteurs › À valider). Ne la relancez pas, attendez sa décision."}


def propose_send(ctx: Ctx, draft_id: str, mailbox: str = "", provider: str = "") -> dict:
    _read(ctx)
    s = settings()
    if not s["allow_send"]:
        raise ConnectorError("L'envoi de mails depuis SOKKAN est désactivé sur cette instance : le "
                             "brouillon attend dans la boîte de la personne.", 403, "send_off")
    p, c = client(ctx, "mail", provider)
    policy.check_mailbox(s, ctx.project, _login(ctx, p), mailbox)
    d = c.get_draft(draft_id, mailbox)
    preview = {"provider": p, "mailbox": mailbox or _login(ctx, p), "subject": d.get("subject", ""),
               "to": d.get("to", []), "cc": d.get("cc", []), "body": (d.get("body") or "")[:4000]}
    title = f"Envoyer « {preview['subject'][:120]} » à {', '.join(preview['to'][:3]) or '—'}"
    a = store.add_action(kind="mail.send", provider=p, owner=ctx.email, session_id=ctx.session_id,
                         project=ctx.project, title=title, preview=preview,
                         payload={"draft_id": draft_id, "mailbox": mailbox, "digest": _digest(d)})
    _hitl_requested(a, ctx)
    return _card(a)


def propose_event(ctx: Ctx, subject: str, start: str, end: str, attendees: list[str] | None = None,
                  location: str = "", body: str = "", online: bool = False, provider: str = "") -> dict:
    _read(ctx)
    from .caldav import _iso, parse_iso
    p, _c = client(ctx, "calendar", provider)
    if p == "ics":
        raise ConnectorError("Un agenda ICS est en lecture seule.", 409, "read_only")
    s, e = parse_iso(start), parse_iso(end)
    if e <= s:
        raise ConnectorError("La fin doit suivre le début.", 400, "date")
    preview = {"provider": p, "subject": subject[:300], "start": _iso(s), "end": _iso(e),
               "attendees": list(attendees or [])[:50], "location": location[:300], "body": body[:4000],
               "online": bool(online)}
    title = f"Créer « {subject[:120]} » le {s.astimezone().strftime('%d.%m %H:%M')}" + (
        f" avec {len(preview['attendees'])} participant(s)" if preview["attendees"] else "")
    a = store.add_action(kind="calendar.create", provider=p, owner=ctx.email, session_id=ctx.session_id,
                         project=ctx.project, title=title, preview=preview, payload=preview)
    _hitl_requested(a, ctx)
    return _card(a)


def propose_write(ctx: Ctx, path: str, content: str, provider: str = "") -> dict:
    _read(ctx)
    p, _c = client(ctx, "files", provider)
    policy.check_path(settings(), ctx.project, p, path)
    data = (content or "").encode()
    if not data:
        raise ConnectorError("Le fichier est vide.", 400, "empty")
    if len(data) > WRITE_MAX:
        raise ConnectorError("5 Mo au plus.", 413, "too_large")
    preview = {"provider": p, "path": path, "size": len(data), "excerpt": content[:2000],
               "sha256": hashlib.sha256(data).hexdigest()}
    title = f"Déposer « {path.rsplit('/', 1)[-1][:120]} » dans {path.rsplit('/', 1)[0] or '/'} ({p})"
    a = store.add_action(kind="files.write", provider=p, owner=ctx.email, session_id=ctx.session_id,
                         project=ctx.project, title=title, preview=preview,
                         payload={"path": path, "content": content})
    _hitl_requested(a, ctx)
    return _card(a)


def decide(aid: str, approver: str, approve: bool, reason: str = "") -> dict:
    """The person's decision on a card (the API only). Only the person whose account acts may
    approve; a card is decided once; what runs is what the card showed."""
    import audit
    import hitl_journal
    _on()
    a = store.get_action(aid)
    if a is None or a["owner"] != (approver or "").lower():
        raise ConnectorError("Carte introuvable.", 404, "not_found")
    if a["status"] != "pending":
        raise ConnectorError(f"Cette carte est déjà « {a['status']} ».", 409, "decided")
    got = store.claim_action(aid, a["owner"], "approved" if approve else "rejected", approver)
    if got is None:
        raise ConnectorError("Cette carte vient d'être décidée ou a expiré.", 409, "decided")
    hitl_journal.decided(f"conn:{aid}", "allow" if approve else "deny", actor=approver, origin="owner",
                         message=reason or None)
    if not approve:
        audit.log(approver, f"connectors.{a['kind']}.rejected", f"action:{aid}", a["title"][:300],
                  project=a["project"])
        store.finish_action(aid, "rejected", reason[:500])
        return store.get_action(aid)
    try:
        result = _execute(a)
    except ConnectorError as e:
        store.finish_action(aid, "failed", e.message)
        audit.log(approver, f"connectors.{a['kind']}.failed", f"action:{aid}", e.message[:300],
                  project=a["project"])
        raise
    except Exception as e:  # noqa: BLE001
        store.finish_action(aid, "failed", type(e).__name__)
        audit.log(approver, f"connectors.{a['kind']}.failed", f"action:{aid}", type(e).__name__,
                  project=a["project"])
        raise ConnectorError(f"Échec : {type(e).__name__}", 502, "failed") from None
    store.finish_action(aid, "done", json.dumps(result)[:2000])
    audit.log(approver, f"connectors.{a['kind']}.done", f"action:{aid}", a["title"][:300],
              project=a["project"])
    return store.get_action(aid)


def _execute(a: dict) -> dict:
    payload = store.action_payload(a["id"])
    ctx = Ctx(a["owner"], a["session_id"], a["project"], zone="local")  # no content goes to a model here
    if a["kind"] == "mail.send":
        if not settings()["allow_send"]:
            raise ConnectorError("L'envoi a été désactivé entre-temps.", 403, "send_off")
        p, c = client(ctx, "mail", a["provider"])
        d = c.get_draft(payload["draft_id"], payload.get("mailbox", ""))
        if _digest(d) != payload["digest"]:
            raise ConnectorError("Le brouillon a changé depuis la carte : rien n'est envoyé. "
                                 "Proposez l'envoi à nouveau pour valider la nouvelle version.", 409, "changed")
        return c.send_draft(payload["draft_id"], payload.get("mailbox", ""))
    if a["kind"] == "calendar.create":
        p, c = client(ctx, "calendar", a["provider"])
        return c.create(payload["subject"], payload["start"], payload["end"], payload["attendees"],
                        payload.get("location", ""), payload.get("body", ""), payload.get("online", False))
    if a["kind"] == "files.write":
        p, c = client(ctx, "files", a["provider"])
        policy.check_path(settings(), a["project"], p, payload["path"])
        return c.write(payload["path"], payload["content"].encode())
    raise ConnectorError("type d'action inconnu", 400, "kind")


# ---- connecting a person --------------------------------------------------------------------
def connect_login(email: str, provider: str, login: str, password: str, url: str = "") -> dict:
    """IMAP / CalDAV / WebDAV with the person's own login: checked against the server before
    it is stored (encrypted)."""
    _on()
    from . import caldav, imapmail, webdav
    s = settings()
    if provider not in ("imap", "caldav", "webdav", "demo"):
        raise ConnectorError("Ce fournisseur se connecte autrement.", 400, "provider")
    if not s["providers"][provider].get("enabled"):
        raise ConnectorError(f"« {provider} » n'est pas activé sur cette instance.", 409, "disabled")
    login = (login or email).strip()
    acc = {"email": email.lower(), "login": login, "config": {"url": url} if url else {}}
    if provider == "imap":
        imapmail.Mail(s, acc, password).check()
    elif provider == "caldav":
        caldav.Calendar(s, acc, password).check()
    elif provider == "webdav":
        webdav.WebDAV(s, acc, password).check()
    store.save_account(email, provider, login=login, config=acc["config"], secret=password or "")
    import audit
    audit.log(email, "connectors.connect", provider, login)
    return {"provider": provider, "login": login}


def revoke(email: str, provider: str, by: str = "") -> bool:
    import audit
    ok = store.delete_account(email, provider)
    if provider == "demo":
        from .demo import _State
        try:
            os.unlink(_State(email, "").path)
        except OSError:
            pass
    audit.log(by or email, "connectors.revoke", f"{provider}:{email.lower()}", "")
    return ok
