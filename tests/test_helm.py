"""SOKKAN 3.3 Helm — hierarchical cards, progress that flows up, context that flows down,
reframe suggestions, the Helm view's rights, the morning brief.

Everything goes through the real modules (board, helm, projects, agents) and, for the
HTTP side, the real request path (middleware → projectgate → route); only the identity
(`auth.resolve_email`), the embedding engine (`helm._embed`) and the clock are chosen.
People:
  * mia@x   — maintainer of `radio` (the manager);
  * dan@x   — dev of `radio` (an engineer);
  * vic@x   — viewer of `radio`;
  * root@x  — instance admin, no grant in `radio` (sees nothing of it), admin of `default`.
"""
import json
import math
import re
import time
from datetime import datetime, timedelta

import pytest

DAY = 86400


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agents
    import audit
    import board
    import helm
    import iam
    import observability
    import projects
    import usage

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    monkeypatch.setenv("SOKKAN_FEATURE_HELM", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_ASSISTANT", "1")
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    iam.upsert_user("root@x", "admin")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    helm.init(force=True)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(observability, "DB", str(tmp_path / "incidents.db"))
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "PROJECT_DIR", tmp_path / "transcripts")   # never the host's
    monkeypatch.setattr(helm, "_EMB_CACHE", {})
    monkeypatch.setattr(helm, "_embed", _bow_embed)
    # 3.4.2: the context note of a project card needs the 3.0 store (sqlite mode refuses it
    # honestly — tests/test_sqlite_honest.py); here the store serves
    import store_backend
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    projects.create("radio", "Radio player", created_by="root@x")
    projects.grant("radio", "user", "mia@x", "maintainer")
    projects.grant("radio", "user", "dan@x", "dev")
    projects.grant("radio", "user", "vic@x", "viewer")
    return {"tmp": tmp_path, "board": board, "helm": helm}


_VOCAB: dict[str, int] = {}


def _bow_embed(texts):
    """A deterministic stand-in for the memory engine: bag of words, unit vectors."""
    out = []
    for t in texts:
        v = [0.0] * 256
        for w in re.findall(r"[a-z]{3,}", t.lower()):
            v[_VOCAB.setdefault(w, len(_VOCAB) % 256)] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def tree(board, project="radio"):
    p = board.add_card("Live radio player v2", "rebuild the player", project=project, kind="project",
                       intent="Ship a live radio player that streams HLS audio on web and mobile",
                       constraints="WCAG AA; no new backend service",
                       decisions=["No Kafka: the stream metadata stays on the existing API"],
                       user="mia@x")
    a = board.add_card("HLS audio player component", "web player streaming HLS audio live",
                       project=project, parent_id=p["id"], assignee="dan@x", user="dan@x")
    b = board.add_card("Mobile radio player streaming", "mobile app live radio audio player",
                       project=project, parent_id=p["id"], assignee="dan@x", user="dan@x")
    s = board.add_card("Buffering of the live audio stream", "HLS buffering for the player",
                       project=project, parent_id=a["id"], assignee="dan@x", user="dan@x")
    return p, a, b, s


# ---- 1. hierarchy ---------------------------------------------------------------------

def test_hierarchy_breadcrumb_children_and_guards(env):
    board = env["board"]
    p, a, b, s = tree(board)
    assert [x["id"] for x in board.ancestors(s["id"])] == [p["id"], a["id"]]
    assert {x["id"] for x in board.descendants(p["id"])} == {a["id"], b["id"], s["id"]}
    d = board.card_detail(s["id"])
    assert [x["id"] for x in d["breadcrumb"]] == [p["id"], a["id"]]
    assert [x["id"] for x in board.card_detail(a["id"])["children"]] == [s["id"]]
    with pytest.raises(ValueError, match="own parent"):
        board.set_parent(a["id"], a["id"])
    with pytest.raises(ValueError, match="cycle"):
        board.set_parent(p["id"], s["id"])
    other = board.add_card("default card", project="default")
    with pytest.raises(ValueError, match="not found"):
        board.set_parent(other["id"], p["id"])          # another project: does not exist
    with pytest.raises(ValueError, match="not found"):
        board.add_card("x", project="default", parent_id=p["id"])
    # depth is bounded
    cur = s
    for i in range(board.MAX_DEPTH - 3):
        cur = board.add_card(f"deep {i}", project="radio", parent_id=cur["id"])
    with pytest.raises(ValueError, match="too deep"):
        board.add_card("too deep", project="radio", parent_id=cur["id"])
    # deleting a middle card moves its children up, never into the void
    board.delete_card(a["id"])
    assert board.get_card(s["id"])["parent_id"] == p["id"]


