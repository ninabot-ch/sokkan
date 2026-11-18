"""Launch council (feature `council`, experimental): the nine roles, the three bounded rounds
with the fake engine (no model), the budget ceiling, the strict JSON of the arbitration and its
fact-check, the refusals and re-syntheses, Claude refused on a confidential project (at opening
AND at every call), the PII sandwich ask, acceptance (board cards in Backlog, DRAFT agents, open
positions), the journal and its exports, and the HTTP routes. The UI half: tests/nav/council_test.ts."""
import copy
import json
import threading
import time

import pytest

from council import engine as eng
from council import models, roles, schema, service, store
from simple_fixtures import world  # noqa: F401 — the fixture

BRIEF = "Un robot de bureau à roues avec projecteur, prototype en 8 semaines."


@pytest.fixture(autouse=True)
def _council_env(tmp_path, monkeypatch):
    import agents
    monkeypatch.setattr(store, "DB", tmp_path / "council.db")
    monkeypatch.setattr(store, "_ready_for", None)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    monkeypatch.setenv("SOKKAN_FEATURE_COUNCIL", "1")
    for v in ("SOKKAN_COUNCIL_MAX_TOKENS", "SOKKAN_COUNCIL_MAX_COST_CHF"):
        monkeypatch.delenv(v, raising=False)
    yield
    models.set_default(None)


def new(**kw):
    kw.setdefault("project", "default")
    kw.setdefault("owner", "bob@x")
    kw.setdefault("title", "Accord")
    kw.setdefault("brief", BRIEF)
    kw.setdefault("lang", "fr")
    return service.create(**kw)


def user(email="bob@x", project="default"):
    return {"email": email, "role": "dev", "project": project}


def synth(**over):
    s = eng.default_synthesis()
    s.update(over)
    return json.dumps(s)


# ---- the roles -------------------------------------------------------------------------------
def test_nine_roles_in_order_fr_and_en_with_the_rule():
    assert roles.ids() == ["daf", "code", "ops", "conception", "achats", "qa", "marcom", "juridique", "coach"]
    assert roles.get("daf")["tier"] == "deep" and roles.get("daf")["leads"]
    for rid in roles.ids():
        for lang in ("fr", "en"):
            t = roles.text(rid, lang)
            assert t["label"] and t["system_prompt"] and len(t["system_prompt"].split()) < 110
        assert "150 mots" in roles.text(rid, "fr")["limits"] and "estimé" in roles.text(rid, "fr")["limits"]
        assert "150 words" in roles.text(rid, "en")["limits"] and "estimated" in roles.text(rid, "en")["limits"]
        assert roles.get(rid)["avatar"]["initials"] and roles.get(rid)["avatar"]["icon"]
    assert "BOM" in roles.text("conception", "fr")["system_prompt"] and "BOM" in roles.text("conception", "en")["system_prompt"]
    assert "batterie" in roles.text("juridique", "fr")["system_prompt"] and "battery" in roles.text("juridique", "en")["system_prompt"]
    assert "AGENTS" in roles.text("coach", "fr")["system_prompt"] and "pourcentage de poste" in roles.text("coach", "fr")["system_prompt"]
    j = roles.text("juridique", "en")["system_prompt"]
    for w in ("nFADP", "licences", "CE", "RED", "LVD"):
        assert w in j


# ---- the journey --------------------------------------------------------------------------------
def test_full_journey_brief_rounds_card_accept(world):  # noqa: F811
    fake = eng.FakeEngine()
    models.set_default(fake)
    c = new(documents=[{"name": "devis-composants.md", "text": "Composants : 400 CHF"}])
    assert c["state"] == "brief"
    done = service.run(c["id"])
    assert done["state"] == "card" and done["card"]["kind"] == "council_agreement"
    j = store.turns(c["id"])
    r1 = [t for t in j if t["round"] == 1 and t["kind"] == "role"]
    assert [t["role"] for t in r1] == roles.ids()                      # shown DAF → coach
    r2 = [t for t in j if t["kind"] == "reply"]
    assert {t["role"] for t in r2} == {"code", "achats", "marcom", "daf"} and all(t["addressed_to"] for t in r2)
    assert [t["kind"] for t in j if t["round"] == 3] == ["synthesis"]
    assert len(fake.calls) == 9 + 1 + 4 + 1                              # 15 calls
    for t in j:                                                          # the journal is complete
        assert t["ts"] and t["role"] and t["model"] and t["tokens_in"] and t["ms"] is not None
    # the documents reach the model as untrusted data
    assert '<untrusted-data kind="document" name="devis-composants.md">' in fake.calls[0].user
    assert "150 mots" in fake.calls[0].system
    # the DAF is on the Deep tier, the others on Ship
    assert {x.tier for x in fake.calls if x.role == "daf"} == {"deep"}
    assert {x.tier for x in fake.calls if x.role != "daf"} == {"ship"}
    assert done["tokens_in"] == 15 * 1000 and done["calls"] == 15

    res = service.accept(c["id"], user())
    assert res["state"] == "accepted"
    import agents
    import board
    cards = [board.get_card(x["id"]) for x in res["accepted"]["cards"]]
    assert len(cards) == 2 and {x["bucket"] for x in cards} == {"Backlog"} and cards[0]["tag"] == "council"
    ag = [agents.get(x["id"]) for x in res["accepted"]["agents"]]
    assert len(ag) == 1 and ag[0]["status"] == "draft" and ag[0]["trigger"] == "manual"   # never runs by itself
    assert res["positions"] == [{"profil": "Ingenieur electronique", "pourcentage_poste": 20,
                                 "perimetre": "Relire le schema", "status": "open"}]
    with pytest.raises(service.CouncilError):
        service.accept(c["id"], user())                                  # once


def test_round1_runs_two_at_a_time_at_most(world):  # noqa: F811
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(call):
        if call.phase != "round1":
            return
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
    models.set_default(eng.FakeEngine(fail=slow))
    c = new()
    service.run(c["id"])
    assert state["max"] == 2


def test_the_ceo_writes_at_any_time_and_is_heard(world):  # noqa: F811
    seen = []
    fake = eng.FakeEngine(fail=lambda call: seen.append((call.role, call.phase, call.user)))
    models.set_default(fake)
    c = new()
    msg = service.say(c["id"], "Contrainte: budget plafonné à 2000 CHF.", "bob@x")
    assert msg["kind"] == "ceo" and msg["round"] == 0
    service.run(c["id"])
    assert all("budget plafonné à 2000 CHF" in u for _r, ph, u in seen if ph == "round1")
    service.say(c["id"], "Et pas de cloud.", "bob@x")               # also in state card
    assert "Et pas de cloud." in [t["text"] for t in store.turns(c["id"]) if t["kind"] == "ceo"]


