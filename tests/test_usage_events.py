"""usage.py — the `events` source (table `usage_events`) next to the transcripts.

An engine reports what it consumed through the session contract (`usage` events); the
session records them. Today's engine also writes transcripts, and Operate › Costs still
reads those: these tests prove the two sources give the SAME session — on a hand-made
transcript replayed as events, on a scripted SDK client, and on a real run of the CLI
against a scripted model — and that what Costs shows did not move.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness_helpers import feed  # noqa: E402

SID, CSID = "sid-usage-1", "c0ffee00-0000-4000-8000-000000000001"
OPUS, UNKNOWN = "claude-opus-5-5", "some-local-model"

# one API message per id; the CLI writes one transcript line PER CONTENT BLOCK, each with
# the usage of the whole message (growing while it streams)
MESSAGES = [
    ("msg_1", OPUS, [
        {"input_tokens": 1200, "output_tokens": 3, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 9000,
         "cache_creation": {"ephemeral_5m_input_tokens": 6000,
                            "ephemeral_1h_input_tokens": 3000}},
        {"input_tokens": 1200, "output_tokens": 410, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 9000,
         "cache_creation": {"ephemeral_5m_input_tokens": 6000,
                            "ephemeral_1h_input_tokens": 3000}}]),
    ("msg_2", OPUS, [
        {"input_tokens": 80, "output_tokens": 55, "cache_read_input_tokens": 10200,
         "cache_creation_input_tokens": 300}]),
    ("msg_3", UNKNOWN, [
        {"input_tokens": 500, "output_tokens": 40}]),
    ("msg_err", "<synthetic>", [
        {"input_tokens": 0, "output_tokens": 0}]),
]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import agentchat
    import audit
    import board
    import usage
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "PROJECT_DIR", tmp_path / "transcripts")
    (tmp_path / "transcripts").mkdir()
    monkeypatch.setattr(agentchat, "_registry", {})
    monkeypatch.delenv("SOKKAN_BILLING_BASIS", raising=False)
    return tmp_path


def _write_transcript(d: Path, csid: str = CSID) -> Path:
    lines = [{"type": "user", "timestamp": "2026-03-02T09:00:00Z",
              "message": {"role": "user", "content": "plan the delivery"}}]
    for mid, model, blocks in MESSAGES:
        for u in blocks:
            lines.append({"type": "assistant", "timestamp": "2026-03-02T09:00:05Z",
                          "message": {"id": mid, "model": model, "usage": u,
                                      "content": [{"type": "text", "text": "…"}]}})
    p = d / f"{csid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


def _same(a: dict, b: dict) -> None:
    for k in ("turns", "in_tokens", "out_tokens", "cache_read", "cache_write", "cost",
              "unpriced_turns", "models"):
        assert a[k] == b[k], (k, a, b)


def test_events_replayed_from_a_transcript_give_the_same_session(world):
    import board
    import usage
    board.add_sdk_session(SID, "t")
    board.set_claude_session_id(SID, CSID)
    _write_transcript(world / "transcripts")
    for mid, model, blocks in MESSAGES:
        for u in blocks:        # every block repeats the message: the last figure supersedes
            det = u.get("cache_creation") or {}
            usage.record_call(session_id=SID, message_id=mid, model=model,
                               in_tokens=u.get("input_tokens", 0),
                               out_tokens=u.get("output_tokens", 0),
                               cache_read=u.get("cache_read_input_tokens", 0),
                               cache_write=u.get("cache_creation_input_tokens", 0),
                               cache_write_long=det.get("ephemeral_1h_input_tokens", 0))
    by_transcript = usage.session_totals(SID, "transcripts")
    by_events = usage.session_totals(SID, "events")
    _same(by_transcript, by_events)
    assert by_events["source"] == "events" and by_transcript["source"] == "transcripts"
    assert by_events["turns"] == 3                      # the placeholder is not a call
    assert (by_events["in_tokens"], by_events["out_tokens"]) == (1780, 505)
    assert (by_events["cache_read"], by_events["cache_write"]) == (10200, 9300)
    assert by_events["cost"] > 0 and by_events["unpriced_turns"] == 1
    assert by_events["models"] == sorted([OPUS, UNKNOWN])
    # the long-lived cache writes are priced apart: the split survived the events
    import pricing
    flat = pricing.cost(OPUS, {"cache_creation_input_tokens": 9000})["total"]
    split = pricing.cost(OPUS, {"cache_creation_input_tokens": 9000, "cache_creation": {
        "ephemeral_5m_input_tokens": 6000, "ephemeral_1h_input_tokens": 3000}})["total"]
    assert split > flat
    with pytest.raises(ValueError):
        usage.session_totals(SID, "tea-leaves")


def test_an_event_without_id_counts_as_it_comes_and_another_session_is_apart(world):
    import usage
    for _ in range(2):
        usage.record_call(session_id="s-a", message_id=None, model=OPUS, in_tokens=10,
                           out_tokens=1)
    usage.record_call(session_id="s-b", message_id="msg_1", model=OPUS, in_tokens=7,
                       out_tokens=1)
    usage.record_call(session_id="s-a", message_id="msg_1", model=OPUS, in_tokens=5,
                       out_tokens=1)                    # same id, other session: not merged
    assert usage.session_totals("s-a", "events")["in_tokens"] == 25
    assert usage.session_totals("s-a", "events")["turns"] == 3
    assert usage.session_totals("s-b", "events")["in_tokens"] == 7
    assert usage.session_totals("nobody", "events")["turns"] == 0
    assert usage.events_db() == world / "usage_events.db" and usage.events_db().exists()


def test_a_session_records_what_its_engine_reports_and_costs_did_not_move(world):
    """Through the adapter: the SDK messages a transcript would hold, fed to a session →
    `usage` events → the ledger. The Costs tab (transcripts) shows what it showed."""
    pytest.importorskip("claude_agent_sdk")
    import agentchat
    import board
    import usage
    from claude_agent_sdk import AssistantMessage, TextBlock
    board.add_sdk_session(SID, "t")
    board.set_claude_session_id(SID, CSID)
    _write_transcript(world / "transcripts")
    before = usage.summary(30)
    s = agentchat.AgentSession(SID, cwd="/tmp", user="alice@x")
    for mid, model, blocks in MESSAGES:
        for u in blocks:
            feed(s, AssistantMessage(content=[TextBlock(text="…")], model=model, usage=u,
                                     message_id=mid))
    _same(usage.session_totals(SID, "transcripts"), usage.session_totals(SID, "events"))
    after = usage.summary(30)
    assert after["totals"] == before["totals"] and after["by_model"] == before["by_model"]
    assert after["sessions"] == before["sessions"] and after["sources"] == before["sources"]
    con = usage._events_con()
    rows = [dict(r) for r in con.execute("SELECT * FROM usage_calls ORDER BY id")]
    con.close()
    assert [r["message_id"] for r in rows] == ["msg_1", "msg_2", "msg_3"]
    assert {(r["session_id"], r["harness"], r["project"]) for r in rows} == {
        (SID, "legacy", "default")}
    assert rows[0]["cw_5m"] == 6000 and rows[0]["cw_1h"] == 3000 and rows[0]["out_tokens"] == 410


def test_a_ledger_that_cannot_be_written_never_breaks_a_turn(world, monkeypatch, capsys):
    pytest.importorskip("claude_agent_sdk")
    import agentchat
    import usage
    from claude_agent_sdk import AssistantMessage, TextBlock

    def boom(**kw):
        raise OSError("disk full")
    monkeypatch.setattr(usage, "record_call", boom)
    s = agentchat.AgentSession("sid-usage-2", cwd="/tmp")
    feed(s, AssistantMessage(content=[TextBlock(text="still here")], model=OPUS,
                             usage={"input_tokens": 1, "output_tokens": 1}, message_id="m"))
    assert [e for e in s.events if e["type"] == "text"] == [
        {"type": "text", "text": "still here", "model": OPUS}]
    assert "usage event of sid-usage-2 not recorded" in capsys.readouterr().err


# ---- a real run: the CLI writes its transcript, the session records the events ------------

def _bundled_cli() -> bool:
    try:
        import claude_agent_sdk
    except ImportError:
        return False
    d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
    return os.path.exists(os.path.join(d, "claude"))


@pytest.mark.skipif(os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0",
                    reason="SOKKAN_TEST_E2E_AGENTS=0")
@pytest.mark.skipif(not _bundled_cli(), reason="claude-agent-sdk without bundled CLI")
def test_a_real_run_gives_the_same_total_by_both_sources(world, monkeypatch):
    """Three turns of the real CLI against a scripted model, one with a tool call: the
    transcript it wrote and the events the session recorded add up to the same tokens and
    the same cost."""
    import agentchat
    import board
    import usage
    from mock_anthropic import MockAnthropic

    class Model(MockAnthropic):
        def answer(self, req, mid):
            msgs = req.get("messages") or []
            if not req.get("tools") or not msgs:
                return {"type": "text", "text": "ok"}, "end_turn"
            last = [m for m in msgs if m.get("role") == "user"][-1].get("content")
            is_result = isinstance(last, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in last)
            if not is_result and "list the files" in json.dumps(last):
                return {"type": "tool_use", "id": f"toolu_{mid:04d}", "name": "Bash",
                        "input": {"command": "ls", "description": "list"}}, "tool_use"
            return {"type": "text", "text": "Done."}, "end_turn"

    for k, v in {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CONFIG_DIR": str(world / "claude"),
                 "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "DISABLE_AUTOUPDATER": "1", "SOKKAN_BILLING_BASIS": "api"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    work = world / "work"
    work.mkdir()
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "t")
    with Model() as mock:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        s = agentchat.AgentSession(sid, cwd=str(work), user="alice@x",
                                   model="claude-sonnet-5-5")
        s.mode = "bypassPermissions"            # nobody to click

        async def go():
            for prompt in ("hello", "list the files", "thanks"):
                await asyncio.wait_for(s.handle_user(prompt), 90)
            await s.close()
        asyncio.new_event_loop().run_until_complete(go())
    csid = board.get_claude_session_id(sid)
    assert csid and s.claude_session_id == csid
    (transcript,) = (world / "claude" / "projects").glob(f"*/{csid}.jsonl")
    monkeypatch.setattr(usage, "PROJECT_DIR", transcript.parent)
    by_transcript = usage.session_totals(sid, "transcripts")
    by_events = usage.session_totals(sid, "events")
    assert by_transcript["turns"] >= 4, by_transcript       # 3 prompts + the tool round trip
    assert by_transcript["in_tokens"] > 0 and by_transcript["out_tokens"] > 0
    assert by_transcript["cost"] > 0, by_transcript
    _same(by_transcript, by_events)
    # and Costs — which reads the transcripts — shows exactly that session
    shown = usage.summary(30)
    (line,) = [x for x in shown["sessions"] if x["session_id"] == csid]
    assert line["source"] == "sokkan" and line["turns"] == by_events["turns"]
    assert line["out_tokens"] == by_events["out_tokens"]
    assert shown["totals"]["all"]["api_equiv"] == pytest.approx(by_events["cost"])
    # one record per turn, what the pane said of each turn
    recs = usage.turns(sid)
    results = [e for e in s.events if e["type"] == "result"]
    assert len(recs) == 3 == len(results)
    assert {r["engine"] for r in recs} == {"legacy"} and {r["project"] for r in recs} == {
        "default"}
    assert all(r["reported"] and r["duration_ms"] is not None and r["model"] for r in recs)
    assert [r["cost_usd"] for r in recs] == [e["cost_usd"] for e in results]
    assert sum(r["in_tokens"] + r["cache_read"] + r["cache_write"] for r in recs) == s.tokens_in
    assert sum(r["out_tokens"] for r in recs) == s.tokens_out
    assert sum(r["calls"] for r in recs) == by_events["turns"]


# ---- one normalised record per turn (usage_events) ----------------------------------------

def test_a_turn_of_the_cli_adapter_is_one_record(world):
    """Through the adapter: two calls, the engine's result → one record, with the engine's
    total for the turn, its duration, its cost as the pane showed it, the list price of
    its calls."""
    pytest.importorskip("claude_agent_sdk")
    import agentchat
    import board
    import usage
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    board.add_sdk_session(SID, "t")
    s = agentchat.AgentSession(SID, cwd="/tmp", user="alice@x")
    feed(s, AssistantMessage(content=[TextBlock(text="…")], model=OPUS, message_id="m1",
                             usage={"input_tokens": 100, "output_tokens": 1,
                                    "cache_read_input_tokens": 2000}))
    feed(s, AssistantMessage(content=[TextBlock(text="done")], model=OPUS, message_id="m2",
                             usage={"input_tokens": 50, "output_tokens": 1}))
    feed(s, ResultMessage(subtype="success", duration_ms=4321, duration_api_ms=4000,
                          is_error=False, num_turns=2, session_id=CSID, total_cost_usd=0.12,
                          result="done", usage={"input_tokens": 150, "output_tokens": 90,
                                                "cache_read_input_tokens": 2000},
                          model_usage={OPUS: {"inputTokens": 150, "outputTokens": 90,
                                              "cacheReadInputTokens": 2000}}))
    (rec,) = usage.turns(SID)
    assert rec == s.last_turn | {"ended_at": rec["ended_at"]}
    assert (rec["engine"], rec["model"], rec["models"], rec["project"]) == (
        "legacy", OPUS, [OPUS], "default")
    assert (rec["in_tokens"], rec["out_tokens"], rec["cache_read"], rec["cache_write"]) == (
        150, 90, 2000, 0)
    assert rec["calls"] == 2 and rec["duration_ms"] == 4321
    assert rec["cost_usd"] == pytest.approx(0.12) and rec["cost_source"] == "engine"
    import pricing
    assert rec["list_cost_usd"] == pytest.approx(pricing.cost(OPUS, {
        "input_tokens": 150, "output_tokens": 90, "cache_read_input_tokens": 2000})["total"],
        abs=1e-6)
    assert rec["unpriced_calls"] == 0 and rec["reported"] and not rec["is_error"]
    (shown,) = [e for e in s.events if e["type"] == "result"]
    assert shown["cost_usd"] == rec["cost_usd"]
    assert (s.tokens_in, s.tokens_out) == (2150, 90)


def test_the_fake_engine_through_the_cockpit_gives_one_record_per_turn(world, monkeypatch):
    """The reference engine (no transcript, no turn total, an unpriced local model, no
    clock of its own): three turns → three records, summed from its calls, timed by the
    cockpit; the study journal reads them."""
    import agentchat
    import board
    import study
    import usage
    from harness import contract
    from harness.conformance import driver, reference
    seen = []
    monkeypatch.setattr(study, "turn_start", lambda *a, **k: len(seen) + 1)
    monkeypatch.setattr(study, "turn_end", lambda t, **kw: seen.append((t, kw)))

    async def go():
        model = reference.ReferenceModel()
        for n in range(3):
            model.script.append([driver.say(f"answer {n}")])
        board.add_sdk_session("sid-ref", "t")
        s = agentchat.AgentSession("sid-ref", cwd=str(world), user="alice@x")
        s.attach(reference.ReferenceHarness(model),
                 contract.RunSpec(run_id="sid-ref", cwd=str(world)))
        for n in range(3):
            await asyncio.wait_for(s.handle_user(f"question {n}"), 10)
        return s

    s = asyncio.new_event_loop().run_until_complete(go())
    recs = usage.turns("sid-ref")
    assert len(recs) == 3
    assert {(r["engine"], r["model"], r["basis"]) for r in recs} == {
        ("reference", reference.MODEL, "local")}
    assert all(r["calls"] == 1 and r["in_tokens"] > 0 and r["out_tokens"] > 0 for r in recs)
    assert all(r["duration_ms"] is not None and r["duration_ms"] >= 0 for r in recs)
    assert all(r["list_cost_usd"] is None and r["unpriced_calls"] == 1 for r in recs)
    assert s.tokens_out == sum(r["out_tokens"] for r in recs)
    assert [kw["tokens_out"] for _t, kw in seen] == [r["out_tokens"] for r in recs]
    assert [kw["tokens_in"] for _t, kw in seen] == [r["in_tokens"] for r in recs]
    assert {kw["model"] for _t, kw in seen} == {reference.MODEL}
    # the pane was told what this engine cannot do (C7), once
    assert [e for e in s.events if e["type"] == "capabilities"] == [
        {"type": "capabilities", "plan": False, "question": False}]


def test_a_turn_without_result_is_in_the_ledger_but_counts_nothing(world):
    pytest.importorskip("claude_agent_sdk")
    import agentchat
    import usage
    from harness import contract
    s = agentchat.AgentSession("sid-lost", cwd="/tmp")
    s._consume(contract.TurnStarted(turn_id="t1"))
    s._consume(contract.Usage(turn_id="t1", model=OPUS, input=10, output=3, message_id="m"))
    s._consume(contract.TurnEnded(turn_id="t1", stop_reason="error", is_error=True,
                                  reported=False))
    (rec,) = usage.turns("sid-lost")
    assert not rec["reported"] and rec["is_error"] and rec["in_tokens"] == 10
    assert rec["cost_usd"] is None and (s.tokens_in, s.tokens_out, s.cost_usd) == (0, 0, 0)


def test_a_draft_ledger_of_the_lot_is_migrated_in_place(world):
    """The first draft of the lot kept the CALLS in `usage_events`: opened by this code,
    they become `usage_calls` and the table of turns is created."""
    import sqlite3
    import usage
    path = usage.events_db()
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '', harness TEXT NOT NULL DEFAULT '',
            engine_session TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT 'default',
            model TEXT NOT NULL DEFAULT '', basis TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
            in_tokens INTEGER NOT NULL DEFAULT 0, out_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read INTEGER NOT NULL DEFAULT 0,
            cw_5m INTEGER NOT NULL DEFAULT 0, cw_1h INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX ux_usage_events_msg ON usage_events(session_id, message_id)
            WHERE message_id != '';
        INSERT INTO usage_events(session_id, message_id, model, ts, in_tokens, out_tokens)
            VALUES('s-old', 'msg_1', 'claude-opus-5-5', 1.0, 40, 2);
    """)
    con.commit()
    con.close()
    assert usage.session_totals("s-old", "events")["in_tokens"] == 40
    usage.record_call(session_id="s-old", message_id="msg_1", model=OPUS, in_tokens=41)
    assert usage.session_totals("s-old", "events")["in_tokens"] == 41     # still one call
    assert usage.turns("s-old") == []
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    cols = {r[1] for r in con.execute("PRAGMA table_info(usage_events)")}
    con.close()
    assert "turn_id" in cols and "message_id" not in cols


