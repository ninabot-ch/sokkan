"""A scripted stand-in for the Anthropic Messages API, for end-to-end hook tests.

Claude Code (the CLI bundled with claude-agent-sdk) is pointed at it with
``ANTHROPIC_BASE_URL``: no credit is spent, every request the CLI sends is recorded, so
a test can check what the model WOULD have seen (the injected recall, the sub-agent's
prompt). Behaviour:

* a user message containing ``SPAWN_SUBAGENT: <task>`` (and no tool result yet) gets a
  tool call to the sub-agent tool (``Agent``, or ``Task`` on older CLIs) with that task;
* everything else gets a short text answer.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SPAWN = "SPAWN_SUBAGENT:"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.append(_text_of(b.get("content")))
    return "\n".join(out)


def all_text(req: dict) -> str:
    """Everything a request shows the model: system + messages."""
    sysp = req.get("system")
    parts = [sysp if isinstance(sysp, str) else _text_of(sysp)]
    for m in req.get("messages", []):
        parts.append(_text_of(m.get("content")))
    return "\n".join(parts)


class MockAnthropic:
    def __init__(self, host: str = "127.0.0.1"):
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._n = 0
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                self._json(200, {"data": [], "has_more": False})

            def do_HEAD(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    req = json.loads(raw)
                except ValueError:
                    req = {}
                path = self.path.split("?")[0]
                if path.endswith("/count_tokens"):
                    return self._json(200, {"input_tokens": 10})
                if not path.endswith("/v1/messages"):
                    return self._json(200, {})
                with mock._lock:
                    mock.requests.append(req)
                    mock._n += 1
                    mid = mock._n
                block, stop = mock.answer(req, mid)
                if req.get("stream"):
                    self._stream(req, block, stop, mid)
                else:
                    self._json(200, {"id": f"msg_{mid}", "type": "message", "role": "assistant",
                                     "model": req.get("model", "mock"), "content": [block],
                                     "stop_reason": stop, "stop_sequence": None,
                                     "usage": {"input_tokens": 10, "output_tokens": 5}})

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, req, block, stop, mid):
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()

                def ev(name, data):
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

                ev("message_start", {"type": "message_start", "message": {
                    "id": f"msg_{mid}", "type": "message", "role": "assistant",
                    "model": req.get("model", "mock"), "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})
                if block["type"] == "text":
                    ev("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}})
                    ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta",
                                                         "text": block["text"]}})
                else:
                    ev("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {**block, "input": {}}})
                    ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "input_json_delta",
                                                         "partial_json": json.dumps(
                                                             block["input"])}})
                ev("content_block_stop", {"type": "content_block_stop", "index": 0})
                ev("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 5}})
                ev("message_stop", {"type": "message_stop"})
                self.wfile.flush()

        self.server = ThreadingHTTPServer((host, 0), H)
        self.url = f"http://{host}:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)

    def answer(self, req: dict, mid: int):
        msgs = req.get("messages") or []
        last = msgs[-1] if msgs else {}
        last_text = _text_of(last.get("content"))
        has_result = any(isinstance(b, dict) and b.get("type") == "tool_result"
                         for b in (last.get("content") or []) if not isinstance(
                             last.get("content"), str))
        tools = {t.get("name") for t in req.get("tools") or []}
        agent_tool = "Agent" if "Agent" in tools else ("Task" if "Task" in tools else None)
        if SPAWN in last_text and not has_result and agent_tool:
            task = last_text.split(SPAWN, 1)[1].strip().splitlines()[0]
            return ({"type": "tool_use", "id": f"toolu_{mid:04d}", "name": agent_tool,
                     "input": {"description": "look into it", "prompt": task,
                               "subagent_type": "general-purpose"}}, "tool_use")
        return {"type": "text", "text": "ok"}, "end_turn"

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # -- helpers for assertions
    def main_requests(self) -> list[dict]:
        """Requests of the conversation loop (they carry tools), not side calls."""
        return [r for r in self.requests if r.get("tools")]

    def subagent_requests(self, marker: str) -> list[dict]:
        """Requests whose FIRST user message contains ``marker`` (a sub-agent's prompt)."""
        out = []
        for r in self.main_requests():
            msgs = r.get("messages") or []
            if msgs and marker in _text_of(msgs[0].get("content")) and \
                    SPAWN not in _text_of(msgs[0].get("content")):
                out.append(r)
        return out
