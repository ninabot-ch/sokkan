"""4.0 job kits of SOKKAN Atelier (« Postes », backend/roles.py, backend/roles/*.json): the
catalog holds together in both languages, a person chooses their jobs (and an admin gives a
default), the home shows the job's templates first, a project is set up for a job (level,
starter notes, its own job), a start carries the job's mission and deliverable brief, a job
that keeps its data local refuses Claude, and a table deliverable is previewed and exported
to Excel. The UI half is tests/nav/simple_roles_test.ts."""
import copy
import io
import json
import zipfile

import pytest

import simple_fixtures as _fx

world = _fx.world

REQUIRED_JOBS = {"executive-assistant", "hr", "finance", "sales", "customer-support",
                 "marketing", "legal", "project-manager", "training"}


# ---- the catalog -------------------------------------------------------------------------------
def test_the_catalog_is_valid_and_covers_the_nine_jobs():
    import roles
    data = roles.load(force=True)
    assert REQUIRED_JOBS <= set(data["roles"])
    assert roles.check(data) == []
    for r in data["roles"].values():
        assert 6 <= len(r["templates"]) <= 10, r["id"]
        assert 3 <= len(r["notes"]) <= 5, r["id"]
        assert len(r["guardrails"]) >= 2, r["id"]
        for t in r["templates"]:
            assert t["format"] in ("docx", "xlsx", "md")
            for lang in ("fr", "en"):
                assert t["i18n"][lang]["checks"], (r["id"], t["id"])


def test_every_text_exists_in_french_and_in_english():
    import roles
    for r in roles.load()["roles"].values():
        assert set(r["i18n"]["fr"]) == set(r["i18n"]["en"])
        for t in r["templates"]:
            assert set(t["i18n"]["fr"]) == set(t["i18n"]["en"])
            assert len(t["i18n"]["fr"]["checks"]) == len(t["i18n"]["en"]["checks"])
        for n in r["notes"]:
            assert "[À compléter" in n["i18n"]["fr"]["body"] and "[To fill" in n["i18n"]["en"]["body"]
    fr = {t["id"] for t in roles.catalog("fr")[0]["templates"]}
    en = {t["id"] for t in roles.catalog("en")[0]["templates"]}
    assert fr == en


def test_jobs_that_handle_personal_or_secret_data_never_go_to_claude():
    import roles
    for rid in ("hr", "legal"):
        r = roles.get(rid)
        assert r["level"] == "confidential" and r["claude"] is False
    assert roles.get("finance")["claude"] is False
    # the validator refuses a confidential job that would allow Claude
    data = copy.deepcopy(roles.load())
    data["roles"]["hr"]["claude"] = True
    assert any("cannot allow Claude" in p for p in roles.check(data))


@pytest.mark.parametrize("mutate,expect", [
    (lambda r: r["templates"].pop(), None),     # still ≥ 6 for most jobs: checked below
    (lambda r: r["templates"][0]["i18n"]["en"].pop("hint"), "en.hint missing"),
    (lambda r: r["templates"][0].update(mission="Too short {subject}"), "mission too short"),
    (lambda r: r["templates"][0].update(brief="nope"), "brief must be one of"),
    (lambda r: r["templates"][0].update(mission=r["templates"][0]["mission"] + " {other}"), "stray brace"),
    (lambda r: r["notes"][0]["i18n"]["fr"].update(body="Rien à remplir."), "has no blank"),
    (lambda r: r["templates"][0]["i18n"]["fr"]["checks"].append("de trop"), "fr and"),
])
def test_the_validator_names_what_is_wrong(mutate, expect):
    import roles
    data = copy.deepcopy(roles.load())
    mutate(data["roles"]["executive-assistant"])
    problems = roles.check(data)
    if expect:
        assert any(expect in p for p in problems), problems


