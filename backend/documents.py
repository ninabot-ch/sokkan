#!/usr/bin/env python3
"""documents.py — document drop (feature `documents`): the files a person hands to a project
or to one session, kept where that session's agent can read them.

What is accepted, and how it is checked (every refusal is logged, `document.refused`):

* **type** — the extension must be in `ALLOWED` AND the content must be what the extension
  says: `%PDF-` for a PDF, a real Office package (zip with `[Content_Types].xml` and the
  `word/`, `ppt/` or `xl/` part, no macro project, a bounded uncompressed size) for
  docx / pptx / xlsx, text without NUL byte nor executable / archive signature for txt / md.
  A renamed executable, a zip called .docx, a PNG called .pdf are refused;
* **size** — `SOKKAN_DOCUMENTS_MAX_MB` (25) per file, enforced while the body streams in
  (the declared length is only a first, early refusal); `SOKKAN_DOCUMENTS_MAX_PER_PROJECT`
  (500) documents per project;
* **name** — what the browser sent is a label, never a path: directory parts dropped,
  control characters and reserved characters removed, no leading dot, bounded length. The
  file is stored under an id SOKKAN generates: ``<project space>/documents/<id>/<name>``;
* **place** — the project's own space (`agentchat.project_cwd`: the workspace for the
  default project, ``$SOKKAN_DATA_DIR/projects/<slug>/work`` otherwise), which is what the
  project sandbox lets a session read. A text copy ``<name>.txt`` is written next to an
  Office / PDF file (`doctext`);
* **who** — the caller's role IN THE DOCUMENT'S PROJECT decides (dev to add, viewer to
  read, the person who added it or a project admin to remove); another project's document
  does not exist (404). With `classification` on, a document carries a level: it is
  listed and served only at or under the reader's clearance, and one above the default
  level cannot be attached to an agent session (its text would enter a transcript that
  lower clearances can read — refused until transcripts carry the level).

Store: ``$SOKKAN_DATA_DIR/documents.db`` (documents, session_documents). This module has no
HTTP in it (routes: `documents_api.py`).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path

import doctext
from features import env_num

ALLOWED = ("pdf", "docx", "pptx", "xlsx", "txt", "md")
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}
_OFFICE_PART = {"docx": "word/", "pptx": "ppt/", "xlsx": "xl/"}
_BINARY_MAGIC = (b"MZ", b"\x7fELF", b"PK\x03\x04", b"%PDF-", b"\x89PNG", b"\xff\xd8\xff",
                 b"GIF8", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"Rar!", b"7z\xbc\xaf",
                 b"\x1f\x8b", b"#!")
MAX_NAME = 120
MAX_UNZIPPED = 300 * 1024 * 1024
MAX_MEMBERS = 5000
DIRNAME = "documents"

DB = Path(os.environ.get("SOKKAN_DOCUMENTS_DB") or os.path.join(
    os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
    "documents.db"))


class DocumentError(Exception):
    """A refusal with its HTTP status and a reason code the UI translates."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def enabled() -> bool:
    import features
    return features.enabled("documents")


def max_bytes() -> int:
    return max(1, int(env_num("SOKKAN_DOCUMENTS_MAX_MB", 25))) * 1024 * 1024


def max_per_project() -> int:
    return max(1, int(env_num("SOKKAN_DOCUMENTS_MAX_PER_PROJECT", 500)))


def limits() -> dict:
    return {"max_bytes": max_bytes(), "extensions": list(ALLOWED),
            "max_per_project": max_per_project()}


# ---- store ---------------------------------------------------------------------------------
def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',
            level INTEGER NOT NULL DEFAULT 2, created_at REAL NOT NULL,
            modified_at REAL, text_state TEXT NOT NULL DEFAULT '', text_chars INTEGER NOT NULL DEFAULT 0,
            deleted_at REAL
        )
        """)
    con.execute("CREATE INDEX IF NOT EXISTS ix_documents_project ON documents(project, created_at)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS session_documents (
            session_id TEXT NOT NULL, doc_id TEXT NOT NULL, at REAL NOT NULL, by TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id, doc_id)
        )
        """)
    return con


def _default_level() -> int:
    try:
        import classification
        return int(classification._lv().DEFAULT)
    except Exception:  # noqa: BLE001
        return 2


# ---- where ---------------------------------------------------------------------------------
def project_root(project: str) -> Path:
    """The project's own space — the directory its sessions work in (and may read)."""
    if (project or "default") == "default":
        import agentchat
        return Path(agentchat.CWD)
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
                ) / "projects" / project / "work"


def docs_dir(project: str) -> Path:
    return project_root(project) / DIRNAME


def path_of(doc: dict) -> Path:
    """Stored file of a document. The id and the name come from the store, and the result
    is checked to be inside the project's documents directory all the same."""
    base = docs_dir(doc["project"]).resolve()
    p = (base / doc["id"] / doc["name"]).resolve()
    if base not in p.parents:
        raise DocumentError(404, "not_found", "not found")
    return p


