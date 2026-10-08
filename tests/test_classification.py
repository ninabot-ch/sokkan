"""SOKKAN 3.4 « classification » — levels, clearances, and every surface that hands out notes
or cards: nothing above a person's clearance reaches them, whoever asks on their behalf.

People of the project `radio` (all `dev` through the SSO group `radio-devs`):
  * alice@x — also in the SSO group `radio-secret`, mapped to `confidential`;
  * carol@x — no other group: clearance `project` (the default of every role).
Each surface has a test that goes red when its filter is removed (checked by mutation)."""
import os

import pytest

RADIO_SID = "r" * 32


# ---- levels and scopes (engine) -----------------------------------------------------------
def test_levels_parse_labels_and_fail_closed(monkeypatch):
    from core import levels as L
    assert L.parse(None) is None and L.parse("") is None
    assert L.parse("confidential") == 3 and L.parse("RESTRICTED") == 4 and L.parse(1) == 1
    assert L.parse("typo-level") == L.MAX           # unknown = restricted, never public
    monkeypatch.setenv("SOKKAN_CLASSIFICATION_LABELS", "Public,Interne,Projet,Confidentiel,Secret")
    assert L.parse("secret") == 4 and L.label(1) == "Interne" and L.ident(1) == "team"
    assert L.highest([1, 3, None, 2]) == 3 and L.highest([]) == L.DEFAULT


def test_scope_entries_carry_the_clearance_and_only_narrow():
    from core import scope as S
    assert S.normalize(["radio@3", "shared"]) == ("radio@3", "shared")
    assert S.normalize(["radio@4", "radio@1"]) == ("radio@1",)        # the lower one wins
    assert S.normalize(["radio@9", "../x", "radio"]) == ("radio",)      # invalid dropped
    sc = S.normalize(["radio@3", "shared"])
    assert S.allows(sc, "radio", 3) and not S.allows(sc, "radio", 4)
    assert S.allows(sc, "shared", 2) and not S.allows(sc, "shared", 3)  # bare = default
    assert not S.allows(sc, "default", 0) and S.allows(None, "x", 4)
    assert S.projects_of(sc) == ("radio", "shared")
    assert S.from_env("radio@3,shared") == sc


def test_filter_hits_drops_what_a_store_returned_above_the_clearance():
    from core import scope as S
    from core.search import Hit
    hits = [Hit("a", 1, 1, 0, None, "", 1, "indexed", project="radio", level=4),
            Hit("b", 1, 1, 0, None, "", 1, "indexed", project="radio", level=2),
            Hit("c", 1, 1, 0, None, "", 1, "indexed", project="radio")]   # no level = default
    assert [h.note_name for h in S.filter_hits(hits, ("radio@3",))] == ["b", "c"]
    assert [h.note_name for h in S.filter_hits(hits, ("radio@1",))] == []


def test_recaller_never_forces_a_quoted_note_above_the_clearance():
    """Channel 5 (quoted names) with a store that IGNORES the scope: the recaller's own
    checks still drop the classified note."""
    from core import recall as R
    from core.contract import NoteRecord
    from core.search import Hit

    class Gen:
        id, dim, embed_identity, chunk_count = 1, 2, "x", 1

    class Leaky:
        def active_generation(self):
            return Gen()

        def search(self, *a, **k):
            return [Hit("radio-keys", 0.9, 0.9, 0.5, None, "k", 1, "indexed",
                        project="radio", level=4)]

        def existing_names(self, names, projects=None):
            return {"radio-keys"}

        def resolve_note(self, name, projects=None):
            return NoteRecord("radio-keys", "d", "project", 0, "/x.md", None, "indexed",
                              "secret body", "radio", 4)

        def recalled_notes(self, *a, **k):
            return set()

        def log_recall(self, *a, **k):
            return 0

        def log_recall_turn(self, *a, **k):
            pass

    from test_recall_scope import Emb
    rc = R.Recaller(Leaky(), Emb(), R.RecallConfig(threshold=0.1, top_k=10), profile="leger")
    q = "what about radio-keys and the radio keys rotation"
    res = rc.recall(q, projects=("radio@2",))
    assert [h.note_name for h in res.hits] == []
    res = rc.recall(q, projects=("radio@4",))
    assert "radio-keys" in [h.note_name for h in res.hits]


