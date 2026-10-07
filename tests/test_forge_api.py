"""Lot 5 — GitLab in the cockpit, end to end against a fake GitLab (tests/fake_gitlab.py):
link with OAuth + PKCE, tokens encrypted and never shown/logged, project role = lowest
GitLab level over the project's repositories, cache 10 min / 2 min, refresh, immediate
revocation (unlink, 401), the forge-only person's onboarding, the credential endpoint."""
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import pytest

from fake_gitlab import FakeGitLab

ALICE, BOB = "alice@x", "bob@x"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import audit
    import auth
    import board
    import iam
    import projects
    from forge import access

    gl = FakeGitLab(tmp_path / "repos").start()
    gl.add_user("7", "alice")
    gl.add_user("8", "bob")
    for r in ("tv/api", "tv/web"):
        gl.add_repo(r)
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_AUTH_MODE", "oidc")            # feature `sso` (integration)
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_GITLAB", "1")
    monkeypatch.setenv("SOKKAN_GITLAB_URL", gl.url)
    monkeypatch.setenv("SOKKAN_GITLAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SOKKAN_GITLAB_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SOKKAN_PUBLIC_URL", "http://cockpit.test")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    iam.upsert_user("admin@x", "admin")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    projects.create("tv", "TV", access_source="forge", created_by="admin@x")
    access.add_repo("tv", "gitlab", gl.url, "tv/api")
    access.add_repo("tv", "gitlab", gl.url, "tv/web")
    who = {"email": ALICE}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    monkeypatch.setattr(a, "_bg", lambda coro: coro.close())
    c = TestClient(a.app, follow_redirects=False)

    def as_(email):
        who["email"] = email
        return c

    def link(email, uid):
        c = as_(email)
        r = c.get("/api/forge/gitlab/link")
        assert r.status_code == 302, r.text
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["scope"] == ["read_user read_api read_repository write_repository"]
        assert q["code_challenge_method"] == ["S256"]
        code = gl.consent(uid, q["code_challenge"][0])
        r = c.get("/api/forge/gitlab/callback", params={"code": code, "state": q["state"][0]})
        assert r.status_code == 302 and "forge=linked" in r.headers["location"], r.text
        return r

    yield {"c": c, "as": as_, "gl": gl, "link": link, "tmp": tmp_path, "a": a}
    gl.stop()


def _role(email, project="tv"):
    import projects
    return projects.effective_role({"email": email, "role": "none"}, project)


def _cache(email, project="tv"):
    import projects
    con = sqlite3.connect(projects.DB)
    r = con.execute("SELECT role, computed_at, expires_at FROM access_cache WHERE email=? AND "
                    "project=?", (email, project)).fetchone()
    con.close()
    return r


def _expire_cache():
    import projects
    con = sqlite3.connect(projects.DB)
    con.execute("UPDATE access_cache SET expires_at=?", (time.time() - 1,))
    con.commit()
    con.close()


def test_link_lowest_role_and_cache(world):
    gl = world["gl"]
    gl.set_level("tv/api", "7", 30)        # Developer
    gl.set_level("tv/web", "7", 20)        # Reporter
    # not linked: no access, and that "no" is cached 2 min
    assert _role(ALICE) is None
    role, comp, exp = _cache(ALICE)
    assert role is None and exp - comp == pytest.approx(120, abs=1)
    world["link"](ALICE, "7")
    # the link re-read the forge: Developer on api + Reporter on web → the LOWEST = viewer
    assert _role(ALICE) == "viewer"
    role, comp, exp = _cache(ALICE)
    assert role == "viewer" and exp - comp == pytest.approx(600, abs=1)
    gl.set_level("tv/web", "7", 40)
    assert _role(ALICE) == "viewer"        # cached for 10 min
    _expire_cache()
    assert _role(ALICE) == "dev"           # Developer + Maintainer → dev
    gl.set_level("tv/api", "7", 50)
    gl.set_level("tv/web", "7", 50)
    r = world["c"].post("/api/forge/refresh")     # "Refresh my access"
    assert r.json()["projects"]["tv"] == "admin"   # Owner everywhere → admin
    gl.set_level("tv/web", "7", None)             # removed from one repository
    world["c"].post("/api/forge/refresh")
    assert _role(ALICE) is None
    import audit
    con = sqlite3.connect(audit.DB)
    acts = [r[0] for r in con.execute("SELECT action FROM events")]
    assert "forge.link" in acts and "forge.access_changed" in acts


