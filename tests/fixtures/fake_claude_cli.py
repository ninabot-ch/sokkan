#!/usr/bin/env python3
"""A stand-in for the Claude Code CLI's stream-json mode (runner tests): answers the
initialize control request, echoes each user message as an assistant message + a result,
and on "ASK" first sends a can_use_tool control request and waits for the answer."""
import json
import sys
import time

n = 0


def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def turn(text, extra=""):
    out({"type": "assistant", "message": {"role": "assistant", "model": "fake",
         "content": [{"type": "text", "text": f"echo: {text}{extra}"}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1,
         "duration_api_ms": 1, "num_turns": 1, "session_id": "fake-session",
         "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}})


pending = {}
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") == "control_request" and msg["request"].get("subtype") == "initialize":
        n += 1
        out({"type": "control_response", "response": {
            "subtype": "success", "request_id": msg["request_id"],
            "response": {"commands": [], "inits": n}}})
    elif msg.get("type") == "control_response":
        rid = msg["response"]["request_id"]
        text = pending.pop(rid, "")
        beh = (msg["response"].get("response") or {}).get("behavior")
        turn(text, f" [{beh}]")
    elif msg.get("type") == "user":
        content = msg["message"]["content"]
        text = content if isinstance(content, str) else content[0].get("text", "")
        if text.startswith("SLOW"):
            time.sleep(1.0)
        if text.startswith("ASK"):
            rid = f"perm-{len(pending) + 1}"
            pending[rid] = text
            out({"type": "control_request", "request_id": rid, "request": {
                "subtype": "can_use_tool", "tool_name": "Bash",
                "input": {"command": "ls"}, "permission_suggestions": []}})
            continue
        if text == "EXIT":
            sys.exit(3)
        turn(text)
