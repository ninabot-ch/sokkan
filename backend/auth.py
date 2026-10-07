#!/usr/bin/env python3
"""auth.py — SOKKAN : abstraction d'authentification (provider par instance).

`SOKKAN_AUTH_MODE` choisit le provider :
- `local` (défaut, self-host) : single-user. Si `SOKKAN_LOCAL_TOKEN` est défini,
  un login par token pose le cookie de session ; sinon l'accès est ouvert et
  l'identité = `SOKKAN_OWNER_EMAIL` (instance sur réseau de confiance).
- `cf-access` : identité = JWT Cloudflare Access validé.
- `oidc`      : login OIDC (Authentik, Keycloak, …) → session cookie.
- `ldaps`     : bind LDAPS → session cookie (à venir).

`current_user(request)` est utilisable comme dépendance FastAPI.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request

import iam

MODE = os.environ.get("SOKKAN_AUTH_MODE", "local").strip()
DEV_USER = os.environ.get("SOKKAN_DEV_USER", "")
OWNER_EMAIL = os.environ.get("SOKKAN_OWNER_EMAIL", "owner@localhost")
OWNER_NAME = os.environ.get("SOKKAN_OWNER_NAME", "Owner")
LOCAL_TOKEN = os.environ.get("SOKKAN_LOCAL_TOKEN", "")


def _cf_token(request: Request) -> str | None:
    tok = request.headers.get("cf-access-jwt-assertion")
    if tok:
        return tok
    for part in (request.headers.get("cookie") or "").split(";"):
        part = part.strip()
        if part.startswith("CF_Authorization="):
            return part[len("CF_Authorization="):]
    return None


def _is_loopback(request: Request) -> bool:
    """Source réellement loopback ? (pas un X-Forwarded-* falsifiable — on lit
    l'IP de la socket). En prod le backend est joint via le proxy Next, donc
    request.client est le proxy ; le vrai loopback n'existe qu'en accès direct
    au conteneur (dev)."""
    host = getattr(request.client, "host", None) if request.client else None
    return host in ("127.0.0.1", "::1", "localhost")


def _email_cf_access(request: Request) -> str:
    import cfaccess

    # identité = JWT CF Access vérifié cryptographiquement (header email NON fiable).
    token = _cf_token(request)
    if cfaccess.ENABLED and token:
        try:
            email = cfaccess.validate(token)
        except Exception:  # noqa: BLE001
            raise HTTPException(401, "invalid CF Access JWT")
        if not email:
            raise HTTPException(401, "CF Access JWT has no email")
        return email
    # SANS JWT valide : n'accorder l'identité configurée QUE sur une source
    # réellement loopback (échappatoire dev). Toute requête distante qui
    # atteint le backend hors CF Access (LAN, edge caddy domaine-custom qui
    # court-circuite Access…) doit être refusée — sinon fallback owner silencieux.
    if _is_loopback(request):
        return DEV_USER or OWNER_EMAIL
    raise HTTPException(401, "CF Access JWT required")


def _email_session(request: Request) -> str:
    import session

    email = session.email_from_request(request)
    if not email:
        raise HTTPException(401, "login required")
    return email


def _email_local(request: Request) -> str:
    if not LOCAL_TOKEN:
        return OWNER_EMAIL  # pas de token configuré → single-user ouvert
    return _email_session(request)  # cookie posé par POST /api/auth/local


def resolve_email(request: Request) -> str:
    if MODE == "local":
        return _email_local(request)
    if MODE == "cf-access":
        return _email_cf_access(request)
    if MODE in ("oidc", "ldaps"):
        return _email_session(request)
    raise HTTPException(500, f"unknown SOKKAN_AUTH_MODE: {MODE}")


# 3.2 lot 5: someone whose ONLY access comes from GitLab has no role anywhere until they
# link their GitLab account — let them reach the few routes that do that, nothing else
_FORGE_ONBOARDING = ("/api/forge/", "/api/me", "/api/projects", "/api/features")


def _forge_onboarding(request: Request) -> bool:
    if not request.url.path.startswith(_FORGE_ONBOARDING):
        return False
    try:
        import features
        import projects
        return features.enabled("gitlab") and any(
            p["access_source"] == "forge" for p in projects.list_projects())
    except Exception:  # noqa: BLE001 — fail-closed
        return False


def instance_user(request: Request) -> dict:
    """The person with their INSTANCE role (iam.py), whatever the request's project."""
    email = resolve_email(request)
    import revocation  # 3.2 lot 6: a revoked / SCIM-deactivated account is refused at once
    if revocation.is_disabled(email):
        raise HTTPException(403, "account disabled on this instance")
    user = iam.get_user(email)
    if not user["known"] and iam.DEFAULT_ROLE == "none":
        # 3.2: someone the instance does not list may still be a member of a project
        # through an SSO team or a grant — let them in, with no instance role
        import projects
        try:
            if not projects.readable_projects(user) and not _forge_onboarding(request):
                raise HTTPException(403, "account not provisioned on this instance")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — fail-closed
            raise HTTPException(403, "account not provisioned on this instance")
    return user


def current_user(request: Request) -> dict:
    """The person as the request sees them. Inside a project-scoped request (3.2,
    projectgate), the role is THEIR ROLE IN THAT PROJECT, mapped onto the instance scale,
    so every existing check applies per project."""
    import projectgate
    ctx = projectgate.current()
    email = resolve_email(request)
    if ctx is not None and ctx.get("email") == (email or "").lower().strip():
        return ctx
    return instance_user(request)


def auth_info() -> dict:
    """Info pour le frontend : faut-il afficher /login, et lequel ?"""
    return {
        "mode": MODE,
        "login_required": MODE in ("oidc", "ldaps") or (MODE == "local" and bool(LOCAL_TOKEN)),
    }
