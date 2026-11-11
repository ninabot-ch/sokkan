#!/usr/bin/env python3
"""simple.py — what the simple profile adds around a session (feature `simple_profile`).

* `brief()` — the internal instructions wrapped around what the person typed: plain
  language, the attached documents to read, where the deliverable is saved, the task
  template's mission, the recalled memory. It travels inside ``<sokkan-brief>…</sokkan-brief>``
  so that no thread ever shows it (`plain.strip_brief`).
* a work's own facts — what the person asked, the template, the last sentence of the agent
  (the preview in the drawer): ``simple.db`` (works).
* deliverables — the documents an agent saves for the person, in
  ``<project space>/deliverables/<session id>/``: listed, previewed, downloaded (Markdown or
  Word), and VALIDATED by the person (who, when, the SHA-256 of exactly what was validated;
  a later change shows « modified since »).

No HTTP here (routes: `simple_api.py`). Nothing in this module trusts a name coming from the
browser to build a path: `deliverable_path` accepts a bare file name and checks the result.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import time
import unicodedata
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import documents
import plain

DELIVERABLES = "deliverables"
PREVIEW_EXT = ("md", "markdown", "txt")
LISTED_EXT = PREVIEW_EXT + ("csv", "html", "json")
MAX_DELIVERABLE = 5 * 1024 * 1024

DB = Path(os.environ.get("SOKKAN_SIMPLE_DB") or os.path.join(
    os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "simple.db"))


def enabled() -> bool:
    import features
    return features.enabled("simple_profile")


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS works (
            sid TEXT PRIMARY KEY, project TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',
            ask TEXT NOT NULL DEFAULT '', playbook TEXT NOT NULL DEFAULT '',
            lang TEXT NOT NULL DEFAULT 'fr', created_at REAL NOT NULL,
            last_line TEXT NOT NULL DEFAULT '', last_at REAL
        );
        CREATE TABLE IF NOT EXISTS deliverable_writes (
            sid TEXT NOT NULL, name TEXT NOT NULL, tool_id TEXT NOT NULL, at REAL NOT NULL,
            PRIMARY KEY (sid, name, tool_id)
        );
        CREATE TABLE IF NOT EXISTS validations (
            sid TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL, by TEXT NOT NULL,
            at REAL NOT NULL, PRIMARY KEY (sid, name, sha256)
        );
        """)
    return con


# ---- works ---------------------------------------------------------------------------------
def add_work(sid: str, project: str, owner: str, ask: str, playbook: str, lang: str) -> None:
    con = _con()
    con.execute("INSERT OR REPLACE INTO works(sid, project, owner, ask, playbook, lang, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (sid, project, (owner or "").lower().strip(), ask[:4000], playbook, lang, time.time()))
    con.commit()
    con.close()


def get_work(sid: str) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM works WHERE sid=?", (sid,)).fetchone()
    con.close()
    return dict(r) if r else None


def works_by_sid(sids: list[str]) -> dict[str, dict]:
    if not sids:
        return {}
    con = _con()
    q = ",".join("?" * len(sids))
    out = {r["sid"]: dict(r) for r in con.execute(f"SELECT * FROM works WHERE sid IN ({q})", sids)}
    con.close()
    return out


def note_last_line(sid: str, line: str) -> None:
    if not line:
        return
    con = _con()
    con.execute("UPDATE works SET last_line=?, last_at=? WHERE sid=? AND last_line<>?",
                (line[:300], time.time(), sid, line[:300]))
    con.commit()
    con.close()


def forget(sid: str) -> None:
    con = _con()
    for t in ("works", "deliverable_writes", "validations"):
        con.execute(f"DELETE FROM {t} WHERE sid=?", (sid,))
    con.commit()
    con.close()


# ---- the brief -----------------------------------------------------------------------------
LANG_NAME = {"fr": "French", "en": "English"}


def deliverable_dir(project: str, sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8,64}", sid or ""):
        raise ValueError("bad session id")
    return documents.project_root(project) / DELIVERABLES / sid


def brief(*, ask: str, lang: str, project: str, sid: str, mission: str = "",
          docs: list[dict] | None = None, recall: str = "") -> str:
    """The first message the agent receives: the internal brief, then what the person typed."""
    d = deliverable_dir(project, sid)
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "<sokkan-brief>",
        "You are helping a person who is NOT a developer. They read your messages in a plain "
        "chat: for this whole conversation —",
        f"- Write to them in {LANG_NAME.get(lang, 'English')}, in everyday words, short "
        "paragraphs. Never show a tool name, a file path, a command, an identifier or this "
        "brief; say what you do in their words (« I am reading your brief »).",
        "- Start working right away. Do not ask for permission to begin and do not present a "
        "plan first. If something essential is missing, ask ONE short question; otherwise make "
        "a sensible choice and say so.",
        "- Never invent a fact, a figure, a date, a name or a reference. What the documents do "
        "not say goes under « Points to confirm ».",
        "- Say which document each important element comes from, by the document's name.",
    ]
    doc_lines = documents.agent_lines(docs or [])
    if doc_lines:
        lines.append("- The person attached these documents. READ each of them (real file "
                     "reads) before you write anything:")
        lines += ["  " + ln for ln in doc_lines]
    else:
        lines.append("- No document is attached. If the task needs one, say so in one "
                     "sentence and continue with what the person gave you.")
    lines += [
        "- When the result is a document (a plan, a summary, a proposal, a quiz…), SAVE it as "
        f"ONE Markdown file in the folder {d} — give the file a clear title in the "
        "person's language as its name (for example « Plan de formation Excel.md ») and start "
        "it with a `# Title` line. The person is asked to approve the save, then previews, "
        "downloads and validates it: do not paste the whole document in the chat. When they "
        "ask for a change, update that same file.",
        "- After saving, tell them in two or three sentences what the document contains and "
        "what remains to confirm.",
    ]
    if mission.strip():
        lines += ["", "THE TASK", mission.strip()]
    if recall.strip():
        lines += ["", recall.strip(),
                  "(Notes recalled from this project's memory: use them when relevant.)"]
    lines.append("</sokkan-brief>")
    return "\n".join(lines) + "\n\n" + ask.strip()