# ---- the instance -------------------------------------------------------------------------
class _LevelStore:
    """Notes with levels; honours (project, clearance) scopes like the Postgres store."""

    def __init__(self):
        from core.contract import NoteRecord
        rows = [("radio", "radio-runbook", 2), ("radio", "radio-keys-rotation", 3),
                ("radio", "radio-incident-root-cause", 4), ("shared", "conventions", 2),
                ("default", "default-plan", 2)]
        self.rows = {(p, n): NoteRecord(n, f"{p} {n} rotation plan", "project", 0,
                                        f"/{p}/{n}.md", None, "indexed", f"body of {n}", p, lv)
                     for p, n, lv in rows}
        self.access, self.levels_set, self.sess = [], [], {}

    def _ok(self, r, projects):
        from core import scope as S
        return S.visible(r, S.normalize(projects) if projects is not None else None)

    def list_notes(self, projects=None):
        from core import levels as L
        return [{"name": n, "project": p, "description": r.description, "mtime": 0,
                 "chunks": 1, "level": L.ident(r.level)}
                for (p, n), r in self.rows.items() if self._ok(r, projects)]

    def resolve_note(self, name, projects=None):
        for (p, n), r in self.rows.items():
            if n == name and self._ok(r, projects):
                return r
        return None

    def find_note_by_path(self, stem, projects=None):
        return None

    def search(self, qv, text, k, rerank=None, projects=None, **kw):
        from core.search import Hit
        return [Hit(n, 0.9, 0.9, 0.1, None, r.body, 1, "indexed", description=r.description,
                    project=p, level=r.level)
                for (p, n), r in self.rows.items() if self._ok(r, projects)][:k]

    def note_level(self, name, project="default"):
        r = self.rows.get((project, name))
        return None if r is None else r.level

    def get_note(self, name, project="default"):
        return self.rows.get((project, name))

    def set_level(self, name, level, project="default", by="", reason=""):
        if (project, name) in self.rows:
            self.rows[(project, name)].level = level
        self.levels_set.append((project, name, level, by, reason))

    def log_access(self, via, notes, actor=None, session_id=None, query=None):
        for n in notes:
            nm = n.get("note_name") or n.get("name") if isinstance(n, dict) else n.name
            self.access.append((via, actor, nm))
        return len(notes)

    def session_level(self, sid):
        return self.sess.get(sid)

    def access_log(self, **kw):
        return [{"id": 1, "at": "2026-10-07T20:00:00+00:00", "via": "nina", "actor": "alice@x",
                 "session_id": None, "project": "radio", "note_name": "radio-keys-rotation",
                 "level": 3, "query": "keys"},
                {"id": 2, "at": "2026-10-07T20:01:00+00:00", "via": "spawn", "actor": None,
                 "session_id": RADIO_SID, "project": "radio", "note_name": "radio-runbook",
                 "level": 2, "query": "x"}]

    def recall_log(self, **kw):
        self.last_recall_scope = kw.get("projects")
        return []

    def recall_summary(self):
        return {}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    return build_world(tmp_path, monkeypatch)


