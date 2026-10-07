"""calendars — the calendar interface of SOKKAN (3.3 brief, 3.4 Teams).

One interface, several providers: the morning brief (Helm) asks "what is on this person's
calendar today?" without knowing where the calendar lives. Microsoft 365 / Teams is the first
provider (`teams.graph.GraphCalendar`, Microsoft Graph); another (CalDAV, Google) registers
itself the same way.

Named ``calendars`` (plural) on purpose: the backend directory is on ``sys.path`` and a
package named ``calendar`` would shadow the standard library module of the same name
(imported by ``http.cookiejar``, ``imaplib``, ``email`` utilities…).

    from calendars import CalendarEvent, CalendarProvider, events_for, register

A provider reads AS the person (their own calendar), never a shared view; what it returns is
not classified content (subjects of meetings), but a brief built from it with project notes
inherits their level (classification.derive).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class CalendarEvent:
    start: str                      # ISO 8601, UTC
    end: str
    subject: str = ""
    location: str = ""
    online: bool = False
    organizer: str = ""
    show_as: str = ""               # free | tentative | busy | oof | workingElsewhere
    attendees: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class CalendarProvider(Protocol):
    name: str

    def configured(self) -> bool:
        """True when this provider can answer (credentials, consent)."""

    def events(self, email: str, start: _dt.datetime, end: _dt.datetime) -> list[CalendarEvent]:
        """Events of ``email``'s calendar overlapping [start, end), ordered by start."""


_providers: list[CalendarProvider] = []


def register(provider: CalendarProvider) -> None:
    """Add a provider (idempotent by name; the latest registration wins)."""
    global _providers
    _providers = [p for p in _providers if p.name != provider.name] + [provider]


def unregister(name: str) -> None:
    global _providers
    _providers = [p for p in _providers if p.name != name]


def provider() -> CalendarProvider | None:
    """The first configured provider, None when none is."""
    for p in _providers:
        try:
            if p.configured():
                return p
        except Exception:  # noqa: BLE001 — a broken provider is not configured
            continue
    return None


def events_for(email: str, day: _dt.date | None = None,
               tz: _dt.tzinfo = _dt.timezone.utc) -> list[CalendarEvent]:
    """The person's events of ``day`` (today by default); [] without a provider."""
    p = provider()
    if p is None:
        return []
    day = day or _dt.datetime.now(tz).date()
    start = _dt.datetime.combine(day, _dt.time(0, 0), tz)
    return p.events(email, start, start + _dt.timedelta(days=1))
