"""teams — SOKKAN 3.4: Microsoft Teams (feature ``teams``).

@Nina in a channel or a chat, HITL approvals as Adaptive Cards, decision capture into the
project memory, channel ↔ project mapping, calendar through Microsoft Graph for the brief.

Security model (docs/enterprise/TEAMS.md):
* single-tenant app — every inbound activity's tenant must be ``SOKKAN_TEAMS_TENANT_ID``;
* every inbound request carries a Bot Framework JWT, verified (signature against the
  published keys, issuer, audience = our app id, expiry, serviceUrl claim, endorsement of
  the msteams channel) before anything is read (`botauth`);
* a Teams user acts only through THEIR SOKKAN account: the link (Entra object id → email)
  is recorded when they sign in to SOKKAN with Entra ID (OIDC `oid` / `tid` claims) —
  never guessed from a display name; Nina reads as them, capped by the channel's level;
* approvals are signed, single-use, expiring tokens, bound to an approver when one is named;
* outbound tokens (Bot Framework, Graph) are cached encrypted (Fernet, key of the instance).

Module map: config · store (SQLite teams.db) · botauth · connector (outbound) · cards
(Adaptive Cards + signed approvals) · bot (activity handling) · graph (calendar, presence)
· api (routes).
"""
from __future__ import annotations

import os


def enabled() -> bool:
    import features
    return features.enabled("teams")


def cfg(name: str, default: str = "") -> str:
    return (os.environ.get(f"SOKKAN_TEAMS_{name}") or default).strip()


def app_id() -> str:
    return cfg("APP_ID")


def tenant_id() -> str:
    return cfg("TENANT_ID")


def app_password() -> str:
    """The client secret: ``SOKKAN_TEAMS_APP_PASSWORD``, or the content of the file named by
    ``SOKKAN_TEAMS_APP_PASSWORD_FILE`` (preferred: rendered 0600 from the secrets store, never in
    the process environment)."""
    v = cfg("APP_PASSWORD")
    if v:
        return v
    path = cfg("APP_PASSWORD_FILE")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        print(f"[teams] SOKKAN_TEAMS_APP_PASSWORD_FILE unreadable: {e.__class__.__name__}")
        return ""


def configured() -> list[str]:
    """Missing configuration (empty = ready)."""
    missing = [f"SOKKAN_TEAMS_{n}" for n in ("APP_ID", "TENANT_ID") if not cfg(n)]
    if not app_password():
        missing.insert(1, "SOKKAN_TEAMS_APP_PASSWORD")
    return missing


def openid_url() -> str:
    return cfg("OPENID_URL", "https://login.botframework.com/v1/.well-known/openidconfiguration")


def login_url() -> str:
    return cfg("LOGIN_URL", "https://login.microsoftonline.com").rstrip("/")


def graph_url() -> str:
    return cfg("GRAPH_URL", "https://graph.microsoft.com/v1.0").rstrip("/")


def public_url() -> str:
    return (cfg("PUBLIC_URL") or os.environ.get("SOKKAN_PUBLIC_URL") or "").rstrip("/")


BOT_ISSUER = "https://api.botframework.com"
# hosts a serviceUrl may point at (we send our bot token there): Microsoft's, plus the ones
# listed in SOKKAN_TEAMS_SERVICE_HOSTS (the test simulator, a sovereign cloud)
SERVICE_HOSTS = ("smba.trafficmanager.net", ".botframework.com", ".botframework.us",
                 ".teams.microsoft.com")

# the transport of every outbound HTTP call (tests plug the Graph / Bot Framework simulator)
TRANSPORT = None


def http():
    import httpx
    return httpx.Client(timeout=10.0, transport=TRANSPORT)
