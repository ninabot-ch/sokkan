"""A person's linked forge accounts: configuration, encryption, storage, token freshness.

* Configuration (one GitLab per instance, self-hosted or gitlab.com): ``SOKKAN_GITLAB_URL``
  (default https://gitlab.com), ``SOKKAN_GITLAB_CLIENT_ID``, ``SOKKAN_GITLAB_CLIENT_SECRET``
  (confidential application; empty for a public PKCE-only application),
  ``SOKKAN_GITLAB_REDIRECT_URI`` (default ``$SOKKAN_PUBLIC_URL/api/forge/gitlab/callback``),
  ``SOKKAN_GITLAB_CA_BUNDLE`` (internal CA of a self-hosted GitLab, optional).
* Tokens are Fernet-encrypted at rest — the vault's scheme (vault.py: Fernet, key file
  generated once, 0600) with its OWN key file ``$SOKKAN_DATA_DIR/forge.key``
  (docs/MULTIUSER.md: a leaked vault key does not open the forge tokens and vice versa).
* ``access_token(link)`` refreshes server-side when the token expires within 2 minutes;
  GitLab rotates refresh tokens, so one refresh at a time per link (lock) and the new
  refresh token is stored at once. A refresh refused by GitLab (invalid_grant / 401) =
  the link is revoked: tokens erased, ``revoked_at`` set, access withdrawn.
* A token is never logged, never returned by an API route, never written anywhere but
  ``projects.db`` encrypted.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import sys
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

import projects
from forge import ForgeUnauthorized, Provider, Tokens, provider_class

REFRESH_MARGIN_S = 120
TX_TTL_S = 600


# ---- configuration ----------------------------------------------------------------------
def gitlab_url() -> str:
    return (os.environ.get("SOKKAN_GITLAB_URL") or "https://gitlab.com").strip().rstrip("/")


def configured(provider: str = "gitlab") -> bool:
    return provider == "gitlab" and bool((os.environ.get("SOKKAN_GITLAB_CLIENT_ID") or "").strip())


def redirect_uri(provider: str = "gitlab") -> str:
    v = (os.environ.get("SOKKAN_GITLAB_REDIRECT_URI") or "").strip()
    if v:
        return v
    pub = (os.environ.get("SOKKAN_PUBLIC_URL") or "http://localhost:3009").rstrip("/")
    return f"{pub}/api/forge/{provider}/callback"


def ca_bundle() -> str:
    return (os.environ.get("SOKKAN_GITLAB_CA_BUNDLE") or "").strip()


def norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def provider_for(provider: str, base_url: str) -> Provider:
    """The provider object for a repository's forge. Its OAuth client is the instance's
    only when the base URL is the configured one (a repository on another GitLab has no
    OAuth application here → no link possible → no access)."""
    cls = provider_class(provider)
    if provider == "gitlab" and norm_url(base_url) == norm_url(gitlab_url()):
        return cls(base_url, os.environ.get("SOKKAN_GITLAB_CLIENT_ID", "").strip(),
                   os.environ.get("SOKKAN_GITLAB_CLIENT_SECRET", "").strip(), ca_bundle())
    return cls(base_url)


# ---- encryption (the vault's scheme, own key) -------------------------------------------
def _key_path() -> str:
    d = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
    return os.environ.get("SOKKAN_FORGE_KEY_FILE") or os.path.join(d, "forge.key")


_key_lock = threading.Lock()


def _key() -> bytes:
    p = _key_path()
    with _key_lock:
        try:
            with open(p, "rb") as f:
                return f.read().strip()
        except OSError:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            k = Fernet.generate_key()
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(k)
            return k


def _enc(v: str) -> str:
    return Fernet(_key()).encrypt(v.encode()).decode() if v else ""


def _dec(v: str) -> str:
    if not v:
        return ""
    try:
        return Fernet(_key()).decrypt(v.encode()).decode()
    except InvalidToken:
        return ""        # key rotated without re-encryption: the link must be redone


def mac_key() -> bytes:
    """Key of the session tickets (forge.gitcred), derived from the forge key."""
    return hashlib.sha256(b"sokkan-gitcred-v1|" + _key()).digest()


# ---- OAuth transactions (state + PKCE verifier), bound to the cockpit person ------------
_tx: dict[str, dict] = {}
_tx_lock = threading.Lock()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                         ).rstrip(b"=").decode()
    return verifier, challenge


def begin(email: str, provider: str = "gitlab") -> tuple[str, str]:
    """→ (state, authorize URL). The state is single-use, 10 min, bound to ``email``."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _tx_lock:
        for k in [k for k, v in _tx.items() if v["exp"] < now]:
            _tx.pop(k, None)
        _tx[state] = {"email": email.lower().strip(), "verifier": verifier, "exp": now + TX_TTL_S,
                      "provider": provider}
    p = provider_for(provider, gitlab_url())
    return state, p.authorize_url(state, challenge, redirect_uri(provider))


def take(state: str, email: str) -> dict | None:
    """The transaction of ``state`` if it belongs to ``email`` and is fresh (single use)."""
    with _tx_lock:
        tx = _tx.pop(state or "", None)
    if not tx or tx["exp"] < time.time() or tx["email"] != email.lower().strip():
        return None
    return tx


# ---- storage ----------------------------------------------------------------------------
def _con() -> sqlite3.Connection:
    return projects._con()


PUBLIC_FIELDS = ("provider", "base_url", "forge_user_id", "forge_username", "scopes",
                 "expires_at", "linked_at", "last_refresh_at", "revoked_at")