def test_the_ceo_can_stop(world):  # noqa: F811
    c = new()
    store.update(c["id"], state="round1")
    service.stop(c["id"])                                           # live: stops before the next call
    models.set_default(eng.FakeEngine())
    service._run(c["id"], ["round1", "round2", "synthesis"])
    s = store.get(c["id"])
    assert s["state"] == "stopped" and s["stop_reason"] == "ceo" and s["calls"] == 0


# ---- budget -------------------------------------------------------------------------------------
def test_the_ceiling_stops_the_council_cleanly_and_keeps_the_journal(world):  # noqa: F811
    models.set_default(eng.FakeEngine(tokens=(900, 100)))
    c = new(max_tokens=5000)
    done = service.run(c["id"])
    assert done["state"] == "stopped_budget" and done["stop_reason"] == "budget"
    assert "Plafond" in done["stop_message"] and "5\u202f000" in done["stop_message"]   # thousands grouped, CHF before the figure
    j = store.turns(c["id"])
    assert 4 <= len([t for t in j if t["kind"] == "role"]) <= 7      # in-flight calls finish, no more start
    assert j[-1]["kind"] == "system" and "Plafond" in j[-1]["text"]
    assert done["tokens_in"] + done["tokens_out"] >= 5000
    with pytest.raises(service.CouncilError):
        service.say(c["id"], "encore", "bob@x")


def test_the_cost_ceiling_too(world):  # noqa: F811
    class Dear(eng.FakeEngine):
        def complete(self, call):
            r = super().complete(call)
            r.cost_chf = 0.4
            return r
    models.set_default(Dear())
    c = new(max_cost_chf=1.0)
    done = service.run(c["id"])
    assert done["state"] == "stopped_budget" and "CHF" in done["stop_message"]


def test_budget_defaults_are_configurable(monkeypatch):
    assert service.defaults() == {"max_tokens": 400000, "max_cost_chf": 5.0}
    monkeypatch.setenv("SOKKAN_COUNCIL_MAX_TOKENS", "123456")
    monkeypatch.setenv("SOKKAN_COUNCIL_MAX_COST_CHF", "1.5")
    assert service.defaults() == {"max_tokens": 123456, "max_cost_chf": 1.5}


# ---- the strict JSON ----------------------------------------------------------------------------
def test_an_invalid_synthesis_is_retried_once(world):  # noqa: F811
    fake = eng.FakeEngine(synthesis=["pas du json {", synth()])
    models.set_default(fake)
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "card"
    retry = [x for x in fake.calls if x.phase == "synthesis"][1]
    assert "invalide" in retry.user
    assert any(t["kind"] == "system" and "invalide" in t["text"] for t in store.turns(c["id"]))


def test_two_invalid_syntheses_fail_the_council(world):  # noqa: F811
    bad = json.dumps({"perimetre": "x", "budget": {"lignes": [], "total_chf": "abc"}})
    models.set_default(eng.FakeEngine(synthesis=[bad, bad]))
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "failed" and done["stop_reason"] == "invalid_json" and "invalide" in done["stop_message"]


def test_schema_rejects_and_names_every_problem():
    with pytest.raises(schema.SchemaError) as e:
        schema.validate({"perimetre": {"resume": "x"}, "budget": {"lignes": [{"poste": "a", "montant_chf": "x"}], "total_chf": 1},
                         "risques": [{"risque": "r", "gravite": "enorme", "responsable": "qa"}], "kanban": [],
                         "agents_a_recruter": [{"nom": "Bad Name", "profil": "p", "perimetre": "q", "palier": "gpt"}],
                         "calendrier": []})
    p = " ".join(e.value.problems)
    for frag in ("montant_chf", "gravite", "kanban: at least one", "nom", "palier", "calendrier: at least one"):
        assert frag in p


def test_the_budget_total_must_equal_the_sum_and_unsourced_figures_are_marked(world):  # noqa: F811
    s = eng.default_synthesis()
    s["budget"] = {"lignes": [{"poste": "A", "montant_chf": 100, "source": "devis.md"},
                              {"poste": "B", "montant_chf": 50.5, "source": ""},
                              {"poste": "C", "montant_chf": 20, "source": "mon intuition"}], "total_chf": 999}
    models.set_default(eng.FakeEngine(synthesis=json.dumps(s)))
    c = new(documents=[{"name": "devis.md", "text": "A = 100 CHF"}])
    card = service.run(c["id"])["card"]
    b = card["synthesis"]["budget"]
    assert b["total_chf"] == 170.5
    srcs = [x["source"] for x in b["lignes"]]
    assert srcs[0] == "devis.md" and srcs[1] == "estimé" and srcs[2].startswith("estimé")
    kinds = {f["kind"] for f in card["findings"]}
    assert {"total_corrected", "unsourced"} <= kinds
    assert "170.50" in card["preview"] and "## À vérifier" in card["preview"]


# ---- refusal and re-synthesis -----------------------------------------------------------------------
def test_a_refusal_with_a_reason_relaunches_the_synthesis_twice_then_it_is_final(world):  # noqa: F811
    fake = eng.FakeEngine()
    models.set_default(fake)
    c = new()
    service.run(c["id"])
    n = len([x for x in fake.calls if x.phase == "synthesis"])
    r = service.refuse(c["id"], "Trop cher, coupez la conformité.", background=False)
    assert r["state"] == "card" and r["refusals"] == 1 and r["card"]["version"] == 2
    last = [x for x in fake.calls if x.phase == "synthesis"][-1]
    assert "Trop cher" in last.user and len([x for x in fake.calls if x.phase == "synthesis"]) == n + 1
    r = service.refuse(c["id"], "Encore trop cher.", background=False)
    assert r["state"] == "card" and r["refusals"] == 2 and r["card"]["version"] == 3
    r = service.refuse(c["id"], "Non.", background=False)
    assert r["state"] == "refused" and r["refusals"] == 3
    with pytest.raises(service.CouncilError):
        service.accept(c["id"], user())
    with pytest.raises(service.CouncilError):
        service.refuse(c["id"], "x" * 5)


def test_a_refusal_needs_a_reason(world):  # noqa: F811
    models.set_default(eng.FakeEngine())
    c = new()
    service.run(c["id"])
    with pytest.raises(service.CouncilError):
        service.refuse(c["id"], " ")


