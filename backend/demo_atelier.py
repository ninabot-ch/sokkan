#!/usr/bin/env python3
"""demo_atelier.py — SOKKAN Atelier on the PUBLIC READ-ONLY DEMO (3.6.7). Never anywhere else.

The public demo showed the Cockpit to an anonymous visitor (a viewer). This module lets the
same visitor *try* SOKKAN Atelier — the simple interface — on finished, fictional work:

1. Read-only, enforced server-side when the feature is on (`demo_atelier`, requires
   `demo_banner` + `simple_profile` + `multi_project`): every non-read request under /api
   from someone below instance admin answers 403 « read-only demo » (Nina excepted — the
   same guard as the Captains demo, `demo_captains.write_refusal`). The visitor's language,
   theme and interface (Atelier or Cockpit) stay in their browser: the demo has ONE identity,
   shared by every visitor, so nothing of theirs is saved on the server.

2. `cta()` — the call to action of the demo band (book a demo, install), from the operator's
   environment only (SOKKAN_DEMO_BOOK_URL, SOKKAN_DEMO_INSTALL_URL; http(s) only, empty =
   the button is not shown). Nothing is hard-coded.

3. `seed [<atelier.json>]` — 2-3 fictional projects, each with memory notes, documents and
   works already done: the thread (what the person asked, what the agent read in the memory
   and the documents, what it saved), the deliverables in their versions, and the human
   validation of each (who — a fictional @example.com address — and when). No model is
   called: the thread is written as the transcript a session leaves behind, and the Atelier
   replays it as it replays any finished work. Idempotent: what a previous seed wrote is
   remembered (table `demo_atelier` in simple.db) and rewritten; a project of the same name
   that the seed did not create is refused. Default content: backend/demo_atelier.json.

Spec: docs/enterprise/SIMPLE-PROFILE.md § Public demo.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_MEMORY = Path(__file__).resolve().parent.parent / "memory"   # store_backend, as app.py does
if _MEMORY.is_dir() and str(_MEMORY) not in sys.path:
    sys.path.insert(0, str(_MEMORY))

SEED_BY = "demo-atelier-seed"
CONTENT = Path(__file__).with_name("demo_atelier.json")
DEFAULT_MODEL = "sokkan-ship"
_NOTE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
_STEP_KINDS = ("memory_search", "memory_get", "read", "say", "write")


def requested() -> bool:
    import features
    return bool(features.requested("demo_atelier"))


def enabled() -> bool:
    """Asked for (SOKKAN_DEMO_ATELIER=1) AND on the demo (banner) AND Atelier + projects on."""
    try:
        import features
        return features.enabled("demo_atelier")
    except Exception:  # noqa: BLE001 — registry unavailable: not the demo
        return False


def visitor() -> str:
    import demo_captains
    return demo_captains.visitor()


# ---- the write guard (called by app.require_auth) -------------------------------------
def guard(method: str, path: str, user: dict) -> str | None:
    """None = let it through; otherwise the 403 detail (« read-only demo »)."""
    import demo_captains
    ro = demo_captains.write_refusal(method, path, user)
    return ro if ro and enabled() else None


# ---- the call to action ---------------------------------------------------------------
def _url(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    return v if re.match(r"^https?://[^\s\"'<>]+$", v) and len(v) <= 500 else ""


def cta() -> dict:
    """The two buttons of the demo band; an empty URL = the button is not shown."""
    return {"book_url": _url("SOKKAN_DEMO_BOOK_URL"), "install_url": _url("SOKKAN_DEMO_INSTALL_URL")}


def project_slugs() -> list[str]:
    """The projects the seed made, in the visitor's picker of the Atelier (the instance's own
    projects — the Cockpit demo's — have no work of the Atelier to show)."""
    try:
        import projects
        return [p["slug"] for p in projects.list_projects() if p.get("created_by") == SEED_BY]
    except Exception:  # noqa: BLE001 — no project store: nothing to list
        return []


# ---- seed: validation -----------------------------------------------------------------
class SeedError(ValueError):
    pass


def _fictional(email: str) -> bool:
    import demo_captains
    return demo_captains.fictional(email)


