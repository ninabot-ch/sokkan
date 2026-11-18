"""4.0 « fiabilité » — the quality gate before the approval card (backend/factcheck.py,
backend/qualitygate.py, the gate in agentchat.py), Swiss law and usage by default, the formal
address. The faulty deliverables are the real ones of the persona audit of 17.11.2026 (a
fictitious SME, 27B of the box), replayed as fixtures: tests/reliability/fixtures/."""
import asyncio
import http.server
import json
import threading
from pathlib import Path

import factcheck
import pytest
import qualitygate

FX = Path(__file__).resolve().parent / "reliability" / "fixtures"


def _sources(*names):
    return "\n".join((FX / "sources" / f"{n}.txt").read_text(encoding="utf-8") for n in names)


ALL = [p.stem for p in (FX / "sources").glob("*.txt")]


def _case(name):
    return ((FX / f"{name}.md").read_text(encoding="utf-8"),
            (FX / f"{name}.ask.txt").read_text(encoding="utf-8"))


def _kinds(findings, severity="block"):
    return sorted({f["kind"] for f in findings if f["severity"] == severity})


# ---- the cases of the audit ----------------------------------------------------------------
def test_the_offer_with_three_totals_is_caught():
    text, ask = _case("offre-v1")
    f = factcheck.check(text, primary=_sources(*ALL), ask=ask, lang="fr", country="CH")
    totals = [x for x in f if x["kind"] == "totals"]
    assert totals, f
    a = next(x for x in totals if "Variante A" in x["fr"])
    assert {round(v) for v in a["data"]["values"]} == {310700, 119880, 93960}
    assert "3 totaux différents" in a["fr"] and "310'700" in a["fr"]


def test_the_corrected_offer_passes():
    text, ask = _case("offre-v2")
    f = factcheck.check(text, primary=_sources(*ALL), ask=ask, lang="fr", country="CH")
    assert factcheck.blocking(f) == []


def test_an_invented_first_name_and_a_copied_one_are_caught():
    text, ask = _case("pv-10-12")
    prior = (FX / "pv-12-11-valide.md").read_text(encoding="utf-8")
    f = factcheck.check(text, primary=_sources(*ALL), secondary=prior, ask=ask, lang="fr", country="CH")
    said = " ".join(x["fr"] for x in factcheck.blocking(f))
    assert "« Théo Bovet »" in said and "« T. Bovet »" in said
    # « résidence Bellerive » for the « Hôtel Bellerive » of the sources
    assert any(x["kind"] == "org" and "Hôtel Bellerive" in x["fr"] for x in f)
    # « Claire Favre »: « C. Favre » and « Claire » are both in the sources — not flagged
    assert "Claire Favre" not in said


def test_a_name_found_only_in_an_earlier_deliverable_is_second_hand():
    f = factcheck.check("Présents : Théo Bovet, Direction commerciale.\n",
                        primary="présents: TB, CF", secondary="Théo Bovet, Direction commerciale",
                        lang="fr", country="CH")
    assert [x["kind"] for x in f] == ["name_second_hand"] and f[0]["severity"] == "warn"


def test_a_name_the_sources_spell_otherwise_is_caught():
    f = factcheck.check("Responsable : Théo Bovet\n", primary="T. Bovet est Thomas Bovet, directeur commercial",
                        lang="fr", country="CH")
    assert f and f[0]["kind"] == "name" and "Thomas Bovet" in f[0]["fr"]


def test_the_work_certificate_reservation_and_invented_reason_are_caught():
    text, ask = _case("certificats")
    f = factcheck.check(text, primary=_sources(*ALL), ask=ask, lang="fr", country="CH")
    kinds = _kinds(f)
    assert "certificate_coded" in kinds and "certificate_leaving" in kinds
    coded = next(x for x in f if x["kind"] == "certificate_coded")
    assert "parfois" in coded["fr"] and "330a CO" in coded["fr"]


def test_a_reason_for_leaving_given_by_the_documents_is_allowed():
    text = ("# Certificat de travail\n\nMme Monnier a travaillé chez nous du 01.03.2020 au 31.12.2026. "
            "Son départ fait suite à sa volonté de reprendre des études.\n")
    f = factcheck.check(text, primary="Dans sa lettre de démission, elle souhaite que le motif de son départ figure.",
                        ask="certificat de travail complet", lang="fr", country="CH")
    assert "certificate_leaving" not in _kinds(f)


def test_a_swiss_register_under_the_gdpr_is_caught():
    text, ask = _case("registre-rgpd")
    f = factcheck.check(text, primary="", ask=ask, lang="fr", country="CH")
    eu = next(x for x in f if x["kind"] == "law_eu")
    assert "RGPD" in eu["data"]["terms"] and "nLPD" in eu["fr"]