def build_world(tmp_path, monkeypatch):
    """The `radio` instance of these tests (also used by test_teams.py)."""
    from fastapi.testclient import TestClient

    import agents
    import app as a
    import audit
    import auth
    import board
    import classification
    import iam
    import projects
    import quarantine
    import store_backend

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    iam.upsert_user("admin@x", "admin")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(classification, "enabled", lambda: True)
    projects.create("radio", "Radio player", created_by="admin@x")
    for who, groups in (("alice@x", ["radio-devs", "radio-secret"]),
                        ("carol@x", ["radio-devs"]), ("max@x", ["radio-devs", "radio-leads"])):
        projects.sync_sso_groups(who, groups)
    projects.grant("radio", "team", "sso:radio-devs", "dev")
    projects.grant("radio", "team", "sso:radio-leads", "maintainer")
    projects.grant("radio", "user", "boss@x", "admin")
    classification.set_group_level("radio-secret", "confidential", "radio", "admin@x")
    classification.set_group_level("radio-leads", "restricted", "radio", "admin@x")
    board.add_sdk_session(RADIO_SID, "backend", title="radio work", project="radio",
                          owner="carol@x")
    pub = board.add_card("radio bug", "player stalls", project="radio")
    conf = board.add_card("key rotation", "the HSM PIN is …", project="radio", level=3)
    quarantine.write("radio-finding", "plain", "body", {"agent": "y"}, project="radio")
    quarantine.write("radio-secret-finding", "conf", "body", {"agent": "y"}, project="radio",
                     level=3)
    st = _LevelStore()
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    monkeypatch.setattr(store_backend, "get_store", lambda: st)
    monkeypatch.setattr(store_backend, "embed_query", lambda q, timeout=30.0: [1.0])
    monkeypatch.setattr(store_backend, "_reranker", lambda deep=False: None)
    monkeypatch.setattr(store_backend, "age_header", lambda *x: "[h]")

    who = {"email": "alice@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    monkeypatch.setattr(a, "_bg", lambda coro: coro.close())
    monkeypatch.setattr(a, "_memory_preseed", lambda *x, **k: "")
    c = TestClient(a.app)

    def as_(email, project="radio"):
        who["email"] = email
        c.headers["x-sokkan-project"] = project
        return c

    return {"c": c, "as": as_, "st": st, "pub": pub, "conf": conf, "tmp": tmp_path}


def test_clearance_comes_from_sso_groups_and_project_roles(world, monkeypatch):
    import classification as C
    u = C.user_for
    assert C.clearance(u("alice@x"), "radio") == 3
    assert C.clearance(u("carol@x"), "radio") == 2
    assert C.clearance(u("max@x"), "radio") == 4
    assert C.clearance(u("stranger@x"), "radio") is None              # no role = no access
    assert C.scope_for(u("alice@x"), "radio") == ("radio@3", "shared")
    assert C.scope_for(u("carol@x"), "radio") == ("radio", "shared")
    C.set_role_level("viewer", "team")
    projects_grant_viewer = __import__("projects")
    projects_grant_viewer.grant("radio", "user", "vic@x", "viewer")
    assert C.clearance(u("vic@x"), "radio") == 1                       # role level
    monkeypatch.setattr(C, "enabled", lambda: False)
    assert C.clearance(u("max@x"), "radio") == 2                       # off: the default
    assert C.scope_for(u("max@x"), "radio") == ("radio", "shared")


def test_memory_routes_answer_within_the_clearance(world):
    alice, st = world["as"]("alice@x"), world["st"]
    names = {n["name"] for n in alice.get("/api/memory/notes").json()}
    assert names == {"radio-runbook", "radio-keys-rotation", "conventions"}
    carol = world["as"]("carol@x")
    assert {n["name"] for n in carol.get("/api/memory/notes").json()} == {
        "radio-runbook", "conventions"}
    hits = {h["note_name"] for h in carol.get("/api/memory/search", params={"q": "rotation"}).json()}
    assert hits == {"radio-runbook", "conventions"}
    assert carol.get("/api/memory/note/radio-keys-rotation").json()["body"] is None
    a = world["as"]("alice@x").get("/api/memory/note/radio-keys-rotation").json()
    assert "body of radio-keys-rotation" in a["body"] and a["level"] == "confidential"
    assert ("cockpit", "alice@x", "radio-keys-rotation") in st.access     # audited
    world["as"]("carol@x").get("/api/memory/recall-log")
    assert st.last_recall_scope == ["radio"]


def test_nina_answers_as_the_person_who_asks(world, monkeypatch):
    """Same question, two people: two different answers (the prompt carries only what the
    person may read; the reply inherits the highest level used)."""
    import assistant
    monkeypatch.setenv("SOKKAN_FEATURE_ASSISTANT", "1")
    monkeypatch.setattr(assistant, "_llm_config", lambda: {"url": "x"})
    monkeypatch.setattr(assistant, "_fallback_config", lambda: None)
    monkeypatch.setattr(assistant, "_dossier", lambda: "")
    monkeypatch.setattr(assistant, "_con", lambda: _MemCon())
    monkeypatch.setattr(assistant, "_persist", lambda *x: None)
    monkeypatch.setattr(assistant, "history", lambda *x, **k: [])
    seen = {}

    def ask(cfg, fb, system, msgs, user, state=None):
        seen[user] = system
        return ("I see: " + ", ".join(sorted(n for n in ("radio-keys-rotation", "radio-runbook")
                                             if n in system)), "primary")
    monkeypatch.setattr(assistant, "_ask_with_fallback", ask)
    q = "how do we do the key rotation plan?"
    ra = world["as"]("alice@x").post("/api/assistant/chat", json={"message": q}).json()
    rc = world["as"]("carol@x").post("/api/assistant/chat", json={"message": q}).json()
    assert ra["reply"] != rc["reply"]
    assert "radio-keys-rotation" in seen["alice@x"] and "radio-keys-rotation" not in seen["carol@x"]
    assert "radio-incident-root-cause" not in seen["alice@x"]
    assert ra["level"] == "confidential" and rc["level"] == "project"
    assert ("nina", "carol@x", "radio-runbook") in world["st"].access


class _MemCon:
    def execute(self, *a):
        class R:
            def fetchone(self):
                return (0,)
        return R()

    def close(self):
        pass


def test_board_cards_within_the_clearance_and_object_routes_404(world):
    conf = world["conf"]
    carol = world["as"]("carol@x")
    titles = [c["title"] for b in carol.get("/api/board").json()["cards"].values() for c in b]
    assert titles == ["radio bug"]
    assert carol.get(f"/api/board/card/{conf['id']}").status_code == 404
    alice = world["as"]("alice@x")
    titles = [c["title"] for b in alice.get("/api/board").json()["cards"].values() for c in b]
    assert set(titles) == {"radio bug", "key rotation"}
    assert alice.get(f"/api/board/card/{conf['id']}").status_code == 200
    made = alice.post("/api/board/card", json={"title": "t", "classification": "team"}).json()
    assert made["level"] == 1


def test_lowering_a_level_takes_a_cleared_maintainer_with_a_reason(world):
    import audit
    conf = world["conf"]
    alice = world["as"]("alice@x")                     # dev, cleared confidential
    r = alice.post(f"/api/board/card/{conf['id']}/level", json={"level": "project"})
    assert r.status_code == 403
    assert alice.post(f"/api/board/card/{conf['id']}/level",
                      json={"level": "restricted"}).status_code == 200   # raising: ok
    mx = world["as"]("max@x")                          # maintainer, cleared restricted
    assert mx.post(f"/api/board/card/{conf['id']}/level",
                   json={"level": "project"}).status_code == 400          # no reason
    r = mx.post(f"/api/board/card/{conf['id']}/level",
                json={"level": "project", "reason": "published in the release notes"})
    assert r.status_code == 200 and r.json()["level"] == 2
    acts = [e["action"] for e in audit.recent(50)]
    assert "classification.card.lower" in acts
    # notes: same rule
    st = world["st"]
    carol = world["as"]("carol@x")
    assert carol.post("/api/memory/note/radio-keys-rotation/level",
                      json={"level": "public", "reason": "x"}).status_code == 404
    mx = world["as"]("max@x")
    r = mx.post("/api/memory/note/radio-keys-rotation/level",
                json={"level": "project", "reason": "rotated, obsolete"})
    assert r.status_code == 200, r.text
    assert st.levels_set[-1][:3] == ("radio", "radio-keys-rotation", 2)


def test_sessions_and_runs_above_the_clearance_do_not_exist(world, monkeypatch):
    import classification
    monkeypatch.setattr(classification, "session_level",
                        lambda sid: 3 if sid == RADIO_SID else None)
    carol = world["as"]("carol@x")
    assert RADIO_SID not in [s["session_id"] for s in carol.get("/api/sessions").json()]
    assert carol.get(f"/api/sessions/{RADIO_SID}").status_code == 404
    alice = world["as"]("alice@x")
    assert RADIO_SID in [s["session_id"] for s in alice.get("/api/sessions").json()]
    run = {"id": 1, "session_id": RADIO_SID, "deliverable": "the PIN", "outputs": {"x": 1},
           "error": ""}
    red = classification.redact_run(run, 2)
    assert red["deliverable"] == "" and red["classified"] == "confidential"
    assert classification.redact_run(run, 3)["deliverable"] == "the PIN"


def test_quarantine_within_the_clearance(world):
    carol = world["as"]("carol@x")
    assert [q["name"] for q in carol.get("/api/memory/quarantine").json()] == ["radio-finding"]
    assert carol.get("/api/memory/quarantine/radio-secret-finding").status_code == 404
    assert carol.post("/api/memory/quarantine/radio-secret-finding/approve").status_code == 404
    assert carol.post("/api/memory/quarantine/radio-secret-finding/reject").status_code == 404
    alice = world["as"]("alice@x")
    assert alice.post("/api/memory/quarantine/radio-secret-finding/approve").status_code == 200
    assert world["st"].levels_set[-1][:3] == ("radio", "radio-secret-finding", 3)
    text = (world["tmp"] / "projects" / "radio" / "memory" / "radio-secret-finding.md").read_text()
    assert "classification: confidential" in text


def test_access_log_for_project_admins_with_csv(world):
    carol = world["as"]("carol@x")
    assert carol.get("/api/classification/audit").status_code == 403
    world["as"]("boss@x")
    import projects
    projects.grant("radio", "user", "boss@x", "admin")
    boss = world["as"]("boss@x")
    rows = boss.get("/api/classification/audit").json()["entries"]
    assert {r["note_name"] for r in rows} == {"radio-runbook"}     # boss cleared `project`
    assert rows[0]["actor"] == "carol@x" and rows[0]["actor_source"] == "session owner"
    import classification
    classification.set_group_level("user:boss@x", "restricted", "radio")
    csv = boss.get("/api/classification/audit", params={"format": "csv"}).text
    assert csv.splitlines()[0] == "at,actor,via,project,note_name,level,session_id,query"
    assert "radio-keys-rotation" in csv and "alice@x,nina" in csv


def test_admin_screen_maps_groups_to_levels(world):
    adm = world["as"]("admin@x", "default")
    r = adm.put("/api/admin/classification/groups",
                json={"team": "radio-ops", "level": "restricted", "project": "*"})
    assert r.status_code == 200
    got = adm.get("/api/admin/classification").json()
    assert {"team_id": "sso:radio-ops", "project": "*"}.items() <= next(
        g for g in got["groups"] if g["team_id"] == "sso:radio-ops").items()
    assert adm.put("/api/admin/classification/groups",
                   json={"team": "x", "level": "ultra"}).status_code == 400
    assert world["as"]("alice@x").get("/api/admin/classification").status_code == 403


def test_corthexis_tab_hides_notes_above_the_clearance(world):
    rdir = world["tmp"] / "projects" / "radio" / "memory"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "radio_runbook.md").write_text("---\nname: radio-runbook\ndescription: r\n---\n"
                                           "see [[radio-keys-rotation]]\n")
    (rdir / "radio_keys_rotation.md").write_text(
        "---\nname: radio-keys-rotation\ndescription: k\nclassification: confidential\n---\nPIN\n")
    carol = world["as"]("carol@x")
    g = carol.get("/api/corthexis/graph").json()
    assert {n["id"] for n in g["nodes"]} == {"radio-runbook"}
    assert all("radio-keys-rotation" not in (e["s"], e["t"]) for e in g["edges"])
    assert carol.get("/api/corthexis/note/radio-keys-rotation").status_code == 404
    alice = world["as"]("alice@x")
    assert {n["id"] for n in alice.get("/api/corthexis/graph").json()["nodes"]} == {
        "radio-runbook", "radio-keys-rotation"}


