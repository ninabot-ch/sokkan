"""teams.signing — approval tokens: signed (HMAC-SHA256, key of the instance), expiring,
single use (a row per nonce, consumed atomically), optionally bound to ONE approver."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import store

TTL_S = 24 * 3600


class Invalid(Exception):
    pass


def _mac_key() -> bytes:
    return _mac_keys()[0]


def _mac_keys() -> list[bytes]:
    """Primary first; every data key of a rotation in progress still verifies."""
    return [hmac.new(k, b"sokkan-teams-approval-v1", hashlib.sha256).digest()
            for k in store.keys()]


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(kind: str, ref: str, project: str, requested_by: str, approver_aad: str = "",
          ttl: int = TTL_S, card: dict | None = None) -> str:
    """A signed approval. ``card`` (title, facts, note, open_url) is what the Adaptive Card
    shows — kept so that a refresh re-renders it."""
    nonce = secrets.token_urlsafe(18)
    now = time.time()
    payload = {"n": nonce, "k": kind, "r": str(ref), "p": project, "a": approver_aad,
               "e": int(now + ttl)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_mac_key(), body.encode(), hashlib.sha256).digest())
    c = store.con()
    with c:
        c.execute("INSERT INTO approvals(nonce, kind, ref, project, requested_by, approver_aad,"
                  " created_at, expires_at, card) VALUES(?,?,?,?,?,?,?,?,?)",
                  (nonce, kind, str(ref), project, requested_by, approver_aad, now, now + ttl,
                   json.dumps(card or {})))
    c.close()
    return f"{body}.{sig}"


def peek(token: str, allow_expired: bool = False) -> dict:
    """Check the signature and expiry, return the payload (does NOT consume)."""
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise Invalid("malformed approval")
    if not any(hmac.compare_digest(_b64(hmac.new(k, body.encode(), hashlib.sha256).digest()), sig)
               for k in _mac_keys()):
        raise Invalid("bad signature")
    try:
        p = json.loads(_unb64(body))
    except ValueError:
        raise Invalid("malformed approval")
    if p.get("e", 0) < time.time() and not allow_expired:
        raise Invalid("this approval has expired")
    return p


def consume(token: str, clicker_aad: str, clicker_email: str, decision: str) -> dict:
    """Validate and consume: signature, expiry, bound approver, single use. Returns the row."""
    p = peek(token)
    if p.get("a") and p["a"] != clicker_aad:
        raise Invalid("this approval is addressed to someone else")
    c = store.con()
    with c:
        n = c.execute("UPDATE approvals SET used_at=?, used_by=?, decision=? WHERE nonce=? AND "
                      "used_at IS NULL AND expires_at > ?",
                      (time.time(), clicker_email, decision, p["n"], time.time())).rowcount
        row = c.execute("SELECT * FROM approvals WHERE nonce=?", (p["n"],)).fetchone()
    c.close()
    if row is None:
        raise Invalid("unknown approval")
    if n != 1:
        raise Invalid(f"already {row['decision'] or 'used'} by {row['used_by'] or 'someone'}")
    return dict(row)


def release(nonce: str) -> None:
    """Give an approval back (the action it carried was refused for that person)."""
    c = store.con()
    with c:
        c.execute("UPDATE approvals SET used_at=NULL, used_by='', decision='' WHERE nonce=?",
                  (nonce,))
    c.close()


def row(nonce: str) -> dict | None:
    c = store.con()
    r = c.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
    c.close()
    return dict(r) if r else None


def close(nonce: str, by: str, decision: str = "closed") -> bool:
    """The action was decided elsewhere (in the cockpit): the card's token is spent, so a
    late click on an old card cannot act a second time."""
    c = store.con()
    with c:
        n = c.execute("UPDATE approvals SET used_at=?, used_by=?, decision=? WHERE nonce=? AND "
                      "used_at IS NULL", (time.time(), by, decision, nonce)).rowcount
    c.close()
    return n == 1
