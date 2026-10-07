"""SOKKAN 3.2 lot 3 — two projects on one instance: nothing of one reaches a person of the
other, through any cockpit route, MCP server or WebSocket.

Every test drives the REAL request path (middleware → projectgate → route), only the
identity is chosen (`auth.resolve_email`). People:
  * bob@x    — instance `dev` (= dev of `default`), nothing in `radio`;
  * alice@x  — unknown to the instance, `dev` of `radio` through the SSO group `radio-devs`;
  * admin@x  — instance `admin`, no grant in `radio` (decision of 07.10: no content access
               until they add themselves, which is logged).
Each test fails when its filter is removed (checked by mutation for the gate, the session
list, the board, agents, memory and the MCP servers)."""
import pytest

DEFAULT_SID = "d" * 32
RADIO_SID = "r" * 32


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import agents
    import app as a
    import audit
    import auth
    import board
    import iam
    import projects
    import quarantine
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")      # unknown people: no instance role
    iam.upsert_user("bob@x", "dev")
    iam.upsert_user("admin@x", "admin")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    vault.set_secret("DEFAULT_DB_PASSWORD", "s3cret-of-the-default-team")
    projects.create("radio", "Radio player", created_by="admin@x")
    projects.sync_sso_groups("alice@x", ["radio-devs"])
    projects.grant("radio", "team", "sso:radio-devs", "dev")
    # content in both projects
    board.add_sdk_session(DEFAULT_SID, "backend", title="default secret plan", project="default")
    board.add_sdk_session(RADIO_SID, "backend", title="radio work", project="radio")
    d_card = board.add_card("default roadmap", "confidential", project="default")
    r_card = board.add_card("radio bug", "player stalls", project="radio")
    d_agent = agents.create({"email": "bob@x", "role": "dev", "project": "default"},
                            {"name": "default-audit", "purpose": "p", "deliverable": "d",
                             "trigger": "manual"}, activate=True)
    r_agent = agents.create({"email": "alice@x", "role": "dev", "project": "radio"},
                            {"name": "radio-check", "purpose": "p", "deliverable": "d",
                             "trigger": "manual"}, activate=True)
    quarantine.write("default-finding", "default only", "body", {"agent": "x"})
    quarantine.write("radio-finding", "radio only", "body", {"agent": "y"}, project="radio")

    who = {"email": "bob@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    monkeypatch.setattr(a, "_bg", lambda coro: coro.close())
    monkeypatch.setattr(a, "_memory_preseed", lambda *x, **k: "")
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    return {"c": c, "as": as_, "d_card": d_card, "r_card": r_card, "d_agent": d_agent,
            "r_agent": r_agent, "tmp": tmp_path}


# ---- sessions -------------------------------------------------------------------------

def test_session_list_and_detail_stay_in_the_project(world):
    c = world["as"]("alice@x", "radio")
    ids = [s["session_id"] for s in c.get("/api/sessions").json()]
    assert ids == [RADIO_SID]
    assert c.get(f"/api/sessions/{DEFAULT_SID}").status_code == 404
    # asking for the default project does not work either: no role there
    assert world["as"]("alice@x", "default").get("/api/sessions").status_code == 404
    c = world["as"]("bob@x")
    assert [s["session_id"] for s in c.get("/api/sessions").json()] == [DEFAULT_SID]
    assert c.get(f"/api/sessions/{RADIO_SID}").status_code == 404
    assert world["as"]("bob@x", "radio").get("/api/sessions").status_code == 404


def test_spawn_in_the_selected_project_without_terminal_or_default_secrets(world):
    c = world["as"]("alice@x", "radio")
    r = c.post("/api/spawn", json={"tag": "x", "kind": "tmux"})
    assert r.status_code == 400 and "default project" in r.json()["detail"]
    r = c.post("/api/spawn", json={"tag": "x", "secrets": ["DEFAULT_DB_PASSWORD"]})
    assert r.status_code == 400 and "not in the vault" in r.json()["detail"]
    r = c.post("/api/spawn", json={"tag": "x", "project": "default"})
    assert r.status_code == 400
    assert c.get("/api/vault/session").json()["names"] == []
    r = c.post("/api/spawn", json={"tag": "x"})
    assert r.status_code == 200 and r.json()["project"] == "radio"


# ---- board ----------------------------------------------------------------------------

def test_board_is_per_project(world):
    c = world["as"]("alice@x", "radio")
    cards = [x["title"] for col in c.get("/api/board").json()["cards"].values() for x in col]
    assert cards == ["radio bug"]
    d = world["d_card"]["id"]
    assert c.get(f"/api/board/card/{d}").status_code == 404
    assert c.patch(f"/api/board/card/{d}", json={"title": "pwned"}).status_code == 404
    assert c.delete(f"/api/board/card/{d}").status_code == 404
    new = c.post("/api/board/card", json={"title": "radio task"}).json()
    assert new["project"] == "radio"
    bob = world["as"]("bob@x")
    assert "radio task" not in str(bob.get("/api/board").json())


def test_board_mcp_sees_only_its_project(world, monkeypatch):
    import board_mcp

    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "alice@x")
    d = world["d_card"]["id"]
    assert "not found" in board_mcp.get_card(d)["error"]
    assert "not found" in board_mcp.close_card(d)["error"]
    titles = [x["title"] for col in board_mcp.list_board().values() for x in col]
    assert titles == ["radio bug"]
    assert board_mcp.search_cards("roadmap")["cards"] == []
    assert [x["title"] for x in board_mcp.search_cards("")["cards"]] == ["radio bug"]
    # a link to an object of another project "does not exist"
    r = board_mcp.link_card(world["r_card"]["id"], "session", DEFAULT_SID)
    assert "error" in r


