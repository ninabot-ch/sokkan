"""4.0 — the kernel adapter on kernel host-prompt (beta-core d632b866): an Atelier work's
instructions (brief, job kit, organisation, continuity) open the kernel session as its
`system_prompt` behind the kernel's invariants only (`prefix: "host"`); the person's message
carries their words and the memory recall only; the system prompt never changes during the
thread (the model's prefix cache) and is given again, byte for byte, when the thread is
resumed. Without a kernel: httpx.MockTransport."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract, kernel  # noqa: E402

RECALL = ("=== Project memory (auto-recalled) ===\n- [charte-quiz] Nos quiz : 8 questions\n\n"
          "(Notes from this project's memory. A note given in full matches this task: follow it "
          "in the first draft, and name it to the person in one sentence.)")
FIRST = ("<sokkan-brief>You are working with: Alice. Do not ask for their own name.</sokkan-brief>\n"
         "<sokkan-brief>\nYou are helping a person who is NOT a developer.\n"
         "- The person attached these documents. READ each of them:\n  « Devis.txt »\n\n"
         + RECALL + "\n</sokkan-brief>\n\nPrépare un mail de relance.\n\n"
         "<sokkan-brief>Every sentence you write to the person is in French.</sokkan-brief>")
HEAD = (contract.ContextBlock(text="<sokkan-house>Atelier Lumen</sokkan-house>",
                              source="organisation"),)


class _Kernel:
    def __init__(self):
        self.sessions, self.chats = [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/events":
            return httpx.Response(200, stream=_Never(), headers={"content-type": "text/event-stream"})
        if p == "/v1/local/sessions" and req.method == "POST":
            self.sessions.append(json.loads(req.content))
            return httpx.Response(201, json={"thread_id": "th_0123456789abcdef"})
        if p.startswith("/v1/threads/") and req.method == "GET":
            return httpx.Response(200, json={"thread_id": p.rsplit("/", 1)[1]})
        if p.endswith("/mode"):
            m = json.loads(req.content)["mode"]
            return httpx.Response(200, json={"mode": m, "effective": m})
        if p == "/v1/chat/completions":
            self.chats.append(json.loads(req.content)["messages"][0]["content"])
            return httpx.Response(200, json={})
        if p == "/v1/local/setup":
            return httpx.Response(200, json={"models": {}})
        return httpx.Response(404, json={})


class _Never(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b""


@pytest.fixture
def k(monkeypatch, tmp_path):
    real = httpx.AsyncClient
    kern = _Kernel()

    def client(*a, **kwa):
        kwa["transport"] = httpx.MockTransport(kern.handler)
        return real(*a, **kwa)
    monkeypatch.setattr(kernel.httpx, "AsyncClient", client)
    monkeypatch.setenv("SOKKAN_KERNEL_TOKEN", "t")
    monkeypatch.setenv("SOKKAN_KERNEL_URL", "http://kernel.test")
    monkeypatch.setenv("SOKKAN_KERNEL_PROMPT_DIR", str(tmp_path / "prompts"))
    monkeypatch.delenv("SOKKAN_KERNEL_HOST_PROMPT", raising=False)
    monkeypatch.delenv("SOKKAN_KERNEL_SANDBOX", raising=False)
    return kern


def _play(spec, prompts):
    async def go():
        h = kernel.KernelHarness()
        stream = h.start(spec)
        await stream.__anext__()
        for p in prompts:
            await h.send(p)
            await h._chat
            h._turn, h._expect_done = None, 0      # the kernel's turn.done, not played here
        await h._close_client()
        return h
    return asyncio.run(go())


def test_the_brief_is_split_from_the_persons_words():
    system, message = kernel.split_first_message(FIRST, HEAD)
    assert system.startswith("<sokkan-house>Atelier Lumen</sokkan-house>")
    assert "You are helping a person who is NOT a developer." in system
    assert "You are working with: Alice." in system
    assert "Every sentence you write to the person is in French." in system
    assert kernel.READ_DOCS in system and "`read_file`" in system
    assert "Project memory" not in system and "charte-quiz" not in system
    assert message.endswith("Prépare un mail de relance.")
    assert "charte-quiz" in message and "You are helping" not in message
    assert kernel.split_first_message("Bonjour.") == ("", "Bonjour.")


def test_the_session_opens_with_the_host_prompt_and_the_message_is_the_persons(k, tmp_path):
    spec = contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False, head=HEAD,
                            first_message=FIRST)
    later = ("Ajoute la date.\n\n<sokkan-brief>Answer in French, in everyday words; update the "
             "saved document when a change is asked.</sokkan-brief>")
    h = _play(spec, [FIRST, later])
    body = k.sessions[0]
    assert body["prefix"] == "host" and body["system_prompt"] == h.host_prompt
    assert "You are helping" in body["system_prompt"]
    assert h.opened_with["system_prompt"]["chars"] == len(h.host_prompt)     # no text kept
    first, second = k.chats
    assert "You are helping" not in first and "Atelier Lumen" not in first
    assert first.endswith("Prépare un mail de relance.") and "charte-quiz" in first
    assert second == "Ajoute la date."
    assert len(k.sessions) == 1            # opened once: the prefix never changes in a thread


def test_a_resumed_thread_gets_the_same_prompt_byte_for_byte(k, tmp_path):
    spec = contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False, head=HEAD,
                            first_message=FIRST)
    h = _play(spec, [FIRST])
    again = contract.RunSpec(run_id="r2", cwd=str(tmp_path), shell=False, head=HEAD,
                             resume="th_0123456789abcdef")
    _play(again, ["Et la suite ?"])
    assert k.sessions[1]["resume"] == "th_0123456789abcdef"
    assert k.sessions[1]["system_prompt"] == h.host_prompt and k.sessions[1]["prefix"] == "host"
    assert k.chats[-1] == "Et la suite ?"
    stored = Path(tmp_path / "prompts" / "th_0123456789abcdef.txt")
    assert (stored.stat().st_mode & 0o777) == 0o600


def test_switched_off_or_without_a_brief_nothing_changes(k, tmp_path, monkeypatch):
    _play(contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False), ["Bonjour."])
    assert "system_prompt" not in k.sessions[0] and "prefix" not in k.sessions[0]
    monkeypatch.setenv("SOKKAN_KERNEL_HOST_PROMPT", "0")
    _play(contract.RunSpec(run_id="r2", cwd=str(tmp_path), shell=False, first_message=FIRST),
          [FIRST])
    assert "system_prompt" not in k.sessions[1]
    assert "You are helping" in k.chats[-1]


def test_the_prompt_names_the_folders_as_the_model_opens_them(k, tmp_path, monkeypatch):
    """Bench n°6: the brief moved to the system prompt kept the cockpit's host paths; in the
    docker sandbox every read and save was refused outside_root. As the message was, the
    prompt is rewritten: `/workspace`, hex-free aliases."""
    monkeypatch.setenv("SOKKAN_KERNEL_SANDBOX", "docker")
    work = tmp_path / "work"
    deliv = work / "deliverables" / "5a4023a7a9bd4606934a62c31b659cfc"
    deliv.mkdir(parents=True)
    first = (f"<sokkan-brief>\nSave it in the folder {deliv} — one file.\n</sokkan-brief>\n\n"
             "Fais le plan.")
    h = _play(contract.RunSpec(run_id="r1", cwd=str(work), shell=False, first_message=first),
              [first])
    sp = k.sessions[0]["system_prompt"]
    assert str(tmp_path) not in sp and "5a4023a7a9bd4606934a62c31b659cfc" not in sp
    assert "/workspace/.sokkan-kernel/" in sp
    assert k.chats[0] == "Fais le plan."
    assert h.host_prompt == sp
