"""« PII sandwich » of « Write with Claude » (4.0, decision of 18.11.2026): personal data is found
on the machine, the person is told and chooses, the values are pseudonymised before anything
reaches Anthropic and put back in what comes back; no job is refused Claude any more, and a
confidential project still never goes there (D9). backend/pii.py, claudepii.py, shiprouter.py.

Every value below is FICTIONAL: example IBANs of the standard, a checksum-valid AVS number made
up for the test, invented names and addresses. Anthropic is never called: a fake server."""
import json

import pytest

import claudetier
import pii
import shiprouter
from simple_fixtures import put, world  # noqa: F401 — the fixture
from test_claude_tier import CLAUDE, KEY, SECRET, ask, box, inst  # noqa: F401 — fixtures

IBAN_CH = "CH93 0076 2011 6238 5295 7"          # the standard's example
IBAN_LI = "LI21 0881 0000 2324 013A A"          # the standard's example
AVS = "756.1234.5678.97"                        # check digit valid, number invented
SAMPLE = (f"Bonjour, je suis Mme Claire Exemple, née le 14.03.1985, AVS {AVS}.\n"
          f"IBAN {IBAN_CH}. Écrire à claire.exemple@example.ch ou au +41 79 123 45 67.\n"
          "Adresse : Rue de la Gare 12, 1201 Genève. Exemple confirmera.")


# ---------------------------------------------------------------- detection

def test_iban_and_avs_are_found_only_when_their_check_digits_hold():
    assert pii.iban_ok(IBAN_CH) and pii.iban_ok(IBAN_LI) and pii.iban_ok(IBAN_CH.replace(" ", ""))
    assert not pii.iban_ok(IBAN_CH.replace("93", "94", 1))
    assert pii.iban_ok("DE89 3704 0044 0532 0130 00")                  # 4.0.1: every country
    assert pii.avs_ok(AVS) and pii.avs_ok(AVS.replace(".", ""))
    assert not pii.avs_ok("756.1234.5678.98")
    text = f"A {IBAN_CH} B CH94 0076 2011 6238 5295 7 C {AVS} D 756.1234.5678.98"
    # 4.0.1: an AVS number with a wrong key is still somebody's number (E2)
    assert [(f.category, f.value) for f in pii.detect(text)] == [("iban", IBAN_CH), ("avs", AVS),
                                                                  ("avs", "756.1234.5678.98")]


@pytest.mark.parametrize("text,value", [
    ("tél. +41 79 123 45 67.", "+41 79 123 45 67"),
    ("tél. 0041 22 345 67 89.", "0041 22 345 67 89"),
    ("tél. 022 345 67 89.", "022 345 67 89"),
    ("tél. 079.123.45.67.", "079.123.45.67"),
    ("portable 06 12 34 56 78", "06 12 34 56 78"),
    ("portable +33 6 12 34 56 78", "+33 6 12 34 56 78"),
    ("mail: a.b-c@example.ch,", "a.b-c@example.ch"),
])
def test_phones_and_emails(text, value):
    (f,) = pii.detect(text)
    assert f.value == value and f.category in ("phone", "email")


@pytest.mark.parametrize("text", [
    "Réunion le 01.02.2023 12:30, salle 4.",       # a date and an hour: not a phone
    "Total 12 345 678 CHF, facture 2026-0042.",
    "La session commence le 14.03.1985.",          # a date without « né le »: not a birthdate
    "Voir le chapitre sur la rue et la place.",    # street words without a number
])
def test_ordinary_text_is_left_alone(text):
    assert pii.detect(text) == []


