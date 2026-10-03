"""Recall bench (memory/core/eval.py) and profile switch (memory/core/switch.py).

Pure parts (metrics, transcript harvesting) always run; the rest needs Postgres
(SOKKAN_TEST_PG_DSN, see test_core_store_pg.py)."""
import hashlib
import json
import math
import os
import uuid

import pytest

from core import eval as ev

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
needs_pg = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


# --------------------------------------------------------------------------- metrics

def test_metrics_match_the_reference_bench():
    m = ev.metrics(["a", "b", "c"], {"b"})
    assert m["rank"] == 2 and m["h1"] == 0 and m["h5"] == 1 and m["mrr"] == 0.5
    assert m["ndcg10"] == pytest.approx(1 / math.log2(3))
    miss = ev.metrics(["a"], {"z"})
    assert miss["rank"] is None and miss["mrr"] == 0 and miss["ndcg10"] == 0
    two = ev.metrics(["x", "y"], {"x", "y"})
    assert two["ndcg10"] == pytest.approx(1.0)


def test_summary_is_weighted():
    rows = [{"h1": 1, "h5": 1, "mrr": 1.0, "ndcg10": 1.0, "weight": 1.0},
            {"h1": 0, "h5": 0, "mrr": 0.0, "ndcg10": 0.0, "weight": 0.25}]
    s = ev.summarize(rows)
    assert s["n"] == 2 and s["mrr"] == pytest.approx(0.8)
    assert ev.summarize([])["mrr"] is None


# --------------------------------------------------------------------------- harvest

def _line(**rec):
    return json.dumps({"sessionId": "s1", "timestamp": "2026-10-01T10:00:00Z", **rec})


def _user(text, **kw):
    return _line(type="user", message={"role": "user", "content": text}, **kw)


def _get(note, tool="mcp__sokkan-memory__memory_get", **kw):
    return _line(type="assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": uuid.uuid4().hex, "name": tool,
         "input": {"note_name": note}}]}, **kw)


def _result():
    return _line(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "body"}]})


def test_harvest_pairs_a_prompt_with_the_notes_read_after_it():
    lines = [
        _user("how do we restore the till backup?"),
        _get("till-backup"), _result(),
        _get("till-backup.md"), _result(),           # same note by file name: once
        _get("harbor-log"), _result(),
        _user("thanks, now something unrelated"),     # no memory_get: no pair
        _line(type="assistant", message={"content": [{"type": "text", "text": "ok"}]}),
        _user("<system-reminder>recall: tide-tables</system-reminder>when is high tide?"),
        _get("tide-tables", tool="mcp__corthexis__memory_get"),
    ]
    out = ev.harvest_lines(lines)
    assert [(h.question, h.expected) for h in out] == [
        ("how do we restore the till backup?", ["till-backup", "harbor-log"]),
        ("when is high tide?", ["tide-tables"]),
    ]
    assert out[0].session_id == "s1"


def test_harvest_ignores_side_chains_commands_meta_and_long_pastes():
    lines = [
        _user("sub-agent brief about roasting", isSidechain=True),
        _get("coffee-roaster-setup", isSidechain=True),
        _user("<command-name>/clear</command-name>"),
        _get("menu-board"),                            # after a command: no prompt
        _user("meta line", isMeta=True),
        _get("menu-board"),
        _user("x" * 900),                              # pasted material
        _get("menu-board"),
        _user("ok"),                                   # too short
        _get("menu-board"),
        _user([{"type": "text", "text": "which roast profile for the house blend?"}]),
        _get("coffee-roaster-setup"), _get("a"), _get("b"), _get("c"),
    ]
    out = ev.harvest_lines(lines)
    assert len(out) == 1
    assert out[0].question == "which roast profile for the house blend?"
    assert out[0].expected == ["coffee-roaster-setup", "a", "b"]   # MAX_EXPECTED


def test_parse_generated_questions():
    text = "1. Where is the tide table kept?\n- How often are olives watered?\ntide-tables?\nok"
    assert ev.parse_questions(text, "tide-tables") == [
        "Where is the tide table kept?", "How often are olives watered?"]


