"""4.0 — the kernel adapter on kernel host-3c (beta-core 99ca9ca3): a refused card no longer ends
the turn (`POST …/approvals/{id}/decide` → 202 + continuation, a `tool.result` `declined` first,
then the model goes on, C3); a file tool outside the run's folder is refused `outside_root`.
Still compatible with the older 200 + `continuation: "none"`. Without a kernel: the decide
call is answered by httpx.MockTransport, the kernel's events are fed to `_on_event`."""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import plain  # noqa: E402
from harness import contract, kernel, refusals  # noqa: E402

TH = "th_0123456789abcdef"


def _harness(status: int, continuation: str | None):
    h = kernel.KernelHarness()
    h._thread, h._turn, h._opened = TH, "turn-1", True

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/local/approvals/ap1/decide"
        body = {} if continuation is None else {"continuation": continuation}
        return httpx.Response(status, json=body)
    h._client = httpx.AsyncClient(base_url="http://kernel.test",
                                  transport=httpx.MockTransport(handler))
    h._on_card({"id": "ap1", "tool_name": "write_file", "call_id": "c1", "turn_index": 0,
                "arguments": {"path": "/workspace/a.md", "content": "x"}})
    return h


def _drain(h) -> list:
    out = []
    while not h._mail.empty():
        out.append(h._mail.get_nowait())
    return out


def _kernel_event(h, event: str, **d):
    h._on_event(event, {"thread_id": TH, **d})


def _declined_result(h):
    _kernel_event(h, "tool.result", call_id="c1", name="write_file", approval_id="ap1",
                  status="refused", reason="declined", preview="refusé par local-owner.",
                  is_error=False)


def _decide(h, decision):
    async def go():
        ok = await h.decide("ap1", decision)
        await h._client.aclose()
        return ok
    return asyncio.run(go())


def _results(evs):
    return [e for e in evs if isinstance(e, contract.ToolResult)]


def _ended(evs):
    return [e for e in evs if isinstance(e, contract.TurnEnded)]


def test_a_refused_card_waits_for_the_continuation_and_says_one_refusal():
    h = _harness(202, "running")
    _drain(h)
    assert _decide(h, contract.Decision.deny("", by="person-1", via="rail"))
    evs = _drain(h)
    assert not _ended(evs), "the turn goes on after a refusal (202 + continuation)"
    assert h._expect_done == 1
    _declined_result(h)                         # the kernel's own result, after the decision
    _kernel_event(h, "turn.started", turn_id=f"{TH}:1")
    _kernel_event(h, "turn.delta", turn_id=f"{TH}:1", text="D'accord, je n'enregistre pas.")
    _kernel_event(h, "turn.done", turn_id=f"{TH}:1")
    evs += _drain(h)
    res = _results(evs)
    assert len(res) == 1 and res[0].refused and not res[0].ok
    assert res[0].output.startswith(refusals.DECLINED)
    ended = _ended(evs)
    assert len(ended) == 1 and ended[0].text == "D'accord, je n'enregistre pas."


def test_the_kernels_declined_result_before_the_answer_is_the_only_one():
    h = _harness(202, "queued")
    _drain(h)
    _declined_result(h)                         # published before decide() returned
    assert _decide(h, contract.Decision.deny("", by="person-1"))
    _kernel_event(h, "turn.started", turn_id=f"{TH}:1")
    _kernel_event(h, "turn.done", turn_id=f"{TH}:1")
    evs = _drain(h)
    res = _results(evs)
    assert len(res) == 1 and res[0].refused
    assert len(_ended(evs)) == 1


def test_an_older_kernel_ends_the_turn_on_a_refusal():
    h = _harness(200, "none")
    _drain(h)
    assert _decide(h, contract.Decision.deny("", by="person-1"))
    evs = _drain(h)
    assert len(_results(evs)) == 1 and _results(evs)[0].refused
    assert len(_ended(evs)) == 1 and h._expect_done == 0


def test_a_refusal_is_read_as_the_cockpit_decided_it():
    person = refusals.declined("", "refusé par local-owner.")
    rule = refusals.declined("No shell in this conversation: the person is not a developer.",
                             "refusé par local-owner.")
    assert any(m in person for m in plain.PERSON_DENIED)
    assert any(m in rule for m in plain.POLICY_DENIED)
    assert not any(m in rule for m in plain.PERSON_DENIED)


def test_outside_root_is_a_rule_of_the_space():
    h = kernel.KernelHarness()
    h._thread, h._turn = TH, "turn-1"
    _kernel_event(h, "tool.result", call_id="c9", name="read_file", status="refused",
                  reason="outside_root", preview="Refusé : hors de /workspace.", is_error=False)
    res = _results(_drain(h))
    assert len(res) == 1 and res[0].refused and not res[0].ok
    assert res[0].output.startswith(refusals.OUTSIDE_ROOT)
    assert any(m in res[0].output for m in plain.POLICY_DENIED)


def test_an_approved_card_still_runs_and_continues():
    h = _harness(200, "running")
    _drain(h)
    assert _decide(h, contract.Decision.allow(by="person-1"))
    assert h._expect_done == 1
    _kernel_event(h, "tool.result", call_id="c1", name="write_file", approval_id="ap1",
                  preview="ok", is_error=False)
    _kernel_event(h, "turn.started", turn_id=f"{TH}:1")
    _kernel_event(h, "turn.done", turn_id=f"{TH}:1")
    evs = _drain(h)
    res = _results(evs)
    assert len(res) == 1 and res[0].ok and not res[0].refused
    assert len(_ended(evs)) == 1
