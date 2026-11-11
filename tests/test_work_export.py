"""Feature `work_export` — « Export our work »: what the archive holds, that no secret leaves,
who may ask, and that the manifest tells the truth.

The world is the simple profile's (tests/simple_fixtures.py): admin@x is an instance admin,
bob@x a developer; the default project has a session with a deliverable; `radio` is a project
admin@x has NO role in (access by SSO group)."""
import hashlib
import io
import json
import zipfile

import pytest
from simple_fixtures import DEFAULT_SID, RADIO_SID, world  # noqa: F401

VAULT_SECRET = "s3cr3t-vault-value-7Kq2ZpLm9Xw4"      # a value of the test vault
MCP_SECRET = "mcp-header-value-Tt81BvQe55RrNn02"     # a value typed in a connector's config
ARG_SECRET = "sk-live-0123456789abcdefghijKLMN"       # a key typed in a connector's arguments
URL = "/api/admin/work-export"

NOTE = """---
name: {name}
description: >-
  {desc}
{extra}metadata:
  type: project
---
{body}
"""


def _note(dir_, name, desc, body, extra=""):
    p = dir_ / f"{name.replace('-', '_')}.md"
    p.write_text(NOTE.format(name=name, desc=desc, body=body, extra=extra), encoding="utf-8")
    return p


def _memory_index(db, notes):
    """A 2.x memory index (memory.db) that lists these note files — what a SQLite instance has."""
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE notes(name TEXT PRIMARY KEY, description TEXT, type TEXT,"
                " mtime REAL, source_path TEXT, priority INTEGER DEFAULT 0)")
    for name, path in notes:
        con.execute("INSERT INTO notes VALUES(?,?,?,?,?,0)", (name, "", "project", 0.0, str(path)))
    con.commit()
    con.close()


@pytest.fixture()
def org(world, monkeypatch):  # noqa: F811
    """An organisation that worked: notes, an agent with a run, a skill, instructions, an MCP
    server with credentials, two deliverables — and secrets placed where they could leak."""
    import agents
    import documents
    import memorykb
    import simple
    import store_backend
    import vault

    tmp, ws = world["tmp"], world["workspace"]
    monkeypatch.setenv("SOKKAN_FEATURE_WORK_EXPORT", "1")
    monkeypatch.setattr(store_backend, "enabled", lambda: False)
    monkeypatch.setattr(agents, "DB", tmp / "agents.db")
    monkeypatch.setattr(agents, "_initialized_for", None)

    vault.set_secret("CRM_TOKEN", VAULT_SECRET)

    mem = tmp / "memory"
    mem.mkdir()
    n1 = _note(mem, "training-plan-method", "How the team builds a training plan",
               "Three steps. See [[client-brief-template]].")
    n2 = _note(mem, "client-brief-template", "The questions asked before a quote",
               f"Never paste the CRM token ({VAULT_SECRET}) in a brief.")
    n3 = _note(mem, "board-minutes", "Minutes of the board", "Restricted content.",
               extra="classification: restricted\n")
    monkeypatch.setattr(memorykb, "MEM_DB", tmp / "memory.db")
    _memory_index(tmp / "memory.db", [("training-plan-method", n1), ("client-brief-template", n2),
                                      ("board-minutes", n3)])

    (ws / "CLAUDE.md").write_text("# Atelier\n\nAnswer in French. Cite the source document.\n")
    skill = ws / ".claude" / "skills" / "quote-follow-up"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: quote-follow-up\ndescription: Draft a follow-up mail from a quote\n---\n"
        "1. Read the quote.\n2. Draft the mail.\n")
    (skill / "template.md").write_text("Dear {name},\n")
    (ws / ".claude" / "commands").mkdir()
    (ws / ".claude" / "commands" / "weekly.md").write_text("Summarise the week.\n")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "crm": {"command": "npx", "args": ["crm-mcp", "--key", ARG_SECRET],
                "env": {"CRM_API_TOKEN": VAULT_SECRET, "CRM_URL": "https://crm.atelier.example"}},
        "wiki": {"type": "http", "url": "https://wiki.atelier.example/mcp",
                 "headers": {"Authorization": f"Bearer {MCP_SECRET}"}}}}))

    admin = {"email": "admin@x", "role": "admin"}
    a = agents.create(admin, {"name": "weekly-digest", "purpose": "Summarise the week's work.",
                              "deliverable": "A one-page digest", "done_criteria": "Sent on Friday",
                              "trigger": "manual", "secrets": ["CRM_TOKEN"]},
                      known_secrets=["CRM_TOKEN"])
    rid = agents.enqueue_run(a["id"], "manual", requested_by="admin@x")
    rid = rid["id"] if isinstance(rid, dict) else rid
    agents.update_run(rid, status="succeeded", deliverable=f"# Digest\n\nToken seen: {VAULT_SECRET}\n",
                      ended_at=1_800_000_000.0)

    d = simple.deliverable_dir("default", DEFAULT_SID)
    d.mkdir(parents=True)
    (d / "Training plan.md").write_text("# Training plan\n\nTwo days, twelve people.\n", encoding="utf-8")
    simple.note_write(DEFAULT_SID, "Training plan.md", "t1")
    simple.validate("default", DEFAULT_SID, "Training plan.md", "bob@x")
    r = simple.deliverable_dir("radio", RADIO_SID)
    r.mkdir(parents=True)
    (r / "Radio notes.md").write_text("# Radio\n", encoding="utf-8")
    assert documents.project_root("default") == ws
    return world


