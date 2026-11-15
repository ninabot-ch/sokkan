"""4.0 kernel engine, cockpit side: what an engine whose approval ends its turn looks like in
the pane — the text it writes while a decision is awaited is flagged (`pending`: a discreet
note, decision of 15.11.2026), the answer after the decision is not."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import agentchat  # noqa: E402
from harness import contract  # noqa: E402


def test_text_written_while_a_gate_waits_is_a_note():
    s = agentchat.AgentSession("sid-kernel-note", cwd="/tmp")
    s._on_gate = lambda gate: None          # nobody decides here: only the flow is read
    gate = contract.Gate(gate_id="ap-1", kind="file_write",
                         subject={"tool": "write_file", "input": {"path": "x.md"}, "path": "x.md"},
                         turn_id="t1")
    s._consume(contract.GateRequested(gate=gate))
    s._consume(contract.AssistantText(turn_id="t1", text="J'attends votre accord."))
    s._consume(contract.GateResolved(gate_id="ap-1", decision="allow_once", via="rail"))
    s._consume(contract.AssistantText(turn_id="t1", text="Le mail est enregistré."))
    texts = [e for e in s.events if e["type"] == "text"]
    assert texts[0] == {"type": "text", "text": "J'attends votre accord.", "pending": True}
    assert texts[1] == {"type": "text", "text": "Le mail est enregistré."}


def test_the_kernel_tools_have_titles():
    assert agentchat._tool_title("terminal", {"command": "ls -la"}) == "ls -la"
    assert agentchat._tool_title("write_file", {"path": "Relance.md"}) == "Relance.md"
