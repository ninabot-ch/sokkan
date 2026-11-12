"""3.6.4 — what the simple profile showed on a real install of 3.6.3.

* one document saved five times in a turn (the agent writes it a section at a time, each save
  approved): the thread showed one card per save, and once replayed (a new connection, the api
  restarted) every card said « version 5 ». The version of a save is now its own rank, live and
  replayed alike; the thread keeps ONE card per document (tests/nav/simple_thread_test.ts);
* a further message whose answer was lost was announced as failed, and sent twice on the second
  click: one id per message, taken once by the server;
* the technical cockpit looked for the transcript of a chat session in the instance's folder
  only (sessions list « active », transcript view);
* the title of a memory note: a slug has no accent, the note's own words have.
"""
import json
import re

import pytest

import simple_fixtures as _fx
from simple_fixtures import DEFAULT_SID, RADIO_SID

world = _fx.world

CSID = "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _transcript_dir(world, monkeypatch, sid):
    """Where the CLI files the transcript of `sid` (its own working directory)."""
    import agentchat
    claude = world["tmp"] / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.delenv("SOKKAN_PROJECT_DIR", raising=False)
    d = claude / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", agentchat.project_cwd(sid))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _five_saves(deliv):
    """One Write then four Edits of the same document, one refused Edit in between."""
    path = str(deliv / "Plan de cours.md")
    lines = [{"type": "user", "uuid": "u0", "message": {"role": "user", "content": "Prépare le plan de cours."}}]
    ids = ["toolu_w1", "toolu_e2", "toolu_x", "toolu_e3", "toolu_e4", "toolu_e5"]
    for i, tid in enumerate(ids):
        tool = "Write" if i == 0 else "Edit"
        inp = {"file_path": path, "content": "# Plan de cours\n"} if tool == "Write" else \
            {"file_path": path, "old_string": "x", "new_string": f"partie {i}"}
        lines.append({"type": "assistant", "uuid": f"a{i}", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": tool, "input": inp}]}})
        lines.append({"type": "user", "uuid": f"r{i}", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "ok" if tid != "toolu_x" else "refused",
             "is_error": tid == "toolu_x"}]}})
    lines.append({"type": "assistant", "uuid": "z", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Le plan est prêt."}]}})
    return lines


def _versions(sid):
    """The deliverable cards a browser receives when it opens the work (the WS replay)."""
    import agentchat
    import simple_api
    view = simple_api.make_view(sid)
    s = agentchat.get_or_create(sid, user="alice@x")
    return [(o["name"], o["version"]) for ev in list(s.events) for o in view.feed(ev)
            if o["type"] == "deliverable"]


@pytest.mark.parametrize("seen_live", [True, False])
def test_replayed_deliverables_keep_the_version_of_each_save_after_a_restart(world, monkeypatch, seen_live):
    import agentchat
    import board
    import simple

    deliv = simple.deliverable_dir("radio", RADIO_SID)
    deliv.mkdir(parents=True, exist_ok=True)
    (deliv / "Plan de cours.md").write_text("# Plan de cours\n", encoding="utf-8")
    board.set_claude_session_id(RADIO_SID, CSID)
    lines = _five_saves(deliv)
    if seen_live:
        # the turn as the person followed it: the live view registers each save as it lands
        import simple_api
        live = simple_api.make_view(RADIO_SID)
        from transcript import parse_lines
        got = []
        for m in parse_lines(json.dumps(x) for x in lines)["messages"]:
            if m.get("kind") == "tool":
                for o in live.feed({"type": "tool_use", "id": m["id"], "tool": m["tool"], "input": m["input"]}) + \
                        live.feed({"type": "tool_result", "tool_use_id": m["id"], **m["result"]}):
                    if o["type"] == "deliverable":
                        got.append(o["version"])
        assert got == [1, 2, 3, 4, 5]
    d = _transcript_dir(world, monkeypatch, RADIO_SID)
    (d / f"{CSID}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    monkeypatch.setattr(agentchat, "_registry", {})          # what a restart leaves

    first = _versions(RADIO_SID)
    assert first == [("Plan de cours.md", n) for n in (1, 2, 3, 4, 5)], first
    # opened again (another tab, a reconnection): the same numbers, nothing counted twice
    assert _versions(RADIO_SID) == first
    item, = simple.list_deliverables("radio", RADIO_SID)
    assert item["version"] == 5


def test_a_follow_up_sent_twice_with_the_same_id_is_taken_once(world):
    import agentchat
    c = world["as"]("bob@x")
    url = f"/api/simple/sessions/{DEFAULT_SID}/message"
    body = {"text": "Ajoute une pause à 10 h.", "request_id": "cd" * 16}
    first = c.post(url, json=body)
    assert first.status_code == 200 and first.json() == {"ok": True}, first.text
    # the agent is working on it — which is exactly when the browser asks again
    agentchat.get_or_create(DEFAULT_SID)._busy = True
    again = c.post(url, json=body)
    assert again.status_code == 200 and again.json() == {"ok": True, "replayed": True}, again.text
    assert len([1 for s, _ in world["turns"] if s == DEFAULT_SID]) == 1
    assert len(world["audit"]("session.send")) == 1
    # another message while it works is still refused as busy, as before
    other = c.post(url, json={"text": "Et une autre chose.", "request_id": "ef" * 16})
    assert other.status_code == 409 and other.json()["detail"]["code"] == "busy"
    agentchat.get_or_create(DEFAULT_SID)._busy = False
    # … and taken once it is free: a refused message did not burn its id
    assert c.post(url, json={"text": "Et une autre chose.", "request_id": "ef" * 16}).json() == {"ok": True}
    # no id, or one that is not an id: every message is sent, as before
    assert c.post(url, json={"text": "Encore."}).json() == {"ok": True}
    assert c.post(url, json={"text": "Encore.", "request_id": "../x"}).json() == {"ok": True}
    assert len(world["audit"]("session.send")) == 4
    # the id belongs to the work: the same one on another work is another message
    alice = world["as"]("alice@x", "radio")
    assert alice.post(f"/api/simple/sessions/{RADIO_SID}/message", json=body).json() == {"ok": True}
