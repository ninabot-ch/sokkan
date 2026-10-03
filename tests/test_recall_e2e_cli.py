"""End to end: the REAL Claude Code CLI (bundled with claude-agent-sdk) talks to a scripted
Messages API (tests/mock_anthropic.py) — no credit spent — and the test reads what the
model would have received: the recall block at turn 1 and turn 5, and the recall appended
to a sub-agent's prompt by the PreToolUse hook (updatedInput).

Two installs are checked, the two SOKKAN uses:
* chat sessions: in-process SDK hooks (backend/memrecall.sdk_hooks);
* terminal sessions: command hooks from a settings file (``claude --settings``), run by
  memory/recall_hook.py in a separate process (lexical-only here: no embedding server).

Opt-in (spawns the CLI, ~30 s): SOKKAN_TEST_E2E_CLI=1 and SOKKAN_TEST_PG_DSN.
"""
import asyncio
import os
import sys
import tempfile
import time
import uuid

import pytest

pytest.importorskip("claude_agent_sdk")
psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(
    not (DSN and os.environ.get("SOKKAN_TEST_E2E_CLI") == "1"),
    reason="SOKKAN_TEST_E2E_CLI=1 and SOKKAN_TEST_PG_DSN needed (spawns the CLI)")

sys.path.insert(0, os.path.dirname(__file__))
from mock_anthropic import MockAnthropic, all_text  # noqa: E402
from test_recall_pg import BowEmbedder  # noqa: E402

from core import bench_recall as B  # noqa: E402
from core.indexer import IndexConfig, IndexRunner  # noqa: E402
from core.recall import MARKER, SUBAGENT_MARKER  # noqa: E402
from core.store import Store  # noqa: E402

TURNS = [
    "Kartonage Weiss cardboard packaging boxes: who do we order from?",
    "Rename the variable tmp to buffer in utils.py please",
    "Run the whole test suite again and tell me what fails",
    "thanks, that looks good to me",
    "Clara personal mailbox IMAP delegation: do you have read access?",
    "SPAWN_SUBAGENT: Check the Intel Arc render node GPU with clinfo",
]


@pytest.fixture()
def db(tmp_path):
    name = "sokkan_e2e_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    store = Store(dsn)
    IndexRunner(lambda: store, BowEmbedder,
                IndexConfig(memory_dir=B.write_corpus(tmp_path / "mem"), write_index=False),
                log=lambda m: None).run_once()
    try:
        yield dsn, store
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _env(mock_url: str, dsn: str) -> dict:
    env = {**os.environ, "ANTHROPIC_BASE_URL": mock_url, "ANTHROPIC_API_KEY": "sk-test",
           "CLAUDE_CONFIG_DIR": tempfile.mkdtemp(), "DISABLE_TELEMETRY": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
           "CORTHEXIS_DATABASE_URL": dsn, "CORTHEXIS_MEMORY_BACKEND": "postgres",
           "CORTHEXIS_RECALL_THRESHOLD": "0.2",
           # command hooks run in their own process: no embedding server → lexical-only
           "CORTHEXIS_MEMORY_PROFILE": "leger", "CORTHEXIS_EMBED_URLS": "http://127.0.0.1:9",
           "PYTHONPATH": os.pathsep.join(sys.path)}
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


async def _allow(_name, inp, _ctx):
    from claude_agent_sdk import PermissionResultAllow

    return PermissionResultAllow(updated_input=inp)


async def _converse(options) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    async with ClaudeSDKClient(options=options) as c:
        for q in TURNS:
            await c.query(q)
            async for _m in c.receive_response():
                pass


def _new_segment(req: dict) -> str:
    """What the request adds since the last assistant message: the user turn + hooks."""
    msgs = req.get("messages") or []
    last_a = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
    return all_text({"messages": msgs[last_a + 1:]})


def _before(t5: str, store: Store, session_key: str) -> set:
    """Notes injected in the session's prompts, minus the ones of the turn-5 block."""
    return {r["note_name"] for r in store.recall_log(session_id=session_key)
            if r["channel"] == "prompt"} - ({"mailbox-access-clara"} if
                                            "mailbox-access-clara" in t5 else set())


def _check(mock: MockAnthropic, store: Store, session_key: str) -> dict:
    subs = mock.subagent_requests("render node")
    main = [r for r in mock.main_requests() if r not in subs]

    def turn(text: str) -> str:
        return next((seg for seg in map(_new_segment, main) if text in seg), "")

    t1, t5 = turn(TURNS[0]), turn(TURNS[4])
    sub_first = all_text({"messages": subs[0]["messages"][:1]}) if subs else ""
    fillers = [turn(t) for t in TURNS[1:4]]
    return {
        "turn1": MARKER in t1 and "packaging-supplier" in t1,
        # in the block at turn 5, or already injected earlier in the session (dedup)
        "turn5": MARKER in t5 and ("mailbox-access-clara" in t5 or "mailbox-access-clara" in
                                   _before(t5, store, session_key)),
        "subagent": SUBAGENT_MARKER in sub_first and "render-node-gpu" in sub_first,
        "filler_with_recall": sum(MARKER in f for f in fillers),
        "logged": {r["channel"] for r in store.recall_log(session_id=session_key)},
    }


def test_sdk_session_in_process_hooks(db, monkeypatch):
    import memrecall
    import store_backend as sb
    from claude_agent_sdk import ClaudeAgentOptions

    dsn, store = db
    with MockAnthropic() as mock:
        env = _env(mock.url, dsn)
        for k in ("CORTHEXIS_DATABASE_URL", "CORTHEXIS_MEMORY_BACKEND",
                  "CORTHEXIS_RECALL_THRESHOLD"):
            monkeypatch.setenv(k, env[k])
        monkeypatch.setattr(sb, "_store", store)
        monkeypatch.setattr(sb, "_qemb", (time.monotonic() + 3600, 1, BowEmbedder()))
        memrecall.reset()
        sid = "sokkan-" + uuid.uuid4().hex[:6]
        opts = ClaudeAgentOptions(cwd=tempfile.mkdtemp(), env=env, setting_sources=[],
                                  model="claude-sonnet-4-5", can_use_tool=_allow,
                                  hooks=memrecall.sdk_hooks(sid))
        asyncio.run(_converse(opts))
        res = _check(mock, store, sid)
    memrecall.reset()
    assert res["turn1"] and res["turn5"] and res["subagent"], res
    assert res["filler_with_recall"] == 0, res
    assert res["logged"] == {"prompt", "subagent"}


def test_terminal_session_command_hooks(db, tmp_path, monkeypatch):
    import memrecall
    from claude_agent_sdk import ClaudeAgentOptions

    dsn, store = db
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", dsn)
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "postgres")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    settings = memrecall.cli_settings_path()            # what `claude --settings` receives
    sid = str(uuid.uuid4())
    with MockAnthropic() as mock:
        opts = ClaudeAgentOptions(cwd=tempfile.mkdtemp(), env=_env(mock.url, dsn),
                                  setting_sources=[], settings=settings,
                                  model="claude-sonnet-4-5", can_use_tool=_allow,
                                  extra_args={"session-id": sid})
        asyncio.run(_converse(opts))
        res = _check(mock, store, sid)
    assert res["turn1"] and res["turn5"] and res["subagent"], res
    assert res["logged"] == {"prompt", "subagent"}