def test_birthdates_addresses_and_names():
    found = {(f.category, f.value) for f in pii.detect(
        "Né le 3 mars 1990 ; date de naissance : 1990-03-03 ; born on 03/03/1990.\n"
        "12, rue des Alpes 1700 Fribourg. Musterstrasse 5, 8000 Zürich. Chemin du Lac 7.\n"
        "Herr Hans Muster, Dr Anna Rossi. Nom : Paul Fictif. Candidate: Léa Modèle.")}
    assert {("birthdate", "3 mars 1990"), ("birthdate", "1990-03-03"), ("birthdate", "03/03/1990")} <= found
    assert {("address", "12, rue des Alpes 1700 Fribourg"), ("address", "Musterstrasse 5, 8000 Zürich"),
            ("address", "Chemin du Lac 7")} <= found
    assert {("person", "Hans Muster"), ("person", "Anna Rossi"), ("person", "Paul Fictif"),
            ("person", "Léa Modèle")} <= found


def test_names_of_the_directory_are_found_anywhere_and_a_surname_alone_too():
    text = "Transmettre à Jean Annuaire et à JEAN ANNUAIRE. Annuaire validera. Mme Claire Exemple puis Exemple."
    m = pii.PseudoMap()
    out, found = m.anonymize(text, ["Jean Annuaire"])
    assert "Annuaire" not in out and "ANNUAIRE" not in out and "Exemple" not in out
    assert out.count("[PERSONNE_1]") == 2                     # the same person, one pseudonym
    assert m.restore(out).replace("JEAN ANNUAIRE", "Jean Annuaire") == text.replace("JEAN ANNUAIRE", "Jean Annuaire")


# ---------------------------------------------------------------- the mapping

def test_pseudonyms_are_stable_numbered_per_category_and_restored():
    m = pii.PseudoMap()
    out, found = m.anonymize(SAMPLE)
    for v in ("Claire Exemple", AVS, IBAN_CH, "claire.exemple@example.ch", "+41 79 123 45 67",
              "14.03.1985", "Rue de la Gare 12"):
        assert v not in out, v
    for p in ("[PERSONNE_1]", "[DATE_NAISSANCE_1]", "[AVS_1]", "[IBAN_1]", "[EMAIL_1]", "[TELEPHONE_1]",
              "[ADRESSE_1]"):
        assert p in out, p
    assert m.restore(out) == SAMPLE
    # the same value spelt another way, in a later turn: the same pseudonym
    again, _ = m.anonymize("Virement sur CH9300762011623852957, appeler +41 79 123 45 67.")
    assert again == "Virement sur [IBAN_1], appeler [TELEPHONE_1]."
    # a saved mapping is the same mapping
    m2 = pii.PseudoMap(json.loads(json.dumps(m.to_dict())))
    assert m2.anonymize(SAMPLE)[0] == out and m2.restore(out) == SAMPLE
    assert m.restore("[PERSONNE_9] reste tel quel") == "[PERSONNE_9] reste tel quel"


def test_what_a_person_is_shown_never_holds_a_value():
    s = pii.summary(pii.detect(SAMPLE), "fr")
    flat = json.dumps(s, ensure_ascii=False)
    for v in ("Claire", "Exemple", "6238", "1234.5678", "claire.exemple", "123 45", "1985", "Gare"):
        assert v not in flat, v
    cats = {c["category"]: c for c in s["categories"]}
    assert cats["person"]["count"] == 2 and cats["iban"]["count"] == 1      # Claire Exemple + « Exemple »
    assert cats["iban"]["label"] == "IBAN" and s["total"] == sum(c["count"] for c in s["categories"])
    assert pii.summary(pii.detect(SAMPLE), "en")["categories"][0]["label"] == "names of people"


def test_a_pseudonym_cut_between_two_pieces_of_a_stream_is_restored_whole():
    m = pii.PseudoMap()
    m.anonymize("Mme Claire Exemple")
    r = pii.StreamRestorer(m)
    pieces = ["Chère [PERS", "ONNE", "_1], merci", " [", "IBAN_7] [x"]
    out = "".join(r.feed(p) for p in pieces) + r.flush()
    assert out == "Chère Claire Exemple, merci [IBAN_7] [x"
    # inside JSON (a tool's input): the value is escaped as a JSON string body
    m2 = pii.PseudoMap()
    m2.anonymize('Pour Jean "Jo" Fictif', ['Jean "Jo" Fictif'])
    j = pii.StreamRestorer(m2, json_mode=True)
    frag = "".join(j.feed(p) for p in ['{"content": "Bonjour [PERSO', 'NNE_1]\\n"}']) + j.flush()
    assert json.loads(frag)["content"] == f"Bonjour {m2.values['[PERSONNE_1]']}\n"