def _zip(resp):
    assert resp.status_code == 200, resp.text
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    return z, {n: z.read(n) for n in z.namelist()}


# ---- who may ask --------------------------------------------------------------------------
def test_off_by_default_404(org, monkeypatch):
    monkeypatch.delenv("SOKKAN_FEATURE_WORK_EXPORT")
    assert org["as"]("admin@x").get(URL).status_code == 404


@pytest.mark.parametrize("who", ["bob@x", "carol@x", "alice@x"])
def test_a_non_admin_is_refused(org, who):
    r = org["as"](who).get(URL)
    assert r.status_code == 403
    assert not org["audit"]("work.export")


def test_a_project_admin_who_is_not_an_instance_admin_is_refused(org):
    import projects
    projects.grant("radio", "user", "alice@x", "admin")
    assert org["as"]("alice@x", "radio").get(URL).status_code == 403


# ---- what the archive holds ---------------------------------------------------------------
def test_archive_holds_each_kind_of_work(org):
    r = org["as"]("admin@x").get(URL)
    z, f = _zip(r)
    assert z.testzip() is None
    assert r.headers["content-type"] == "application/zip"
    assert "sokkan-work-export-" in r.headers["content-disposition"]
    names = set(f)
    # the memory: markdown with its frontmatter, as written
    plan = f["memory/default/training_plan_method.md"].decode()
    assert plan.startswith("---\nname: training-plan-method\n") and "[[client-brief-template]]" in plan
    # the tools
    agent = json.loads(f["tools/default/agents/weekly-digest.json"])
    assert agent["purpose"] == "Summarise the week's work." and agent["secrets"] == ["CRM_TOKEN"]
    assert agent["deliverable"] == "A one-page digest" and agent["trigger"] == "manual"
    assert f["tools/default/instructions/CLAUDE.md"].decode().startswith("# Atelier")
    assert f["tools/default/skills/quote-follow-up/SKILL.md"].decode().startswith("---\nname: quote-follow-up")
    assert "tools/default/skills/quote-follow-up/template.md" in names
    assert "tools/default/commands/weekly.md" in names
    mcp = json.loads(f["tools/default/connectors/mcp.json"])
    assert set(mcp["mcpServers"]) == {"crm", "wiki"}
    assert mcp["mcpServers"]["crm"]["env"] == {"CRM_API_TOKEN": "[masked]", "CRM_URL": "[masked]"}
    assert mcp["mcpServers"]["wiki"]["url"] == "https://wiki.atelier.example/mcp"
    assert mcp["mcpServers"]["crm"]["args"] == ["crm-mcp", "--key", "[masked:key]"]
    # the deliverables, with who validated them
    folder = f"deliverables/default/default work ({DEFAULT_SID[:8]})"
    assert f[f"{folder}/Training plan.md"].decode().startswith("# Training plan")
    index = json.loads(f["deliverables/default/index.json"])
    assert index[0]["file"] == f"{folder}/Training plan.md" and index[0]["validated_by"] == "bob@x"
    assert index[0]["work"] == "default work" and index[0]["opened_by"] == "bob@x"
    assert any(n.startswith("deliverables/default/crew/weekly-digest/run-") for n in names)
    # the README says it in both languages and names every folder
    readme = f["README.md"].decode()
    for word in ("Your work, out of SOKKAN", "Votre travail, hors de SOKKAN", "memory/", "tools/",
                 "deliverables/", "manifest.json", "SKILL.md"):
        assert word in readme


