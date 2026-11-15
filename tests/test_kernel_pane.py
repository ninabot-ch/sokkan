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


def test_folders_named_by_a_hex_id_get_an_alias_inside_the_run(tmp_path):
    """The kernel scrubs hex runs of 16+ characters from what its model reads: a document's
    folder named by its id would become a placeholder. Inside the run's folder it gets an alias;
    outside, nothing is aliased (the kernel refuses it anyway)."""
    from harness import kernel
    work = tmp_path / "work"
    doc = work / "documents" / "0f3a9c1be27d4e58"
    doc.mkdir(parents=True)
    (doc / "Devis.txt").write_text("devis")
    out = tmp_path / "other" / "aa11bb22cc33dd44"
    out.mkdir(parents=True)
    h = kernel.KernelHarness()
    h._spec = contract.RunSpec(run_id="47a7c42ece550b13d2593459dd070f69", cwd=str(work))
    text = h._alias_paths(f"read: {doc}/Devis.txt and {out}/x.txt")
    alias = text.split("read: ", 1)[1].split("/Devis.txt")[0]
    assert "0f3a9c1be27d4e58" not in text and str(out) in text
    assert os.path.realpath(alias) == str(doc) and alias.startswith(str(work / ".sokkan-kernel"))
    assert open(os.path.join(alias, "Devis.txt")).read() == "devis"
    assert h._alias_paths(f"{doc}/Devis.txt") == f"{alias}/Devis.txt"     # stable in a run
    import re
    assert not re.search(r"[0-9a-f]{16,}", alias)
