"""Non-regression of the security review of the « PII sandwich » (4.0.1): each case the review
demonstrated against 4.0.0's branch, now held. E1 names, E2 formats, M1 the person's name,
M2 a pseudonym that comes in, M3 the work's token, F1 retention, F2 normalisation collisions,
F3 fields of a request. Every value is FICTIONAL (standard example IBANs, made-up names)."""
import json
import time

import pytest

import pii
from simple_fixtures import world  # noqa: F401 — the fixture
from test_claude_tier import SECRET, ask, inst  # noqa: F401 — fixtures

IBAN_CH = "CH93 0076 2011 6238 5295 7"


def cats(t, names=()):
    return [(f.category, f.value) for f in pii.detect(t, names)]


# ---------------------------------------------------------------- E2 formats

@pytest.mark.parametrize("t,value", [
    ("IBAN CH93 0076 2011 6238 5295 7", "CH93 0076 2011 6238 5295 7"),
    ("IBAN CH9300762011623852957", "CH9300762011623852957"),
    ("iban ch93 0076 2011 6238 5295 7", "ch93 0076 2011 6238 5295 7"),                  # lower case
    ("IBAN CH93 0076 2011 6238 5295 7", "CH93 0076 2011 6238 5295 7"),
    ("IBAN CH93 0076 2011 6238 5295 7", "CH93 0076 2011 6238 5295 7"),
    ("IBAN CH93-0076-2011-6238-5295-7", "CH93-0076-2011-6238-5295-7"),
    ("IBAN DE89 3704 0044 0532 0130 00 merci", "DE89 3704 0044 0532 0130 00"),          # any country
    ("IBAN FR14 2004 1010 0505 0001 3M02 606", "FR14 2004 1010 0505 0001 3M02 606"),
    ("IBAN GB82 WEST 1234 5698 7654 32.", "GB82 WEST 1234 5698 7654 32"),
])
def test_e2_iban_of_every_country_any_case_any_separator(t, value):
    assert cats(t) == [("iban", value)]


def test_e2_iban_still_needs_its_check_digits():
    for t in ("IBAN DE88 3704 0044 0532 0130 00", "IBAN CH94 0076 2011 6238 5295 7"):
        assert "iban" not in {c for c, _ in cats(t)}


@pytest.mark.parametrize("t", ["756.1234.5678.97", "7561234567897", "756 1234 5678 97", "756-1234-5678-97",
                               "756.1234.5678.98"])                  # last one: a wrong key is still a number
def test_e2_avs_with_any_separator_and_even_with_a_wrong_key(t):
    assert cats(f"AVS {t} merci") == [("avs", t)]


@pytest.mark.parametrize("t", ["+41 79 123 45 67", "0041 79 123 45 67", "079 123 45 67", "0791234567",
                               "+41791234567", "+41 79 1234567", "079 123 4567", "079/123 45 67", "079/123.45.67",
                               "022 / 345 67 89", "+41 (0)79 123 45 67", "+41(0)79 123 45 67", "+49 170 1234567",
                               "+39 333 123 4567", "+41 79 123-45-67", "+43 1 234 5678", "+33 6 12 34 56 78",
                               "079 123 45 67"])
def test_e2_phones(t):
    assert cats("Tél. " + t + " merci") == [("phone", t)]


def test_e2_a_number_glued_after_a_phone_is_not_taken_and_dates_are_not_phones():
    assert cats("Tél. +41 79 123 45 67 2 fois") == [("phone", "+41 79 123 45 67")]
    assert cats("Réunion le 01.02.2023 12:30, salle 4.") == []


# ---------------------------------------------------------------- E1 names and dates

@pytest.mark.parametrize("t,value", [
    ("Nom : DUPONT Jean", "DUPONT Jean"),
    ("Nom: Jean DUPONT", "Jean DUPONT"),
    ("Nom : Jean Fictif; poste : comptable", "Jean Fictif"),
    ("madame fictive est arrivée", "fictive"),
    ("Mme FICTIF a signé", "FICTIF"),
])
def test_e1_a_whole_name_is_taken_never_a_part(t, value):
    assert cats(t) == [("person", value)]
    m = pii.PseudoMap()
    out, _ = m.anonymize(t)
    assert all(w not in out for w in value.split())


