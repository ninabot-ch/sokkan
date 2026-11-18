"""connectors — SOKKAN 4.0: the coworker works where things are (feature `connectors`).

Mail, calendar and shared documents, read on demand by a session AS the person who drives
it, and written only through an approval card (« carte d'accord »). One package, three
families, several providers each:

=========  ============================================  ====================================
family     providers                                     what a session may do without a card
=========  ============================================  ====================================
mail       ``graph`` (Microsoft 365), ``imap``           list, read a thread, write a DRAFT in
           (IMAP + SMTP), ``demo``                       the person's own mailbox
calendar   ``graph``, ``caldav``, ``ics`` (read only),   read events, free slots, prepare a
           ``demo``                                      meeting
files      ``graph`` (SharePoint / OneDrive),            list, search, read
           ``webdav``, ``folder`` (an SMB share
           mounted on the host), ``demo``
=========  ============================================  ====================================

Sending a mail, creating an event and writing a file into a shared folder are ACTIONS: the
session only proposes them (`service.propose`), the person approves them in SOKKAN
(`POST /api/connectors/actions/{id}/approve`), and the API — never the session's process —
executes them with the person's credentials. Every proposal and decision is journaled
(`hitl_journal`, kind ``connector``; `audit`). A session in ``bypassPermissions`` mode does not
change that: the card is not the engine's permission prompt, it is a row in SOKKAN's store.

Delegation is per person: OAuth authorization code + PKCE for Microsoft Graph (delegated
scopes, the person's own consent, refresh token encrypted with the vault's data key), a
personal login for IMAP / CalDAV / WebDAV. No service account reads every mailbox.

Classification (`policy`): the content of a connector reaches a model only when the
project's level allows the zone where that model runs — a project classified
``confidential`` or above only on a model in Switzerland or on the machine.

Modules: `store` (connectors.db), `policy`, `service` (what the MCP server and the API
call), `graph`, `imapmail`, `caldav`, `webdav`, `demo`, `api`, `ingest` (doc_ingest source
kind ``webdav``). The MCP server is `backend/connectors_mcp.py` (``sokkan-connectors``).
"""
from __future__ import annotations

import os

FAMILIES = ("mail", "calendar", "files")
PROVIDERS = {
    "mail": ("graph", "imap", "demo"),
    "calendar": ("graph", "caldav", "ics", "demo"),
    "files": ("graph", "webdav", "folder", "demo"),
}
# the providers a person connects with their own credentials (graph = OAuth, the others a
# login); `folder` and `ics` are configured, not connected
PERSONAL = ("graph", "imap", "caldav", "webdav", "demo")
ACTIONS = ("mail.send", "calendar.create", "files.write")
SERVER = "sokkan-connectors"


class ConnectorError(Exception):
    """A readable refusal or failure (status: the HTTP code the API answers with)."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code or "error"


def enabled() -> bool:
    import features
    return features.enabled("connectors")


def data_dir() -> str:
    return os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))


def http(timeout: float = 20.0):
    """The HTTP client of every provider (replaced in tests). Redirects are not followed: a
    redirect to another host would carry the person's bearer token."""
    import httpx
    return httpx.Client(timeout=timeout, follow_redirects=False)