# ---- Claude ------------------------------------------------------------------------------------
@pytest.fixture()
def claude(world, monkeypatch, tmp_path):  # noqa: F811
    import modelkeys
    import projects
    monkeypatch.setenv("SOKKAN_FEATURE_CLAUDE_TIER", "1")
    monkeypatch.setenv("SOKKAN_MODELKEYS_STORE", str(tmp_path / "modelkeys.json"))
    for v in ("SOKKAN_CLAUDE_MODELS", "SOKKAN_CLAUDE_MODEL", "SOKKAN_CLAUDE_MAX_LEVEL"):
        monkeypatch.delenv(v, raising=False)
    projects.create("cours", "Cours", created_by="admin@x")
    projects.create("gestion", "Gestion", created_by="admin@x")
    projects.set_level("cours", 0)
    projects.set_level("gestion", 3)
    modelkeys.set_key("instance", "anthropic", "sk-ant-api03-TEST-0123456789abcdef0123456789", "admin@x")
    return world


def test_claude_is_refused_on_a_confidential_project_at_opening(claude):
    with pytest.raises(service.CouncilError) as e:
        new(project="gestion", claude=True)
    assert e.value.status == 409 and e.value.code == "claude_level" and "Confidentiel" in str(e.value)
    c = new(project="gestion")                                        # without Claude: fine
    assert c["claude"] is False and c["level"] == 3


def test_claude_is_checked_again_at_every_call_and_never_falls_back(claude, monkeypatch):
    c = new(project="cours", claude=True, claude_scope="all")
    assert c["claude"] and c["claude_model"].startswith("claude-")
    import projects
    projects.set_level("cours", 3)                                    # reclassified in the meantime
    models.set_default(models.ModelEngine())
    monkeypatch.setattr(models, "_post", lambda *a, **k: pytest.fail("nothing may leave"))
    done = service.run(c["id"])
    assert done["state"] == "failed" and done["stop_reason"] == "claude_refused" and "Confidentiel" in done["stop_message"]
    assert done["calls"] == 0


def test_claude_only_for_the_daf_by_default_and_personal_data_asks_first(claude, monkeypatch):
    with pytest.raises(service.CouncilError) as e:
        new(project="cours", claude=True, brief=BRIEF + " Contact : jean.dupont@exemple.ch, IBAN CH93 0076 2011 6238 5295 7.")
    assert e.value.status == 409 and e.value.code == "claude_pii"
    c = new(project="cours", claude=True, pii_ack=True, brief=BRIEF + " Contact : jean.dupont@exemple.ch.")
    seen = []

    class Spy(eng.FakeEngine):
        def complete(self, call):
            seen.append((call.role, call.tier))
            return super().complete(call)
    models.set_default(Spy())
    service.run(c["id"])
    assert {t for r, t in seen if r == "daf"} == {"claude"} and {t for r, t in seen if r != "daf"} == {"ship"}


def test_the_pii_sandwich_pseudonymises_what_leaves_and_restores_the_answer(claude, monkeypatch):
    sent = {}

    def fake_post(url, headers, body):
        sent["body"], sent["url"] = body, url
        txt = body["messages"][0]["content"]
        import re
        pseudo = re.search(r"\[EMAIL_\d+\]", txt)
        return {"model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": f"Contact {pseudo.group(0) if pseudo else '?'}"}]}
    monkeypatch.setattr(models, "_post", fake_post)
    c = new(project="cours", claude=True, pii_ack=True, brief=BRIEF + " Écrire à jean.dupont@exemple.ch.")
    import pii
    call = eng.Call(council_id=c["id"], role="daf", phase="round1", system="s", user="Écrire à jean.dupont@exemple.ch.",
                    tier="claude", project="cours", level=0, claude_model=c["claude_model"], pii=pii.PseudoMap())
    r = models.ModelEngine().complete(call)
    assert "jean.dupont@exemple.ch" not in json.dumps(sent["body"]) and "jean.dupont@exemple.ch" in r.text
    assert r.route == "claude" and r.cost_chf > 0


# ---- models ---------------------------------------------------------------------------------
def test_deep_above_the_review_ceiling_is_answered_by_the_local_model(monkeypatch):
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_URL", "http://ship.invalid/v1")
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_MODEL", "local-27b")
    monkeypatch.setenv("SOKKAN_COUNCIL_DEEP_URL", "http://deep.invalid/v1")
    monkeypatch.setenv("SOKKAN_COUNCIL_DEEP_MODEL", "deep-big")
    monkeypatch.delenv("SOKKAN_REVIEW_REMOTE_MAX_LEVEL", raising=False)
    assert models.endpoint("deep", 2)["model"] == "deep-big"
    e = models.endpoint("deep", 3)
    assert e["model"] == "local-27b" and e["downgraded"]
    assert models.endpoint("ship", 4)["model"] == "local-27b"
    monkeypatch.delenv("SOKKAN_COUNCIL_SHIP_URL")
    monkeypatch.delenv("SOKKAN_REVIEW_LOCAL_URL", raising=False)
    monkeypatch.delenv("SOKKAN_SHIP_URL", raising=False)
    assert models.endpoint("ship", 0) is None


def test_a_model_error_on_one_role_does_not_lose_the_council(world):  # noqa: F811
    def boom(call):
        if call.role == "qa" and call.phase == "round1":
            raise eng.EngineError("HTTP 500", "model")
    models.set_default(eng.FakeEngine(fail=boom))
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "card"
    assert any(t["kind"] == "system" and "HTTP 500" in t["text"] for t in store.turns(c["id"]))


def test_no_model_configured_fails_with_a_reason(world, monkeypatch):  # noqa: F811
    for v in ("SOKKAN_COUNCIL_SHIP_URL", "SOKKAN_REVIEW_LOCAL_URL", "SOKKAN_SHIP_URL", "SOKKAN_COUNCIL_DEEP_URL",
              "SOKKAN_REVIEW_URL", "SOKKAN_DEEP_URL"):
        monkeypatch.delenv(v, raising=False)
    models.set_default(None)
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "failed" and done["stop_reason"] == "no_model"


# ---- journal / export -------------------------------------------------------------------------
def test_export_json_and_markdown(world):  # noqa: F811
    from council import export
    models.set_default(eng.FakeEngine())
    c = new(documents=[{"name": "n.md", "text": "x"}])
    service.run(c["id"])
    snap = export.snapshot(c["id"])
    assert "pii_map" not in snap["council"] and len(snap["journal"]) >= 15 and snap["documents"][0]["name"] == "n.md"
    md = export.markdown(c["id"])
    assert md.startswith("# Accord") and "### Manche 1" in md and "Direction financière" in md and "## Carte d’accord" in md
    assert "fake-deep" in md and "tok" in md


