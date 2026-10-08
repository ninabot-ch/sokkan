"""SOKKAN 3.4 « Bridge » — what the manager journey of 08.10 changed in Helm and Nina.

The journey (a department head creates a project with Nina, follows it, acts on a reframe
suggestion, reads the brief) was ABANDONED on 3.3.0: Nina's proposal was cut at 800 tokens
or empty, it invented the owners, it landed in the header's project where the manager does
not steer it, the brief was only reachable through an agent. Each fix has its test here.
"""
# ruff: noqa: F811 — `env` is the 3.3 fixture, imported and requested by name
import json

import pytest

from test_helm import _client, env, tree  # noqa: F401 — the 3.3 fixture and helpers

import assistant


def _team(env):
    import iam
    import projects
    iam.upsert_user("mia@x", "dev", "Mia Manager")
    iam.upsert_user("dan@x", "dev", "Dan Dev")
    iam.upsert_user("ana@x", "dev", "Ana Dev")
    iam.upsert_user("vic@x", "dev", "Vic Viewer")
    projects.sync_sso_groups("ana@x", ["radio-devs"])
    projects.grant("radio", "team", "sso:radio-devs", "dev")


# ---- where a project can be created, and by whom its cards can be owned ---------------

def test_targets_list_projects_steered_first_with_their_people(env):
    import helm
    import iam
    _team(env)
    t = helm.targets(iam.get_user("mia@x"))
    assert [x["slug"] for x in t] == ["radio", "default"]          # steered first
    assert t[1]["steers"] is False and t[1]["role"] == "dev"       # instance dev = dev of default
    radio = t[0]
    assert radio["steers"] is True and radio["role"] == "maintainer"
    people = {p["email"]: p["name"] for p in radio["people"]}
    assert people == {"mia@x": "Mia Manager", "dan@x": "Dan Dev", "ana@x": "Ana Dev"}  # team member in, viewer out
    t = helm.targets(iam.get_user("dan@x"))
    assert t[0]["slug"] == "radio" and t[0]["steers"] is False
    assert [x["slug"] for x in helm.targets(iam.get_user("vic@x"))] == ["default"]  # viewer of radio: not there


def test_targets_route_and_owners_outside_the_project_are_reported(env, monkeypatch):
    _team(env)
    who = {"email": "mia@x"}
    c, _ = _client(monkeypatch, who)
    r = c.get("/api/helm/targets")
    assert r.status_code == 200 and r.json()[0]["slug"] == "radio"
    body = {"project": "radio", "title": "Bridge", "decisions": ["No new service"],
            "children": [{"title": "Logout", "assignee": "ana@x"},
                         {"title": "Teams", "assignee": "root@x"},          # known user, not in radio
                         {"title": "Brief", "assignee": "dan@example.ch"}]}  # invented
    out = c.post("/api/helm/projects", json=body).json()
    assert out["project"] == "radio"
    assert [ch["assignee"] for ch in out["children"]] == ["ana@x", "", ""]
    assert [d["assignee"] for d in out["dropped_owners"]] == ["root@x", "dan@example.ch"]
    assert out["card"]["decisions"] == ["No new service"]
    monkeypatch.setenv("SOKKAN_FEATURE_HELM", "0")
    assert c.get("/api/helm/targets").status_code == 404


# ---- suggestions: accepting the new scope answers the scope suggestion at once ----------

def test_accepting_the_new_scope_resolves_the_scope_suggestion(env, monkeypatch):
    import board
    import helm
    _team(env)
    who = {"email": "mia@x"}
    c, _ = _client(monkeypatch, who)
    out = c.post("/api/helm/projects", json={"project": "radio", "title": "Bridge",
                                             "children": [{"title": f"t{i}"} for i in range(3)]}).json()
    pid = out["card"]["id"]
    con = board._con()
    con.execute("UPDATE cards SET baseline_at=baseline_at-3600 WHERE id=?", (pid,))
    con.commit()
    con.close()
    for i in range(4):
        board.add_card(f"extra {i}", project="radio", user="dan@x", parent_id=pid, assignee="dan@x")
    helm.suggest("radio")
    assert any(s["kind"] == "scope" for s in helm.list_suggestions(["radio"]))
    assert c.post(f"/api/helm/cards/{pid}/baseline").status_code == 200
    assert not any(s["kind"] == "scope" for s in helm.list_suggestions(["radio"]))


