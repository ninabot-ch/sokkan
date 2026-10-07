"""3.1.1 — the board MCP knows which SDK session calls it (open_preview, cards).

Regression: from an SDK session (a card spawned from the board, the cockpit chat,
an agent run) open_preview wrote preview-trigger.json with session_id="" and
tag="" — the board server only looked at TMUX_PANE (which it may even inherit
from the API process), never at SOKKAN_SESSION_ID that the API puts in the env
of every embedded MCP server."""
import os

import pytest


@pytest.fixture()
def bmcp(tmp_path, monkeypatch):
    import audit
    import board
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    import board_mcp
    return board_mcp


def test_sdk_session_is_identified_by_its_env(bmcp, monkeypatch):
    import board
    board.add_sdk_session("a" * 32, "frontend", title="Card #3")
    monkeypatch.setenv("SOKKAN_SESSION_ID", "a" * 32)
    monkeypatch.setenv("TMUX_PANE", "%0")  # inherited from the API: must not win
    ctx = bmcp._session_ctx()
    assert ctx["session_id"] == "a" * 32 and ctx["tag"] == "frontend"


def test_open_preview_carries_the_session(bmcp, monkeypatch):
    import board
    import previewenv
    board.add_sdk_session("b" * 32, "frontend", title="Card #4")
    monkeypatch.setenv("SOKKAN_SESSION_ID", "b" * 32)
    seen = {}

    def fake_trigger(env, path="/", session_id="", tag="", window="", user=""):
        seen.update(env=env, session_id=session_id, tag=tag, user=user)
        return {"env": env, "path": path, "session_id": session_id, "tag": tag}
    monkeypatch.setattr(previewenv, "trigger", fake_trigger)
    out = bmcp.open_preview("site", "/pricing")
    assert seen["session_id"] == "b" * 32 and seen["tag"] == "frontend"
    assert seen["user"] == "session:frontend" and out["session_id"] == "b" * 32


def test_card_created_from_an_sdk_session_is_attributed(bmcp, monkeypatch):
    import board
    board.add_sdk_session("c" * 32, "backend")
    monkeypatch.setenv("SOKKAN_SESSION_ID", "c" * 32)
    card = bmcp.create_card("Fix the flaky test", tag="backend")
    assert card["id"] and bmcp._actor(bmcp._session_ctx()) == "session:backend"


def test_every_embedded_server_gets_the_session_identity():
    import agentchat
    servers = agentchat.mcp_servers_for("d" * 32, "dev@x.ch")
    assert set(servers) >= {"sokkan-memory", "sokkan-board", "sokkan-observability",
                            "sokkan-agents"}
    for name, cfg in servers.items():
        assert cfg["env"]["SOKKAN_SESSION_ID"] == "d" * 32, name
        assert cfg["env"]["SOKKAN_SESSION_USER"] == "dev@x.ch", name


def test_terminal_session_still_resolves_through_tmux(bmcp, monkeypatch):
    monkeypatch.delenv("SOKKAN_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert bmcp._session_ctx() == {}
    assert os.environ.get("SOKKAN_SESSION_ID") is None