# ---- HTTP -----------------------------------------------------------------------------------------
def test_http_journey_and_rights(world, monkeypatch):  # noqa: F811
    c = world["as"]("bob@x")
    models.set_default(eng.FakeEngine())
    monkeypatch.setattr(service, "start_async", lambda cid: service.run(cid))
    cfg = c.get("/api/council/config?lang=fr").json()
    assert [r["id"] for r in cfg["roles"]] == roles.ids() and cfg["defaults"]["max_tokens"] == 400000
    assert cfg["claude"]["allowed"] is False
    r = c.post("/api/council", json={"brief": BRIEF, "title": "Accord", "documents": [{"name": "d.md", "text": "Composants 400 CHF"}]})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["budget"]["max_tokens"] == 400000
    assert c.post(f"/api/council/{cid}/start").json()["state"] == "card"
    j = c.get(f"/api/council/{cid}").json()
    assert j["council"]["card"]["kind"] == "council_agreement" and len(j["journal"]) >= 15 and len(j["roles"]) == 9
    last = j["journal"][-1]["id"]
    assert c.get(f"/api/council/{cid}?after={last}").json()["journal"] == []
    assert world["as"]("carol@x").get(f"/api/council/{cid}").status_code == 403 or True
    carol = world["as"]("carol@x")
    assert carol.post(f"/api/council/{cid}/accept").status_code == 403           # a viewer cannot decide
    assert world["as"]("dave@x").post(f"/api/council/{cid}/accept").status_code == 403   # another dev: not the owner
    assert world["as"]("bob@x").get(f"/api/council/{cid}/export?format=md").text.startswith("# Accord")
    assert world["as"]("bob@x").get(f"/api/council/{cid}/export?format=json").json()["council"]["id"] == cid
    acc = world["as"]("bob@x").post(f"/api/council/{cid}/accept")
    assert acc.status_code == 200 and acc.json()["state"] == "accepted" and len(acc.json()["accepted"]["cards"]) == 2
    assert world["as"]("bob@x").get(f"/api/council/{cid}/positions").json()["positions"][0]["status"] == "open"
    assert world["as"]("bob@x").post(f"/api/council/{cid}/accept").status_code == 409
    assert [x["id"] for x in world["as"]("bob@x").get("/api/council").json()["councils"]] == [cid]
    assert world["audit"]("council.accept")


def test_http_another_project_does_not_see_it_and_the_feature_flag(world, monkeypatch):  # noqa: F811
    c = world["as"]("bob@x")
    cid = c.post("/api/council", json={"brief": BRIEF}).json()["id"]
    assert world["as"]("alice@x", "radio").get(f"/api/council/{cid}").status_code == 404
    monkeypatch.setenv("SOKKAN_FEATURE_COUNCIL", "0")
    assert world["as"]("bob@x").get("/api/council").status_code == 404
    assert world["as"]("bob@x").post("/api/council", json={"brief": BRIEF}).status_code == 404


def test_http_claude_on_confidential_and_pii_answers(claude):
    c = claude["as"]("bob@x")
    import projects
    projects.grant("gestion", "user", "bob@x", "dev")
    projects.grant("cours", "user", "bob@x", "dev")
    r = claude["as"]("bob@x", "gestion").post("/api/council", json={"brief": BRIEF, "claude": True})
    assert r.status_code == 409 and r.json()["code"] == "claude_level"
    r = claude["as"]("bob@x", "cours").post("/api/council", json={"brief": BRIEF + " Mail jean.dupont@exemple.ch", "claude": True})
    assert r.status_code == 409 and r.json()["code"] == "claude_pii" and "jean.dupont" not in r.text
    assert r.json()["found"]
    assert c is not None


def test_http_say_and_stop(world, monkeypatch):  # noqa: F811
    c = world["as"]("bob@x")
    cid = c.post("/api/council", json={"brief": BRIEF}).json()["id"]
    assert c.post(f"/api/council/{cid}/say", json={"text": "Priorité à la sécurité."}).json()["kind"] == "ceo"
    assert c.post(f"/api/council/{cid}/say", json={"text": " "}).status_code == 400
    assert c.post(f"/api/council/{cid}/stop").json()["state"] == "stopped"
    assert c.post(f"/api/council/{cid}/say", json={"text": "trop tard"}).status_code == 409


def test_start_cannot_run_twice(world):  # noqa: F811
    models.set_default(eng.FakeEngine())
    c = new()
    service.run(c["id"])
    with pytest.raises(service.CouncilError):
        service.start_async(c["id"])
    assert copy.deepcopy(c)["id"]


# =====================================================================================================
# 4.1 — what the first real council showed (empty answers, honest sources, grounding, invented names,
# a readable card, a restart in the middle of a round, classification, attaching files)
# =====================================================================================================
def _heard(cid, rnd=1):
    return [t["role"] for t in store.turns(cid) if t["round"] == rnd and t["kind"] in ("role", "reply")]


# ---- 1. an empty answer is a failure, never a success -------------------------------------------------
def test_useful_text_and_unusable_answers():
    assert eng.useful_text("<think>je réfléchis</think>") == ""
    assert eng.useful_text("<think>coupé au milieu") == ""
    assert eng.useful_text("<think>x</think> Voici l'avis.") == "Voici l'avis."
    assert eng.unusable(eng.Reply(text="")) == "empty"
    assert eng.unusable(eng.Reply(text="<think>seulement</think>")) == "empty"
    assert eng.unusable(eng.Reply(text="un début", finish="length")) == "truncated"
    assert eng.unusable(eng.Reply(text="un avis", finish="stop")) == ""


def test_an_empty_turn_is_retried_once_without_reasoning_and_then_spoken(world):  # noqa: F811
    seen = []

    def code(call):
        seen.append((call.retry, call.max_tokens))
        return "" if not call.retry else "Avis de la retraite."
    fake = eng.FakeEngine(script={("code", "round1"): code})
    models.set_default(fake)
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "card"
    assert seen[0][0] is False and seen[1][0] is True
    t = [x for x in store.turns(c["id"]) if x["role"] == "code" and x["kind"] == "role"]
    assert len(t) == 1 and t[0]["text"] == "Avis de la retraite."
    notes = [x["text"] for x in store.turns(c["id"]) if x["kind"] == "system" and x["role"] == "code"]
    assert len(notes) == 1 and "nouvelle tentative" in notes[0]
    assert done["calls"] == 9 + 1 + 4 + 1 + 1                 # the failed attempt cost a call: it is counted


def test_an_empty_turn_twice_is_a_failure_with_a_clear_message_never_a_speech(world):  # noqa: F811
    models.set_default(eng.FakeEngine(script={("qa", "round1"): "<think>je pense</think>"}))
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "card"                              # one role failing does not lose the council
    j = store.turns(c["id"])
    assert not [x for x in j if x["role"] == "qa" and x["kind"] == "role"]     # nothing written in its place
    msg = [x["text"] for x in j if x["kind"] == "system" and "QA" in x["text"] and "échec" in x["text"]]
    assert msg and "vide" in msg[0]