def test_no_secret_anywhere_in_the_archive(org):
    r = org["as"]("admin@x").get(URL)
    _z, f = _zip(r)
    import base64
    for needle in (VAULT_SECRET, MCP_SECRET, ARG_SECRET,
                   base64.b64encode(VAULT_SECRET.encode()).decode()):
        assert needle.encode() not in r.content
        for name, data in f.items():
            assert needle.encode() not in data, f"{needle[:6]}… found in {name}"
    # masked where it stood, the text around it kept — and the manifest says which files
    brief = f["memory/default/client_brief_template.md"].decode()
    assert "Never paste the CRM token ([secret:CRM_TOKEN]) in a brief." in brief
    run = next(d for n, d in f.items() if "/crew/weekly-digest/" in n).decode()
    assert "[secret:CRM_TOKEN]" in run
    by_path = {m["path"]: m for m in json.loads(f["manifest.json"])["files"]}
    assert by_path["memory/default/client_brief_template.md"]["masked"] is True
    assert by_path["memory/default/training_plan_method.md"]["masked"] is False
    # … and a file that is not marked is byte for byte what was saved
    assert f["memory/default/training_plan_method.md"] == (
        org["tmp"] / "memory" / "training_plan_method.md").read_bytes()
    assert f["tools/default/skills/quote-follow-up/SKILL.md"] == (
        org["workspace"] / ".claude" / "skills" / "quote-follow-up" / "SKILL.md").read_bytes()
    # the vault file and its key are not in it under any name
    assert not [n for n in f if "vault" in n.lower() or n.endswith(".key")]


def test_a_binary_holding_a_vault_value_is_left_out_and_listed(org):
    (org["workspace"] / ".claude" / "skills" / "quote-follow-up" / "blob.bin").write_bytes(
        b"\xff\xfe\x00" + VAULT_SECRET.encode() + b"\x00\xff")
    r = org["as"]("admin@x").get(URL)
    _z, f = _zip(r)
    assert "tools/default/skills/quote-follow-up/blob.bin" not in f
    skipped = json.loads(f["manifest.json"])["skipped"]
    assert [s for s in skipped if s["path"].endswith("blob.bin") and "vault value" in s["reason"]]
    assert VAULT_SECRET.encode() not in r.content


def test_export_refused_when_a_value_would_remain(org, monkeypatch):
    """The last line of defence: masking switched off by accident = no archive at all."""
    import agents
    monkeypatch.setattr(agents, "redact_obj", lambda obj, secrets: obj)
    r = org["as"]("admin@x").get(URL)
    assert r.status_code == 500 and "export refused" in r.json()["detail"]
    assert VAULT_SECRET not in r.text
    refused = org["audit"]("work.export.refused")
    assert len(refused) == 1 and VAULT_SECRET not in refused[0]["detail"]
    assert not org["audit"]("work.export")


def test_export_refused_when_the_vault_cannot_be_read(org, monkeypatch):
    import vault

    def down(*a, **k):
        raise RuntimeError("provider unreachable")
    monkeypatch.setattr(vault, "session_env", down)
    r = org["as"]("admin@x").get(URL)
    assert r.status_code == 503
    assert org["audit"]("work.export.refused")


