#!/usr/bin/env python3
"""work_export.py — « Export our work » (feature `work_export`).

An organisation that leaves — the end of a pilot, a change of tool — takes what its people
built here with it: ONE zip, readable without SOKKAN.

    README.md                                  what each folder is, how to reuse it (EN + FR)
    manifest.json                              version, date, counts, sha256 of every file
    memory/<project>/<note>.md                 the notes, as written (markdown + frontmatter)
    tools/<project>/agents/<name>.json         the Crew agents (their job description)
    tools/<project>/instructions/…             CLAUDE.md of the project's workspace
    tools/<project>/skills/<skill>/…           agent skills found in the workspace (SKILL.md)
    tools/<project>/commands/ · subagents/     slash commands and sub-agents of the workspace
    tools/<project>/connectors/…               declared MCP servers, alert channels — NO secret
    tools/_instance/connectors/…               instance-wide declarations (document sources…)
    deliverables/<project>/<work>/<file>       the deliverables of the simple profile
    deliverables/<project>/crew/<agent>/…      what the agents' runs delivered

What goes in is what THE PERSON WHO ASKS may read, by the rules that already exist — nothing
is decided here:

* a project is exported only when the person has a role in it (`projects.effective_role`);
  being an instance admin does not open a project one is not a member of;
* notes: the memory scope of the person in that project (`classification.scope_for`), the
  same filter the cockpit's memory tab applies — a note above their clearance is not in the
  archive, and nothing in the archive says it exists;
* agents: `agents.can_read`; their runs through `classification.redact_run`;
* deliverables: the sessions of the project the person may open (`projectgate.above_clearance`).

Secrets never leave. Three layers, the last one refuses the export instead of repairing it:

1. connectors are exported as their PUBLIC declaration: the `env` and `headers` of an MCP
   server keep their keys and lose every value; alert channels come from the store's public
   view (a secret field is `<key>_set: true`);
2. everything under ``tools/`` goes through `hitl_journal.mask` (the vault's exact values and
   their encoded forms, then the credential patterns) — file by file for what is copied from
   a workspace, value by value for the JSON written here; notes and deliverables go through
   the vault pass only (`agents.redact_obj`) — they are people's texts, a pattern pass would
   rewrite sentences that merely talk about a password;
3. `_guard`: the finished archive is searched for every value the vault and the model-key
   store hold; a hit raises `ExportLeak` (fail closed).

The vault itself, the model keys, the accounts, the journals and the documents people
uploaded are not part of this archive (docs/enterprise/OPERATIONS.md says where each lives).
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path

import hitl_journal

FORMAT = "sokkan-work-export/1"
MAX_FILE = 25 * 1024 * 1024          # a bigger file is listed in the manifest, not copied
MAX_TREE_FILES = 2000                # per workspace folder (skills, commands, sub-agents)
MAX_RUNS = 500                       # runs read per agent (the API's own ceiling)
INSTANCE = "_instance"
MASKED = "[masked]"

# workspace folder -> folder in the archive. These are the places a session of the project
# loads from (agentchat.setting_sources_for: `project` settings), in their own formats.
_WORKSPACE_TREES = ((".claude/skills", "skills"), (".claude/commands", "commands"),
                    (".claude/agents", "subagents"))
_INSTRUCTIONS = ("CLAUDE.md", ".claude/CLAUDE.md")


class ExportLeak(Exception):
    """The finished archive still held a value of the vault: not delivered."""


class ExportUnavailable(Exception):
    """The export cannot be made safely right now (the vault cannot be read)."""


# ---- small helpers ------------------------------------------------------------------------
def _iso(ts=None) -> str:
    d = (datetime.datetime.now(datetime.timezone.utc) if ts is None else
         datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc))
    return d.isoformat(timespec="seconds").replace("+00:00", "Z")


def version() -> str:
    """What this instance runs: the release the installer recorded, else the VERSION file."""
    env = (os.environ.get("SOKKAN_VERSION") or "").strip()
    root = Path(__file__).resolve().parent.parent
    for cand in (root / "VERSION", Path("/app/VERSION")):
        try:
            v = cand.read_text().strip()
        except OSError:
            continue
        return env if env.lstrip("v").startswith(v) else v
    return env or "dev"


_UNSAFE = re.compile(r'[<>:"|?*\\\x00-\x1f\x7f]')


def _seg(name: str) -> str:
    """One path segment that is safe in any zip reader (no separator, no dot-dot)."""
    s = _UNSAFE.sub("_", str(name or "")).replace("/", "_").strip().strip(".")
    return s[:150] or "_"


def _json(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True, default=str)
            + "\n").encode("utf-8")


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class _Archive:
    """The files of the archive while it is built, with what the manifest says of each."""

    def __init__(self, secrets: dict[str, str]):
        self.secrets = secrets
        self.files: dict[str, bytes] = {}
        self.meta: dict[str, dict] = {}
        self.skipped: list[dict] = []

    def _free(self, path: str) -> str:
        if path not in self.files:
            return path
        stem, dot, ext = path.rpartition(".")
        if not dot or "/" in ext:
            stem, ext = path, ""
        n = 2
        while f"{stem} ({n}){dot}{ext}" in self.files:
            n += 1
        return f"{stem} ({n}){dot}{ext}"

    def skip(self, kind: str, project: str, path: str, reason: str) -> None:
        self.skipped.append({"type": kind, "project": project, "path": path, "reason": reason})

    def add(self, path: str, data: bytes, kind: str, project: str, *, tool: bool = False,
            **extra) -> str | None:
        """Add one file. Text is masked — tool=True (a file copied from a workspace): the
        vault's values AND the credential patterns; else the vault's values only (people's
        texts, and the JSON written here, whose VALUES were masked before it was dumped: a
        pattern pass over serialised JSON would eat the keys). A binary that holds a vault
        value is left out."""
        if len(data) > MAX_FILE:
            self.skip(kind, project, path, f"larger than {MAX_FILE // (1024 * 1024)} MB")
            return None
        text = _text(data)
        masked = False
        if text is None:
            if _hits(data, self.secrets):
                self.skip(kind, project, path, "binary file holding a vault value")
                return None
        else:
            import agents
            clean = hitl_journal.mask(text, self.secrets) if tool else \
                agents.redact_obj(text, self.secrets)
            masked = clean != text
            data = clean.encode("utf-8")
        path = self._free(path)
        self.files[path] = data
        self.meta[path] = {"path": path, "type": kind, "project": project, "bytes": len(data),
                           "sha256": hashlib.sha256(data).hexdigest(), "masked": masked, **extra}
        return path


# ---- secrets ------------------------------------------------------------------------------
def known_secrets(project_slugs: list[str]) -> dict[str, str]:
    """{label: value} of everything the instance keeps secret and could find in a text: the
    vault of every project (not only the exported ones: a value copied from another team's
    vault must not leave either) and the model keys. Raises ExportUnavailable when the vault
    cannot be read — an export that cannot mask is not made."""
    import vault
    out: dict[str, str] = {}
    try:
        for slug in dict.fromkeys(["default", *project_slugs]):
            for name, val in (vault.session_env(None, project=slug) or {}).items():
                if val:
                    out[name if slug == "default" else f"{slug}:{name}"] = val
    except Exception as e:  # noqa: BLE001 — provider down, wrong key: fail closed
        raise ExportUnavailable(f"the vault cannot be read ({type(e).__name__}): the export "
                                "cannot mask its values, so it is not made") from e
    try:
        import modelkeys
        for k in modelkeys.list_keys():
            ref = modelkeys.ref(k.get("scope") or "instance", k.get("provider") or "")
            val = modelkeys.get_plain(ref)
            if val:
                out[f"MODEL_KEY_{(k.get('provider') or '').upper()}"] = val
    except Exception:  # noqa: BLE001 — no key store on this instance
        pass
    return out


def _hits(data: bytes, secrets: dict[str, str]) -> str | None:
    """Label of the first secret whose value is in `data` (same minimum length as the
    vault's own masking), None when clean."""
    import agents
    for name, val in secrets.items():
        if val and len(val) >= agents._REDACT_MIN and val.encode("utf-8") in data:
            return name
    return None


def _guard(arc: _Archive) -> None:
    for path, data in arc.files.items():
        name = _hits(data, arc.secrets)
        if name:
            raise ExportLeak(f"a vault value ({name.split(':')[-1]}) reached {path}: "
                             "export refused")


# ---- memory -------------------------------------------------------------------------------
def _frontmatter_level(text: str, filename: str) -> int | None:
    try:
        from core import levels, notes
        fm = notes.parse_note(text, filename).fm or {}
        return levels.parse(fm.get("classification"))
    except Exception:  # noqa: BLE001
        return None


def _rebuilt_note(rec) -> str:
    """A note whose file is gone from disk, written back from the store."""
    from core import levels
    desc = " ".join((rec.description or "").split())
    head = ["---", f"name: {rec.name}", "description: >-", f"  {desc}" if desc else "  ",
            f"classification: {levels.ident(rec.level)}", "metadata:",
            f"  type: {rec.type or 'project'}", "---", ""]
    return "\n".join(head) + (rec.body or "").strip() + "\n"


def _export_notes(arc: _Archive, user: dict, project: str) -> None:
    import classification
    import store_backend
    from core import levels
    from core import scope as sc

    own = classification.own_entry(classification.scope_for(user, project), project)
    scope = sc.normalize(own)
    cap = (sc.caps(scope) or {}).get(project)
    if cap is None:
        return
    base = f"memory/{_seg(project)}"
    handed = []
    if store_backend.enabled():
        st = store_backend.get_store()
        for row in st.list_notes(scope):
            if row.get("project") != project:
                continue
            rec = st.get_note(row["name"], project)
            if rec is None or not sc.visible(rec, scope):
                continue
            p = Path(rec.source_path) if rec.source_path else None
            text, rebuilt = None, False
            if p is not None and p.suffix == ".md" and p.is_file() and not p.is_symlink():
                text = _text(p.read_bytes())
            if text is None:
                text, rebuilt = _rebuilt_note(rec), True
            # the file may have been reclassified by hand since it was indexed: the higher wins
            fl = _frontmatter_level(text, p.name if p else f"{rec.name}.md")
            if fl is not None and fl > cap:
                continue
            name = p.name if p is not None and p.suffix == ".md" else f"{_seg(rec.name)}.md"
            arc.add(f"{base}/{_seg(name)}", text.encode("utf-8"), "note", project,
                    note=rec.name, level=levels.ident(rec.level),
                    **({"rebuilt_from_store": True} if rebuilt else {}))
            handed.append(rec)
        # audited recall: an export hands the notes out, like the cockpit does
        classification.log_access("export", handed, actor=user.get("email"))
        return
    # 2.x index (sqlite): it has no project — it is the default project's, at the default level
    import memorykb
    if project != "default" or not sc.allows(scope, "default") or not memorykb.MEM_DB.exists():
        return
    con = sqlite3.connect(f"file:{memorykb.MEM_DB}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT name, source_path FROM notes ORDER BY name").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for name, source in rows:
        p = Path(source or "")
        if p.suffix != ".md" or not p.is_file() or p.is_symlink():
            arc.skip("note", project, f"{base}/{_seg(name)}.md", "its file is no longer on disk")
            continue
        text = _text(p.read_bytes())
        if text is None:
            arc.skip("note", project, f"{base}/{_seg(p.name)}", "not UTF-8 text")
            continue
        fl = _frontmatter_level(text, p.name)
        if fl is not None and fl > cap:
            continue
        arc.add(f"{base}/{_seg(p.name)}", text.encode("utf-8"), "note", project, note=name,
                level=levels.ident(levels.DEFAULT if fl is None else fl))


# ---- tools --------------------------------------------------------------------------------
_AGENT_FIELDS = ("name", "owner", "model", "purpose", "deliverable", "done_criteria", "playbook",
                 "trigger", "schedule", "timezone", "once_at", "event", "tools", "mcp",
                 "auto_approve", "secrets", "budget_usd", "max_minutes", "outputs", "notify_on",
                 "status", "created_by", "approved_by", "approved_at")


def _export_agents(arc: _Archive, pu: dict, project: str) -> None:
    import agents
    import classification
    if not agents.DB.exists():
        return
    for a in agents.list_agents(pu, include_archived=True):
        if (a.get("project") or "default") != project:
            continue
        card = hitl_journal.mask({k: a.get(k) for k in _AGENT_FIELDS if k in a}, arc.secrets)
        arc.add(f"tools/{_seg(project)}/agents/{_seg(a['name'])}.json", _json(card), "agent",
                project, agent=a["name"], status=a.get("status"))
        try:
            runs = agents.list_runs(pu, a["id"], limit=MAX_RUNS)
        except agents.AgentError:
            continue
        for r in runs:
            r = classification.redact_run(r, pu.get("clearance"))
            body = (r.get("deliverable") or "").strip()
            if not body:
                continue
            arc.add(f"deliverables/{_seg(project)}/crew/{_seg(a['name'])}/run-{r['id']}.md",
                    (body + "\n").encode("utf-8"), "agent_deliverable", project,
                    agent=a["name"], run=r["id"], status=r.get("status"),
                    ended_at=_iso(r["ended_at"]) if r.get("ended_at") else None)


def _walk(root: Path):
    """Regular files under `root`, symlinks never followed, bounded."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.is_symlink() or not p.is_file():
                continue
            n += 1
            if n > MAX_TREE_FILES:
                return
            yield p


def _read(arc: _Archive, p: Path, path: str, kind: str, project: str) -> bytes | None:
    """The bytes of a file, None (and a line under `skipped`) when it is too large or gone."""
    try:
        if p.stat().st_size > MAX_FILE:
            arc.skip(kind, project, path, f"larger than {MAX_FILE // (1024 * 1024)} MB")
            return None
        return p.read_bytes()
    except OSError:
        return None


_SECRET_KEY = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|apikey|"
                         r"authorization|credential|private[_-]?key)")


def public_mcp(raw: str, secrets: dict[str, str] | None = None) -> bytes:
    """A `.mcp.json` as it may leave: every `env` / `headers` value replaced (the keys say
    what the server needs), like the value of any key named as a credential; then the
    masking over what remains (a token typed in `args`, a URL with credentials). Not JSON:
    masked as text."""
    try:
        d = json.loads(raw)
    except ValueError:
        return str(hitl_journal.mask(raw, secrets)).encode("utf-8")

    def strip(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in ("env", "headers") and isinstance(v, dict):
                    out[k] = {kk: MASKED for kk in v}
                elif isinstance(v, str) and v and _SECRET_KEY.search(str(k)):
                    out[k] = MASKED
                else:
                    out[k] = strip(v)
            return out
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node
    return _json(hitl_journal.mask(strip(d), secrets))


def _export_workspace(arc: _Archive, project: str) -> None:
    import documents
    root = documents.project_root(project)
    base = f"tools/{_seg(project)}"
    try:
        root_r = root.resolve()
    except OSError:
        return
    if not root_r.is_dir():
        return
    for rel in _INSTRUCTIONS:
        p = root / rel
        if p.is_file() and not p.is_symlink():
            arc.add(f"{base}/instructions/{_seg(rel.replace('/', '_').lstrip('.'))}",
                    p.read_bytes(), "instructions", project, tool=True)
    for rel, dest in _WORKSPACE_TREES:
        d = root / rel
        if not d.is_dir() or d.is_symlink():
            continue
        for p in _walk(d):
            arc_rel = "/".join(_seg(x) for x in p.relative_to(d).parts)
            kind = {"skills": "skill_file", "commands": "command", "subagents": "subagent"}[dest]
            data = _read(arc, p, f"{base}/{dest}/{arc_rel}", kind, project)
            if data is not None:
                arc.add(f"{base}/{dest}/{arc_rel}", data, kind, project, tool=True)
    mcp = root / ".mcp.json"
    if mcp.is_file() and not mcp.is_symlink():
        raw = _text(mcp.read_bytes())
        if raw is not None:
            arc.add(f"{base}/connectors/mcp.json", public_mcp(raw, arc.secrets), "connector",
                    project, declares="MCP servers")


def _export_channels(arc: _Archive, project: str, instance_admin: bool) -> None:
    """Alert delivery channels (Operate › Alerts) from the store's public view."""
    try:
        from alerting import channels
        rows = [c for c in channels.list_channels(project) if not c.get("builtin")]
    except Exception:  # noqa: BLE001 — no alerting store on this instance
        return
    keep = ("name", "kind", "scope", "enabled", "config")
    mine = [{k: c.get(k) for k in keep} for c in rows if c.get("project") == project]
    if mine:
        arc.add(f"tools/{_seg(project)}/connectors/alert-channels.json",
                _json(hitl_journal.mask(mine, arc.secrets)), "connector", project,
                declares="alert channels")
    wide = [{k: c.get(k) for k in keep} for c in rows if c.get("project") == "*"]
    path = f"tools/{INSTANCE}/connectors/alert-channels.json"
    if wide and instance_admin and path not in arc.files:
        arc.add(path, _json(hitl_journal.mask(wide, arc.secrets)), "connector", INSTANCE,
                declares="alert channels")


def _export_sources(arc: _Archive, slugs: list[str]) -> None:
    """Document sources of the memory agent (feature doc_ingest), declared on the host."""
    try:
        from docingest import config as dc
        if not dc.config_path().is_file():
            return
        srcs = [s.public() for s in dc.load() if s.project in slugs]
    except Exception:  # noqa: BLE001 — unreadable declaration: nothing to export
        return
    if srcs:
        arc.add(f"tools/{INSTANCE}/connectors/document-sources.json",
                _json(hitl_journal.mask({"sources": srcs}, arc.secrets)), "connector", INSTANCE,
                declares="document sources")


# ---- deliverables -------------------------------------------------------------------------
def _export_deliverables(arc: _Archive, pu: dict, project: str) -> None:
    import board
    import projectgate
    import simple
    try:
        sessions = [s for s in board.list_sessions() if (s.get("project") or "default") == project]
    except Exception:  # noqa: BLE001
        return
    works = simple.works_by_sid([s["session_id"] for s in sessions]) if simple.DB.exists() else {}
    index = []
    for s in sessions:
        sid = s["session_id"]
        items = simple.list_deliverables(project, sid)
        if not items or projectgate.above_clearance(pu, "session", sid):
            continue
        title = " ".join((s.get("title") or (works.get(sid) or {}).get("ask") or "").split())
        folder = f"deliverables/{_seg(project)}/" + _seg(f"{title[:60]} ({sid[:8]})" if title
                                                          else sid[:8])
        for d in items:
            try:
                p = simple.deliverable_path(project, sid, d["name"])
            except (OSError, ValueError):
                continue
            data = _read(arc, p, f"{folder}/{_seg(d['name'])}", "deliverable", project)
            if data is None:
                continue
            path = arc.add(f"{folder}/{_seg(d['name'])}", data, "deliverable", project,
                           title=d["title"], validated=d["validated"])
            if path is None:
                continue
            index.append({
                "file": path, "title": d["title"], "version": d["version"],
                "work": title[:120],
                "opened_by": s.get("owner") or "", "updated_at": _iso(d["updated_at"]),
                "validated": d["validated"], "validated_by": d["validated_by"],
                "validated_at": _iso(d["validated_at"]) if d["validated_at"] else None,
                "changed_since_validation": d["changed_since_validation"],
                # 3.6.5: which model wrote it — the model the API answered with ('' = not
                # recorded, a save made before 3.6.5) — and how the work was asked for
                "model": d.get("model") or "", "models": d.get("models") or [],
                "written_with": (d.get("engine") or {}).get("label") or "",
                "engine": (works.get(sid) or {}).get("tier") or "local"})
    if index:
        arc.add(f"deliverables/{_seg(project)}/index.json", _json(index), "index", project)


# ---- the archive --------------------------------------------------------------------------
COUNTED = (("notes", "note"), ("agents", "agent"), ("skills", None), ("instructions", "instructions"),
           ("commands", "command"), ("subagents", "subagent"), ("connectors", "connector"),
           ("deliverables", "deliverable"), ("agent_deliverables", "agent_deliverable"))


def _counts(meta: list[dict]) -> dict:
    out = {}
    for label, kind in COUNTED:
        if label == "skills":      # one skill = one folder holding a SKILL.md
            out[label] = sum(1 for m in meta if m["type"] == "skill_file"
                             and m["path"].rsplit("/", 1)[-1] == "SKILL.md")
        else:
            out[label] = sum(1 for m in meta if m["type"] == kind)
    return out


def build(user: dict) -> dict:
    """Build the archive for `user` (a person with their INSTANCE role). Returns
    {"zip": bytes, "manifest": dict, "sha256": str}. Raises ExportLeak / ExportUnavailable."""
    import iam
    import projectgate
    import projects
    from core import levels

    user = {**user, "role": user.get("instance_role") or user.get("role")}
    for k in ("project", "project_role", "clearance", "instance_role"):
        user.pop(k, None)
    instance_admin = iam.rank(user.get("role") or "") >= iam.rank("admin")
    every = [p["slug"] for p in projects.list_projects()]
    arc = _Archive(known_secrets(every))
    exported = []
    for slug in every:
        pu = projectgate.project_user(user, slug)
        if pu is None:
            continue
        before = len(arc.files)
        _export_notes(arc, user, slug)
        _export_agents(arc, pu, slug)
        _export_workspace(arc, slug)
        _export_channels(arc, slug, instance_admin)
        _export_deliverables(arc, pu, slug)
        exported.append({"slug": slug, "name": (projects.get(slug) or {}).get("name") or slug,
                         "role": pu.get("project_role"), "files": len(arc.files) - before,
                         "read_up_to": levels.ident(pu["clearance"]) if pu.get("clearance")
                         is not None else levels.ident(levels.DEFAULT)})
    if instance_admin:
        _export_sources(arc, [p["slug"] for p in exported])
    _guard(arc)

    files = [arc.meta[p] for p in sorted(arc.files)]
    manifest = {
        "format": FORMAT, "sokkan_version": version(), "generated_at": _iso(),
        "generated_by": user.get("email") or "",
        "projects": exported,
        "projects_not_included": len(every) - len(exported),
        "counts": _counts(files), "files_total": len(files),
        "bytes_total": sum(m["bytes"] for m in files),
        "masked_files": sum(1 for m in files if m["masked"]),
        "skipped": arc.skipped,
        "hash": "sha256 of each file as stored in this archive (after masking)",
        "files": files,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", readme(manifest))
        z.writestr("manifest.json", _json(manifest))
        for path in sorted(arc.files):
            z.writestr(path, arc.files[path])
    data = buf.getvalue()
    return {"zip": data, "manifest": manifest, "sha256": hashlib.sha256(data).hexdigest()}


def filename(manifest: dict) -> str:
    return f"sokkan-work-export-{manifest['generated_at'][:10]}.zip"


# ---- README -------------------------------------------------------------------------------
def readme(manifest: dict) -> str:
    c = manifest["counts"]
    prj = ", ".join(f"`{p['slug']}`" for p in manifest["projects"]) or "—"
    head = (f"SOKKAN {manifest['sokkan_version']} · {manifest['generated_at']} · "
            f"{manifest['files_total']} files")
    return f"""# Your work, out of SOKKAN

{head}

This archive holds what your organisation built in SOKKAN, in plain files. Nothing in it
needs SOKKAN to be read. *(Version française plus bas.)*

Projects in this archive: {prj}

| Folder | What it is | Count |
|---|---|---|
| `memory/<project>/` | The notes of the project memory | {c['notes']} |
| `tools/<project>/agents/` | The Crew agents: one JSON job description each | {c['agents']} |
| `tools/<project>/instructions/` | The project instructions (`CLAUDE.md`) | {c['instructions']} |
| `tools/<project>/skills/` | Agent skills found in the project workspace | {c['skills']} |
| `tools/<project>/commands/`, `subagents/` | Slash commands and sub-agents of the workspace | {c['commands'] + c['subagents']} |
| `tools/…/connectors/` | Declared MCP servers, alert channels, document sources | {c['connectors']} |
| `deliverables/<project>/` | Deliverables of the simple profile, one folder per piece of work | {c['deliverables']} |
| `deliverables/<project>/crew/` | What the agents' runs delivered | {c['agent_deliverables']} |

## How to reuse it

- **Notes** are Markdown files with a YAML frontmatter (`name`, `description`, sometimes
  `classification`, `priority`, `metadata`). Links between notes are written `[[note-name]]`.
  Any Markdown editor or note tool that reads a folder of `.md` files opens them; an AI
  assistant can be given the folder as its knowledge.
- **Agents** are JSON files. `purpose`, `deliverable` and `done_criteria` are the
  instructions the agent was given, in plain text: they can be pasted as the brief of an
  agent in another tool. `trigger` / `schedule` (cron) / `timezone` say when it ran, `tools`
  and `mcp` what it was allowed to use, `secrets` the NAMES of the secrets it received
  (their values are not in this archive). The JSON layout is SOKKAN's own.
- **Instructions** (`CLAUDE.md`) are plain Markdown: the standing instructions every
  session of the project started with.
- **Skills, commands and sub-agents** are copied as they were in the project workspace
  (`.claude/skills`, `.claude/commands`, `.claude/agents`), in the file layout Claude Code
  reads: a skill is a folder with a `SKILL.md`. SOKKAN has no skill editor of its own: this
  folder is empty when nobody added such files to the workspace.
- **Connectors** are declarations only: which MCP servers, alert channels and document
  sources were set up. Every credential is replaced by `{MASKED}` or `[secret:NAME]`; you
  will have to enter your own again wherever you reuse them.
- **Deliverables** are the files as they were saved (Markdown, text, CSV, HTML or JSON).
  `deliverables/<project>/index.json` gives, for each one, its title, the piece of work it
  belongs to, who validated it and when, and which model wrote it (`model`: the model the
  API answered with; `engine`: `claude` when a person chose « Write with Claude »).

## What is not in this archive

- No secret: the vault, the model keys and every credential stay on the instance. A vault
  value found inside a note, a deliverable or a tool file is replaced by `[secret:NAME]`,
  a credential-looking string in a workspace tool file by `[masked]`; `manifest.json` marks
  those files `"masked": true`. A note, deliverable, instruction, skill, command or
  sub-agent file that is not marked is byte for byte what was saved.
- Only what the person who made the export may read: projects they have a role in, notes
  up to their clearance.
- Not the conversations, the documents people uploaded, the accounts or the journals. The
  instance's own backup holds everything; the journal of human validations has its own
  export.
- `manifest.json` lists every file with its size and SHA-256, and under `skipped` anything
  that was left out with the reason.

---

# Votre travail, hors de SOKKAN

Cette archive contient ce que votre organisation a construit dans SOKKAN, en fichiers
ordinaires. Rien n'y demande SOKKAN pour être lu.

Projets dans cette archive : {prj}

| Dossier | Contenu | Nombre |
|---|---|---|
| `memory/<projet>/` | Les notes de la mémoire du projet | {c['notes']} |
| `tools/<projet>/agents/` | Les agents (Crew) : une fiche JSON par agent | {c['agents']} |
| `tools/<projet>/instructions/` | Les instructions du projet (`CLAUDE.md`) | {c['instructions']} |
| `tools/<projet>/skills/` | Les skills d'agent trouvés dans l'espace de travail du projet | {c['skills']} |
| `tools/<projet>/commands/`, `subagents/` | Commandes et sous-agents de l'espace de travail | {c['commands'] + c['subagents']} |
| `tools/…/connectors/` | Serveurs MCP, canaux d'alerte et sources de documents déclarés | {c['connectors']} |
| `deliverables/<projet>/` | Les livrables du profil simple, un dossier par travail | {c['deliverables']} |
| `deliverables/<projet>/crew/` | Ce que les exécutions des agents ont livré | {c['agent_deliverables']} |

## Comment le réutiliser

- **Les notes** sont des fichiers Markdown avec un en-tête YAML (`name`, `description`,
  parfois `classification`, `priority`, `metadata`). Les liens entre notes s'écrivent
  `[[nom-de-note]]`. Tout éditeur Markdown ou outil de notes qui lit un dossier de `.md`
  les ouvre ; un assistant IA peut recevoir le dossier comme base de connaissances.
- **Les agents** sont des fichiers JSON. `purpose`, `deliverable` et `done_criteria` sont
  les consignes données à l'agent, en texte : elles se collent telles quelles comme brief
  d'un agent dans un autre outil. `trigger` / `schedule` (cron) / `timezone` disent quand
  il tournait, `tools` et `mcp` ce qu'il avait le droit d'utiliser, `secrets` les NOMS des
  secrets qu'il recevait (leurs valeurs ne sont pas dans l'archive). La structure du JSON
  est propre à SOKKAN.
- **Les instructions** (`CLAUDE.md`) sont du Markdown : les consignes permanentes avec
  lesquelles chaque session du projet démarrait.
- **Les skills, commandes et sous-agents** sont copiés tels qu'ils étaient dans l'espace
  de travail du projet (`.claude/skills`, `.claude/commands`, `.claude/agents`), dans la
  disposition de fichiers que lit Claude Code : un skill est un dossier contenant un
  `SKILL.md`. SOKKAN n'a pas d'éditeur de skills : ce dossier est vide si personne n'a
  ajouté de tels fichiers à l'espace de travail.
- **Les connecteurs** ne sont que des déclarations : quels serveurs MCP, canaux d'alerte
  et sources de documents étaient configurés. Chaque identifiant est remplacé par
  `{MASKED}` ou `[secret:NOM]` ; il faudra saisir les vôtres là où vous les réutilisez.
- **Les livrables** sont les fichiers tels qu'enregistrés (Markdown, texte, CSV, HTML ou
  JSON). `deliverables/<projet>/index.json` donne pour chacun son titre, le travail
  auquel il appartient, qui l'a validé et quand, et quel modèle l'a rédigé (`model` : le
  modèle qui a répondu ; `engine` : `claude` quand la personne a choisi « Rédiger avec Claude »).

## Ce que l'archive ne contient pas

- Aucun secret : le coffre, les clés de modèles et tout identifiant restent sur l'instance.
  Une valeur du coffre trouvée dans une note, un livrable ou un fichier d'outil est
  remplacée par `[secret:NOM]`, une chaîne qui ressemble à un identifiant dans un fichier
  d'outil de l'espace de travail par `[masked]` ; `manifest.json` marque ces fichiers
  `"masked": true`. Un fichier de note, de livrable, d'instructions, de skill, de commande
  ou de sous-agent qui n'est pas marqué est, à l'octet près, ce qui a été enregistré.
- Seulement ce que la personne qui a fait l'export a le droit de lire : les projets où
  elle a un rôle, les notes jusqu'à son niveau d'habilitation.
- Ni les conversations, ni les documents déposés, ni les comptes, ni les journaux. La
  sauvegarde de l'instance contient tout ; le journal des validations humaines a son
  propre export.
- `manifest.json` liste chaque fichier avec sa taille et son empreinte SHA-256 et, sous
  `skipped`, ce qui a été laissé de côté et pourquoi.
"""