# ---- MCP servers --------------------------------------------------------------------------
def test_memory_mcp_scope_and_write_inherit_the_session_level(world, monkeypatch, tmp_path):
    import memory_search_server as M
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "radio@3,shared,default@4")
    monkeypatch.setenv("SOKKAN_SESSION_ID", RADIO_SID)
    monkeypatch.setenv("SOKKAN_SESSION_USER", "alice@x")
    assert M._scope() == ("radio@3", "shared")           # default@4 is not its to read
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "")
    assert M._scope() == ("radio",)                       # lost clearance = default only
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "radio@3,shared")
    assert "body of radio-keys-rotation" in M.memory_get("radio-keys-rotation")
    assert "not found" in M.memory_get("radio-incident-root-cause")
    assert ("mcp", "alice@x", "radio-keys-rotation") in world["st"].access
    world["st"].sess[RADIO_SID] = 3
    out = M.memory_write("radio-summary", "summary of the rotation", "derived text",
                         classification="public")
    assert out["ok"] and out["classification"] == "confidential"
    text = (tmp_path / "projects" / "radio" / "memory" / "radio-summary.md").read_text()
    assert "classification: confidential" in text
    assert world["st"].levels_set[-1][:3] == ("radio", "radio-summary", 3)
    world["st"].rows[("radio", "radio-incident-root-cause")].source_path = "x"
    (tmp_path / "projects" / "radio" / "memory" / "radio-incident-root-cause.md").write_text("x")
    r = M.memory_write("radio-incident-root-cause", "d", "b", overwrite=True)
    assert not r["ok"] and "not available" in r["error"]


