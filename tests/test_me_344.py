"""3.4.4 — /api/me : le bandeau montrait « admin » pour une maintainer de projet, et son
e-mail comme nom (prod 09.10, Léa Demo : rôle d'instance none, accès par grant)."""
from test_project_isolation import world  # noqa: F401 — the fixture


def test_maintainer_is_labelled_maintainer_and_named_from_the_login(world, monkeypatch):  # noqa: F811
    import projects
    import session as sess
    monkeypatch.setattr(sess, "SECRET", "t" * 32)
    projects.grant("radio", "user", "lea@x", "maintainer", created_by="admin@x")
    c = world["as"]("lea@x", "radio")
    c.cookies.set(sess.COOKIE, sess.make("lea@x", "Léa Demo"))
    try:
        me = c.get("/api/me").json()
    finally:
        c.cookies.clear()
    assert me["role"] == "admin"                 # the scale the UI checks: unchanged
    assert me["role_label"] == "maintainer"      # what the header shows
    assert me["name"] == "Léa Demo"              # not lea@x


def test_name_falls_back_to_the_address(world):  # noqa: F811
    me = world["as"]("stranger@x").get("/api/me").json()
    assert me["name"] == "stranger@x" and me["role_label"] == "none"