# ---- the rights that already exist --------------------------------------------------------
def test_only_the_projects_the_admin_has_a_role_in(org):
    """admin@x manages the instance but has no role in `radio` (access by SSO group)."""
    _z, f = _zip(org["as"]("admin@x").get(URL))
    m = json.loads(f["manifest.json"])
    assert "radio" not in [p["slug"] for p in m["projects"]]
    assert m["projects_not_included"] >= 1
    # 3.6.3: the same two numbers reach the screen (response headers), nothing more
    r = org["as"]("admin@x").get(URL)
    assert r.headers["x-sokkan-export-projects"] == str(len(m["projects"]))
    assert r.headers["x-sokkan-export-projects-not-included"] == str(m["projects_not_included"])
    assert not [n for n in f if "/radio/" in n]
    assert b"Radio" not in b"".join(f.values())


def test_a_project_the_admin_joins_is_exported(org):
    import projects
    projects.grant("radio", "user", "admin@x", "admin")
    _z, f = _zip(org["as"]("admin@x").get(URL))
    assert f[f"deliverables/radio/radio work ({RADIO_SID[:8]})/Radio notes.md"].decode() == "# Radio\n"


def test_a_note_above_the_clearance_is_not_exported(org):
    """`board-minutes` is restricted; every role reads up to `project` by default."""
    _z, f = _zip(org["as"]("admin@x").get(URL))
    assert not [n for n in f if "board_minutes" in n]
    blob = b"".join(f.values())
    assert b"board-minutes" not in blob and b"Restricted content" not in blob


def test_the_memory_scope_decides_not_the_export(org, monkeypatch):
    """No rule of its own: with an empty memory scope for the person, no note leaves."""
    import classification
    monkeypatch.setattr(classification, "scope_for", lambda user, project: ())
    _z, f = _zip(org["as"]("admin@x").get(URL))
    assert not [n for n in f if n.startswith("memory/")]
    assert json.loads(f["manifest.json"])["counts"]["notes"] == 0


def test_workspace_symlinks_are_not_followed(org):
    outside = org["tmp"] / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("not part of the workspace")
    skills = org["workspace"] / ".claude" / "skills"
    (skills / "linked").symlink_to(outside, target_is_directory=True)
    (skills / "quote-follow-up" / "link.md").symlink_to(outside / "private.md")
    _z, f = _zip(org["as"]("admin@x").get(URL))
    assert b"not part of the workspace" not in b"".join(f.values())


