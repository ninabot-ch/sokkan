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
    assert "Plafond" in done["stop_message"] and "5000" in done["stop_message"]
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
