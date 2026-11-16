"""The parity bench's own logic (tests/parity): the meter on the wire, the cockpit's rules as
the bench plays them, the grading helpers and the verdict. No engine, no model."""
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "parity"))
sys.path.insert(0, str(HERE.parent / "backend"))

import journeys  # noqa: E402
import policy  # noqa: E402
import report  # noqa: E402
import wire  # noqa: E402
from harness.contract import Gate  # noqa: E402


# ---- the meter ---------------------------------------------------------------------------
def test_usage_is_normalised_the_same_way_for_both_formats():
    m = wire.normalise("messages", {"input_tokens": 1584, "output_tokens": 5,
                                    "cache_read_input_tokens": 832})
    assert m == {"prompt_total": 1584, "prompt_cached": 832, "prompt_uncached": 752,
                 "output": 5, "reasoning": 0}
    # Anthropic's own API counts the input WITHOUT the cache
    a = wire.normalise("messages", {"input_tokens": 100, "output_tokens": 5,
                                    "cache_read_input_tokens": 800, "cache_creation_input_tokens": 50},
                       messages_input_includes_cache=False)
    assert a["prompt_total"] == 950 and a["prompt_uncached"] == 150
    c = wire.normalise("chat", {"prompt_tokens": 1584, "completion_tokens": 12,
                                "prompt_tokens_details": {"cached_tokens": 832},
                                "completion_tokens_details": {"reasoning_tokens": 3}})
    assert c == {"prompt_total": 1584, "prompt_cached": 832, "prompt_uncached": 752,
                 "output": 12, "reasoning": 3}