# --------------------------------------------------------------------------- Postgres

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")

NOTES = {
    "till-backup": ("Nightly backup of the till sales", "The till exports the sales every night "
                    "at two. Restore by importing the export file in the till admin page."),
    "tide-tables": ("Harbour tide tables for deliveries", "Boats deliver only at high tide; the "
                    "harbour office prints the monthly tide tables."),
    "coffee-roaster": ("Roaster profile for the house blend", "Medium roast, first crack at nine "
                       "minutes, drum preheated to two hundred degrees."),
    "menu-board": ("Chalk menu board conventions", "Prices in francs, one line per drink, "
                   "oat milk is free since spring."),
    "garden-watering": ("Terrace plants watering rota", "Water the olive trees on Mondays and "
                        "the herbs every second day."),
    "espresso-rule": ("No espresso after six", "Rule from the owner: never serve espresso "
                      "after six in the evening."),
    "wifi-password": ("Guest wifi access", "The guest network password is on the back of the "
                      "menu card and changes every quarter."),
    "staff-rota": ("Weekly staff schedule", "Shifts are planned on Thursdays for the next "
                   "week; swaps go through the manager."),
}
QUESTIONS = [
    ("how do we restore the sales export of the till", "till-backup"),
    ("when can the boats deliver at the harbour", "tide-tables"),
    ("what roast for the house blend", "coffee-roaster"),
    ("how are prices written on the chalk board", "menu-board"),
    ("when do we water the olive trees", "garden-watering"),
    ("can I serve an espresso in the evening", "espresso-rule"),
    ("where is the guest wifi password", "wifi-password"),
    ("when is the staff schedule planned", "staff-rota"),
]


class BowEmbedder:
    """Bag of words hashed into 64 dims: retrieval that works, deterministic."""
    rerank_policy = "off"

    def __init__(self, ident="fake:bow@64", dim=64, noise=False):
        self.ident, self.dim, self.noise = ident, dim, noise

    def identity(self):
        return self.ident

    def _vec(self, text):
        v = [0.0] * self.dim
        words = text.lower().split()
        for w in words:
            h = hashlib.md5((w + ("#" if self.noise else "")).encode()).digest()
            v[h[0] % self.dim] += 1.0
            if self.noise:   # a useless model: every text lands on random dimensions
                v[h[1] % self.dim] -= 2.0
        return v if any(v) else [1.0] + [0.0] * (self.dim - 1)

    def embed_docs(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text, timeout=30):
        if self.noise:
            h = hashlib.md5(text.encode()).digest()
            return [((b % 7) - 3) * 1.0 + 0.1 for b in (h * 4)[: self.dim]]
        return self._vec(text)

    def rerank(self, query, docs, timeout=3):
        return None


@pytest.fixture()
def store():
    from core.store import Store

    name = "sokkan_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    s = Store(DSN.rsplit("/", 1)[0] + "/" + name)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _corpus(d):
    from memcore_fixtures import fm, write

    d.mkdir(parents=True, exist_ok=True)
    for name, (desc, body) in NOTES.items():
        write(d, f"{name}.md", fm(name, desc) + body + "\n")
    return d


def _index_cfg(d):
    from core.indexer import IndexConfig

    return IndexConfig(memory_dir=d, normalize=False, write_index=False)


@pytest.fixture()
def corpus(store, tmp_path):
    from core.indexer import Indexer

    d = _corpus(tmp_path / "memory")
    rep = Indexer(store, BowEmbedder(), _index_cfg(d), log=lambda m: None).run()
    assert rep.activated
    for q, n in QUESTIONS:
        ev.add_question(store, q, [n], author="test")
    return d