def text_path_of(doc: dict) -> Path | None:
    """What the agent reads: the file itself for txt / md, its text copy otherwise (None
    when no text could be extracted)."""
    p = path_of(doc)
    if doc["ext"] in ("txt", "md"):
        return p
    t = p.with_name(p.name + ".txt")
    return t if t.is_file() else None


# ---- name ----------------------------------------------------------------------------------
_RESERVED = re.compile(r'[<>:"|?*\\/\x00-\x1f\x7f]')
_WIN_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}


def safe_name(raw: str) -> tuple[str, str]:
    """→ (name, ext) from what the browser sent. Raises when the type is not accepted.
    The name is a label: it never decides where the file goes."""
    s = unicodedata.normalize("NFC", str(raw or ""))
    s = s.replace("\\", "/").rsplit("/", 1)[-1]           # directory parts: dropped
    s = re.sub(r"\s", " ", s)                              # tabs, line breaks: a space
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")  # control / format
    s = _RESERVED.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(". ").rstrip(". ")
    stem, dot, ext = s.rpartition(".")
    ext = ext.lower() if dot else ""
    if not dot or ext not in ALLOWED:
        shown = (ext or s or "?")[:20]
        raise DocumentError(415, "type", f"type not accepted (.{shown}): "
                                         f"{', '.join(ALLOWED)} only")
    stem = stem.strip().lstrip(". ").rstrip(". ") or "document"
    if stem.lower() in _WIN_DEVICES:
        stem = f"{stem}-document"
    stem = stem[: MAX_NAME - len(ext) - 1].rstrip(". ")
    return f"{stem}.{ext}", ext


# ---- content -------------------------------------------------------------------------------
def check_content(path: Path, ext: str) -> None:
    """The content must be what the extension says. Raises DocumentError(415, "disguised")."""
    def bad(why: str):
        raise DocumentError(415, "disguised", f"the content is not a .{ext} file ({why})")

    with open(path, "rb") as f:
        head = f.read(8192)
    if not head:
        raise DocumentError(400, "empty", "empty file")
    if ext == "pdf":
        if not head.startswith(b"%PDF-"):
            bad("no PDF header")
        return
    if ext in _OFFICE_PART:
        if not head.startswith(b"PK\x03\x04") or not zipfile.is_zipfile(path):
            bad("not an Office package")
        try:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                names = {i.filename for i in infos}
                if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNZIPPED:
                    raise DocumentError(413, "too_large", "the package expands beyond the limit")
                if any(i.flag_bits & 0x1 for i in infos):
                    bad("encrypted package")
                if "[Content_Types].xml" not in names or not any(
                        n.startswith(_OFFICE_PART[ext]) for n in names):
                    bad(f"no {_OFFICE_PART[ext]} part")
                if any(n.lower().endswith("vbaproject.bin") for n in names):
                    bad("macros are not accepted")
        except zipfile.BadZipFile:
            bad("damaged package")
        return
    # txt / md: text, not something else wearing the extension
    if any(head.startswith(m) for m in _BINARY_MAGIC):
        bad("binary signature")
    utf16 = head.startswith((b"\xff\xfe", b"\xfe\xff"))
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            if not utf16 and b"\x00" in chunk:
                bad("binary content")


# ---- add -----------------------------------------------------------------------------------
def _count(project: str) -> int:
    con = _con()
    try:
        return con.execute("SELECT count(*) FROM documents WHERE project=? AND deleted_at IS NULL",
                           (project,)).fetchone()[0]
    finally:
        con.close()


def incoming_path(project: str) -> Path:
    d = docs_dir(project) / ".incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}.part"


def add_file(project: str, raw_name: str, tmp: Path, *, owner: str = "",
             level: int | None = None, modified_at: float | None = None) -> dict:
    """Register an uploaded file already written at ``tmp`` (inside the project's
    ``.incoming``). Checks name, size, content; moves it under a fresh id; extracts the text
    copy. On any refusal ``tmp`` is removed and DocumentError raised."""
    try:
        name, ext = safe_name(raw_name)
        size = tmp.stat().st_size
        if size > max_bytes():
            raise DocumentError(413, "too_large", f"file larger than {max_bytes() // (1024 * 1024)} MB")
        if _count(project) >= max_per_project():
            raise DocumentError(409, "quota", f"this project already holds {max_per_project()} "
                                              "documents: remove some first")
        check_content(tmp, ext)
    except DocumentError:
        tmp.unlink(missing_ok=True)
        raise
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    doc_id = uuid.uuid4().hex[:16]
    dest_dir = docs_dir(project) / doc_id
    dest_dir.mkdir(parents=True, exist_ok=False)
    dest = dest_dir / name
    os.replace(tmp, dest)
    try:
        dest.chmod(0o640)
    except OSError:
        pass
    text_state, text_chars = "ok", 0
    if ext in ("txt", "md"):
        try:
            text_chars = len(doctext.decode_text(dest.read_bytes()[:4_000_000]))
        except OSError:
            text_state = "failed"
    else:
        text, text_state = doctext.extract(dest, ext)
        if text:
            (dest_dir / (name + ".txt")).write_text(text, encoding="utf-8")
            text_chars = len(text)
    now = time.time()
    lvl = _default_level() if level is None else int(level)
    if modified_at is not None and not (0 < modified_at <= now + 86400):
        modified_at = None
    con = _con()
    con.execute(
        "INSERT INTO documents(id, project, name, ext, size, sha256, owner, level, created_at,"
        " modified_at, text_state, text_chars) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, project, name, ext, size, h.hexdigest(), (owner or "").lower().strip(), lvl,
         now, modified_at, text_state, text_chars))
    con.commit()
    con.close()
    return get(doc_id)