def test_a_broken_kit_file_in_the_operator_folder_is_refused_with_its_name(tmp_path, monkeypatch):
    import roles
    (tmp_path / "bad.json").write_text(json.dumps({"id": "bad", "level": "team", "claude": True}))
    monkeypatch.setenv("SOKKAN_ROLES_DIR", str(tmp_path))
    with pytest.raises(roles.CatalogError, match="bad.json"):
        roles.load(force=True)
    monkeypatch.delenv("SOKKAN_ROLES_DIR")
    roles.load(force=True)


def test_an_operator_adds_a_job_by_dropping_a_file(tmp_path, monkeypatch):
    import roles
    job = copy.deepcopy(roles.get("executive-assistant"))
    job.pop("_file", None)
    job["id"] = "reception"
    for n in job["notes"]:
        n["name"] = "reception-" + n["name"]
    (tmp_path / "reception.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("SOKKAN_ROLES_DIR", str(tmp_path))
    try:
        assert "reception" in roles.ids()
    finally:
        monkeypatch.delenv("SOKKAN_ROLES_DIR")
        roles.load(force=True)


# ---- the mission and the deliverable brief ------------------------------------------------------
def test_a_kit_template_resolves_as_a_playbook_with_its_brief_and_rules():
    import playbooks
    import roles
    for r in roles.load()["roles"].values():
        for t in r["templates"]:
            tid = f"{r['id']}.{t['id']}"
            pb = playbooks.get(tid)
            assert pb and pb["audience"] == "simple" and pb["i18n"]["fr"]["label"]
            prompt, tag = playbooks.render(tid, "SUBJECT-XYZ")
            assert "SUBJECT-XYZ" in prompt and tag == "docs"
            assert "WHAT A GOOD DELIVERABLE OF THIS KIND LOOKS LIKE" in prompt
            assert "RULES OF THIS JOB" in prompt
            for g in r["guardrails"]:
                assert g["agent"].strip() in prompt
            if t["format"] == "xlsx":
                assert "format: xlsx" in prompt and "FORMULA" in prompt and "in your message to the person" in prompt
                assert "HOW A GOOD COLLEAGUE WORKS" in prompt
            else:
                assert "Points to confirm" in prompt
    # the technical spawn form is unchanged, an unknown kit id is unknown
    assert playbooks.get("hr.nope") is None and playbooks.render("hr.nope") is None
    assert "hr.job-posting" not in [p["id"] for p in playbooks.catalog()]


def test_the_brief_of_each_deliverable_type_is_complete():
    import roles
    for bid, b in roles.load()["briefs"].items():
        text = roles.deliverable_brief(bid, b["format"])
        for part in ("Structure", "Length", "Sources", "To check"):
            assert part in text, (bid, part)


# ---- choosing a job ------------------------------------------------------------------------------
def test_a_person_chooses_their_jobs_once_and_can_say_later(world):
    c = world["as"]("bob@x")
    assert c.get("/api/me/prefs").json()["roles"] is None
    h = c.get("/api/simple/home", params={"lang": "fr"}).json()
    assert h["ask_roles"] is True and h["roles"] == [] and h["project_role"] == ""
    r = c.put("/api/me/prefs", json={"roles": ["sales", "executive-assistant", "sales"]})
    assert r.status_code == 200 and r.json()["roles"] == ["executive-assistant", "sales"]  # catalog order
    assert c.put("/api/me/prefs", json={"roles": ["astronaut"]}).status_code == 400
    h = c.get("/api/simple/home", params={"lang": "fr"}).json()
    assert h["ask_roles"] is False
    assert [j["id"] for j in h["roles"]] == ["executive-assistant", "sales"]
    ids = [p["id"] for p in h["playbooks"]]
    assert ids[0].startswith("executive-assistant.") and any(i.startswith("sales.") for i in ids)
    assert ids[-4:] == ["training-plan", "document-summary", "client-proposal", "assessment-quiz"]
    kit = h["playbooks"][0]
    assert kit["attach"] and kit["produces"] and kit["checks"] and kit["format"] in ("docx", "xlsx", "md")
    assert kit["role_label"] == "Assistant·e de direction"
    # « later » = an empty list: not asked again, the four general templates only
    c.put("/api/me/prefs", json={"roles": []})
    h = c.get("/api/simple/home").json()
    assert h["ask_roles"] is False and len(h["playbooks"]) == 4


def test_an_admin_gives_a_person_a_default_job(world):
    a = world["as"]("admin@x")
    r = a.put("/api/admin/prefs/dave@x", json={"roles": ["customer-support"]})
    assert r.status_code == 200 and r.json()["roles"] == ["customer-support"]
    assert any(e["action"] == "roles.set" and e["resource"] == "dave@x" for e in world["audit"]("roles.set"))
    people = a.get("/api/simple/roles/people").json()["people"]
    assert next(p for p in people if p["email"] == "dave@x")["roles"] == ["customer-support"]
    d = world["as"]("dave@x")
    h = d.get("/api/simple/home").json()
    assert h["ask_roles"] is False and h["playbooks"][0]["role"] == "customer-support"
    assert world["as"]("bob@x").put("/api/admin/prefs/dave@x", json={"roles": []}).status_code == 403
    assert world["as"]("bob@x").get("/api/simple/roles/people").status_code == 403
    a = world["as"]("admin@x")       # one client: the identity is the last one set
    assert a.put("/api/admin/prefs/dave@x", json={"roles": ["nope"]}).status_code == 400
    # the profile route is unchanged: profile alone still works, nothing at all is a 400
    assert a.put("/api/admin/prefs/dave@x", json={"profile": "simple"}).status_code == 200
    assert a.put("/api/admin/prefs/dave@x", json={}).status_code == 400


def test_the_catalog_route_says_what_the_caller_may_set_up(world):
    b = world["as"]("bob@x").get("/api/simple/roles", params={"lang": "en"}).json()
    assert REQUIRED_JOBS <= {r["id"] for r in b["roles"]}
    hr = next(r for r in b["roles"] if r["id"] == "hr")
    assert hr["label"] == "Human resources" and hr["claude"] is False and hr["guardrails"]
    assert b["can_create_project"] is False
    a = world["as"]("admin@x").get("/api/simple/roles").json()
    assert a["can_create_project"] is True and a["roles"][0]["label"]


# ---- a project for a job -------------------------------------------------------------------------
def test_an_admin_sets_up_a_confidential_project_for_hr_with_its_starter_notes(world):
    import projects
    import roles
    import store_backend
    a = world["as"]("admin@x")
    r = a.post("/api/simple/roles/hr/setup", json={"project": "", "lang": "fr"})
    assert r.status_code == 200, r.text
    out = r.json()
    slug = out["project"]
    assert out["created"] is True and out["level"] == "confidential"
    assert projects.get(slug)["level"] == 3
    notes = store_backend.memory_dir_for(slug)
    names = sorted(p.name for p in notes.glob("*.md"))
    assert len(names) == len(roles.get("hr")["notes"]) == len(out["notes"])
    text = (notes / names[0]).read_text(encoding="utf-8")
    assert "status: to-fill" in text and "[À compléter" in text
    # the admin works in it, its home leads with the HR templates, the job is theirs now
    h = world["as"]("admin@x", slug).get("/api/simple/home", params={"lang": "fr"}).json()
    assert h["project_role"] == "hr" and h["playbooks"][0]["role"] == "hr"
    assert "hr" in a.get("/api/me/prefs").json()["roles"]
    # a second HR project gets its own slug; a note the team filled is never overwritten
    (notes / names[0]).write_text("filled by the team", encoding="utf-8")
    again = a.post("/api/simple/roles/hr/setup", json={"project": slug}).json()
    assert again["notes"] == [] and (notes / names[0]).read_text() == "filled by the team"
    second = a.post("/api/simple/roles/hr/setup", json={"project": ""}).json()
    assert second["project"] != slug


def test_setting_up_a_job_takes_the_right_role(world):
    b = world["as"]("bob@x")
    r = b.post("/api/simple/roles/sales/setup", json={"project": ""})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "admin"
    # a dev of a project is not its admin
    r = world["as"]("alice@x").post("/api/simple/roles/sales/setup", json={"project": "radio"})
    assert r.status_code == 403
    assert world["as"]("eve@x").post("/api/simple/roles/sales/setup", json={"project": "nope"}).status_code == 404
    assert world["as"]("admin@x").post("/api/simple/roles/astronaut/setup", json={}).status_code == 404


def test_a_project_for_a_job_needs_the_multi_project_feature_not_a_second_project(world, monkeypatch):
    import projects
    projects.update("radio", archived=True)          # one work project left: still allowed
    a = world["as"]("admin@x")
    assert a.get("/api/simple/roles").json()["can_create_project"] is True
    assert a.post("/api/simple/roles/sales/setup", json={"project": ""}).status_code == 200
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "0")
    r = a.post("/api/simple/roles/sales/setup", json={"project": ""})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "single_project"
    assert a.post("/api/simple/roles/sales/setup", json={"project": "default"}).status_code == 200