def test_brief_of_the_whole_project_is_for_managers(env, monkeypatch):
    tree(env["board"])
    who = {"email": "dan@x"}
    c, _ = _client(monkeypatch, who)
    assert c.get("/api/helm/brief?project=radio&all=1").status_code == 403
    who["email"] = "mia@x"
    r = c.get("/api/helm/brief?project=radio&all=1")
    assert r.status_code == 200 and r.json()["person"] == "" and r.json()["team"] == ""
    assert "Morning brief — the project" in r.json()["markdown"]


# ---- Nina: the real projects and people, a budget that fits a proposal -----------------

def test_nina_gets_the_real_projects_and_people_never_example_addresses(env):
    _team(env)
    ctx = assistant._helm_context("mia@x")
    assert "project `radio` (Radio player) — role maintainer, steers it in Helm" in ctx
    radio_line = next(line for line in ctx.splitlines() if "`radio`" in line)
    assert "Ana Dev <ana@x>" in radio_line and "Vic" not in radio_line   # a viewer owns no card
    assert "Never invent an address" in ctx and "never tell the person to copy" in ctx
    import iam
    iam.upsert_user("zed@x", "viewer", "Zed")
    assert assistant._helm_context("zed@x") == ""                  # nowhere to create
    kb = (assistant.KB_DIR / "09-helm.md").read_text()
    assert "example.ch" not in kb and '"project": "<slug>"' in kb


def test_the_kb_follows_the_conversation_not_the_last_line(env, monkeypatch):
    import features
    seen = {}
    monkeypatch.setattr(assistant, "_llm_config", lambda: {"url": "http://x", "token": "t", "api": "openai", "model": "m"})
    monkeypatch.setattr(assistant, "_fallback_config", lambda: None)
    monkeypatch.setattr(assistant, "history", lambda u, n: [
        {"role": "user", "content": "Create a project with me: interview me"},
        {"role": "assistant", "content": "What is the goal?"}])
    monkeypatch.setattr(assistant, "_memory_hits", lambda *a, **k: [])
    monkeypatch.setattr(features, "enabled", lambda f: f != "helm")
    _cfg, _fb, system, _msgs = assistant._prepare("mia@x", "In scope: the web player only.")
    seen["system"] = system
    assert "## Si l'utilisateur veut créer un projet avec toi" in seen["system"]


def test_answer_budget_is_configurable_and_large_enough_for_a_proposal(monkeypatch):
    import importlib
    assert assistant.MAX_TOKENS >= 2048
    monkeypatch.setenv("SOKKAN_ASSISTANT_MAX_TOKENS", "100")
    m = importlib.reload(assistant)
    try:
        assert m.MAX_TOKENS == 256                                   # floor
        monkeypatch.setenv("SOKKAN_ASSISTANT_MAX_TOKENS", "")        # empty compose value
        assert importlib.reload(assistant).MAX_TOKENS == 2048
    finally:
        monkeypatch.delenv("SOKKAN_ASSISTANT_MAX_TOKENS", raising=False)
        importlib.reload(assistant)


