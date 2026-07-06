import pytest


@pytest.fixture()
def board(tmp_path, monkeypatch):
    import board as b

    monkeypatch.setattr(b, "DB", tmp_path / "board.db")
    b.init(force=True)
    return b


def test_add_card_title_falls_back_to_description(board):
    c = board.add_card("", description="Fix the flux capacitor before Tuesday", tag="backend")
    assert c["title"].startswith("Fix the flux capacitor")
    assert c["bucket"] == "Backlog"
    assert c["checklist"] == []


def test_add_card_invalid_bucket_goes_to_backlog(board):
    c = board.add_card("t", bucket="NotABucket")
    assert c["bucket"] == "Backlog"


def test_update_card_field_allowlist(board):
    c = board.add_card("t")
    out = board.update_card(c["id"], user="u", title="new title", evil_field="x")
    assert out["title"] == "new title"
    assert "evil_field" not in out


def test_update_unknown_card_returns_none(board):
    assert board.update_card(99999, user="u", title="x") is None


def test_checklist_json_roundtrip(board):
    c = board.add_card("t")
    items = [{"text": "step one", "done": True}, {"text": "step two", "done": False}]
    out = board.update_card(c["id"], user="u", checklist=items)
    assert out["checklist"] == items
    # re-read from db
    assert board.get_card(c["id"])["checklist"] == items


def test_events_recorded(board):
    c = board.add_card("t", user="nick")
    board.update_card(c["id"], user="nick", bucket="Doing")
    board.update_card(c["id"], user="nick", priority=0)
    actions = [e["action"] for e in board.card_events(c["id"])]
    assert "création" in actions
    assert "déplacement" in actions
    assert "édition" in actions


def test_sdk_session_store_roundtrip(board):
    s = board.add_sdk_session("sid123", "backend", title="My task")
    assert s["kind"] == "sdk"
    board.set_claude_session_id("sid123", "csid456")
    assert board.get_claude_session_id("sid123") == "csid456"
    rows = board.list_sessions()
    assert rows and rows[0]["session_id"] == "sid123"