@needs_pg
def test_questions_are_validated_and_merged(store, corpus):
    with pytest.raises(ValueError, match="unknown note"):
        ev.add_question(store, "where is the missing note", ["nope"])
    with pytest.raises(ValueError):
        ev.add_question(store, "short", ["till-backup"])
    a = ev.add_question(store, "restore the till please", ["till-backup"], source="transcript",
                        session_id="s1")
    again = ev.add_question(store, "Restore  the till please", ["tide-tables"],
                            source="transcript", session_id="s1")
    assert again.id == a.id and again.seen == 1 and again.expected == ["till-backup"]
    b = ev.add_question(store, "restore the till please", ["tide-tables"],
                        source="transcript", session_id="s2")
    assert b.id == a.id and b.seen == 2 and b.expected == ["till-backup", "tide-tables"]
    g = ev.add_question(store, "what does the harbour office print", ["tide-tables"],
                        source="generated")
    assert g.weight == pytest.approx(0.4)
    c = ev.question_counts(store)
    assert c["client"]["active"] == len(QUESTIONS) and c["transcript"]["active"] == 1
    assert ev.set_question_status(store, g.id, "disabled")
    assert ev.question_counts(store)["generated"]["disabled"] == 1
    assert ev.delete_question(store, g.id)


@needs_pg
def test_run_measures_and_stores_results(store, corpus):
    ev.add_question(store, "who prints tide tables", ["tide-tables"], source="generated")
    q = ev.add_question(store, "a question about a note that will vanish", ["staff-rota"])
    store.delete_note("staff-rota", store.active_generation().id)
    r = ev.run(store, BowEmbedder(), profile="leger")
    m = r["metrics"]
    assert r["status"] == "done" and r["generation_id"] == store.active_generation().id
    assert m["overall"]["mrr"] > 0.8
    assert m["stale"] == 2                       # the two staff-rota questions
    assert set(m["by_source"]) == {"client", "generated"}
    assert m["by_source"]["generated"]["weight"] == pytest.approx(0.4)
    assert m["search_ms_p50"] is not None
    res = ev._results(store, r["id"])
    assert q.id not in res and len(res) == len(QUESTIONS) - 1 + 1


@needs_pg
def test_regression_is_detected_and_becomes_a_finding(store, corpus):
    good = ev.run(store, BowEmbedder(), profile="leger", trigger="nightly")
    assert ev.check_regression(store, good["id"]) is None          # nothing to compare to
    assert ev.findings(store) == [] or ev.findings(store)[0]["check"] == "recall-bench-small"
    # same identity, broken answers (e.g. a server that loaded another model)
    bad = ev.run(store, BowEmbedder(noise=True), profile="leger", trigger="nightly")
    reg = ev.check_regression(store, bad["id"], max_drop_=0.02)
    assert reg and reg["regressed"] and reg["delta"]["mrr"] < 0
    assert reg["worse"] >= 1 and reg["worse_examples"][0]["question"]
    f = [x for x in ev.findings(store) if x["check"] == "recall-regression"]
    assert f and f[0]["severity"] in ("warning", "critical") and f[0]["notes"]
    cmp = ev.compare(store, good["id"], bad["id"])
    assert cmp["common"] == len(QUESTIONS) and cmp["regressed"]


@needs_pg
def test_harvest_from_transcript_files(store, corpus, tmp_path):
    proj = tmp_path / "projects"
    proj.mkdir()
    (proj / "s1.jsonl").write_text("\n".join([
        _user("how do I bring back yesterday's till sales?"), _get("till-backup"),
        _user("and the guest network code?"), _get("wifi-password"), _get("ghost-note"),
        _user("one about a note that does not exist"), _get("ghost-note"),
    ]) + "\n")
    rep = ev.harvest(store, [proj])
    assert rep == {"files": 1, "pairs": 2, "new": 2, "merged": 0, "unknown_notes": 1}
    again = ev.harvest(store, [proj], since=0)
    assert again["new"] == 0 and again["merged"] == 2                # idempotent
    qs = ev.list_questions(store, source="transcript")
    assert {q.question for q in qs} == {"how do I bring back yesterday's till sales?",
                                        "and the guest network code?"}
    assert next(q for q in qs if "guest" in q.question).expected == ["wifi-password"]
    assert all(q.seen == 1 for q in qs)
    assert ev.get_state(store, "harvest")["pairs"] == 2