# ---- Operate › Costs and the budgets read the ledger for an engine without transcripts ---

def test_costs_and_budgets_read_the_ledger_of_an_engine_without_transcripts(world, monkeypatch):
    """A session of an engine that writes no transcript is in Operate › Costs, in its
    project's spend (budgets) and priced by the same table; a session of the CLI, whose
    calls are ALSO in the ledger, is counted once — from its transcript."""
    import board
    import pricing
    import usage
    monkeypatch.setenv("SOKKAN_BILLING_BASIS", "api")
    board.add_sdk_session(SID, "t")
    board.set_claude_session_id(SID, CSID)
    _write_transcript(world / "transcripts")
    before = usage.summary(30)
    # the CLI's own calls, recorded by the session as well: must not be counted twice
    for mid, model, blocks in MESSAGES:
        u = blocks[-1]
        usage.record_call(session_id=SID, message_id=mid, model=model, harness="legacy",
                          in_tokens=u.get("input_tokens", 0), out_tokens=u.get("output_tokens", 0))
    assert usage.summary(30)["totals"] == before["totals"]
    # a session of another engine (here: one that is not declared, as a future one)
    board.add_sdk_session("sid-kern", "t", title="Relance du devis", project="radio")
    usage.record_call(session_id="sid-kern", message_id="k1", model=OPUS, harness="kernel",
                      project="radio", in_tokens=1000, out_tokens=200)
    usage.record_call(session_id="sid-kern", message_id="k2", model=OPUS, harness="kernel",
                      project="radio", in_tokens=500, out_tokens=100)
    after = usage.summary(30)
    cost = pricing.cost(OPUS, {"input_tokens": 1500, "output_tokens": 300})["total"]
    assert after["totals"]["all"]["turns"] == before["totals"]["all"]["turns"] + 2
    assert after["totals"]["all"]["api_equiv"] == pytest.approx(
        before["totals"]["all"]["api_equiv"] + cost, abs=1e-4)
    (line,) = [x for x in after["sessions"] if x["session_id"] == "sid-kern"]
    assert line["turns"] == 2 and line["out_tokens"] == 300 and line["project"] == "radio"
    assert line["title"] == "Relance du devis"
    assert {p["project"] for p in after["by_project"]} >= {"radio"}
    spend = usage.project_spend("radio")
    assert spend["day"] == pytest.approx(cost, abs=1e-4) and spend["month"] >= spend["day"]
    assert usage.summary(30, project="radio")["totals"]["all"]["turns"] == 2
    assert usage.transcript_engines() == {"legacy"}
