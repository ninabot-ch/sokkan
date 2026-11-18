"""4.0 « the coworker knows the house » (audit coworker 17.11.2026, points 2, 8, 9):

* the organisation's profile, filled once by an administrator, in the head of every work
  (legacy: the system prompt; kernel: in front of the first turn), the IBAN only where the
  work needs it;
* continuity: a new work of a project sees the project's VALIDATED deliverables (list,
  summary, path), bounded, filtered by level;
* « remember for next time »: one action after a validation, through the quarantine.
"""
import asyncio

import pytest

import simple_fixtures as _fx

world = _fx.world


@pytest.fixture(autouse=True)
def _memory_dirs(tmp_path, monkeypatch):
    """Notes go to the test's folders, never to the operator's memory."""
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    import instance
    monkeypatch.setattr(instance, "STORE", tmp_path / "instance.json")
    monkeypatch.delenv("SOKKAN_ORG_NAME", raising=False)

# a fictitious but VALID Swiss IBAN (mod 97) and company number (mod 11)
IBAN = "CH93 0076 2011 6238 5295 7"
UID = "CHE-116.281.710"
PROFILE = {
    "name": "Morand Domotique", "legal_name": "Morand Domotique SA", "legal_form": "SA",
    "street": "Chemin des Ateliers 12", "postcode": "1023", "city": "Crissier", "country": "CH",
    "uid": UID, "vat": "CHE-116.281.710 TVA", "iban": IBAN, "bank": "Banque Exemple",
    "signatories": [{"name": "Marc Morand", "function": "Directeur général"}],
    "people": [{"initials": "TB", "name": "Thomas Bovet", "function": "Responsable commercial"},
               {"name": "Bob Martin", "function": "Assistant de direction", "email": "bob@x"}],
    "tone": "Sobre et chaleureux.",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- the profile ------------------------------------------------------------------------------
def test_the_profile_is_checked_and_the_iban_shown_to_admins_only(world):
    c = world["as"]("bob@x")
    assert c.put("/api/org/profile", json=PROFILE).status_code == 403
    a = world["as"]("admin@x")
    assert a.put("/api/org/profile", json={**PROFILE, "iban": "CH93 0076 2011 6238 5295 8"}
                 ).json()["detail"]["code"] == "iban_invalid"
    assert a.put("/api/org/profile", json={**PROFILE, "uid": "CHE-116.281.711"}
                 ).json()["detail"]["code"] == "uid_invalid"
    r = a.put("/api/org/profile", json=PROFILE)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["iban"] == IBAN and p["uid"] == UID and p["name"] == "Morand Domotique"
    assert p["people"][0]["initials"] == "TB" and p["people"][1]["initials"] == "BM"
    assert p["completeness"]["ready"] is True and "iban" in p["completeness"]["filled"]
    seen = world["as"]("bob@x").get("/api/org/profile").json()
    assert seen["iban"] == "CH93 •••• 2957" and seen["can_edit"] is False
    # the masked value sent back unchanged keeps the IBAN; the journal names fields, not values
    a = world["as"]("admin@x")
    assert a.put("/api/org/profile", json={"iban": "CH93 •••• 2957", "tone": "Direct."}).json()["iban"] == IBAN
    ev = world["audit"]("org.profile.update")
    assert ev and "iban" in ev[-1]["detail"] and "0076" not in str(ev)
    # the name lives where the instance keeps it (the onboarding step writes it there too)
    import instance
    assert instance.info()["org_name"] == "Morand Domotique"


def test_the_logo_is_an_image_and_only_an_image(world):
    a = world["as"]("admin@x")
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    put = lambda data, name: a.put("/api/org/logo", params={"name": name}, content=data,  # noqa: E731
                                   headers={"content-type": "application/octet-stream"})
    assert put(b"MZ....", "logo.png").status_code == 400
    assert put(b"<svg><script>x</script></svg>", "logo.svg").status_code == 400
    assert put(png, "logo.exe").status_code == 400
    assert put(png, "logo.png").status_code == 200
    r = world["as"]("bob@x").get("/api/org/logo")
    assert r.status_code == 200 and r.content == png and r.headers["content-type"] == "image/png"
    assert world["as"]("bob@x").delete("/api/org/logo").status_code == 403
    # 4.0.1: every member reads that there is a logo, with its own cache key (top of the Atelier)
    prof = world["as"]("bob@x").get("/api/org/profile").json()
    assert prof["logo"] is True and prof["logo_v"] > 0


def test_the_block_says_who_and_where_and_keeps_the_iban_for_payments(world):
    import orgprofile
    orgprofile.update(PROFILE, "admin@x")
    b = orgprofile.block(email="bob@x", display_name="Bob Martin",
                         ask="Rédige le procès-verbal du comité")
    assert "Morand Domotique SA" in b and "Chemin des Ateliers 12, 1023 Crissier, Suisse" in b
    assert "TB = Thomas Bovet — Responsable commercial" in b
    assert "« T. Bovet » is « Thomas Bovet »" in b
    assert "nLPD" in b and "NOT the GDPR" in b and "1'234.50 CHF" in b and "never « tu »" in b
    assert "You are working with: Bob Martin, Assistant de direction" in b
    assert "Marc Morand (Directeur général)" in b
    assert IBAN not in b and "not needed for this work" in b
    pay = orgprofile.block(email="bob@x", ask="Prépare les lettres de relance des factures")
    assert f"IBAN {IBAN}" in pay
    assert orgprofile.needs_bank(playbook="finance.invoice-reconciliation")
    assert orgprofile.block(email="x@y", ask="ok") == orgprofile.block(email="x@y", ask="ok")  # stable
    assert len(b) < 2400                                                       # ≈ 600 tokens


def test_an_empty_profile_adds_nothing(world):
    import orgprofile
    assert orgprofile.block(email="bob@x") == ""


# ---- continuity -------------------------------------------------------------------------------
def _work(sid, project="default", owner="bob@x", ask="travail"):
    import board
    import simple
    board.add_sdk_session(sid, "docs", title=ask, project=project, owner=owner)
    simple.add_work(sid, project, owner, ask, "", "fr")


def _deliverable(world, sid, name, text, validate_as="bob@x"):
    import simple
    folder = simple.deliverable_dir("default", sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    if validate_as:
        simple.validate("default", sid, name, validate_as)
    return folder / name


PV = """# Procès-verbal du comité de direction du 10.12.2026

## Décisions
- Budget v1 présenté le **14.01.2027**.

## Actions
| Qui | Quoi | Quand |
|---|---|---|
| TB | Besoins avant le 20.12 | 20.12.2026 |
"""
RAPPROCHEMENT = """# Rapprochement bancaire octobre

## Factures soldées
- F-2026-101 Les Cèdres : payée le 12.10 (8'120.50 CHF).
- F-2026-104 Chappuis : payée le 20.10.

## Partiel
- F-2026-107 Régie du Lac : 12'350.00 restants.

## À identifier
- Virement de 1'250.00 du 28.10.
"""


def test_a_new_work_sees_the_projects_validated_work(world):
    import househead
    s1, s2, s3, new = "1" * 32, "2" * 32, "3" * 32, "9" * 32
    for s in (s1, s2, s3, new):
        _work(s)
    _deliverable(world, s1, "PV comité 10.12.md", PV)
    _deliverable(world, s2, "Rapprochement octobre.md", RAPPROCHEMENT)
    _deliverable(world, s3, "Brouillon.md", "# Brouillon\n\nPas validé.", validate_as=None)
    changed = _deliverable(world, s3, "Changé.md", "# Changé\n\nv1")
    changed.write_text("# Changé\n\nv2", encoding="utf-8")       # validated, then changed
    b = househead.continuity_block("default", sid=new, email="bob@x")
    assert "« Procès-verbal du comité de direction du 10.12.2026 »" in b
    assert "« Rapprochement bancaire octobre »" in b and "Factures soldées" in b
    assert "by Bob Martin" in b and "Full text: " in b
    assert "Brouillon" not in b and "Changé" not in b
    assert "an invoice it shows as paid" in b
    # its own deliverables are not « previous work »
    assert "Rapprochement" not in househead.continuity_block("default", sid=s2, email="bob@x")
    # an empty project says nothing
    assert househead.continuity_block("radio", sid=new, email="bob@x") == ""


def test_continuity_is_bounded(world):
    import househead
    for i in range(10):
        sid = f"{i:x}" * 32
        _work(sid)
        _deliverable(world, sid, f"Doc {i}.md", f"# Document {i}\n\n" + "Un texte assez long. " * 200)
    b = househead.continuity_block("default", sid="f" * 32, email="bob@x")
    assert b.count("Full text:") == househead.MAX_ITEMS
    assert len(b) <= househead.MAX_CHARS + 900          # the closing rule is outside the bound
    assert "Document 9" in b and "Document 0" not in b  # the newest first


def test_continuity_hides_what_the_person_may_not_read(world, monkeypatch):
    import househead
    s1, new = "1" * 32, "9" * 32
    _work(s1)
    _work(new)
    _deliverable(world, s1, "PV.md", PV)
    monkeypatch.setattr(househead, "_level_of", lambda sid: 3)       # read a confidential document
    monkeypatch.setattr(househead, "_cap", lambda sid, email, project: 2)
    assert househead.continuity_block("default", sid=new, email="bob@x") == ""
    monkeypatch.setattr(househead, "_cap", lambda sid, email, project: 3)
    assert "Procès-verbal" in househead.continuity_block("default", sid=new, email="bob@x")


def test_an_atelier_work_starts_with_the_house_in_its_head(world):
    import agentchat
    import orgprofile
    orgprofile.update(PROFILE, "admin@x")
    s1, new = "1" * 32, "9" * 32
    _work(s1)
    _work(new, ask="Prépare l'ordre du jour du comité du 14.01")
    _deliverable(world, s1, "PV.md", PV)
    s = agentchat.AgentSession(new, cwd=str(world["workspace"]), user="bob@x")
    spec = _run(s._run_spec())
    assert [b.source for b in spec.head] == ["organisation", "project"]
    assert "working with" not in spec.head[0].text          # one head per project: cached once
    assert "Morand Domotique SA" in spec.head[0].text and "Procès-verbal" in spec.head[1].text
    # an ordinary (Cockpit) session gets no head
    import board
    board.add_sdk_session("e" * 32, "docs", title="dev", project="default", owner="bob@x")
    s2 = agentchat.AgentSession("e" * 32, cwd=str(world["workspace"]), user="bob@x")
    assert _run(s2._run_spec()).head == ()


# ---- delivery by the engines ----------------------------------------------------------------
def test_the_legacy_engine_puts_the_head_in_its_system_prompt():
    from harness import contract, legacy
    if "system_prompt" not in legacy._OPTION_FIELDS:
        pytest.skip("SDK without system_prompt")
    head = (contract.ContextBlock(text="<sokkan-house>X</sokkan-house>", source="organisation"),
            contract.ContextBlock(text="<sokkan-project>Y</sokkan-project>", source="project"))
    kw = legacy.LegacyHarness()._options(contract.RunSpec(run_id="s", cwd="/w", head=head))
    assert kw["system_prompt"] == "<sokkan-house>X</sokkan-house>\n\n<sokkan-project>Y</sokkan-project>"
    assert "system_prompt" not in legacy.LegacyHarness()._options(contract.RunSpec(run_id="s", cwd="/w"))
    argv = legacy.build_argv(legacy.ClaudeAgentOptions(**kw))
    i = argv.index("--system-prompt")
    assert argv[i + 1].startswith("<sokkan-house>X")
    assert contract.RunSpec(run_id="s", cwd="/w", head=head).describe()["head"] == ["organisation", "project"]


def test_the_kernel_gets_the_head_with_the_first_turn_only(tmp_path):
    from harness import contract, kernel
    sent = []
    head = (contract.ContextBlock(text="<sokkan-house>\nMorand\n</sokkan-house>", source="organisation"),)

    async def go(resume=None):
        h = kernel.KernelHarness()
        h._spec = contract.RunSpec(run_id="r", cwd=str(tmp_path), head=head, resume=resume)
        h._thread = "t"

        async def post(turn, text):
            sent.append(text)
        h._post_chat = post
        await h.send("Bonjour")
        await asyncio.sleep(0)
        h._turn = None
        await h.send("Encore")
        await asyncio.sleep(0)
        return h
    h = _run(go())
    assert sent[0].startswith("<sokkan-brief>\n<sokkan-house>") and sent[0].endswith("Bonjour")
    assert sent[1] == "Encore"
    evs = h._mail
    seen = []
    while not evs.empty():
        seen.append(evs.get_nowait())
    assert any(isinstance(e, contract.ContextInjected) and e.channel == "head" for e in seen)
    # a resumed thread already has it
    sent.clear()
    _run(go(resume="t"))
    assert sent[0] == "Bonjour"
    # read back, the person's words only
    items = kernel._history_items([{"role": "user", "content": "<sokkan-brief>\n<sokkan-house>\nMorand\n"
                                    "</sokkan-house>\n</sokkan-brief>\n\nBonjour"}], str(tmp_path))
    assert items[0].text == "Bonjour"


# ---- remember for next time -------------------------------------------------------------------
def test_remember_after_validation_in_one_action(world):
    import documents  # noqa: F401
    import learn
    import quarantine
    c = world["as"]("bob@x")
    sid = "1" * 32
    _work(sid, ask="PV du comité")
    _deliverable(world, sid, "PV.md", PV, validate_as=None)
    learn.note_message(sid, "bob@x", "Merci")
    learn.note_message(sid, "bob@x", "Corrige : c'est Thomas Bovet, pas Théo, et l'Hôtel Bellerive.")
    base = f"/api/simple/sessions/{sid}/deliverables/PV.md/learn"
    assert c.get(base).json()["state"] == "not_validated"
    assert c.post(f"/api/simple/sessions/{sid}/deliverables/PV.md/validate").status_code == 200
    offer = c.get(base).json()
    assert offer["state"] == "offer" and 1 <= len(offer["facts"]) <= 3
    texts = [f["text"] for f in offer["facts"]]
    assert texts[0] == ("Pour « Procès-verbal du comité de direction » : Corrige : c'est Thomas "
                        "Bovet, pas Théo, et l'Hôtel Bellerive.")
    assert all("Merci" not in t for t in texts)
    # somebody else cannot keep it for them
    assert world["as"]("dave@x").post(base, json={"facts": offer["facts"]}).status_code == 403
    r = world["as"]("bob@x").post(base, json={"facts": [offer["facts"][0]]})
    assert r.status_code == 200, r.text
    notes = r.json()["notes"]
    assert len(notes) == 1 and notes[0].startswith("retenu-proces-verbal-du-comite-de-direction-")
    md = quarantine.memory_dir("default") / f"{notes[0]}.md"
    body = md.read_text(encoding="utf-8")
    assert "Thomas Bovet" in body and "approved_by" in body and "remembered by bob@x" in body
    assert not (quarantine.qdir("default") / f"{notes[0]}.md").exists()
    assert world["audit"]("memory.quarantine.approve") and world["audit"]("memory.learn")
    assert c.get(base).json()["state"] == "retained"


def test_not_now_is_remembered_and_nothing_is_written(world):
    import quarantine
    c = world["as"]("bob@x")
    sid = "2" * 32
    _work(sid)
    _deliverable(world, sid, "Rapprochement.md", RAPPROCHEMENT)
    base = f"/api/simple/sessions/{sid}/deliverables/Rapprochement.md/learn"
    assert c.get(base).json()["state"] == "offer"         # the structure (3 sections)
    assert c.post(base, json={"dismiss": True}).json()["state"] == "dismissed"
    assert c.get(base).json()["state"] == "dismissed"
    assert not list(quarantine.memory_dir("default").glob("retenu-*.md")) \
        if quarantine.memory_dir("default").is_dir() else True
    assert c.post(base, json={"facts": []}).status_code in (400, 409)


def test_kind_of_drops_the_date_and_number():
    import learn
    assert learn.kind_of("Procès-verbal du comité de direction du 10.12.2026") == \
        "Procès-verbal du comité de direction"
    assert learn.kind_of("Offre n° 2026-14 Résidence Bellerive") == "Offre"
    assert learn.kind_of("Relances", "Lettres de relance") == "Lettres de relance"


def test_the_org_dictionaries_hold_the_same_keys_none_empty():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "frontend" / "lib" / "simple" / "dictOrg.ts").read_text(encoding="utf-8")
    fr_part, en_part = src.split("export const orgEn", 1)
    rx = re.compile(r'^\s*"([\w.]+)":\s*"(.*)",\s*$', re.M)
    fr, en = dict(rx.findall(fr_part)), dict(rx.findall(en_part))
    assert fr and set(fr) == set(en)
    assert all(v.strip() for v in (*fr.values(), *en.values()))
    used = set()
    for f in (Path(__file__).resolve().parent.parent / "frontend" / "components" / "simple").glob("*.tsx"):
        used |= set(re.findall(r'\borgT\(\s*"([\w.]+)"', f.read_text(encoding="utf-8")))
    assert used and used <= set(fr), sorted(used - set(fr))


def test_the_first_message_says_who_the_work_is_for(world):
    import orgprofile
    orgprofile.update(PROFILE, "admin@x")
    c = world["as"]("bob@x")
    r = c.post("/api/simple/start", json={"ask": "Prépare l'ordre du jour", "lang": "fr"})
    assert r.status_code == 200, r.text
    sid, text = world["turns"][-1]
    assert sid == r.json()["session_id"]
    assert text.startswith("<sokkan-brief>You are working with: Bob Martin, Assistant de direction.")
    import plain
    assert plain.strip_brief(text).startswith("Prépare l'ordre du jour")