# ---- read ----------------------------------------------------------------------------------
def _out(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["modified_at"] = d["modified_at"] or d["created_at"]
    return d


def get(doc_id: str, include_deleted: bool = False) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{16}", doc_id or ""):
        return None
    con = _con()
    r = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    con.close()
    if not r or (r["deleted_at"] and not include_deleted):
        return None
    return _out(r)


def list_project(project: str, max_level: int | None = None) -> list[dict]:
    con = _con()
    q = "SELECT * FROM documents WHERE project=? AND deleted_at IS NULL"
    args: list = [project]
    if max_level is not None:
        q += " AND level<=?"
        args.append(int(max_level))
    rows = [_out(r) for r in con.execute(q + " ORDER BY created_at DESC", args)]
    con.close()
    return rows


def visible(doc: dict | None, clearance: int | None) -> bool:
    return bool(doc) and (clearance is None or int(doc["level"]) <= int(clearance))


def delete(doc_id: str) -> None:
    d = get(doc_id)
    if not d:
        return
    try:
        shutil.rmtree(path_of(d).parent, ignore_errors=True)
    except DocumentError:
        pass
    con = _con()
    con.execute("UPDATE documents SET deleted_at=? WHERE id=?", (time.time(), doc_id))
    con.execute("DELETE FROM session_documents WHERE doc_id=?", (doc_id,))
    con.commit()
    con.close()


# ---- sessions ------------------------------------------------------------------------------
def attachable(doc: dict) -> bool:
    """With classification on, only documents at or under the default level go to a session."""
    try:
        import classification
        if not classification.enabled():
            return True
    except Exception:  # noqa: BLE001
        return True
    return int(doc["level"]) <= _default_level()


def attach(session_id: str, doc_id: str, by: str = "") -> None:
    con = _con()
    con.execute("INSERT OR IGNORE INTO session_documents(session_id, doc_id, at, by) VALUES(?,?,?,?)",
                (session_id, doc_id, time.time(), (by or "").lower().strip()))
    con.commit()
    con.close()


def of_session(session_id: str) -> list[dict]:
    con = _con()
    rows = [_out(r) for r in con.execute(
        "SELECT d.* FROM documents d JOIN session_documents s ON s.doc_id = d.id"
        " WHERE s.session_id=? AND d.deleted_at IS NULL ORDER BY s.at", (session_id,))]
    con.close()
    return rows


def session_has_documents(session_id: str) -> bool:
    """A document was attached to this session — removed since or not: its text may already
    be in the transcript. Decides that the session's memory writes are quarantined (the same
    question is asked by the sokkan-memory MCP server, which reads this table itself)."""
    if not session_id or not DB.exists():
        return False
    con = _con()
    try:
        return con.execute("SELECT 1 FROM session_documents WHERE session_id=? LIMIT 1",
                           (session_id,)).fetchone() is not None
    finally:
        con.close()


def session_counts(project: str) -> dict[str, int]:
    """doc id → number of sessions it is attached to (the library says « used in 2 »)."""
    con = _con()
    out = {r[0]: r[1] for r in con.execute(
        "SELECT s.doc_id, count(*) FROM session_documents s JOIN documents d ON d.id = s.doc_id"
        " WHERE d.project=? GROUP BY s.doc_id", (project,))}
    con.close()
    return out


def agent_lines(docs: list[dict]) -> list[str]:
    """One line per document for the agent's brief: its name and the file to read."""
    out = []
    for d in docs:
        try:
            p, t = path_of(d), text_path_of(d)
        except DocumentError:
            continue
        if t is not None:
            out.append(f"- « {d['name']} » — read: {t}")
        else:
            out.append(f"- « {d['name']} » — no text copy could be made ({d['text_state']}); "
                       f"original file: {p}")
    return out


def public(doc: dict, uses: int = 0) -> dict:
    """What the UI gets: never a path."""
    return {"id": doc["id"], "name": doc["name"], "ext": doc["ext"], "size": doc["size"],
            "owner": doc["owner"], "level": doc["level"], "created_at": doc["created_at"],
            "modified_at": doc["modified_at"], "text_state": doc["text_state"],
            "text_chars": doc["text_chars"], "uses": uses}