def _sse(lines):
    class R:
        headers = {"content-type": "text/event-stream"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_lines(self): return iter(lines)
    return R()


def _chat(monkeypatch, lines, message):
    monkeypatch.setattr(assistant.httpx, "stream", lambda *a, **k: _sse(lines))
    monkeypatch.setattr(assistant, "_prepare", lambda *a, **k: (
        {"url": "http://x/v1", "token": "t", "api": "openai", "model": "m"}, None, "S", []))
    saved = []
    monkeypatch.setattr(assistant, "_persist", lambda u, m, r: saved.append(r))
    return list(assistant.chat_stream("mia@x", message)), saved


def test_reasoning_is_never_shown_and_an_exhausted_budget_is_said(monkeypatch):
    # gpt-oss on vLLM: the thinking arrives in `reasoning`, the budget runs out before any text
    ev, saved = _chat(monkeypatch, [
        'data: {"choices":[{"delta":{"reasoning":"We need to ask the scope..."}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        'data: [DONE]'], "Create a project with me please")
    assert [k for k, _ in ev] == ["done"]
    assert "answer budget" in ev[0][1] and "SOKKAN_ASSISTANT_MAX_TOKENS" in ev[0][1]
    assert "We need" not in ev[0][1] and saved == [ev[0][1]]


def test_a_cut_answer_says_so_in_the_person_s_language(monkeypatch):
    ev, saved = _chat(monkeypatch, [
        'data: {"choices":[{"delta":{"content":"```sokkan-project {\\"title\\": \\"B"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'],
        "Peux-tu me proposer le découpage du projet ?")
    assert ev[-1][0] == "done" and "Réponse coupée" in ev[-1][1] and "Réponse coupée" in saved[0]


def test_non_streaming_cut_is_flagged(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "part"}, "finish_reason": "length"}]}
    monkeypatch.setattr(assistant.httpx, "post", lambda *a, **k: R())
    st = {}
    assert assistant._ask({"url": "http://x/v1", "token": "t", "api": "openai", "model": "m"},
                          "S", [], "a@b", st) == "part"
    assert st == {"cut": True}


# ---- classification × Helm: what a manager is not cleared for does not exist in Helm ----

@pytest.fixture()
def classified(env, monkeypatch):
    """`radio`: a public project card with a confidential child, and a confidential project
    card. mia (maintainer, clearance `project` by default) steers radio; lea (maintainer,
    cleared confidential through user:lea@x) steers it too."""
    import board
    import classification
    import helm
    import projects
    monkeypatch.setattr(classification, "enabled", lambda: True)
    projects.grant("radio", "user", "lea@x", "maintainer")
    classification.set_group_level("user:lea@x", "confidential", "radio", "root@x")
    pub = board.add_card("Player v2", project="radio", user="mia@x", kind="project",
                         intent="HLS everywhere", assignee="mia@x")
    ok = board.add_card("HLS web player", project="radio", user="dan@x", parent_id=pub["id"],
                        assignee="dan@x")
    sec = board.add_card("Rotate the DRM keys (HSM PIN 1234)", project="radio", user="dan@x",
                         parent_id=pub["id"], assignee="dan@x", level=3)
    board.update_card(sec["id"], user="dan@x", bucket="Doing")
    top = board.add_card("Acquisition of a rival radio", project="radio", user="lea@x",
                         kind="project", level=3, assignee="lea@x",
                         decisions=["No word to the newsroom"])
    board.add_card("Due diligence", project="radio", user="lea@x", parent_id=top["id"], level=3)
    for c in (pub, top):
        helm.refresh(c["id"])
    return {"pub": pub, "ok": ok, "sec": sec, "top": top}


def test_helm_shows_a_manager_only_what_they_are_cleared_for(env, classified, monkeypatch):
    pub, sec, top = classified["pub"], classified["sec"], classified["top"]
    who = {"email": "mia@x"}
    c, _ = _client(monkeypatch, who)
    deck = c.get("/api/helm/deck").json()
    assert [i["card"]["id"] for i in deck["items"]] == [pub["id"]]          # not the confidential one
    d = c.get(f"/api/helm/cards/{pub['id']}").json()
    kids = [k["id"] for col in d["kanban"]["cards"].values() for k in col]
    assert sec["id"] not in kids and classified["ok"]["id"] in kids
    assert sec["id"] not in [k["id"] for k in d["children"]]
    assert "HSM" not in json.dumps(d)
    assert "HSM" not in json.dumps(c.get(f"/api/helm/cards/{pub['id']}/activity").json())
    assert c.get(f"/api/helm/cards/{sec['id']}").status_code == 404
    assert c.get(f"/api/helm/cards/{top['id']}/activity").status_code == 404
    brief = c.get("/api/helm/brief?project=radio&all=1").json()
    assert "HSM" not in brief["markdown"] and "Acquisition" not in brief["markdown"]
    assert "No word to the newsroom" not in json.dumps(brief)
    # the board's own card dialog: the confidential child is not listed under its parent
    assert sec["id"] not in [k["id"] for k in c.get(f"/api/board/card/{pub['id']}").json()["children"]]
    # cleared: lea sees all of it
    who["email"] = "lea@x"
    assert {i["card"]["id"] for i in c.get("/api/helm/deck").json()["items"]} == {pub["id"], top["id"]}
    d = c.get(f"/api/helm/cards/{pub['id']}").json()
    assert sec["id"] in [k["id"] for col in d["kanban"]["cards"].values() for k in col]
    assert "Acquisition" in c.get("/api/helm/brief?project=radio&all=1").json()["markdown"]