def test_a_team_job_does_not_lower_a_project(world):
    import projects
    projects.set_level("radio", 3)
    projects.grant("radio", "user", "admin@x", "admin")
    r = world["as"]("admin@x").post("/api/simple/roles/marketing/setup", json={"project": "radio"})
    assert r.status_code == 200 and projects.get("radio")["level"] == 3


# ---- a start ---------------------------------------------------------------------------------------
def test_a_start_from_a_kit_template_carries_the_mission_and_the_brief(world):
    c = world["as"]("bob@x")
    r = c.post("/api/simple/start", json={"ask": "PV du comité du 12.11", "lang": "fr",
                                          "playbook": "executive-assistant.meeting-minutes"})
    assert r.status_code == 200, r.text
    assert r.json()["title"].startswith("Procès-verbal de séance — ")
    sid, text = world["turns"][-1]
    assert sid == r.json()["session_id"]
    assert "Write the minutes of a meeting" in text and "(minutes)" in text
    assert "RULES OF THIS JOB" in text and "PV du comité du 12.11" in text


def test_a_reminder_or_an_invitation_never_promises_a_send_the_connectors_cannot_do(world, monkeypatch):
    """4.0 connectors × job kits: a template that touches the mailbox / calendar (« uses ») tells
    the agent and the person what the connectors really allow — off = the person sends it."""
    import roles
    assert set(roles.uses_of("finance.payment-reminder")) == {"mail"}
    assert set(roles.uses_of("training.invitation")) == {"mail", "calendar"}
    assert roles.uses_of("executive-assistant.meeting-minutes") == []
    c = world["as"]("bob@x")
    r = c.post("/api/simple/start", json={"ask": "Relance facture 2026-118", "lang": "fr",
                                          "playbook": "finance.payment-reminder"})
    assert r.status_code == 200, r.text
    text = world["turns"][-1][1]
    assert "MAILBOX AND CALENDAR" in text and "mailbox is NOT connected" in text
    assert "calendar is NOT connected" not in text                         # only what the template uses
    # connected with sending on: a draft, sent after the person's card
    from connectors import service as conn
    monkeypatch.setattr(conn, "capabilities", lambda e, p="default", s="": {"mail": "act", "calendar": "read",
                                                                            "files": "off"})
    c.post("/api/simple/start", json={"ask": "Convocation 3.12", "lang": "fr", "playbook": "training.invitation"})
    text = world["turns"][-1][1]
    assert "mail_propose_send" in text and "never « sent »" in text and "calendar is readable" in text
    # the home says it too, per template
    c.put("/api/me/prefs", json={"roles": ["training"]})
    tpl = {x["id"]: x for x in c.get("/api/simple/home").json()["playbooks"]}
    assert tpl["training.invitation"]["connectors"] == {"mail": "act", "calendar": "read"}
    assert "connectors" not in tpl["training.quiz"]
    assert roles.connector_rules("executive-assistant.meeting-minutes", {}) == ""
    bad = {"roles": {"x": {"templates": [{"id": "a", "uses": ["fax"]}]}}, "briefs": {}}
    assert any("uses must list" in p for p in roles.check(bad))