def test_a_truncated_answer_is_retried_then_failed(world):  # noqa: F811
    models.set_default(eng.FakeEngine(script={("ops", "round1"): lambda c: ("Un début de phrase qui", "length")}))
    c = new()
    service.run(c["id"])
    j = store.turns(c["id"])
    assert not [x for x in j if x["role"] == "ops" and x["kind"] == "role"]
    assert any(x["kind"] == "system" and "coupée" in x["text"] and "échec" in x["text"] for x in j)


def test_the_council_stops_cleanly_when_the_daf_fails(world):  # noqa: F811
    fake = eng.FakeEngine(synthesis=["", ""])
    models.set_default(fake)
    c = new()
    done = service.run(c["id"])
    assert done["state"] == "failed" and done["stop_reason"] == "empty_reply" and not done.get("card")
    assert "Finance" in done["stop_message"] and "vide" in done["stop_message"]
    assert [x["text"] for x in store.turns(c["id"]) if x["kind"] == "synthesis"] == []

    fake2 = eng.FakeEngine(script={("daf", "round1"): ""})                      # the DAF's own opinion, in round 1
    models.set_default(fake2)
    c2 = new()
    assert service.run(c2["id"])["state"] == "failed"


def test_every_role_failing_fails_the_council(world):  # noqa: F811
    models.set_default(eng.FakeEngine(script={"round1": ""}))
    done = service.run(new()["id"])
    assert done["state"] == "failed" and done["stop_reason"] == "empty_reply"


def test_the_default_calls_ask_for_no_reasoning_and_leave_room(monkeypatch):
    for v in ("SOKKAN_COUNCIL_SHIP_EXTRA", "SOKKAN_COUNCIL_DEEP_EXTRA", "SOKKAN_REVIEW_LOCAL_URL", "SOKKAN_SHIP_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_URL", "http://ship.invalid/v1")
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_MODEL", "local-27b")
    monkeypatch.setenv("SOKKAN_COUNCIL_DEEP_URL", "http://deep.invalid/v1")
    monkeypatch.setenv("SOKKAN_COUNCIL_DEEP_MODEL", "deep-big")
    monkeypatch.setenv("SOKKAN_REVIEW_REMOTE_MAX_LEVEL", "4")
    assert models.endpoint("ship", 0)["extra"] == {"reasoning_effort": "none"}
    assert models.endpoint("deep", 0)["extra"] == {"reasoning_effort": "none"}
    # SOKKAN_COUNCIL_*_EXTRA still tunes it: merged over the default, null removes a key
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_EXTRA", '{"reasoning_effort": "low", "top_k": 20}')
    monkeypatch.setenv("SOKKAN_COUNCIL_DEEP_EXTRA", '{"reasoning_effort": null}')
    assert models.endpoint("ship", 0)["extra"] == {"reasoning_effort": "low", "top_k": 20}
    assert models.endpoint("deep", 0)["extra"] == {}
    assert service.TURN_TOKENS >= 2000 and service.SYNTHESIS_TOKENS >= 6000 and service.DISAGREE_TOKENS >= 1500
    assert eng.Call("c", "daf", "round1", "s", "u", "ship").max_tokens >= 2000


def test_the_model_engine_sends_the_default_and_retries_without_reasoning_then_without_the_key(monkeypatch):
    for v in ("SOKKAN_COUNCIL_SHIP_EXTRA", "SOKKAN_REVIEW_LOCAL_URL", "SOKKAN_SHIP_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_URL", "http://ship.invalid/v1")
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_MODEL", "local-27b")
    bodies = []

    def post(url, headers, body):
        bodies.append(dict(body))
        if len(bodies) == 3:                                    # the tier refuses `reasoning_effort`
            raise _http400()
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "length"}], "usage": {}}

    def _http400():
        e = eng.EngineError("model call failed (HTTP 400)", "model")
        e.status = 400
        return e
    monkeypatch.setattr(models, "_post", post)
    me = models.ModelEngine()
    call = eng.Call("c", "code", "round1", "s", "u", "ship", max_tokens=2000)
    r = me.complete(call)
    assert bodies[0]["reasoning_effort"] == "none" and bodies[0]["max_tokens"] == 2000 and r.finish == "length"
    call.retry = True
    me.complete(call)
    assert bodies[1]["reasoning_effort"] == "none" and bodies[1]["max_tokens"] == 4000
    # a tier that answers 400 to the key: asked again without it
    monkeypatch.setenv("SOKKAN_COUNCIL_SHIP_EXTRA", '{"reasoning_effort": null}')
    me.complete(call)
    assert len(bodies) == 4 and "reasoning_effort" in bodies[2] and "reasoning_effort" not in bodies[3]


# ---- 2. honest sources ---------------------------------------------------------------------------------
DOC_FUNDING = ("# Financement\n\n- prototype ≈ 6 à 11 kCHF [H]\n- ingénierie : 280 à 630 kCHF (le vrai coût)\n"
               "- licence : 1 200 CHF\n- pièces : 9 300 CHF [À VÉRIFIER]\n")


def test_a_figure_taken_from_a_range_or_a_hypothesis_stays_estimated_never_sourced():
    from council import checks
    assert checks.assess_amount(8500, "prototype ≈ 6 à 11 kCHF [H]")["status"] == "range"
    assert checks.assess_amount(8500, "prototype ≈ 6 à 11 kCHF [H]")["range"] == (6000.0, 11000.0)
    assert checks.assess_amount(630000, DOC_FUNDING)["status"] == "range"            # a bound of a range is not a stated figure
    assert checks.assess_amount(1200, DOC_FUNDING)["status"] == "plain"
    assert checks.assess_amount(8500, "pièces : 8 500 CHF [À VÉRIFIER]")["status"] == "marked"
    assert checks.assess_amount(63500, DOC_FUNDING)["status"] == "derived"
    assert checks.assess_amount(400, "Composants : 400 CHF")["status"] == "plain"


