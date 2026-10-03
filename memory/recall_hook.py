#!/usr/bin/env python3
"""Command hook installed by SOKKAN in terminal sessions (``claude --settings``).

Runs ``core.recall.main``: ``UserPromptSubmit`` → notes related to the message,
``PreToolUse`` on ``Task|Agent`` → recall appended to the sub-agent's prompt.
Never fails: any error = no output, exit 0. Debug: ``CORTHEXIS_RECALL_DEBUG=1``::

    echo '{"hook_event_name":"UserPromptSubmit","session_id":"t","prompt":"…"}' \\
      | CORTHEXIS_RECALL_DEBUG=1 python memory/recall_hook.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from core.recall import main
except Exception:  # noqa: BLE001 — missing dependency: no recall, never a broken turn
    sys.exit(0)

sys.exit(main())
