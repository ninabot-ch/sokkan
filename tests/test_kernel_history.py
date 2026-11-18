"""4.0 — the kernel engine's history() (backend/harness/kernel.py): the kernel's thread
export read back as contract items. Without a kernel: the export is a recorded one (the
shape of nokker-thread-export/1, a gated write then its continuation)."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import kernel  # noqa: E402

CWD = "/data/projects/p/work"
EXPORT = {
    "format": "nokker-thread-export/1",
    "messages": [
        {"index": 0, "role": "user", "channel": "api",
         "content": '<memory-recall source="project memory" trigger="this message">\n- a note\n'
                    "</memory-recall>\n\nWrite the minutes, then say so."},
        {"index": 0, "role": "assistant", "tool_calls": [
            {"id": "call_1", "name": "write_file",
             "arguments": '{"path": "%s/minutes.md", "content": "# Minutes\\n"}' % CWD}]},
        {"index": 0, "role": "tool", "tool_call_id": "call_1", "name": "write_file", "sealed": False,
         "content": "EN ATTENTE D'APPROBATION HUMAINE (…) [approval:3138e432-4979-42c7-ba2d-2452114224fe]. "
                    "L'action n'a pas été exécutée."},
        {"index": 0, "role": "assistant", "content": ""},
        {"index": 1, "role": "user", "channel": "api",
         "content": "[Approbation]\nOutil : write_file\nDécision : executed\nDécideur : local-owner\n"
                    "Moyen : local-token\nRésultat : {\"bytes_written\": 10}\nCette action est tranchée"
                    " … Continue la tâche à partir de ce résultat.\n[/Approbation]"},
        {"index": 1, "role": "assistant", "content": "The minutes are saved."},
        {"index": 2, "role": "user", "channel": "api", "content": "And delete the draft."},
        {"index": 2, "role": "assistant", "tool_calls": [
            {"id": "call_2", "name": "terminal", "arguments": '{"command": "rm draft.md"}'}]},
        {"index": 2, "role": "tool", "tool_call_id": "call_2", "name": "terminal", "sealed": False,
         "content": "EN ATTENTE D'APPROBATION HUMAINE [approval:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]"},
        {"index": 2, "role": "assistant", "content": ""},
        {"index": 3, "role": "user", "channel": "api",
         "content": "[Approbation]\nOutil : terminal\nCommande : rm draft.md\nDécision : rejected\n"
                    "Décideur : local-owner\nMoyen : local-token\nRésultat : refusée par l'humain\n"
                    "Cette action est tranchée.\n[/Approbation]"},
        {"index": 3, "role": "assistant", "content": "I left the draft."},
    ],
    "page": {"before": None, "limit": 500, "next_before": None, "truncated": False},
}


@pytest.fixture
def kernel_api(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/threads/th_abc123/export":
            return httpx.Response(200, json=EXPORT)
        return httpx.Response(404, json={"error": {"message": "unknown"}})
    real = httpx.Client
    monkeypatch.setattr(kernel.httpx, "Client",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setenv("SOKKAN_KERNEL_URL", "http://kernel.test")
    monkeypatch.setenv("SOKKAN_KERNEL_TOKEN", "t")
    monkeypatch.delenv("SOKKAN_KERNEL_SANDBOX", raising=False)
    return seen


def test_the_kernel_declares_and_reads_a_thread_back(kernel_api):
    h = kernel.KernelHarness()
    assert h.capabilities().history is True
    items = h.history("th_abc123", cwd=CWD)
    assert [(i.kind, i.text) for i in items if i.kind != "tool"] == [
        ("prompt", "Write the minutes, then say so."),     # no recall preamble, no [Approbation]
        ("text", "The minutes are saved."),
        ("prompt", "And delete the draft."),
        ("text", "I left the draft.")]
    tools = [i for i in items if i.kind == "tool"]
    assert [(t.name, t.classification, t.ok) for t in tools] == [
        ("write_file", "file_write", True), ("terminal", "shell", False)]
    assert tools[0].output == '{"bytes_written": 10}' and tools[0].input["path"] == CWD + "/minutes.md"
    assert tools[1].output == "refusée par l'humain"
    assert kernel_api[0].headers["authorization"] == "Bearer t"
    assert kernel_api[0].url.params["include_tools"] == "1"


def test_an_unknown_or_foreign_reference_is_an_empty_thread(kernel_api):
    h = kernel.KernelHarness()
    assert h.history("th_unknown", cwd=CWD) == []
    assert h.history("0f0f0f0f-dead-4bee-8f00-0123456789ab", cwd=CWD) == []   # not a kernel id
    assert h.history("th_../../etc", cwd=CWD) == []
    assert len(kernel_api) == 1                     # only the well-formed id was asked


def test_the_sandbox_folder_is_named_as_the_cockpit_names_it(kernel_api, monkeypatch):
    monkeypatch.setenv("SOKKAN_KERNEL_SANDBOX", "docker")
    EXPORT_SB = {**EXPORT, "messages": [
        {"index": 0, "role": "user", "content": "Read /workspace/notes.md"},
        {"index": 0, "role": "assistant", "content": "Done in /workspace."}]}
    assert [i.text for i in kernel._history_items(EXPORT_SB["messages"], CWD)] == [
        "Read " + CWD + "/notes.md", "Done in " + CWD + "."]
