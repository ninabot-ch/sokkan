#!/usr/bin/env python3
"""oidc_logout.py — SOKKAN 3.4 « Bridge »: the identity provider ends a session, SOKKAN
follows.

* ``POST /api/auth/backchannel-logout`` — OpenID Connect Back-Channel Logout 1.0. The IdP
  (Authentik ≥ 2025.8, Keycloak, Okta…) posts a signed ``logout_token`` server to server.
  Validated by ``oidc.verify_logout_token`` (JWKS signature, iss, aud, iat, events, sid/sub,
  no nonce, jti) then the jti is consumed once (replay = 400). 200 = done (also when no
  session matches: nothing left to end), 400 = token refused, ``Cache-Control: no-store``.
* ``GET /api/auth/frontchannel-logout?sid=…[&iss=…]`` — OpenID Connect Front-Channel Logout,
  for IdPs without back-channel (Microsoft Entra ID). Off unless
  ``SOKKAN_OIDC_FRONTCHANNEL_LOGOUT=1``: the request is unsigned, the only proof is the
  sid (an opaque IdP session id), so it can only END a session, never open anything.

Both need OIDC configured and the ``revocation`` feature (404 otherwise). Effect:
``revocation.logout`` — cookies of that IdP session refused, its WebSockets closed, the
person's SDK sessions stopped cleanly when no other IdP session of theirs is signed in.
A logout is not a revocation: account, agents and forge tokens are untouched.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import oidc
import revocation

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def available() -> bool:
    return bool(oidc.ENABLED) and revocation.enabled()


def frontchannel_enabled() -> bool:
    return (os.environ.get("SOKKAN_OIDC_FRONTCHANNEL_LOGOUT") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _bad(why: str) -> JSONResponse:
    print(f"[sokkan] OIDC logout refused: {why}", file=sys.stderr)
    return JSONResponse({"error": "invalid_request", "error_description": why},
                        status_code=400, headers=NO_STORE)


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


async def handle_logout_token(token: str) -> tuple[int, dict]:
    """Validate a logout token and end what it names. Returns (status, body)."""
    try:
        claims = oidc.verify_logout_token(token)
    except oidc.LogoutTokenError as e:
        return 400, {"error": "invalid_request", "error_description": str(e)}
    if not revocation.consume_jti(str(claims["jti"])):
        return 400, {"error": "invalid_request", "error_description": "token replayed (jti)"}
    sid = str(claims.get("sid") or "")
    sub = str(claims.get("sub") or "")
    ended: list[dict] = []
    if sid:
        row = revocation.oidc_session(sid)
        if row is not None:
            if sub and row.get("sub") and row["sub"] != sub:
                return 400, {"error": "invalid_request",
                             "error_description": "sid and sub name different people"}
            ended.append(await revocation.logout(row["email"], sid=sid, by="idp"))
    else:
        for email in revocation.emails_for_sub(sub):
            ended.append(await revocation.logout(email, sid=None, by="idp"))
    return 200, {"ended": len(ended)}


def router() -> APIRouter:
    r = APIRouter()

    @r.post("/api/auth/backchannel-logout")
    async def backchannel_logout(request: Request):
        if not available():
            return _not_found()
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return _bad("expected application/x-www-form-urlencoded")
        raw = (await request.body())[:64 * 1024].decode("utf-8", errors="replace")
        token = (urllib.parse.parse_qs(raw).get("logout_token") or [""])[0].strip()
        if not token:
            return _bad("logout_token missing")
        status, body = await handle_logout_token(token)
        if status != 200:
            return _bad(body.get("error_description", "invalid"))
        return JSONResponse(body, status_code=200, headers=NO_STORE)

    @r.get("/api/auth/frontchannel-logout")
    async def frontchannel_logout(request: Request, sid: str = "", iss: str = ""):
        if not available() or not frontchannel_enabled():
            return _not_found()
        if iss:
            try:
                expected = oidc.discovery()["issuer"]
            except Exception:  # noqa: BLE001
                return _bad("issuer unknown (discovery failed)")
            if iss.rstrip("/") != str(expected).rstrip("/"):
                return _bad("iss does not match the configured issuer")
        if not sid:
            return _bad("sid missing")
        row = revocation.oidc_session(sid)
        if row is not None and not row.get("logged_out_at"):
            await revocation.logout(row["email"], sid=sid, by="idp", channel="frontchannel")
        import session as sess
        resp = Response("signed out", media_type="text/plain", headers=NO_STORE)
        if sess.sid_from_cookies(request.cookies) == sid:
            resp.delete_cookie(sess.COOKIE)
        return resp

    return r