@needs_pg
def test_generate_with_the_instance_llm(store, corpus):
    calls = []

    def para(name, description, body):
        calls.append(name)
        if name == "menu-board":
            raise RuntimeError("llm down")
        return [f"question about {description.lower()}?", f"{name} verbatim?"]

    rep = ev.generate(store, para, limit=3)
    assert len(calls) == 3 and rep["questions"] + rep["errors"] * 2 >= 4
    gen = ev.list_questions(store, source="generated")
    assert gen and all(q.weight == pytest.approx(0.4) for q in gen)
    rep2 = ev.generate(store, para, limit=50)
    assert set(calls[3:]).isdisjoint(set(calls[:3]) - {"menu-board"})
    assert rep2["notes"] >= 1


@needs_pg
def test_nightly_runs_and_purges(store, corpus):
    rep = ev.nightly(store, BowEmbedder(), profile="leger", log=lambda m: None)
    assert rep["run"] and rep["regression"] is False and rep["purged"] == []
    ov = ev.overview(store)
    assert ov["last"]["id"] == rep["run"] and ov["questions"]["total_active"] == len(QUESTIONS)


# --------------------------------------------------------------------------- switch

def _switcher(store, d, factory, **kw):
    from core import switch as sw

    return sw.Switcher(store, index_config=lambda: _index_cfg(d), embedder_factory=factory,
                       check=lambda e: [], min_questions=5, **kw)


@needs_pg
def test_same_model_switch_reuses_the_index_and_rolls_back(store, corpus, monkeypatch):
    from core import switch as sw

    monkeypatch.setenv("CORTHEXIS_MEMORY_PROFILE", "leger")
    s = _switcher(store, corpus, lambda t: BowEmbedder())
    g1 = store.active_generation().id
    job = s.start(sw.Target("gpu", urls=["http://gpu-node:8080"]), "owner@x", background=False)
    assert job["status"] == "switched", job
    assert job["to_generation"] == g1 and not job["built"]
    assert job["comparison"]["common"] == len(QUESTIONS)
    assert store.active_generation().id == g1
    assert sw.current_target(store).profile == "gpu"
    st = s.status()
    assert st["current"]["profile"] == "gpu" and st["rollback"]["switch"] == job["id"]
    rb = s.rollback("owner@x")
    assert rb["kind"] == "rollback" and sw.current_target(store).profile == "leger"
    assert s.get(job["id"])["status"] == "rolled_back"
    assert s.rollback_candidate() is None


@needs_pg
def test_model_change_builds_in_background_and_switches_atomically(store, corpus,
                                                                  monkeypatch):
    from core import switch as sw

    monkeypatch.setenv("CORTHEXIS_MEMORY_PROFILE", "leger")
    g1 = store.active_generation()
    s = _switcher(store, corpus,
                  lambda t: BowEmbedder("fake:bow2@128", 128) if t.model else BowEmbedder())
    job = s.start(sw.Target("standard", model="multilingual-e5-base-q8"), "owner@x")
    s.wait(60)
    job = s.get(job["id"])
    assert job["status"] == "switched", job
    assert job["built"] and job["to_generation"] != g1.id and job["progress"] == 1.0
    new = store.active_generation()
    assert new.id == job["to_generation"] and new.embed_identity == "fake:bow2@128"
    assert store.get_generation(g1.id).status == "retired"
    assert job["baseline_run"] and job["candidate_run"]
    st = s.status()
    old = next(g for g in st["generations"] if g["id"] == g1.id)
    assert old["rollback_until"]
    s.rollback("owner@x")
    assert store.active_generation().id == g1.id
    assert sw.current_target(store).profile == "leger"


