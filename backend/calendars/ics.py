#!/usr/bin/env python3
"""calendars.ics — SOKKAN 3.3: ICS calendars for the morning brief (Helm).

Part of the `calendars` interface (backend/calendars/__init__.py): `ICSCalendar` is a
`CalendarProvider` (an ICS URL — the private "secret address" every calendar exports:
Google, Outlook/Exchange, Nextcloud…). Microsoft 365 is the other provider
(`teams.graph.GraphCalendar`, feature `teams`, 3.4), registered with `calendars.register`.
The brief (helm._agenda) reads an ICS configured for the person / team first, then
`calendars.events_for` (Graph when the Teams app is configured).

The URL of a private ICS feed IS a credential (whoever has it reads the calendar), so
it lives in the vault and is referenced BY NAME: per person or team in Helm
(`helm_calendars`, set by the person themselves), with an instance fallback
`SOKKAN_HELM_CALENDAR_ICS` (name of a vault secret holding a team calendar URL).

Fetching: https only, private / loopback addresses refused (SSRF guard) unless
SOKKAN_HELM_CALENDAR_ALLOW_PRIVATE=1, 10 s, 2 MB at most. Recurrences: DAILY and
WEEKLY (BYDAY, INTERVAL, UNTIL, COUNT, EXDATE); anything more exotic shows its first
occurrence only.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")
MAX_BYTES = 2 * 1024 * 1024
KINDS = ("ics",)            # Graph is not set per person: it comes with the teams feature


@dataclass
class Event:
    start: datetime
    end: datetime | None
    title: str
    all_day: bool = False
    location: str = ""

    def as_dict(self) -> dict:
        s = self.start.astimezone(TZ)
        e = self.end.astimezone(TZ) if self.end else None
        return {"start": s.isoformat(), "end": e.isoformat() if e else None, "title": self.title,
                "all_day": self.all_day, "location": self.location,
                "start_local": "all day" if self.all_day else s.strftime("%H:%M"),
                "end_local": "" if self.all_day or not e else e.strftime("%H:%M")}


class CalendarSource:
    """Interface: the events overlapping [start, end)."""

    def events(self, start: datetime, end: datetime) -> list[Event]:  # pragma: no cover
        raise NotImplementedError


# ---- ICS ------------------------------------------------------------------------------
def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _dt(value: str, params: dict) -> tuple[datetime, bool]:
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(tzinfo=TZ), True
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    tz = TZ
    if params.get("TZID"):
        try:
            tz = ZoneInfo(params["TZID"].strip('"'))
        except Exception:  # noqa: BLE001 — Windows names ("W. Europe Standard Time"…)
            tz = TZ
    return datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz), False


def _prop(line: str) -> tuple[str, dict, str]:
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v
    return parts[0].upper(), params, value


_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _expand(ev: Event, rrule: str, exdates: set, start: datetime, end: datetime) -> list[Event]:
    r = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = r.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY"):
        return [ev]
    interval = max(1, int(r.get("INTERVAL", "1") or 1))
    count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
    until = None
    if r.get("UNTIL"):
        until, _ = _dt(r["UNTIL"], {})
    days = sorted({_DAYS[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in _DAYS}) \
        or [ev.start.weekday()]
    dur = (ev.end - ev.start) if ev.end else None
    out, n = [], 0
    cur = ev.start
    if count is None and cur < start - timedelta(days=14):
        # an old series (a weekly stand-up since 2020): jump close to the window, by whole
        # weeks so the weekday and the interval arithmetic stay right
        cur = cur + timedelta(days=((start - cur).days // 7 - 1) * 7)
    week0 = ev.start.date() - timedelta(days=ev.start.weekday())
    for _ in range(3000):
        if until and cur > until or cur >= end or (count is not None and n >= count):
            break
        ok = True
        if freq == "WEEKLY":
            wk = ((cur.date() - timedelta(days=cur.weekday())) - week0).days // 7
            ok = cur.weekday() in days and wk % interval == 0
        else:
            ok = (cur.date() - ev.start.date()).days % interval == 0
        if ok and cur >= ev.start:
            n += 1
            if cur.strftime("%Y%m%d%H%M") not in exdates and (cur + (dur or timedelta(0))) > start:
                out.append(Event(cur, cur + dur if dur else None, ev.title, ev.all_day, ev.location))
        cur = cur + timedelta(days=1)
    return out


def parse_ics(text: str, start: datetime, end: datetime) -> list[Event]:
    """Events overlapping [start, end) from an ICS document, sorted."""
    out: list[Event] = []
    cur: dict | None = None
    for line in _unfold(text):
        name, params, value = _prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"ex": set()}
        elif name == "END" and value.upper() == "VEVENT" and cur is not None:
            if "start" in cur and cur.get("status") != "CANCELLED":
                s, allday = cur["start"]
                e = cur.get("end", (None, allday))[0] or (s + timedelta(days=1) if allday else None)
                ev = Event(s, e, cur.get("title") or "(no title)", allday, cur.get("location") or "")
                evs = _expand(ev, cur["rrule"], cur["ex"], start, end) if cur.get("rrule") else [ev]
                for x in evs:
                    xe = x.end or x.start
                    if x.start < end and (xe > start or x.start >= start):
                        out.append(x)
            cur = None
        elif cur is not None:
            if name == "DTSTART":
                cur["start"] = _dt(value, params)
            elif name == "DTEND":
                cur["end"] = _dt(value, params)
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)[:200]
            elif name == "LOCATION":
                cur["location"] = _unescape(value)[:200]
            elif name == "RRULE":
                cur["rrule"] = value
            elif name == "STATUS":
                cur["status"] = value.upper()
            elif name == "EXDATE":
                for v in value.split(","):
                    try:
                        cur["ex"].add(_dt(v, params)[0].strftime("%Y%m%d%H%M"))
                    except ValueError:
                        pass
    return sorted(out, key=lambda e: e.start)


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("calendar URL: https only")
    if os.environ.get("SOKKAN_HELM_CALENDAR_ALLOW_PRIVATE") == "1":
        return
    for info in socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("calendar URL: private or loopback address refused")


class ICSUrlSource(CalendarSource):
    def __init__(self, url: str, fetch=None):
        self.url = url
        self._fetch = fetch or self._http

    @staticmethod
    def _http(url: str) -> str:
        import httpx
        _check_url(url)
        with httpx.Client(timeout=10.0, follow_redirects=False) as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                buf = b""
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise ValueError("calendar too large")
        return buf.decode("utf-8", "replace")

    def events(self, start: datetime, end: datetime) -> list[Event]:
        return parse_ics(self._fetch(self.url), start, end)


# ---- configuration (vault secret NAMES) -----------------------------------------------
def set_source(principal: str, kind: str, secret: str) -> dict:
    import helm
    import vault
    if kind not in KINDS:
        raise ValueError(f"calendar kind: one of {', '.join(KINDS)} (Microsoft 365 calendars come "
                         "from the teams feature, not from a per-person setting)")
    if not vault.valid_name(secret):
        raise ValueError("secret: a vault secret NAME (the ICS address is a credential)")
    con = helm._con()
    con.execute("INSERT INTO helm_calendars(principal, kind, secret, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(principal) DO UPDATE SET kind=excluded.kind, secret=excluded.secret,"
                " updated_at=excluded.updated_at", (principal.lower(), kind, secret, time.time()))
    con.commit()
    con.close()
    return get_source_config(principal) or {}


def clear_source(principal: str) -> None:
    import helm
    con = helm._con()
    con.execute("DELETE FROM helm_calendars WHERE principal=?", (principal.lower(),))
    con.commit()
    con.close()


def get_source_config(principal: str) -> dict | None:
    import helm
    con = helm._con()
    r = con.execute("SELECT principal, kind, secret, updated_at FROM helm_calendars WHERE principal=?",
                    ((principal or "").lower(),)).fetchone()
    con.close()
    return dict(r) if r else None


def source_for(principal: str, project: str = "default") -> CalendarSource | None:
    """The calendar of a person / team, else the instance's team calendar, else None.
    The secret is read in the vault of `project` (3.2 lot 4: one vault per project)."""
    import vault
    cfg = get_source_config(principal) if principal else None
    name = (cfg or {}).get("secret") or (os.environ.get("SOKKAN_HELM_CALENDAR_ICS") or "").strip()
    if not name:
        return None
    url = vault.session_env([name], project=project or "default").get(name)
    if not url:
        return None
    return ICSUrlSource(url)


def today_window(now: float | None = None) -> tuple[datetime, datetime]:
    d = datetime.fromtimestamp(now or time.time(), TZ)
    s = datetime.combine(date(d.year, d.month, d.day), datetime.min.time(), TZ)
    return s, s + timedelta(days=1)


class ICSCalendar:
    """`calendars.CalendarProvider` over the ICS sources above (per person, else the
    instance's team calendar). Not registered globally: Helm asks it first, then
    `calendars.events_for` (see helm._agenda)."""
    name = "ics"

    def __init__(self, project: str = "default"):
        self.project = project

    def configured(self) -> bool:
        return True

    def events(self, email: str, start: datetime, end: datetime) -> list:
        from calendars import CalendarEvent
        src = source_for(email, self.project)
        if src is None:
            return []
        return [CalendarEvent(start=e.start.astimezone(timezone.utc).isoformat(),
                              end=(e.end or e.start).astimezone(timezone.utc).isoformat(),
                              subject=e.title, location=e.location)
                for e in src.events(start, end)]
