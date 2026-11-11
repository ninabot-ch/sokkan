"""The pilot candidate (3.6) — what holds BETWEEN the four lots, which none of them could test
alone: `onprem-appliance` (named accounts in local auth, local router), `study-mode` (journal
of human validations, study), `simple-profile` (document drop, simple profile) and
`doc-ingest` (ingestion by batch).

  a. a click on the simple profile's approval card is a line of the journal of validations;
  b. the study hooks of the simple profile work with `study_mode` on, and are silent without;
  c. three named accounts = three pseudonyms, three actors, one project each — no name out;
  d. a dropped document and the ingestion do not step on each other, and nothing a session
     learnt from a dropped document enters the memory without the quarantine;
  e. every decision of the batch review screen is journaled;
  f. the appliance profile turns the four features on, with what they need;
  g. the contract with the inference gateway (GET /usage, headers, 410 / 412 / 429).

The world is the appliance: local sign-in, the owner's token, three people with a personal
access code and a project each. No model: a turn is recorded by a client that answers nothing.
"""
import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simple_fixtures import docx

ROOT = Path(__file__).resolve().parent.parent
OWNER = "admin@eskills.example"
OWNER_TOKEN = "owner-token-0123456789"
STUDY = "heg-eskills-2027"
PEOPLE = [("anaelle.rochat@eskills.example", "Anaëlle Rochat", "formation"),
          ("benoit.favre@eskills.example", "Benoît Favre", "qualite"),
          ("cleo.dumas@eskills.example", "Cléo Dumas", "direction")]
# what must never be in a researcher's package
NAMES = [x for e, n, p in PEOPLE for x in (e, e.split("@")[0], *n.split(" "))] + \
        ["eskills.example", OWNER, "formation", "qualite", "direction"]


class _SilentClient:
    """The model of these tests: receives the turn, answers nothing."""

    def __init__(self):
        self.asked: list[str] = []

    async def query(self, text):
        self.asked.append(text)

    async def receive_response(self):
        if False:
            yield None

    async def interrupt(self):
        return None


