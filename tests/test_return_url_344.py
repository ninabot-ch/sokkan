"""3.4.4 — un lien de carte (Teams outreach) ouvert sans session : après le SSO la personne
atterrissait sur Helm, la carte perdue ; et le lien visait Build sans projet (prod 09.10)."""
import jwt
from test_revocation import world  # noqa: F401 — the shared fixture


def test_safe_next_keeps_local_paths_only():
    import app as a
    assert a._safe_next("/?plane=control&tab=board&project=radio&card=16") == \
        "/?plane=control&tab=board&project=radio&card=16"
    for bad in ("https://evil.example/", "//evil.example/x", "/\\evil", "/api/auth/login",
                "javascript:alert(1)", "", "/x\r\nSet-Cookie: a=b"):
        assert a._safe_next(bad) == "/", bad


def test_login_carries_the_target_through_the_callback(world, monkeypatch):  # noqa: F811
    import oidc
    import session as sess
    c = world["c"]
    monkeypatch.setattr(oidc, "ENABLED", True)
    monkeypatch.setattr(oidc, "authorize_url", lambda *x: "https://idp.example.test/auth")
    target = "/?plane=control&tab=board&project=radio&card=16"
    r = c.get("/api/auth/login", params={"next": target}, follow_redirects=False)
    assert r.status_code == 302
    tx = r.cookies.get("sokkan_oidc_tx")
    state = jwt.decode(tx, sess.SECRET, algorithms=["HS256"])["s"]
    monkeypatch.setattr(oidc, "exchange", lambda *x: {"id_token": "t"})
    monkeypatch.setattr(oidc, "verify_id_token", lambda t: {
        "email": "alice@x", "name": "Alice", "sid": "s1", "sub": "sub-a"})
    c.cookies.set("sokkan_oidc_tx", tx)
    r = c.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False)
    c.cookies.clear()
    assert r.status_code == 302, r.text
    assert r.headers["location"].endswith(target)


def test_outreach_card_link_opens_the_card_in_its_project(monkeypatch):
    import teams
    from teams import outreach
    monkeypatch.setattr(teams, "public_url", lambda: "https://sokkan.example")
    assert outreach.card_link("sales emea", 16) == \
        "https://sokkan.example/?plane=control&tab=board&project=sales%20emea&card=16"
