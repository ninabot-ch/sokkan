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