def test_links_screen_never_shows_a_token_and_tokens_are_encrypted(world, capsys):
    import projects
    gl = world["gl"]
    gl.set_level("tv/api", "7", 30)
    gl.set_level("tv/web", "7", 30)
    world["link"](ALICE, "7")
    tok = gl.tokens_of("7")[0]
    refresh = next(k for k, v in gl.refresh.items() if v == "7")
    d = world["c"].get("/api/forge/links").json()
    assert d["links"][0]["state"] == "active" and d["links"][0]["forge_username"] == "alice"
    assert d["projects"][0]["role"] == "dev"
    assert tok not in str(d) and "token_enc" not in str(d)
    raw = projects.DB.read_bytes()
    assert tok.encode() not in raw and refresh.encode() not in raw   # Fernet at rest
    key = (world["tmp"] / "forge.key")
    assert key.exists() and oct(key.stat().st_mode)[-3:] == "600"
    assert not (world["tmp"] / "vault.key").exists() or key.read_bytes() != (
        world["tmp"] / "vault.key").read_bytes()
    # nothing logged: API output, audit journal
    import audit
    out, err = capsys.readouterr()
    con = sqlite3.connect(audit.DB)
    journal = str(con.execute("SELECT * FROM events").fetchall())
    for secret in (tok, refresh):
        assert secret not in out and secret not in err and secret not in journal


def test_unlink_revokes_at_once(world):
    import projects
    gl = world["gl"]
    gl.set_level("tv/api", "7", 30)
    gl.set_level("tv/web", "7", 30)
    world["link"](ALICE, "7")
    assert _role(ALICE) == "dev"
    tok = gl.tokens_of("7")[0]
    r = world["c"].delete("/api/forge/links/gitlab")
    assert r.status_code == 200
    assert tok in gl.revoked                         # revoked at GitLab too
    assert _role(ALICE) is None                      # no 10-min grace
    con = sqlite3.connect(projects.DB)
    assert con.execute("SELECT count(*) FROM forge_links").fetchone()[0] == 0
    # the forge-only person can still reach the link screen, nothing else
    assert world["c"].get("/api/forge/links").status_code == 200
    assert world["c"].get("/api/sessions").status_code in (403, 404)


def test_gitlab_401_withdraws_access_and_erases_tokens(world):
    import projects
    gl = world["gl"]
    gl.set_level("tv/api", "7", 30)
    gl.set_level("tv/web", "7", 30)
    world["link"](ALICE, "7")
    assert _role(ALICE) == "dev"
    for t in gl.tokens_of("7"):
        gl.revoked.append(t)                          # token revoked on the GitLab side
    world["c"].post("/api/forge/refresh")
    assert _role(ALICE) is None
    con = sqlite3.connect(projects.DB)
    row = con.execute("SELECT token_enc, refresh_enc, revoked_at FROM forge_links").fetchone()
    assert row[0] == "" and row[1] == "" and row[2]
    assert world["c"].get("/api/forge/links").json()["links"][0]["state"] == "revoked"


def test_expired_token_is_refreshed_with_rotation(world):
    import projects
    from forge import links
    gl = world["gl"]
    gl.set_level("tv/api", "7", 30)
    gl.set_level("tv/web", "7", 30)
    world["link"](ALICE, "7")
    old = gl.tokens_of("7")[0]
    gl.expire_tokens("7")
    con = sqlite3.connect(projects.DB)
    con.execute("UPDATE forge_links SET expires_at=?", (time.time() + 10,))
    con.commit()
    new = links.access_token(ALICE, "gitlab", gl.url)
    assert new and new != old and len(gl.tokens_of("7")) == 2
    _expire_cache()
    assert _role(ALICE) == "dev"
    # a refused refresh (user blocked) = link revoked
    gl.users["7"]["state"] = "blocked"
    con.execute("UPDATE forge_links SET expires_at=?", (time.time() + 10,))
    con.commit()
    assert links.access_token(ALICE, "gitlab", gl.url) is None
    _expire_cache()
    assert _role(ALICE) is None


