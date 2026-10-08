"""3.4.2 — honest refusal on the 2.x index (``CORTHEXIS_MEMORY_BACKEND=sqlite``).

Seen live on 08.10: a decision captured in Teams for a project other than ``default`` was
written under ``projects/<slug>/memory`` where nothing indexes it — the person believed it
was in memory; ``/api/memory/notes`` said []. The 2.x index knows neither projects nor
levels: such a write is now refused with a clear message (and journaled), never written
half-way. ``default`` without a level: the 2.x behaviour, unchanged.
"""
from __future__ import annotations

import pytest

from test_classification import build_world
from test_teams import AAD, CHANNEL, _say, make_tw


def _sqlite(monkeypatch):
    """The world of test_classification serves the 3.0 store (a fake); here it does not."""
    import store_backend
    monkeypatch.setattr(store_backend, "enabled", lambda: False)
    monkeypatch.setattr(store_backend, "migrating", lambda: False)
    monkeypatch.setattr(store_backend, "configured", lambda: "sqlite")


@pytest.fixture()
def tw(tmp_path, monkeypatch):
    w = make_tw(tmp_path, monkeypatch)
    _sqlite(monkeypatch)
    return w


@pytest.fixture()
def world(tmp_path, monkeypatch):
    w = build_world(tmp_path, monkeypatch)
    _sqlite(monkeypatch)
    return w


def test_require_store_rules():
    import store_backend as sb
    sb.require_store("default", None)                    # the 2.x case: nothing to check
    sb.require_store("default", 2)
    sb.require_store("", 2)
    with pytest.raises(sb.StoreRequired) as e:
        sb.require_store("radio", 2)
    assert e.value.code == "memory_store_required" and "nothing was written" in str(e.value)
    with pytest.raises(sb.StoreRequired):
        sb.require_store("default", 3)
    info = sb.store_info()
    assert info["mode"] == "sqlite" and info["project_memory"] is False
    assert "store 3.0" in sb.store_required_message("fr")
    assert "migration en cours" in sb.store_required_message("fr", True)


# ---- Teams: a decision for a project ≠ default is refused, in the asker's language ---------
def test_teams_decision_for_a_project_is_refused_and_journaled(tw):
    import audit
    out = _say(tw, "alice@x", "note la décision : on garde Postgres 16 jusqu'en mars")[0]["text"]
    assert "store 3.0" in out and "rien n'a été écrit" in out       # French: French trigger
    assert "Decision noted" not in out and "Décision notée" not in out
    d = tw["tmp"] / "projects" / "radio" / "memory"
    assert not d.exists() or not list(d.glob("decision_*.md"))     # no phantom note
    rows = [r for r in audit.recent(50, project="radio") if r["action"] == "teams.decision.refused"]
    assert rows and rows[0]["user"] == "alice@x" and "memory_store_required" in rows[0]["detail"]
    assert not [r for r in audit.recent(50, project="radio") if r["action"] == "teams.decision"]
    out = _say(tw, "alice@x", "decision: keep Postgres 16 until March")[0]["text"]
    assert "3.0 store" in out and "nothing was written" in out     # English trigger


def test_teams_decision_in_a_personal_chat_names_the_store_too(tw):
    out = _say(tw, "alice@x", "dans radio : note la décision : on garde Postgres 16",
                conv_type="personal")[0]["text"]
    assert "store 3.0" in out


# ---- memory_write (MCP): a level above the default, or another project → 409 ---------------
def test_memory_write_refuses_a_level_or_a_project_but_keeps_default(world, monkeypatch, tmp_path):
    import memory_search_server as M
    monkeypatch.setattr(M, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "default")
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "default@4")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "admin@x")
    r = M.memory_write("conf-note", "a confidential fact", "body", classification="confidential")
    assert r["ok"] is False and r["code"] == "memory_store_required" and r["status"] == 409
    assert "nothing was written" in r["error"] and r["store"] == "sqlite"
    assert not (tmp_path / "memory" / "conf-note.md").exists()
    r = M.memory_write("plain-note", "a plain fact", "body")           # default, no level: 2.x
    assert r["ok"] and (tmp_path / "memory" / "plain-note.md").exists()
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_SCOPE", "radio@3,shared")
    r = M.memory_write("radio-note", "a radio fact", "body")
    assert r["ok"] is False and r["code"] == "memory_store_required"
    assert not (tmp_path / "projects" / "radio" / "memory").exists()
    monkeypatch.setenv("SOKKAN_AGENT_RUN", "1")                        # a run: no quarantine either
    r = M.memory_write("radio-run-note", "from a run", "body")
    assert r["ok"] is False and r["code"] == "memory_store_required" and not r.get("quarantined")


def test_quarantine_approval_of_a_project_note_is_409(world):
    carol = world["as"]("carol@x")
    r = carol.post("/api/memory/quarantine/radio-finding/approve")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "memory_store_required"
    assert r.json()["detail"]["store"]["mode"] == "sqlite"
    assert carol.get("/api/memory/quarantine/radio-finding").status_code == 200   # still there


def test_helm_writes_no_context_note_for_a_project_on_sqlite(world, tmp_path):
    import board
    import helm
    c = board.add_card("radio plan", "", project="radio", kind="project", intent="ship v2")
    assert helm.write_context_note(c["id"]) is None
    assert not (tmp_path / "projects" / "radio" / "memory").exists()
    k = board.add_card("radio step", "", project="radio", parent_id=c["id"])
    assert "ship v2" in helm.spawn_context(k["id"])                    # the block stays


# ---- visibility: the API says what serves -----------------------------------------------------
def test_memory_stats_and_status_expose_the_store(world):
    alice = world["as"]("alice@x")
    s = alice.get("/api/memory/stats").json()
    assert s["project"] == "radio" and s["notes"] == 0
    assert s["store"]["mode"] == "sqlite" and s["store"]["project_memory"] is False
    st = alice.get("/api/memory/status").json()
    assert st["backend"] == "sqlite" and st["store"]["mode"] == "sqlite"
    admin = world["as"]("admin@x", "default")
    s = admin.get("/api/memory/stats").json()
    assert s["project"] == "default" and s["store"]["mode"] == "sqlite"


def test_store_info_in_postgres_mode(monkeypatch):
    import store_backend as sb
    monkeypatch.setattr(sb, "enabled", lambda: True)
    monkeypatch.setattr(sb, "configured", lambda: "postgres")
    info = sb.store_info()
    assert info["mode"] == "postgres" and info["project_memory"] and not info["migrating"]
    sb.require_store("radio", 4)                         # nothing to refuse


_ = (AAD, CHANNEL)
