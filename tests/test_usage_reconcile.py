"""3.2.3 — Costs reconciliation: a synthetic transcript with a KNOWN usage gives exactly the
expected figure in every billing basis (api key, subscription, SOKKAN Inference gateway,
local engine), counts each API message once (the CLI writes one line per content block),
prices from the versioned table only, and keeps the transcripts SOKKAN did not start out of
the totals."""
import json
from datetime import datetime, timezone

import pytest

# --- the known usage -----------------------------------------------------------------------
# A: claude-opus-5-5, written as THREE lines (thinking / text / tool_use) with the same id
#    in 1 000 × $4 + out 2 000 × $20 + cache read 1 000 000 × $0.20 + cache write 1h
#    10 000 × $4 × 2  =  0.004 + 0.04 + 0.2 + 0.08  = $0.324
# B: claude-sonnet-5-5: in 500 × $2 + out 100 × $10 + cache write (no TTL split → 5 min)
#    2 000 × $2 × 1.25 = 0.001 + 0.001 + 0.005 = $0.007
# C: claude-haiku-4-5-20251001 (dated id → claude-haiku-4-5): in 1 000 000 × $1 = $1.00
# D: claude-opus-9 (not in the table): tokens counted, NOT priced
# E: sub-agent of the same session, claude-sonnet-5-5: out 1 000 000 × $10 = $10.00
A = 0.324
B = 0.007
C = 1.0
E = 10.0
CLAUDE_TOTAL = A + B + C + E


