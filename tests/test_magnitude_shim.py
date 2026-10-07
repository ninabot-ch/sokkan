"""Magnitude shim (Anthropic Messages ⇄ OpenAI chat/completions).

Translation unit tests (images, mid-conversation system messages, tool results) and
end-to-end tests against a fake llama-server: connect retry before the first byte,
never after; deferred stream close; `connection: close` on pre-body errors.
"""
import http.client
import importlib.util
import json
import socket
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# chargé par chemin : le nom `magnitude` est déjà pris par backend/magnitude.py (conftest
# met backend/ dans sys.path) — importer le paquet de l'agent l'écraserait dans sys.modules
_SHIM_PATH = Path(__file__).resolve().parent.parent / "magnitude" / "shim.py"
_spec = importlib.util.spec_from_file_location("magnitude_agent_shim", _SHIM_PATH)
S = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = S
_spec.loader.exec_module(S)

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}


def tr(req, vision=False):
    return S.anthropic_to_openai(req, "m", vision=vision)["messages"]


# ------------------------------------------------------------------ translation

def test_plain_conversation_unchanged():
    msgs = tr({"system": "S", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        {"role": "user", "content": [{"type": "text", "text": "a"},
                                     {"type": "text", "text": "b"}]}]})
    assert msgs == [{"role": "system", "content": "S"},
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "yo"},
                    {"role": "user", "content": "a\nb"}]


def test_image_without_vision_is_explicit_text():
    msgs = tr({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"}, IMG]}]})
    assert msgs[0]["content"] == "look\n" + S.IMAGE_OMITTED_NO_VISION
    assert "no vision support" in S.IMAGE_OMITTED_NO_VISION


def test_image_with_vision_becomes_image_url():
    msgs = tr({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"}, IMG,
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
        {"type": "image", "source": {"type": "file", "file_id": "f"}}]}]}, vision=True)
    parts = msgs[0]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1] == {"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{PNG}"}}
    assert parts[2]["image_url"]["url"] == "https://x/y.png"
    assert parts[3] == {"type": "text", "text": S.IMAGE_OMITTED_BAD_SOURCE}


def _read_png_turn():
    return {"messages": [
        {"role": "user", "content": "read shot.png"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "shot.png"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "PNG 1x1"}, IMG]},
            {"type": "text", "text": "what color?"}]}]}


def test_image_in_tool_result_with_vision():
    msgs = tr(_read_png_turn(), vision=True)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
    tool = msgs[2]
    assert tool["tool_call_id"] == "t1" and isinstance(tool["content"], str)
    assert tool["content"].startswith("PNG 1x1\n") and "next user message" in tool["content"]
    parts = msgs[3]["content"]
    assert parts[0]["type"] == "text" and "t1" in parts[0]["text"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2] == {"type": "text", "text": "what color?"}


def test_image_in_tool_result_without_vision():
    msgs = tr(_read_png_turn())
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
    assert msgs[2]["content"] == "PNG 1x1\n" + S.IMAGE_OMITTED_NO_VISION
    assert msgs[3]["content"] == "what color?"
    assert "image_url" not in json.dumps(msgs)


