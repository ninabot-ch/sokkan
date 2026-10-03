"""Recall at every turn and for sub-agents (memory/core/recall.py) — unit tests, no I/O."""
import io
import json

from core import recall as R
from core.contract import Generation, NoteRecord
from core.search import Hit


def H(name, score, desc="d", rerank=None, age=3, src="indexed", gen=1):
    return Hit(name, score, score, 0.1, rerank, f"snippet of {name}", age, src,
               description=desc, generation=gen)


class FakeEmbedder:
    def __init__(self, policy="off", fail=False):
        self.rerank_policy = policy
        self.fail = fail
        self.calls = []

    def embed_query(self, text, timeout=30):
        self.calls.append(("q", timeout))
        if self.fail:
            raise RuntimeError("down")
        return [1.0, 0.0]

    def rerank(self, q, docs, timeout=3):
        self.calls.append(("rr", len(docs), timeout))
        return [float(i) for i in range(len(docs))]


class FakeStore:
    def __init__(self, hits, identity="llamacpp:embeddinggemma-300m-q8@768", notes=None):
        self.hits = hits
        self.gen = Generation(7, identity, 2, "2026-01-01", "active")
        self.recalled: dict = {}
        self.logged = []
        self.turns = []
        self.search_calls = []
        self.notes = notes or {}

    def active_generation(self):
        return self.gen

    def search(self, qv, text, k, rerank=None, **over):
        self.search_calls.append((qv, k, rerank is not None, over))
        if rerank is not None:
            rerank(text, ["a", "b"])
        return list(self.hits)[:k]

    def recalled_notes(self, sid, agent_id=None):
        return set(self.recalled.get((sid, agent_id), set()))

    def existing_names(self, names):
        return {n for n in names if n in self.notes}

    def get_note(self, name):
        return self.notes.get(name)

    def log_recall(self, channel, hits, session_id=None, agent_id=None, query=None):
        self.logged.append((channel, [h.note_name for h in hits], session_id, agent_id))
        self.recalled.setdefault((session_id, agent_id), set()).update(h.note_name for h in hits)

    def log_recall_turn(self, channel, **kw):
        self.turns.append((channel, kw))


def recaller(store, emb=None, **cfg):
    return R.Recaller(store, emb or FakeEmbedder(), R.RecallConfig(**cfg), profile="leger")


def test_threshold_per_model():
    assert R.default_threshold("llamacpp:embeddinggemma-300m-q8@768") == \
        dict(R.MODEL_THRESHOLDS)["embeddinggemma"]
    assert R.default_threshold("llamacpp:multilingual-e5-base-q8@768") == \
        dict(R.MODEL_THRESHOLDS)["e5"]
    assert R.default_threshold("something-else") == R.DEFAULT_THRESHOLD
    st = FakeStore([], identity="llamacpp:multilingual-e5-base-q8@768")
    assert recaller(st).threshold() == dict(R.MODEL_THRESHOLDS)["e5"]
    assert recaller(st, threshold=0.2).threshold() == 0.2


def test_top_k_above_threshold_and_logged():
    st = FakeStore([H("a-note", 0.9), H("b-note", 0.8), H("c-note", 0.7), H("d-note", 0.6),
                    H("e-note", 0.55), H("low-note", 0.1)])
    res = recaller(st, threshold=0.5).recall("how do we deploy the staging billing api",
                                             session_id="s1")
    assert res.notes == ["a-note", "b-note", "c-note", "d-note"]
    assert "`a-note`" in res.context and res.context.startswith(R.MARKER)
    assert "not instructions" in res.context and "memory_get" in res.context
    assert st.logged == [("prompt", res.notes, "s1", None)]
    ch, turn = st.turns[0]
    assert ch == "prompt" and turn["injected"] == 4 and turn["candidates"] == 6
    assert turn["profile"] == "leger" and turn["generation"] == 7


def test_below_threshold_nothing_but_turn_logged():
    st = FakeStore([H("a-note", 0.2)])
    res = recaller(st, threshold=0.5).recall("unrelated question about the weather today",
                                             session_id="s1")
    assert res.hits == [] and res.context == "" and st.logged == []
    assert st.turns[0][1]["injected"] == 0 and st.turns[0][1]["skipped"] is None


def test_skips_short_slash_and_shell():
    st = FakeStore([H("a-note", 0.9)])
    r = recaller(st)
    for text, why in [("", "empty"), ("/compact", "command"), ("!ls -la", "command"),
                      ("ok thanks", "short"), (R.MARKER + " x y z w", "already-recalled")]:
        res = r.recall(text, session_id="s")
        assert res.skipped == why and not res.hits
    assert st.search_calls == []


def test_dedup_within_a_session_only():
    st = FakeStore([H("a-note", 0.9), H("b-note", 0.8)])
    r = recaller(st, threshold=0.5, top_k=1)
    assert r.recall("first question about the deploy", session_id="s1").notes == ["a-note"]
    second = r.recall("second question about the deploy", session_id="s1")
    assert second.notes == ["b-note"] and second.deduplicated == ["a-note"]
    assert r.recall("first question about the deploy", session_id="s2").notes == ["a-note"]
    # the search asks for more candidates when some are excluded
    assert st.search_calls[1][1] > st.search_calls[0][1]