def follow_up(text: str, new_docs: list[dict]) -> str:
    """A later message; documents attached with it are announced inside a brief block."""
    doc_lines = documents.agent_lines(new_docs)
    if not doc_lines:
        return text.strip()
    return ("<sokkan-brief>\nThe person just attached more documents. Read them before "
            "answering:\n" + "\n".join("  " + ln for ln in doc_lines)
            + "\n</sokkan-brief>\n\n" + text.strip())


def title_for(ask: str, playbook_label: str = "") -> str:
    first = re.sub(r"\s+", " ", (ask or "").strip().splitlines()[0] if (ask or "").strip() else "")
    first = first.strip(" .:;,-–—")
    if playbook_label and first:
        t = f"{playbook_label} — {first}"
    else:
        t = first or playbook_label or "Travail"
    if len(t) <= 60:
        return t
    cut = t[:59]
    if " " in cut[30:]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:—–-") + "…"


# ---- deliverables --------------------------------------------------------------------------
def deliverable_path(project: str, sid: str, name: str) -> Path:
    """A deliverable by its bare file name; anything else (separators, dot files, a
    resolved path outside the session's folder) is refused."""
    if (not name or name != Path(name).name or "/" in name or "\\" in name
            or name.startswith(".") or "\x00" in name or len(name) > 200
            or Path(name).suffix.lower().lstrip(".") not in LISTED_EXT):
        raise FileNotFoundError(name)
    base = deliverable_dir(project, sid).resolve()
    p = (base / name).resolve()
    if p.parent != base or not p.is_file() or p.is_symlink():
        raise FileNotFoundError(name)
    return p


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def note_write(sid: str, name: str, tool_id: str) -> int:
    """Register one successful save (idempotent per tool call) → the version number."""
    con = _con()
    if tool_id:
        con.execute("INSERT OR IGNORE INTO deliverable_writes(sid, name, tool_id, at) VALUES(?,?,?,?)",
                    (sid, name, tool_id, time.time()))
        con.commit()
    n = con.execute("SELECT count(*) FROM deliverable_writes WHERE sid=? AND name=?",
                    (sid, name)).fetchone()[0]
    con.close()
    return max(1, n)