def test_tool_result_only_image_no_extra_text_turn():
    msgs = tr({"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                           "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": [IMG], "is_error": False}]}]}, vision=True)
    assert [m["role"] for m in msgs] == ["assistant", "tool", "user"]
    assert msgs[1]["content"].startswith("[1 image(s)")


def test_tool_result_string_and_error():
    msgs = tr({"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "boom", "is_error": True}]}]})
    assert msgs == [{"role": "tool", "tool_call_id": "t", "content": "[tool_error] boom"}]


def _roles_ok(msgs):
    """One system, first; no two consecutive user/assistant turns."""
    roles = [m["role"] for m in msgs]
    assert roles.count("system") <= 1
    assert "system" not in roles[1:]
    convo = [r for r in roles if r != "system"]
    for a, b in zip(convo, convo[1:]):
        assert not (a == b and a in ("user", "assistant")), roles


def test_leading_system_messages_merge_into_head():
    msgs = tr({"system": [{"type": "text", "text": "S0"}], "messages": [
        {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
        {"role": "user", "content": "a"}]})
    assert msgs[0] == {"role": "system", "content": "S0\n\n# Environment"}
    assert msgs[1] == {"role": "user", "content": "a"}


def test_mid_conversation_system_becomes_tagged_user_content():
    msgs = tr({"system": "S0", "messages": [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "system", "content": "# Environment\ncwd=/x"},
        {"role": "user", "content": "c"}]})
    _roles_ok(msgs)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "S0"
    last = msgs[3]["content"]
    assert last.startswith("<system-reminder>\n# Environment\ncwd=/x\n</system-reminder>")
    assert last.endswith("\nc")


def test_mid_system_between_tool_use_and_tool_result():
    msgs = tr({"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                           "input": {"command": "ls"}}]},
        {"role": "system", "content": "note"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ok"}]}]})
    _roles_ok(msgs)
    # le tool suit immédiatement l'assistant à tool_calls ; le system arrive après
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
    assert "<system-reminder>\nnote\n</system-reminder>" == msgs[3]["content"]


def test_mid_system_with_images_keeps_parts_list():
    msgs = tr({"messages": [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "system", "content": "env"},
        {"role": "user", "content": [IMG]}]}, vision=True)
    parts = msgs[2]["content"]
    assert parts[0]["type"] == "text" and "env" in parts[0]["text"]
    assert parts[1]["type"] == "image_url"


def test_trailing_system_is_appended():
    msgs = tr({"messages": [{"role": "user", "content": "a"},
                            {"role": "system", "content": "late"}]})
    assert msgs == [{"role": "user",
                     "content": "a\n\n<system-reminder>\nlate\n</system-reminder>"}]
    msgs = tr({"messages": [{"role": "user", "content": "a"},
                            {"role": "assistant", "content": "b"},
                            {"role": "system", "content": "late"}]})
    _roles_ok(msgs)
    assert msgs[-1] == {"role": "user", "content": "<system-reminder>\nlate\n</system-reminder>"}


def test_vision_default_from_env(monkeypatch):
    monkeypatch.delenv("MAGNITUDE_VISION", raising=False)
    assert S.Shim(token="t").vision is False
    monkeypatch.setenv("MAGNITUDE_VISION", "1")
    assert S.Shim(token="t").vision is True
    assert S.Shim(token="t", vision=False).vision is False


def test_connect_failure_classification():
    assert S._is_connect_failure(urllib.error.URLError(ConnectionRefusedError()))
    assert S._is_connect_failure(http.client.RemoteDisconnected("closed"))
    assert S._is_connect_failure(urllib.error.URLError(ConnectionResetError()))
    assert not S._is_connect_failure(urllib.error.URLError(socket.timeout("t")))
    assert not S._is_connect_failure(TimeoutError())
    assert not S._is_connect_failure(
        urllib.error.HTTPError("http://x", 500, "err", {}, None))


# ------------------------------------------------------------- fake llama-server

class FakeUpstream:
    """llama-server de test : chaque requête consomme le comportement suivant."""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.requests: list[dict] = []
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                up.requests.append(json.loads(body))
                beh = up.behaviors.pop(0) if up.behaviors else "json"
                getattr(up, "_" + beh)(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- comportements
    @staticmethod
    def _drop(h):  # ferme sans un octet de réponse → RemoteDisconnected
        h.close_connection = True

    @staticmethod
    def _http500(h):
        body = b'{"error":"boom"}'
        h.send_response(500)
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    @staticmethod
    def _json(h):
        body = json.dumps({"id": "x", "choices": [{"message": {"content": "hello"},
                                                    "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    @staticmethod
    def _sse_head(h):
        h.send_response(200)
        h.send_header("content-type", "text/event-stream")
        h.send_header("connection", "close")
        h.end_headers()
        h.close_connection = True

    @staticmethod
    def _chunk(h, obj):
        h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        h.wfile.flush()

    def _sse_usage_after_finish(self, h):
        self._sse_head(h)
        self._chunk(h, {"choices": [{"delta": {"content": "hel"}}]})
        self._chunk(h, {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]})
        self._chunk(h, {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 42}})
        h.wfile.write(b"data: [DONE]\n\n")

    def _sse_dies(self, h):  # premier octet parti, puis l'amont meurt
        self._sse_head(h)
        self._chunk(h, {"choices": [{"delta": {"content": "partial"}}]})
        h.wfile.flush()
        h.connection.shutdown(socket.SHUT_RDWR)


@pytest.fixture
def stack():
    started = []

    def make(behaviors, upstream_url=None):
        up = FakeUpstream(behaviors)
        sh = S.Shim(upstream=upstream_url or up.url, port=0, token="tok", model_id="m",
                    vision=False)
        sh.start()
        started.append((up, sh))
        return up, sh._httpd.server_address[1]

    yield make
    for up, sh in started:
        sh.stop()
        up.close()


def post(port, body, token="tok"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/v1/messages", body=json.dumps(body),
              headers={"content-type": "application/json", "x-api-key": token})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def sse_events(data: bytes):
    out = []
    for block in data.decode().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) == 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


REQ = {"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}


def test_retry_once_on_connect_failure_before_first_byte(stack):
    up, port = stack(["drop", "json"])
    r, data = post(port, REQ)
    assert r.status == 200
    assert json.loads(data)["content"] == [{"type": "text", "text": "hello"}]
    assert len(up.requests) == 2


def test_retry_once_on_stream_request(stack):
    up, port = stack(["drop", "sse_usage_after_finish"])
    r, data = post(port, {**REQ, "stream": True})
    assert r.status == 200 and len(up.requests) == 2
    assert sse_events(data)[-1][0] == "message_stop"


def test_only_one_retry_then_502_with_connection_close(stack):
    up, port = stack(["drop", "drop", "json"])
    r, data = post(port, REQ)
    assert r.status == 502
    assert r.getheader("connection") == "close"
    assert json.loads(data)["error"]["type"] == "api_error"
    assert len(up.requests) == 2


def test_connection_refused_gives_502(stack):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    s.close()  # port libre, personne n'écoute
    _, port = stack([], upstream_url=dead)
    r, _ = post(port, REQ)
    assert r.status == 502 and r.getheader("connection") == "close"


def test_http_error_not_retried(stack):
    up, port = stack(["http500", "json"])
    r, data = post(port, REQ)
    assert r.status == 500 and "upstream error 500" in json.loads(data)["error"]["message"]
    assert r.getheader("connection") == "close"
    assert len(up.requests) == 1


def test_no_retry_after_first_streamed_byte(stack):
    up, port = stack(["sse_dies", "sse_usage_after_finish"])
    r, data = post(port, {**REQ, "stream": True})
    ev = sse_events(data)
    names = [n for n, _ in ev]
    assert len(up.requests) == 1
    assert names[0] == "message_start"
    assert any(p.get("delta", {}).get("text") == "partial" for _, p in ev)
    assert names[-2:] == ["message_delta", "message_stop"]  # séquence close proprement


def test_deferred_stream_close_uses_late_usage(stack):
    _, port = stack(["sse_usage_after_finish"])
    r, data = post(port, {**REQ, "stream": True})
    ev = sse_events(data)
    delta = [p for n, p in ev if n == "message_delta"]
    assert len(delta) == 1 and delta[0]["usage"]["output_tokens"] == 42
    assert delta[0]["delta"]["stop_reason"] == "end_turn"
    assert "".join(p["delta"]["text"] for n, p in ev if n == "content_block_delta") == "hello"


def test_bad_token_401_closes_connection(stack):
    up, port = stack([])
    r, _ = post(port, REQ, token="nope")
    assert r.status == 401 and r.getheader("connection") == "close"
    assert up.requests == []


def test_mid_system_reaches_upstream_normalized(stack):
    up, port = stack(["json"])
    post(port, {**REQ, "system": "S0", "messages": [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
        {"role": "system", "content": "env"}, {"role": "user", "content": [
            {"type": "text", "text": "c"}, IMG]}]})
    msgs = up.requests[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "<system-reminder>\nenv\n</system-reminder>" in msgs[3]["content"]
    assert S.IMAGE_OMITTED_NO_VISION in msgs[3]["content"]
