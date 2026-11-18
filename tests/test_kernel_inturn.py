"""4.0 — the kernel adapter on kernel host-3e (beta-core 70ecd121): a card decided within the
turn's wait (`local.approval_wait_s`) is applied INSIDE the turn (`continuation: inline`, one
result of the same call_id without approval_id, no continuation turn); a refusal sends its
reason (`reason` → `decision_reason`, ≤ 500 characters); a second decision answers 409
not_pending (decided already: read back, never guessed). Without a kernel: MockTransport."""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract, kernel  # noqa: E402

TH = "th_0123456789abcdef"


class _K:
    def __init__(self, status=202, continuation="inline", card=None):
        self.status, self.continuation, self.card = status, continuation, card or {}
        self.decided: list[dict] = []
        self.on_decide = None           # events the kernel publishes while it decides

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/local/approvals/ap1/decide":
            self.decided.append(json.loads(req.content))
            if self.on_decide:
                self.on_decide()
            if self.status == 409:
                return httpx.Response(409, json={"error": {"code": "not_pending"}})
            return httpx.Response(self.status, json={"continuation": self.continuation})
        if req.url.path == "/v1/approvals/ap1":
            return httpx.Response(200, json=self.card)
        return httpx.Response(404, json={})


def _harness(k: _K):
    h = kernel.KernelHarness()
    h._thread, h._turn, h._opened = TH, "turn-1", True
    h._expect_done = 1                  # as send() leaves it: the turn's own turn.done
    h._client = httpx.AsyncClient(base_url="http://kernel.test", transport=httpx.MockTransport(k.handler))
    _ev(h, "turn.started", turn_id=f"{TH}:0")
    h._on_card({"id": "ap1", "tool_name": "write_file", "call_id": "c1", "turn_index": 0,
                "arguments": {"path": "/workspace/Relance.md", "content": "x"}})
    return h


def _ev(h, event, **d):
    h._on_event(event, {"thread_id": TH, **d})


def _drain(h):
    out = []
    while not h._mail.empty():
        out.append(h._mail.get_nowait())
    return out


def _decide(h, decision):
    async def go():
        ok = await h.decide("ap1", decision)
        await h._client.aclose()
        return ok
    return asyncio.run(go())


def _of(evs, t):
    return [e for e in evs if isinstance(e, t)]


def test_a_card_decided_in_the_turn_is_one_turn_and_one_result():
    k = _K(202, "inline")
    h = _harness(k)
    _drain(h)
    # the kernel runs the tool under the turn's lock as soon as it is decided: its result may
    # come before the answer of `decide`
    k.on_decide = lambda: _ev(h, "tool.result", call_id="c1", name="write_file",
                              preview='{"bytes_written": 1}', is_error=False)
    assert _decide(h, contract.Decision.allow(by="parity-person", via="rail"))
    _ev(h, "turn.delta", turn_id=f"{TH}:0", text="Le mail est enregistré.")
    _ev(h, "turn.done", turn_id=f"{TH}:0")
    evs = _drain(h)
    res = _of(evs, contract.ToolResult)
    assert len(res) == 1 and res[0].ok and res[0].call_id == "c1"
    assert len(_of(evs, contract.GateResolved)) == 1
    ended = _of(evs, contract.TurnEnded)
    assert len(ended) == 1 and ended[0].text == "Le mail est enregistré."
    assert h._expect_done == 0          # no continuation turn awaited


def test_the_placeholder_before_a_decision_is_still_not_a_result():
    k = _K(202, "inline")
    h = _harness(k)
    _ev(h, "tool.result", call_id="c1", name="write_file", is_error=False,
        preview="EN ATTENTE D'APPROBATION HUMAINE [approval:3138e432-4979-42c7-ba2d-2452114224fe]")
    assert not _of(_drain(h), contract.ToolResult)


def test_a_refusal_in_the_turn_carries_its_reason_and_says_one_refusal():
    k = _K(202, "inline")
    h = _harness(k)
    _drain(h)
    defects = "Le total TTC indiqué (5 000.00) ne correspond pas au devis (5 188.80). Corrigez-le."
    k.on_decide = lambda: _ev(h, "tool.result", call_id="c1", name="write_file", status="refused",
                              reason="declined", decision_reason=defects, is_error=False,
                              preview="Refusé par la personne")
    assert _decide(h, contract.Decision.deny(defects, by="policy", via="auto_rule"))
    assert k.decided[0]["decision"] == "reject" and k.decided[0]["reason"] == defects
    _ev(h, "turn.done", turn_id=f"{TH}:0")
    evs = _drain(h)
    res = _of(evs, contract.ToolResult)
    assert len(res) == 1 and res[0].refused and defects in res[0].output
    assert len(_of(evs, contract.TurnEnded)) == 1


def test_a_plain_no_sends_no_reason_and_a_long_one_is_cut_at_a_word():
    assert kernel.refusal_reason(contract.Decision.deny("Denied by the user")) == ""
    assert kernel.refusal_reason(contract.Decision.deny("")) == ""
    assert kernel.refusal_reason(contract.Decision.allow()) == ""
    assert kernel.refusal_reason(contract.Decision.deny("Mettez la date du 3 décembre.")) \
        == "Mettez la date du 3 décembre."
    long = kernel.refusal_reason(contract.Decision.deny(("défaut numéro " * 60).strip()))
    assert len(long) <= 500 and long.endswith("…") and not long[:-1].endswith("numé")
    k = _K(202, "running")
    h = _harness(k)
    _decide(h, contract.Decision.deny("Denied by the user", by="u"))
    assert "reason" not in k.decided[0]


def test_a_card_already_decided_is_read_back():
    k = _K(409, card={"status": "executed", "decided_by": "local-owner"})
    h = _harness(k)
    _drain(h)
    assert not _decide(h, contract.Decision.deny("Mauvaise date", by="u"))
    _ev(h, "turn.done", turn_id=f"{TH}:0")   # announced once the raising turn is done
    res = _of(_drain(h), contract.GateResolved)
    assert len(res) == 1 and res[0].decision == contract.ALLOW_ONCE


def test_after_the_wait_the_usual_path_still_holds():
    k = _K(202, "queued")
    h = _harness(k)
    _drain(h)
    assert _decide(h, contract.Decision.allow(by="u"))
    assert h._expect_done == 2          # the raising turn, then the continuation turn
    _ev(h, "turn.done", turn_id=f"{TH}:0")
    _ev(h, "tool.result", call_id="c1", name="write_file", approval_id="ap1", preview="ok",
        is_error=False)
    _ev(h, "turn.started", turn_id=f"{TH}:1")
    _ev(h, "turn.done", turn_id=f"{TH}:1")
    evs = _drain(h)
    assert len(_of(evs, contract.ToolResult)) == 1 and len(_of(evs, contract.TurnEnded)) == 1
