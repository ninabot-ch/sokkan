#!/usr/bin/env python3
"""cfaccess.py — SOKKAN : validation cryptographique du JWT Cloudflare Access.

Au lieu de faire confiance au header email (spoofable si on atteint le backend en
direct), on vérifie le JWT signé `Cf-Access-Jwt-Assertion` contre les clés publiques
du team (JWKS), avec contrôle de `aud` (l'app SOKKAN) et `iss` (le team Authentik/CF).
L'email vient du claim vérifié. Désactivé si SOKKAN_CF_TEAM/AUD absents (dev loopback).
"""
from __future__ import annotations

import os

import jwt
from jwt import PyJWKClient

TEAM = os.environ.get("SOKKAN_CF_TEAM", "").strip()  # ex. ninabot.cloudflareaccess.com
AUD = os.environ.get("SOKKAN_CF_AUD", "").strip()
ENABLED = bool(TEAM and AUD)
ISSUER = f"https://{TEAM}" if TEAM else ""
CERTS = f"https://{TEAM}/cdn-cgi/access/certs" if TEAM else ""

_jwks = PyJWKClient(CERTS) if ENABLED else None  # met en cache les clés


def validate(token: str) -> str | None:
    """Retourne l'email vérifié, ou lève en cas de JWT invalide. None si désactivé."""
    if not ENABLED or not token:
        return None
    key = _jwks.get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUD, issuer=ISSUER)
    return (claims.get("email") or "").lower() or None