# ---- agents ---------------------------------------------------------------------------

def test_agents_are_per_project(world, monkeypatch):
    c = world["as"]("alice@x", "radio")
    names = [x["name"] for x in c.get("/api/agents").json()["agents"]]
    assert names == ["radio-check"]
    assert c.get(f"/api/agents/{world['d_agent']['id']}").status_code == 404
    assert c.post(f"/api/agents/{world['d_agent']['id']}/pause").status_code == 404
    assert c.get("/api/agents/meta").json()["secrets"] == []

    # a MAINTAINER of radio (≈ admin inside the project) still sees nothing of default
    import projects
    projects.grant("radio", "user", "carol@x", "maintainer")
    m = world["as"]("carol@x", "radio")
    assert [x["name"] for x in m.get("/api/agents").json()["agents"]] == ["radio-check"]
    assert m.get(f"/api/agents/{world['d_agent']['id']}").status_code == 404

    import agents_mcp
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "alice@x")
    assert [a["name"] for a in agents_mcp.list_agents()] == ["radio-check"]
    assert "error" in agents_mcp.get_agent("default-audit")


# ---- memory: quarantine, notes, search, CortHeXis ------------------------------------

def test_quarantine_is_per_project(world):
    c = world["as"]("alice@x", "radio")
    assert [q["name"] for q in c.get("/api/memory/quarantine").json()] == ["radio-finding"]
    assert c.get("/api/memory/quarantine/default-finding").status_code == 404
    assert c.post("/api/memory/quarantine/default-finding/approve").status_code == 404
    r = c.post("/api/memory/quarantine/radio-finding/approve")
    assert r.status_code == 200
    assert (world["tmp"] / "projects" / "radio" / "memory" / "radio-finding.md").exists()
    assert not (world["tmp"] / "memory" / "radio-finding.md").exists()


