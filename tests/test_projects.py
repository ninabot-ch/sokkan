"""SOKKAN 3.2 lot 1 — projects data model, the "default project" migration, access
resolution (backend/projects.py) and the project columns added to board / agents."""
import sqlite3
import time

import pytest


@pytest.fixture()
def P(tmp_path, monkeypatch):
    import projects

    monkeypatch.setattr(projects, "DB", tmp_path / "projects.db")
    monkeypatch.setattr(projects, "_initialized_for", None)
    return projects


def U(email, role="dev"):
    return {"email": email, "role": role}


# ---- migration ------------------------------------------------------------------------

def test_default_project_is_created_once_with_the_instance_access_source(P):
    P.init()
    P.init(force=True)                      # idempotent: a restart never duplicates it
    rows = P.list_projects()
    assert [p["slug"] for p in rows] == ["default"]
    assert rows[0]["access_source"] == "instance"
    assert rows[0]["created_by"] == "migration-3.2"
    con = sqlite3.connect(P.DB)
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"


def test_instance_roles_keep_applying_to_the_default_project(P):
    """A 3.1 install upgraded to 3.2: everybody keeps exactly the rights they had."""
    assert P.effective_role(U("v@x", "viewer"), "default") == "viewer"
    assert P.effective_role(U("d@x", "dev"), "default") == "dev"
    assert P.effective_role(U("a@x", "admin"), "default") == "admin"
    assert P.effective_role(U("o@x", "owner"), "default") == "admin"
    assert P.effective_role(U("x@x", "none"), "default") is None
    assert P.readable_projects(U("v@x", "viewer")) == ["default"]
    assert not P.multi_project()


# ---- grants ---------------------------------------------------------------------------

def test_sso_group_project_grants_by_user_and_by_team(P):
    P.create("radio-play", "Radio player", created_by="admin@x")
    alice, bob, carol = U("alice@x"), U("bob@x"), U("carol@x", "admin")
    assert P.effective_role(alice, "radio-play") is None
    # an instance admin gets NO implicit access to a project's content
    assert P.effective_role(carol, "radio-play") is None
    P.grant("radio-play", "user", "Alice@X", "viewer")
    assert P.effective_role(alice, "radio-play") == "viewer"
    P.sync_sso_groups("bob@x", ["dev-radio", "everyone"])
    P.grant("radio-play", "team", "sso:dev-radio", "maintainer")
    assert P.effective_role(bob, "radio-play") == "maintainer"
    # best of the person's grants
    P.sync_sso_groups("alice@x", ["dev-radio"])
    assert P.effective_role(alice, "radio-play") == "maintainer"
    # the group disappears from the IdP claim at the next login → access gone
    P.sync_sso_groups("bob@x", ["everyone"])
    assert P.effective_role(bob, "radio-play") is None
    assert P.can(alice, "radio-play", "dev") and not P.can(alice, "radio-play", "admin")
    assert P.multi_project()
    assert P.readable_projects(bob) == ["default"]


def test_forge_projects_only_trust_a_fresh_cache_row(P):
    P.create("tv-api", "TV API", access_source="forge")
    dev = U("dev@x")
    assert P.effective_role(dev, "tv-api") is None
    con = sqlite3.connect(P.DB)
    now = time.time()
    con.execute("INSERT INTO access_cache VALUES(?,?,?,?,?,?)",
                ("dev@x", "tv-api", "dev", "gitlab", now, now + 300))
    con.commit()
    assert P.effective_role(dev, "tv-api") == "dev"
    assert P.effective_role(dev, "tv-api", now=now + 301) is None   # expired = no access


def test_archived_and_unknown_projects_grant_nothing(P):
    P.create("old", "Old")
    P.grant("old", "user", "a@x", "admin")
    con = sqlite3.connect(P.DB)
    con.execute("UPDATE projects SET archived_at=? WHERE slug='old'", (time.time(),))
    con.commit()
    assert P.effective_role(U("a@x"), "old") is None
    assert P.effective_role(U("a@x", "owner"), "nope") is None


def test_invalid_slugs_and_roles_are_refused(P):
    for bad in ("", "Radio", "a/b", "../x", "-x", "x" * 64):
        with pytest.raises(ValueError):
            P.create(bad, "x")
    P.create("ok-1", "ok")
    with pytest.raises(ValueError):
        P.create("ok-1", "again")
    with pytest.raises(ValueError):
        P.grant("ok-1", "user", "a@x", "owner")
    with pytest.raises(ValueError):
        P.create("ok-2", "x", access_source="ldap")


# ---- recall scope ---------------------------------------------------------------------

def test_recall_scope_is_one_project_and_fail_closed(P):
    P.create("radio-play", "Radio")
    assert P.recall_scope("radio-play") == ("radio-play",)
    assert P.recall_scope(None) == ("default",)        # session created before 3.2
    assert P.recall_scope("") == ("default",)
    assert P.recall_scope("ghost") == ()               # unknown project: nothing
    assert P.recall_scope("Bad Slug") == ()


def test_unknown_session_gets_default_only_while_single_project(P):
    assert P.session_scope(None) == ("default",)
    P.create("radio-play", "Radio")
    assert P.session_scope(None) == ()
    assert P.session_scope("radio-play") == ("radio-play",)


# ---- project columns of the other stores (no data lost) -------------------------------

def test_board_migration_keeps_31_rows_and_puts_them_in_default(tmp_path, monkeypatch):
    import board

    db = tmp_path / "board.db"
    con = sqlite3.connect(db)            # a 3.1 board.db: no `project` columns
    con.executescript("""
        CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            description TEXT DEFAULT '', tag TEXT DEFAULT 'backend',
            bucket TEXT NOT NULL DEFAULT 'Backlog', session_id TEXT, window TEXT,
            created_at REAL, sort REAL DEFAULT 0);
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, tag TEXT, window TEXT,
            title TEXT, prompt TEXT, created_at REAL, kind TEXT DEFAULT 'tmux',
            claude_session_id TEXT DEFAULT '', secrets TEXT DEFAULT NULL);
        INSERT INTO cards(title, created_at) VALUES ('keep me', 1.0);
        INSERT INTO sessions(session_id, tag, title, created_at, kind)
            VALUES ('s-31', 'old', 'an old session', 1.0, 'sdk');
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(board, "DB", db)
    board.init(force=True)
    assert board.get_session_project("s-31") == "default"
    assert board.get_session_project("missing") is None
    con = sqlite3.connect(db)
    assert con.execute("SELECT title, project FROM cards").fetchall() == [("keep me", "default")]
    assert con.execute("SELECT session_id, title, project FROM sessions").fetchall() == [
        ("s-31", "an old session", "default")]
    s = board.add_sdk_session("s-32", "new", project="radio-play")
    assert s["project"] == "radio-play"
    assert board.get_session_project("s-32") == "radio-play"


def test_agents_migration_puts_existing_agents_in_default(tmp_path, monkeypatch):
    import agents

    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    con = sqlite3.connect(tmp_path / "agents.db")
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(agents)")}
    assert "project" in cols and cols["project"][4] == "'default'"
