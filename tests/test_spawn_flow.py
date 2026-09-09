"""Integration of the critical flow: spawn → memory recall pre-seed → tool approval.

No live SDK/model: the seed is asserted as text, the approval path is exercised
directly on AgentSession (futures + resolve_permission), the SDK client untouched.
"""
import asyncio

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

import agentchat
import board


# ---- spawn pre-seeds the memory ritual --------------------------------------

def test_seed_text_carries_task_and_memory_ritual():
    seed = board.seed_text("Add DELETE /notes/{id} to the API")
    assert seed.startswith("Add DELETE /notes/{id} to the API")
    # the RAG recall must be the FIRST thing a session does…
    assert "mcp__sokkan-memory__memory_search" in seed
    assert "memory_get" in seed
    # …and nothing executes without the human go
    assert "go-ahead" in seed


def test_card_description_becomes_the_spawn_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    card = board.add_card("Add stats", description="Add GET /stats with tests", tag="backend")
    seed = board.seed_text(card["description"])
    assert seed.startswith("Add GET /stats with tests")


# ---- approval gate on mutating tools ----------------------------------------

def _session() -> agentchat.AgentSession:
    return agentchat.AgentSession("test-sid", cwd="/tmp")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _ask_and_resolve(sess, decision: dict, tool="Bash", inp=None):
    task = asyncio.ensure_future(sess._can_use_tool(tool, inp or {"command": "rm -rf x"}, None))
    for _ in range(100):  # wait for the permission future to be armed
        await asyncio.sleep(0)
        if sess._perms:
            break
    assert sess._perms, "no permission was requested for a mutating tool"
    pid = next(iter(sess._perms))
    # the pending permission was emitted to the UI event stream
    assert any(e["type"] == "permission" and e["id"] == pid for e in sess.events)
    sess.resolve_permission(pid, decision)
    return await task


def test_mutating_tool_waits_for_allow():
    sess = _session()
    res = _run(_ask_and_resolve(sess, {"decision": "allow"}))
    assert isinstance(res, PermissionResultAllow)
    assert res.updated_input == {"command": "rm -rf x"}
    assert not sess._perms  # future cleaned up


def test_mutating_tool_deny_carries_message():
    sess = _session()
    res = _run(_ask_and_resolve(sess, {"decision": "deny", "message": "not on prod"}))
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "not on prod"


def test_accept_edits_auto_allows_edits_but_not_bash():
    sess = _session()

    async def flow():
        sess.mode = "acceptEdits"
        allowed = await sess._can_use_tool("Edit", {"file_path": "/x"}, None)
        assert isinstance(allowed, PermissionResultAllow)
        # Bash still goes through the human gate
        return await _ask_and_resolve(sess, {"decision": "allow"})

    assert isinstance(_run(flow()), PermissionResultAllow)


def test_bypass_mode_auto_allows():
    sess = _session()

    async def flow():
        sess.mode = "bypassPermissions"
        return await sess._can_use_tool("Bash", {"command": "ls"}, None)

    assert isinstance(_run(flow()), PermissionResultAllow)


def test_resolving_unknown_permission_is_noop():
    sess = _session()
    sess.resolve_permission("nope", {"decision": "allow"})  # must not raise
    assert not sess.events or all(e.get("id") != "nope" or e["type"] != "permission_resolved"
                                  for e in sess.events)


def test_seed_text_with_recall_preinjects_memory():
    """Le rappel mémoire pré-injecté (déterministe) remplace le rituel « va
    chercher » : les notes sont DANS le premier message, memory_get reste
    disponible pour approfondir."""
    import board

    recall = "=== Project memory (auto-recalled) ===\n- [ports-note] the API listens on 5055"
    seed = board.seed_text("Fix the healthcheck", recall)
    assert "the API listens on 5055" in seed
    assert "mcp__sokkan-memory__memory_get" in seed
    assert "go-ahead" in seed
    # sans recall → rituel historique inchangé
    legacy = board.seed_text("Fix the healthcheck")
    assert "Start by calling the mcp__sokkan-memory__memory_search" in legacy


def test_playbooks_render_and_catalog():
    import playbooks

    cat = playbooks.catalog()
    ids = {p["id"] for p in cat}
    assert {"onboard-memory", "refactor", "debug", "digest"} <= ids
    prompt, tag = playbooks.render("refactor", "split billing.py")
    assert "split billing.py" in prompt and tag == "backend"
    assert "behavior must stay identical" in prompt
    prompt2, _ = playbooks.render("onboard-memory")
    assert "ONE durable fact per file" in prompt2
    assert playbooks.render("nope") is None