def check(spec: dict) -> dict:
    """Validate a seed file without writing anything."""
    import projects
    prj = spec.get("projects") or []
    if not 2 <= len(prj) <= 3:
        raise SeedError("seed: 2 or 3 projects")
    works = deliverables = 0
    slugs = set()
    for p in prj:
        slug = p.get("slug") or ""
        if not projects.valid_slug(slug) or slug in (projects.DEFAULT_PROJECT, projects.SHARED_PROJECT):
            raise SeedError(f"project slug {slug!r}")
        if slug in slugs:
            raise SeedError(f"project {slug} twice")
        slugs.add(slug)
        if p.get("lang") not in ("fr", "en"):
            raise SeedError(f"{slug}: lang fr | en")
        notes = {}
        for n in p.get("notes") or []:
            if not _NOTE_NAME.fullmatch(n.get("name") or ""):
                raise SeedError(f"{slug}: note name {n.get('name')!r} (lowercase slug)")
            if not (n.get("body") or "").strip() or not (n.get("description") or "").strip():
                raise SeedError(f"{slug}: note {n['name']} needs a description and a body")
            notes[n["name"]] = n
        docs = {}
        for d in p.get("documents") or []:
            if not _KEY.fullmatch(d.get("key") or "") or not re.fullmatch(r"[^/\\]{1,120}\.(md|txt)", d.get("name") or ""):
                raise SeedError(f"{slug}: document {d.get('key')!r} / {d.get('name')!r} (a .md or .txt name)")
            if not (d.get("text") or "").strip():
                raise SeedError(f"{slug}: document {d['key']} is empty")
            docs[d["key"]] = d
        keys = set()
        for w in p.get("works") or []:
            k = w.get("key") or ""
            if not _KEY.fullmatch(k) or k in keys:
                raise SeedError(f"{slug}: work key {k!r}")
            keys.add(k)
            if not (w.get("title") or "").strip() or not (w.get("ask") or "").strip():
                raise SeedError(f"{slug}/{k}: a title and an ask")
            dl = w.get("deliverable") or {}
            fname = dl.get("file") or ""
            if not re.fullmatch(r"[^/\\.][^/\\]{0,110}\.md", fname):
                raise SeedError(f"{slug}/{k}: deliverable file {fname!r} (a .md name)")
            versions = dl.get("versions") or {}
            if not versions or not all((v or "").lstrip().startswith("# ") for v in versions.values()):
                raise SeedError(f"{slug}/{k}: each version starts with a `# Title` line")
            for d in w.get("documents") or []:
                if d not in docs:
                    raise SeedError(f"{slug}/{k}: document {d!r} is not in the project")
            turns = w.get("turns") or []
            if not turns:
                raise SeedError(f"{slug}/{k}: at least one turn")
            written = []
            for i, t in enumerate(turns):
                if i and not (t.get("user") or "").strip():
                    raise SeedError(f"{slug}/{k}: turn {i + 1} needs what the person wrote")
                for s in t.get("steps") or []:
                    kind = next((x for x in _STEP_KINDS if x in s), None)
                    if kind is None:
                        raise SeedError(f"{slug}/{k}: step {s!r}")
                    if kind == "memory_get" and s["memory_get"] not in notes:
                        raise SeedError(f"{slug}/{k}: note {s['memory_get']!r} is not in the project")
                    if kind == "memory_search" and any(x not in notes for x in s.get("found") or []):
                        raise SeedError(f"{slug}/{k}: memory_search finds a note not in the project")
                    if kind == "read" and s["read"] not in docs:
                        raise SeedError(f"{slug}/{k}: document {s['read']!r} is not in the project")
                    if kind == "write":
                        if s["write"] not in versions:
                            raise SeedError(f"{slug}/{k}: version {s['write']!r} is not in deliverable.versions")
                        written.append(s["write"])
            if not written:
                raise SeedError(f"{slug}/{k}: the work never saves its deliverable")
            v = w.get("validated_by")
            if v and not _fictional(v):
                raise SeedError(f"{slug}/{k}: validated_by must be fictional (@example.com)")
            works += 1
            deliverables += 1
    return {"projects": len(prj), "works": works, "deliverables": deliverables,
            "notes": sum(len(p.get("notes") or []) for p in prj),
            "documents": sum(len(p.get("documents") or []) for p in prj)}


