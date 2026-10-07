"""SOKKAN 3.2 lot 1 — the memory recall never crosses a project boundary.

Unit level (no database): core.scope, the Recaller (ranking AND quoted names), the command
hook entry point, the memory MCP server's scope from its environment, and the SOKKAN glue
(memrecall CLI settings, spawn pre-seed). The same rule against a real Postgres is in
test_recall_scope_pg.py.
"""
import io
import json

import pytest

from core import recall as R
from core import scope as S
from core.contract import Generation, NoteRecord
from core.search import Hit


def H(name, project, score=0.9):
    return Hit(name, score, score, 0.1, None, f"snippet of {name}", 3, "indexed",
               description=f"about {name}", generation=7, project=project)


def NR(name, project):
    return NoteRecord(name, f"about {name}", "project", 0, f"/m/{name}.md", None, "indexed",
                      f"body of {name}", project)


class Emb:
    rerank_policy = "off"

    def embed_query(self, text, timeout=30):
        return [1.0, 0.0]


class TwoProjectStore:
    """Notes of two projects. ``honours_scope=False`` = a store that ignores the scope
    (old implementation): the Recaller's own filter must still hold."""

    def __init__(self, honours_scope=True):
        self.honours = honours_scope
        self.gen = Generation(7, "llamacpp:embeddinggemma-300m-q8@768", 2, "2026-01-01", "active")
        self.notes = {
            "radio-deploy-runbook": NR("radio-deploy-runbook", "radio"),
            "radio-db-credentials-rotation": NR("radio-db-credentials-rotation", "radio"),
            "tv-deploy-runbook": NR("tv-deploy-runbook", "tv"),
            "tv-secret-roadmap": NR("tv-secret-roadmap", "tv"),
            "legacy-note": NR("legacy-note", "default"),
        }
        self.search_kw = []

    def active_generation(self):
        return self.gen

    def search(self, qv, text, k, rerank=None, **kw):
        self.search_kw.append(kw)
        hits = [H(n, r.project) for n, r in self.notes.items()]
        if self.honours and kw.get("projects") is not None:
            hits = [h for h in hits if h.project in kw["projects"]]
        return hits[:k]

    def recalled_notes(self, sid, agent_id=None):
        return set()

    def existing_names(self, names, projects=None):
        out = {n for n in names if n in self.notes}
        if self.honours and projects is not None:
            out = {n for n in out if self.notes[n].project in projects}
        return out

    def get_note(self, name):
        return self.notes.get(name)

    def log_recall(self, *a, **k):
        pass

    def log_recall_turn(self, *a, **k):
        pass


def rec(store, **cfg):
    return R.Recaller(store, Emb(), R.RecallConfig(threshold=0.1, top_k=10, **cfg),
                      profile="leger")


# ---- core.scope -----------------------------------------------------------------------

def test_scope_normalisation_is_fail_closed():
    assert S.normalize(None) is None
    assert S.normalize([]) == ()
    assert S.normalize("radio") == ("radio",)            # a string is ONE project
    assert S.normalize(["tv", "radio", "tv"]) == ("radio", "tv")
    assert S.normalize(["Bad Slug", "../x", ""]) == ()   # invalid names never widen
    assert S.from_env(None) is None and S.from_env("  ") is None
    assert S.from_env("radio, tv") == ("radio", "tv")
    assert S.project_of({"project": None}) == "default"  # unknown = default, not "everywhere"
    assert S.project_of(H("x", "Weird Name")) == "default"
    assert not S.visible(H("x", None), ("radio",))
    assert S.visible(H("x", None), ("default",))


# ---- Recaller -------------------------------------------------------------------------

@pytest.mark.parametrize("honours", [True, False])
def test_recall_only_injects_notes_of_the_session_project(honours):
    st = TwoProjectStore(honours_scope=honours)
    res = rec(st).recall("how do we deploy the player to production", projects=("radio",))
    assert res.notes and set(res.notes) <= {"radio-deploy-runbook",
                                            "radio-db-credentials-rotation"}
    assert all("tv-" not in line for line in res.context.splitlines())
    assert st.search_kw[-1] == {"projects": ("radio",)}


@pytest.mark.parametrize("honours", [True, False])
def test_a_quoted_note_name_of_another_project_is_not_forced(honours):
    """The name is a strong recall signal — it must not become a way around the scope."""
    st = TwoProjectStore(honours_scope=honours)
    res = rec(st).recall("please read tv-secret-roadmap and tell me the plan",
                         projects=("radio",))
    assert "tv-secret-roadmap" not in res.notes
    assert "tv-secret-roadmap" not in res.forced
    # the same quote inside the right project is forced as before
    res = rec(st).recall("please read tv-secret-roadmap and tell me the plan",
                         projects=("tv",))
    assert "tv-secret-roadmap" in res.notes


def test_empty_scope_recalls_nothing_and_no_scope_is_unchanged():
    st = TwoProjectStore()
    res = rec(st).recall("how do we deploy the player to production", projects=())
    assert res.skipped == "no-project" and not res.notes and not st.search_kw
    res = rec(st).recall("how do we deploy the player to production")
    assert {"radio-deploy-runbook", "tv-deploy-runbook", "legacy-note"} <= set(res.notes)
    assert st.search_kw[-1] == {}     # no scope → the store is called exactly as in 3.1


def test_hook_output_scopes_prompt_and_subagent_recall():
    st = TwoProjectStore(honours_scope=False)
    out = R.hook_output({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                         "prompt": "how do we deploy the player to production"},
                        rec(st), projects=("radio",))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "radio-deploy-runbook" in ctx and "tv-" not in ctx
    out = R.hook_output({"hook_event_name": "PreToolUse", "tool_name": "Task",
                         "session_id": "s", "tool_input": {
                             "description": "deploy", "prompt": "deploy the tv player now"}},
                        rec(st), projects=("radio",))
    prompt = out["hookSpecificOutput"]["updatedInput"]["prompt"]
    assert "radio-deploy-runbook" in prompt and "tv-deploy-runbook" not in prompt