def test_e1_words_that_follow_a_civility_without_being_a_name():
    assert cats("Madame la directrice confirme.") == []
    assert cats("call me Peter") == []


@pytest.mark.parametrize("t,value", [
    ("Date de naissance\n14.03.1985", "14.03.1985"),
    ("Geburtsdatum: 14.3.85", "14.3.85"),
    ("né le 14 mars 1985", "14 mars 1985"),
])
def test_e1_dates_of_birth_on_the_next_line_and_short_years(t, value):
    assert cats(t) == [("birthdate", value)]


@pytest.mark.parametrize("table", [
    "Nom;Prénom;Naissance\nFictif;Sophie;14.03.1985\nExemple;Paul;02.11.1990",
    "Nom\tPrénom\tNaissance\nFictif\tSophie\t14.03.1985\nExemple\tPaul\t02.11.1990",
    "| Nom | Prénom | Naissance |\n|---|---|---|\n| Fictif | Sophie | 14.03.1985 |\n| Exemple | Paul | 02.11.1990 |",
    "Nom | Prénom | Naissance\nFictif | Sophie | 14.03.1985\nExemple | Paul | 02.11.1990",     # Excel as text
])
def test_e1_a_column_under_a_personal_header_is_personal_whoever_wrote_it(table):
    m = pii.PseudoMap()
    out, found = m.anonymize(table)
    for v in ("Fictif", "Sophie", "Exemple", "Paul", "14.03.1985", "02.11.1990"):
        assert v not in out, (v, out)
    assert pii.counts(found) == {"person": 4, "birthdate": 2}


def test_e1_a_sentence_with_commas_is_not_a_table():
    assert cats("Nom, prénom et adresse sont requis, merci.\nVoici, la suite, du texte") == []


def test_e1_a_work_about_people_is_asked_even_when_nothing_is_spotted(inst):  # noqa: F811
    import roles
    c = inst["as"]("bob@x", "cours")
    tid = f"hr.{roles.get('hr')['templates'][0]['id']}"
    ask_ = "Rédige un avertissement pour la personne arrivée en retard cinq fois."
    r = c.post("/api/simple/start", json={"ask": ask_, "playbook": tid, "tier": "claude"})
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "claude_pii" and d["pii"]["people_job"] is True
    assert d["pii"]["total"] == 0 and d["pii"]["choices"] == ["anonymize", "local"]
    assert "rien de repéré ne veut pas dire rien de personnel" in d["message"]
    # a job that does not handle people: no card when nothing is spotted
    tid = f"training.{roles.get('training')['templates'][0]['id']}"
    assert c.post("/api/simple/start", json={"ask": "Plan du module 3", "playbook": tid,
                                              "tier": "claude"}).status_code == 200
    assert {r_["id"] for r_ in roles.catalog("fr") if r_["people"]} == {"hr", "executive-assistant",
                                                                         "customer-support", "sales", "legal",
                                                                         "finance"}
    assert "Un nom seul dans le texte peut ne pas être repéré" in d["message"]


# ---------------------------------------------------------------- M1 the person's own name

def test_m1_the_name_the_person_is_shown_under_never_leaves(inst):  # noqa: F811
    import claudepii
    import iam
    iam.upsert_user("bob@x", "dev", "Robert Fictif")
    c = inst["as"]("bob@x", "cours")
    sid = c.post("/api/simple/start", json={"ask": "Rédige le module 3", "tier": "claude"}).json()["session_id"]
    payload = ask("You are working with: Robert Fictif. Do not ask for their own name.\nRédige le module 3")
    st, out = claudepii.anonymize_request(sid, "cours", payload, claudepii.work_token(sid, "cours"))
    assert st == 200 and "Robert" not in json.dumps(out["payload"]) and "Fictif" not in json.dumps(out["payload"])


# ---------------------------------------------------------------- M2 a pseudonym that comes in