def test_quoted_name_forces_recall_below_threshold():
    st = FakeStore([H("weak-hit", 0.1), H("brosim-browser", 0.2)])
    res = recaller(st, threshold=0.5).recall("is brosim still the browser we use for agents?")
    assert res.notes == ["brosim-browser"] and res.forced == ["brosim-browser"]
    assert "name quoted" in res.context


def test_full_name_quoted_but_not_found_by_search():
    note = NoteRecord("deploy-runbook", "How we deploy", "project", 0, "/m/deploy_runbook.md",
                      "2026-01-01T00:00:00+00:00", "frontmatter", "Body of the runbook")
    st = FakeStore([H("other-note", 0.9)], notes={"deploy-runbook": note})
    res = recaller(st, threshold=0.5).recall("read deploy-runbook and apply it to the api")
    assert res.notes[0] == "deploy-runbook" and "other-note" in res.notes


def test_reranker_only_when_interactive_and_top3():
    st = FakeStore([H("a-note", 0.9)])
    recaller(st, FakeEmbedder("off")).recall("question about the deploy pipeline")
    recaller(st, FakeEmbedder("async")).recall("question about the deploy pipeline")
    emb = FakeEmbedder("interactive")
    res = recaller(st, emb).recall("question about the deploy pipeline")
    assert [c[2] for c in st.search_calls] == [False, False, True]
    assert st.search_calls[2][3] == {"rerank_top": 3}
    rr = [c for c in emb.calls if c[0] == "rr"][0]
    assert rr[2] <= 1.5 and res.latency_ms >= 0


def test_embedding_down_is_lexical_and_flagged():
    st = FakeStore([H("a-note", 0.9)])
    res = recaller(st, FakeEmbedder(fail=True)).recall("question about the deploy pipeline")
    assert st.search_calls[0][0] is None and "lexical-only" in res.degraded
    assert "warning" in res.context


def test_store_failure_is_silent():
    class Broken(FakeStore):
        def search(self, *a, **k):
            raise RuntimeError("db down")
    st = Broken([])
    res = recaller(st).recall("question about the deploy pipeline", session_id="s")
    assert res.hits == [] and res.error and res.context == ""
    assert st.turns[0][1]["skipped"] == "error"


def test_age_and_provenance_in_block():
    st = FakeStore([H("a-note", 0.9, age=40, src="inferred"), H("b-note", 0.8, age=2)])
    ctx = recaller(st, threshold=0.5).recall("question about the deploy pipeline").context
    assert "updated 40 d ago, reconstructed date" in ctx
    assert "updated 2 d ago, score" in ctx


def test_hook_output_prompt_and_subagent():
    st = FakeStore([H("a-note", 0.9)])
    r = recaller(st, threshold=0.5)
    out = R.hook_output({"hook_event_name": "UserPromptSubmit", "session_id": "cli-1",
                         "prompt": "how do we deploy the billing api"}, r)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit" and "a-note" in hso["additionalContext"]
    assert st.logged[-1][2] == "cli-1"

    payload = {"hook_event_name": "PreToolUse", "session_id": "cli-1", "tool_name": "Agent",
               "tool_use_id": "toolu_9",
               "tool_input": {"description": "deploy", "prompt": "Deploy the billing api",
                              "subagent_type": "general-purpose"}}
    out = R.hook_output(payload, r, session_id="sokkan-sid")
    upd = out["hookSpecificOutput"]["updatedInput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert upd["prompt"].startswith("Deploy the billing api") and R.SUBAGENT_MARKER in upd["prompt"]
    assert upd["subagent_type"] == "general-purpose" and upd["description"] == "deploy"
    assert "> snippet of a-note" in upd["prompt"]                # sub-agents get the excerpt
    # logged under the sub-agent, not deduplicated against the parent session
    assert st.logged[-1] == ("subagent", ["a-note"], "sokkan-sid", "toolu_9")
    # never twice in the same prompt, other tools untouched
    payload["tool_input"] = upd
    assert R.hook_output(payload, r) == {}
    assert R.hook_output({**payload, "tool_name": "Bash"}, r) == {}


def test_command_hook_main_never_fails(monkeypatch):
    out = io.StringIO()
    assert R.main(io.StringIO("not json"), out) == 0 and out.getvalue() == ""
    monkeypatch.setattr(R, "default_recaller", lambda **k: (_ for _ in ()).throw(
        RuntimeError("no database")))
    assert R.main(io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit",
                                          "prompt": "a real question about deploys"})),
                  out) == 0
    assert out.getvalue() == ""
    monkeypatch.setenv("CORTHEXIS_RECALL", "0")
    assert not R.enabled()


def test_command_hook_main_outputs_json(monkeypatch):
    st = FakeStore([H("a-note", 0.9)])
    st.close = lambda: None
    monkeypatch.setattr(R, "default_recaller", lambda **k: recaller(st, threshold=0.5))
    out = io.StringIO()
    R.main(io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "x",
                                   "prompt": "how do we deploy the billing api"})), out)
    assert "a-note" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CORTHEXIS_RECALL_TOPK", "2")
    monkeypatch.setenv("SOKKAN_RECALL_SEUIL", "0.4")
    monkeypatch.setenv("CORTHEXIS_RECALL_BUDGET_S", "0.9")
    c = R.RecallConfig.from_env()
    assert (c.top_k, c.threshold, c.budget_s) == (2, 0.4, 0.9)
