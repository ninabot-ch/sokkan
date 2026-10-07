"""A Microsoft Graph / Bot Framework simulator for the Teams tests (no real app, no network).

* OpenID metadata + JWKS of the Bot Framework (one RSA key, endorsed for msteams);
* the Entra ID token endpoint (client credentials) — counts the calls;
* the Bot Connector (replies and proactive messages are recorded in ``sent``);
* Graph ``/users/{id}/calendarView`` and ``/presence``.

It is plugged as the transport of every outbound call (``teams.TRANSPORT``); inbound
activities are signed with its key (``token()``), or with another key to test rejection.
"""
from __future__ import annotations

import json
import time
import uuid
from urllib.parse import unquote, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

APP_ID = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SERVICE = "https://smba.sim.botframework.com/emea/"
OPENID = "https://login.sim.botframework.com/v1/.well-known/openidconfiguration"
JWKS = "https://login.sim.botframework.com/v1/keys"
LOGIN = "https://login.sim.microsoftonline.com"
GRAPH = "https://graph.sim.microsoft.com/v1.0"
CHANNEL = "19:radio-general@thread.tacv2"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Sim:
    def __init__(self):
        self.key, self.other = _key(), _key()
        self.kid = "sim-key-1"
        self.sent: list[dict] = []
        self.token_calls: list[dict] = []
        self.graph_calls: list[str] = []
        self.events: dict[str, list[dict]] = {}

    # ---- signing of inbound activities --------------------------------------------------
    def token(self, *, aud=APP_ID, iss="https://api.botframework.com", service=SERVICE,
              exp_in=600, other_key=False, kid=None) -> str:
        now = int(time.time())
        return jwt.encode({"iss": iss, "aud": aud, "exp": now + exp_in, "nbf": now - 5,
                           "serviceurl": service},
                          self.other if other_key else self.key, algorithm="RS256",
                          headers={"kid": kid or self.kid})

    def jwk(self) -> dict:
        d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**d, "kid": self.kid, "use": "sig", "endorsements": ["msteams"]}

    # ---- the HTTP side ------------------------------------------------------------------
    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == OPENID:
            return httpx.Response(200, json={"issuer": "https://api.botframework.com",
                                             "jwks_uri": JWKS})
        if url == JWKS:
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if url.startswith(LOGIN):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            form = {k: unquote(v) for k, v in form.items()}
            self.token_calls.append(form)
            if form.get("client_secret") != "sim-secret":
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": f"tok-{uuid.uuid4().hex}",
                                             "expires_in": 3600})
        if url.startswith(SERVICE.rstrip("/")):
            if not req.headers.get("authorization", "").startswith("Bearer tok-"):
                return httpx.Response(401)
            body = json.loads(req.content)
            self.sent.append({"path": urlparse(url).path, **body})
            return httpx.Response(200, json={"id": uuid.uuid4().hex})
        if url.startswith(GRAPH):
            self.graph_calls.append(url)
            path = urlparse(url).path.removeprefix(urlparse(GRAPH).path)
            if path.endswith("/calendarView"):
                who = unquote(path.split("/")[2])
                return httpx.Response(200, json={"value": self.events.get(who, [])})
            if path.endswith("/presence"):
                return httpx.Response(200, json={"availability": "Busy"})
            return httpx.Response(404)
        return httpx.Response(599, text=f"simulator: no route for {url}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---- activities ---------------------------------------------------------------------
    def activity(self, text: str, aad: str, *, conv_type="channel", channel=CHANNEL,
                 tenant=TENANT, service=SERVICE, kind="message", value=None, name=None) -> dict:
        conv_id = (f"{channel};messageid=1700000000000" if conv_type == "channel"
                   else f"a:{aad}-personal" if conv_type == "personal" else "19:chat@thread.v2")
        a = {"type": kind, "id": uuid.uuid4().hex[:12], "channelId": "msteams",
             "serviceUrl": service, "text": f"<at>Nina</at> {text}",
             "from": {"id": f"29:{aad}", "aadObjectId": aad, "name": aad},
             "recipient": {"id": f"28:{APP_ID}", "name": "Nina"},
             "conversation": {"id": conv_id, "conversationType": conv_type, "tenantId": tenant},
             "channelData": {"tenant": {"id": tenant},
                             **({"channel": {"id": channel},
                                 "team": {"id": "19:team@thread", "aadGroupId": "g-1"}}
                                if conv_type == "channel" else {})}}
        if value is not None:
            a["value"] = value
        if name:
            a["name"] = name
        return a

    def texts(self) -> list[str]:
        return [m.get("text", "") for m in self.sent]

    def cards(self) -> list[dict]:
        return [att["content"] for m in self.sent for att in m.get("attachments") or []]