@needs_pg
def test_a_worse_model_is_blocked_then_discarded_or_approved(store, corpus):
    from core import switch as sw

    g1 = store.active_generation().id
    s = _switcher(store, corpus, lambda t: BowEmbedder("fake:noise@64", noise=True)
                  if t.model else BowEmbedder())
    job = s.start(sw.Target("leger", model="multilingual-e5-base-q8"), "a@x", background=False)
    assert job["status"] == "blocked", job
    assert job["comparison"]["regressed"] and job["comparison"]["delta"]["mrr"] < 0
    assert store.active_generation().id == g1                      # nothing changed
    cand = job["to_generation"]
    s.cancel(job["id"], "a@x")
    assert store.get_generation(cand) is None                       # dropped
    assert s.get(job["id"])["status"] == "cancelled"
    job2 = s.start(sw.Target("leger", model="multilingual-e5-base-q8"), "a@x",
                   background=False)
    assert job2["status"] == "blocked"
    with pytest.raises(sw.SwitchError):
        s.cancel(job["id"], "a@x")
    s.approve(job2["id"], "boss@x")
    assert store.active_generation().id == job2["to_generation"]
    assert s.get(job2["id"])["decided_by"] == "boss@x"


@needs_pg
def test_few_questions_need_a_decision_and_unreachable_servers_fail(store, corpus):
    from core import switch as sw

    s = sw.Switcher(store, index_config=lambda: _index_cfg(corpus),
                    embedder_factory=lambda t: BowEmbedder(), check=lambda e: [],
                    min_questions=50)
    job = s.start(sw.Target("gpu"), "a@x", background=False)
    assert job["status"] == "blocked" and job["comparison"]["enough"] is False
    s.cancel(job["id"], "a@x")
    down = sw.Switcher(store, embedder_factory=lambda t: BowEmbedder(),
                       check=lambda e: ["no memory server of this profile answers"])
    job = down.start(sw.Target("gpu"), "a@x", background=False)
    assert job["status"] == "failed" and "answers" in job["detail"]
    with pytest.raises(sw.SwitchError):
        sw.Target("turbo").validate()
    with pytest.raises(sw.SwitchError):
        sw.Target("gpu", urls=["ftp://x"]).validate()


@needs_pg
def test_one_change_at_a_time_and_recovery(store, corpus):
    from core import switch as sw

    s = _switcher(store, corpus, lambda t: BowEmbedder())
    with store.pool.connection() as con:
        con.execute("INSERT INTO index_switches (to_target, status) VALUES ('{}', 'building')")
    with pytest.raises(sw.SwitchError, match="already in progress"):
        s.start(sw.Target("gpu"), "a@x", background=False)
    assert s.recover() == 1
    assert s.start(sw.Target("gpu"), "a@x", background=False)["status"] == "switched"


def test_preflight_detects_a_server_with_another_model(monkeypatch):
    from core import embed, switch as sw

    class R:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout: R(
        {"data": [{"id": "/models/multilingual-e5-base-q8_0.gguf"}]}))
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", ["http://a:8080"])
    monkeypatch.setattr(e, "embed_query", lambda text, timeout=30: [0.0] * 768)
    probs = sw.preflight(e)
    assert probs and "not embeddinggemma-300m-q8_0.gguf" in probs[0]
    monkeypatch.setattr(httpx, "get", lambda url, timeout: R(
        {"data": [{"id": "/models/embeddinggemma-300M-Q8_0.gguf"}]}))
    assert sw.preflight(e) == []

    def boom(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    assert "answers" in sw.preflight(e)[0]


def test_build_embedder_follows_the_target(monkeypatch):
    from core import switch as sw

    monkeypatch.delenv("CORTHEXIS_EMBED_URLS", raising=False)
    monkeypatch.delenv("SOKKAN_EMBED_URLS", raising=False)
    monkeypatch.delenv("CORTHEXIS_RERANK_URL", raising=False)
    monkeypatch.delenv("SOKKAN_RERANK_URL", raising=False)
    e = sw.build_embedder(sw.Target("gpu", model="embeddinggemma-300m-q8"))
    assert e.urls[0].endswith("corthexis-embed-gpu:8080") and e.rerank_policy == "interactive"
    e = sw.build_embedder(sw.Target("leger", model="multilingual-e5-base-q8",
                                    urls=["http://n:1"], rerank_url="http://r:2"))
    assert e.urls == ["http://n:1"] and e.rerank_url is None        # leger never reranks
    assert e.identity().startswith("llamacpp:multilingual-e5-base-q8@")
