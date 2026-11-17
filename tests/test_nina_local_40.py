"""4.0 (audit coworker) — Nina on the appliance's local model (OpenAI door, reasoning parser).

Seen on the 27B: 79 s, the whole answer budget spent thinking, then « … ask the admin to raise
SOKKAN_ASSISTANT_MAX_TOKENS » shown to a person. Measured after the fix on the real 27B: first
word in 2-5 s, whole answer in 4-6 s (cards-rapport.md)."""
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import assistant  # noqa: E402

CFG = {"url": "http://box:8004/v1", "token": "local", "api": "openai", "model": "qwen"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(assistant, "THINKING", False)
    monkeypatch.setattr(assistant, "_NO_TEMPLATE_KWARGS", set())


class _Resp:
    def __init__(self, payload, status=200, lines=None, ctype="application/json"):
        self._p, self.status_code, self._lines = payload, status, lines or []
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._p

    def read(self):
        return b""

    def close(self):
        pass

    def iter_lines(self):
        yield from self._lines


def test_the_reasoning_pass_is_off_by_default(monkeypatch):
    bodies = []

    def post(url, headers=None, json=None, timeout=None):
        bodies.append(json)
        return _Resp({"choices": [{"message": {"content": "Bonjour"}}]})

    monkeypatch.setattr(assistant.httpx, "post", post)
    assert assistant._ask(CFG, "S", [], "a@b.ch") == "Bonjour"
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    monkeypatch.setattr(assistant, "THINKING", True)              # SOKKAN_ASSISTANT_THINKING=1
    assistant._ask(CFG, "S", [], "a@b.ch")
    assert "chat_template_kwargs" not in bodies[1]


def test_an_endpoint_that_refuses_the_switch_is_asked_again_without_it(monkeypatch):
    bodies = []

    def post(url, headers=None, json=None, timeout=None):
        bodies.append(dict(json))
        if "chat_template_kwargs" in json:
            return _Resp({"error": "unknown field"}, status=400)
        return _Resp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(assistant.httpx, "post", post)
    assert assistant._ask(CFG, "S", [], "a@b.ch") == "ok"
    assert assistant._ask(CFG, "S", [], "a@b.ch") == "ok"
    # once refused, never sent again to that endpoint
    assert ["chat_template_kwargs" in b for b in bodies] == [True, False, False]


def test_a_cut_reasoning_is_never_the_answer(monkeypatch):
    monkeypatch.setattr(assistant.httpx, "post", lambda *a, **k: _Resp(
        {"choices": [{"finish_reason": "length",
                      "message": {"content": None, "reasoning_content": "The user asks…"}}]}))
    st: dict = {}
    assert assistant._ask(CFG, "S", [], "a@b.ch", st) == ""
    assert st["cut"] is True


def _sse(*deltas, finish=None):
    import json
    out = [f"data: {json.dumps({'choices': [{'delta': d}]})}" for d in deltas]
    if finish:
        out.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}")
    return out + ["data: [DONE]"]


def _streaming(monkeypatch, lines, seen=None):
    @contextmanager
    def stream(method, url, headers=None, json=None, timeout=None):
        if seen is not None:
            seen.append(json)
        yield _Resp(None, lines=lines, ctype="text/event-stream")

    monkeypatch.setattr(assistant.httpx, "stream", stream)


def test_the_stream_never_shows_the_models_thinking(monkeypatch):
    seen = []
    _streaming(monkeypatch, _sse({"reasoning_content": "The user is asking "},
                                 {"reasoning_content": "about memory."},
                                 {"content": "Cliquez sur "}, {"content": "« Mémoire »."}), seen)
    out = list(assistant._stream(CFG, "S", [], "a@b.ch", {}))
    assert "".join(out) == "Cliquez sur « Mémoire »."
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_gateway_that_answers_in_reasoning_content_still_answers(monkeypatch):
    _streaming(monkeypatch, _sse({"reasoning_content": "Réponse "}, {"reasoning_content": "entière"},
                                 finish="stop"))
    assert "".join(assistant._stream(CFG, "S", [], "a@b.ch", {})) == "Réponse entière"


def test_a_cut_thought_streams_nothing_and_the_person_reads_no_setting(monkeypatch):
    _streaming(monkeypatch, _sse({"reasoning_content": "Thinking…"}, finish="length"))
    st: dict = {}
    assert list(assistant._stream(CFG, "S", [], "a@b.ch", st)) == []
    assert st["cut"]
    for msg in ("Comment retenir que nous sommes suisses ?", "How do I attach a file?"):
        for cut in (True, False):
            said = assistant._empty_reply(msg, cut)
            assert "SOKKAN_" not in said and "MAX_TOKENS" not in said and "admin " not in said
    assert "personne qui administre SOKKAN" in assistant._empty_reply("Comment je fais valider un document par mon chef ?", True)


def test_the_atelier_guide_says_vous_and_names_the_memory_screen():
    g = assistant.SIMPLE_PROFILE_GUIDE
    assert "vouvoie" in g and "Mémoire" in g and "120 mots" in g
