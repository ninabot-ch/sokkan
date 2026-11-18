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
    second = p.decide(_gate("file_write", tool="Edit", path=doc))      # 2nd save, same turn
    assert (second.kind, second.by, second.via) == ("allow_once", policy.PERSON, "auto_rule")
    p.new_turn()
    assert p.decide(_gate("file_write", tool="Edit", path=doc)).kind == "allow_once"
    s = p.summary()
    assert s["shell_refused"] == 1 and s["memory_write_refused"] == 1
    assert s["same_document_by_rule"] == 1 and s["max_cards_per_document_per_turn"] == 1
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


def test_a_quiz_counts_its_numbered_stems_and_its_answer_key():
    """Bench n°4: « ? » alone counted 6 questions for 8 (stems ending in « … »), and a change
    made in several saves (the 2nd turned away by rule) left a 9th question without its line in
    the key — the check `change_complete` reads both parts."""
    import run
    q = "\n".join(f"**{i}.** La plage doit être…\n\nA. oui\nB. non\n" for i in range(1, 10))
    key = "\n".join(f"| {i} | B | parce que |" for i in range(1, 9))
    c = run.quiz_counts(f"# Quiz\n\nPuis le corrigé.\n\n## Questions\n{q}\n## Corrigé\n\n| N° | R | Pourquoi |\n|---|---|---|\n{key}\n")
    assert c == {"questions": 9, "numbered": 9, "key_entries": 8, "has_key": True}
    c = run.quiz_counts("# Quiz\n### Question 1\nQuoi ?\n### Question 2\nQui ?\n## Réponses\n1. a\n2. b\n")
    assert c["questions"] == 2 and c["key_entries"] == 2
    c = run.quiz_counts("# Quiz\n## Questions\n**1.** Quoi ?\n## La correction\n| 1 | a |\n")
    assert c["has_key"] and c["key_entries"] == 1
    c = run.quiz_counts("# Quiz\n### Question 1\nQuoi ?\n### Question 2\nQui ?\n## Barème\n| 1 | a |\n| 2 | b |\n")
    assert c["has_key"] and c["key_entries"] == 2
    c = run.quiz_counts("# Quiz\n**Question 1**\nQuoi ?\n## Réponses et explications\n"
                        "**Réponse à la question 1**\nB\n")
    assert c["key_entries"] == 1
    c = run.quiz_counts("# Quiz\n1. Quoi ?\n2. Qui ?\n\n**Pour l'animateur**\n1. a\n2. b\n")
    assert c == {"questions": 2, "numbered": 2, "key_entries": 2, "has_key": True}
    assert journeys.ATELIER[0].expect.questions_after_change == 9


def test_the_second_save_refusal_is_the_products_own_words():
    """The bench mirrors agentchat.py: plain.POLICY_DENIED reads it as a rule of the space."""
    import plain
    assert any(m in policy.ONE_CARD for m in plain.POLICY_DENIED)


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


def test_a_pass_without_a_decided_card_is_not_played_not_a_kernel_gap():
    """n°3 (who decided, in the kernel's journal): no card decided in a pass = nothing to
    check, said « non joué » — never « attend noyau n°3 » (bench n°6, R1). A card decided
    and not named by the kernel still waits."""
    assert not report.journal_waits({"status": "not played", "cards_found": 0, "gap": None})
    # results written before the « not played » status
    assert not report.journal_waits({"status": "waits", "cards_found": 0, "gap": 3})
    assert report.journal_waits({"status": "waits", "cards_found": 1, "gap": 3})
    assert not report.journal_waits({"status": "pass", "cards_found": 2})
    run = {"kind": "refusal", "journey": "R1-pas-de-shell", "engine": "kernel", "pass_no": 1,
           "started": True, "grade": {"success": True, "checks": {}}, "gaps": [],
           "kernel_journal": {"status": "waits", "cards_found": 0, "gap": 3, "sample": []}}
    st = report.stats([run])
    assert 3 not in st["gaps"]
    md = report.markdown({"runs": [run, {**run, "engine": "legacy", "kernel_journal": None}],
                        "conformance": {}})
    assert "attend noyau n°3" not in md
    assert "R1-pas-de-shell p1 — décideur dans le journal du noyau : non joué" in md
