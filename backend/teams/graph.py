"""teams.graph — Microsoft Graph, application permissions of the customer's tenant only:

* ``Calendars.Read`` → ``GraphCalendar`` (the `calendars` interface, for the brief);
* ``Presence.Read.All`` → ``presence`` (optional).

Restrict the mailboxes the app may read with an Exchange application access policy (or RBAC
for Applications) — docs/enterprise/TEAMS.md. Every call reads ONE person's data, the person
the brief is for."""
from __future__ import annotations

import datetime as _dt
from urllib.parse import quote

import teams
from calendars import CalendarEvent
from teams import connector


def _get(path: str, params: dict | None = None, headers: dict | None = None) -> dict:
    tok = connector.app_token(connector.GRAPH_SCOPE)
    with teams.http() as h:
        r = h.get(teams.graph_url() + path, params=params,
                  headers={"authorization": f"Bearer {tok}", **(headers or {})})
    if r.status_code == 404:
        return {}
    if r.status_code >= 300:
        raise connector.Error(f"Graph answered {r.status_code} on {path.split('?')[0]}")
    return r.json()


def _iso(v: dict | None) -> str:
    s = (v or {}).get("dateTime") or ""
    return s if not s or s.endswith("Z") or "+" in s[10:] else s.split(".")[0] + "Z"


class GraphCalendar:
    """The calendar of a Microsoft 365 user, read with Calendars.Read (application)."""
    name = "microsoft-graph"

    def configured(self) -> bool:
        return teams.enabled() and not teams.configured() and teams.cfg("CALENDAR", "1") != "0"

    def events(self, email: str, start: _dt.datetime, end: _dt.datetime) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        params = {"startDateTime": start.astimezone(_dt.timezone.utc).isoformat(),
                  "endDateTime": end.astimezone(_dt.timezone.utc).isoformat(),
                  "$select": "subject,start,end,location,isOnlineMeeting,organizer,showAs,attendees",
                  "$orderby": "start/dateTime", "$top": "50"}
        j = _get(f"/users/{quote(email)}/calendarView", params,
                 {"Prefer": 'outlook.timezone="UTC"'})
        for e in j.get("value", []):
            out.append(CalendarEvent(
                start=_iso(e.get("start")), end=_iso(e.get("end")), subject=e.get("subject") or "",
                location=((e.get("location") or {}).get("displayName") or ""),
                online=bool(e.get("isOnlineMeeting")),
                organizer=(((e.get("organizer") or {}).get("emailAddress") or {}).get("address") or ""),
                show_as=e.get("showAs") or "",
                attendees=[((a.get("emailAddress") or {}).get("address") or "")
                           for a in e.get("attendees") or []]))
        return out


def presence(aad_object_id: str) -> str:
    """availability of a user (Available, Busy, Away…), '' when unknown."""
    return (_get(f"/users/{quote(aad_object_id)}/presence") or {}).get("availability") or ""


def register() -> None:
    import calendars
    calendars.register(GraphCalendar())