def test_a_classified_ancestor_keeps_its_id_not_its_title(env, classified, monkeypatch):
    import board
    sec = classified["sec"]
    under = board.add_card("Sub-task of the rotation", project="radio", user="dan@x",
                           parent_id=sec["id"], level=3)
    con = board._con()
    con.execute("UPDATE cards SET level=2 WHERE id=?", (under["id"],))   # a harmless child
    con.commit()
    con.close()
    d = board.card_detail(under["id"], max_level=2)
    crumb = {b["id"]: b for b in d["breadcrumb"]}
    assert crumb[sec["id"]]["title"] == "(classified)" and crumb[sec["id"]]["classified"]
    assert crumb[classified["pub"]["id"]]["title"] == "Player v2"


def test_suggestions_about_a_classified_card_are_not_shown(env, classified, monkeypatch):
    import helm
    sec = classified["sec"]
    helm.init()
    con = helm._con()
    now = __import__("time").time()
    con.execute("INSERT INTO helm_suggestions(project, card_id, target_id, kind, fingerprint, title,"
                " detail, evidence, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("radio", classified["pub"]["id"], sec["id"], "contradiction", "fp-x",
                 f"#{sec['id']} “Rotate the DRM keys (HSM PIN 1234)” may contradict a decision",
                 "", "{}", "open", now, now))
    con.commit()
    sid = con.execute("SELECT max(id) FROM helm_suggestions").fetchone()[0]
    con.close()
    who = {"email": "mia@x"}
    c, _ = _client(monkeypatch, who)
    assert "HSM" not in json.dumps(c.get(f"/api/helm/cards/{classified['pub']['id']}").json()["suggestions"])
    assert c.post(f"/api/helm/suggestions/{sid}/ignore").status_code == 404
    who["email"] = "lea@x"
    assert "HSM" in json.dumps(c.get(f"/api/helm/cards/{classified['pub']['id']}").json()["suggestions"])


def test_a_proposal_s_non_breaking_spaces_become_plain(env):
    import helm
    out = helm.create_project("mia@x", "radio", "SOKKAN 3.4 Bridge",
                              children=[{"title": "back‑channel logout"}])
    assert out["card"]["title"] == "SOKKAN 3.4 Bridge"
    assert out["children"][0]["title"] == "back-channel logout"


def test_a_streamed_answer_announces_its_level_first(monkeypatch):
    def prep(user, message, scope=None, meta=None, via="nina"):
        meta["level"] = "confidential"
        return {"url": "http://x/v1", "token": "t", "api": "openai", "model": "m"}, None, "S", []
    monkeypatch.setattr(assistant, "_prepare", prep)
    monkeypatch.setattr(assistant.httpx, "stream", lambda *a, **k: _sse(
        ['data: {"choices":[{"delta":{"content":"CHF 48 000"}}]}']))
    monkeypatch.setattr(assistant, "_persist", lambda *a: None)
    ev = list(assistant.chat_stream("lea@x", "what does the POC cost?"))
    assert ev[0] == ("level", "confidential") and ev[-1] == ("done", "CHF 48 000")
