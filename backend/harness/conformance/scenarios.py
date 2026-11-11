"""The conformance scenarios C1–C8. Each takes a `Rig` and raises `AssertionError` when
the engine does not conform. They assert on contract events and on effects (a file that is
or is not there), never on anything of a particular engine."""
from __future__ import annotations

import asyncio

from ..contract import (
    ANSWER, BASES, ContextBlock, ContextInjected, Decision, Error, GateRequested,
    GateResolved, RunInfo, ToolResult, Unsupported, Usage,
)
from .driver import Rig, Run, allow, ask, deny, say, shell, write


def _run(rig: Rig, **spec) -> Run:
    return Run(rig.harness(), rig.spec(**spec))


async def c1_shell_gate_denied(rig: Rig) -> None:
    """A shell command that deletes a file in the run's directory: one `shell` gate
    reaches the cockpit; denied, the command has not run (the file is the proof)."""
    victim = rig.workspace / "victim.txt"
    victim.write_text("keep me\n")
    rig.script([shell("rm victim.txt")])
    async with _run(rig) as run:
        t = await run.turn("Clean the directory.", decide=deny)
    gates = [e.gate for e in t.of(GateRequested)]
    assert len(gates) == 1, f"expected one gate, got {[g.kind for g in gates]}"
    assert gates[0].kind == "shell", gates[0].kind
    assert "rm victim.txt" in gates[0].subject.get("command", ""), gates[0].subject
    (resolved,) = t.of(GateResolved)
    assert resolved.gate_id == gates[0].gate_id and resolved.decision == "deny"
    assert victim.exists(), "the command ran although the gate was denied"
    assert any(not r.ok for r in t.of(ToolResult)), "the model was not told of the refusal"
    assert t.ended.reported, "the turn must end normally after a refusal"


async def c2_context_injected(rig: Rig) -> None:
    """The cockpit has a note that answers the prompt: the engine is given it, says so
    (`context.injected`, with the note's name), and the model is shown it."""
    note = "The harbour office closes at 16:30 on Fridays (note harbour-hours)."
    asked = []

    async def provider(req):
        asked.append(req)
        if req.channel != "prompt":
            return None
        return ContextBlock(text=f"[memory] {note}", note_names=("harbour-hours",))

    rig.script([say("At 16:30.")])
    async with _run(rig, context=provider) as run:
        t = await run.turn("When does the harbour office close on Fridays?")
    assert asked and "harbour office close" in asked[0].text, "the provider was not asked"
    injected = t.of(ContextInjected)
    assert len(injected) == 1, f"expected one context.injected, got {len(injected)}"
    inj = injected[0]
    assert inj.note_names == ("harbour-hours",) and inj.chars == len(f"[memory] {note}")
    assert inj.source in rig.capabilities().context, (inj.source, rig.capabilities().context)
    assert rig.model_saw(note), "context.injected was said but the model did not see the note"


async def c3_write_outside_refused(rig: Rig) -> None:
    """A confined run writes outside its space: refused, said as a recoverable `error`,
    and the run goes on (the next write, inside, works)."""
    leak = rig.outside / "leak.txt"
    rig.script([write(str(leak), "should never exist\n"), write("inside.txt", "fine\n")])
    async with _run(rig, confined=True) as run:
        t = await run.turn("Write both files.", decide=allow)
    refused = [e for e in t.of(Error) if e.code == "confined"]
    assert refused, "no `confined` error for a write outside the run's space"
    assert all(e.recoverable for e in refused)
    assert not leak.exists(), "the file outside the run's space was written"
    assert (rig.workspace / "inside.txt").read_text() == "fine\n", "the run did not go on"
    assert t.ended.reported and not t.ended.is_error


async def c4_resume(rig: Rig) -> None:
    """The run is stopped (the process that held it is gone), then resumed: the same
    engine session, the history intact — or the adapter says honestly that it cannot."""
    rig.script([say("Noted.")], [say("heron")])
    async with _run(rig) as run:
        await run.turn("Remember this word: heron.")
    ended = run.ended
    assert ended is not None, "no run.ended after stop()"
    if not rig.capabilities().resume:
        assert ended.resumable is False, "resume is declared absent but run.ended says resumable"
        return
    assert ended.resumable and ended.engine_session, ended
    infos = [e.engine_session for e in run.events if isinstance(e, RunInfo) and e.engine_session]
    assert infos and infos[-1] == ended.engine_session, "run.info never gave the engine session"
    async with _run(rig, resume=ended.engine_session) as again:
        assert again.started.resumed_from == ended.engine_session
        t = await again.turn("Which word did I ask you to remember?")
    assert t.ended.reported and not t.ended.is_error
    assert rig.last_request_saw("Remember this word: heron."), "the history was lost on resume"


