"""connectors.caldav — calendars over CalDAV (Nextcloud, Radicale, SOGo, iCloud, Fastmail…)
and the read-only ``ics`` provider (the person's private ICS address, set for the brief).

CalDAV: the instance settings give the server (`providers.caldav.url`, a template that may hold
``{login}``, e.g. ``https://dav.example.ch/{login}/calendar/``); a person connects with their
own login. Reads are a ``calendar-query`` REPORT with a time range and server-side expansion
of recurrences; a creation is a ``PUT`` of a new VCALENDAR with ``If-None-Match: *`` (never
overwrites an event).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET

from calendars import ics as _ics

from . import ConnectorError, http

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ConnectorError(f"date invalide : {s!r} (ISO 8601)", 400, "date") from None
    return d if d.tzinfo else d.replace(tzinfo=_ics.TZ)


def _esc(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def parse_vevents(text: str) -> list[dict]:
    """Every VEVENT of an iCalendar text → dicts (uid, subject, start, end, location,
    organizer, attendees, body)."""
    out, cur = [], None
    for line in _ics._unfold(text):
        name, params, value = _ics._prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"attendees": []}
        elif name == "END" and value.upper() == "VEVENT" and cur is not None:
            if "start" in cur and cur.get("status") != "CANCELLED":
                out.append(cur)
            cur = None
        elif cur is not None:
            if name in ("DTSTART", "DTEND"):
                d, allday = _ics._dt(value, params)
                cur["start" if name == "DTSTART" else "end"] = _iso(d)
                cur["all_day"] = allday
            elif name == "SUMMARY":
                cur["subject"] = _ics._unescape(value)[:300]
            elif name == "LOCATION":
                cur["location"] = _ics._unescape(value)[:300]
            elif name == "DESCRIPTION":
                cur["body"] = value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")[:20000]
            elif name == "UID":
                cur["uid"] = value
            elif name == "STATUS":
                cur["status"] = value.upper()
            elif name == "ORGANIZER":
                cur["organizer"] = re.sub(r"(?i)^mailto:", "", value)
            elif name == "ATTENDEE":
                cur["attendees"].append(re.sub(r"(?i)^mailto:", "", value))
            elif name == "ATTACH":
                cur.setdefault("attachments", []).append({"name": params.get("FILENAME") or value[:120], "size": 0})
    for e in out:
        e.setdefault("subject", "(sans titre)")
        e.setdefault("end", e["start"])
        e.setdefault("location", "")
        e.setdefault("organizer", "")
    return out


def vcalendar(uid: str, subject: str, start: datetime, end: datetime, attendees: list[str],
              location: str, body: str, organizer: str) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SOKKAN//connectors//EN", "BEGIN:VEVENT",
             f"UID:{uid}", f"DTSTAMP:{_z(datetime.now(timezone.utc))}", f"DTSTART:{_z(start)}",
             f"DTEND:{_z(end)}", f"SUMMARY:{_esc(subject)}"]
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    if body:
        lines.append(f"DESCRIPTION:{_esc(body)}")
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    lines += [f"ATTENDEE;RSVP=TRUE:mailto:{a}" for a in attendees]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


class Calendar:
    def __init__(self, settings: dict, acc: dict, password: str):
        base = ((settings.get("providers") or {}).get("caldav") or {}).get("url") or ""
        if not base:
            raise ConnectorError("CalDAV n'est pas configuré (Réglages › Connecteurs).", 409, "not_configured")
        self.login = acc.get("login") or acc["email"]
        url = (acc.get("config") or {}).get("url") or base.replace("{login}", quote(self.login))
        if urlparse(url).netloc != urlparse(base.replace("{login}", "x")).netloc:
            raise ConnectorError("Le calendrier doit être sur le serveur configuré.", 400, "url")
        if urlparse(url).scheme != "https" and not _plain_ok(url):
            raise ConnectorError("CalDAV : https obligatoire.", 400, "url")
        self.url = url if url.endswith("/") else url + "/"
        self.auth = (self.login, password)

    def _req(self, method: str, url: str, body: str = "", headers: dict | None = None):
        with http() as h:
            r = h.request(method, url, content=body.encode() if body else None, auth=self.auth,
                          headers={"content-type": "application/xml; charset=utf-8", **(headers or {})})
        if r.status_code in (401, 403):
            raise ConnectorError("Le serveur CalDAV refuse ces identifiants.", 403, "denied")
        if r.status_code == 404:
            raise ConnectorError("Calendrier introuvable.", 404, "not_found")
        if r.status_code == 412:
            raise ConnectorError("Cet événement existe déjà.", 409, "exists")
        if r.status_code >= 300:
            raise ConnectorError(f"Le serveur CalDAV a répondu {r.status_code}.", 502, "upstream")
        return r

    def _report(self, start: datetime, end: datetime) -> list[tuple[str, str]]:
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data><c:expand start="{_z(start)}" end="{_z(end)}"/></c:calendar-data></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{_z(start)}" end="{_z(end)}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
        r = self._req("REPORT", self.url, body, {"depth": "1"})
        out = []
        for resp in ET.fromstring(r.content).findall("d:response", NS):
            href = (resp.findtext("d:href", "", NS) or "").strip()
            data = resp.findtext(".//c:calendar-data", "", NS) or ""
            if data:
                out.append((href, data))
        return out

    def events(self, start: str, end: str) -> list[dict]:
        s, e = parse_iso(start), parse_iso(end)
        out = []
        for href, data in self._report(s, e):
            for ev in parse_vevents(data):
                ps, pe = parse_iso(ev["start"]), parse_iso(ev["end"])
                if ps < e and (pe > s or (ps == pe and ps >= s)):
                    out.append({"id": href, "subject": ev["subject"], "start": ev["start"],
                                "end": ev["end"], "location": ev["location"],
                                "organizer": ev["organizer"], "attendees": ev["attendees"],
                                "online": False, "show_as": "busy"})
        return sorted(out, key=lambda x: x["start"])

    def event(self, event_id: str) -> dict:
        url = urljoin(self.url, event_id)
        if urlparse(url).netloc != urlparse(self.url).netloc or not urlparse(url).path.startswith(urlparse(self.url).path):
            raise ConnectorError("Événement hors de ce calendrier.", 400, "id")
        r = self._req("GET", url, headers={"content-type": "text/calendar"})
        evs = parse_vevents(r.text)
        if not evs:
            raise ConnectorError("Événement introuvable.", 404, "not_found")
        ev = evs[0]
        return {"id": event_id, **{k: ev.get(k, "") for k in ("subject", "start", "end", "location",
                                                               "organizer", "body")},
                "attendees": ev["attendees"], "attachments": ev.get("attachments", []),
                "online": False}

    def busy_of(self, attendees, start, end):
        return {}

    def create(self, subject: str, start: str, end: str, attendees: list[str], location: str = "",
               body: str = "", online: bool = False) -> dict:
        uid = f"{uuid.uuid4()}@sokkan"
        ics = vcalendar(uid, subject, parse_iso(start), parse_iso(end), attendees, location, body,
                        self.login if "@" in self.login else "")
        href = urljoin(self.url, f"{uid}.ics")
        self._req("PUT", href, ics, {"content-type": "text/calendar; charset=utf-8", "if-none-match": "*"})
        return {"id": urlparse(href).path, "uid": uid}

    def check(self) -> None:
        self._req("PROPFIND", self.url, '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                  '<d:resourcetype/></d:prop></d:propfind>', {"depth": "0"})


def _plain_ok(url: str) -> bool:
    import os
    from .imapmail import _private
    return os.environ.get("SOKKAN_CONNECTORS_ALLOW_PLAIN") == "1" and _private(urlparse(url).hostname or "")


class ICS:
    """Read-only: the person's private ICS address (Helm › calendar, a vault secret NAME)."""

    def __init__(self, email: str, project: str):
        self.src = _ics.source_for(email, project)
        if self.src is None:
            raise ConnectorError("Aucune adresse ICS n'est enregistrée pour vous (Helm).", 409,
                                 "not_configured")

    def events(self, start: str, end: str) -> list[dict]:
        out = []
        for i, e in enumerate(self.src.events(parse_iso(start), parse_iso(end))):
            out.append({"id": f"ics:{i}", "subject": e.title, "start": _iso(e.start),
                        "end": _iso(e.end or e.start + timedelta(hours=1)), "location": e.location,
                        "organizer": "", "attendees": [], "online": False, "show_as": "busy"})
        return out

    def event(self, event_id: str) -> dict:
        raise ConnectorError("Un agenda ICS ne donne pas le détail d'un événement.", 409, "read_only")

    def busy_of(self, attendees, start, end):
        return {}

    def create(self, *a, **k) -> dict:
        raise ConnectorError("Un agenda ICS est en lecture seule.", 409, "read_only")