def test_a_job_that_keeps_its_data_local_refuses_claude(world):
    import roles
    c = world["as"]("bob@x")
    for rid in ("hr", "legal", "finance"):
        tid = f"{rid}.{roles.get(rid)['templates'][0]['id']}"
        r = c.post("/api/simple/start", json={"ask": "x", "lang": "fr", "playbook": tid, "tier": "claude"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_role", tid
    # and in a project set up for HR, even a general template stays local
    a = world["as"]("admin@x")
    slug = a.post("/api/simple/roles/hr/setup", json={"project": ""}).json()["project"]
    r = world["as"]("admin@x", slug).post("/api/simple/start", json={
        "ask": "x", "lang": "en", "playbook": "document-summary", "tier": "claude"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_role"
    assert "no Claude" in r.json()["detail"]["message"]


# ---- a table deliverable ----------------------------------------------------------------------------
def test_a_table_deliverable_is_previewed_as_a_table_and_downloaded_as_excel(world):
    """Integration n°2: a kit's Excel deliverable goes through `outputs` (formats) — a Markdown
    source with `format: xlsx`, totals as formulas SOKKAN computes; a « .csv » still works."""
    import openpyxl
    import simple
    sid = _fx.DEFAULT_SID
    d = simple.deliverable_dir("default", sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Suivi des actions.md").write_text(
        "---\nformat: xlsx\n---\n# Suivi des actions\n\n| Libellé | Montant (CHF) |\n|---|---|\n"
        "| Réserver la salle, 2 jours | 1250.50 |\n| Traiteur | 800 |\n| Total | =SUM(ABOVE) |\n",
        encoding="utf-8")
    (d / "Ancien tableau.csv").write_text("Type;Montant\naction;1'250.50\n", encoding="utf-8")
    c = world["as"]("bob@x")
    lst = {x["name"]: x for x in c.get(f"/api/simple/sessions/{sid}/deliverables").json()["deliverables"]}
    assert lst["Suivi des actions.md"]["formats"][0] == "xlsx"
    prev = c.get(f"/api/simple/sessions/{sid}/deliverables/Suivi des actions.md").json()
    assert "2'050.50" in prev["text"] and "format: xlsx" not in prev["text"]     # computed, Swiss format
    x = c.get(f"/api/simple/sessions/{sid}/deliverables/Suivi des actions.md/download", params={"format": "xlsx"})
    assert x.status_code == 200 and x.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml")
    ws = openpyxl.load_workbook(io.BytesIO(x.content)).active
    vals = [c_.value for row in ws.iter_rows() for c_ in row]
    assert "Réserver la salle, 2 jours" in vals and any(isinstance(v, str) and v.startswith("=SUM") for v in vals)
    old = c.get(f"/api/simple/sessions/{sid}/deliverables/Ancien tableau.csv/download", params={"format": "xlsx"})
    assert old.status_code == 200


def test_csv_with_semicolons_and_quotes():
    import outputs
    rows = outputs.csv_rows('A;B\n"x;y";2\n\n')
    assert rows == [["A", "B"], ["x;y", "2"]]
    assert "1'250.50" in outputs.preview("t.csv", "a;b\nc;1250.50\n")


def test_starter_notes_are_valid_memory_files(tmp_path):
    import roles
    names = roles.seed_notes("finance", tmp_path, "en")
    assert names and all((tmp_path / f"{n.replace('-', '_')}.md").is_file() for n in names)
    for p in tmp_path.glob("*.md"):
        head = p.read_text(encoding="utf-8").split("---")[1]
        assert "name:" in head and "description: >-" in head and "status: to-fill" in head
    assert roles.seed_notes("finance", tmp_path, "en") == []
