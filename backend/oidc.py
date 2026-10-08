#!/usr/bin/env python3
"""oidc.py — SOKKAN : client OpenID Connect (Authorization Code + PKCE).

Provider configurable (Authentik pour nous, ou IdP client). Découverte via
.well-known/openid-configuration, échange code→tokens, vérif id_token (JWKS).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

import jwt
from jwt import PyJWKClient

ISSUER = os.environ.get("SOKKAN_OIDC_ISSUER", "").rstrip("/")
CID = os.environ.get("SOKKAN_OIDC_CLIENT_ID", "")
CSECRET = os.environ.get("SOKKAN_OIDC_CLIENT_SECRET", "")
SCOPES = os.environ.get("SOKKAN_OIDC_SCOPES", "openid email profile")
ENABLED = bool(ISSUER and CID and CSECRET)

UA = {"User-Agent": "SOKKAN/1.0"}  # Cloudflare bloque le UA Python-urllib par défaut
_disc: dict | None = None
_jwks: PyJWKClient | None = None


def discovery() -> dict:
    global _disc
    if _disc is None:
        req = urllib.request.Request(ISSUER + "/.well-known/openid-configuration", headers=UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            _disc = json.load(r)
    return _disc


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    verifier = _b64(secrets.token_bytes(40))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": CID, "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return discovery()["authorization_endpoint"] + "?" + q


def exchange(code: str, redirect_uri: str, verifier: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": CID, "client_secret": CSECRET, "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        discovery()["token_endpoint"], data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", **UA},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def verify_id_token(id_token: str) -> dict:
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(discovery()["jwks_uri"], headers=UA)
    key = _jwks.get_signing_key_from_jwt(id_token).key
    return jwt.decode(id_token, key, algorithms=["RS256"], audience=CID, issuer=discovery()["issuer"])


# ---- 3.4 Bridge: OpenID Connect Back-Channel Logout 1.0 (logout_token) -----------------
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
# asymmetric only: a token "signed" with HS256 and the public key as secret is refused
_ASYM = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
LOGOUT_LEEWAY_S = 60
LOGOUT_MAX_AGE_S = 600  # Authentik sends no `exp`: iat is the only clock, keep it fresh


class LogoutTokenError(ValueError):
    """The logout_token is not acceptable (the endpoint answers 400, per the spec)."""


def _jwks_client() -> PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(discovery()["jwks_uri"], headers=UA)
    return _jwks


def verify_logout_token(token: str) -> dict:
    """Validate a back-channel logout token (spec § 2.6): signature by a key of the IdP's
    JWKS (asymmetric algorithms only), iss, aud = our client id, iat (fresh, not in the
    future; exp checked when present), `events` carrying the back-channel logout event as
    a JSON object, `sid` and/or `sub`, NO `nonce`, a `jti` (replay is checked by the
    caller, which stores it). Returns the claims."""
    try:
        hdr = jwt.get_unverified_header(token)
    except Exception as e:  # noqa: BLE001
        raise LogoutTokenError(f"malformed token: {e}") from None
    typ = str(hdr.get("typ") or "")
    if typ and typ.lower() not in ("logout+jwt", "jwt"):
        raise LogoutTokenError(f"unexpected typ {typ}")
    if hdr.get("alg") not in _ASYM:
        raise LogoutTokenError(f"algorithm {hdr.get('alg')} not accepted")
    try:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=_ASYM, audience=CID,
                            issuer=discovery()["issuer"], leeway=LOGOUT_LEEWAY_S,
                            options={"require": ["iss", "aud", "iat"]})
    except Exception as e:  # noqa: BLE001 — bad signature, iss, aud, exp, unknown kid…
        raise LogoutTokenError(f"invalid token: {e}") from None
    now = time.time()
    try:
        iat = float(claims.get("iat"))
    except (TypeError, ValueError):
        raise LogoutTokenError("iat is not a number") from None
    # (an iat in the future is already refused by jwt.decode: ImmatureSignatureError)
    if now - iat > LOGOUT_MAX_AGE_S + LOGOUT_LEEWAY_S:
        raise LogoutTokenError("token too old")
    events = claims.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(LOGOUT_EVENT), dict):
        raise LogoutTokenError("events claim without the back-channel logout event")
    if "nonce" in claims:
        raise LogoutTokenError("a logout token must not carry a nonce")
    if not claims.get("sid") and not claims.get("sub"):
        raise LogoutTokenError("neither sid nor sub")
    if not claims.get("jti"):
        raise LogoutTokenError("no jti")
    return claims