@pytest.fixture()
def box(tmp_path, monkeypatch):
    import agentchat
    import audit
    import auth
    import board
    import documents
    import iam
    import llm
    import localaccounts
    import projects
    import simple
    import study
    import uiprefs
    import vault

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(data))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    monkeypatch.setenv("SOKKAN_DOCUMENTS_DB", str(tmp_path / "documents.db"))
    for f in ("MULTI_PROJECT", "DOCUMENTS", "SIMPLE_PROFILE", "STUDY_MODE"):
        monkeypatch.setenv(f"SOKKAN_FEATURE_{f}", "1")
    monkeypatch.setenv("SOKKAN_PROFILE_DEFAULT", "simple")
    monkeypatch.setenv("SOKKAN_STUDY_ID", STUDY)
    for v in ("SOKKAN_EDITION", "SOKKAN_PROFILE_LOCKED", "SOKKAN_STUDY_EXPIRES_AT",
              "SOKKAN_STUDY_AUDIT_REQUIRED", "SOKKAN_INFER_BASE_URL", "SOKKAN_INFER_TOKEN",
              "SOKKAN_SHIP_URL", "SOKKAN_DEEP_URL", "SOKKAN_DEEP_TOKEN", "SOKKAN_UIPREFS_STORE",
              "SOKKAN_FEATURE_CLASSIFICATION", "SOKKAN_FEATURE_DOC_INGEST", "SOKKAN_AGENT_RUN",
              "SOKKAN_SESSION_ID", "CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_API_KEY",
              "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    monkeypatch.setattr(iam, "SEED", {OWNER: ("owner", "Admin eSkills")})
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")          # what the appliance profile sets
    monkeypatch.setattr(projects, "DB", tmp_path / "projects.db")
    projects.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(documents, "DB", tmp_path / "documents.db")
    monkeypatch.setattr(simple, "DB", tmp_path / "simple.db")
    monkeypatch.setattr(study, "DB", tmp_path / "study.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    if hasattr(uiprefs, "STORE"):
        monkeypatch.setattr(uiprefs, "STORE", tmp_path / "uiprefs.json")
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(agentchat, "CWD", str(ws))
    monkeypatch.setattr(agentchat, "_registry", {})
    monkeypatch.setenv("SOKKAN_PROJECT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setattr(auth, "MODE", "local")
    monkeypatch.setattr(auth, "LOCAL_TOKEN", OWNER_TOKEN)
    monkeypatch.setattr(auth, "OWNER_EMAIL", OWNER)
    study.reset_cache()

    clients: list[_SilentClient] = []

    async def started(self):
        if self.client is None:
            self.client = _SilentClient()
            clients.append(self.client)
    monkeypatch.setattr(agentchat.AgentSession, "ensure_started", started)

    import app as a
    a._login_fails.clear()
    codes = {e: localaccounts.add(e, n, project=p)["code"] for e, n, p in PEOPLE}

    def login(code: str, project: str = "") -> TestClient:
        c = TestClient(a.app)
        r = c.post("/api/auth/local", json={"token": code})
        assert r.status_code == 200, r.text
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    people = {e: login(codes[e], p) for e, _n, p in PEOPLE}
    yield {"app": a, "tmp": tmp_path, "data": data, "workspace": ws, "people": people,
           "owner": lambda: login(OWNER_TOKEN), "clients": clients, "login": login,
           "codes": codes}
    study.reset_cache()


# ---- helpers ------------------------------------------------------------------------------
def _consent(c, scenarios=()):
    r = c.post("/api/study/consent", json={"content_scenarios": list(scenarios)})
    assert r.status_code == 200, r.text


def _start(c, ask="Un plan de formation Excel sur deux jours", docs=()):
    r = c.post("/api/simple/start", json={"ask": ask, "lang": "fr", "documents": list(docs)})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _wait(cond, what, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


class _Tool:
    """A tool call of the session's agent, played for real (`AgentSession._can_use_tool`) until
    the person decides. With `ws` (an open cockpit WebSocket) it runs in the API's own event
    loop, where the SDK's callback runs in production: the card reaches the browser live.
    Without, in a loop of its own (nobody is connected: refusals nobody clicks, replays)."""

    def __init__(self, sess, tool, inp, ws=None):
        self.sess, self.result, self.error, self.future, self.thread = sess, None, None, None, None
        self.known = set(sess._perms)
        if ws is not None:
            self.future = ws.portal.start_task_soon(sess._can_use_tool, tool, inp, None)
            return

        def run():
            loop = asyncio.new_event_loop()

            async def main():
                task = asyncio.ensure_future(sess._can_use_tool(tool, inp, None))
                while not task.done():          # keeps the loop turning: the decision comes
                    await asyncio.sleep(0.005)  # from the API's own thread
                return task.result()
            try:
                self.result = loop.run_until_complete(main())
            except Exception as e:  # noqa: BLE001
                self.error = e
            finally:
                loop.close()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def _over(self) -> bool:
        return self.future.done() if self.future is not None else not self.thread.is_alive()

    def pid(self) -> str:
        pid = _wait(lambda: next(iter(set(self.sess._perms) - self.known), None)
                    or (self._over() and "over"), "the session asks a person")
        assert pid != "over", "the tool call ended without asking anybody"
        return pid

    def done(self):
        if self.future is not None:
            return self.future.result(5)
        self.thread.join(5)
        assert not self.thread.is_alive() and self.error is None, self.error
        return self.result


def _until(ws, kind, **match):
    for _ in range(200):
        ev = ws.receive_json()
        if ev.get("type") == kind and all(ev.get(k) == v for k, v in match.items()):
            return ev
    raise AssertionError(f"no {kind} event")


def _hitl(**kw):
    import hitl_journal
    return hitl_journal.query(**kw)


def _deliverable(box, project, sid, name="Plan de formation.md"):
    import simple
    return str(simple.deliverable_dir(project, sid) / name)


# ============ a. the approval card of the simple profile → the journal of validations ========
def test_each_click_of_the_simple_approval_card_is_a_line_of_the_journal(box):
    """Allow, Refuse, Correct-then-allow — through the simple view's WebSocket, the way the
    browser does — each leave ONE row: asked at, decided at, delay, who, and the edit marked."""
    import agentchat
    import audit
    ana_mail, _name, project = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    sid = _start(ana)
    sess = agentchat.peek(sid)
    target = _deliverable(box, project, sid)
    draft = "# Plan de formation\n\nJour 1 : tableaux croisés.\n"

    decided = {}
    with ana.websocket_connect(f"/api/agent/ws/{sid}?view=simple") as ws:
        for label, send in (
                ("allow", {"decision": "allow"}),
                ("deny", {"decision": "deny", "message": "Il manque le jour 2"}),
                ("edit", {"decision": "allow", "draft_text": draft + "Jour 2 : graphiques.\n",
                          # the browser never chooses the path nor the raw input: ignored
                          "updated_input": {"file_path": "/etc/passwd", "content": "x"}})):
            tool = _Tool(sess, "Write", {"file_path": target, "content": draft}, ws)
            pid = tool.pid()
            card = _until(ws, "approval", id=pid)
            assert "Write" not in json.dumps({k: v for k, v in card.items() if k != "detail"})
            time.sleep(0.12)                                 # the person reads before clicking
            ws.send_json({"type": "permission", "id": pid, **send})
            _until(ws, "approval_resolved", id=pid)
            decided[label] = (pid, tool.done())
    assert type(decided["allow"][1]).__name__ == "PermissionResultAllow"
    assert type(decided["deny"][1]).__name__ == "PermissionResultDeny"
    assert decided["edit"][1].updated_input == {"file_path": target,
                                                "content": draft + "Jour 2 : graphiques.\n"}

    rows = {r["request_id"]: r for r in _hitl(session_id=sid) if r["kind"] == "permission"
            and not r["auto"]}
    assert len(rows) == 3
    for label, (pid, _res) in decided.items():
        r = rows[pid]
        assert r["decision"] == label, (label, r["decision"])
        assert (r["actor"], r["origin"], r["owner"], r["project"]) == (ana_mail, "owner", ana_mail, project)
        assert r["tool"] == "Write" and r["session_id"] == sid
        assert r["requested_at"] < r["decided_at"] and r["delay_ms"] >= 100
        assert r["delay_ms"] == pytest.approx((r["decided_at"] - r["requested_at"]) * 1000, abs=2)
    assert rows[decided["allow"][0]]["edited"] == 0 and rows[decided["allow"][0]]["edited_summary"] == ""
    assert rows[decided["deny"][0]]["message"] == "Il manque le jour 2"
    edit = rows[decided["edit"][0]]
    assert edit["edited"] == 1 and "Jour 2 : graphiques" in edit["edited_summary"]
    assert "/etc/passwd" not in edit["edited_summary"] and "Jour 2" not in edit["input_summary"]
    # … and in Operate › Journal, under the person's own name
    lines = [e for e in audit.recent(200) if e["action"].startswith("hitl.")]
    assert sorted(e["action"] for e in lines) == ["hitl.allow", "hitl.deny", "hitl.edit"]
    assert {e["user"] for e in lines} == {ana_mail} and {e["project"] for e in lines} == {project}


def test_what_nobody_clicked_in_a_simple_work_is_not_a_validation(box):
    """The shell request a simple work refuses on its own is in the journal as automatic,
    never as a person's refusal; a card left unanswered is closed, not left looking live."""
    import agentchat
    ana_mail, _n, project = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    sid = _start(ana)
    sess = agentchat.peek(sid)
    res = _Tool(sess, "Bash", {"command": "mkdir -p out"}).done()
    assert type(res).__name__ == "PermissionResultDeny"
    (row,) = _hitl(session_id=sid)
    assert (row["decision"], row["auto"], row["actor"], row["origin"]) == ("auto_deny", 1, "", "system")
    assert row["auto_reason"] == "policy:simple_profile_no_shell" and row["delay_ms"] == 0


def test_the_approval_mode_cannot_be_widened_from_the_simple_view_and_nothing_is_journaled(box):
    import agentchat
    ana_mail, _n, _p = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    sid = _start(ana)
    with ana.websocket_connect(f"/api/agent/ws/{sid}?view=simple") as ws:
        ws.send_json({"type": "mode", "mode": "bypassPermissions"})
        ws.send_json({"type": "interrupt"})
        time.sleep(0.2)
    assert agentchat.peek(sid).mode == "default"
    assert not [r for r in _hitl(session_id=sid) if r["kind"] == "mode"]


# ============ b. the study hooks of the simple profile ======================================
def test_study_hooks_of_the_simple_profile_with_study_mode_on(box):
    """What components/simple/StudyHooks.tsx and the consent dialog call, in their order."""
    import study
    ana_mail, _n, project = PEOPLE[0]
    ana = box["people"][ana_mail]
    feats = ana.get("/api/features").json()
    assert next(i for i in feats["registry"]["items"] if i["id"] == "study_mode")["enabled"] is True
    me = ana.get("/api/study/me").json()
    assert me["enabled"] and me["active"] and me["study_id"] == STUDY and me["blocked"] is True
    assert me["scenarios"] == list(study.SCENARIOS) and me["consent"]["given"] is False
    # no work before the consent: refused BEFORE a conversation is created, with a code the
    # simple profile turns into « What is recorded »
    r = ana.post("/api/simple/start", json={"ask": "Un plan de formation", "lang": "fr"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "study_consent"
    assert "Étude" in r.json()["detail"]["message"]
    assert ana.get("/api/simple/works").json()["works"] == []
    _consent(ana, ["S2"])
    me = ana.get("/api/study/me").json()
    assert me["consent"]["given"] and me["consent"]["current"] and me["blocked"] is False
    assert me["pseudonym"] == "P1" and me["consent"]["content_scenarios"] == ["S2"]

    sid = _start(ana)
    # the first turn of a consenting participant tags the work by itself (study.turn_start)
    tag = _wait(lambda: (lambda t: t if t.get("tagged") else None)(
        ana.get(f"/api/sessions/{sid}/study").json()), "the work is tagged")
    assert tag["participant"] == "P1" and tag["mine"] is True and tag["scenario"] == ""
    assert box["clients"] and "plan de formation" in box["clients"][0].asked[0].lower()
    # the scenario picked under the title
    r = ana.put(f"/api/sessions/{sid}/study", json={"scenario": "S2"})
    assert r.status_code == 200 and r.json()["scenario"] == "S2"
    assert ana.get(f"/api/sessions/{sid}/study").json()["scenario"] == "S2"
    assert ana.put(f"/api/sessions/{sid}/study", json={"scenario": "S9"}).status_code == 400
    # the two questions, once the deliverable is validated — and the band does not ask for
    # them while the agent is still working on that very task
    import sqlite3
    con = sqlite3.connect(box["tmp"] / "study.db")
    con.execute("UPDATE turns SET ended_at = NULL WHERE session_id = ?", (sid,))
    con.commit()
    assert ana.get("/api/study/me").json()["pending_feedback"] == 0      # a turn is running
    con.execute("UPDATE turns SET ended_at = started_at + 1 WHERE session_id = ?", (sid,))
    con.commit()
    con.close()
    assert ana.get("/api/study/me").json()["pending_feedback"] == 1
    r = ana.post(f"/api/sessions/{sid}/study/feedback", json={"useful": 4, "verify_minutes": 7.5})
    assert r.status_code == 200 and r.json()["useful"] == 4 and r.json()["verify_minutes"] == 7.5
    assert ana.post(f"/api/sessions/{sid}/study/feedback",
                    json={"useful": 9, "verify_minutes": 1}).status_code == 400
    assert ana.get("/api/study/me").json()["pending_feedback"] == 0
    (mine,) = ana.get("/api/study/sessions").json()["sessions"]
    assert (mine["session_id"], mine["scenario"], mine["useful"], mine["turns"]) == (sid, "S2", 4, 1)
    # somebody else's work is not theirs to tag nor to answer for
    ben = box["people"][PEOPLE[1][0]]
    _consent(ben)
    assert ben.put(f"/api/sessions/{sid}/study", json={"scenario": "S1"}).status_code in (403, 404)
    assert ben.post(f"/api/sessions/{sid}/study/feedback",
                    json={"useful": 1, "verify_minutes": 0}).status_code in (403, 404)
    assert ana.get(f"/api/sessions/{sid}/study").json()["scenario"] == "S2"


def test_study_hooks_are_silent_without_study_mode(box, monkeypatch):
    import study
    monkeypatch.setenv("SOKKAN_FEATURE_STUDY_MODE", "0")
    study.reset_cache()
    ana = box["people"][PEOPLE[0][0]]
    feats = ana.get("/api/features").json()
    assert next(i for i in feats["registry"]["items"] if i["id"] == "study_mode")["enabled"] is False
    assert ana.get("/api/study/me").json() == {"enabled": False, "active": False}
    sid = _start(ana)                                          # nobody is asked for a consent
    _wait(lambda: box["clients"] and box["clients"][0].asked, "the turn ran")
    for method, url, body in (("get", f"/api/sessions/{sid}/study", None),
                              ("put", f"/api/sessions/{sid}/study", {"scenario": "S1"}),
                              ("post", f"/api/sessions/{sid}/study/feedback",
                               {"useful": 3, "verify_minutes": 1}),
                              ("post", "/api/study/consent", {"content_scenarios": []})):
        r = getattr(ana, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (url, r.status_code)
    # nothing was tagged, nobody got a pseudonym, the journal of validations still works
    db = box["tmp"] / "study.db"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(db)
        for table in ("participants", "tags", "turns", "feedback", "consents"):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        con.close()
    import llm
    assert "x-sokkan-study" not in llm.identity_headers(PEOPLE[0][0])


def test_without_an_active_study_the_feature_asks_nothing(box, monkeypatch):
    """`study_mode` on (the appliance default) but no study declared: nobody is blocked."""
    import study
    monkeypatch.delenv("SOKKAN_STUDY_ID")
    study.reset_cache()
    ana = box["people"][PEOPLE[0][0]]
    me = ana.get("/api/study/me").json()
    assert me["enabled"] is True and me["active"] is False and me["blocked"] is False
    sid = _start(ana)
    _wait(lambda: box["clients"] and box["clients"][0].asked, "the turn ran")
    assert ana.get(f"/api/sessions/{sid}/study").json() == {"tagged": False}


# ============ c. three named accounts of the local sign-in ==================================
def _everything_exported(c) -> str:
    import io
    import zipfile
    r = c.get("/api/study/export?format=json")
    assert r.status_code == 200, r.text
    z = c.get("/api/study/export?format=zip")
    assert z.status_code == 200
    parts = [r.text]
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        parts += [n + "\n" + zf.read(n).decode("utf-8", "replace") for n in zf.namelist()]
    return "\n".join(parts)


def test_three_named_accounts_are_three_pseudonyms_three_actors_and_a_project_each(box):
    import agentchat
    import audit
    import features
    import llm
    import study
    assert study.single_identity() is False and features._study_check() is None
    sids = {}
    for mail, _name, project in PEOPLE:
        c = box["people"][mail]
        _consent(c)
        sid = sids[mail] = _start(c, ask=f"Note de synthèse pour {project}")
        sess = agentchat.peek(sid)
        with c.websocket_connect(f"/api/agent/ws/{sid}?view=simple") as ws:
            tool = _Tool(sess, "Write", {"file_path": _deliverable(box, project, sid),
                                         "content": f"# Synthèse {project}\n"}, ws)
            pid = tool.pid()
            _until(ws, "approval", id=pid)
            ws.send_json({"type": "permission", "id": pid, "decision": "allow"})
            _until(ws, "approval_resolved", id=pid)
            tool.done()
        assert c.put(f"/api/sessions/{sid}/study", json={"scenario": "S1"}).status_code == 200
        assert c.post(f"/api/sessions/{sid}/study/feedback",
                      json={"useful": 5, "verify_minutes": 3}).status_code == 200

    # three pseudonyms, in the order of the first consent
    assert [study.pseudonym_of(m, create=False) for m, _n, _p in PEOPLE] == ["P1", "P2", "P3"]
    # three actors in the journal of validations, each in their own project
    rows = [r for r in _hitl() if r["kind"] == "permission"]
    assert sorted((r["actor"], r["project"], r["origin"]) for r in rows) == sorted(
        (m, p, "owner") for m, _n, p in PEOPLE)
    # … and in the action journal: nobody acts as the owner
    for action in ("hitl.allow", "session.spawn", "auth.login"):
        users = {e["user"] for e in audit.recent(500) if e["action"] == action}
        assert users == {m for m, _n, _p in PEOPLE}, (action, users)
    # one project per person: the others do not exist for them
    for mail, _n, project in PEOPLE:
        c = box["people"][mail]
        assert [w["sid"] for w in c.get("/api/simple/works").json()["works"]] == [sids[mail]]
        for other_mail, _on, other in PEOPLE:
            if other == project:
                continue
            assert c.get("/api/simple/works", headers={"x-sokkan-project": other}).status_code == 404
            assert c.get(f"/api/simple/sessions/{sids[other_mail]}").status_code == 404
            assert c.get(f"/api/sessions/{sids[other_mail]}/study").status_code in (403, 404)
    # the inference gateway hears a pseudonym and the study, never an address
    for i, (mail, _n, _p) in enumerate(PEOPLE, 1):
        assert llm.identity_headers(mail) == {"x-sokkan-study": STUDY, "x-sokkan-user": f"P{i}"}

    # the researcher's package: read with an export token the owner created
    owner = box["owner"]()
    admin = owner.get("/api/admin/study").json()
    assert admin["single_identity"] is False and admin["auth_mode"] == "local"
    assert [(p["pseudonym"], p["email"]) for p in admin["participants"]] == [
        (f"P{i}", m) for i, (m, _n, _p) in enumerate(PEOPLE, 1)]      # the local table, admins only
    tok = owner.post("/api/admin/study/tokens", json={"label": "HEG", "days": 30}).json()["token"]
    researcher = TestClient(box["app"].app)
    researcher.headers["authorization"] = f"Bearer {tok}"
    blob = _everything_exported(researcher)
    for needle in NAMES + list(sids.values()) + ["Synthèse", "Note de synthèse"]:
        assert needle not in blob, f"{needle!r} is in the researcher's package"
    pkg = researcher.get("/api/study/export?format=json").json()
    flat = json.dumps(pkg)
    assert all(f'"P{i}"' in flat for i in (1, 2, 3)) and '"P4"' not in flat
    assert pkg["manifest"]["study_id"] == STUDY
    # the token opens that package and nothing else
    assert researcher.get("/api/admin/study").status_code in (401, 403)
    assert researcher.get("/api/hitl/journal").status_code in (401, 403)


def test_the_owner_token_alone_is_one_identity_and_the_study_says_so(box):
    import features
    import localaccounts
    import study
    for mail, _n, _p in PEOPLE:
        localaccounts.revoke(mail)
    assert study.single_identity() is True
    assert "personal access code" in features._study_check()
    assert box["owner"]().get("/api/admin/study").json()["single_identity"] is True
    code = localaccounts.add("nouvelle@eskills.example", "Nouvelle", project="essai")["code"]
    assert code and study.single_identity() is False and features._study_check() is None


# ============ d. dropped documents × ingestion × memory =====================================
def _drop(c, name="Brief client.docx", session=""):
    r = c.put("/api/documents", params={"name": name, **({"session": session} if session else {})},
              content=docx(("Brief client", "Douze assistantes, deux jours, Excel intermédiaire.")),
              headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    return r.json()


def test_an_ingestion_source_never_reads_the_instances_own_space(box):
    """The documents people drop, the deliverables, the memory and its quarantine have their
    own levels and their own review: a source of `doc_ingest` cannot be pointed at them."""
    import documents
    import quarantine
    import simple
    from docingest import batches, config
    ana_mail, _n, project = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    doc = _drop(ana)
    stored = documents.path_of(documents.get(doc["id"]))
    outside = box["tmp"] / "nas" / "formation"
    outside.mkdir(parents=True)
    ok = config.parse_source({"id": "nas", "kind": "folder", "root": str(outside), "allow": ["Cours"]})
    assert ok.root == str(outside)
    sid = _start(ana, docs=[doc["id"]])
    own = [box["data"], documents.docs_dir(project), stored.parent,
           simple.deliverable_dir(project, sid), documents.docs_dir("default"), box["workspace"],
           quarantine.memory_dir(), quarantine.qdir(), box["tmp"]]       # the last one CONTAINS them
    for root in own:
        with pytest.raises(config.ConfigError, match="overlaps SOKKAN's own space"):
            config.parse_source({"id": "piege", "kind": "folder", "root": str(root), "allow": ["**"]})
    # where each one writes: three places, none inside another
    places = [documents.docs_dir(project).resolve(), config.home().resolve(),
              batches.batch_dir(project, 1).resolve(), quarantine.memory_dir(project).resolve()]
    for i, a in enumerate(places):
        for j, b in enumerate(places):
            assert i == j or (a != b and a not in b.parents), (a, b)


def test_a_dropped_document_creates_no_note_no_batch_and_keeps_its_own_level(box, monkeypatch):
    import documents
    import quarantine
    from docingest import batches, state
    monkeypatch.setenv("SOKKAN_FEATURE_DOC_INGEST", "1")
    state._ready.clear()
    ana_mail, _n, project = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    doc = _drop(ana)
    sid = _start(ana, docs=[doc["id"]])
    _wait(lambda: box["clients"] and box["clients"][0].asked, "the turn ran")
    assert batches.list_batches(project) == [] and state.pass_list(project=project) == []
    for d in (quarantine.memory_dir(project), quarantine.qdir(project)):
        assert not d.exists() or not list(d.rglob("*.md")), d
    # the document's level is the document's (documents.db), not a source's floor
    stored = documents.get(doc["id"])
    assert stored["level"] == documents._default_level() and stored["project"] == project
    assert documents.session_has_documents(sid) is True
    assert documents.session_has_documents("0" * 32) is False


def test_what_a_session_learnt_from_a_dropped_document_waits_in_quarantine(box, monkeypatch):
    """The two ways a session writes memory: the memory server's tool, and a file tool aimed
    at the memory directory. With a document attached, neither lands in the memory."""
    import agentchat
    import index_memory
    import memory_search_server as mem
    import quarantine
    ana_mail, _n, _project = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    mdir = quarantine.memory_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(index_memory, "MEMORY_DIR", mdir)
    monkeypatch.setattr(index_memory, "DB_PATH", box["tmp"] / "memory.db")
    monkeypatch.setattr(mem, "DB_PATH", box["tmp"] / "memory.db")
    monkeypatch.setattr(mem, "MEMORY_DIR", mdir)
    monkeypatch.setattr(index_memory, "embed_batch", lambda texts: [[1.0, 0.0] for _ in texts])
    index_memory.run_index(rebuild=True)

    # a work on the DEFAULT project of the instance, owned by the owner, with one document
    owner = box["owner"]()
    _consent(owner)
    doc = _drop(owner)
    with_doc = _start(owner, docs=[doc["id"]])
    without = _start(owner, ask="Une question sans document")
    # the MCP server of a session is a process of its own: it knows the session by its env
    monkeypatch.setenv("SOKKAN_SESSION_USER", OWNER)
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "default")

    monkeypatch.setenv("SOKKAN_SESSION_ID", with_doc)
    out = mem.memory_write("client-lemania-brief", "Brief du client : 12 assistantes, 2 jours",
                           "Le brief demande Excel intermédiaire.")
    assert out["ok"] and out.get("quarantined") is True, out
    assert not (mdir / "client-lemania-brief.md").exists()
    q = quarantine.get("client-lemania-brief")
    assert q["provenance"]["reason"] == "session_documents" and q["provenance"]["session"] == with_doc
    index_memory.run_index(rebuild=True)
    assert "client-lemania-brief" not in [h.get("note_name") for h in mem.memory_search("brief Lémania", 8)]

    # a session nobody handed a document to writes as before (unchanged behaviour)
    monkeypatch.setenv("SOKKAN_SESSION_ID", without)
    out = mem.memory_write("decision-format-cours", "Les cours durent deux jours", "Décidé le 11.11.")
    assert out["ok"] and not out.get("quarantined"), out
    assert (mdir / "decision-format-cours.md").exists()

    # the file tools: refused for the session with a document, asked as usual for the other
    target = {"file_path": str(mdir / "glisse.md"), "content": "---\nname: glisse\n---\nx\n"}
    res = _Tool(agentchat.peek(with_doc), "Write", target).done()
    assert type(res).__name__ == "PermissionResultDeny" and "memory_write" in res.message
    auto = [r for r in _hitl(session_id=with_doc) if r["auto"]]
    assert [r["auto_reason"] for r in auto] == ["policy:documents_memory_write"]
    assert not (mdir / "glisse.md").exists()
    # approved by a person, the quarantined note enters the memory — the only way in
    quarantine.approve("client-lemania-brief", OWNER)
    assert (mdir / "client-lemania-brief.md").exists()


def test_a_work_with_documents_still_saves_its_deliverable_in_any_project(box):
    """The rule above is about the MEMORY directories. A project's own space — where its
    documents and deliverables live, `<data>/projects/<slug>/work` — is not one: the save of
    the deliverable reaches the person's card. (The first journey of the integration, on the
    real model, had the deliverable refused as « a memory file » in a project other than the
    default one: the agent then asked to keep its plan in the memory instead.)"""
    import agentchat
    import quarantine
    ana_mail, _n, project = PEOPLE[0]
    ana = box["people"][ana_mail]
    _consent(ana)
    doc = _drop(ana)
    sid = _start(ana, docs=[doc["id"]])
    sess = agentchat.peek(sid)
    assert sess._has_documents() and project != "default"
    target = _deliverable(box, project, sid)
    assert str(box["data"] / "projects" / project / "work") in target
    with ana.websocket_connect(f"/api/agent/ws/{sid}?view=simple") as ws:
        tool = _Tool(sess, "Write", {"file_path": target, "content": "# Plan de formation\n"}, ws)
        pid = tool.pid()                                     # a person is asked: not refused
        card = _until(ws, "approval", id=pid)
        assert card["kind"] == "write_draft" and card["draft"]["text"].startswith("# Plan")
        ws.send_json({"type": "permission", "id": pid, "decision": "allow"})
        _until(ws, "approval_resolved", id=pid)
        assert type(tool.done()).__name__ == "PermissionResultAllow"
    assert [r["decision"] for r in _hitl(session_id=sid)] == ["allow"]
    # the memory of THAT project, and the quarantine, stay closed to the file tools
    for path in (quarantine.memory_dir(project) / "glisse.md", quarantine.qdir(project) / "glisse.md",
                 box["data"] / "projects" / "qualite" / "memory" / "glisse.md"):
        res = _Tool(sess, "Write", {"file_path": str(path), "content": "x"}).done()
        assert type(res).__name__ == "PermissionResultDeny" and "memory_write" in res.message, path
    assert [r["auto_reason"] for r in _hitl(session_id=sid) if r["auto"]] == ["policy:documents_memory_write"] * 3
    for other in ("documents/x.md", "notes.md", "deliverables/autre/Plan.md"):
        assert agentchat._is_memory_path(str(box["data"] / "projects" / project / "work" / other)) is False


# ============ e. the batch review screen: every decision is journaled ========================
@pytest.fixture()
def ingest(tmp_path, monkeypatch):
    from docingest_helpers import FakeLLM, write_sources
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    for v in ("SOKKAN_DOC_INGEST_DIR", "SOKKAN_DOC_INGEST_CONFIG", "SOKKAN_MEMORY_QUARANTINE_DIR",
              "SOKKAN_DOC_INGEST_REVIEW_MIN_SAMPLE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SOKKAN_FEATURE_DOC_INGEST", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_AGENTS", "1")
    monkeypatch.setenv("SOKKAN_DOC_INGEST_CONCURRENCY", "1")
    import app as a
    import audit
    import auth
    import iam
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    iam.upsert_user("relecteur@x", "dev")
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    from docingest import config, runner, state
    state._ready.clear()
    docs = tmp_path / "docs"
    (docs / "pv").mkdir(parents=True)
    write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(docs), "allow": ["pv"]}])
    monkeypatch.setattr(auth, "resolve_email", lambda request: "relecteur@x")

    def flagging(doc, user):
        from docingest_helpers import default_script
        out = default_script(doc, user)
        if "séance 2 " in doc:
            out["decisions"][0]["quote"] = "une phrase qui n'est pas dans le document"
        return out

    def batch(n=4):
        for i in range(1, n + 1):
            (docs / "pv" / f"pv-{i:03d}.md").write_text(
                f"# PV de la séance {i}\n\nOrdre du jour de la séance {i} du comité.\n"
                f"DECISION: la séance {i} valide le plan de formation du trimestre {i}\n")
        p = runner.Pass(config.get("nas"), client=FakeLLM(flagging), memory_neighbours=lambda *x: []).run()
        assert p["status"] == "done", p
        return p["batch_id"]
    return {"c": TestClient(a.app), "batch": batch, "audit": audit, "memory": tmp_path / "memory"}


def test_every_decision_of_the_review_screen_is_journaled(ingest):
    """What components/DocIngestReview.tsx reads and sends, button by button."""
    c, audit = ingest["c"], ingest["audit"]
    bid = ingest["batch"](4)
    (b,) = c.get("/api/doc-ingest/batches").json()["batches"]
    assert b["id"] == bid and b["status"] == "pending" and b["source"] == "nas"
    assert b["counts"] == {"pending": 8}
    st = c.get(f"/api/doc-ingest/batches/{bid}").json()
    assert st["notes"] == 8 and st["by_status"] == {"pending": 8} and st["documents"] == 4
    assert st["flagged"] == 1 and st["flags"] == {"quote_not_found": 1}
    assert st["by_type"] == {"reference": 4, "decision": 4} and st["by_level"] == {"project": 8}
    assert st["viewed_by_you"] == 0 and st["review_min_sample"] >= 1
    # flagged notes come first in the sample, and opening it is recorded
    sample = c.get(f"/api/doc-ingest/batches/{bid}/sample?n=5").json()
    assert sample["notes"][0]["flags"] == ["quote_not_found"]
    assert not any(n["flags"] for n in sample["notes"][1:]) and sample["viewed_by_you"] == 5
    assert all(n["text"] and n["title"] and n["rel"] and n["level_id"] for n in sample["notes"])
    flagged, plain = sample["notes"][0]["name"], sample["notes"][1]["name"]
    # the four buttons
    assert c.post(f"/api/doc-ingest/batches/{bid}/notes/{plain}/reject",
                  json={"reason": "hors sujet"}).status_code == 200
    assert c.post(f"/api/doc-ingest/batches/{bid}/notes/{flagged}/approve").status_code == 200
    r = c.post(f"/api/doc-ingest/batches/{bid}/approve")
    assert r.status_code == 200 and r.json()["approved"] == 6, r.text
    bid2 = ingest["batch"](6)
    c.get(f"/api/doc-ingest/batches/{bid2}/sample?n=5")
    r = c.post(f"/api/doc-ingest/batches/{bid2}/reject", json={"reason": "mauvais dossier"})
    assert r.status_code == 200, r.text
    # the journal: one line per decision, with who and why — in audit.db AND in the ledger
    acts = [(e["action"], e["user"]) for e in audit.recent(200) if e["action"].startswith("doc_ingest.")]
    kinds = [a for a, _u in acts]
    for needed in ("doc_ingest.sample", "doc_ingest.reject", "doc_ingest.approve",
                   "doc_ingest.approve_batch", "doc_ingest.reject_batch"):
        assert needed in kinds, (needed, kinds)
    assert {u for _a, u in acts} == {"relecteur@x"}
    iam_admin = __import__("iam")
    iam_admin.upsert_user("relecteur@x", "admin")              # the decisions ledger is for maintainers
    rows = c.get("/api/doc-ingest/decisions").json()["decisions"]
    by = {r["action"]: r for r in rows}
    assert by["reject"]["reason"] == "hors sujet" and by["reject"]["note"] == plain
    assert by["reject_batch"]["reason"] == "mauvais dossier" and by["reject_batch"]["batch_id"] == bid2
    assert by["approve"]["note"] == flagged and by["approve_batch"]["batch_id"] == bid
    assert all(r["user"] == "relecteur@x" and r["ts"] for r in rows)
    # and what was approved — only that — is in the memory
    notes = sorted(p.name for p in ingest["memory"].glob("*.md"))
    file_of = lambda name: name.replace("-", "_") + ".md"  # noqa: E731 — a note's file name
    assert len(notes) == 7 and file_of(plain) not in notes and file_of(flagged) in notes


# ============ f. the appliance profile ======================================================
def _template() -> dict:
    out = {}
    for line in (ROOT / "deploy/appliance/env.appliance.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_the_appliance_profile_turns_the_pilot_features_on_with_what_they_need(monkeypatch):
    import features
    env = _template()
    assert env["SOKKAN_PROFILE_DEFAULT"] == "simple" and env["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"
    assert env["SOKKAN_AUTH_MODE"] == "local" and env["SOKKAN_DEFAULT_ROLE"] == "none"
    for k in list(os.environ):
        if k.startswith(("SOKKAN_", "CORTHEXIS_")) and k != "SOKKAN_DATA_DIR":
            monkeypatch.delenv(k)
    for k, v in env.items():
        if k.startswith(("SOKKAN_", "CORTHEXIS_", "CLAUDE_")) and "<" not in v:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(features, "_which", lambda b: None)     # the image has no tmux, no browser
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: object())   # extractors built in
    import localaccounts
    monkeypatch.setattr(localaccounts, "any_account", lambda: True)   # the installer creates them
    st = features.resolve()
    wanted = {"simple_profile": ("documents",), "documents": (), "study_mode": (),
              "doc_ingest": ("agents", "memory_quarantine"), "multi_project": (), "assistant": ()}
    for fid, needs in wanted.items():
        assert st[fid].enabled and not st[fid].problem, (fid, st[fid].reason)
        assert tuple(features.BY_ID[fid].requires) == needs, fid
        assert all(st[n].enabled for n in needs), (fid, needs)
    for off in ("tmux", "preview", "magnitude", "missions_link", "alerting", "agent_incidents"):
        assert not st[off].enabled, off
    # nothing asked for in the template is refused (a missing dependency would show here)
    asked = {p.id for p in features.problems()}
    assert not asked & set(wanted), asked
    import uiprefs
    assert uiprefs.default_profile() == "simple"


def test_the_appliance_profile_has_no_switch_without_its_dependency(monkeypatch):
    """Mutation of the profile: `simple_profile` without `documents` is reported, not half on."""
    import features
    for k in list(os.environ):
        if k.startswith("SOKKAN_FEATURE_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SOKKAN_FEATURE_SIMPLE_PROFILE", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_DOCUMENTS", "0")
    st = features.resolve()
    assert not st["simple_profile"].enabled and st["simple_profile"].problem


def test_the_installer_writes_the_pilot_lines_and_keeps_ingestion_local():
    sh = (ROOT / "scripts/install.sh").read_text()
    assert 'set_env SOKKAN_DOC_INGEST_LLM_URL "${SOKKAN_SHIP_URL%/}/v1"' in sh
    # no local model = no ingestion (never the Deep gateway: documents would leave the site)
    assert re.search(r'else set_env SOKKAN_FEATURE_DOC_INGEST 0', sh)
    rel = (ROOT / "scripts/release-artifacts.sh").read_text()
    assert "SOKKAN_DOC_INGEST=${SOKKAN_RELEASE_DOC_INGEST:-1}" in rel
    compose = (ROOT / "docker-compose.yml").read_text()
    api = compose.split("\n  api:")[1].split("\n  db:")[0]
    for var in _template():
        if var.startswith(("SOKKAN_FEATURE_", "SOKKAN_PROFILE_", "SOKKAN_DOC_INGEST_", "CLAUDE_CODE_",
                           "SOKKAN_STUDY_")):
            assert f"      {var}: " in api, f"{var} is in the appliance profile but never reaches the api"


def test_sessions_on_the_local_engine_get_medium_effort_and_the_study_headers(box, monkeypatch):
    """The appliance engine (SOKKAN_SHIP_URL → the local router): the CLI's default effort is
    refused by vLLM with a 400, and the router needs the study to hand it to the Deep gateway."""
    import llm
    import study
    monkeypatch.setenv("SOKKAN_SHIP_URL", "http://host.docker.internal:8004")
    ana = PEOPLE[0][0]
    box["people"][ana].post("/api/study/consent", json={"content_scenarios": []})
    env = llm.session_env(ana)
    assert env["ANTHROPIC_BASE_URL"] == "http://router:8096" and env["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"
    assert env["ANTHROPIC_CUSTOM_HEADERS"].split("\n") == [f"x-sokkan-study: {STUDY}", "x-sokkan-user: P1"]
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")           # the operator's choice wins
    assert llm.session_env(ana)["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "banana")        # … when it is a real level
    assert llm.session_env(ana)["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"
    monkeypatch.setenv("SOKKAN_FEATURE_STUDY_MODE", "0")
    study.reset_cache()
    assert "x-sokkan-study" not in llm.session_env(ana).get("ANTHROPIC_CUSTOM_HEADERS", "")


# ============ g. the contract with the inference gateway ====================================
# Inputs are NOT written from the instance's code: tests/fixtures/gateway_contract/ holds the
# `GET /usage` example of the gateway's own documentation (sokkan-cloud infer/pilote-etude.md
# § 4) and the four refusals as its app answers them (infer/gateway.py `_refuse`), written by
# tests/fixtures/gateway_contract/regenerate.py. When a checkout of sokkan-cloud is at hand
# (SOKKAN_CLOUD_SRC), the copy is compared with it.
# The refusals themselves (router, session, simple profile): tests/test_gateway_refusals.py.
CONTRACT = ROOT / "tests" / "fixtures" / "gateway_contract"


def _contract() -> dict:
    return json.loads((CONTRACT / "contract.json").read_text(encoding="utf-8"))


def _cloud() -> Path | None:
    for p in (os.environ.get("SOKKAN_CLOUD_SRC"), "/root/sokkan-cloud-wt-noretry"):
        if p and (Path(p) / "infer" / "pilote-etude.md").exists():
            return Path(p)
    return None


def test_the_frozen_contract_is_the_gateways_own_text():
    cloud = _cloud()
    if cloud is None:
        pytest.skip("no sokkan-cloud checkout (SOKKAN_CLOUD_SRC): the frozen copy is used as is")
    doc = (cloud / "infer" / "pilote-etude.md").read_text(encoding="utf-8")
    block = re.search(r"### `GET /usage`.*?```json\n(.*?)```", doc, re.S).group(1)
    assert json.loads(block) == _contract()["usage"]
    for h in _contract()["headers_sent"]:
        assert f"`{h}: <" in doc, h
    gw = (cloud / "infer" / "gateway.py").read_text(encoding="utf-8")
    for r in _contract()["refusals"]:
        assert f'_refuse({r["status"]}, "{r["type"]}", "{r["code"]}"' in gw, r["code"]
        for words in r["words_in_source"]:
            assert words in gw, (r["code"], words)
        assert re.search(rf'\|\s*{r["status"]}\s*\|\s*`{r["code"]}`', doc), r["code"]
    # since sokkan-cloud `study-no-retry` the gateway itself forbids the retry of these four
    # (a session that talks to it WITHOUT the local router waited 177 s on a 429) — and of
    # these four only: the tenant-wide 429 keeps the client's retry
    no_retry = _contract()["no_retry_header"]
    assert no_retry == "x-should-retry"
    for r in _contract()["refusals"]:
        assert r["headers"] == {_contract()["error_header"]: r["code"], no_retry: "false"}, r["code"]
    assert _contract()["tenant_cap"]["headers"] == {}
    assert f'NO_RETRY_HEADER = "{no_retry}"' in gw
    assert 'headers={ERR_HEADER: code, NO_RETRY_HEADER: "false"}' in gw
    assert f"`{no_retry}: false`" in doc


def test_usage_of_the_gateway_is_read_as_documented(box, monkeypatch):
    import llm
    import study
    usage = _contract()["usage"]
    monkeypatch.delenv("SOKKAN_STUDY_ID")
    seen = []
    monkeypatch.setattr(llm, "usage", lambda headers=None: (seen.append(headers), usage)[1])
    study.reset_cache()
    st = study.state(refresh=True)
    assert (st["active"], st["study_id"], st["audit_required"], st["source"]) == (
        True, usage["study"]["study_id"], True, "gateway")
    assert st["expires_at"] == usage["study"]["expires_at"] and st["notice"] is False
    assert st["limits"] == {"tokens_day": 6000000, "tokens_month": 120000000,
                            "deep_per_user_day": 20, "user_tokens_day": None}    # 0 = no ceiling
    assert st["used"] == {"tokens_day": 4960, "tokens_month": 4960}
    ana = PEOPLE[0][0]
    _consent(box["people"][ana])
    assert study.my_usage(ana) == {"tokens_day": 4960, "deep_day": 2}
    assert seen[-1] == {"x-sokkan-study": usage["study"]["study_id"], "x-sokkan-user": "P1"}
    # what the simple profile's gauge shows comes from the fields the gateway always served
    expired = json.loads(json.dumps(usage))
    expired["study"].update(active=False, expired=True)
    monkeypatch.setattr(llm, "usage", lambda headers=None: expired)
    study.reset_cache()
    st = study.state(refresh=True)
    assert st["active"] is False and st["study_id"] == usage["study"]["study_id"]


def test_on_an_appliance_the_study_is_read_from_the_deep_gateway(box, monkeypatch):
    """Sessions of an appliance talk to the local router; `GET /usage` goes to the Deep
    gateway with the instance token and the identity headers of the contract."""
    import httpx
    import llm
    import study
    usage = _contract()["usage"]
    monkeypatch.delenv("SOKKAN_STUDY_ID")
    monkeypatch.setenv("SOKKAN_SHIP_URL", "http://host.docker.internal:8004")
    calls = []

    class R:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return usage

    def get(url, headers=None, timeout=None):
        calls.append((url, dict(headers or {})))
        return R()
    monkeypatch.setattr(httpx, "get", get)
    assert llm.usage() is None                      # no Deep gateway configured: nothing is asked
    monkeypatch.setenv("SOKKAN_DEEP_URL", "https://infer.example/")
    monkeypatch.setenv("SOKKAN_DEEP_TOKEN", "sik_instance")
    study.reset_cache()
    st = study.state(refresh=True)
    assert st["active"] and st["source"] == "gateway" and st["study_id"] == usage["study"]["study_id"]
    assert calls[-1] == ("https://infer.example/usage", {"x-api-key": "sik_instance"})
    ana = PEOPLE[0][0]
    _consent(box["people"][ana])
    study.my_usage(ana)
    url, headers = calls[-1]
    assert headers == {"x-api-key": "sik_instance", "x-sokkan-study": usage["study"]["study_id"],
                       "x-sokkan-user": "P1"}
    assert "@" not in json.dumps(calls)             # no address ever leaves for the gateway


@pytest.mark.parametrize("code", ["study_expired", "study_mode_required", "user_daily_cap", "user_limit_reached"])
def test_a_refusal_of_the_gateway_is_readable_and_reaches_the_person(code):
    """The refusal as the gateway builds it, shown by Claude Code as « API Error: <status>
    <message> » — recognised by its words, said in French and in English."""
    import study
    r = next(x for x in _contract()["refusals"] if x["code"] == code)
    shown = f'API Error: {r["status"]} {r["message"]}'
    said = study.explain_refusal(shown)
    assert said and " — " in said, (code, said)
    assert study.explain_refusal(json.dumps(r["body"])) == said     # … and by its code
    assert study.explain_refusal("API Error: 500 upstream") is None


def test_the_local_router_carries_the_contract_to_the_deep_gateway(monkeypatch):
    """Appliance: the router between the sessions and the gateway hands over the study and the
    pseudonym, keeps an address on the box, and gives the study refusals back as they are.
    The gateway is a real socket (the fake upstream of test_shiprouter.py)."""
    import shiprouter
    from test_shiprouter import Fake
    deep = Fake("deep")
    for k in list(os.environ):
        if k.startswith(("SOKKAN_SHIP", "SOKKAN_DEEP", "SOKKAN_ESCALATION", "SOKKAN_ERROR_LANG")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SOKKAN_DEEP_URL", deep.url)
    monkeypatch.setenv("SOKKAN_DEEP_TOKEN", "sik_instance")
    try:
        c = TestClient(shiprouter.app)
        body = {"model": "sokkan-ship", "max_tokens": 16, "stream": True,
                "messages": [{"role": "user", "content": "bonjour"}]}
        study_h = {"x-sokkan-study": STUDY, "x-sokkan-user": "P2"}

        r = c.post("/v1/messages", json=body, headers=study_h)
        assert r.status_code == 200 and r.headers["x-sokkan-tier"] == "deep"
        up = deep.seen[-1]["headers"]
        assert up["x-sokkan-study"] == STUDY and up["x-sokkan-user"] == "P2" and up["x-api-key"] == "sik_instance"
        # outside a study the header is a person's address: it stays on the box
        c.post("/v1/messages", json=body, headers={"x-sokkan-user": PEOPLE[0][0]})
        assert "x-sokkan-user" not in deep.seen[-1]["headers"]
        # … and an address is never passed off as a pseudonym by adding the study header
        c.post("/v1/messages", json=body, headers={"x-sokkan-study": STUDY, "x-sokkan-user": PEOPLE[0][0]})
        assert "x-sokkan-user" not in deep.seen[-1]["headers"]
        assert deep.seen[-1]["headers"]["x-sokkan-study"] == STUDY
        assert "@" not in json.dumps([x["headers"] for x in deep.seen])

        for r in _contract()["refusals"]:
            deep.status, deep.body = r["status"], json.dumps(r["body"]).encode()
            out = c.post("/v1/messages", json=body, headers=study_h)
            assert out.status_code == r["status"], r["code"]
            assert out.json()["error"]["code"] == r["code"]
            assert out.json()["error"]["message"] == r["message"]
            assert out.headers["x-sokkan-error"] == r["code"]
            assert out.headers["x-should-retry"] == "false"   # the CLI would retry a 429 for minutes
        # a tenant-wide 429 (no study code) keeps the router's own plain-language message
        deep.status, deep.body = 429, json.dumps({"type": "error", "error": {
            "type": "rate_limit_error", "message": "daily inference cap reached for this environment"}}).encode()
        out = c.post("/v1/messages", json=body, headers=study_h)
        assert out.status_code == 429 and out.headers["x-sokkan-error"] == "rate_limited"
        assert shiprouter.study_refusal(429, "{}", "user_daily_cap") == "user_daily_cap"   # by its header
        assert shiprouter.study_refusal(500, json.dumps(_contract()["refusals"][0]["body"])) == ""
    finally:
        deep.close()
