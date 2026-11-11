"""An agent run writes in the work folder of ITS project — and nowhere else under <data>/projects.

Since 3.2 the rule « an agent run does not write memory files directly » matched the whole
`<data>/projects` tree (`agentchat._in_memory_dirs`): a Crew agent of a project other than
`default` works in `<data>/projects/<slug>/work` and could not write a single file there
(noted in docs/enterprise/PILOT-RELEASE-NOTES.md § 4, 11.11.2026). The rule now closes the
memory directories, the quarantine, and the rest of the tree — except the run's own work
folder. The decision is played through `AgentSession._can_use_tool`, as the SDK calls it.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def run(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(data))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory-default"))
    monkeypatch.delenv("SOKKAN_MEMORY_QUARANTINE_DIR", raising=False)
    import agentchat
    import hitl_journal
    journal = []
    monkeypatch.setattr(hitl_journal, "automatic", lambda kind, **kw: journal.append(
        (kw["allowed"], kw["reason"], kw["input_data"].get("file_path"))))
    project = {"slug": "formation"}
    monkeypatch.setattr(agentchat, "session_project", lambda sid: project["slug"])

    def session(mode="acceptEdits"):
        s = agentchat.AgentSession("run-1", cwd=str(data / "projects" / project["slug"] / "work"),
                                   user="ana@eskills.example",
                                   policy={"agent": "redacteur", "tools": ["Write", "Edit", "Read"], "mcp": []})
        s.mode = mode
        return s

    def write(s, path, tool="Write"):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        return asyncio.run(s._can_use_tool(tool, {key: str(path), "content": "x"}, None))
    return {"data": data, "tmp": tmp_path, "session": session, "write": write, "journal": journal,
            "project": project}


def _denied(res) -> bool:
    return type(res).__name__ == "PermissionResultDeny" and "memory_write" in res.message


def test_an_agent_run_writes_in_the_work_folder_of_its_own_project(run):
    s, work = run["session"](), run["data"] / "projects" / "formation" / "work"
    for path in (work / "rapport.md", work / "deliverables" / "abc" / "Plan de formation.md"):
        res = run["write"](s, path)
        assert type(res).__name__ == "PermissionResultAllow", (path, getattr(res, "message", ""))
    assert type(run["write"](s, work / "notes.md", tool="Edit")).__name__ == "PermissionResultAllow"
    # allowed by the run's mode, like any file outside the instance's own space — not by a hole
    assert [(a, r) for a, r, _p in run["journal"]] == [(True, "mode:acceptEdits")] * 3


def test_the_memory_the_quarantine_and_the_other_projects_stay_closed(run):
    import quarantine
    s, data = run["session"](), run["data"]
    closed = [
        data / "projects" / "formation" / "memory" / "glisse.md",        # its own project's memory
        data / "projects" / "formation" / "corthexis-review.db",         # the rest of its project
        data / "projects" / "formation" / "work",                         # the folder itself is not a file in it
        data / "projects" / "qualite" / "work" / "rapport.md",           # another project's work
        data / "projects" / "qualite" / "memory" / "glisse.md",
        data / "projects" / "formation" / "work" / ".." / "memory" / "glisse.md",   # a way round
        quarantine.memory_dir() / "glisse.md",                            # the default project's memory
        quarantine.qdir() / "glisse.md", quarantine.qdir("formation") / "glisse.md",
    ]
    for path in closed:
        assert _denied(run["write"](s, path)), path
    assert [(a, r) for a, r, _p in run["journal"]] == [(False, "policy:memory_write")] * len(closed)


def test_a_link_from_the_work_folder_into_the_memory_is_still_the_memory(run):
    s, data = run["session"](), run["data"]
    work, mem = data / "projects" / "formation" / "work", data / "projects" / "formation" / "memory"
    work.mkdir(parents=True)
    mem.mkdir(parents=True)
    (work / "raccourci").symlink_to(mem, target_is_directory=True)
    assert _denied(run["write"](s, work / "raccourci" / "glisse.md"))


@pytest.mark.parametrize("slug", ["default", ""])
def test_a_run_with_no_project_of_its_own_gets_nothing_under_projects(run, slug):
    """`default` works in the instance's workspace, an unknown session has no project: for
    both the whole tree stays closed, as it was."""
    run["project"]["slug"] = slug
    s = run["session"]()
    for path in (run["data"] / "projects" / "formation" / "work" / "rapport.md",
                 run["data"] / "projects" / "default" / "work" / "rapport.md",
                 run["data"] / "projects" / "_no-project" / "work" / "rapport.md"):
        assert _denied(run["write"](s, path)), (slug, path)
    assert type(run["write"](s, run["tmp"] / "elsewhere.md")).__name__ == "PermissionResultAllow"