def test_the_gdpr_is_fine_when_the_request_is_about_the_eu():
    f = factcheck.check("Selon l'art. 30 RGPD, le registre…", ask="Notre registre RGPD pour la filiale de Lyon",
                        lang="fr", country="CH")
    assert "law_eu" not in _kinds(f)


def test_wrong_and_unsourced_nlpd_articles_are_caught():
    text, ask = _case("registre-corrige")
    f = factcheck.check(text, primary="", ask=ask, lang="fr", country="CH")
    wrong = [x for x in f if x["kind"] == "law_article"]
    said = " ".join(x["fr"] for x in wrong)
    assert "art. 10 nLPD" in said            # the adviser is art. 10, not art. 26
    assert "art. 8 nLPD" in said             # data security is art. 8, not art. 7
    unsourced = next(x for x in f if x["kind"] == "law_unsourced")
    assert "à vérifier par un juriste" in unsourced["fr"]


def test_an_article_flagged_to_be_checked_or_given_by_a_source_passes():
    f = factcheck.check("Durée : selon l'art. 958f CO.\nMotif : art. 336 CO (à vérifier par un juriste).\n"
                        "Le registre (art. 12 nLPD) est tenu à jour.\nArt. 321 CO.\n",
                        primary="Le contrat cite l'art. 321 CO.", lang="fr", country="CH")
    assert factcheck.blocking(f) == []


def test_an_invented_nlpd_article_is_caught():
    f = factcheck.check("Base légale (art. 15bis nLPD).\n", lang="fr", country="CH")
    assert any(x["kind"] == "law_article" and "n'existe pas" in x["fr"] for x in f)


# ---- figures -------------------------------------------------------------------------------
def test_quantity_times_unit_price_and_table_total():
    md = ("| Réf. | Désignation | Qté | Prix unit. CHF | Montant CHF |\n|---|---|---|---|---|\n"
          "| A1 | Actionneur | 24 | 420 | 10'080 |\n| B2 | Poussoir | 48 | 165 | 7'290 |\n"
          "| | Total | | | 17'370 |\n")
    f = factcheck.check(md, lang="fr", country="CH")
    kinds = _kinds(f)
    assert "line" in kinds                     # 48 × 165 = 7'920, not 7'290
    assert "sum" not in kinds                  # the total is the sum of what is written


def test_a_total_that_is_not_the_sum_of_its_lines():
    md = "| Poste | Montant |\n|---|---|\n| Matériel | 1'000 |\n| Pose | 500 |\n| Total | 1'600 |\n"
    f = factcheck.check(md, lang="fr", country="CH")
    s = next(x for x in f if x["kind"] == "sum")
    assert s["data"]["expected"] == 1500 and s["data"]["found"] == 1600


def test_a_subtotal_then_vat_then_total_is_understood():
    md = ("| Poste | Montant CHF |\n|---|---|\n| Matériel | 1'000.00 |\n| Pose | 500.00 |\n"
          "| Sous-total HT | 1'500.00 |\n| TVA 8,1 % | 121.50 |\n| Total TTC | 1'621.50 |\n")
    assert factcheck.blocking(factcheck.check(md, lang="fr", country="CH")) == []


def test_wrong_vat_and_the_old_swiss_rate():
    md = "Total HT : CHF 1'000.00\nTVA 7,7 % : CHF 77.00\nTotal TTC : CHF 1'087.00\n"
    kinds = _kinds(factcheck.check(md, lang="fr", country="CH"))
    assert "vat_rate" in kinds and "vat_total" in kinds


def test_a_weekday_that_does_not_match_its_date():
    f = factcheck.check("Prochain comité : mercredi 14.01.2027 à 9h00.\n", lang="fr", country="CH")
    assert f and f[0]["kind"] == "weekday" and "jeudi" in f[0]["fr"]
    assert factcheck.check("Prochain comité : vendredi 14 janvier 2027.\n", lang="fr", country="CH") != []
    assert factcheck.check("Prochain comité : jeudi 14.01.2027.\n", lang="fr", country="CH") == []


def test_swiss_numbers_are_read():
    assert factcheck.parse_number("310'700") == 310700
    assert factcheck.parse_number("8 120,50") == 8120.5
    assert factcheck.parse_number("31.2k") == 31200
    assert factcheck.swiss(8120.5) == "8'120.50" and factcheck.swiss(93960) == "93'960"