class _ScopedStore:
    """Notes of default / radio / shared; honours the scope like the Postgres store, and
    records what scope each call received."""

    def __init__(self):
        from core.contract import NoteRecord
        self.rows = {(p, n): NoteRecord(n, f"{p} {n}", "project", 0, f"/{p}/{n}.md", None,
                                        "indexed", f"body of {p}/{n}", p)
                     for p, n in (("default", "default-plan"), ("radio", "radio-runbook"),
                                  ("shared", "conventions"), ("default", "runbook-db"))}
        self.scopes = []

    def list_notes(self, projects=None):
        self.scopes.append(projects)
        return [{"name": n, "project": p, "description": r.description, "mtime": 0,
                 "chunks": 1} for (p, n), r in self.rows.items()
                if projects is None or p in projects]

    def resolve_note(self, name, projects=None):
        self.scopes.append(projects)
        for (p, n), r in self.rows.items():
            if n == name and (projects is None or p in projects):
                return r
        return None

    def find_note_by_path(self, stem, projects=None):
        return None

    def search(self, qv, text, k, rerank=None, projects=None, **kw):
        from core.search import Hit
        self.scopes.append(projects)
        return [Hit(n, 0.9, 0.9, 0.1, None, r.body, 1, "indexed", description=r.description,
                    project=p) for (p, n), r in self.rows.items()
                if projects is None or p in projects][:k]

    def recall_log(self, **kw):
        self.scopes.append(kw.get("projects"))
        return []

    def recall_summary(self):
        return {}


@pytest.fixture()
def pg_like(monkeypatch):
    import store_backend
    st = _ScopedStore()
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    monkeypatch.setattr(store_backend, "get_store", lambda: st)
    monkeypatch.setattr(store_backend, "embed_query", lambda q, timeout=30.0: [1.0])
    monkeypatch.setattr(store_backend, "_reranker", lambda deep=False: None)
    monkeypatch.setattr(store_backend, "age_header", lambda *a: "[h]")
    return st


def test_memory_routes_read_the_project_and_shared_only(world, pg_like):
    c = world["as"]("alice@x", "radio")
    names = {n["name"] for n in c.get("/api/memory/notes").json()}
    assert names == {"radio-runbook", "conventions"}
    hits = {h["note_name"] for h in c.get("/api/memory/search", params={"q": "plan"}).json()}
    assert hits == {"radio-runbook", "conventions"}
    assert c.get("/api/memory/note/default-plan").json()["body"] is None
    assert "radio" in c.get("/api/memory/note/radio-runbook").json()["body"]
    assert [r["name"] for r in c.get("/api/runbooks").json()] == []
    assert c.post("/api/runbooks/runbook-db/run").status_code == 404
    c.get("/api/memory/recall-log")
    assert pg_like.scopes[-1] == ["radio"]
    bob = world["as"]("bob@x")
    assert {n["name"] for n in bob.get("/api/memory/notes").json()} == {
        "default-plan", "conventions", "runbook-db"}


def test_corthexis_tab_reads_the_project_directory_and_no_default_review(world):
    mem = world["tmp"] / "memory"
    mem.mkdir()
    (mem / "default_plan.md").write_text("---\nname: default-plan\ndescription: d\n---\nbody\n")
    rdir = world["tmp"] / "projects" / "radio" / "memory"
    rdir.mkdir(parents=True)
    (rdir / "radio_runbook.md").write_text("---\nname: radio-runbook\ndescription: r\n---\nb\n")
    c = world["as"]("alice@x", "radio")
    g = c.get("/api/corthexis/graph").json()
    assert [n["id"] for n in g["nodes"]] == ["radio-runbook"]
    assert c.get("/api/corthexis/note/default-plan").status_code == 404
    assert c.get("/api/corthexis/review").json()["report"] == {}
    assert c.get("/api/corthexis/proposals").json() == []
    assert c.post("/api/corthexis/review/run").status_code == 409


# ---- usage, audit, Operate, instance admin --------------------------------------------

