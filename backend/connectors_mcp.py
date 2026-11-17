#!/usr/bin/env python3
"""connectors_mcp.py — SOKKAN 4.0: MCP stdio server ``sokkan-connectors`` (feature `connectors`).

The session's mailbox, calendar and shared documents, AS the person who drives the session
(SOKKAN_SESSION_USER, set by the API — no tool takes a person argument, the model cannot
choose whose mailbox it reads). Reads answer directly; a draft goes in the person's own
mailbox; sending, creating an event and writing a deliverable into a shared folder only
create a CARD the person decides in SOKKAN (backend/connectors/service.py).

Every answer that carries content passed the project's classification against the zone of
the session's model first (connectors/policy.py).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "memory"))
try:
    from harness.identity import expand_env as _expand_identity
    _expand_identity()
except Exception:  # noqa: BLE001 — no handle, no mapping: the environment is used as is
    pass

from connectors import ConnectorError, service  # noqa: E402
from connectors import store as _store  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sokkan-connectors")


def _run(fn, *a, **k) -> dict:
    try:
        return fn(service.Ctx.from_env(), *a, **k)
    except ConnectorError as e:
        return {"error": e.message, "code": e.code}


@mcp.tool()
def status() -> dict:
    """Which mail, calendar and document connectors this person has here, whether their content
    may reach this session's model (project classification), and how many cards wait."""
    return _run(service.status)


@mcp.tool()
def mail_list(folder: str = "inbox", query: str = "", mailbox: str = "", limit: int = 20,
              provider: str = "") -> dict:
    """List the messages of a folder of the person's mailbox (newest first), or of a shared
    mailbox opened to this project (`mailbox` = its address). `query` searches subject, sender
    and body."""
    return _run(service.mail_list, folder, mailbox, query, limit, provider)


@mcp.tool()
def mail_read_thread(thread_id: str, mailbox: str = "", provider: str = "") -> dict:
    """Read a whole conversation (every message, oldest first, with its text and attachments'
    names). `thread_id` comes from mail_list."""
    return _run(service.mail_thread, thread_id, mailbox, provider)


@mcp.tool()
def mail_draft(body: str, reply_to: str = "", to: list[str] | None = None, subject: str = "",
               cc: list[str] | None = None, reply_all: bool = False, mailbox: str = "",
               provider: str = "") -> dict:
    """Write a DRAFT in the person's mailbox — a reply (`reply_to` = the message id) or a new
    message (`to`, `subject`). Nothing is sent: the person reads it in their own mail client,
    or you propose sending it with mail_propose_send."""
    return _run(service.mail_draft, body, reply_to, to, subject, cc, mailbox, reply_all, provider)


@mcp.tool()
def mail_propose_send(draft_id: str, mailbox: str = "", provider: str = "") -> dict:
    """Ask the person to approve sending a draft. Creates an approval card; the mail leaves only
    when the person approves it in SOKKAN, exactly as the card shows it."""
    return _run(service.propose_send, draft_id, mailbox, provider)


@mcp.tool()
def calendar_events(start: str = "", end: str = "", provider: str = "") -> dict:
    """The person's events between `start` and `end` (ISO 8601; default: today)."""
    return _run(service.calendar_events, start, end, provider)


@mcp.tool()
def calendar_free_slots(duration_min: int = 30, start: str = "", end: str = "",
                        attendees: list[str] | None = None, provider: str = "") -> dict:
    """Propose free slots of `duration_min` on working days (08:00-18:00, Europe/Zurich), from the
    person's calendar and — when the provider shares them — the busy times of `attendees`."""
    return _run(service.calendar_free_slots, duration_min, start, end, attendees, provider=provider)


@mcp.tool()
def calendar_prepare_meeting(event_id: str, provider: str = "") -> dict:
    """Prepare a meeting: its detail, agenda, attendees, attachments' names, and the recent mails
    that mention it."""
    return _run(service.calendar_prepare, event_id, provider)


@mcp.tool()
def calendar_propose_event(subject: str, start: str, end: str, attendees: list[str] | None = None,
                           location: str = "", body: str = "", online: bool = False,
                           provider: str = "") -> dict:
    """Ask the person to approve creating an event (invitations go to `attendees` once created).
    Creates an approval card; nothing is in the calendar before the person approves."""
    return _run(service.propose_event, subject, start, end, attendees, location, body, online, provider)


@mcp.tool()
def files_list(path: str = "/", provider: str = "") -> dict:
    """List a folder of the shared documents (SharePoint / OneDrive: `/me/...` or `/<library>/...`;
    WebDAV or network folder: `/...`)."""
    return _run(service.files_list, path, provider)


@mcp.tool()
def files_search(query: str, path: str = "/", provider: str = "") -> dict:
    """Find shared documents by name (and content where the provider searches it)."""
    return _run(service.files_search, query, path, provider)


@mcp.tool()
def files_read(path: str, provider: str = "") -> dict:
    """Read a shared document as text (Word, PowerPoint, Excel, PDF with a text layer, text)."""
    return _run(service.files_read, path, provider)


@mcp.tool()
def files_propose_write(path: str, content: str, provider: str = "") -> dict:
    """Ask the person to approve depositing a deliverable (text / markdown) at `path` in the shared
    folder. Creates an approval card; an existing file is never overwritten (a numbered copy)."""
    return _run(service.propose_write, path, content, provider)


@mcp.tool()
def actions_list(status: str = "") -> dict:
    """The approval cards of this person (pending, done, rejected, failed, expired)."""
    try:
        ctx = service.Ctx.from_env()
        service._on()
    except ConnectorError as e:
        return {"error": e.message, "code": e.code}
    rows = _store.actions_of(ctx.email, status, 30)
    return {"actions": [{k: r[k] for k in ("id", "kind", "title", "status", "created_at", "result")}
                        for r in rows]}


if __name__ == "__main__":
    mcp.run()
