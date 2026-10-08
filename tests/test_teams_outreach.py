"""SOKKAN 3.4.1 — « Nina asks for help »: from the cockpit's Nina panel, a proposal (candidates
of the project with their availability, the mapped channel, the message with a real @mention,
a signed single-use token) that the person sends with a click — against the Graph / Bot
Framework simulator (tests/teams_sim.py), in the `radio` world of test_teams (alice: dev
cleared confidential; carol: dev; max: maintainer; boss@x: admin by grant, never signed in with
Entra → no link)."""
import json
import re

import pytest

from teams_sim import CHANNEL, TENANT
from test_teams import AAD, make_tw

BLOCK = re.compile(r"```sokkan-outreach\n(.*?)\n```", re.S)


@pytest.fixture()
def tw(tmp_path, monkeypatch):
    w = make_tw(tmp_path, monkeypatch)
    from teams_sim import SERVICE
    monkeypatch.setenv("SOKKAN_TEAMS_SERVICE_URL", SERVICE)   # no activity seen in the channel yet
    import iam
    for email, name in (("alice@x", "Alice"), ("carol@x", "Carol"), ("max@x", "Max")):
        iam.upsert_user(email, "dev", name)
    return w


def _ask(tw, who, text, project="radio"):
    r = tw["as"](who, project).post("/api/assistant/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def _proposal(reply: str) -> dict:
    m = BLOCK.search(reply)
    assert m, reply
    return json.loads(m.group(1))


# ---- the proposal ----------------------------------------------------------------------------
def test_candidates_are_the_project_members_minus_me_and_the_assignee(tw):
    import board
    sim = tw["sim"]
    sim.presence.update({"aad-carol": "Available", "aad-max": "Busy"})
    out = _ask(tw, "alice@x", "find me someone available to help with the radio bug")
    assert out["via"] == "outreach" and sim.sent == []          # nothing posted: a proposal
    p = _proposal(out["reply"])
    assert p["card"]["id"] == board.search_cards("radio bug", project="radio")[0]["id"]
    emails = [c["email"] for c in p["candidates"]]
    assert "alice@x" not in emails and set(emails) == {"carol@x", "max@x", "boss@x"}
    assert p["channel"]["id"] == CHANNEL and p["channel"]["name"] == "radio · General"
    assert p["token"] and p["to"] == "carol@x"
    # the assignee of the card is not asked to help on their own card
    board.update_card(p["card"]["id"], assignee="carol@x", user="alice@x")
    p = _proposal(_ask(tw, "alice@x", f"demande de l'aide sur la carte #{p['card']['id']}")["reply"])
    assert "carol@x" not in [c["email"] for c in p["candidates"]] and p["lang"] == "fr"
    assert "peux-tu aider Alice sur « radio bug »" in p["candidates"][0]["text"]


def test_ranking_available_then_unknown_then_busy_with_reasons(tw):
    sim = tw["sim"]
    sim.presence.update({"aad-carol": "Busy", "aad-max": "Available"})
    sim.events["carol@x"] = [{"subject": "Release review", "showAs": "busy",
                              "start": {"dateTime": "2000-01-01T00:00:00.0000000"},
                              "end": {"dateTime": "2100-01-01T00:00:00.0000000"}}]
    p = _proposal(_ask(tw, "alice@x", "who is available to help with the radio bug?")["reply"])
    by = {c["email"]: c for c in p["candidates"]}
    assert [c["email"] for c in p["candidates"]] == ["max@x", "boss@x", "carol@x"]
    assert by["max@x"]["state"] == "available" and "available in Teams" in by["max@x"]["reason"]
    assert by["boss@x"]["state"] == "free" and "not signed in" in by["boss@x"]["reason"]
    assert "calendar free" in by["boss@x"]["reason"]
    assert by["carol@x"]["state"] == "busy" and "in a meeting" in by["carol@x"]["reason"]
    assert by["carol@x"]["next_free"].startswith("2100-01-01")
    assert by["max@x"]["mention"] is True and by["boss@x"]["mention"] is False
    assert "<at>Max</at>" in by["max@x"]["text"] and "<at>" not in by["boss@x"]["text"]
    assert p["to"] == "max@x" and "**Max**" in _ask(tw, "alice@x", "who is available to help with the radio bug?")["reply"]


def test_graph_down_means_unknown_never_an_error(tw, monkeypatch):
    from teams import graph
    monkeypatch.setattr(graph, "presence", lambda oid: (_ for _ in ()).throw(RuntimeError("503")))
    monkeypatch.setattr(graph.GraphCalendar, "events", lambda self, *a: (_ for _ in ()).throw(RuntimeError("503")))
    p = _proposal(_ask(tw, "alice@x", "trouve-moi quelqu'un de disponible pour aider sur le radio bug")["reply"])
    assert p["token"] and all(c["state"] == "unknown" for c in p["candidates"])
    assert all("indisponibles" in c["reason"] for c in p["candidates"] if c["email"] != "boss@x")


def test_a_card_above_the_channel_is_never_named_in_teams(tw):
    """alice (cleared confidential) asks about « key rotation » (level 3); the only channel is
    at level project → « a confidential card (#n) », the title stays out of the message. A
    confidential channel exists → the widest channel that may hear it, title shown."""
    import board
    from teams import store
    conf = board.search_cards("key rotation", project="radio", max_level=3)[0]
    out = _ask(tw, "alice@x", "find me someone to help with key rotation")
    p = _proposal(out["reply"])
    assert p["card"]["id"] == conf["id"] and p["card"]["title"] is None and p["redacted"]
    assert all("key rotation" not in c["text"] for c in p["candidates"])      # never in Teams
    assert "key rotation" not in out["reply"].split("```")[0]                  # nor in the intro
    assert f"a confidential card (#{conf['id']})" in p["candidates"][0]["text"]
    assert "above the level of the channel" in out["reply"]
    # carol (clearance project) cannot even find that card: her own words are the subject
    p = _proposal(_ask(tw, "carol@x", "find me someone to help with key rotation")["reply"])
    assert p["card"] is None and p["subject"] == "key rotation"
    store.map_channel("19:sec@thread.tacv2", "radio", 3, "radio · Security")
    store.map_channel("19:top@thread.tacv2", "radio", 4, "radio · Restricted")
    p = _proposal(_ask(tw, "alice@x", "find me someone to help with key rotation")["reply"])
    assert p["channel"]["id"] == "19:sec@thread.tacv2" and not p["redacted"]
    assert "“key rotation”" in p["candidates"][0]["text"]
    # a free subject goes to the widest channel
    p = _proposal(_ask(tw, "alice@x", "find me someone to help with the TLS rotation")["reply"])
    assert p["channel"]["id"] == CHANNEL and p["card"] is None


def test_no_channel_no_people_or_ambiguous_card_nina_says_so(tw):
    import board
    from teams import store
    store.unmap_channel(CHANNEL)
    out = _ask(tw, "alice@x", "find me someone available to help with the radio bug")
    p = _proposal(out["reply"])
    assert p["channel"] is None and p["token"] is None and p["candidates"]
    assert "Setup › Organization › Teams" in out["reply"]
    store.map_channel(CHANNEL, "radio", 2, "radio · General")
    board.add_card("radio bug on iOS", "", project="radio")
    out = _ask(tw, "alice@x", "find me someone available to help with radio")
    assert "Several cards match" in out["reply"] and "radio bug on iOS" in out["reply"] and "```sokkan-outreach" not in out["reply"]
    out = _ask(tw, "alice@x", "trouve-moi quelqu'un de disponible")
    assert "quelle carte" in out["reply"] and "```" not in out["reply"]
    # nobody else: a project where the requester is alone
    import projects
    projects.create("solo", "Solo", created_by="admin@x")
    projects.grant("solo", "user", "alice@x", "dev")
    store.map_channel("19:solo@thread.tacv2", "solo", 2, "solo")
    out = _ask(tw, "alice@x", "find me someone to help with the build", project="solo")
    assert "Nobody but you" in out["reply"]


def test_not_an_outreach_request_goes_to_the_model(tw):
    out = _ask(tw, "alice@x", "how do I get help on the key rotation plan?")
    assert out["via"] == "primary" and "notes:" in out["reply"]


def test_feature_off_the_model_answers(tw, monkeypatch):
    import teams
    monkeypatch.setattr(teams, "enabled", lambda: False)
    out = _ask(tw, "alice@x", "find me someone available to help with the radio bug")
    assert out["via"] == "primary"


# ---- the send --------------------------------------------------------------------------------
def test_send_posts_once_with_a_real_mention_and_is_journaled(tw):
    import audit
    sim = tw["sim"]
    sim.presence.update({"aad-carol": "Available"})
    p = _proposal(_ask(tw, "alice@x", "find me someone available to help with the radio bug")["reply"])
    c = tw["as"]("alice@x", "radio")
    r = c.post("/api/assistant/outreach/send", json={"token": p["token"], "to": "carol@x"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["to"] == "carol@x"
    assert out["link"].startswith(f"https://teams.microsoft.com/l/message/{CHANNEL}/") and TENANT in out["link"]
    msg = sim.sent[-1]
    assert msg["path"].endswith(f"/v3/conversations/{CHANNEL}/activities") and msg["method"] == "POST"
    assert msg["text"].startswith("<at>Carol</at>, could you help Alice with “radio bug”?")
    assert msg["entities"] == [{"type": "mention", "text": "<at>Carol</at>",
                                "mentioned": {"id": "aad-carol", "name": "Carol"}}]
    ev = [e for e in audit.recent(project="radio") if e["action"] == "teams.outreach"]
    assert ev and ev[0]["user"] == "alice@x" and ev[0]["resource"] == CHANNEL
    assert "to carol@x" in ev[0]["detail"] and f"card #{p['card']['id']}" in ev[0]["detail"]
    # single use
    r = c.post("/api/assistant/outreach/send", json={"token": p["token"], "to": "carol@x"})
    assert r.status_code == 409 and len(sim.sent) == 1


def test_send_without_a_link_mentions_nobody_and_only_the_requester_may_send(tw):
    sim = tw["sim"]
    p = _proposal(_ask(tw, "alice@x", "find me someone available to help with the radio bug")["reply"])
    # someone else holding the token (another session, a copied payload): refused, token kept
    r = tw["as"]("carol@x", "radio").post("/api/assistant/outreach/send", json={"token": p["token"], "to": "max@x"})
    assert r.status_code == 403 and sim.sent == []
    # a recipient outside the proposal: refused, token kept
    r = tw["as"]("alice@x", "radio").post("/api/assistant/outreach/send", json={"token": p["token"], "to": "alice@x"})
    assert r.status_code == 400 and sim.sent == []
    # boss@x has no Entra link: plain name, no entity
    r = tw["as"]("alice@x", "radio").post("/api/assistant/outreach/send", json={"token": p["token"], "to": "boss@x"})
    assert r.status_code == 200, r.text
    assert "entities" not in sim.sent[-1] and sim.sent[-1]["text"].startswith("boss, could you help Alice")
    # tampered token
    r = tw["as"]("alice@x", "radio").post("/api/assistant/outreach/send", json={"token": p["token"][:-3] + "abc", "to": "boss@x"})
    assert r.status_code == 400


def test_teams_down_gives_the_token_back(tw):
    sim = tw["sim"]
    p = _proposal(_ask(tw, "alice@x", "find me someone available to help with the radio bug")["reply"])
    sim.fail_connector = True
    c = tw["as"]("alice@x", "radio")
    r = c.post("/api/assistant/outreach/send", json={"token": p["token"], "to": "carol@x"})
    assert r.status_code == 502
    sim.fail_connector = False
    assert c.post("/api/assistant/outreach/send", json={"token": p["token"], "to": "carol@x"}).status_code == 200


def test_the_proposal_is_bound_to_the_project_of_the_request(tw):
    import projects
    p = _proposal(_ask(tw, "alice@x", "find me someone available to help with the radio bug")["reply"])
    projects.create("other", "Other", created_by="admin@x")
    projects.grant("other", "user", "alice@x", "dev")
    r = tw["as"]("alice@x", "other").post("/api/assistant/outreach/send", json={"token": p["token"], "to": "carol@x"})
    assert r.status_code == 404 and tw["sim"].sent == []


def test_post_message_contract(tw):
    """The Bot Framework contract of a mention: the entity's text is the tag in the message."""
    from teams import outreach
    from teams_sim import SERVICE
    res = outreach.post_message(SERVICE, CHANNEL, "<at>Max</at> ping", {"id": AAD["max@x"], "name": "Max"})
    assert res["id"]
    with pytest.raises(ValueError):
        outreach.post_message(SERVICE, CHANNEL, "Max ping", {"id": AAD["max@x"], "name": "Max"})