def test_migration_of_an_existing_board_is_safe(env, tmp_path, monkeypatch):
    import sqlite3

    import board
    old = tmp_path / "old-board.db"
    con = sqlite3.connect(old)
    con.executescript("""
        CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          description TEXT DEFAULT '', tag TEXT DEFAULT 'backend', bucket TEXT NOT NULL DEFAULT 'Backlog',
          session_id TEXT, window TEXT, created_at REAL, sort REAL DEFAULT 0);
        INSERT INTO cards(title, bucket, created_at) VALUES ('legacy', 'Doing', 1);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(board, "DB", old)
    board.init(force=True)
    c = board.get_card(1)
    assert c["title"] == "legacy" and c["parent_id"] is None and c["kind"] == "task"
    assert c["decisions"] == [] and c["project"] == "default"
    board.init(force=True)                                   # idempotent
    assert board.get_card(1)["bucket"] == "Doing"


# ---- 2. progress flows up -------------------------------------------------------------

def test_rollup_is_computed_from_the_children_never_declared(env):
    board, helm = env["board"], env["helm"]
    p, a, b, s = tree(board)
    r = helm.rollup(p["id"])
    assert r["state"] == "todo" and r["total"] == 2 and r["progress"] == 0   # leaves: b, s
    board.update_card(s["id"], bucket="Doing", user="dan@x")
    assert helm.rollup(p["id"])["state"] == "in_progress"
    assert helm.rollup(a["id"])["state"] == "in_progress"
    board.update_card(b["id"], bucket="Review", user="dan@x")
    assert helm.rollup(p["id"])["state"] == "waiting"
    board.close_card(s["id"], user="dan@x")
    board.close_card(b["id"], user="dan@x")
    r = helm.rollup(p["id"])
    assert r["state"] == "done" and r["progress"] == 1.0
    # declaring the parent by hand changes nothing: it is computed
    board.reopen_card(b["id"], user="dan@x")
    board.update_card(p["id"], bucket="Done", user="mia@x")
    assert helm.rollup(p["id"])["state"] == "in_progress"
    # every change was journaled on the parent by `helm` (recomputed at each event)
    prog = [e for e in board.card_events(p["id"]) if e["action"] == "progress"]
    assert prog and all(e["user"] == "helm" for e in prog)
    assert any("Done" in e["detail"] for e in prog)


def test_signals_sessions_runs_mrs_incidents_feed_the_rollup(env, monkeypatch):
    import agents
    import observability
    board, helm = env["board"], env["helm"]
    p, a, b, s = tree(board)
    board.add_sdk_session("w" * 32, "backend", title="dan's session", project="radio")
    board.link_card(b["id"], "session", "w" * 32, user="dan@x")
    monkeypatch.setattr(helm, "_session_working", lambda sid: sid == "w" * 32)
    r = helm.rollup(p["id"])
    assert r["state"] == "in_progress" and r["working"] == 1 and r["signals"]["sessions_working"] == 1
    board.link_card(s["id"], "mr", "https://git.example.com/radio/player/-/merge_requests/7", user="dan@x")
    with pytest.raises(ValueError):
        board.link_card(s["id"], "mr", "javascript:alert(1)", user="dan@x")
    assert helm.rollup(p["id"])["signals"]["mrs"][0]["url"].endswith("/7")
    ag = agents.create({"email": "dan@x", "role": "dev", "project": "radio"},
                       {"name": "player-smoke", "purpose": "p", "deliverable": "d"}, activate=True)
    run = agents.enqueue_run(ag["id"], "manual", requested_by="dan@x")
    agents.update_run(run["id"], status="failed")
    board.link_card(a["id"], "run", run["id"], user="dan@x")
    assert helm.rollup(p["id"])["state"] == "blocked"
    iid = observability.record_incident("HLS origin down", "5xx", "critical")
    board.link_card(b["id"], "incident", iid, user="dan@x")
    r = helm.rollup(p["id"])
    assert r["state"] == "blocked" and r["signals"]["incidents"][0]["id"] == iid
    observability.set_incident_status(iid, "resolved")
    assert not helm.rollup(p["id"])["signals"]["incidents"]


# ---- 1b. context flows down -----------------------------------------------------------

def _client(monkeypatch, who):
    from fastapi.testclient import TestClient

    import app as a
    import auth
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    import board
    seeds = []
    orig = board.seed_text

    def capture(prompt, recall=""):
        seeds.append(orig(prompt, recall))
        return seeds[-1]
    monkeypatch.setattr(board, "seed_text", capture)
    monkeypatch.setattr(a, "_bg", lambda coro: coro.close())
    monkeypatch.setattr(a, "_memory_preseed", lambda *x, **k: "=== Project memory (auto-recalled) ===\n- [x] y")
    c = TestClient(a.app)
    c.headers["x-sokkan-project"] = "radio"
    return c, seeds


def test_context_of_the_parents_is_injected_at_spawn_and_stored_as_a_note(env, monkeypatch):
    board = env["board"]
    p, a, b, s = tree(board)
    board.link_card(p["id"], "mr", "https://git.example.com/radio/design/-/merge_requests/1", user="mia@x")
    who = {"email": "dan@x"}
    c, seeds = _client(monkeypatch, who)
    r = c.post(f"/api/board/card/{s['id']}/spawn")
    assert r.status_code == 200, r.text
    seed = seeds[-1]
    assert "Context from the parent cards (Helm)" in seed
    assert "Ship a live radio player that streams HLS audio" in seed          # intent
    assert "WCAG AA; no new backend service" in seed                          # constraints
    assert "No Kafka: the stream metadata stays on the existing API" in seed  # decisions
    assert "merge_requests/1" in seed                                         # links
    assert f"card:{p['id']}" in seed
    assert seed.index("Context from the parent") < seed.index("Project memory")
    note = env["tmp"] / "projects" / "radio" / "memory" / f"helm-card-{p['id']}.md"
    text = note.read_text()
    assert f'card: "card:{p["id"]}"' in text and "WCAG AA" in text and "No Kafka" in text
    # a top-level card without parents: no Helm block
    top = board.add_card("lonely", "just a task", project="radio")
    c.post(f"/api/board/card/{top['id']}/spawn")
    assert "Context from the parent" not in seeds[-1]


def test_editing_the_context_rewrites_the_note_and_journals_decisions(env, monkeypatch):
    board = env["board"]
    p, a, b, s = tree(board)
    c, _ = _client(monkeypatch, {"email": "mia@x"})
    r = c.patch(f"/api/board/card/{p['id']}", json={"decisions": [
        "No Kafka: the stream metadata stays on the existing API", "Use HLS rather than DASH"]})
    assert r.status_code == 200, r.text
    note = env["tmp"] / "projects" / "radio" / "memory" / f"helm-card-{p['id']}.md"
    assert "Use HLS rather than DASH" in note.read_text()
    ev = [e for e in board.card_events(p["id"]) if e["action"] == "decision recorded"]
    assert ev[0]["detail"] == "Use HLS rather than DASH"
    # move a card under another through the API (0 = top level), cycles refused
    assert c.patch(f"/api/board/card/{b['id']}", json={"parent_id": a["id"]}).status_code == 200
    assert board.get_card(b["id"])["parent_id"] == a["id"]
    r = c.patch(f"/api/board/card/{p['id']}", json={"parent_id": s["id"]})
    assert r.status_code == 400 and "cycle" in r.json()["detail"]
    assert c.patch(f"/api/board/card/{b['id']}", json={"parent_id": 0}).status_code == 200
    assert board.get_card(b["id"])["parent_id"] is None


def test_feature_off_hides_helm_and_refuses_hierarchy(env, monkeypatch):
    board = env["board"]
    p, *_ = tree(board)
    monkeypatch.setenv("SOKKAN_FEATURE_HELM", "0")
    c, seeds = _client(monkeypatch, {"email": "mia@x"})
    assert c.get("/api/helm/deck").status_code == 404
    r = c.patch(f"/api/board/card/{p['id']}", json={"intent": "x"})
    assert r.status_code == 400 and "Helm" in r.json()["detail"]
    assert c.get("/api/features").json()["helm"] is False
    # dependency: helm needs assistant (and multi_project)
    monkeypatch.setenv("SOKKAN_FEATURE_HELM", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_ASSISTANT", "0")
    import features
    assert not features.enabled("helm")


# ---- 3b. suggestions on constructed cases ---------------------------------------------

def _kinds(helm, project="radio", now=None):
    return {s["kind"]: s for s in helm.detect(project, now)[0]}


def test_drift_contradiction_unassigned_and_incident(env):
    import observability
    board, helm = env["board"], env["helm"]
    p, a, b, s = tree(board)
    k = _kinds(helm)
    assert "drift" not in k and "contradiction" not in k and "unassigned" not in k
    off = board.add_card("Quarterly marketing newsletter", "write the newsletter for investors",
                         project="radio", parent_id=p["id"], assignee="dan@x")
    k = _kinds(helm)
    assert k["drift"]["target_id"] == off["id"] and k["drift"]["evidence"]["similarity"] < helm.drift_min()
    kaf = board.add_card("Metadata bus", "push now-playing metadata through Kafka topics",
                         project="radio", parent_id=p["id"], assignee="dan@x")
    k = _kinds(helm)
    assert k["contradiction"]["target_id"] == kaf["id"] and k["contradiction"]["evidence"]["term"] == "kafka"
    board.add_card("Nobody's card", "audio player for radio live HLS stream", project="radio",
                   parent_id=p["id"])
    assert "unassigned" in _kinds(helm)
    iid = observability.record_incident("CDN 5xx on HLS", "x", "critical")
    board.link_card(b["id"], "incident", iid, user="dan@x")
    k = _kinds(helm)
    assert k["incident"]["evidence"]["incident"] == iid and k["incident"]["target_id"] == b["id"]


def test_forbidden_terms_heuristics():
    import helm
    assert helm.forbidden_terms("No Kafka: metadata stays on the API") == ["kafka"]
    assert helm.forbidden_terms("Pas de microservices pour la v1") == ["microservices"]
    assert helm.forbidden_terms("Use REST instead of GraphQL") == ["graphql"]
    assert helm.forbidden_terms("Mobile offline mode is out of scope") == ["mobile offline mode"]
    assert helm.forbidden_terms("We ship on the 30th") == []


def test_scope_creep_unparented_and_velocity(env):
    board, helm = env["board"], env["helm"]
    p, a, b, s = tree(board)
    con = board._con()
    con.execute("UPDATE cards SET baseline_at=? WHERE id=?", (time.time() - 3600, p["id"]))
    con.execute("UPDATE cards SET created_at=? WHERE id IN (?,?)", (time.time() - 7200, a["id"], b["id"]))
    con.commit()
    con.close()
    for i in range(3):
        board.add_card(f"extra player feature {i}", "radio live audio player HLS", project="radio",
                       parent_id=p["id"], assignee="dan@x")
    k = _kinds(helm)
    assert k["scope"]["evidence"]["added"] and k["scope"]["evidence"]["before"] == 2
    for i in range(3):
        board.add_card(f"loose {i}", "a task outside the project", project="radio")
    assert len(_kinds(helm)["unparented"]["evidence"]["cards"]) == 3
    # velocity: a project done 3 cards/week before, nothing this week → slowing
    now = time.time()
    done = [board.add_card(f"old {i}", "radio player audio", project="radio", parent_id=p["id"],
                           assignee="dan@x") for i in range(6)]
    con = board._con()
    con.execute("UPDATE cards SET created_at=? WHERE id=?", (now - 40 * DAY, p["id"]))
    for i, d in enumerate(done):
        con.execute("UPDATE cards SET bucket='Done', closed_at=?, created_at=? WHERE id=?",
                    (now - (10 + i) * DAY, now - 30 * DAY, d["id"]))
    con.commit()
    con.close()
    k = _kinds(helm, now=now)
    assert k["slowing"]["evidence"]["done_7d"] == 0 and k["slowing"]["evidence"]["weekly_before"] >= 1
    # racing: lots of new cards in a week, few done
    for i in range(6):
        board.add_card(f"new {i}", "radio player audio stream", project="radio", parent_id=p["id"],
                       assignee="dan@x")
    assert "racing" in _kinds(helm, now=now)


def test_suggestion_lifecycle_approve_ignore_snooze_resolve(env):
    board, helm = env["board"], env["helm"]
    p, a, b, s = tree(board)
    kaf = board.add_card("Metadata bus", "push metadata through Kafka for the radio player",
                         project="radio", parent_id=p["id"], assignee="dan@x")
    lost = board.add_card("Radio player audio QA", "test the live radio audio player", project="radio",
                          parent_id=p["id"])
    res = helm.suggest("radio")
    kinds = {x["kind"]: x for x in res["new"]}
    assert {"contradiction", "unassigned"} <= set(kinds)
    # nothing applied by Helm itself: the cards are untouched
    assert board.get_card(kaf["id"])["bucket"] == "Backlog" and not board.get_card(lost["id"])["assignee"]
    again = helm.suggest("radio")
    assert again["new"] == [] and again["kept"] >= 2                       # no duplicate
    out = helm.approve(kinds["contradiction"]["id"], "mia@x")
    rc = out["reframe_card"]
    assert rc["kind"] == "reframe" and rc["parent_id"] == p["id"] and rc["assignee"] == "mia@x"
    assert any("Helm reframe approved by mia@x" in c["body"] for c in board.card_comments(kaf["id"]))
    with pytest.raises(ValueError):
        helm.approve(kinds["contradiction"]["id"], "mia@x")
    helm.ignore(kinds["unassigned"]["id"], "mia@x")
    assert all(x["kind"] != "unassigned" for x in helm.suggest("radio")["new"])   # snoozed
    # the condition disappears → an open suggestion is resolved (not applied)
    off = board.add_card("Investor newsletter", "quarterly marketing letter", project="radio",
                         parent_id=p["id"], assignee="dan@x")
    assert any(x["kind"] == "drift" for x in helm.suggest("radio")["new"])
    board.close_card(off["id"], user="dan@x")
    assert helm.suggest("radio")["resolved"] >= 1
    assert not [x for x in helm.list_suggestions(["radio"]) if x["target_id"] == off["id"]]


def test_drift_detector_degrades_without_embeddings(env, monkeypatch):
    board, helm = env["board"], env["helm"]
    tree(board)

    def boom(texts):
        raise RuntimeError("no model")
    monkeypatch.setattr(helm, "_embed", boom)
    sug, notes = helm.detect("radio")
    assert any(n.startswith("drift: embeddings unavailable") for n in notes)


# ---- 4. the Helm view: who sees it ----------------------------------------------------

def test_helm_view_rights(env, monkeypatch):
    board = env["board"]
    p, a, b, s = tree(board)
    board.add_card("default steering", "x", project="default", kind="project")
    who = {"email": "mia@x"}
    c, _ = _client(monkeypatch, who)
    d = c.get("/api/helm/deck").json()
    assert [i["card"]["id"] for i in d["items"]] == [p["id"]] and d["projects"] == ["radio"]
    assert d["items"][0]["rollup"]["state"] == "todo"
    assert c.get(f"/api/helm/cards/{p['id']}").json()["kanban"]["cards"]["Backlog"][0]["id"] in (a["id"], b["id"])
    assert c.get("/api/helm/access").json()["steers"] == ["radio"]
    # a dev / a viewer of the project: no Helm (404, never a hint)
    for e in ("dan@x", "vic@x"):
        who["email"] = e
        assert c.get("/api/helm/deck").json()["items"] == []
        assert c.get(f"/api/helm/cards/{p['id']}").status_code == 404
        assert c.get("/api/helm/deck?project=radio").status_code == 404
    # instance admin: Helm of the projects they belong to only (decision of 07.10)
    who["email"] = "root@x"
    d = c.get("/api/helm/deck").json()
    assert d["projects"] == ["default"] and all(i["card"]["project"] == "default" for i in d["items"])
    assert c.get(f"/api/helm/cards/{p['id']}").status_code == 404
    import projects
    projects.grant("radio", "user", "root@x", "viewer")
    assert c.get(f"/api/helm/cards/{p['id']}").status_code == 200
    # filters: person
    who["email"] = "mia@x"
    assert c.get("/api/helm/deck?person=dan@x").json()["items"]
    assert c.get("/api/helm/deck?person=nobody@x").json()["items"] == []


def test_suggestion_decisions_need_a_manager(env, monkeypatch):
    board, helm = env["board"], env["helm"]
    p, *_ = tree(board)
    board.add_card("Radio player audio QA", "test the live radio audio player", project="radio",
                   parent_id=p["id"])
    sid = next(x["id"] for x in helm.suggest("radio")["new"] if x["kind"] == "unassigned")
    who = {"email": "dan@x"}
    c, _ = _client(monkeypatch, who)
    assert c.post(f"/api/helm/suggestions/{sid}/approve").status_code == 404
    who["email"] = "mia@x"
    r = c.post(f"/api/helm/suggestions/{sid}/approve")
    assert r.status_code == 200 and r.json()["reframe_card"]["kind"] == "reframe"
    assert c.post(f"/api/helm/suggestions/{sid}/ignore").status_code == 409
    r = c.post(f"/api/helm/cards/{p['id']}/suggestions/refresh")
    assert r.status_code == 200 and "suggestions" in r.json()
    assert c.get(f"/api/helm/cards/{p['id']}/activity").json()
    assert c.get(f"/api/helm/cards/{p['id']}/costs").json()["total_usd"] == 0


# ---- 3a. creating a project from Nina's (edited) proposal -----------------------------

def test_create_project_from_a_validated_proposal(env, monkeypatch):
    who = {"email": "dan@x"}
    c, _ = _client(monkeypatch, who)
    body = {"title": "Podcast chapters", "intent": "Listeners jump to a chapter",
            "scope": "web player only", "constraints": "no new service", "deadline": "2026-11-30",
            "team": ["dan@x"], "decisions": ["Chapters come from the CMS"],
            "children": [{"title": "Chapter API read", "assignee": "dan@x"},
                         {"title": "Chapter list UI", "assignee": "ghost@x"}]}
    r = c.post("/api/helm/projects", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    pc = out["card"]
    assert pc["kind"] == "project" and pc["intent"] == "Listeners jump to a chapter" and pc["due"] == "2026-11-30"
    assert pc["baseline_at"] >= max(ch["created_at"] for ch in out["children"])
    assert [ch["parent_id"] for ch in out["children"]] == [pc["id"], pc["id"]]
    assert out["children"][1]["assignee"] == ""                 # unknown person: not assigned
    assert out["note"]["card"] == f"card:{pc['id']}"
    who["email"] = "vic@x"
    assert c.post("/api/helm/projects", json=body).status_code == 403
    who["email"] = "root@x"
    assert c.post("/api/helm/projects", json=body).status_code == 404


# ---- 5. morning brief -----------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=Europe/Zurich:20261001T091500
DTEND;TZID=Europe/Zurich:20261001T093000
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
SUMMARY:Stand-up radio
END:VEVENT
BEGIN:VEVENT
DTSTART:20261008T120000Z
DTEND:20261008T130000Z
SUMMARY:Lunch with \\, the CTO
LOCATION:Geneva
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261009
SUMMARY:Tomorrow, not today
END:VEVENT
END:VCALENDAR
"""


def test_ics_parser_recurrence_and_window():
    from calendars import ics as hc
    s = datetime(2026, 10, 8, tzinfo=hc.TZ)
    ev = hc.parse_ics(ICS, s, s + timedelta(days=1))
    assert [e.title for e in ev] == ["Stand-up radio", "Lunch with , the CTO"]
    assert ev[0].as_dict()["start_local"] == "09:15" and ev[1].as_dict()["start_local"] == "14:00"
    sat = datetime(2026, 10, 10, tzinfo=hc.TZ)
    assert hc.parse_ics(ICS, sat, sat + timedelta(days=1)) == []      # weekend: no stand-up
    with pytest.raises(ValueError):
        hc._check_url("http://example.com/cal.ics")
    with pytest.raises(ValueError):
        hc._check_url("https://127.0.0.1/cal.ics")
    # Microsoft 365 is not an ICS kind: it is the `calendars` provider of the teams feature
    with pytest.raises(ValueError):
        hc.set_source("dan@x", "graph", "X")


def test_morning_brief_content(env, monkeypatch):
    import agents
    import observability
    board, helm = env["board"], env["helm"]
    p, a, b, s = tree(board)
    board.update_card(a["id"], bucket="Doing", user="dan@x")
    board.update_card(b["id"], bucket="Review", user="dan@x")
    iid = observability.record_incident("CDN 5xx", "x", "critical")
    board.link_card(s["id"], "incident", iid, user="dan@x")
    board.update_card(p["id"], user="mia@x", decisions=["No Kafka: the stream metadata stays on the existing API",
                                                       "Ship behind a flag"])
    ag = agents.create({"email": "dan@x", "role": "dev", "project": "radio"},
                       {"name": "player-smoke", "purpose": "p", "deliverable": "d"}, activate=True)
    run = agents.enqueue_run(ag["id"], "manual", requested_by="dan@x")
    agents.update_run(run["id"], status="failed", ended_at=time.time())
    from calendars import ics as hc
    sday = datetime(2026, 10, 8, tzinfo=hc.TZ)
    cal = [e.as_dict() for e in hc.parse_ics(ICS, sday, sday + timedelta(days=1))]
    out = helm.morning_brief("radio", person="dan@x", calendar_events=cal)
    assert {x["card_id"] for x in out["moved"]} >= {a["id"], b["id"]}
    assert out["blocked"][0]["card_id"] == s["id"] and f"incident #{iid}" in out["blocked"][0]["why"]
    assert out["waiting"][0]["card_id"] == b["id"]
    assert out["incidents"][0]["id"] == iid
    assert out["agents_in_error"][0]["name"] == "player-smoke"
    # decisions of the cards above dan's, recorded in the window (creation + the new one)
    assert [d["decision"] for d in out["decisions"]][-1] == "Ship behind a flag"
    assert all(d["card_id"] == p["id"] for d in out["decisions"])
    md = out["markdown"]
    for h in ("## Agenda", "Stand-up radio", "## Blocked", "## Waiting for an approval",
              "## Incidents", "## Agents in error", "## Cards that moved", "## Recent decisions"):
        assert h in md
    # someone else's card is not in dan's brief
    board.add_card("mia's own", "x", project="radio", assignee="mia@x", bucket="Doing")
    assert all(x["title"] != "mia's own" for x in helm.morning_brief("radio", person="dan@x")["moved"])
    # a calendar configured by NAME in the vault
    import vault
    monkeypatch.setattr(vault, "KEY_PATH", str(env["tmp"] / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(env["tmp"] / "vault.json"))
    vault.set_secret("DAN_ICS", "https://cal.example.com/private.ics")
    hc.set_source("dan@x", "ics", "DAN_ICS")
    src = hc.source_for("dan@x")
    assert isinstance(src, hc.ICSUrlSource) and src.url.endswith("private.ics")
    # the same calendar through the `calendars` interface (3.4) used by the brief
    import calendars
    assert isinstance(hc.ICSCalendar(), calendars.CalendarProvider)
    assert hc.source_for("dan@x", "radio") is None        # radio's vault has no DAN_ICS
    with pytest.raises(ValueError):
        hc.set_source("dan@x", "graph", "DAN_ICS")


def test_morning_brief_template_is_a_valid_agent_and_delivers_to_quarantine(env):
    import agent_templates
    import agents
    f = agent_templates.render("morning-brief", person="dan@x")
    assert f["name"] == "morning-brief-dan" and "person=\"dan@x\"" in f["purpose"]
    v = agents.validate(f)
    assert v["outputs"] == ["memory", "notify"] and "success" in v["notify_on"]
    assert v["auto_approve"] == ["mcp__sokkan-board__morning_brief"]
    # a read-only tool: allowed unasked even on an alert-triggered agent
    assert not agents.is_write_rule("mcp__sokkan-board__morning_brief")
    a = agents.create({"email": "dan@x", "role": "dev", "project": "radio"}, f, activate=True)
    assert a["status"] == "active" and a["trigger"] == "cron"
    # the deliverable of a run is a memory note → quarantine (3.1 invariant)
    import quarantine
    r = quarantine.write(f"agent-{a['name']}-latest", "brief", "# Morning brief", {"agent": a["name"]},
                         project="radio")
    assert r["quarantined"] and (quarantine.qdir("radio") / f"agent-{a['name']}-latest.md").exists()
    assert [t["id"] for t in agent_templates.catalog(lambda fid: True)] == ["morning-brief"]
    assert agent_templates.catalog(lambda fid: False) == []


def test_brief_route_rights(env, monkeypatch):
    tree(env["board"])
    who = {"email": "dan@x"}
    c, _ = _client(monkeypatch, who)
    r = c.get("/api/helm/brief?project=radio")
    assert r.status_code == 200 and r.json()["person"] == "dan@x"
    assert c.get("/api/helm/brief?project=radio&person=mia@x").status_code == 403
    who["email"] = "mia@x"
    assert c.get("/api/helm/brief?project=radio&person=dan@x").status_code == 200
    assert c.get("/api/agent-templates").json()[0]["id"] == "morning-brief"


# ---- MCP: the board server drives the hierarchy --------------------------------------

def test_board_mcp_tree_brief_and_parent(env, monkeypatch):
    import board_mcp
    board = env["board"]
    p, a, b, s = tree(board)
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "dan@x")
    t = board_mcp.get_card_tree(s["id"])
    assert [x["id"] for x in t["parents"]] == [p["id"], a["id"]] and "No Kafka" in t["context"]
    c = board_mcp.create_card("sub-task from a session", parent_id=a["id"])
    assert c["parent_id"] == a["id"]
    assert "error" in board_mcp.update_card(p["id"], parent_id=c["id"])          # cycle
    assert board_mcp.update_card(c["id"], parent_id=0)["parent_id"] is None
    assert "markdown" in board_mcp.morning_brief(person="dan@x")
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "default")
    assert "error" in board_mcp.get_card_tree(s["id"])                           # other project
    assert "error" in board_mcp.create_card("x", parent_id=p["id"])