# ---- seed: memory of what was written -------------------------------------------------
_SCHEMA = """CREATE TABLE IF NOT EXISTS demo_atelier (
    kind TEXT NOT NULL, ref TEXT NOT NULL, project TEXT NOT NULL, PRIMARY KEY (kind, ref))"""


def _scon() -> sqlite3.Connection:
    import simple
    con = simple._con()
    con.execute(_SCHEMA)
    return con


def _remember(kind: str, ref: str, project: str) -> None:
    con = _scon()
    con.execute("INSERT OR REPLACE INTO demo_atelier(kind, ref, project) VALUES(?,?,?)", (kind, ref, project))
    con.commit()
    con.close()


def _transcript_file(csid: str) -> Path:
    import agentchat
    return agentchat._transcript_path(csid)


def _forget() -> dict:
    """Remove what a previous seed wrote: works (session, thread, deliverables), documents,
    note files. Projects and grants are rewritten by `seed` itself."""
    import board
    import documents
    import simple
    con = _scon()
    rows = [dict(r) for r in con.execute("SELECT kind, ref, project FROM demo_atelier")]
    con.close()
    n = {"works": 0, "documents": 0, "notes": 0}
    for r in rows:
        if r["kind"] == "session":
            sid = r["ref"]
            csid = board.get_claude_session_id(sid)
            if csid:
                _transcript_file(csid).unlink(missing_ok=True)
            try:
                shutil.rmtree(simple.deliverable_dir(r["project"], sid), ignore_errors=True)
            except ValueError:
                pass
            board.close_session(sid)
            simple.forget(sid)
            n["works"] += 1
        elif r["kind"] == "document":
            documents.delete(r["ref"])
            n["documents"] += 1
        elif r["kind"] == "note":
            Path(r["ref"]).unlink(missing_ok=True)
            n["notes"] += 1
    con = _scon()
    con.execute("DELETE FROM demo_atelier")
    con.commit()
    con.close()
    return n


# ---- seed: writing --------------------------------------------------------------------
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sid(slug: str, key: str) -> str:
    return hashlib.sha256(f"{SEED_BY}\0{slug}\0{key}".encode()).hexdigest()[:32]


def _note_file(n: dict) -> str:
    desc = " ".join((n["description"] or "").split())
    return (f"---\nname: {n['name']}\ndescription: >-\n  {desc}\ntype: {n.get('type', 'project')}\n---\n\n"
            + n["body"].strip() + "\n")


def _add_document(slug: str, d: dict, owner: str, ts: float) -> dict:
    import documents
    inc = documents.incoming_path(slug)
    inc.mkdir(parents=True, exist_ok=True)
    tmp = inc / f"seed-{uuid.uuid4().hex[:12]}"
    tmp.write_text(d["text"].strip() + "\n", encoding="utf-8")
    doc = documents.add_file(slug, d["name"], tmp, owner=owner, modified_at=ts)
    con = documents._con()
    con.execute("UPDATE documents SET created_at=? WHERE id=?", (ts, doc["id"]))
    con.commit()
    con.close()
    _remember("document", doc["id"], slug)
    return documents.get(doc["id"]) or doc


