"""D2 of the on-prem audit — local auth knew ONE identity. Three people on one box must be
three names in the journal: a personal access code per account (backend/localaccounts.py)."""
import pytest
from fastapi.testclient import TestClient

PEOPLE = [("ana@pilot.example", "Ana", "proj-ana"), ("ben@pilot.example", "Ben", "proj-ben"),
          ("cleo@pilot.example", "Cléo", "proj-cleo")]


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """An instance in local auth mode, owner token set, multi_project on, empty stores."""
    import audit
    import auth
    import iam
    import localaccounts
    import projects
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")        # what the appliance profile sets
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(projects, "DB", tmp_path / "projects.db")
    iam.init(force=True)
    projects.init(force=True)
    monkeypatch.setattr(auth, "MODE", "local")
    monkeypatch.setattr(auth, "LOCAL_TOKEN", "owner-token-0123456789")
    monkeypatch.setattr(auth, "OWNER_EMAIL", "owner@localhost")
    import app as a
    a._login_fails.clear()
    return a, localaccounts, audit, projects


def _login(a, code) -> TestClient:
    c = TestClient(a.app)
    r = c.post("/api/auth/local", json={"token": code})
    assert r.status_code == 200, r.text
    return c


def test_three_people_are_three_identities_in_the_journal(box):
    a, la, audit, projects = box
    codes = {e: la.add(e, n, project=p)["code"] for e, n, p in PEOPLE}
    assert len(set(codes.values())) == 3
    clients = {e: _login(a, c) for e, c in codes.items()}
    for (e, n, p) in PEOPLE:
        me = clients[e].get("/api/me").json()
        assert me["email"] == e and me.get("name") == n, me
        mine = clients[e].get("/api/projects")
        if mine.status_code == 200:                         # only their own project is listed
            slugs = {x["slug"] for x in (mine.json().get("projects") if isinstance(mine.json(), dict) else mine.json())}
            assert p in slugs and not (slugs & {q for _, _, q in PEOPLE if q != p}), slugs
    # each person does something that is journaled: their name is on it, not the owner's
    for (e, n, p) in PEOPLE:
        r = clients[e].get("/api/audit")
        assert r.status_code in (200, 403)
    ana = clients[PEOPLE[0][0]]
    assert ana.get("/api/sessions", headers={"x-sokkan-project": "proj-ben"}).status_code == 404
    assert ana.get("/api/sessions", headers={"x-sokkan-project": "proj-ana"}).status_code == 200
    logins = [ev for ev in audit.recent(50) if ev["action"] == "auth.login"]
    assert {ev["user"] for ev in logins} == {e for e, _, _ in PEOPLE}
    assert "owner@localhost" not in {ev["user"] for ev in logins}
    # one project each, and only their own
    for (e, n, p) in PEOPLE:
        u = {"email": e, "role": "none", "known": False}      # no instance role at all
        assert projects.effective_role(u, p) == "admin"
        others = [q for _, _, q in PEOPLE if q != p]
        assert all(projects.effective_role(u, q) is None for q in others), e


def test_the_owner_token_still_signs_the_owner_in(box):
    a, la, audit, _ = box
    la.add("ana@pilot.example", "Ana", "dev")
    c = _login(a, "owner-token-0123456789")
    assert c.get("/api/me").json()["email"] == "owner@localhost"


def test_a_wrong_code_is_refused_and_journaled(box):
    a, la, audit, _ = box
    la.add("ana@pilot.example", "Ana", "dev")
    c = TestClient(a.app)
    assert c.post("/api/auth/local", json={"token": "sk-local-not-a-real-code"}).status_code == 401
    assert c.get("/api/me").status_code == 401
    assert any(ev["action"] == "auth.login.refused" for ev in audit.recent(10))


def test_codes_are_never_stored_or_listed(box, tmp_path):
    a, la, audit, _ = box
    code = la.add("ana@pilot.example", "Ana", "dev")["code"]
    assert code.startswith("sk-local-") and len(code) > 40
    blob = (tmp_path / "iam.db").read_bytes()
    assert code.encode() not in blob and code[len("sk-local-"):].encode() not in blob
    owner = _login(a, "owner-token-0123456789")
    listing = owner.get("/api/iam/access-codes")
    assert listing.status_code == 200 and code not in listing.text and "hash" not in listing.text
    assert [r["email"] for r in listing.json()] == ["ana@pilot.example"]
    assert code not in str(audit.recent(50))


def test_a_new_code_replaces_the_old_one_and_revoke_ends_the_open_session(box):
    a, la, audit, _ = box
    old = la.add("ana@pilot.example", "Ana", "dev")["code"]
    owner = _login(a, "owner-token-0123456789")
    r = owner.post("/api/iam/users/ana@pilot.example/access-code")
    assert r.status_code == 200
    new = r.json()["code"]
    assert new != old
    assert TestClient(a.app).post("/api/auth/local", json={"token": old}).status_code == 401
    ana = _login(a, new)
    assert ana.get("/api/me").status_code == 200
    assert owner.delete("/api/iam/users/ana@pilot.example/access-code").json() == {"ok": True}
    assert ana.get("/api/me").status_code == 401            # at once, not at cookie expiry
    assert TestClient(a.app).post("/api/auth/local", json={"token": new}).status_code == 401
    acts = [ev["action"] for ev in audit.recent(50)]
    assert "iam.access_code.create" in acts and "iam.access_code.revoke" in acts


def test_only_an_admin_issues_codes_and_only_for_known_accounts(box):
    a, la, audit, _ = box
    ana = _login(a, la.add("ana@pilot.example", "Ana", "dev")["code"])
    assert ana.post("/api/iam/users/ben@pilot.example/access-code").status_code == 403
    owner = _login(a, "owner-token-0123456789")
    assert owner.post("/api/iam/users/ghost@pilot.example/access-code").status_code == 404


def test_first_personal_code_closes_the_open_single_user_access(box, monkeypatch):
    """No owner token = open access as the owner. The day a named account exists, a visitor
    without a code must not be the owner any more."""
    a, la, audit, _ = box
    import auth
    monkeypatch.setattr(auth, "LOCAL_TOKEN", "")
    assert TestClient(a.app).get("/api/me").json()["email"] == "owner@localhost"
    code = la.add("ana@pilot.example", "Ana", "dev")["code"]
    assert TestClient(a.app).get("/api/me").status_code == 401
    assert auth.auth_info()["login_required"] is True
    assert _login(a, code).get("/api/me").json()["email"] == "ana@pilot.example"


def test_deleting_the_account_removes_its_code(box):
    a, la, audit, _ = box
    code = la.add("ana@pilot.example", "Ana", "dev")["code"]
    owner = _login(a, "owner-token-0123456789")
    assert owner.delete("/api/iam/users/ana@pilot.example").status_code == 200
    assert TestClient(a.app).post("/api/auth/local", json={"token": code}).status_code == 401
    assert la.accounts() == []


def test_cli_prints_the_code_once(box, capsys):
    a, la, audit, _ = box
    assert la._main(["add", "ana@pilot.example", "--name", "Ana", "--project", "proj-ana"]) == 0
    email, code = capsys.readouterr().out.strip().split("\t")
    assert email == "ana@pilot.example" and la.match(code) == email
    assert la._main(["list"]) == 0
    assert code not in capsys.readouterr().out