async def c5_usage_per_turn(rig: Rig) -> None:
    """Three turns: each one reports what it consumed (model, tokens), as message events
    that are never double counted."""
    rig.script([say("one")], [say("two")], [say("three")])
    seen: set[str] = set()
    async with _run(rig) as run:
        for prompt in ("First.", "Second.", "Third."):
            t = await run.turn(prompt)
            msgs = [u for u in t.of(Usage) if u.scope == "message"]
            assert msgs, f"no usage event for the turn {prompt!r}"
            for u in msgs:
                assert u.turn_id == t.turn_id and u.model, u
                assert u.basis is None or u.basis in BASES, u.basis
                assert min(u.input, u.output, u.cache_read, u.cache_write) >= 0
            assert sum(u.input + u.output for u in msgs) > 0, "a turn with no token at all"
            ids = {u.message_id for u in msgs if u.message_id}
            assert not (ids & seen), "a message id of an earlier turn came back"
            seen |= ids


async def c6_decision_held_and_attributed(rig: Rig) -> None:
    """What a second approver needs: while the cockpit holds its decision nothing runs;
    who decided and through what is echoed by the engine; a decision is applied once."""
    made = rig.workspace / "made.txt"
    rig.script([shell("touch made.txt")])
    async with _run(rig) as run:
        turn_id = await run.harness.send("Create the file.")
        gate = None
        while gate is None:
            ev = await run.next_event()
            if isinstance(ev, GateRequested):
                gate = ev.gate
        await asyncio.sleep(0.3)        # a first person approved: the cockpit still holds
        assert not made.exists(), "the command ran before the cockpit decided"
        assert await run.harness.decide("no-such-gate", Decision.allow()) is False
        second = Decision.allow(by="second@example.org", via="teams")
        assert await run.harness.decide(gate.gate_id, second) is True
        assert await run.harness.decide(gate.gate_id, Decision.deny("late")) is False
        resolved = None
        while True:
            ev = await run.next_event()
            if isinstance(ev, GateResolved):
                resolved = ev
            if getattr(ev, "type", "") == "turn.ended" and ev.turn_id == turn_id:
                break
    assert resolved is not None and resolved.gate_id == gate.gate_id
    assert (resolved.decision, resolved.by, resolved.via) == (
        "allow_once", "second@example.org", "teams"), resolved
    assert made.exists(), "allowed, and the command did not run"


async def c7_capabilities_are_honest(rig: Rig) -> None:
    """A capability declared absent is absent — no gate of that kind, no emulation — and
    one declared present works: the plan mode, the question with choices."""
    caps = rig.capabilities()
    rig.script([ask("Which environment?", ("staging", "production"))])

    def decide(gate):
        return Decision.answer({"Which environment?": "staging"}, by="conformance",
                               via="rail") if gate.kind == "question" else allow(gate)

    async with _run(rig) as run:
        if caps.plan:
            await run.harness.set_mode("plan")
            await run.harness.set_mode("default")
        else:
            try:
                await run.harness.set_mode("plan")
            except Unsupported:
                pass
            else:
                raise AssertionError("plan is declared absent but set_mode('plan') passed")
        t = await run.turn("Ask me which environment, then stop.", decide=decide)
    questions = [e.gate for e in t.of(GateRequested) if e.gate.kind == "question"]
    if caps.question:
        assert len(questions) == 1, "the question never reached the cockpit as a gate"
        assert ANSWER in questions[0].options
        asked = questions[0].input.get("questions") or []
        assert asked and asked[0].get("question") == "Which environment?", questions[0].input
        (resolved,) = [e for e in t.of(GateResolved) if e.gate_id == questions[0].gate_id]
        assert resolved.decision == ANSWER
        assert rig.model_saw("staging"), "the answer did not reach the model"
    else:
        assert not questions, "a question gate from an engine that declares none"
        assert "question" not in caps.gate_kinds and ANSWER not in caps.decisions
    assert t.ended.reported


async def c8_only_what_the_cockpit_returns(rig: Rig) -> None:
    """The cockpit filtered the recall for this person (project, clearance): the engine
    injects exactly that. What the cockpit withheld never reaches the model, and when the
    cockpit returns nothing, nothing is injected."""
    public = "Deliveries are on Tuesdays (note delivery-days)."
    restricted = "The safe code is 4471 (note safe-code)."   # exists, above the clearance
    calls = []

    async def provider(req):
        calls.append(req)
        if "safe" in req.text:
            return None                 # nothing this person may read
        return ContextBlock(text=f"[memory] {public}", note_names=("delivery-days",))

    rig.script([say("Tuesdays.")], [say("I do not know.")])
    async with _run(rig, context=provider) as run:
        t1 = await run.turn("When are the deliveries?")
        t2 = await run.turn("What is the safe code?")
    assert [i.note_names for i in t1.of(ContextInjected)] == [("delivery-days",)]
    assert t2.of(ContextInjected) == [], "context.injected although the cockpit returned nothing"
    assert [c.channel for c in calls] == ["prompt", "prompt"], \
        "the provider must be asked once per prompt"
    assert rig.model_saw(public)
    assert not rig.model_saw(restricted) and not rig.model_saw("safe-code"), \
        "the model saw a note the cockpit withheld"


ALL = (
    ("C1", c1_shell_gate_denied),
    ("C2", c2_context_injected),
    ("C3", c3_write_outside_refused),
    ("C4", c4_resume),
    ("C5", c5_usage_per_turn),
    ("C6", c6_decision_held_and_attributed),
    ("C7", c7_capabilities_are_honest),
    ("C8", c8_only_what_the_cockpit_returns),
)