def test_board_mcp_filters_by_the_session_clearance(world, monkeypatch):
    import board_mcp
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "radio,shared")
    monkeypatch.delenv("SOKKAN_SESSION_ID", raising=False)
    titles = [c["title"] for b in board_mcp.list_board().values() for c in b]
    assert titles == ["radio bug"]
    assert board_mcp.search_cards("key")["count"] == 0
    assert "error" in board_mcp.get_card(world["conf"]["id"])
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "radio@3,shared")
    assert board_mcp.search_cards("key")["count"] == 1
    assert board_mcp.get_card(world["conf"]["id"])["title"] == "key rotation"


def test_session_scope_uses_the_owner_clearance(world, monkeypatch):
    import agentchat
    import classification as C
    assert C.session_scope(RADIO_SID) == ("radio", "shared")           # carol's session
    assert agentchat.session_scope_env("radio", "alice@x") == "radio@3,shared"
    assert C.session_scope("unknown" * 4) == ()                         # several projects


def test_frontmatter_level_rewrite():
    import classification as C
    t = "---\nname: a\ndescription: d\nmetadata:\n  type: project\n---\nbody\n"
    out = C.set_frontmatter_level(t, 3)
    assert "description: d\nclassification: confidential\nmetadata:" in out
    assert C.set_frontmatter_level(out, 1).count("classification:") == 1
    from core.notes import parse_note
    assert parse_note(C.set_frontmatter_level(out, 1), "a.md").level == 1


def test_feature_registry_dependencies(monkeypatch):
    import features
    for k in list(os.environ):
        if k.startswith("SOKKAN_FEATURE_") or k in ("SOKKAN_AUTH_MODE", "SOKKAN_EDITION"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SOKKAN_FEATURE_CLASSIFICATION", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    assert not features.resolve()["classification"].enabled          # sso_teams needs sso
    monkeypatch.setenv("SOKKAN_AUTH_MODE", "oidc")
    assert features.resolve()["classification"].enabled
    monkeypatch.setenv("SOKKAN_FEATURE_TEAMS", "1")
    assert not features.resolve()["teams"].enabled                   # needs assistant
    monkeypatch.setenv("SOKKAN_FEATURE_ASSISTANT", "1")
    assert features.resolve()["teams"].enabled
    monkeypatch.delenv("SOKKAN_FEATURE_CLASSIFICATION")
    monkeypatch.delenv("SOKKAN_FEATURE_MULTI_PROJECT")
    monkeypatch.setenv("SOKKAN_EDITION", "enterprise")
    assert features.resolve()["classification"].enabled
