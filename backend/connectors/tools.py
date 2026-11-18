"""connectors.tools — the names of the tools of the ``sokkan-connectors`` MCP server, for the
engines (harness contract): `read_tools` (no card at the engine), `write_tools` (the engine's
permission card), in both namings — ``mcp__<server>__<tool>`` (Claude Code CLI, legacy engine)
and ``<server>__<tool>`` (the kernel).

The ``propose_*`` tools are in `READ` on purpose: they change nothing outside SOKKAN — they
store a CARD that the person decides in SOKKAN, and only that decision acts. Asking the engine's
permission first would show the person two cards for one gesture; in ``bypassPermissions`` the
engine's card would vanish anyway, the connector's card never does."""
from __future__ import annotations

from . import SERVER

READ = ("status", "mail_list", "mail_read_thread", "calendar_events", "calendar_free_slots",
        "calendar_prepare_meeting", "files_list", "files_search", "files_read", "actions_list",
        "mail_propose_send", "calendar_propose_event", "files_propose_write")
WRITE = ("mail_draft",)
ALL = READ + WRITE


def names(tools=ALL, style: str = "legacy") -> list[str]:
    sep = f"mcp__{SERVER}__" if style == "legacy" else f"{SERVER}__"
    return [sep + t for t in tools]


def read_tools(style: str = "legacy") -> list[str]:
    return names(READ, style)
