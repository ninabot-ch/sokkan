"""teams.botauth — verification of an inbound Bot Framework request, BEFORE anything else.

* ``Authorization: Bearer <JWT>`` signed by a key of the Bot Framework OpenID metadata
  (``jwks_uri``), RS256, issuer ``https://api.botframework.com``, audience = our app id,
  not expired (5 min leeway);
* the key's endorsements include the activity's channel (``msteams``);
* the token's ``serviceUrl`` claim equals the activity's ``serviceUrl``, which must be an
  https URL of a Microsoft host (or one listed in ``SOKKAN_TEAMS_SERVICE_HOSTS``): replies
  carry our bot token, they never go anywhere else;
* the activity's tenant is the customer's (``SOKKAN_TEAMS_TENANT_ID``) — enforced here, on
  every activity: the bot registration is multi-tenant (a single-tenant Azure Bot would send
  tokens issued by Entra, not by ``api.botframework.com``).
"""
from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import jwt

import teams

_cache: dict = {"at": 0.0, "keys": {}, "jwks_uri": ""}
_lock = threading.Lock()
TTL_S = 24 * 3600


class Rejected(Exception):
    pass


def _refresh() -> None:
    with teams.http() as h:
        meta = h.get(teams.openid_url()).json()
        jwks = h.get(meta["jwks_uri"]).json()
    _cache.update(at=time.time(), jwks_uri=meta["jwks_uri"],
                  keys={k["kid"]: k for k in jwks.get("keys", []) if k.get("kid")})


def _key(kid: str) -> dict:
    with _lock:
        if time.time() - _cache["at"] > TTL_S or kid not in _cache["keys"]:
            _refresh()
        k = _cache["keys"].get(kid)
    if k is None:
        raise Rejected("unknown signing key")
    return k


def reset_cache() -> None:
    _cache.update(at=0.0, keys={}, jwks_uri="")


def service_url_ok(url: str) -> bool:
    u = urlparse(url or "")
    if u.scheme != "https" or not u.hostname:
        return False
    extra = tuple(h.strip() for h in (os.environ.get("SOKKAN_TEAMS_SERVICE_HOSTS") or "").split(",")
                  if h.strip())
    host = u.hostname.lower()
    return any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h))
               for h in teams.SERVICE_HOSTS + extra)


def tenant_of(activity: dict) -> str:
    return (((activity.get("channelData") or {}).get("tenant") or {}).get("id")
            or (activity.get("conversation") or {}).get("tenantId") or "")


def verify(authorization: str | None, activity: dict) -> dict:
    """Claims of a valid request, or Rejected."""
    if not teams.app_id() or not teams.tenant_id():
        raise Rejected("Teams app not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise Rejected("missing bearer token")
    token = authorization[7:].strip()
    try:
        head = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise Rejected("malformed token")
    if head.get("alg") != "RS256":
        raise Rejected("unexpected signing algorithm")
    jwk = _key(head.get("kid") or "")
    try:
        claims = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["RS256"],
                            audience=teams.app_id(), issuer=teams.BOT_ISSUER, leeway=300,
                            options={"require": ["exp", "iss", "aud"]})
    except jwt.PyJWTError as e:
        raise Rejected(f"invalid token: {e}")
    channel = activity.get("channelId") or ""
    if channel != "msteams":
        raise Rejected("not a Teams activity")
    endorse = jwk.get("endorsements")
    if endorse is not None and channel not in endorse:
        raise Rejected("signing key not endorsed for msteams")
    surl = activity.get("serviceUrl") or ""
    if claims.get("serviceurl", claims.get("serviceUrl")) not in (surl, surl.rstrip("/"),
                                                                 surl.rstrip("/") + "/"):
        raise Rejected("serviceUrl does not match the token")
    if not service_url_ok(surl):
        raise Rejected("serviceUrl is not a Microsoft host")
    if tenant_of(activity) != teams.tenant_id():
        raise Rejected("activity from another tenant")
    return claims