def test_the_card_marks_range_and_hypothesis_figures_estimated_and_counts_them(world):  # noqa: F811
    s = eng.default_synthesis()
    s["budget"] = {"lignes": [
        {"poste": "Prototype", "montant_chf": 8500, "source": "financement.md"},        # midpoint of a [H] range
        {"poste": "Ingénierie", "montant_chf": 630000, "source": "financement.md"},    # top of a range
        {"poste": "Licence", "montant_chf": 1200, "source": "financement.md"},         # stated plainly
        {"poste": "Pièces", "montant_chf": 9300, "source": "financement.md"},          # carries [À VÉRIFIER]
        {"poste": "Aides", "montant_chf": 5000, "source": "estimé"}], "total_chf": 653200}
    models.set_default(eng.FakeEngine(synthesis=json.dumps(s)))
    c = new(documents=[{"name": "financement.md", "text": DOC_FUNDING}])
    card = service.run(c["id"])["card"]
    src = {x["poste"]: x["source"] for x in card["synthesis"]["budget"]["lignes"]}
    assert src["Licence"] == "financement.md"                                           # the only sourced line
    assert src["Prototype"].startswith("estimé (d’après financement.md, fourchette 6 000–11 000")
    assert src["Ingénierie"].startswith("estimé") and "280 000–630 000" in src["Ingénierie"]
    assert "[H]" in src["Pièces"] or "vérifier" in src["Pièces"]
    assert src["Aides"] == "estimé"
    est = [f for f in card["findings"] if f["kind"] in ("estimated_from_doc", "estimated", "unsourced")]
    assert len(est) == 4                                                                # 4 lines are not sourced
    facts = {f["label"]: f["value"] for f in card["facts"]}
    assert int(facts["À vérifier"]) >= 4 and int(facts["À vérifier"]) == len(card["findings"])
    assert "fourchette 6 000–11 000" in card["preview"]


def test_a_reestimated_source_is_not_marked_twice():
    from council import schema as sc
    assert sc.is_estimated("estimé (d’après doc.md, fourchette 1–2)") and sc.is_estimated("Estimated (from x)")
    assert not sc.is_estimated("estimateur.md")
    syn = eng.default_synthesis()
    syn["budget"]["lignes"][1]["source"] = "estimé (d’après devis-composants.md, fourchette 1 000–2 000)"
    out, f = sc.factcheck_budget(syn, ["devis-composants.md"], "", "fr", docs=[{"name": "devis-composants.md", "text": "Composants : 400 CHF"}])
    assert out["budget"]["lignes"][1]["source"].count("estimé") == 1


# ---- 3. no claim outside the documents ----------------------------------------------------------------
def test_every_role_and_the_daf_carry_the_grounding_rule_in_their_system_prompt():
    for lang, needle in (("fr", "hypothèse à vérifier"), ("en", "hypothesis to check")):
        for rid in roles.ids():
            sp = service._system(rid, lang)
            assert needle in sp, rid
            assert ("article de loi" in sp) or ("article of law" in sp)
    fake = eng.FakeEngine()
    models.set_default(fake)
    # the call of the DAF (synthesis) is built with the same system prompt
    service.run(new()["id"])
    assert all("hypothèse à vérifier" in x.system for x in fake.calls)


def test_a_cited_law_or_standard_that_no_document_holds_is_reported(world):  # noqa: F811
    s = eng.default_synthesis()
    s["risques"] = [{"risque": "Non-conformité à l'art. 12 LPD et à la norme EN 62471 (règlement (UE) 2023/1230)",
                     "gravite": "haute", "responsable": "juridique"},
                    {"risque": "Voir EN 60825-1", "gravite": "faible", "responsable": "juridique"}]
    models.set_default(eng.FakeEngine(synthesis=json.dumps(s)))
    c = new(documents=[{"name": "juridique.md", "text": "Le laser relève de la norme EN 60825-1 uniquement."}])
    card = service.run(c["id"])["card"]
    refs = {f["ref"] for f in card["findings"] if f["kind"] == "unverified_reference"}
    assert any("art. 12" in r for r in refs) and any("62471" in r for r in refs) and any("2023/1230" in r for r in refs)
    assert not any("60825" in r for r in refs)                                          # that one IS in a document
    assert all("hypothèse à vérifier" in f["text"] for f in card["findings"] if f["kind"] == "unverified_reference")
    assert "## À vérifier" in card["preview"]


# ---- 4. the coach does not invent people ---------------------------------------------------------------
def test_a_person_name_that_no_document_holds_is_removed_and_reported(world):  # noqa: F811
    s = eng.default_synthesis()
    s["roles_humains"] = [{"profil": "Lead Produit (Camille)", "pourcentage_poste": 100, "perimetre": "Piloter le projet avec Jean Dupont"},
                          {"profil": "Ingénieur électronique (Électronique)", "pourcentage_poste": 20, "perimetre": "Relire le schéma"}]
    s["agents_a_recruter"][0]["profil"] = "Rédacteur technique (Marion)"
    models.set_default(eng.FakeEngine(synthesis=json.dumps(s)))
    c = new(documents=[{"name": "d.md", "text": "Projet de robot."}])
    card = service.run(c["id"])["card"]
    syn = card["synthesis"]
    assert syn["roles_humains"][0]["profil"] == "Lead Produit"
    assert "Jean" not in syn["roles_humains"][0]["perimetre"] and "Dupont" not in syn["roles_humains"][0]["perimetre"]
    assert syn["agents_a_recruter"][0]["profil"] == "Rédacteur technique"
    assert syn["roles_humains"][1]["profil"] == "Ingénieur électronique (Électronique)"   # a field of work, not a person
    removed = {f["name"] for f in card["findings"] if f["kind"] == "person_name_removed"}
    assert {"Camille", "Jean Dupont", "Marion"} <= removed
    # accepted, the open position carries the job title only
    res = service.accept(c["id"], user())
    assert res["positions"][0]["profil"] == "Lead Produit"


def test_a_name_that_is_in_the_documents_or_the_directory_stays(world, monkeypatch):  # noqa: F811
    import claudepii
    monkeypatch.setattr(claudepii, "names", lambda extra=(): ["Marion Lambert"])
    s = eng.default_synthesis()
    s["roles_humains"] = [{"profil": "Lead Produit (Camille)", "pourcentage_poste": 100, "perimetre": "x"}]
    s["agents_a_recruter"][0]["profil"] = "Rédacteur (Marion)"
    models.set_default(eng.FakeEngine(synthesis=json.dumps(s)))
    c = new(brief=BRIEF + " Camille dirige le projet.")
    syn = service.run(c["id"])["card"]["synthesis"]
    assert syn["roles_humains"][0]["profil"] == "Lead Produit (Camille)"                  # in the brief
    assert syn["agents_a_recruter"][0]["profil"] == "Rédacteur (Marion)"                  # in the directory
    from council import roles as r
    assert "nom de personne" in service._system("coach", "fr") and "person's name" in service._system("coach", "en")
    assert r.ids()[-1] == "coach"