def test_usage_of_a_stream():
    msgs = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":900,"cache_read_input_tokens":400,"output_tokens":1}}}\n\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n')
    u = wire.usage_from_sse("messages", msgs)
    assert u["input_tokens"] == 900 and u["output_tokens"] == 42
    chat = (b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
    assert wire.usage_from_sse("chat", chat)["prompt_tokens"] == 7


def test_reasoning_is_turned_off_on_both_formats_and_the_stream_carries_its_usage():
    b = wire.shape("messages", {"model": "x", "thinking": {"type": "enabled"}}, thinking=False)
    assert "thinking" not in b and b["chat_template_kwargs"] == {"enable_thinking": False}
    c = wire.shape("chat", {"stream": True}, thinking=False)
    assert c["stream_options"]["include_usage"] is True
    assert "chat_template_kwargs" not in wire.shape("chat", {}, thinking=True)


def test_the_meter_forwards_and_records_each_request():
    seen = []

    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            out = json.dumps({"choices": [{"message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    log = HERE / ".parity-wire-test.jsonl"
    try:
        with wire.Wire(f"http://127.0.0.1:{up.server_address[1]}/v1", str(log), model="m1") as w:
            w.tag = "kernel/A4/p1"
            req = urllib.request.Request(w.url + "/v1/chat/completions", method="POST",
                                         data=json.dumps({"model": "other", "messages": []}).encode(),
                                         headers={"Content-Type": "application/json"})
            assert json.load(urllib.request.urlopen(req, timeout=5))["choices"][0]["message"]["content"] == "ok"
            recs = w.of("kernel/A4/p1")
        assert seen[0][0] == "/v1/chat/completions" and seen[0][1]["model"] == "m1"
        assert seen[0][1]["chat_template_kwargs"] == {"enable_thinking": False}
        assert len(recs) == 1 and recs[0]["prompt_total"] == 10 and recs[0]["output"] == 2
        assert wire.totals(recs)["requests"] == 1
    finally:
        up.shutdown()
        if log.exists():
            os.unlink(log)


# ---- the cockpit's rules -----------------------------------------------------------------
def _gate(kind, **subject):
    return Gate(gate_id=f"g-{kind}-{len(subject)}-{id(subject)}", kind=kind, subject=subject)


def test_atelier_rules(tmp_path):
    deliv, mem = tmp_path / "deliverables" / "s1", tmp_path / "memory"
    deliv.mkdir(parents=True)
    mem.mkdir()
    p = policy.Policy("atelier", deliverables=deliv, memory=mem, has_documents=True)
    p.new_turn()
    assert p.decide(_gate("shell", tool="Bash", command="mkdir archives")).kind == "deny"
    assert p.decide(_gate("file_write", tool="Write", path=str(mem / "n.md"))).kind == "deny"
    doc = str(deliv / "Quiz.md")
    assert p.decide(_gate("file_write", tool="Write", path=doc)).kind == "allow_once"
    assert p.decide(_gate("file_write", tool="Edit", path=doc)).kind == "deny"   # 2nd save
    p.new_turn()
    assert p.decide(_gate("file_write", tool="Edit", path=doc)).kind == "allow_once"
    s = p.summary()
    assert s["shell_refused"] == 1 and s["memory_write_refused"] == 1
    assert s["second_saves_refused"] == 1 and s["max_cards_per_document_per_turn"] == 1
    assert s["deliverable_cards"] == 2 and s["cards_shown_to_person"] == 2


def test_a_deliverable_reached_through_an_alias_is_still_the_deliverable(tmp_path):
    deliv = tmp_path / "deliverables" / "0123456789abcdef"
    deliv.mkdir(parents=True)
    alias = tmp_path / ".sokkan-kernel" / "f1"
    alias.parent.mkdir()
    alias.symlink_to(deliv)
    p = policy.Policy("atelier", deliverables=deliv)
    p.new_turn()
    p.decide(_gate("file_write", tool="write_file", path=str(alias / "Mail.md")))
    assert p.journal[-1].deliverable


def test_cockpit_rule_approves_everything_and_says_so():
    p = policy.Policy("cockpit")
    p.new_turn()
    d = p.decide(_gate("shell", tool="terminal", command="python3 -m unittest"))
    assert d.kind == "allow_once" and d.by == "policy" and p.journal[0].rule == "rule:bypass"


# ---- grading and verdict -------------------------------------------------------------------
def test_invented_numbers_and_language():
    import run
    src = ["Total TTC 5 188.80 CHF, devis 2026-117, validité 30 jours"]
    assert run.invented_numbers("Le total est de 5'188.80 CHF (devis 2026-117).", src) == []
    assert run.invented_numbers("Remise de 450 CHF", src) == ["450"]
    assert run.language("Bonjour, voici le mail pour vous et votre équipe dans la journée.")["french"]
    assert not run.language("Here is the document and the plan with your notes, please.")["french"]


def _run(engine, journey, kind, ok, **kw):
    base = {"engine": engine, "journey": journey, "kind": kind, "started": True, "pass_no": 1,
            "grade": {"success": ok}, "gaps": [], "wire": {"prompt_total": 1000, "requests": 4},
            "cards": {"cards_shown_to_person": 1, "max_cards_per_document_per_turn": 1},
            "failures": {}, "first_draft_s": 10.0}
    base.update(kw)
    return base


def test_verdict_parity_then_gap_then_gap_closed():
    data = {"passes": 1, "conformance": {"kernel": {"passed": 11, "failed": 0, "skipped": 0}},
            "runs": [_run("legacy", "A4", "atelier", True), _run("kernel", "A4", "atelier", True,
                                                                 wire={"prompt_total": 200})]}
    assert report.verdict(data)["overall"] == "PARITÉ"
    data["runs"].append(_run("kernel", "R1b", "refusal", False, started=False, status="waits",
                             gaps=[{"check": "deny_tools", "gap": 2, "status": "waits"}]))
    v = report.verdict(data)
    assert v["overall"] == "INCOMPLET" and v["gaps_open"] == [2]
    assert "attend noyau n°2" in report.markdown(data)
    data["runs"][-1] = _run("kernel", "R1b", "refusal", True)
    data["runs"].append(_run("kernel", "A3", "atelier", True, first_draft_s=60.0))
    data["runs"].append(_run("legacy", "A3", "atelier", True, first_draft_s=20.0))
    v = report.verdict(data)
    assert v["overall"] == "PAS DE PARITÉ"
    assert any("T2" in w for r in v["rows"] for w in r["why"])


def test_every_kernel_gap_quoted_by_a_journey_is_named():
    for j in journeys.all_journeys():
        for gap in (j.needs.get("kernel") or {}).values():
            assert gap in journeys.KERNEL_GAPS