def test_a_broken_csv_table_is_caught():
    csv = "Client;Montant;Statut\nRégie du Lac;12'350;partiel\nCèdres;0;soldé; payé le 3.11\n"
    f = factcheck.check(csv, name="Suivi.csv", lang="fr", country="CH")
    assert f and f[0]["kind"] == "csv_fields" and f[0]["data"]["rows"] == [3]


# ---- tone and form -------------------------------------------------------------------------
def test_tu_amounts_and_quotes_are_style_findings():
    f = factcheck.check('Tu peux nous rappeler. Montant : 8 120,50 CHF, le "devis" du 17/11/2026.\n',
                        lang="fr", country="CH", form="vous")
    kinds = {x["kind"]: x["severity"] for x in f}
    assert kinds == {"tone_tu": "warn", "format_amount": "style", "format_date": "style", "typo_quotes": "style"}
    assert factcheck.check("Tu peux nous rappeler.\n", lang="fr", country="CH", form="tu") == []


def test_the_brief_carries_swiss_defaults_and_the_formal_address(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    import simple
    b = simple.brief(ask="Établis le registre des traitements", lang="fr", project="p1", sid="d" * 32)
    for must in ("« vous »", "nLPD", "RS 235.1", "330a CO", "à vérifier par un juriste", "8'120.50",
                 "17.11.2026", "« T. Bovet » stays « T. Bovet »", "« … »"):
        assert must in b, must
    assert b.rstrip().endswith("is in French, with « vous ».</sokkan-brief>")
    monkeypatch.setenv("SOKKAN_ORG_ADDRESS_FORM", "tu")
    monkeypatch.setenv("SOKKAN_ORG_COUNTRY", "FR")
    b2 = simple.brief(ask="x", lang="fr", project="p1", sid="d" * 32)
    assert "never « tu »" not in b2 and "nLPD" not in b2


# ---- the gate ------------------------------------------------------------------------------
class _Reviewer(http.server.BaseHTTPRequestHandler):
    answers: list = []
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Reviewer.seen.append(body)
        content = _Reviewer.answers.pop(0) if _Reviewer.answers else '{"verdict":"ok","defects":[]}'
        out = json.dumps({"choices": [{"message": {"content": content}}],
                          "usage": {"prompt_tokens": 3000, "completion_tokens": 400}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture()
def reviewer(monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Reviewer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    monkeypatch.setenv("SOKKAN_REVIEW_URL", url)
    monkeypatch.setenv("SOKKAN_REVIEW_MODEL", "strong-model")
    monkeypatch.setenv("SOKKAN_REVIEW_LOCAL_URL", url)
    monkeypatch.setenv("SOKKAN_REVIEW_LOCAL_MODEL", "local-model")
    _Reviewer.answers, _Reviewer.seen = [], []
    yield _Reviewer
    srv.shutdown()


def test_a_confidential_project_is_never_sent_to_the_strong_reviewer(monkeypatch, reviewer):
    assert qualitygate.reviewer_for(1)["route"] == "strong"
    assert qualitygate.reviewer_for(2)["route"] == "strong"
    assert qualitygate.reviewer_for(3)["route"] == "local"
    assert qualitygate.reviewer_for(None)["route"] == "local"        # unknown = confidential
    monkeypatch.setenv("SOKKAN_REVIEW_REMOTE_MAX_LEVEL", "1")
    assert qualitygate.reviewer_for(2)["route"] == "local"
    monkeypatch.delenv("SOKKAN_REVIEW_LOCAL_URL")
    assert qualitygate.reviewer_for(3) is None


def test_the_review_finds_defects_and_the_card_says_what_remains(reviewer, monkeypatch):
    monkeypatch.setenv("SOKKAN_QUALITY_REWRITE", "on")
    reviewer.answers = ['```json\n{"verdict":"fix","defects":[{"where":"Total","problem":"119\'880 au lieu de 93\'960",'
                        '"fix":"93\'960"}],"verified":["prix du tarif"],"to_check":["nombre de lots"]}\n```']
    text, ask = _case("offre-v1")
    res = qualitygate.review(text=text, name="Offre.md", ask=ask, mission="Une offre en deux variantes",
                             sources=[("tarif", _sources("tarif-2026"))], lang="fr", level=1)
    assert res["needs_fix"] and res["route"] == "strong" and res["tokens_in"] == 3000
    sent = reviewer.seen[0]
    assert sent["model"] == "strong-model" and "SOURCE « tarif »" in sent["messages"][1]["content"]
    msg = qualitygate.agent_message(res)
    assert "3 totaux différents" in msg and "119'880 au lieu de 93'960" in msg
    assert msg.rstrip().endswith(factcheck.AGENT_MARK)
    card = qualitygate.card(res, corrected=False)
    assert not card["ok"] and card["reviewed_by"] == "strong" and card["to_check"] == ["nombre de lots"]


def test_the_corrected_version_is_rechecked_against_the_first_defects(reviewer):
    reviewer.answers = ['{"verdict":"ok","defects":[],"verified":["totaux recalculés"],"to_check":[]}']
    text, ask = _case("offre-v2")
    prev = {"defects": [{"where": "Total", "problem": "trois totaux"}]}
    res = qualitygate.review(text=text, name="Offre.md", ask=ask, sources=[], lang="fr", level=1, previous=prev)
    assert not res["needs_fix"]
    assert "A FIRST REVIEW FOUND THESE DEFECTS" in reviewer.seen[0]["messages"][1]["content"]
    card = qualitygate.card(res, corrected=True)
    assert card["ok"] and card["corrected"] and card["verified"] == ["totaux recalculés"]


def test_an_unreachable_reviewer_leaves_the_deterministic_pass(monkeypatch):
    monkeypatch.setenv("SOKKAN_REVIEW_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("SOKKAN_REVIEW_MODEL", "x")
    res = qualitygate.review(text="# Note\n\nRien à signaler.\n", name="n.md", level=0)
    assert res["error"] and not res["needs_fix"]
    assert qualitygate.card(res, corrected=False)["review_failed"]


def test_the_content_of_an_edit_is_the_file_with_its_replacement(tmp_path):
    p = tmp_path / "Offre.md"
    p.write_text("# Offre\n\nTotal : 119'880\n", encoding="utf-8")
    assert qualitygate.content_after("Edit", {"old_string": "119'880", "new_string": "93'960"}, str(p)) \
        == "# Offre\n\nTotal : 93'960\n"
    assert qualitygate.content_after("write_file", {"path": str(p), "content": "# A"}, str(p)) == "# A"
    assert qualitygate.content_after("Edit", {"old_string": "absent", "new_string": "x"}, str(p)) is None


# ---- in the session (both engines go through agentchat.gate) ------------------------------
def _session(monkeypatch, tmp_path):
    import agentchat
    s = agentchat.AgentSession.__new__(agentchat.AgentSession)
    s.sid, s.user, s.mode, s.policy, s.sandboxed, s.cwd = "e" * 32, "a@x", "default", None, None, str(tmp_path)
    s._perms, s._questions, s.redact_values, s._turn_model = {}, {}, [], ""
    s._pending = {}
    s._simple_work = True
    s._approved_saves, s._declined_saves = {}, {}
    s._open_gates = set()
    s.events = []
    s._emit = lambda ev: s.events.append(ev)
    journal = []
    monkeypatch.setattr(agentchat.hitl_journal, "requested", lambda *a, **k: None)
    monkeypatch.setattr(agentchat.hitl_journal, "decided", lambda *a, **k: None)
    monkeypatch.setattr(agentchat.hitl_journal, "automatic", lambda *a, **k: journal.append(k.get("reason")))
    monkeypatch.setattr(s, "_arm_hitl_notify", lambda *a: None, raising=False)
    monkeypatch.setattr(s, "_set_waiting", lambda *a: None, raising=False)
    monkeypatch.setattr(s, "_has_documents", lambda: False, raising=False)
    monkeypatch.setattr(qualitygate, "work_context", lambda sid, project: {
        "ask": _case("offre-v1")[1], "lang": "fr", "mission": "", "level": 1, "secondary": "",
        "sources": [("tarif", _sources("tarif-2026", "brief-client-residence-tilleuls-2"))]})
    monkeypatch.setenv("SOKKAN_REVIEW", "on")
    monkeypatch.delenv("SOKKAN_REVIEW_URL", raising=False)
    monkeypatch.delenv("SOKKAN_REVIEW_LOCAL_URL", raising=False)
    return s, journal


def test_the_session_sends_the_offer_back_once_then_shows_the_card(monkeypatch, tmp_path):
    from harness_helpers import ask
    s, journal = _session(monkeypatch, tmp_path)
    path = str(tmp_path / "deliverables" / s.sid / "Offre.md")
    v1, _ = _case("offre-v1")
    v2, _ = _case("offre-v2")

    async def turn():
        r1 = await ask(s, "Write", {"file_path": path, "content": v1})      # sent back, no card
        fut = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": v2}))
        while not s._perms:
            await asyncio.sleep(0.01)
        s.resolve_permission(next(iter(s._perms)), {"decision": "allow"})
        return r1, await fut

    r1, r2 = asyncio.run(turn())
    assert r1.denied and "3 totaux différents" in r1.message and factcheck.AGENT_MARK in r1.message
    assert journal == ["policy:quality_gate"]
    perms = [e for e in s.events if e["type"] == "permission"]
    assert len(perms) == 1 and perms[0]["check"]["corrected"] and perms[0]["check"]["ok"]
    assert r2.allowed


def test_a_defect_that_remains_is_on_the_card_never_a_second_bounce(monkeypatch, tmp_path):
    from harness_helpers import ask
    s, journal = _session(monkeypatch, tmp_path)
    path = str(tmp_path / "deliverables" / s.sid / "Offre.md")
    v1, _ = _case("offre-v1")

    async def turn():
        await ask(s, "Write", {"file_path": path, "content": v1})
        fut = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": v1}))
        while not s._perms:
            await asyncio.sleep(0.01)
        s.resolve_permission(next(iter(s._perms)), {"decision": "allow"})
        return await fut

    assert asyncio.run(turn()).allowed
    card = next(e for e in s.events if e["type"] == "permission")["check"]
    assert not card["ok"] and any("totaux différents" in x for x in card["remaining"])


def test_an_engine_that_drops_the_refusal_text_gets_the_defects_in_a_turn(monkeypatch, tmp_path):
    from harness_helpers import ask
    s, _ = _session(monkeypatch, tmp_path)

    class Kernelish:
        deny_text_reaches_model = False
    s.harness = Kernelish()
    path = str(tmp_path / "deliverables" / s.sid / "Offre.md")
    r = asyncio.run(ask(s, "Write", {"file_path": path, "content": _case("offre-v1")[0]}))
    assert r.denied and s._qg_carry and "3 totaux différents" in s._qg_carry


def test_the_thread_reads_the_gate_s_refusal_as_a_rule_not_a_failure():
    import plain
    assert factcheck.AGENT_MARK in plain.POLICY_DENIED


def test_the_deliverable_carries_the_report_of_its_own_text(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_SIMPLE_DB", str(tmp_path / "simple.db"))
    import importlib
    import hashlib
    import simple
    importlib.reload(simple)
    sid = "f" * 32
    d = simple.deliverable_dir("p1", sid)
    d.mkdir(parents=True)
    (d / "Offre.md").write_text("# Offre\n", encoding="utf-8")
    simple.note_check(sid, "Offre.md", hashlib.sha256(b"# Offre\n").hexdigest(),
                      {"ok": False, "remaining": ["x"]})
    assert simple.list_deliverables("p1", sid)[0]["check"]["remaining"] == ["x"]
    (d / "Offre.md").write_text("# Offre modifiée\n", encoding="utf-8")
    assert simple.list_deliverables("p1", sid)[0]["check"] is None


# ---- with the native formats (backend/outputs.py of seven-seas-formats) ---------------------
def test_a_total_cited_in_the_text_is_compared_with_the_one_sokkan_computed():
    pytest.importorskip("outputs")
    md = ("# Offre\n\n| Poste | Montant CHF |\n|---|---|\n| Matériel | 53'520.00 |\n"
          "| Programmation | 15'600.00 |\n| Main d'oeuvre | 24'840.00 |\n| **Total HT** | **=SUM(ABOVE)** |\n"
          "| Remise | =[Inconnu]*2 |\n\nTotal HT : CHF 119'880.00.\n")
    f = factcheck.check(md, lang="fr", country="CH", name="Offre.md")
    said = " ".join(x["fr"] for x in factcheck.blocking(f))
    assert "le tableau donne 93'960, le texte dit 119'880" in said
    assert "ne comprend pas" in said


def test_a_bare_total_in_the_text_matching_its_table_passes():
    md = ("| Poste | Montant |\n|---|---|\n| A | 1'000 |\n| B | 500 |\n| Total HT | 1'500 |\n\n"
          "Total HT : CHF 1'500.\n")
    assert factcheck.blocking(factcheck.check(md, lang="fr", country="CH")) == []
    two = md + "\n| Poste | Montant |\n|---|---|\n| C | 10 |\n| D | 20 |\n| Total HT | 30 |\n"
    assert factcheck.blocking(factcheck.check(two, lang="fr", country="CH")) == []


# ---- the attached documents are read before the first save (banc n°5, A4) -----------------
def test_read_calls_of_both_engines_count():
    assert qualitygate.is_read("Read") and qualitygate.is_read("read_file")
    assert qualitygate.is_read("anything", "file_read") and not qualitygate.is_read("Write", "file_write")
    docs = [{"id": "a1b2c3d4e5f60718", "name": "Devis 2026-117.txt"}]
    assert qualitygate.unread(docs, []) == ["Devis 2026-117.txt"]
    assert qualitygate.unread(docs, ["/data/p/documents/a1b2c3d4e5f60718/Devis 2026-117.txt.txt"]) == []
    assert qualitygate.unread(docs, ["/workspace/documents/x/Devis 2026-117.txt"]) == []      # kernel alias
    msg = qualitygate.read_first_message(["Devis 2026-117.txt"], "read_file")
    assert "read_file tool" in msg and "Read tool" not in msg and factcheck.AGENT_MARK in msg


def test_a_save_without_reading_the_attached_quote_is_sent_back_once(monkeypatch, tmp_path):
    from harness_helpers import ask
    import documents
    s, journal = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(documents, "of_session", lambda sid: [{"id": "a1b2c3d4e5f60718", "name": "Devis 2026-117.txt"}])
    path = str(tmp_path / "deliverables" / s.sid / "Relance.md")

    async def turn():
        r1 = await ask(s, "Write", {"file_path": path, "content": "# Relance\n\nVotre devis 2026-099…\n"})
        fut = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# Relance\n"}))
        while not s._perms:
            await asyncio.sleep(0.01)
        s.resolve_permission(next(iter(s._perms)), {"decision": "allow"})
        return r1, await fut

    r1, r2 = asyncio.run(turn())
    assert r1.denied and "you did not read « Devis 2026-117.txt »" in r1.message
    assert journal[0] == "policy:read_documents_first" and r2.allowed      # once, never a loop


def test_a_save_after_reading_goes_to_the_gate(monkeypatch, tmp_path):
    from harness_helpers import ask
    import documents
    from harness import contract
    s, journal = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(documents, "of_session", lambda sid: [{"id": "a1b2c3d4e5f60718", "name": "Devis 2026-117.txt"}])
    s._note_read(contract.ToolCalled(turn_id="t", call_id="c", name="read_file",
                                     input={"path": "/workspace/documents/a1b2c3d4e5f60718/Devis 2026-117.txt.txt"},
                                     classification="file_read"))
    path = str(tmp_path / "deliverables" / s.sid / "Relance.md")

    async def turn():
        fut = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# Relance\n"}))
        while not s._perms:
            await asyncio.sleep(0.01)
        s.resolve_permission(next(iter(s._perms)), {"decision": "allow"})
        return await fut
    assert asyncio.run(turn()).allowed and "policy:read_documents_first" not in journal


def test_the_brief_names_the_engine_s_read_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    import simple
    b = simple.brief(ask="x", lang="fr", project="p1", sid="d" * 32, read_tool="read_file",
                     docs=[])
    assert "Read tool" not in b


def test_a_total_excl_vat_and_a_total_incl_vat_of_one_variant_are_two_totals():
    # seen on a real run of the offer (27B): the gate had flagged 116'400 / 125'828.40
    md = ("**Total HT variante A : 116'400.00 CHF**\n**Total TTC variante A : 125'828.40 CHF**\n"
          "**Total HT variante B : 95'040.00 CHF**\n**Total TTC variante B : 102'738.24 CHF**\n")
    assert factcheck.blocking(factcheck.check(md, lang="fr", country="CH")) == []


def test_a_filtered_total_and_a_label_are_not_flagged():
    # seen on real runs: a total of the open deals only, « Portée Google » read as a person
    md = ("| Affaire | Statut | Montant |\n|---|---|---|\n| A | ouverte | 100 |\n| B | gagnée | 50 |\n"
          "| C | ouverte | 30 |\n| Total (affaires ouvertes) | | 130 |\n\nPortée Google : 12'000 vues.\n")
    f = factcheck.check(md, primary="Annonces Google, portée 12'000", lang="fr", country="CH")
    assert factcheck.blocking(f) == []


def test_by_default_a_defect_found_only_by_the_review_goes_to_the_card_not_back_to_the_agent(reviewer, monkeypatch):
    monkeypatch.delenv("SOKKAN_QUALITY_REWRITE", raising=False)
    reviewer.answers = ['{"verdict":"fix","defects":[{"where":"Objet","problem":"date fausse"}]}']
    res = qualitygate.review(text="# Note\n\nRien.\n", name="n.md", sources=[], lang="fr", level=1)
    assert res["defects"] and not res["needs_fix"]
    assert not qualitygate.card(res, corrected=False)["ok"]
    text, ask = _case("offre-v1")          # a deterministic finding still sends it back
    reviewer.answers = ['{"verdict":"ok","defects":[]}']
    assert qualitygate.review(text=text, name="o.md", ask=ask, sources=[], lang="fr", level=1)["needs_fix"]


# ---- 20.11: (a) tables built by SOKKAN, (b) delivery message = file, (c) appraisal sourced ----
def test_a_csv_with_an_unquoted_separator_is_rebuilt_by_sokkan():
    pytest.importorskip("outputs")
    hand = "Client;Montant;Note\nRégie du Lac;12350;partiel; reste à payer\nCèdres;0;soldé\n"
    built, err = qualitygate.build_table("Suivi.csv", hand)
    assert not err and built.startswith("﻿")
    import csv
    import io
    rows = list(csv.reader(io.StringIO(built.lstrip("﻿")), delimiter=";"))
    assert all(len(r) == 3 for r in rows) and rows[1][2] == "partiel; reste à payer"


def test_rows_given_as_markdown_or_json_become_a_csv():
    pytest.importorskip("outputs")
    md = "| Client | Montant |\n|---|---|\n| A; B | 10 |\n| C | 20 |\n"
    built, err = qualitygate.build_table("t.csv", md)
    assert not err and '"A; B";10' in built
    js = '[{"Client": "A", "Montant": 10}, {"Client": "C", "Montant": 20}]'
    built, err = qualitygate.build_table("t.csv", js)
    assert not err and built.lstrip("﻿").splitlines()[0] == "Client;Montant"


def test_a_table_that_cannot_be_rebuilt_gets_one_precise_error():
    pytest.importorskip("outputs")
    built, err = qualitygate.build_table("t.csv", "A;B;C\n1;2\n")
    assert built is None and "Row 2 has 2 cells but the header has 3" in err
    src = "---\nformat: xlsx\n---\n# Suivi\n\n## Factures\n\n| N° | Montant |\n|---|---|\n| 1 | 10 | 3 |\n"
    built, err = qualitygate.build_table("Suivi.md", src)
    assert built is None and "« Factures »" in err and "3 cells but the header has 2" in err
    assert factcheck.AGENT_MARK in qualitygate.table_message(err)


def test_a_false_delivery_message_is_caught_and_rewritten_from_the_file():
    pytest.importorskip("outputs")
    csvt = ("Ticket;Priorité;À vérifier\n4811;P2;oui\n4812;P1;\n4813;P3;oui\n4815;P1;non\n")
    bad = "J'ai trié les 5 lignes ; 3 lignes à vérifier. Le total est de CHF 93'030."
    problems = qualitygate.check_message(bad, "Tri.csv", csvt)
    assert any("5 lignes" in p and "4" in p for p in problems)
    assert any("3 lignes à vérifier" in p for p in problems)
    assert any("93'030" in p for p in problems)
    assert qualitygate.check_message("J'ai trié les 4 lignes ; 2 lignes à vérifier.", "Tri.csv", csvt) == []
    msg = qualitygate.factual_message("Tri.csv", csvt, "Tri des tickets")
    assert "4 lignes" in msg and "2 marquées à vérifier" in msg


def test_line_counts_are_read_table_by_table():
    pytest.importorskip("outputs")
    doc = ("# Suivi\n\n| Décision | À vérifier |\n|---|---|\n| D1 | oui |\n| D2 | |\n| D3 | |\n\n"
           "## Résumé\n\n| Indicateur | Valeur |\n|---|---|\n| Décisions | 3 |\n| Actions | 8 |\n")
    assert qualitygate.check_message("Le tableau compte 3 lignes, dont 1 ligne à vérifier.", "s.md", doc) == []
    assert qualitygate.check_message("Le fichier compte 5 lignes.", "s.md", doc) == []      # both tables
    assert qualitygate.check_message("Le tableau compte 9 lignes.", "s.md", doc) != []
    assert "tableau principal compte 3 lignes" in qualitygate.factual_message("s.md", doc)


def test_counted_points_to_check_and_computed_totals():
    pytest.importorskip("outputs")
    doc = ("# Rapport\n\n| Poste | Montant |\n|---|---|\n| A | 100 |\n| B | 200 |\n| Total | =SUM(ABOVE) |\n\n"
           "## Points à vérifier\n\n- un\n- deux\n")
    assert qualitygate.check_message("Total : CHF 300. 2 points à vérifier.", "r.md", doc) == []
    assert qualitygate.check_message("Total : CHF 400 ; 3 points à vérifier.", "r.md", doc) != []


def test_an_appraisal_absent_from_this_work_s_documents_is_flagged():
    cert = ("# Certificat de travail\n\nMme Monnier a travaillé du 01.03.2021 au 31.12.2026. Son travail "
            "était excellent et son comportement exemplaire. Elle est fiable.\n")
    notes = "Évaluation : travail fiable, prestations très bonnes, s'est efforcée sur les délais."
    old_pv = "Mme Monnier, au comportement exemplaire selon l'ancien PV"
    f = factcheck.check(cert, primary=notes + "\n" + old_pv, attached=notes,
                        ask="certificat de travail de Claire Monnier", lang="fr", country="CH")
    a = next(x for x in f if x["kind"] == "certificate_appraisal")
    assert "excellent" in a["fr"] and "exemplaire" in a["fr"] and "fiable" not in a["data"]["words"]


def test_a_table_written_on_one_line_is_refused_and_never_described_as_empty():
    pytest.importorskip("outputs")
    glued = ("# Rapprochement\n\n| N° | Tiers | Montant | | F-1 | A | 10 | | F-2 | B | 20 | | F-3 | C | 30 | "
             "| | Total | =SUM(ABOVE) |\n\n## Points à confirmer\n")
    built, err = qualitygate.build_table("r.md", glued)
    assert built is None and "ONE line" in err
    # the delivery message is never rewritten from a file SOKKAN could not read
    assert qualitygate.check_message("J'ai rapproché les 9 lignes.", "r.md", glued) == []


def _table(cols: int, rows: int = 3, empty: bool = True) -> str:
    head = "| " + " | ".join(f"Col {i + 1}" for i in range(cols)) + " |"
    sep = "|" + "|".join("---" for _ in range(cols)) + "|"
    body = ["| " + " | ".join(("" if empty and j == 2 else f"v{r}{j}") for j in range(cols)) + " |"
            for r in range(rows)]
    return "\n".join([head, sep, *body])


@pytest.mark.parametrize("cols", [5, 6, 7, 8])
def test_a_wide_markdown_table_on_several_lines_is_not_refused(cols):
    """4.0.1: a table of 5 columns or more was refused as « written on one line » — the old
    pattern's blank spanned the header, the separator and the first row."""
    pytest.importorskip("outputs")
    text = f"# Offre\n\nClient : Exemple SA\n\n{_table(cols)}\n\n| | | | | **Total** | 1 |\n"
    built, err = qualitygate.build_table("offre.md", text)
    assert err == "" and built == text
    assert qualitygate.glued_table_line(text) == 0
    # same with no empty cell and with an aligned separator
    t2 = _table(cols, empty=False).replace("---", ":---:")
    assert qualitygate.glued_table_line(t2) == 0


@pytest.mark.parametrize("cols", [5, 8])
def test_a_really_glued_wide_table_is_still_refused(cols):
    pytest.importorskip("outputs")
    lines = _table(cols, rows=3).split("\n")
    glued = " ".join([lines[0], *lines[2:]])            # header + rows on one line, no separator
    built, err = qualitygate.build_table("t.md", f"# T\n\n{glued}\n")
    assert built is None and "ONE line" in err
    # rows glued after a proper header and separator
    rows_glued = "\n".join([lines[0], lines[1], " ".join(lines[2:])])
    assert qualitygate.glued_table_line(rows_glued) == 3


def test_the_card_says_what_the_agent_corrected_in_words():
    """4.0.1: « L'agent a corrigé ce que la vérification a trouvé » said THAT, never WHAT."""
    prev = {"findings": [factcheck._f("sum", "block", "« Total » : la somme des lignes donne 6'600.00, "
                                      "le document dit 6'480.00", "x", expected=6600.0, found=6480.0),
                         factcheck._f("weekday", "block", "Le 3 mars 2027 est un mercredi, pas un lundi", "x")],
            "defects": [{"where": "Conditions", "problem": "délai absent", "fix": "délai de paiement 30 jours"}]}
    res = {"findings": [], "defects": [], "verified": ["totaux recalculés"], "to_check": [], "route": ""}
    card = qualitygate.card(res, corrected=True, previous=prev)
    assert card["fixed"][0] == "Total : 6'480.00 → 6'600.00"
    assert "Le 3 mars 2027 est un mercredi, pas un lundi" in card["fixed"]
    assert "Conditions : délai de paiement 30 jours" in card["fixed"]
    # what is still there is not « corrected »
    still = {**res, "findings": [prev["findings"][1]]}
    assert "Le 3 mars 2027 est un mercredi, pas un lundi" not in qualitygate.card(still, corrected=True, previous=prev)["fixed"]
    assert qualitygate.card(res, corrected=False, previous=prev)["fixed"] == []
