"""3.6.7 — the CLI of an idle session is closed after SOKKAN_SESSION_IDLE_CLOSE_S and started
again, on the same conversation, at its next turn (backend/agentchat.py: park / park_idle).
Each open session held 0.3-0.4 GB until the api restarted (5.8 GB for 16 on the appliance
bench). Never closed: a session that is working, or waits for a person's approval or answer."""
import asyncio

import pytest

import agentchat


class FakeCLI:
    started: list = []

    def __init__(self, options=None, transport=None):
        self.options, self.closed = options, False
        FakeCLI.started.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True

    async def query(self, text):
        self.asked = text

    async def receive_response(self):
        if False:
            yield None


@pytest.fixture()
def sess(monkeypatch):
    FakeCLI.started = []
    monkeypatch.setattr(agentchat, "ClaudeSDKClient", FakeCLI)
    monkeypatch.setattr(agentchat.study, "gate", lambda user: None)
    monkeypatch.setattr(agentchat.study, "turn_start", lambda *a, **k: None)
    monkeypatch.setattr(agentchat.study, "turn_end", lambda *a, **k: None)
    monkeypatch.setattr(agentchat.budgets, "check", lambda project: ("ok", ""))
    monkeypatch.setattr(agentchat.board, "set_claude_session_id", lambda *a: None)
    s = agentchat.AgentSession("idle-sid", cwd="/tmp", user="ana@pilot.example")
    agentchat._registry["idle-sid"] = s
    yield s
    agentchat._registry.pop("idle-sid", None)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_an_idle_session_is_closed_then_resumed_on_the_same_conversation(sess):
    async def go():
        await sess.handle_user("bonjour")
        first = sess.client
        sess.claude_session_id = "csid-42"          # what the CLI's init message said
        sess.events.append({"type": "text", "text": "Voici le plan."})
        n_events = len(sess.events)
        sess.last_active -= 31 * 60
        assert await agentchat.park_idle(1800) == ["idle-sid"]
        assert first.closed and sess.client is None and sess.parked == 1
        assert len(sess.events) == n_events            # the thread is replayed as before
        assert agentchat.peek("idle-sid") is sess      # still known: nothing to reopen by hand
        await sess.handle_user("et la suite ?")        # the next turn: started again, resumed
        second = sess.client
        assert second is not first and not second.closed
        assert second.options.resume == "csid-42" and second.asked == "et la suite ?"
    run(go())


def test_never_while_working_or_waiting_for_a_person(sess):
    async def go():
        await sess.ensure_started()
        sess.last_active -= 3600
        loop = asyncio.get_running_loop()
        for state in ("busy", "perm", "question"):
            fut = loop.create_future()
            if state == "busy":
                sess._busy = True
            elif state == "perm":
                sess._perms["p1"] = fut
            else:
                sess._questions["q1"] = fut
            assert await agentchat.park_idle(60) == [], state
            assert sess.client is not None and not sess.client.closed
            sess._busy = False
            sess._perms.clear()
            sess._questions.clear()
        # recent activity, or the switch at 0: kept too
        sess.last_active = __import__("time").monotonic()
        assert await agentchat.park_idle(60) == []
        sess.last_active -= 3600
        assert await agentchat.park_idle(0) == []
        assert await agentchat.park_idle(60) == ["idle-sid"]
        # a session never started (no CLI) has nothing to close
        assert await agentchat.park_idle(60) == []
    run(go())


@pytest.mark.parametrize("value,expected", [(None, 1800.0), ("600", 600.0), ("0", 0.0), ("x", 1800.0), ("-5", 0.0)])
def test_the_delay_is_a_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SOKKAN_SESSION_IDLE_CLOSE_S", raising=False)
    else:
        monkeypatch.setenv("SOKKAN_SESSION_IDLE_CLOSE_S", value)
    assert agentchat.idle_close_s() == expected


def test_a_cli_left_running_by_a_failed_close_is_reaped(sess):
    """Seen on the end-to-end pass (real CLI): one parked session kept its CLI alive, parent
    1, ~0.3 GB — the client's close raised before the process was stopped."""
    import subprocess
    import types

    class Broken(FakeCLI):
        async def __aexit__(self, *a):
            raise RuntimeError("Attempted to exit cancel scope in a different task")

    proc = subprocess.Popen(["sleep", "60"])
    try:
        async def go():
            sess.client = Broken()
            sess.client._transport = types.SimpleNamespace(_process=proc)
            sess.last_active -= 31 * 60
            assert await agentchat.park_idle(1800) == ["idle-sid"]
        run(go())
        assert proc.poll() is not None          # gone, not orphaned
        assert sess.client is None
    finally:
        if proc.poll() is None:
            proc.kill()
