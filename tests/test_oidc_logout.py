"""3.4 Bridge — OpenID Connect Back-Channel Logout 1.0 (and front-channel for Entra ID).

A fake identity provider: an RSA key, its JWKS served to the real PyJWKClient (fetch
monkeypatched), the discovery document set in `oidc`. Logins go through the real
callback (`/api/auth/callback`, id_token claims with `sid` / `sub`), the logout token
through the real middleware and route. Each check of the token has a refusal test.
"""
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from test_revocation import FakeWS, world  # noqa: F401 — the shared fixture

ISS = "https://idp.example.test/application/o/sokkan/"
CID = "sokkan-client"
EVENT = "http://schemas.openid.net/event/backchannel-logout"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _key()
OTHER = _key()


@pytest.fixture()
def idp(world, monkeypatch):  # noqa: F811
    import oidc
    monkeypatch.setattr(oidc, "ENABLED", True)
    monkeypatch.setattr(oidc, "CID", CID)
    monkeypatch.setattr(oidc, "_disc", {"issuer": ISS, "jwks_uri": "https://idp.example.test/jwks"})
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    client = jwt.PyJWKClient("https://idp.example.test/jwks")
    monkeypatch.setattr(client, "fetch_data", lambda: {"keys": [jwk]})
    monkeypatch.setattr(oidc, "_jwks", client)
    return world


def token(key=KEY, kid="k1", alg="RS256", headers=None, drop=(), **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": CID, "iat": now, "jti": uuid.uuid4().hex,
              "events": {EVENT: {}}, "sub": "sub-alice", "sid": "sid-A"}
    claims.update(over)
    for k in drop:
        claims.pop(k, None)
    h = {"kid": kid, "typ": "logout+jwt"}
    h.update(headers or {})
    return jwt.encode(claims, key, algorithm=alg, headers=h)


def post(w, tok):
    return w["c"].post("/api/auth/backchannel-logout", content=f"logout_token={tok}",
                       headers=FORM)


def login(w, monkeypatch, email, sid, sub="sub-alice"):
    """Real callback with an id_token carrying sid/sub; returns the cockpit cookie."""
    import oidc
    import session as sess
    monkeypatch.setattr(oidc, "exchange", lambda *x: {"id_token": "t"})
    monkeypatch.setattr(oidc, "verify_id_token", lambda t: {
        "email": email, "name": email, "groups": ["radio-devs"], "sid": sid, "sub": sub})
    tx = jwt.encode({"s": "st", "v": "ver", "exp": int(time.time()) + 600}, sess.SECRET,
                    algorithm="HS256")
    c = w["c"]
    c.cookies.set("sokkan_oidc_tx", tx)
    r = c.get("/api/auth/callback?code=x&state=st", follow_redirects=False)
    assert r.status_code == 302, r.text
    cookie = r.cookies.get(sess.COOKIE)
    c.cookies.clear()
    assert cookie
    time.sleep(1.05)  # cookies compare iat in whole seconds
    return cookie


def who(cookie):
    import session as sess

    class R:
        cookies = {sess.COOKIE: cookie}
    return sess.email_from_request(R())


def ws_with(email, cookie):
    import revocation
    import session as sess
    ws = FakeWS()
    ws.cookies = {sess.COOKIE: cookie}
    revocation.track(email, ws)
    return ws


def test_logout_by_sid_ends_that_session_only(idp, monkeypatch):
    import agentchat
    al = idp["alice"]()                                   # SDK session, agent, forge token, ws
    a = login(idp, monkeypatch, "alice@x", "sid-A")
    b = login(idp, monkeypatch, "alice@x", "sid-B")
    assert who(a) == "alice@x" and who(b) == "alice@x"
    wa, wb = ws_with("alice@x", a), ws_with("alice@x", b)
    r = post(idp, token(sid="sid-A"))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert who(a) is None, "cookie of the ended IdP session still honoured"
    assert who(b) == "alice@x", "another IdP session of the same person was cut"
    assert wa.closed == 4401 and wb.closed is None
    assert al["sid"] in agentchat._registry, "SDK session stopped while still signed in elsewhere"
    # the last IdP session ends → the SDK sessions stop cleanly, every socket closes
    r = post(idp, token(sid="sid-B"))
    assert r.status_code == 200
    assert who(b) is None
    assert al["sid"] not in agentchat._registry
    assert wb.closed == 4401 and al["ws"].closed == 4401


def test_logout_by_sub_ends_every_session_and_is_not_a_revocation(idp, monkeypatch):
    import agentchat
    import agents
    import audit
    import projects
    import revocation
    al = idp["alice"]()
    a = login(idp, monkeypatch, "alice@x", "sid-A")
    b = login(idp, monkeypatch, "alice@x", "sid-B")
    r = post(idp, token(drop=("sid",)))
    assert r.status_code == 200 and r.json()["ended"] == 1
    assert who(a) is None and who(b) is None
    assert al["sid"] not in agentchat._registry and al["ws"].closed == 4401
    # a logout is not a revocation
    assert not revocation.is_disabled("alice@x")
    assert agents.get(al["agent"]["id"])["status"] == "active"
    con = projects._con()
    fl = dict(con.execute("SELECT * FROM forge_links WHERE email='alice@x'").fetchone())
    con.close()
    assert fl["token_enc"] == "enc" and not fl["revoked_at"]
    assert projects.team_ids("alice@x") == ["sso:radio-devs"]
    # a new login works (cookies compare iat in whole seconds: the next second)
    time.sleep(1.05)
    c = login(idp, monkeypatch, "alice@x", "sid-C")
    assert who(c) == "alice@x"
    con = __import__("sqlite3").connect(audit.DB)
    n = con.execute("SELECT count(*) FROM events WHERE action='auth.backchannel_logout'"
                    ).fetchone()[0]
    con.close()
    assert n == 1


