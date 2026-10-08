"""3.4.1 — SOKKAN_DEFAULT_ROLE=none: an SSO sign-in the instance does not list is CONNECTED with
no instance role (no 403), sees nothing of `default`, and reaches only the projects a grant or
an SSO team gives them. Seen live on 08.10: a second user of the tenant, fallback `viewer`,
listed the owner's sessions."""
from test_project_isolation import DEFAULT_SID, world  # noqa: F401 — the fixture


def test_edition_decides_the_default(monkeypatch):
    import iam
    monkeypatch.delenv("SOKKAN_DEFAULT_ROLE", raising=False)
    monkeypatch.delenv("SOKKAN_EDITION", raising=False)
    assert iam.default_role() == "viewer"                 # community: as before
    monkeypatch.setenv("SOKKAN_EDITION", "enterprise")
    assert iam.default_role() == "none"
    monkeypatch.setenv("SOKKAN_DEFAULT_ROLE", "viewer")   # explicit always wins
    assert iam.default_role() == "viewer"
    monkeypatch.setenv("SOKKAN_DEFAULT_ROLE", "boss")
    import pytest
    with pytest.raises(RuntimeError):
        iam.default_role()


def test_unknown_sso_user_is_in_with_no_role_and_sees_nothing_of_default(world):  # noqa: F811
    c = world["as"]("stranger@x")                          # default project (header absent)
    r = c.get("/api/me")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["role"] == "none" and me["project_role"] is None and me["known"] is False
    assert me["instance_role"] == "none"
    assert c.get("/api/projects").json()["projects"] == []       # not even `shared`
    assert c.get("/api/sessions").status_code == 404             # default does not exist for them
    assert c.get(f"/api/sessions/{DEFAULT_SID}").status_code == 404
    assert c.get("/api/board").status_code == 404
    assert c.get("/api/memory/notes").status_code == 404
    assert c.get("/api/audit").status_code == 403                # instance-level: no role
    assert world["as"]("stranger@x", "radio").get("/api/sessions").status_code == 404


def test_a_grant_or_an_sso_team_opens_exactly_that_project(world):  # noqa: F811
    import projects
    projects.sync_sso_groups("newbie@x", ["radio-devs"])     # the groups claim of her login
    c = world["as"]("newbie@x", "radio")
    me = c.get("/api/me").json()
    assert me["role"] == "dev" and me["project_role"] == "dev" and me["instance_role"] == "none"
    assert [s["title"] for s in c.get("/api/sessions").json()] == ["radio work"]
    slugs = {p["slug"] for p in c.get("/api/projects").json()["projects"]}
    assert slugs == {"radio", "shared"}                         # shared opens with the first project
    assert world["as"]("newbie@x", "default").get("/api/sessions").status_code == 404
    assert world["as"]("newbie@x", "default").get("/api/me").json()["role"] == "none"
    projects.grant("radio", "user", "guest@x", "viewer", created_by="admin@x")
    g = world["as"]("guest@x", "radio")
    assert g.get("/api/me").json()["role"] == "viewer"
    assert g.get("/api/sessions").status_code == 200