# ---------------------------------------------------------------- a Messages request

def test_a_request_is_pseudonymised_everywhere_a_person_or_a_document_wrote():
    m = pii.PseudoMap()
    payload = {
        "model": "claude-opus-5-5", "stream": True,
        "system": [{"type": "text", "text": "Signataire : Paul Fictif", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "Write", "description": "Nom : outil", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": SAMPLE},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Mme Claire Exemple?", "signature": "sig"},
                {"type": "tool_use", "id": "t1", "name": "Write",
                 "input": {"file_path": "/w/lettre.md", "content": "Pour Mme Claire Exemple"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": f"IBAN lu : {IBAN_LI}"}]},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "BBBB"}}]},
        ]}
    out, found = pii.anonymize_payload(payload, m, ["Paul Fictif"], "fr")
    flat = json.dumps(out, ensure_ascii=False)
    for v in ("Paul Fictif", "claire.exemple", IBAN_LI, AVS, "AAAA", "BBBB"):
        assert v not in flat, v
    assert out["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert out["tools"] == payload["tools"]                                    # the cockpit's own words
    assert out["messages"][1]["content"][0] == payload["messages"][1]["content"][0]  # thinking: signed, untouched
    assert out["messages"][1]["content"][1]["input"]["content"] == "Pour Mme [PERSONNE_2]"
    assert out["messages"][1]["content"][1]["input"]["file_path"] == "/w/lettre.md"
    assert out["messages"][2]["content"][2]["text"] == pii.WITHHELD["fr"]
    assert payload["messages"][0]["content"] == SAMPLE                         # the original is not touched
    # the second request of the same conversation: the same prefix (Anthropic's cache holds)
    assert pii.anonymize_payload(payload, m, ["Paul Fictif"], "fr")[0] == out


def test_the_stream_and_a_complete_answer_get_the_values_back():
    m = pii.PseudoMap()
    m.anonymize(f"Mme Claire Exemple, {IBAN_CH}")

    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
    stream = (ev("message_start", {"type": "message_start", "message": {"model": "claude-opus-5-5"}})
              + ev("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}})
              + ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": "Bonjour [PERSONNE"}})
              + ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": "_1], IBAN [IBAN_1]"}})
              + ev("content_block_stop", {"type": "content_block_stop", "index": 0})
              + ev("content_block_start", {"type": "content_block_start", "index": 1,
                                           "content_block": {"type": "tool_use", "id": "t", "name": "Write", "input": {}}})
              + ev("content_block_delta", {"type": "content_block_delta", "index": 1,
                                           "delta": {"type": "input_json_delta", "partial_json": '{"content": "[PERSON'}})
              + ev("content_block_delta", {"type": "content_block_delta", "index": 1,
                                           "delta": {"type": "input_json_delta", "partial_json": 'NE_1]"}'}})
              + ev("content_block_stop", {"type": "content_block_stop", "index": 1})
              + ev("message_stop", {"type": "message_stop"}))
    sse = pii.SSERestorer(m)
    out = b"".join(sse.feed(stream[i:i + 37]) for i in range(0, len(stream), 37)) + sse.close()
    text, tool = "", ""
    for raw in out.decode().split("\n\n"):
        data = next((ln[6:] for ln in raw.split("\n") if ln.startswith("data: ")), None)
        if not data:
            continue
        d = json.loads(data)
        if d["type"] == "content_block_delta" and d["delta"]["type"] == "text_delta":
            text += d["delta"]["text"]
        if d["type"] == "content_block_delta" and d["delta"]["type"] == "input_json_delta":
            tool += d["delta"]["partial_json"]
    assert text == f"Bonjour Claire Exemple, IBAN {IBAN_CH}"
    assert json.loads(tool) == {"content": "Claire Exemple"}
    msg = {"content": [{"type": "text", "text": "Pour [PERSONNE_1]"},
                       {"type": "tool_use", "input": {"content": ["[IBAN_1]"]}},
                       {"type": "thinking", "thinking": "[PERSONNE_1]"}]}
    r = pii.restore_message(msg, m)
    assert r["content"][0]["text"] == "Pour Claire Exemple" and r["content"][1]["input"]["content"] == [IBAN_CH]
    assert r["content"][2]["thinking"] == "[PERSONNE_1]"


# ---------------------------------------------------------------- the router

def _api_sandwich(api, names=()):
    m = pii.PseudoMap()

    def handler(body):
        out, found = pii.anonymize_payload(body["payload"], m, names)
        return {"payload": out, "restore": dict(m.values), "found": pii.counts(found)}
    api.pii = handler
    return m


def test_the_router_sends_pseudonyms_to_anthropic_and_the_values_back_to_the_cli(box):  # noqa: F811
    c, ship, deep, api, anthropic = box
    _api_sandwich(api)
    anthropic.sse = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5"}}\n\n'
                     b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
                     b'"content_block":{"type":"text","text":""}}\n\n'
                     b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                     b'"delta":{"type":"text_delta","text":"Dear [PERSONNE_1]"}}\n\n'
                     b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
                     b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    r = c.post("/v1/messages", json=ask(SAMPLE), headers=CLAUDE)
    assert r.status_code == 200, r.text
    (sent,) = anthropic.seen
    flat = json.dumps(sent["body"], ensure_ascii=False)
    for v in ("Claire Exemple", IBAN_CH, AVS, "claire.exemple@example.ch", "+41 79 123 45 67", "Rue de la Gare"):
        assert v not in flat, v
    assert "[PERSONNE_1]" in flat and "[IBAN_1]" in flat
    assert "Dear Claire Exemple" in r.text and "[PERSONNE_1]" not in r.text
    (p,) = [x for x in api.seen if x["path"] == "/api/internal/claude-pii"]
    assert p["body"]["work"] == "w1" and p["body"]["project"] == "cours"
    assert p["headers"]["x-sokkan-router-secret"] == SECRET
    assert not [k for k in sent["headers"] if k.startswith("x-sokkan-")]       # the work's id stays home
    assert shiprouter._stats["claude:pii_requests"] == 1


@pytest.mark.parametrize("breakit", ["no_work", "api_404", "api_500"])
def test_without_its_sandwich_nothing_is_sent_to_anthropic(box, breakit):  # noqa: F811
    c, ship, deep, api, anthropic = box
    h = dict(CLAUDE)
    if breakit == "no_work":
        h.pop("x-sokkan-work")
    else:
        api.pii_status = 404 if breakit == "api_404" else 500
    r = c.post("/v1/messages", json=ask(SAMPLE), headers=h)
    assert r.status_code >= 400 and r.headers["x-sokkan-error"] == "claude_unavailable"
    assert r.headers["x-should-retry"] == "false"
    assert anthropic.seen == [] and ship.seen == [] and deep.seen == []
    assert "Claire" not in r.text


# ---------------------------------------------------------------- the instance

def _journal(project, kind="pii"):
    import hitl_journal
    return [x for x in hitl_journal.query(project=project, limit=100) if x["kind"] == kind]


def test_personal_data_is_said_before_anything_leaves_and_the_person_chooses(inst):  # noqa: F811
    import simple
    c = inst["as"]("bob@x", "cours")
    n = len(inst["turns"])
    r = c.post("/api/simple/start", json={"ask": SAMPLE, "tier": "claude", "request_id": "a" * 32})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "claude_pii" and d["pii"]["choices"] == ["anonymize", "local"]
    assert {x["category"] for x in d["pii"]["categories"]} == {"person", "iban", "avs", "email", "phone",
                                                               "birthdate", "address"}
    flat = json.dumps(d, ensure_ascii=False)
    for v in ("Claire", "6238", "1234.5678", "claire.exemple", "123 45 67", "Gare"):
        assert v not in flat, v
    assert len(inst["turns"]) == n and not _journal("cours")                  # nothing started, nothing sent
    assert "données personnelles" in d["message"]
    # « Anonymiser et envoyer à Claude »
    r = c.post("/api/simple/start", json={"ask": SAMPLE, "tier": "claude", "pii": "anonymize",
                                          "request_id": "b" * 32})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert simple.get_work(sid)["pii"] == "anonymize" and claudetier.work_tier(sid)[0] == "claude"
    (row,) = _journal("cours")
    assert row["decision"] == "anonymize" and row["actor"] == "bob@x" and row["decided_at"]
    assert "person:2" in row["message"] and "iban:1" in row["message"] and "Claire" not in json.dumps(row)
    assert [e for e in inst["audit"]("claude.pii.anonymize") if "Claire" not in e["detail"]]
    m = pii.PseudoMap(simple.get_pii_map(sid))
    assert m.values["[PERSONNE_1]"] == "Claire Exemple" and m.values["[IBAN_1]"] == IBAN_CH
    # « Rester sur le modèle local »: an ordinary work, journaled as such
    r = c.post("/api/simple/start", json={"ask": SAMPLE, "pii": "local", "request_id": "c" * 32})
    assert r.status_code == 200
    sid2 = r.json()["session_id"]
    assert claudetier.work_tier(sid2) == ("", "") and claudetier.session_env(sid2, "cours", {}) == {}
    assert [x["decision"] for x in _journal("cours")].count("local") == 1
    # nothing personal: no card
    r = c.post("/api/simple/start", json={"ask": "Rédige le module 3", "tier": "claude"})
    assert r.status_code == 200


def test_an_attachment_counts_and_the_directory_names_count(inst):  # noqa: F811
    import orgprofile
    orgprofile.update({"people": [{"name": "Jean Annuaire", "function": "RH"}]}, by="admin@x")
    c = inst["as"]("bob@x", "cours")
    doc = put(c, "liste.txt", f"Participants : Jean Annuaire, {IBAN_LI}\n".encode()).json()
    import documents
    con = documents._con()                    # a public list: Claude may receive it
    con.execute("UPDATE documents SET level=0 WHERE id=?", (doc["id"],))
    con.commit()
    con.close()
    r = c.post("/api/simple/start", json={"ask": "Prépare les convocations", "tier": "claude",
                                          "documents": [doc["id"]]})
    assert r.status_code == 409, r.text
    cats = {x["category"]: x["count"] for x in r.json()["detail"]["pii"]["categories"]}
    assert cats == {"person": 1, "iban": 1}


def test_d9_a_confidential_project_never_goes_to_claude_even_anonymised(inst):  # noqa: F811
    c = inst["as"]("bob@x", "gestion")
    r = c.post("/api/simple/start", json={"ask": SAMPLE, "tier": "claude", "pii": "anonymize"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_level"
    assert not _journal("gestion")
    assert claudetier.HARD_MAX == 2                                            # confidential (3) and above


def test_a_job_kit_no_longer_refuses_claude_in_a_project_that_allows_it(inst):  # noqa: F811
    import roles
    c = inst["as"]("bob@x", "cours")
    for rid in ("hr", "legal", "finance"):
        tid = f"{rid}.{roles.get(rid)['templates'][0]['id']}"
        r = c.post("/api/simple/start", json={"ask": "Prépare le modèle", "playbook": tid, "tier": "claude"})
        if roles.get(rid).get("people"):       # a job about people: asked even with nothing spotted (E1)
            assert r.status_code == 409 and r.json()["detail"]["pii"]["people_job"] is True, (tid, r.text)
            r = c.post("/api/simple/start", json={"ask": "Prépare le modèle", "playbook": tid, "tier": "claude",
                                                  "pii": "anonymize"})
        assert r.status_code == 200, (tid, r.text)
    # the project a job kit sets up for HR is confidential: Claude stays out by its level
    a = inst["as"]("admin@x")
    slug = a.post("/api/simple/roles/hr/setup", json={"project": ""}).json()["project"]
    r = inst["as"]("admin@x", slug).post("/api/simple/start", json={"ask": "x", "tier": "claude"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_level"


def test_a_follow_up_with_personal_data_is_asked_too(inst):  # noqa: F811
    c = inst["as"]("bob@x", "cours")
    sid = c.post("/api/simple/start", json={"ask": "Rédige le module 3", "tier": "claude"}).json()["session_id"]
    n = len(inst["turns"])
    r = c.post(f"/api/simple/sessions/{sid}/message", json={"text": f"Ajoute l'IBAN {IBAN_CH}"})
    assert r.status_code == 409 and r.json()["detail"]["pii"]["choices"] == ["anonymize", "cancel"]
    assert len(inst["turns"]) == n
    r = c.post(f"/api/simple/sessions/{sid}/pii-decline", json={"counts": {"iban": 1, "bogus": 3}})
    assert r.status_code == 200
    r = c.post(f"/api/simple/sessions/{sid}/message", json={"text": f"Ajoute l'IBAN {IBAN_CH}", "pii": "anonymize"})
    assert r.status_code == 200, r.text
    assert [x["decision"] for x in _journal("cours")].count("declined") == 1
    rows = {x["decision"]: x for x in _journal("cours")}
    assert "iban:1" in rows["declined"]["message"] and "bogus" not in rows["declined"]["message"]
    # agreed once for this work: the next personal data goes without a second card
    r = c.post(f"/api/simple/sessions/{sid}/message", json={"text": "Et Mme Claire Exemple en copie"})
    assert r.status_code in (200, 409) and r.json().get("detail", {}).get("code") != "claude_pii"


def test_the_internal_route_answers_the_router_only_and_only_for_a_claude_work(inst):  # noqa: F811
    c = inst["as"]("bob@x", "cours")
    sid = c.post("/api/simple/start", json={"ask": SAMPLE, "tier": "claude", "pii": "anonymize"}).json()["session_id"]
    import claudepii
    body = {"work": sid, "project": "cours", "payload": ask(SAMPLE), "token": claudepii.work_token(sid, "cours")}
    assert c.post("/api/internal/claude-pii", json=body).status_code == 404
    assert c.post("/api/internal/claude-pii", json=body,
                  headers={"x-sokkan-router-secret": "wrong"}).status_code == 404
    assert c.post("/api/internal/claude-pii", json=body,
                  headers={"x-sokkan-router-secret": SECRET, "x-forwarded-for": "10.0.0.9"}).status_code == 404
    r = c.post("/api/internal/claude-pii", json=body, headers={"x-sokkan-router-secret": SECRET})
    assert r.status_code == 200, r.text
    out = r.json()
    assert "Claire" not in json.dumps(out["payload"]) and out["restore"]["[PERSONNE_1]"] == "Claire Exemple"
    assert out["found"]["iban"] == 1
    # the same request again: the same pseudonyms (the mapping is the work's)
    assert c.post("/api/internal/claude-pii", json=body,
                  headers={"x-sokkan-router-secret": SECRET}).json()["payload"] == out["payload"]
    # another project, or a work that is not a Claude work: refused
    assert c.post("/api/internal/claude-pii", json={**body, "project": "gestion"},
                  headers={"x-sokkan-router-secret": SECRET}).status_code == 404
    sid2 = c.post("/api/simple/start", json={"ask": "x"}).json()["session_id"]
    assert c.post("/api/internal/claude-pii", json={**body, "work": sid2},
                  headers={"x-sokkan-router-secret": SECRET}).status_code == 404
    assert KEY not in r.text                                                   # never in a sandwich answer