def test_periodic_job_refreshes_and_suggests(env, monkeypatch):
    board, helm = env["board"], env["helm"]
    p, *_ = tree(board)
    board.add_card("Radio player audio QA", "test the live radio audio player", project="radio",
                   parent_id=p["id"])
    sent = []
    import notify
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or {})
    out = helm.run_once()
    assert out["radio"]["new"] >= 1 and sent and "radio" in sent[0][0]
    assert json.loads(json.dumps(out))   # serialisable for the logs


def test_classified_parent_context_does_not_flow_below_its_level(env):
    """3.3 × 3.4: a child card inherits its parent's level; a parent classified ABOVE a card
    (moved under it afterwards) does not flow down into that card's sessions; the context
    note carries the card's level."""
    board, helm = env["board"], env["helm"]
    top = board.add_card("Merger talks", "x", project="radio", kind="project", level=3,
                         intent="Prepare the merger with the other broadcaster", user="mia@x")
    kid = board.add_card("Due diligence", "x", project="radio", parent_id=top["id"], user="mia@x")
    assert kid["level"] == 3                                 # inherited, never below
    assert "Prepare the merger" in helm.context_block(kid["id"])
    loose = board.add_card("Public FAQ", "x", project="radio", user="mia@x")
    board.set_parent(loose["id"], top["id"], user="mia@x")    # moved under: stays project
    assert board.get_card(loose["id"])["level"] == 2
    assert helm.context_block(loose["id"]) == ""
    note = helm.write_context_note(top["id"])
    assert "classification: confidential" in open(note["path"]).read()
