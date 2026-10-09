"""3.4.4 security fixes found by the 09.10.2026 end-to-end smoke test.

1. A tmux target is a live window of a SOKKAN session of the request's project —
   never an arbitrary window of the host's tmux server (a `dev` could type into the
   operator's own root sessions through /api/send).
2. A vault value a tool printed in a PERSON's session is masked in the transcript a
   viewer reads (it was masked for agent runs only).
"""
import json
import time
from types import SimpleNamespace

import pytest

SECRET = "smk-9f3a1c77e2b4d6Zq"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agents
    import audit
    import board
    import iam
    import vault
    from fastapi.testclient import TestClient

    import app as appmod

    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(appmod, "PROJECT_DIR", proj)
    who = {"u": {"email": "dev@x.ch", "role": "dev", "name": "dev"}}
    iam.upsert_user("dev@x.ch", "dev")
    iam.upsert_user("viewer@x.ch", "viewer")

    def fake_user(request=None):
        return who["u"]
    monkeypatch.setattr(appmod.auth, "current_user", fake_user)
    appmod.app.dependency_overrides[appmod.current_user] = fake_user
    yield SimpleNamespace(app=appmod, board=board, vault=vault, proj=proj, who=who,
                          client=TestClient(appmod.app))
    appmod.app.dependency_overrides.clear()


def _tmux_row(board, sid, window, project="default"):
    con = board._con()
    con.execute("INSERT INTO sessions(session_id, tag, window, title, prompt, created_at, kind,"
                " project) VALUES(?,?,?,?,?,?, 'tmux', ?)",
                (sid, "infra", window, "t", "", time.time(), project))
    con.commit()
    con.close()


@pytest.fixture()
def tmux_host(env, monkeypatch):
    """The host tmux server: the operator's own sessions + one SOKKAN window."""
    host = [{"session": "A", "index": "0", "window": "Front_desk", "cmd": "claude", "activity": "0"},
            {"session": "sokkan", "index": "1", "window": "infra", "cmd": "claude", "activity": "0"},
            {"session": "sokkan", "index": "2", "window": "eng", "cmd": "claude", "activity": "0"}]
    monkeypatch.setattr(env.app, "_tmux_windows", lambda: host)
    sent = []
    monkeypatch.setattr(env.app.subprocess, "run", lambda args, **kw: sent.append(args))
    _tmux_row(env.board, "s-default", "sokkan:infra")
    _tmux_row(env.board, "s-eng", "sokkan:eng", project="eng")
    return sent


def test_send_refuses_a_window_of_the_host(env, tmux_host):
    r = env.client.post("/api/send", json={"target": "A:Front_desk", "text": "rm -rf ~"})
    assert r.status_code == 404, r.text
    assert tmux_host == []                      # nothing typed anywhere


def test_send_refuses_a_session_of_another_project(env, tmux_host):
    r = env.client.post("/api/send", json={"target": "sokkan:eng", "text": "hello"})
    assert r.status_code == 404, r.text
    assert tmux_host == []


def test_send_reaches_a_session_of_the_project(env, tmux_host):
    r = env.client.post("/api/send", json={"target": "sokkan:infra", "text": "hello"})
    assert r.status_code == 200, r.text
    assert ["tmux", "send-keys", "-t", "sokkan:infra", "-l", "hello"] in tmux_host


def test_tmux_listing_shows_only_the_project_sessions(env, tmux_host):
    r = env.client.get("/api/tmux")
    assert r.status_code == 200, r.text
    assert [f"{w['session']}:{w['window']}" for w in r.json()] == ["sokkan:infra"]


def _transcript(env, sid, csid, value):
    env.board.set_claude_session_id(sid, csid)
    (env.proj / f"{csid}.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "printenv"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": f"SMOKE_TOKEN={value}"}]}},
    ]))


@pytest.mark.parametrize("mode", ["named", "all"])
def test_person_session_transcript_masks_the_vault_values(env, monkeypatch, mode):
    monkeypatch.setenv("SOKKAN_SESSION_SECRETS", mode)
    env.vault.set_secret("SMOKE_TOKEN", SECRET)
    env.board.add_sdk_session("sidhuman1", "infra", title="ops",
                              secrets=["SMOKE_TOKEN"] if mode == "named" else None)
    _transcript(env, "sidhuman1", "c0ffee00-0000-4000-8000-0000000000a1", SECRET)
    env.who["u"] = {"email": "viewer@x.ch", "role": "viewer", "name": "viewer"}
    body = env.client.get("/api/sessions/sidhuman1")
    assert body.status_code == 200, body.text
    assert SECRET not in body.text
    assert "[secret:SMOKE_TOKEN]" in body.text


def test_named_session_keeps_values_it_did_not_receive(env, monkeypatch):
    """Only what the env held is masked: a session opened with no secret shows text as is."""
    monkeypatch.setenv("SOKKAN_SESSION_SECRETS", "named")
    env.vault.set_secret("SMOKE_TOKEN", SECRET)
    env.board.add_sdk_session("sidhuman2", "infra", title="ops", secrets=[])
    _transcript(env, "sidhuman2", "c0ffee00-0000-4000-8000-0000000000a2", "not-a-vault-value")
    body = env.client.get("/api/sessions/sidhuman2")
    assert body.status_code == 200 and "not-a-vault-value" in body.text


def test_live_events_of_a_person_session_are_masked(env, monkeypatch):
    """The WS replay ring of a reopened person session (after an API restart)."""
    import agentchat
    monkeypatch.setenv("SOKKAN_SESSION_SECRETS", "named")
    env.vault.set_secret("SMOKE_TOKEN", SECRET)
    env.board.add_sdk_session("sidhuman3", "infra", title="ops", secrets=["SMOKE_TOKEN"])
    monkeypatch.setattr(agentchat, "PROJECT_DIR", env.proj, raising=False)
    s = agentchat.get_or_create("sidhuman3")
    s._emit({"type": "tool_result", "text": f"SMOKE_TOKEN={SECRET}"})
    assert SECRET not in json.dumps(s.events) and "[secret:SMOKE_TOKEN]" in json.dumps(s.events)
    agentchat._registry.pop("sidhuman3", None)