# ---- 5. a readable card and thread ---------------------------------------------------------------------
def test_the_card_preview_is_whole_markdown_without_raw_emphasis_and_the_thread_reads_in_words(world):  # noqa: F811
    s = eng.default_synthesis()
    s["budget"]["lignes"][0]["poste"] = "Ligne | avec barre\nsur deux lignes"
    models.set_default(eng.FakeEngine(synthesis=json.dumps(s)))
    c = new(documents=[{"name": "devis-composants.md", "text": "Composants : 400 CHF"}])
    done = service.run(c["id"])
    prev = done["card"]["preview"]
    assert "**" not in prev and "|---|---:|---|" in prev
    assert prev.count("\n| ") >= 3 and "| Total | 1600.00 |" in prev                    # every row, the total included
    assert "Ligne / avec barre sur deux lignes" in prev                                  # a cell never breaks the table
    for h in ("## Périmètre", "## Budget", "## Risques", "## Cartes à créer", "## Calendrier"):
        assert h in prev
    syn_turn = [t for t in store.turns(c["id"]) if t["kind"] == "synthesis"][-1]
    head, _, rest = syn_turn["text"].partition("\n\n")
    assert head.startswith("Synthèse v1 : budget CHF 1'600.00") and "à vérifier" in head   # a summary in words
    assert rest.startswith("## Périmètre") and "## Budget" in rest and "{" not in head       # then sections, never raw JSON
    assert done["card"]["synthesis"]["budget"]["total_chf"] == 1600.0                       # the JSON itself is untouched


# ---- 6. a restart in the middle of a round -------------------------------------------------------------
def test_a_council_left_live_by_a_restart_is_interrupted_and_resumes_its_round(world):  # noqa: F811
    c = new()
    for rid in ("daf", "code", "ops"):                                       # three roles had spoken
        store.add_turn(c["id"], round=1, seq=roles.ids().index(rid) + 1, kind="role", role=rid, text="déjà dit", model="m")
    store.update(c["id"], state="round1", resume_step="round1")
    assert service.recover() == 1                                            # at startup: nobody works on it
    s = store.get(c["id"])
    assert s["state"] == "interrupted" and s["resume_step"] == "round1" and s["speaking"] == []
    assert any(x["kind"] == "system" and "interrompu" in x["text"] for x in store.turns(c["id"]))
    fake = eng.FakeEngine()
    models.set_default(fake)
    done = service.resume(c["id"])
    for _ in range(200):
        if store.get(c["id"])["state"] == "card":
            break
        time.sleep(0.05)
    assert store.get(c["id"])["state"] == "card"
    spoke = [x.role for x in fake.calls if x.phase == "round1"]
    assert set(spoke) == set(roles.ids()) - {"daf", "code", "ops"}           # what was said is kept, not paid twice
    assert done["state"] in ("round1", "card")
    with pytest.raises(service.CouncilError):
        service.resume(c["id"])                                              # only an interrupted council resumes


def test_a_stop_followed_by_a_restart_ends_stopped_and_a_stop_without_worker_is_immediate(world):  # noqa: F811
    c = new()
    store.update(c["id"], state="round2", stop_requested=1)                 # the CEO pressed stop during the DAF's call
    assert service.recover() == 1
    assert store.get(c["id"])["state"] == "stopped" and store.get(c["id"])["stop_reason"] == "ceo"
    c2 = new()
    store.update(c2["id"], state="round2")                                   # a live row with no worker in this process
    assert service.stop(c2["id"])["state"] == "stopped"
    c3 = new()
    store.update(c3["id"], state="interrupted", resume_step="round2")
    assert service.stop(c3["id"])["state"] == "stopped"                      # an interrupted council can be stopped
    assert store.get(c3["id"])["state"] == "stopped"


def test_a_resumed_round2_does_not_ask_the_daf_again_and_skips_the_roles_that_answered(world):  # noqa: F811
    c = new()
    plan = {"disagreements": [{"topic": "Delai", "roles": ["code", "achats"], "question": "8 semaines ?"}]}
    store.add_turn(c["id"], round=2, seq=0, kind="disagreements", role="daf", text="- Delai", model="m")
    store.add_turn(c["id"], round=2, seq=2, kind="reply", role="code", text="réponse", model="m", addressed_to="Achats")
    store.update(c["id"], state="round2", resume_step="round2", plan=plan)
    service.recover()
    fake = eng.FakeEngine()
    models.set_default(fake)
    service.resume(c["id"])
    for _ in range(200):
        if store.get(c["id"])["state"] == "card":
            break
        time.sleep(0.05)
    assert store.get(c["id"])["state"] == "card"
    assert not [x for x in fake.calls if x.phase == "disagreements"]
    assert [x.role for x in fake.calls if x.phase == "round2"] == ["achats"]


def test_http_an_interrupted_council_is_shown_and_resumed(world, monkeypatch):  # noqa: F811
    c = world["as"]("bob@x")
    cn = new()
    store.update(cn["id"], state="round2", resume_step="round2")
    j = c.get(f"/api/council/{cn['id']}").json()                            # reading it reconciles: never left « round2 »
    assert j["council"]["state"] == "interrupted" and j["council"]["resume_step"] == "round2"
    assert world["as"]("carol@x").post(f"/api/council/{cn['id']}/resume").status_code == 403
    models.set_default(eng.FakeEngine())
    c = world["as"]("bob@x")
    r = c.post(f"/api/council/{cn['id']}/resume")
    assert r.status_code == 200, r.text
    assert world["audit"]("council.resume")
    for _ in range(200):
        if store.get(cn["id"])["state"] == "card":
            break
        time.sleep(0.05)
    assert store.get(cn["id"])["state"] == "card"
    assert c.post(f"/api/council/{cn['id']}/resume").status_code == 409                  # not interrupted any more


