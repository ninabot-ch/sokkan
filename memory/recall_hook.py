#!/usr/bin/env python3
"""Command hook installed by SOKKAN in terminal sessions (``claude --settings``).

``UserPromptSubmit`` → notes related to the message; ``PreToolUse`` on ``Task|Agent`` →
recall appended to the sub-agent's prompt. Fast path: the payload is posted to the warm
backend (``CORTHEXIS_RECALL_API_URL`` + the token in ``CORTHEXIS_RECALL_TOKEN_FILE``),
stdlib only. When the API does not answer, the recall runs in this process
(``core.recall.main``: ~1 s more, psycopg and numpy imports). Never fails: any error =
no output, exit 0. Debug: ``CORTHEXIS_RECALL_DEBUG=1``::

    echo '{"hook_event_name":"UserPromptSubmit","session_id":"t","prompt":"…"}' \\
      | CORTHEXIS_RECALL_DEBUG=1 python memory/recall_hook.py
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _debug(msg: str) -> None:
    if os.environ.get("CORTHEXIS_RECALL_DEBUG") == "1":
        print(f"[recall] {msg}", file=sys.stderr)


def via_api(raw: bytes) -> bool:
    """True when the backend answered (its answer, possibly empty, is printed)."""
    url = os.environ.get("CORTHEXIS_RECALL_API_URL")
    tok_file = os.environ.get("CORTHEXIS_RECALL_TOKEN_FILE")
    if not url or not tok_file:
        return False
    try:
        token = Path(tok_file).read_text(encoding="utf-8").strip()
        req = urllib.request.Request(
            url.rstrip("/") + "/api/memory/hook", data=raw, method="POST",
            headers={"content-type": "application/json", "x-sokkan-hook-token": token})
        with urllib.request.urlopen(req, timeout=float(
                os.environ.get("CORTHEXIS_RECALL_API_TIMEOUT", "4"))) as r:
            out = r.read()
    except urllib.error.HTTPError as e:
        _debug(f"api answered {e.code}")
        return e.code not in (401, 403, 404, 502, 503)   # other errors: give up quietly
    except (OSError, ValueError) as e:
        _debug(f"api unreachable: {e!r}")
        return isinstance(e, TimeoutError)                # a timeout: no second try
    if out.strip() not in (b"", b"{}"):
        sys.stdout.write(out.decode("utf-8"))
    return True


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        json.loads(raw or b"{}")
    except ValueError:
        return 0
    if via_api(raw):
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from core.recall import main as recall_main
    except Exception:  # noqa: BLE001 — missing dependency: no recall, never a broken turn
        return 0
    return recall_main(io.StringIO(raw.decode("utf-8", "replace")))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        _debug(f"error: {e!r}")
        sys.exit(0)