def test_m2_a_pseudonym_written_by_a_third_party_is_defused():
    m = pii.PseudoMap()
    m.anonymize(f"Mon IBAN est {IBAN_CH}.")
    doc = "Merci de confirmer vos coordonnées bancaires : [IBAN_1]"
    out, found = m.anonymize(doc)
    assert "[IBAN_1]" not in out and "[IBAN-1]" in out and len(out) == len(doc)
    assert IBAN_CH not in m.restore(out)
    # in a whole request too
    payload, _ = pii.anonymize_payload({"messages": [{"role": "user", "content": doc}]}, m)
    assert "[IBAN_1]" not in json.dumps(payload)


# ---------------------------------------------------------------- M3 the work's token

def test_m3_a_work_is_answered_only_with_its_own_token(inst):  # noqa: F811
    import claudepii
    import claudetier
    c = inst["as"]("bob@x", "cours")
    a = c.post("/api/simple/start", json={"ask": f"IBAN {IBAN_CH}", "tier": "claude", "pii": "anonymize"}).json()["session_id"]
    b = c.post("/api/simple/start", json={"ask": "Rédige le module 3", "tier": "claude"}).json()["session_id"]
    tok_b = claudepii.work_token(b, "cours")
    assert claudetier.session_env(b, "cours", {})["ANTHROPIC_CUSTOM_HEADERS"].endswith(f"x-sokkan-work-token: {tok_b}")
    h = {"x-sokkan-router-secret": SECRET}
    body = {"work": a, "project": "cours", "payload": ask("x")}
    assert c.post("/api/internal/claude-pii", json=body, headers=h).status_code == 404              # no token
    assert c.post("/api/internal/claude-pii", json={**body, "token": tok_b}, headers=h).status_code == 404  # B's
    r = c.post("/api/internal/claude-pii", json={**body, "token": claudepii.work_token(a, "cours")}, headers=h)
    assert r.status_code == 200 and r.json()["restore"]["[IBAN_1]"] == IBAN_CH
    assert claudepii.work_token(a, "cours") != claudepii.work_token(a, "gestion")


# ---------------------------------------------------------------- F1 forget, retention, encryption

def test_f1_the_mapping_is_encrypted_forgotten_with_the_work_and_purged_after_its_retention(inst, monkeypatch):  # noqa: F811
    import claudepii
    import simple
    c = inst["as"]("bob@x", "cours")
    sid = c.post("/api/simple/start", json={"ask": f"IBAN {IBAN_CH}", "tier": "claude", "pii": "anonymize"}).json()["session_id"]
    con = simple._con()
    blob = con.execute("SELECT data FROM pii_maps WHERE sid=?", (sid,)).fetchone()[0]
    con.close()
    assert "6238" not in blob and "IBAN" not in blob                               # not readable from the file
    assert simple.get_pii_map(sid)["values"]["[IBAN_1]"] == IBAN_CH
    simple.forget(sid)
    assert simple.get_pii_map(sid) == {}
    sid2 = c.post("/api/simple/start", json={"ask": f"IBAN {IBAN_CH}", "tier": "claude", "pii": "anonymize"}).json()["session_id"]
    assert claudepii.retention_days() == 30
    con = simple._con()
    con.execute("UPDATE pii_maps SET updated_at=? WHERE sid=?", (time.time() - 31 * 86400, sid2))
    con.commit()
    con.close()
    assert simple.purge_pii_maps(30) == 1 and simple.get_pii_map(sid2) == {}
    monkeypatch.setenv("SOKKAN_CLAUDE_PII_RETENTION_DAYS", "0")
    assert claudepii.retention_days() == 0 and simple.purge_pii_maps(0) == 0


# ---------------------------------------------------------------- F2 normalisation collisions

def test_f2_two_numbers_are_never_one_and_each_is_restored_as_written():
    m = pii.PseudoMap()
    a, _ = m.anonymize("Contact Bâle : 061 234 56 78.")
    b, _ = m.anonymize("Contact Lyon : 06 12 34 56 78.")
    assert a.split(": ")[1] != b.split(": ")[1]
    assert m.restore(a) == "Contact Bâle : 061 234 56 78." and m.restore(b) == "Contact Lyon : 06 12 34 56 78."
    c, _ = m.anonymize("Zurich : +41 61 234 56 78, Paris : +33 6 12 34 56 78")
    assert len(set(m.values)) == 4
    # one IBAN written two ways is still one value
    m2 = pii.PseudoMap()
    x, _ = m2.anonymize(f"{IBAN_CH} / CH9300762011623852957")
    assert x == "[IBAN_1] / [IBAN_1]"