class _Transcript:
    """The lines a Claude Code session leaves in its transcript, for one work."""

    def __init__(self, csid: str, cwd: str, model: str, start: float):
        self.csid, self.cwd, self.model, self.t = csid, cwd, model, start
        self.lines: list[dict] = []
        self.parent: str | None = None
        self.n = 0

    def _rec(self, typ: str, message: dict, step: float = 7.0) -> None:
        self.t += step
        u = str(uuid.UUID(hashlib.md5(f"{self.csid}:{len(self.lines)}".encode()).hexdigest()))
        self.lines.append({"parentUuid": self.parent, "isSidechain": False, "userType": "external",
                           "cwd": self.cwd, "sessionId": self.csid, "version": "2.1.322",
                           "type": typ, "message": message, "uuid": u, "timestamp": _iso(self.t)})
        self.parent = u

    def user(self, text: str) -> None:
        self._rec("user", {"role": "user", "content": text}, step=1.0)

    def say(self, text: str) -> None:
        self._rec("assistant", {"role": "assistant", "model": self.model, "type": "message",
                                "content": [{"type": "text", "text": text}]})

    def tool(self, name: str, inp: dict, result: str, step: float = 5.0) -> str:
        self.n += 1
        tid = "toolu_demo_" + hashlib.sha256(f"{self.csid}:{self.n}".encode()).hexdigest()[:20]
        self._rec("assistant", {"role": "assistant", "model": self.model, "type": "message",
                                "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}, step)
        self._rec("user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result}]}, step=1.5)
        return tid

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.lines),
                        encoding="utf-8")


_BRIEF = {
    "fr": ("<sokkan-brief>\nVous aidez une personne qui n'est pas développeuse. (Consigne interne de "
           "SOKKAN Atelier : jamais affichée à la personne.)\n</sokkan-brief>"),
    "en": ("<sokkan-brief>\nYou are helping a person who is NOT a developer. (SOKKAN Atelier's "
           "internal brief: never shown to the person.)\n</sokkan-brief>"),
}


def _work(slug: str, lang: str, w: dict, notes: dict, docs: dict, model: str, me: str,
          now: float) -> dict:
    import board
    import documents
    import simple
    sid = _sid(slug, w["key"])
    csid = str(uuid.UUID(hashlib.md5(f"{SEED_BY}:{sid}".encode()).hexdigest()))
    start = now - float(w.get("ago_days", 3)) * 86400
    deliv = simple.deliverable_dir(slug, sid)
    deliv.mkdir(parents=True, exist_ok=True)
    board.add_sdk_session(sid, "docs", title=w["title"], prompt=w["ask"], project=slug, owner=me)
    board.set_claude_session_id(sid, csid)
    simple.add_work(sid, slug, me, w["ask"], w.get("playbook", ""), lang)
    _remember("session", sid, slug)
    for k in w.get("documents") or []:
        documents.attach(sid, docs[k]["id"], by=me)

    tr = _Transcript(csid, str(documents.project_root(slug)), model, start)
    dl = w["deliverable"]
    target = deliv / dl["file"]
    writes: list[str] = []
    last_text = ""
    for i, turn in enumerate(w["turns"]):
        if i == 0:
            tr.user(_BRIEF[lang] + "\n\n" + w["ask"].strip() + "\n\n" + _BRIEF[lang])
        else:
            tr.t += float(turn.get("after_hours", 2)) * 3600
            tr.user(turn["user"].strip())
        for s in turn.get("steps") or []:
            if "memory_search" in s:
                found = s.get("found") or []
                res = "\n".join(f"- {x} — {notes[x]['description']}" for x in found) or "(no match)"
                tr.tool("mcp__sokkan-memory__memory_search", {"query": s["memory_search"]}, res)
            elif "memory_get" in s:
                n = notes[s["memory_get"]]
                tr.tool("mcp__sokkan-memory__memory_get", {"note_name": n["name"]}, n["body"].strip())
            elif "read" in s:
                d = docs[s["read"]]
                tr.tool("Read", {"file_path": str(documents.path_of(d))}, d["text"].strip(), step=4.0)
            elif "say" in s:
                tr.say(s["say"].strip())
                last_text = s["say"].strip()
            elif "write" in s:
                body = dl["versions"][s["write"]].strip() + "\n"
                existed = target.exists()
                target.write_text(body, encoding="utf-8")
                said = (f"The file {target} has been updated successfully." if existed
                        else f"File created successfully at: {target}")
                tid = tr.tool("Write", {"file_path": str(target), "content": body}, said, step=40.0)
                writes.append(tid)
    tr.write(_transcript_file(csid))
    for tid in writes:
        simple.note_write(sid, dl["file"], tid, model)
    end = tr.t
    os.utime(target, (end, end))
    con = simple._con()
    con.execute("UPDATE works SET created_at=?, last_line=?, last_at=? WHERE sid=?",
                (start, _last_line(last_text, lang), end, sid))
    if w.get("validated_by"):
        vat = end + float(w.get("validated_after_hours", 3)) * 3600
        con.execute("INSERT OR REPLACE INTO validations(sid, name, sha256, by, at) VALUES(?,?,?,?,?)",
                    (sid, dl["file"], simple._sha(target), w["validated_by"].lower(), min(vat, now - 600)))
    con.commit()
    con.close()
    bcon = board._con()
    bcon.execute("UPDATE sessions SET created_at=? WHERE session_id=?", (start, sid))
    bcon.commit()
    bcon.close()
    return {"sid": sid, "title": w["title"], "versions": len(writes),
            "validated": bool(w.get("validated_by"))}


def _last_line(text: str, lang: str) -> str:
    import plain
    return plain.last_line([{"type": "text", "text": text}], lang=lang) if text else ""


def seed(spec: dict, now: float | None = None) -> dict:
    import iam
    import projects
    import store_backend
    import uiprefs
    now = time.time() if now is None else now
    check(spec)
    me = visitor()
    model = (spec.get("model") or os.environ.get("SOKKAN_SHIP_ALIAS") or DEFAULT_MODEL).strip()
    removed = _forget()
    report: dict = {"visitor": me, "removed": removed, "projects": []}
    for p in spec["projects"]:
        slug = p["slug"]
        cur = projects.get(slug)
        if cur is None:
            projects.create(slug, p.get("name") or slug, access_source="sso_group",
                            created_by=SEED_BY, description=p.get("description", ""))
        elif cur["created_by"] != SEED_BY:
            raise SeedError(f"project {slug} exists and was not made by the Atelier demo seed — refused")
        else:
            projects.update(slug, name=p.get("name") or slug, description=p.get("description", ""))
        for g in projects.list_grants(slug):          # grants = exactly the seed's
            projects.revoke(slug, g["principal_kind"], g["principal"])
        projects.grant(slug, "user", me, "viewer", created_by=SEED_BY)
        # memory: one file per note in the project's memory directory (indexed by the
        # project indexer of the api within a minute — CORTHEXIS_WATCH_S)
        mdir = store_backend.memory_dir_for(slug)
        mdir.mkdir(parents=True, exist_ok=True)
        notes = {}
        for n in p.get("notes") or []:
            f = mdir / f"{n['name'].replace('-', '_')}.md"
            f.write_text(_note_file(n), encoding="utf-8")
            ts = now - float(n.get("ago_days", 30)) * 86400
            os.utime(f, (ts, ts))
            _remember("note", str(f), slug)
            notes[n["name"]] = n
        docs = {}
        for d in p.get("documents") or []:
            doc = _add_document(slug, d, me, now - float(d.get("ago_days", 10)) * 86400)
            docs[d["key"]] = {**doc, "text": d["text"]}
        made = [_work(slug, p["lang"], w, notes, docs, model, me, now) for w in p.get("works") or []]
        report["projects"].append({"slug": slug, "lang": p["lang"], "notes": len(notes),
                                   "documents": len(docs), "works": made})
    first = spec.get("visitor_project") or spec["projects"][0]["slug"]
    uiprefs.set_prefs(me, project=first)
    role = (iam.get_user(me) or {}).get("role")
    if role != "viewer":
        report["warning"] = (f"{me} is {role!r} on this instance, not a viewer: the demo is read-only "
                             "only for a viewer (SOKKAN_DEFAULT_ROLE=viewer and the visitor's role)")
    _chown_back()
    return report


def _chown_back() -> None:
    """Run as root (`docker compose exec` without -u): give the files back to the app (uid
    1000), or it can no longer write its own databases."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    data = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    for base in (data, claude):
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            for x in [root, *(os.path.join(root, f) for f in files)]:
                try:
                    os.lchown(x, 1000, 1000)
                except OSError:
                    pass


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("seed", "check"):
        print("usage: demo_atelier.py check|seed [<atelier.json>] [--force]", file=sys.stderr)
        return 2
    args = [a for a in argv[1:] if not a.startswith("--")]
    spec = json.loads((Path(args[0]) if args else CONTENT).read_text(encoding="utf-8"))
    if argv[0] == "check":
        print(json.dumps(check(spec)))
        return 0
    if "--force" not in argv and not requested():
        print("refused: SOKKAN_DEMO_ATELIER is not set — this seeds FICTIONAL work, meant for the "
              "public demo only (--force to seed anyway)", file=sys.stderr)
        return 1
    print(json.dumps(seed(spec), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