# ---- the manifest and the journal ---------------------------------------------------------
def test_manifest_matches_the_archive(org):
    r = org["as"]("admin@x").get(URL)
    _z, f = _zip(r)
    m = json.loads(f["manifest.json"])
    assert m["format"] == "sokkan-work-export/1" and m["generated_by"] == "admin@x"
    from pathlib import Path
    assert m["sokkan_version"] == (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert m["generated_at"].endswith("Z")
    listed = {x["path"]: x for x in m["files"]}
    assert set(listed) == set(f) - {"README.md", "manifest.json"}
    for path, x in listed.items():
        assert hashlib.sha256(f[path]).hexdigest() == x["sha256"], path
        assert len(f[path]) == x["bytes"]
    assert m["files_total"] == len(listed) and m["bytes_total"] == sum(x["bytes"] for x in listed.values())
    assert m["counts"] == {"notes": 2, "agents": 1, "skills": 1, "instructions": 1, "commands": 1,
                           "subagents": 0, "connectors": 1, "deliverables": 1,
                           "agent_deliverables": 1}
    assert m["masked_files"] >= 2


def test_every_export_is_journaled_with_its_fingerprint(org):
    r = org["as"]("admin@x").get(URL)
    sha = hashlib.sha256(r.content).hexdigest()
    assert r.headers["x-sokkan-export-sha256"] == sha
    ev = org["audit"]("work.export")
    assert len(ev) == 1
    e = ev[0]
    assert e["user"] == "admin@x" and "default" in e["resource"].split(",")
    assert "radio" not in e["resource"]
    assert sha in e["detail"] and "2 notes" in e["detail"] and "1 agents" in e["detail"]
    assert e["ts"] > 0


def test_public_mcp_keeps_names_and_drops_values():
    import work_export
    out = json.loads(work_export.public_mcp(json.dumps({"mcpServers": {"a": {
        "command": "run", "args": ["--token", "ghp_0123456789abcdefghijklmnop"],
        "env": {"A_KEY": "x", "B": "y"}, "headers": {"X-Api-Key": "z"},
        "options": {"client_secret": "plainvalue", "region": "eu"}}}})))
    a = out["mcpServers"]["a"]
    assert a["env"] == {"A_KEY": "[masked]", "B": "[masked]"} and a["headers"] == {"X-Api-Key": "[masked]"}
    assert a["command"] == "run" and "ghp_" not in json.dumps(a)
    assert a["options"] == {"client_secret": "[masked]", "region": "eu"}
    assert b"hunter2hunter2" not in work_export.public_mcp("not json password=hunter2hunter2")


def test_registry_declares_the_feature():
    import features
    f = features.BY_ID["work_export"]
    assert f.defaults == {"community": False, "enterprise": False} and f.status == "beta"
    assert features.enabled("work_export", {"SOKKAN_FEATURE_WORK_EXPORT": "1"})
    assert not features.enabled("work_export", {})


# ---- the 3.0 store (Postgres): levels come from the store, the clearance from the scope ---
PG_DSN = __import__("os").environ.get("SOKKAN_TEST_PG_DSN", "")


@pytest.fixture()
def pg_store():
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("pgvector")
    pytest.importorskip("psycopg_pool")
    if not PG_DSN:
        pytest.skip("SOKKAN_TEST_PG_DSN not set (needs Postgres)")
    import uuid

    from core.store import Store
    name = "sokkan_wx_" + uuid.uuid4().hex[:10]
    with psycopg.connect(PG_DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    s = Store(PG_DSN.rsplit("/", 1)[0] + "/" + name)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(PG_DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


def test_store_levels_and_clearance_decide_the_notes(org, pg_store, monkeypatch):
    import classification
    import store_backend
    from core.contract import ChunkRecord, NoteRecord

    mem = org["tmp"] / "memory"
    files = {"plan": (2, mem / "training_plan_method.md"),
             "pricing": (3, _note(mem, "pricing-grid", "The price grid", "Confidential grid.")),
             "minutes": (4, mem / "board_minutes.md"),
             "gone": (2, mem / "gone.md")}                       # in the store, no file on disk
    g = pg_store.create_generation("test:model@8", 8)
    pg_store.upsert_notes([
        (NoteRecord(name, f"about {name}", "project", 0, str(path), None, "indexed",
                    f"body of {name}", "default", level), [ChunkRecord(0, name, [0.1] * 8)])
        for name, (level, path) in files.items()], g.id)
    pg_store.activate_generation(g.id)
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    monkeypatch.setattr(store_backend, "get_store", lambda: pg_store)

    def notes(cap):
        monkeypatch.setattr(classification, "scope_for",
                            lambda user, project: (f"{project}@{cap}", f"shared@{cap}"))
        _z, f = _zip(org["as"]("admin@x").get(URL))
        m = json.loads(f["manifest.json"])
        return {x["note"]: x for x in m["files"] if x["type"] == "note"}, f

    got, f = notes(2)
    assert set(got) == {"plan", "gone"}
    assert got["gone"]["rebuilt_from_store"] is True
    rebuilt = f["memory/default/gone.md"].decode()
    assert rebuilt.startswith("---\nname: gone\n") and rebuilt.rstrip().endswith("body of gone")
    assert b"Confidential grid" not in b"".join(f.values())
    got, f = notes(3)
    assert set(got) == {"plan", "gone", "pricing"} and got["pricing"]["level"] == "confidential"
    assert b"Restricted content" not in b"".join(f.values())
    # the file says `restricted` in its frontmatter while the store still says `project`
    # (reclassified by hand, not indexed yet): the higher of the two wins
    pg_store.set_level("minutes", 2, project="default", by="admin@x", reason="test")
    got, f = notes(3)
    assert "minutes" not in got and b"Restricted content" not in b"".join(f.values())
    assert set(notes(4)[0]) == {"plan", "gone", "pricing", "minutes"}
    # the audited recall knows the export handed these notes out
    log = pg_store.access_log(projects=["default"])
    assert {r["via"] for r in log} == {"export"} and {r["actor"] for r in log} == {"admin@x"}