def test_usage_lists_only_the_project_sessions_and_audit_is_for_admins(world, monkeypatch):
    import usage
    monkeypatch.setattr(usage, "summary", lambda days_back=30: {
        "totals": {}, "sessions": [{"session_id": DEFAULT_SID, "title": "default secret plan"},
                                   {"session_id": RADIO_SID, "title": "radio work"},
                                   {"session_id": "x" * 32, "title": "unknown prompt"}]})
    c = world["as"]("alice@x", "radio")
    assert [s["title"] for s in c.get("/api/usage").json()["sessions"]] == ["radio work"]
    assert world["as"]("bob@x").get("/api/audit").status_code == 403
    assert world["as"]("admin@x").get("/api/audit").status_code == 200


def test_operate_is_for_the_ops_team_and_admins(world):
    import projects
    assert world["as"]("alice@x").get("/api/observability").status_code == 403
    assert world["as"]("bob@x").get("/api/infra/nodes").status_code == 403
    assert world["as"]("admin@x").get("/api/observability").status_code == 200
    projects.set_ops_group("sre")
    projects.sync_sso_groups("alice@x", ["radio-devs", "sre"])
    assert world["as"]("alice@x").get("/api/observability").status_code == 200
    me = world["as"]("alice@x").get("/api/me").json()
    assert me["ops"] is True


def test_instance_admin_sees_no_content_until_they_add_themselves(world):
    import audit
    adm = world["as"]("admin@x", "radio")
    assert adm.get("/api/sessions").status_code == 404
    assert adm.get(f"/api/board/card/{world['r_card']['id']}").status_code == 404
    listing = world["as"]("admin@x").get("/api/admin/projects").json()
    assert {p["slug"] for p in listing["projects"]} == {"default", "shared", "radio"}
    assert "radio work" not in str(listing)                 # administration, not content
    r = world["as"]("admin@x").post("/api/admin/projects/radio/grants", json={
        "principal_kind": "user", "principal": "admin@x", "role": "viewer"})
    assert r.status_code == 200
    assert [x["action"] for x in audit.recent(limit=5)][0] == "project.grant.self"
    assert [s["session_id"] for s in world["as"]("admin@x", "radio").get(
        "/api/sessions").json()] == [RADIO_SID]
    # a dev cannot administer
    assert world["as"]("bob@x").get("/api/admin/projects").status_code == 403


def test_project_selector_lists_what_the_person_can_read(world):
    p = world["as"]("alice@x").get("/api/projects").json()
    assert {(x["slug"], x["role"]) for x in p["projects"]} == {("radio", "dev"),
                                                                ("shared", "viewer")}
    assert p["multi"] is True
    p = world["as"]("bob@x").get("/api/projects").json()
    assert {x["slug"] for x in p["projects"]} == {"default", "shared"}


def test_websocket_of_another_project_session_is_refused(world):
    from starlette.websockets import WebSocketDisconnect
    c = world["as"]("alice@x")
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(f"/api/agent/ws/{DEFAULT_SID}",
                                 headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()
    assert e.value.code in (4404, 4403)


def test_me_is_the_person_in_the_selected_project(world):
    me = world["as"]("alice@x", "radio").get("/api/me").json()
    assert (me["project"], me["project_role"], me["role"]) == ("radio", "dev", "dev")
    me = world["as"]("alice@x", "default").get("/api/me").json()
    assert me["role"] == "none" and me["project_role"] is None   # the UI switches project
    me = world["as"]("bob@x").get("/api/me").json()
    assert (me["project"], me["role"], me["instance_role"]) == ("default", "dev", "dev")


def test_a_session_of_another_project_works_in_its_own_workspace(world):
    import agentchat
    assert agentchat.project_cwd(RADIO_SID) == str(world["tmp"] / "projects" / "radio" / "work")
    assert agentchat.project_cwd(DEFAULT_SID) == agentchat.CWD
    assert agentchat.project_cwd("z" * 32).endswith("/projects/_no-project/work")