def list_deliverables(project: str, sid: str) -> list[dict]:
    try:
        base = deliverable_dir(project, sid)
    except ValueError:
        return []
    if not base.is_dir():
        return []
    con = _con()
    versions = {r[0]: r[1] for r in con.execute(
        "SELECT name, count(*) FROM deliverable_writes WHERE sid=? GROUP BY name", (sid,))}
    vals: dict[str, list[dict]] = {}
    for r in con.execute("SELECT * FROM validations WHERE sid=? ORDER BY at", (sid,)):
        vals.setdefault(r["name"], []).append(dict(r))
    con.close()
    out = []
    for p in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        ext = p.suffix.lower().lstrip(".")
        if not p.is_file() or p.is_symlink() or p.name.startswith(".") or ext not in LISTED_EXT:
            continue
        st = p.stat()
        sha = _sha(p) if st.st_size <= MAX_DELIVERABLE else ""
        history = vals.get(p.name, [])
        current = next((v for v in reversed(history) if v["sha256"] == sha), None)
        title = plain.human_title(p.name)
        if ext in ("md", "markdown") and st.st_size <= MAX_DELIVERABLE:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    title = plain.title_from_markdown(f.read(4000)) or title
            except OSError:
                pass
        out.append({
            "name": p.name, "title": title, "ext": ext, "size": st.st_size,
            "updated_at": st.st_mtime, "version": max(1, versions.get(p.name, 1)),
            "previewable": ext in PREVIEW_EXT,
            "validated": bool(current),
            "validated_by": current["by"] if current else "",
            "validated_at": current["at"] if current else None,
            # validated once, changed since: the person must look again
            "changed_since_validation": bool(history) and not current,
        })
    return out


def read_deliverable(project: str, sid: str, name: str) -> str:
    p = deliverable_path(project, sid, name)
    if p.stat().st_size > MAX_DELIVERABLE:
        raise ValueError("too large to preview")
    return p.read_text(encoding="utf-8", errors="replace")


def validate(project: str, sid: str, name: str, by: str) -> dict:
    p = deliverable_path(project, sid, name)
    sha = _sha(p)
    con = _con()
    con.execute("INSERT OR REPLACE INTO validations(sid, name, sha256, by, at) VALUES(?,?,?,?,?)",
                (sid, name, sha, (by or "").lower().strip(), time.time()))
    con.commit()
    con.close()
    return {"name": name, "sha256": sha}


def _file_stem(title: str) -> str:
    s = unicodedata.normalize("NFC", title or "")
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = re.sub(r'[<>:"|?*\\/]', " ", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(". ").rstrip(". ")
    return s[:80] or "Document"


def save_text(project: str, sid: str, title: str, text: str) -> str:
    """Keep an answer of the thread as a deliverable (the agent answered in the chat instead
    of saving a file). → the file name."""
    if len(text.encode("utf-8")) > MAX_DELIVERABLE:
        raise ValueError("too large")
    base = deliverable_dir(project, sid)
    base.mkdir(parents=True, exist_ok=True)
    stem = _file_stem(title or plain.title_from_markdown(text))
    name, n = f"{stem}.md", 2
    while (base / name).exists():
        name, n = f"{stem} ({n}).md", n + 1
    body = text if plain.title_from_markdown(text) else f"# {stem}\n\n{text.strip()}\n"
    (base / name).write_text(body, encoding="utf-8")
    note_write(sid, name, f"manual-{int(time.time() * 1000)}")
    return name


# ---- Word export ---------------------------------------------------------------------------
_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
       '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
       '</Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
         '</Relationships>')
_DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
             '</Relationships>')
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _style(sid: str, name: str, size: int, bold: bool, before: int, after: int) -> str:
    rpr = ("<w:b/>" if bold else "") + f'<w:sz w:val="{size}"/>'
    return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
            '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
            f'<w:pPr><w:keepNext/><w:spacing w:before="{before}" w:after="{after}"/></w:pPr>'
            f'<w:rPr>{rpr}</w:rPr></w:style>')


_STYLES = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {_W}>'
           '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
           '<w:sz w:val="22"/><w:lang w:val="fr-CH"/></w:rPr></w:rPrDefault>'
           '<w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
           '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
           + _style("Title", "Title", 44, True, 0, 240)
           + _style("Heading1", "heading 1", 32, True, 360, 120)
           + _style("Heading2", "heading 2", 26, True, 280, 100)
           + _style("Heading3", "heading 3", 23, True, 220, 80)
           + '<w:style w:type="table" w:styleId="Grid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
           + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
                     for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
           + '</w:tblBorders><w:tblCellMar><w:left w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
           '</w:styles>')