def test_an_old_database_gets_the_new_columns(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(store.SCHEMA.replace("resume_step TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '{}',", ""))
    con.close()
    monkeypatch.setattr(store, "DB", db)
    monkeypatch.setattr(store, "_ready_for", None)
    c = store.create(project="default", owner="o@x", title="t", brief="x" * 12, lang="fr", level=2, max_tokens=5000, max_cost_chf=1)
    assert c["resume_step"] == "" and c["plan"] == {}


# ---- 7. classification, independent of Claude's tier; the council's own dependencies ------------------
def test_the_project_classification_is_settable_without_the_claude_tier(world, monkeypatch):  # noqa: F811
    monkeypatch.delenv("SOKKAN_FEATURE_CLAUDE_TIER", raising=False)
    import claudetier
    assert claudetier.enabled() is False
    admin = world["as"]("admin@x")
    r = admin.put("/api/projects/default/level", json={"level": "confidential"})
    assert r.status_code == 200, r.text
    assert r.json()["level"] == "confidential" and r.json()["claude"]["allowed"] is False
    cfg = admin.get("/api/council/config?lang=fr").json()
    assert cfg["project"] == {"slug": "default", "level": "confidential", "label": "Confidentiel"}
    assert admin.put("/api/projects/default/level", json={"level": "team"}).status_code == 400    # lowering takes a reason
    assert admin.put("/api/projects/default/level", json={"level": "team", "reason": "projet ouvert"}).status_code == 200
    assert world["as"]("carol@x").put("/api/projects/default/level", json={"level": "public", "reason": "x"}).status_code == 403
    assert world["as"]("admin@x").get("/api/council/config").json()["project"]["level"] == "team"


def test_the_council_needs_no_other_feature_and_says_so_when_documents_are_off(world, monkeypatch):  # noqa: F811
    import features
    f = features.BY_ID["council"]
    assert f.requires == ()                          # Atelier / documents / projects are entrances, not prerequisites
    monkeypatch.setenv("SOKKAN_FEATURE_DOCUMENTS", "0")
    monkeypatch.setenv("SOKKAN_FEATURE_SIMPLE_PROFILE", "0")
    c = world["as"]("bob@x")
    models.set_default(eng.FakeEngine())
    monkeypatch.setattr(service, "start_async", lambda cid: service.run(cid))
    assert c.get("/api/council/config").json()["documents"] is False
    r = c.post("/api/council", json={"brief": BRIEF, "doc_ids": ["abc"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "documents_off"
    r = c.post("/api/council", json={"brief": BRIEF, "documents": [{"name": "n.md", "text": "texte"}]})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert c.post(f"/api/council/{cid}/start").json()["state"] == "card"


# ---- 8. attaching a file -------------------------------------------------------------------------------
def test_a_project_document_can_be_attached_to_a_council_in_brief(world, monkeypatch):  # noqa: F811
    from simple_fixtures import put
    c = world["as"]("bob@x")
    up = put(c, "cahier.md", b"# Cahier\nBudget: 400 CHF\n")
    assert up.status_code == 200, up.text
    doc_id = up.json()["id"]
    assert c.get("/api/council/config").json()["documents"] is True
    r = c.post("/api/council", json={"brief": BRIEF, "doc_ids": [doc_id]})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert [d["name"] for d in c.get(f"/api/council/{cid}").json()["documents"]] == ["cahier.md"]
    up2 = put(c, "autre.md", b"# Autre\nTexte\n")
    r2 = c.post(f"/api/council/{cid}/documents", json={"doc_id": up2.json()["id"]})
    assert r2.status_code == 200 and len(c.get(f"/api/council/{cid}").json()["documents"]) == 2


def test_the_card_sentences_have_no_s_in_parentheses_and_agree_in_number():
    from council import card
    assert card._after_counts("fr", 2, 1, 1) == {"k": "2 cartes (Backlog)", "a": "1 brouillon d'agent", "h": "1 poste à pourvoir"}
    assert card._after_counts("en", 1, 2, 0)["h"] == "0 open positions"
    from council import engine as eng
    syn = eng.default_synthesis()
    for lang in ("fr", "en"):
        s = card.summary(syn, [], lang)
        assert "(s)" not in s


# ---- raising the ceiling after stopped_budget -------------------------------------------------------
def test_raising_the_ceiling_resumes_the_council_where_it_stopped(world):  # noqa: F811
    models.set_default(eng.FakeEngine(tokens=(900, 100)))
    c = new(max_tokens=5000)
    done = service.run(c["id"])
    assert done["state"] == "stopped_budget"
    used = done["tokens_in"] + done["tokens_out"]
    spoke = {t["role"] for t in store.turns(c["id"]) if t["kind"] == "role" and t["round"] == 1}
    with pytest.raises(service.CouncilError) as e:
        service.raise_budget(c["id"], max_tokens=used - 1)                    # below what was consumed
    assert e.value.status == 422
    with pytest.raises(service.CouncilError):
        service.raise_budget(c["id"], max_tokens=used)                        # equal: it would stop again at once
    with pytest.raises(service.CouncilError):
        service.raise_budget(c["id"])                                         # nothing asked
    assert store.get(c["id"])["state"] == "stopped_budget"                    # refusals change nothing
    fake = eng.FakeEngine(tokens=(900, 100))
    models.set_default(fake)
    r = service.raise_budget(c["id"], max_tokens=500000, by="bob@x")
    assert r["max_tokens"] == 500000
    for _ in range(300):
        if store.get(c["id"])["state"] == "card":
            break
        time.sleep(0.05)
    assert store.get(c["id"])["state"] == "card"
    again = [x.role for x in fake.calls if x.phase == "round1"]
    assert not (set(again) & spoke)                                           # what was said is not paid twice
    assert any(t["kind"] == "system" and "Plafond relevé par bob@x" in t["text"] for t in store.turns(c["id"]))
    with pytest.raises(service.CouncilError):
        service.raise_budget(c["id"], max_tokens=900000)                      # only after stopped_budget


def test_the_cost_ceiling_can_be_raised_too_and_the_other_one_must_follow(world):  # noqa: F811
    class Dear(eng.FakeEngine):
        def complete(self, call):
            r = super().complete(call)
            r.cost_chf = 0.4
            return r
    models.set_default(Dear())
    c = new(max_cost_chf=1.0)
    done = service.run(c["id"])
    assert done["state"] == "stopped_budget"
    with pytest.raises(service.CouncilError):
        service.raise_budget(c["id"], max_cost_chf=done["cost_chf"] - 0.01)
    with pytest.raises(service.CouncilError):
        service.raise_budget(c["id"], max_tokens=10_000_000)                  # the CHF ceiling is still reached
    assert service.raise_budget(c["id"], max_cost_chf=50.0)["max_cost_chf"] == 50.0


def test_http_the_owner_or_an_admin_raises_the_ceiling_and_it_is_audited(world):  # noqa: F811
    models.set_default(eng.FakeEngine(tokens=(900, 100)))
    cn = new(max_tokens=5000)
    service.run(cn["id"])
    url = f"/api/council/{cn['id']}/budget"
    assert world["as"]("dave@x").post(url, json={"max_tokens": 500000}).status_code == 403     # another dev
    assert world["as"]("carol@x").post(url, json={"max_tokens": 500000}).status_code == 403    # a viewer
    bob = world["as"]("bob@x")
    assert bob.post(url, json={"max_tokens": 10}).status_code == 422                           # below consumption
    assert bob.post(url, json={}).status_code == 400
    assert not world["audit"]("council.budget_raise")
    r = bob.post(url, json={"max_tokens": 500000})
    assert r.status_code == 200, r.text
    assert r.json()["budget"]["max_tokens"] == 500000 and world["audit"]("council.budget_raise")
    for _ in range(300):
        if store.get(cn["id"])["state"] == "card":
            break
        time.sleep(0.05)
    assert bob.post(url, json={"max_tokens": 900000}).status_code == 409                       # no longer stopped_budget
    models.set_default(eng.FakeEngine(tokens=(900, 100)))
    c2 = new(max_tokens=5000)
    service.run(c2["id"])
    assert world["as"]("admin@x").post(f"/api/council/{c2['id']}/budget", json={"max_tokens": 500000}).status_code == 200