def public(row: dict) -> dict:
    """What the cockpit may show about a link: never a token."""
    out = {k: row.get(k) for k in PUBLIC_FIELDS}
    now = time.time()
    out["state"] = ("revoked" if row.get("revoked_at") else
                    "expired" if not row.get("refresh_enc") and row.get("expires_at")
                    and row["expires_at"] < now else "active")
    return out


def get(email: str, provider: str, base_url: str) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM forge_links WHERE email=? AND provider=? AND base_url=?",
                    (email.lower().strip(), provider, norm_url(base_url))).fetchone()
    con.close()
    return dict(r) if r else None


def list_for(email: str) -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM forge_links WHERE email=? ORDER BY provider, base_url",
        (email.lower().strip(),))]
    con.close()
    return rows


def save(email: str, provider: str, base_url: str, tokens: Tokens, user_id: str,
         username: str) -> None:
    now = time.time()
    con = _con()
    with con:
        con.execute(
            "INSERT INTO forge_links(email, provider, base_url, forge_user_id, forge_username,"
            " token_enc, refresh_enc, scopes, expires_at, linked_at, last_refresh_at, revoked_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(email, provider, base_url) DO UPDATE"
            " SET forge_user_id=excluded.forge_user_id, forge_username=excluded.forge_username,"
            " token_enc=excluded.token_enc, refresh_enc=excluded.refresh_enc,"
            " scopes=excluded.scopes, expires_at=excluded.expires_at,"
            " linked_at=excluded.linked_at, last_refresh_at=excluded.last_refresh_at,"
            " revoked_at=NULL",
            (email.lower().strip(), provider, norm_url(base_url), user_id, username,
             _enc(tokens.access_token), _enc(tokens.refresh_token), tokens.scopes,
             tokens.expires_at, now, now))
    con.close()


def _store_refreshed(row: dict, tokens: Tokens) -> None:
    con = _con()
    with con:
        con.execute("UPDATE forge_links SET token_enc=?, refresh_enc=?, expires_at=?, scopes=?,"
                    " last_refresh_at=? WHERE email=? AND provider=? AND base_url=?",
                    (_enc(tokens.access_token), _enc(tokens.refresh_token or "") or
                     row["refresh_enc"], tokens.expires_at, tokens.scopes or row["scopes"],
                     time.time(), row["email"], row["provider"], row["base_url"]))
    con.close()


_listeners: list = []


def on_revoked(fn) -> None:
    """Register ``fn(email, provider, base_url, reason)``, called after a revocation."""
    _listeners.append(fn)


def revoke(email: str, provider: str, base_url: str, reason: str, by: str = "") -> None:
    """Erase the tokens, mark the link revoked, withdraw every cached forge access of the
    person (the next read is a fresh, negative one). Idempotent."""
    email = email.lower().strip()
    con = _con()
    with con:
        con.execute("UPDATE forge_links SET token_enc='', refresh_enc='', revoked_at=?"
                    " WHERE email=? AND provider=? AND base_url=?",
                    (time.time(), email, provider, norm_url(base_url)))
        con.execute("DELETE FROM access_cache WHERE email=? AND project IN"
                    " (SELECT slug FROM projects WHERE access_source='forge')", (email,))
    con.close()
    try:
        import audit
        audit.log(by or email, "forge.unlink" if reason == "unlink" else "forge.refresh_failed"
                  if reason == "refresh_failed" else "forge.revoked",
                  f"{provider} {norm_url(base_url)}", f"{email}: {reason}")
    except Exception:  # noqa: BLE001 — audit is best effort
        pass
    for fn in list(_listeners):
        try:
            fn(email, provider, norm_url(base_url), reason)
        except Exception as e:  # noqa: BLE001
            print(f"[forge] revocation listener failed: {type(e).__name__}", file=sys.stderr)


def delete(email: str, provider: str, base_url: str) -> None:
    con = _con()
    with con:
        con.execute("DELETE FROM forge_links WHERE email=? AND provider=? AND base_url=?",
                    (email.lower().strip(), provider, norm_url(base_url)))
    con.close()


_refresh_locks: dict[tuple, threading.Lock] = {}
_refresh_guard = threading.Lock()


def _lock_for(key: tuple) -> threading.Lock:
    with _refresh_guard:
        return _refresh_locks.setdefault(key, threading.Lock())


def access_token(email: str, provider: str, base_url: str) -> str | None:
    """A valid access token of the person for that forge, refreshed if needed; None when
    there is no active link. Raises ForgeUnavailable when the forge cannot be reached for
    a needed refresh; a refused refresh revokes the link and returns None."""
    key = (email.lower().strip(), provider, norm_url(base_url))
    row = get(*key)
    if not row or row.get("revoked_at") or not row.get("token_enc"):
        return None
    if row.get("expires_at") is None or row["expires_at"] - time.time() > REFRESH_MARGIN_S:
        return _dec(row["token_enc"]) or None
    with _lock_for(key):
        row = get(*key)                          # another thread may have refreshed it
        if not row or row.get("revoked_at") or not row.get("token_enc"):
            return None
        if row.get("expires_at") and row["expires_at"] - time.time() > REFRESH_MARGIN_S:
            return _dec(row["token_enc"]) or None
        p = provider_for(provider, base_url)
        try:
            tokens = p.refresh(_dec(row["refresh_enc"]), redirect_uri(provider))
        except ForgeUnauthorized:
            revoke(email, provider, base_url, "refresh_failed")
            return None
        _store_refreshed(row, tokens)
        return tokens.access_token