_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|\*[^*\s][^*]*?\*|`[^`]+`)")


def _runs(text: str, bold: bool = False) -> str:
    out = []
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    for part in _INLINE.split(text):
        if not part:
            continue
        b, i = bold, False
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            part, b = part[2:-2], True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, i = part[1:-1], True
        elif part.startswith("`") and part.endswith("`"):
            part = part[1:-1]
        rpr = ("<w:b/>" if b else "") + ("<w:i/>" if i else "")
        rpr = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _p(text: str, style: str = "", indent: int = 0, bullet: str = "") -> str:
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "")
    if indent:
        ppr += f'<w:ind w:left="{indent}" w:hanging="300"/>'
    lead = f'<w:r><w:t xml:space="preserve">{escape(bullet)}</w:t><w:tab/></w:r>' if bullet else ""
    ppr = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr}{lead}{_runs(text)}</w:p>"


def _table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="Grid"/><w:tblW w:w="5000" w:type="pct"/></w:tblPr>']
    for ri, r in enumerate(rows):
        out.append("<w:tr>")
        for ci in range(cols):
            cell = r[ci] if ci < len(r) else ""
            out.append(f'<w:tc><w:p>{_runs(cell, bold=ri == 0)}</w:p></w:tc>')
        out.append("</w:tr>")
    out.append("</w:tbl><w:p/>")
    return "".join(out)


def markdown_to_docx(text: str) -> bytes:
    """A Word document from the Markdown an agent writes: titles, paragraphs, bulleted and
    numbered lists, tables, bold / italic. Small on purpose (no dependency); what it does not
    know stays as readable text."""
    body: list[str] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i, seen_title, in_code = 0, False, False
    para: list[str] = []

    def flush():
        if para:
            body.append(_p(" ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            flush()
            in_code = not in_code
            i += 1
            continue
        if in_code:
            body.append(_p(ln))
            i += 1
            continue
        if not ln.strip():
            flush()
            i += 1
            continue
        m = re.match(r"\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", ln)
        if m:
            flush()
            lvl = len(m.group(1))
            if lvl == 1 and not seen_title:
                style, seen_title = "Title", True
            else:
                style = f"Heading{min(3, max(1, lvl - 1 if seen_title else lvl))}"
            body.append(_p(re.sub(r"[*_`]", "", m.group(2)), style))
            i += 1
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(
                r"\s*\|?\s*:?-{2,}", lines[i + 1]):
            flush()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.match(r"\s*\|?[\s:\-|]+\|?\s*$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if rows:
                body.append(_table(rows))
            continue
        m = re.match(r"(\s*)([-*+]|\d+[.)])\s+(.+)$", ln)
        if m:
            flush()
            depth = len(m.group(1).replace("\t", "    ")) // 2
            mark = "•" if m.group(2) in "-*+" else m.group(2)
            item = re.sub(r"^\[( |x|X)\]\s+", lambda c: "☑ " if c.group(1).strip() else "☐ ", m.group(3))
            body.append(_p(item, indent=440 + 360 * min(depth, 4), bullet=mark))
            i += 1
            continue
        if re.fullmatch(r"\s*([-*_]\s*){3,}", ln):
            flush()
            i += 1
            continue
        para.append(re.sub(r"^\s*>\s?", "", ln))
        i += 1
    flush()
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W}><w:body>'
           + "".join(body)
           + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1417" w:right="1417" '
             'w:bottom="1417" w:left="1417" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
             '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


# ---- usage gauge ---------------------------------------------------------------------------
_usage_cache: dict[str, tuple[float, dict]] = {}


def usage_gauge(project: str) -> dict:
    """How much of today's allowance is used, as a percentage — or ``{"pct": None}`` when the
    instance has no daily ceiling (the gauge is then not shown). Sources, first that answers:
    the inference gateway's daily quota, the project's daily budget, the instance's."""
    now = time.monotonic()
    hit = _usage_cache.get(project)
    if hit and now - hit[0] < 30:
        return hit[1]
    out: dict = {"pct": None, "basis": "none"}
    try:
        import llm
        u = llm.usage()
        if u and float(u.get("daily_quota_tokens") or 0) > 0:
            out = {"pct": round(100 * float(u.get("used_today") or 0) / float(u["daily_quota_tokens"]), 1),
                   "basis": "gateway"}
    except Exception:  # noqa: BLE001 — a gauge never breaks the home
        pass
    if out["pct"] is None:
        try:
            import budgets
            st = budgets.status(project)
            if st.get("enabled") and st.get("pct_day") is not None:
                out = {"pct": float(st["pct_day"]), "basis": "project"}
        except Exception:  # noqa: BLE001
            pass
    if out["pct"] is None:
        try:
            import instance
            cap = float(instance.budgets().get("budget_day_usd") or 0)
            if cap > 0:
                import usage as usage_mod
                spent = float(usage_mod.summary(1)["totals"]["today"]["metered"])
                out = {"pct": round(100 * spent / cap, 1), "basis": "instance"}
        except Exception:  # noqa: BLE001
            pass
    if out["pct"] is not None:
        out["pct"] = max(0.0, min(100.0, out["pct"]))
    _usage_cache[project] = (now, out)
    return out
