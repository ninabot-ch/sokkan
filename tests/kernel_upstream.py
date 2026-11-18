"""A scripted model behind an OpenAI-compatible endpoint, for driving the kernel engine in
the conformance scenarios: the kernel is configured with this endpoint as its model, and the
scenario's steps come back as the kernel's own tool calls (``terminal``, ``write_file``,
``clarify``). A request whose last message is the person's starts the next scripted turn; a
request whose last message is a tool result — or the kernel's report of a decided card, which
it sends as a user-role block — plays the next step.

    with ScriptedUpstream(port=18004) as up:
        up.turns.append([driver.shell("rm victim.txt")])
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, list):
        return "".join(str(b.get("text") or "") for b in c if isinstance(b, dict))
    return str(c or "")


def _waiting(text: str) -> bool:
    """The kernel's « waiting for approval » placeholder — not a decided card's result: since
    kernel host-3e a card decided within the turn (`approval_wait_s`) comes back in the SAME
    turn as its outcome, refused or done, which also names `[approval:…]`."""
    up = text.upper()
    return "[approval:" in text and ("EN ATTENTE D'APPROBATION" in up or "AWAITING APPROVAL" in up
                                      or "WAITING FOR APPROVAL" in up)


class ScriptedUpstream:
    def __init__(self, port: int = 0, workspace: str | None = None) -> None:
        self.turns: deque = deque()
        self.requests: list[dict] = []
        self.workspace = workspace
        self._current: deque = deque()
        self._lock = threading.Lock()
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, obj) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self._send(200, {"object": "list",
                                            "data": [{"id": "scripted", "object": "model"}]})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not self.path.endswith("/chat/completions"):
                    return self._send(404, {})
                msg, finish = up._reply(body)
                prompt = sum(len(json.dumps(m)) for m in body.get("messages") or []) // 4
                usage = {"prompt_tokens": max(1, prompt),
                         "completion_tokens": max(1, len(msg.get("content") or "") // 4 + 1)}
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                cid = "chatcmpl-" + uuid.uuid4().hex[:10]
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    delta = {"role": "assistant"}
                    if msg.get("content"):
                        delta["content"] = msg["content"]
                    if msg.get("tool_calls"):
                        delta["tool_calls"] = [dict(tc, index=i)
                                               for i, tc in enumerate(msg["tool_calls"])]
                    for chunk in (
                            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                            {"choices": [], "usage": usage}):
                        chunk.update(id=cid, object="chat.completion.chunk",
                                     model=body.get("model"))
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._send(200, {"id": cid, "object": "chat.completion", "model": body.get("model"),
                                 "choices": [{"index": 0, "message": msg,
                                              "finish_reason": finish}], "usage": usage})

        # a kernel in a container on a bridge network reaches the host on the network's
        # gateway, not on 127.0.0.1 (tests/parity/lab)
        host = os.environ.get("SOKKAN_KERNEL_TEST_UPSTREAM_HOST") or "127.0.0.1"
        self._httpd = ThreadingHTTPServer((host, port), H)
        self.port = self._httpd.server_address[1]
        self.url = f"http://{host}:{self.port}/v1"

    # ---- what the model does -------------------------------------------------------------
    def _tool(self, step) -> tuple[str, dict]:
        if step.kind == "shell":
            return "terminal", {"command": step.text}
        if step.kind == "write":
            path = step.path
            if self.workspace and not os.path.isabs(path):
                path = os.path.join(self.workspace, path)
            return "write_file", {"path": path, "content": step.content}
        if step.kind == "ask":
            return "clarify", {"question": step.text, "options": list(step.options)}
        raise AssertionError(step)

    def _reply(self, body: dict) -> tuple[dict, str]:
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        with self._lock:
            self.requests.append(body)
            # the kernel's continuation after a card is a user-role block « [Approbation] … »:
            # the same scripted turn goes on
            if last.get("role") == "user" and "[Approbation]" not in _text(last):
                self._current = deque(self.turns.popleft()) if self.turns else deque()
            if last.get("role") == "tool" and _waiting(_text(last)):
                # the kernel's « waiting for approval » placeholder: a model waits quietly; the
                # scripted turn goes on in the continuation, with the call's real outcome
                return {"role": "assistant", "content": ""}, "stop"
            said = None
            while self._current:
                step = self._current.popleft()
                if step.kind == "say":
                    said = step.text
                    continue
                name, args = self._tool(step)
                return {"role": "assistant", "content": said or "",
                        "tool_calls": [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                        "function": {"name": name,
                                                     "arguments": json.dumps(args)}}]}, \
                    "tool_calls"
            return {"role": "assistant", "content": said or "Done."}, "stop"

    # ---- what the model was shown --------------------------------------------------------
    def shown(self, body: dict) -> str:
        return "\n".join(_text(m) for m in body.get("messages") or [])

    def saw(self, text: str) -> bool:
        with self._lock:
            return any(text in self.shown(b) for b in self.requests)

    def last_saw(self, text: str) -> bool:
        with self._lock:
            return bool(self.requests) and text in self.shown(self.requests[-1])

    def __enter__(self) -> "ScriptedUpstream":
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