# ---------------------------------------------------------------- F3 the fields of a request

def test_f3_tool_definitions_citations_and_metadata_are_walked_names_of_tools_kept():
    m = pii.PseudoMap()
    payload = {"model": "claude-x", "system": "x",
               "tools": [{"name": "send_mail", "description": "Envoie à jean.fictif@example.ch",
                          "input_schema": {"type": "object", "required": ["to"], "properties": {
                              "to": {"type": "string", "description": "défaut +41 79 123 45 67"}}}}],
               "metadata": {"user_id": "jean.fictif@example.ch"},
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": "ok", "citations": [{"cited_text": "jean.fictif@example.ch"}]},
                   {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "x",
                    "name": "jean.fictif@example.ch"}]}]},
                   {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "send_mail",
                                                      "input": {"to": "jean.fictif@example.ch"}}]}]}
    out, found = pii.anonymize_payload(payload, m)
    s = json.dumps(out, ensure_ascii=False)
    assert "jean.fictif@example.ch" not in s and "+41 79 123 45 67" not in s
    assert out["tools"][0]["name"] == "send_mail" and out["messages"][1]["content"][0]["name"] == "send_mail"
    assert out["tools"][0]["input_schema"]["required"] == ["to"]
    assert list(out["tools"][0]["input_schema"]["properties"]) == ["to"]


# ---------------------------------------------------------------- 2nd review (4.0.1)

def test_ma_quoted_csv_headers_and_cells():
    m = pii.PseudoMap()
    out, found = m.anonymize('"Nom","Prénom","Naissance"\n"Fictif","Paul","02.11.1990"\n')
    assert out == '"Nom","Prénom","Naissance"\n"[PERSONNE_1]","[PERSONNE_2]","[DATE_NAISSANCE_1]"\n'
    assert m.restore(out) == '"Nom","Prénom","Naissance"\n"Fictif","Paul","02.11.1990"\n'


def test_mb_decomposed_unicode_is_seen_and_replaced_where_it_was_written():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "Nom : Ève DUPONT, née le 14.03.1985")
    assert nfd != unicodedata.normalize("NFC", nfd)
    m = pii.PseudoMap()
    out, found = m.anonymize(nfd)
    assert out == "Nom : [PERSONNE_1], née le [DATE_NAISSANCE_1]".replace("née", unicodedata.normalize("NFD", "née"))
    assert m.restore(out) == nfd and pii.counts(found) == {"person": 1, "birthdate": 1}
    # a name of the directory written in NFD is found in a text in NFC, and the reverse
    assert cats("Contact : Ève Exemple", [unicodedata.normalize("NFD", "Ève Exemple")])[0][1] == "Ève Exemple"


def test_mc_lower_case_cells_under_a_personal_header():
    m = pii.PseudoMap()
    out, found = m.anonymize("nom;prénom\nfictif;paul\nexemple;léa")
    for v in ("fictif", "paul", "exemple", "léa"):
        assert v not in out
    assert pii.counts(found) == {"person": 4}


def test_purge_runs_when_the_api_starts():
    import inspect

    import app
    src = inspect.getsource(app)
    i = src.index("hitl_journal.mark_interrupted()")
    assert "purge_pii_maps(claudepii.retention_days())" in src[i:i + 600]


def test_the_card_tells_the_person_a_name_alone_may_be_missed(inst):  # noqa: F811
    c = inst["as"]("bob@x", "cours")
    r = c.post("/api/simple/start", json={"ask": f"IBAN {IBAN_CH}", "tier": "claude"})
    assert "Un nom seul dans le texte peut ne pas être repéré" in r.json()["detail"]["message"]
    r = c.post("/api/simple/start", json={"ask": f"IBAN {IBAN_CH}", "tier": "claude", "lang": "en"})
    assert "A name alone in the text may not be spotted" in r.json()["detail"]["message"]
    src = open("frontend/lib/simple/dict.ts", encoding="utf-8").read()
    assert src.count('"pii.alone"') == 2
    assert 't("pii.alone")' in open("frontend/components/simple/PiiCard.tsx", encoding="utf-8").read()