def test_unknown_session_is_200_and_nothing_happens(idp, monkeypatch):
    a = login(idp, monkeypatch, "alice@x", "sid-A")
    assert post(idp, token(sid="sid-unknown", sub="sub-alice")).status_code == 200
    assert post(idp, token(drop=("sid",), sub="sub-nobody")).status_code == 200
    assert who(a) == "alice@x"


@pytest.mark.parametrize("why,tok", [
    ("signature by another key", lambda: token(key=OTHER)),
    ("unknown kid", lambda: token(kid="nope")),
    ("wrong audience", lambda: token(aud="someone-else")),
    ("wrong issuer", lambda: token(iss="https://evil.example/")),
    ("nonce present", lambda: token(nonce="n")),
    ("events missing", lambda: token(drop=("events",))),
    ("events without the logout event", lambda: token(events={"x": {}})),
    ("event value not an object", lambda: token(events={EVENT: "yes"})),
    ("neither sid nor sub", lambda: token(drop=("sid", "sub"))),
    ("no jti", lambda: token(drop=("jti",))),
    ("no iat", lambda: token(drop=("iat",))),
    ("iat too old", lambda: token(iat=int(time.time()) - 3600)),
    ("iat in the future", lambda: token(iat=int(time.time()) + 3600)),
    ("expired", lambda: token(exp=int(time.time()) - 600)),
    ("wrong typ", lambda: token(headers={"typ": "at+jwt"})),
    ("symmetric algorithm", lambda: jwt.encode(
        {"iss": ISS, "aud": CID, "iat": int(time.time()), "jti": "j", "sid": "sid-A",
         "events": {EVENT: {}}}, "k" * 32, algorithm="HS256", headers={"kid": "k1"})),
    ("not a jwt", lambda: "garbage"),
])
def test_bad_tokens_are_400_and_end_nothing(idp, monkeypatch, why, tok):
    a = login(idp, monkeypatch, "alice@x", "sid-A")
    r = post(idp, tok())
    assert r.status_code == 400, f"{why}: {r.status_code} {r.text}"
    assert r.json()["error"] == "invalid_request"
    assert r.headers["cache-control"] == "no-store"
    assert who(a) == "alice@x", f"{why}: session ended anyway"


def test_replayed_token_is_refused(idp, monkeypatch):
    t = token(sid="sid-A")
    assert post(idp, t).status_code == 200
    r = post(idp, t)
    assert r.status_code == 400 and "jti" in r.json()["error_description"]


def test_sid_and_sub_of_different_people_is_refused(idp, monkeypatch):
    a = login(idp, monkeypatch, "alice@x", "sid-A", sub="sub-alice")
    assert post(idp, token(sid="sid-A", sub="sub-bob")).status_code == 400
    assert who(a) == "alice@x"


def test_form_is_required(idp):
    c = idp["c"]
    assert c.post("/api/auth/backchannel-logout", content="", headers=FORM).status_code == 400
    r = c.post("/api/auth/backchannel-logout", json={"logout_token": token()})
    assert r.status_code == 400 and "x-www-form-urlencoded" in r.json()["error_description"]


def test_endpoint_needs_no_cookie(idp, monkeypatch):
    import auth

    def nobody(request):
        raise HTTPException(401, "login required")
    monkeypatch.setattr(auth, "resolve_email", nobody)
    assert idp["c"].get("/api/me").status_code == 401        # the gate is on…
    assert post(idp, token(sid="sid-x")).status_code == 200  # …the IdP still gets through


def test_404_when_the_feature_or_oidc_is_off(idp, monkeypatch):
    import oidc
    monkeypatch.setenv("SOKKAN_FEATURE_REVOCATION", "0")
    assert post(idp, token()).status_code == 404
    monkeypatch.setenv("SOKKAN_FEATURE_REVOCATION", "1")
    monkeypatch.setattr(oidc, "ENABLED", False)
    assert post(idp, token()).status_code == 404


def test_frontchannel_is_off_by_default_then_ends_the_sid(idp, monkeypatch):
    c = idp["c"]
    a = login(idp, monkeypatch, "alice@x", "sid-A")
    b = login(idp, monkeypatch, "alice@x", "sid-B")
    assert c.get("/api/auth/frontchannel-logout?sid=sid-A").status_code == 404
    monkeypatch.setenv("SOKKAN_OIDC_FRONTCHANNEL_LOGOUT", "1")
    assert c.get("/api/auth/frontchannel-logout?sid=sid-A&iss=https://evil/").status_code == 400
    assert c.get("/api/auth/frontchannel-logout").status_code == 400
    assert who(a) == "alice@x"
    r = c.get("/api/auth/frontchannel-logout?sid=sid-A")        # Entra: sid without iss
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert who(a) is None and who(b) == "alice@x"
    assert c.get(f"/api/auth/frontchannel-logout?sid=sid-B&iss={ISS}").status_code == 200
    assert who(b) is None


def test_cookie_without_sid_still_works(idp, monkeypatch):
    """IdPs that send no sid (or cookies issued before 3.4): unaffected by a sid logout,
    ended by a subject-wide one."""
    a = login(idp, monkeypatch, "alice@x", "")
    assert who(a) == "alice@x"
    assert post(idp, token(sid="sid-A")).status_code == 200
    assert who(a) == "alice@x"
    assert post(idp, token(drop=("sid",))).status_code == 200
    assert who(a) is None