def test_unreachable_forge_keeps_the_cache_then_fails_closed(world):
    gl = world["gl"]
    gl.set_level("tv/api", "7", 30)
    gl.set_level("tv/web", "7", 30)
    world["link"](ALICE, "7")
    assert _role(ALICE) == "dev"
    gl.down = True
    from forge import access
    assert access.resolve(ALICE, "tv", force=True) == "dev"   # cached decision kept
    _expire_cache()
    assert _role(ALICE) is None                               # then nothing
    gl.down = False
    _expire_cache()
    assert _role(ALICE) == "dev"                              # link still valid


def test_oauth_state_is_bound_to_the_browser_and_the_person(world):
    c = world["as"](ALICE)
    r = c.get("/api/forge/gitlab/link")
    q = parse_qs(urlparse(r.headers["location"]).query)
    code = world["gl"].consent("7", q["code_challenge"][0])
    c.cookies.clear()
    assert c.get("/api/forge/gitlab/callback",
                 params={"code": code, "state": q["state"][0]}).status_code == 400
    # another person cannot finish alice's transaction
    c.cookies.set("sokkan_forge_tx", q["state"][0], path="/api/forge/")
    world["as"](BOB)
    assert c.get("/api/forge/gitlab/callback",
                 params={"code": code, "state": q["state"][0]}).status_code == 400


def test_feature_off_and_not_configured(world, monkeypatch):
    c = world["as"](ALICE)
    monkeypatch.delenv("SOKKAN_GITLAB_CLIENT_ID")
    assert c.get("/api/forge/gitlab/link").status_code == 409
    monkeypatch.setenv("SOKKAN_FEATURE_GITLAB", "0")
    assert c.get("/api/forge/links").status_code in (403, 404)
    assert _role(ALICE) is None


def test_admin_manages_the_repositories(world):
    c = world["as"]("admin@x")
    gl = world["gl"]
    assert len(c.get("/api/admin/projects/tv/repos").json()["repos"]) == 2
    r = c.post("/api/admin/projects/tv/repos", json={"repo_path": "../etc"})
    assert r.status_code == 400
    r = c.post("/api/admin/projects/tv/repos", json={"repo_path": "tv/docs"})
    assert r.status_code == 200 and {x["base_url"] for x in r.json()["repos"]} == {gl.url}
    r = c.request("DELETE", "/api/admin/projects/tv/repos", json={"repo_path": "tv/docs"})
    assert len(r.json()["repos"]) == 2
    world["as"](ALICE)
    assert c.get("/api/admin/projects/tv/repos").status_code == 403


def test_credential_endpoint_is_loopback_and_ticket_only(world):
    from forge import gitcred
    c = world["as"](ALICE)
    t = gitcred.ticket("s" * 32, ALICE)
    body = {"ticket": t, "protocol": "http", "host": urlparse(world["gl"].url).netloc}
    assert c.post("/api/forge/git-credential", json=body).status_code == 403  # testclient ≠ lo
    from forge import routes
    world["a"].app  # noqa: B018
    assert not routes.is_loopback(type("R", (), {"headers": {"x-forwarded-for": "1.2.3.4"},
                                                 "client": type("C", (), {"host": "127.0.0.1"})})())
    assert gitcred.verify(t) == ("s" * 32, ALICE)
    assert gitcred.verify(t[:-1] + ("0" if t[-1] != "0" else "1")) is None
    forged = gitcred.ticket("s" * 32, BOB).rsplit(".", 1)[0] + "." + t.rsplit(".", 1)[1]
    assert gitcred.verify(forged) is None