def _line(mid, model, usage, block="text"):
    return {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
            "entrypoint": "sdk-py",
            "message": {"id": mid, "model": model, "usage": usage,
                        "content": [{"type": block}]}}


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import board
    import llm
    import magnitude
    import usage

    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SOKKAN_BILLING_BASIS",
                "SOKKAN_USAGE_EXTERNAL", "SOKKAN_CLAUDE_PRICES", "SOKKAN_MODEL_PRICES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    monkeypatch.setattr(magnitude, "STATE", tmp_path / "magnitude.json")
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    claude = tmp_path / "claude"
    ws = claude / "projects" / "-workspace"
    monkeypatch.setattr(usage, "_claude_dir", str(claude))
    monkeypatch.setattr(usage, "PROJECT_DIR", ws)

    # a SOKKAN session (board) whose Claude session id is "c-sokkan"
    board.add_sdk_session("s1", "infra", title="SOKKAN session")
    board.set_claude_session_id("s1", "c-sokkan")
    a_usage = {"input_tokens": 1000, "output_tokens": 2000,
               "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 10_000,
               "cache_creation": {"ephemeral_1h_input_tokens": 10_000,
                                  "ephemeral_5m_input_tokens": 0}}
    _write(ws / "c-sokkan.jsonl", [
        {"type": "user", "message": {"content": "fix the build"}},
        _line("msg_A", "claude-opus-5-5", a_usage, "thinking"),
        _line("msg_A", "claude-opus-5-5", a_usage, "text"),
        _line("msg_A", "claude-opus-5-5", a_usage, "tool_use"),
        _line("msg_B", "claude-sonnet-5-5", {"input_tokens": 500, "output_tokens": 100,
                                              "cache_creation_input_tokens": 2000}),
        _line("msg_C", "claude-haiku-4-5-20251001", {"input_tokens": 1_000_000, "output_tokens": 0}),
        _line("msg_D", "claude-opus-9", {"input_tokens": 1000, "output_tokens": 10}),
        _line("err", "<synthetic>", {"input_tokens": 99, "output_tokens": 99}),
    ])
    _write(ws / "c-sokkan" / "subagents" / "agent-x.jsonl", [
        _line("msg_E", "claude-sonnet-5-5", {"input_tokens": 0, "output_tokens": 1_000_000}),
    ])
    # the operator's own CLI in the same folder: not a SOKKAN session
    _write(ws / "c-operator.jsonl", [
        _line("msg_X", "claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 0}),  # $4
    ])
    return {"tmp": tmp_path, "usage": usage, "llm": llm}


def test_message_counted_once_and_priced_from_the_table(world):
    s = world["usage"].summary(30)
    by = {m["model"]: m for m in s["by_model"]}
    a = by["claude-opus-5-5"]
    assert a["turns"] == 1                                     # 3 lines, 1 API message
    assert a["in_tokens"] == 1000 and a["out_tokens"] == 2000
    assert a["cache_read"] == 1_000_000 and a["cache_write"] == 10_000
    assert a["cost_in"] == pytest.approx(0.004)
    assert a["cost_out"] == pytest.approx(0.04)
    assert a["cost_cr"] == pytest.approx(0.2)
    assert a["cost_cw"] == pytest.approx(0.08)                 # 1-hour TTL = 2 × input
    assert a["api_equiv"] == pytest.approx(A)
    assert by["claude-sonnet-5-5"]["api_equiv"] == pytest.approx(B + E)
    assert by["claude-haiku-4-5-20251001"]["priced_as"] == "claude-haiku-4-5"
    assert by["claude-haiku-4-5-20251001"]["api_equiv"] == pytest.approx(C)
    unknown = by["claude-opus-9"]
    assert unknown["priced"] is False and unknown["api_equiv"] == 0 and unknown["in_tokens"] == 1000
    assert s["unpriced_models"] == ["claude-opus-9"]
    assert "<synthetic>" not in by
    assert s["pricing"]["version"] == "2026-09-25"


def test_subscription_basis_bills_nothing_and_shows_the_api_equivalent(world):
    s = world["usage"].summary(30)
    assert s["billing"]["claude"] == "subscription"            # no key: CLI login
    t = s["totals"]["today"]
    assert t["cost"] == 0.0
    assert t["api_equiv"] == pytest.approx(CLAUDE_TOTAL)
    assert t["metered"] == pytest.approx(CLAUDE_TOTAL)          # budgets still have a brake
    assert [b["basis"] for b in s["by_basis"]] == ["subscription"]
    # the session line carries its sub-agent
    (sess,) = s["sessions"]
    assert sess["session_id"] == "c-sokkan" and sess["subagents"] == 1
    assert sess["api_equiv"] == pytest.approx(CLAUDE_TOTAL)


def test_api_key_basis_bills_the_public_price(world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = world["usage"].summary(30)
    assert s["billing"]["claude"] == "api"
    assert s["totals"]["today"]["cost"] == pytest.approx(CLAUDE_TOTAL)
    assert s["totals"]["today"]["api_equiv"] == pytest.approx(CLAUDE_TOTAL)
    # byok in llm.json too
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    world["llm"].save({"mode": "byok", "anthropic_api_key": "sk-ant-x"})
    assert world["usage"].summary(30)["totals"]["today"]["cost"] == pytest.approx(CLAUDE_TOTAL)
    world["llm"].save({"mode": "byok", "claude_oauth_token": "sk-ant-oat-x"})
    assert world["usage"].summary(30)["totals"]["today"]["cost"] == 0.0


def test_external_transcripts_are_reported_apart(world, monkeypatch):
    s = world["usage"].summary(30)
    ext = s["sources"]["external"]
    assert ext["transcripts"] == 1 and ext["api_equiv"] == pytest.approx(4.0)
    assert ext["counted"] is False
    assert s["totals"]["today"]["api_equiv"] == pytest.approx(CLAUDE_TOTAL)   # not + $4
    monkeypatch.setenv("SOKKAN_USAGE_EXTERNAL", "include")
    s = world["usage"].summary(30)
    assert s["totals"]["today"]["api_equiv"] == pytest.approx(CLAUDE_TOTAL + 4.0)


def test_gateway_and_local_bases(world, monkeypatch):
    import agentcost

    ws = world["usage"].PROJECT_DIR
    import board
    board.add_sdk_session("s2", "ops", title="on the gateway")
    board.set_claude_session_id("s2", "c-gw")
    _write(ws / "c-gw.jsonl", [
        _line("g1", "sokkan-ship", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}),
        _line("l1", "qwen3-8b", {"input_tokens": 5_000_000, "output_tokens": 5_000_000}),
    ])
    monkeypatch.setattr(agentcost, "_tiers", lambda: [
        {"id": "sokkan-ship", "chf_per_mtok_in": 0.4, "chf_per_mtok_out": 1.5}])
    monkeypatch.setenv("SOKKAN_FX_USD_PER_CHF", "1.30")
    s = world["usage"].summary(30)
    by = {b["basis"]: b for b in s["by_basis"]}
    assert by["gateway"]["billed"] == pytest.approx((0.4 + 1.5) * 1.30)     # CHF tier → USD
    assert by["local"]["billed"] == 0.0 and by["local"]["out_tokens"] == 5_000_000
    assert by["subscription"]["billed"] == 0.0
    assert s["totals"]["today"]["cost"] == pytest.approx((0.4 + 1.5) * 1.30)


def test_project_spend_uses_the_metered_figure(world):
    spend = world["usage"].project_spend("default")
    assert spend["day"] == pytest.approx(CLAUDE_TOTAL) and spend["month"] == pytest.approx(CLAUDE_TOTAL)


def test_operator_price_override(world, monkeypatch, tmp_path):
    import pricing

    extra = tmp_path / "prices.json"
    extra.write_text(json.dumps({"models": {"claude-opus-9": {"input": 1, "output": 1,
                                                               "cache_read": 0.1}}}))
    monkeypatch.setenv("SOKKAN_CLAUDE_PRICES", str(extra))
    assert pricing.price("claude-opus-9")["input"] == 1
    assert pricing.canonical("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert pricing.canonical("us.anthropic.claude-sonnet-5-5-v1:0") == "claude-sonnet-5-5"
    assert pricing.canonical("sonnet") == "claude-sonnet-5-5"
    assert pricing.canonical("claude-opus") is None            # no prefix guessing
