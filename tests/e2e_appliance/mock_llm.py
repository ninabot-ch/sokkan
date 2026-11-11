#!/usr/bin/env python3
"""A stand-in for an inference endpoint (Anthropic Messages API + OpenAI chat), stdlib only,
for tests/e2e_appliance: the local model (« ship ») and the Deep gateway (« deep »).

    python3 mock_llm.py <name> <port>

Every answer names the endpoint (`[ship] ok` / `[deep] ok`) so a transcript shows who served.
Control (the test harness only — never exposed outside the test machine):
    POST /_mode {"status": 402}     answer that status to /v1/messages until further notice
    POST /_mode {"status": 200}     back to normal
    GET  /_seen                     {"count": n, "models": […], "last_user": "…", "auth": "…", "posts": {…}}
    GET  /_grep?needle=…            {"found": bool} — did any request show that text to the model
    POST /hb                        a supervision heartbeat: body and bearer kept in /_seen
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = sys.argv[1] if len(sys.argv) > 1 else "ship"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8004
STATE = {"status": 200, "count": 0, "models": [], "last_user": "", "auth": "", "concurrent": 0, "max_concurrent": 0,
         "posts": {}, "hb_auth": "", "hb_body": None}
SEEN_TEXT = [""]       # everything the model was shown since the start (system + messages), for /_grep
LOCK = threading.Lock()
BODY = {402: {"type": "error", "error": {"type": "billing_error", "message": "wallet empty (mock)"}},
        429: {"type": "error", "error": {"type": "rate_limit_error", "message": "daily quota reached (mock)"}},
        401: {"type": "error", "error": {"type": "authentication_error", "message": "bad token (mock)"}}}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        p = self.path.split("?")[0]
        if p == "/_seen":
            return self._json(200, STATE)
        if p == "/_grep":
            from urllib.parse import parse_qs, urlparse
            needle = (parse_qs(urlparse(self.path).query).get("needle") or [""])[0]
            return self._json(200, {"found": bool(needle) and needle in SEEN_TEXT[0]})
        if p == "/health":
            return self._json(200, {"ok": True, "name": NAME})
        if p == "/tiers":
            return self._json(200, {"tiers": [{"id": "sokkan-deep"}]})
        return self._json(200, {"data": [{"id": "mock"}], "has_more": False})

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        try:
            req = json.loads(self.rfile.read(n) if n else b"{}")
        except ValueError:
            req = {}
        p = self.path.split("?")[0]
        with LOCK:
            STATE["posts"][p] = STATE["posts"].get(p, 0) + 1
        if p == "/hb":
            STATE["hb_auth"] = self.headers.get("authorization") or ""
            STATE["hb_body"] = req
            return self._json(200, {"ok": True})
        if p == "/_mode":
            STATE["status"] = int(req.get("status", 200))
            return self._json(200, STATE)
        if p == "/_reset":
            STATE.update(count=0, models=[], last_user="", max_concurrent=0)
            return self._json(200, STATE)
        if p.endswith("/count_tokens"):
            return self._json(200, {"input_tokens": 10})
        if p.endswith("/chat/completions"):       # OpenAI door (Nina)
            return self._json(200, {"id": "c1", "object": "chat.completion", "model": req.get("model"),
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": f"[{NAME}] ok"}}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
        if not p.endswith("/v1/messages"):
            return self._json(200, {})
        user = ""
        for m in reversed(req.get("messages") or []):
            if m.get("role") == "user" and _text(m.get("content")).strip():
                user = _text(m.get("content"))
                break
        sysp = req.get("system")
        shown = (sysp if isinstance(sysp, str) else _text(sysp)) + "\n" + "\n".join(
            _text(m.get("content")) for m in req.get("messages") or [])
        with LOCK:
            SEEN_TEXT[0] = (SEEN_TEXT[0] + "\n" + shown)[-2_000_000:]
            STATE["count"] += 1
            STATE["models"] = (STATE["models"] + [req.get("model")])[-20:]
            STATE["last_user"] = user[-200:]
            STATE["auth"] = (self.headers.get("x-api-key") or "")[:12]
            STATE["concurrent"] += 1
            STATE["max_concurrent"] = max(STATE["max_concurrent"], STATE["concurrent"])
        try:
            if STATE["status"] != 200:
                return self._json(STATE["status"], BODY.get(STATE["status"], {"error": {"message": "mock failure"}}))
            text = f"[{NAME}] ok"
            if not req.get("stream"):
                return self._json(200, {"id": "msg_1", "type": "message", "role": "assistant",
                                        "model": req.get("model", NAME), "content": [{"type": "text", "text": text}],
                                        "stop_reason": "end_turn", "stop_sequence": None,
                                        "usage": {"input_tokens": 10, "output_tokens": 5}})
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()

            def ev(name, data):
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()

            ev("message_start", {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": req.get("model", NAME),
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1}}})
            ev("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})
            import time
            time.sleep(float(req.get("metadata", {}).get("mock_delay_s", 0.3)) if isinstance(req.get("metadata"), dict) else 0.3)
            ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": text}})
            ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                 "usage": {"output_tokens": 5}})
            ev("message_stop", {"type": "message_stop"})
            self.close_connection = True
        finally:
            with LOCK:
                STATE["concurrent"] -= 1


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