def test_command_hook_requires_a_scope_when_asked(monkeypatch):
    """SOKKAN sets CORTHEXIS_RECALL_REQUIRE_SCOPE=1: without CORTHEXIS_RECALL_PROJECTS the
    in-process fallback recalls nothing (and never opens the store)."""
    opened = []
    monkeypatch.setattr(R, "default_recaller", lambda **k: opened.append(1) or rec(
        TwoProjectStore(honours_scope=False)))
    monkeypatch.setenv("CORTHEXIS_RECALL_REQUIRE_SCOPE", "1")
    monkeypatch.delenv("CORTHEXIS_RECALL_PROJECTS", raising=False)
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                          "prompt": "how do we deploy the player to production"})
    out = io.StringIO()
    assert R.main(io.StringIO(payload), out) == 0
    assert out.getvalue() == "" and not opened
    monkeypatch.setenv("CORTHEXIS_RECALL_PROJECTS", "radio")
    out = io.StringIO()
    R.main(io.StringIO(payload), out)
    ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "radio-deploy-runbook" in ctx and "tv-" not in ctx


# ---- memory MCP server ----------------------------------------------------------------

def test_mcp_server_reads_its_scope_from_the_api_environment(monkeypatch):
    import memory_search_server as mem
    import store_backend

    calls = {}
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    monkeypatch.setattr(store_backend, "memory_search",
                        lambda q, k, f, label, **kw: calls.setdefault("search", kw) and [])
    monkeypatch.setattr(store_backend, "memory_get",
                        lambda n, projects=None: calls.setdefault("get", projects) and None)
    monkeypatch.delenv("SOKKAN_SESSION_PROJECT", raising=False)
    mem.memory_search("deploy the player")
    assert calls.pop("search") == {"projects": None}      # outside SOKKAN: unchanged
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    mem.memory_search("deploy the player")
    assert calls.pop("search") == {"projects": ("radio",)}
    mem.memory_get("tv-secret-roadmap")
    assert calls.pop("get") == ("radio",)
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "")      # invalid → empty scope, not "all"
    mem.memory_search("deploy the player")
    assert calls.pop("search") == {"projects": ()}


def test_mcp_server_refuses_writes_and_legacy_reads_outside_the_default_project(
        monkeypatch, tmp_path):
    import memory_search_server as mem
    import store_backend

    monkeypatch.setattr(store_backend, "enabled", lambda: False)   # 2.x index
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    assert mem.memory_search("deploy")[0].get("empty")
    assert mem.memory_get("anything").startswith("note not found")
    r = mem.memory_write("radio-note", "d", "b")
    assert not r["ok"] and not list(tmp_path.iterdir())


def test_store_backend_filters_what_a_store_returns(monkeypatch):
    import store_backend

    st = TwoProjectStore(honours_scope=False)
    monkeypatch.setattr(store_backend, "get_store", lambda: st)
    monkeypatch.setattr(store_backend, "_reranker", lambda deep=False: None)
    out = store_backend.memory_search("deploy the player", 10, lambda q: [1.0, 0.0],
                                      projects=("radio",))
    assert {d["note_name"] for d in out} == {"radio-deploy-runbook",
                                            "radio-db-credentials-rotation"}
    assert store_backend.memory_search("deploy", 5, lambda q: [1.0], projects=())[0]["empty"]
    monkeypatch.setattr(store_backend, "age_header", lambda *a: "[h]")
    assert store_backend.memory_get("tv-secret-roadmap", projects=("radio",)) is None
    assert store_backend.memory_get("tv-secret-roadmap", projects=("tv",)).endswith(
        "body of tv-secret-roadmap")
    assert store_backend.memory_get("tv-secret-roadmap") is not None   # no scope: unchanged


# ---- SOKKAN glue ----------------------------------------------------------------------

def test_cli_settings_scope_the_fallback_hook(monkeypatch, tmp_path):
    import memrecall
    import projects

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    cmd = memrecall.cli_settings(projects=("default",))["hooks"]["UserPromptSubmit"][0][
        "hooks"][0]["command"]
    assert "CORTHEXIS_RECALL_REQUIRE_SCOPE=1" in cmd and "CORTHEXIS_RECALL_PROJECTS=default" in cmd
    assert memrecall._shared_scope() == ("default",)
    projects.create("radio", "Radio")
    assert memrecall._shared_scope() is None       # several projects: no shared fallback
    cmd = memrecall.cli_settings(projects=None)["hooks"]["UserPromptSubmit"][0]["hooks"][0][
        "command"]
    assert "CORTHEXIS_RECALL_PROJECTS" not in cmd and "REQUIRE_SCOPE=1" in cmd


def test_spawn_preseed_uses_the_session_project(monkeypatch, tmp_path):
    import app
    import board
    import projects

    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    projects.create("radio", "Radio")
    board.add_sdk_session("s-radio", "x", project="radio")
    seen = []
    monkeypatch.setattr(app.mem, "search_scoped",
                        lambda q, k, scope: seen.append(scope) or [])
    monkeypatch.setattr(app.store_backend, "enabled", lambda: False)
    app._memory_preseed("deploy the player", session_id="s-radio")
    app._memory_preseed("deploy the player", session_id="s-unknown")
    app._memory_preseed("deploy the player")
    assert seen == [("radio",), (), ()]   # unknown session with 2 projects: nothing
