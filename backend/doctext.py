#!/usr/bin/env python3
"""doctext.py — a readable text copy of a dropped document (feature `documents`).

The session's agent reads files with plain file tools: a .docx is a zip of XML to it. At
upload SOKKAN writes next to each document a ``<name>.txt`` with its text, so « read the
brief » is one Read on any engine (a model that is not Claude cannot open a PDF natively).

Deliberately small and defensive — NOT the memory ingestion (extraction with structure,
chunks, notes: the `ingest/` work), which can replace `extract` later:

* Office files are read as zip members with a size ceiling per member and in total (zip
  bomb), and their XML is scanned with regular expressions — no XML parser, so no entity
  expansion and no external entity;
* PDF goes through `pypdf` when it is installed (optional dependency); without it, or on
  a scanned PDF, the state says so and the agent is pointed at the original file;
* the result is capped (`MAX_CHARS`).

`extract(path, ext)` → ``(text, state)``; state: ``ok`` | ``empty`` | ``partial`` |
``unavailable`` | ``failed``. Never raises.
"""
from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

MAX_CHARS = 1_500_000            # ~375k tokens: more than any session will read at once
MAX_MEMBER = 40 * 1024 * 1024    # one XML part, uncompressed
MAX_TOTAL = 200 * 1024 * 1024    # every part read for one document
MAX_PDF_PAGES = 2000

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")


def _clean(s: str) -> str:
    s = html.unescape(_TAG.sub("", s))
    return _WS.sub(" ", s).strip()


def _member(z: zipfile.ZipFile, name: str, budget: list[int]) -> str:
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER or info.file_size > budget[0]:
        raise ValueError("part too large")
    budget[0] -= info.file_size
    with z.open(info) as f:
        return f.read(MAX_MEMBER + 1).decode("utf-8", errors="replace")


def _natural(name: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name))


def _runs(xml: str) -> str:
    return "".join(html.unescape(t) for t in re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))


def _table(m: re.Match) -> str:
    """A table becomes one paragraph per row, cells separated by « | »."""
    rows = []
    for tr in re.findall(r"<w:tr\b.*?</w:tr>", m.group(0), flags=re.S):
        cells = [_WS.sub(" ", _runs(tc)).strip()
                 for tc in re.findall(r"<w:tc\b.*?</w:tc>", tr, flags=re.S)]
        if any(cells):
            rows.append(" | ".join(cells))
    return "".join(f"<w:p><w:t>{html.escape(r)}</w:t></w:p>" for r in rows)


def _docx(path: Path) -> str:
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        budget = [MAX_TOTAL]
        names = [n for n in z.namelist()
                 if re.fullmatch(r"word/(document|footnotes|endnotes)\.xml", n)]
        for n in sorted(names, key=lambda x: (x != "word/document.xml", x)):
            xml = _member(z, n, budget)
            xml = re.sub(r"<w:tab\b[^>]*/>", "<w:t>\t</w:t>", xml)
            xml = re.sub(r"<w:br\b[^>]*/>", "<w:t>\n</w:t>", xml)
            xml = re.sub(r"<w:tbl>.*?</w:tbl>", _table, xml, flags=re.S)
            for para in xml.split("</w:p>"):
                text = _WS.sub(" ", _runs(para)).strip()
                if not text:
                    continue
                style = re.search(r'<w:pStyle w:val="([^"]+)"', para)
                name = style.group(1) if style else ""
                lvl = re.match(r"(?i)(?:heading|titre)\s*([1-6])", name)
                if lvl:
                    text = "#" * int(lvl.group(1)) + " " + text
                elif re.match(r"(?i)title|titre$", name):
                    text = "# " + text
                out.append(text)
    return "\n\n".join(out)


def _pptx(path: Path) -> str:
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        budget = [MAX_TOTAL]
        slides = sorted((n for n in z.namelist()
                         if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_natural)
        for i, n in enumerate(slides, 1):
            xml = _member(z, n, budget)
            paras = []
            for para in re.split(r"</a:p>", xml):
                t = "".join(html.unescape(x) for x in re.findall(r"<a:t>([^<]*)</a:t>", para))
                t = _WS.sub(" ", t).strip()
                if t:
                    paras.append(t)
            notes_name = f"ppt/notesSlides/notesSlide{i}.xml"
            notes = ""
            if notes_name in z.namelist():
                nx = _member(z, notes_name, budget)
                notes = " ".join(_WS.sub(" ", html.unescape(x)).strip()
                                 for x in re.findall(r"<a:t>([^<]*)</a:t>", nx)).strip()
            if paras or notes:
                block = [f"## Slide {i}"] + paras
                if notes and not notes.isdigit():
                    block.append(f"(notes) {notes}")
                out.append("\n".join(block))
    return "\n\n".join(out)


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
    return n


def _xlsx(path: Path) -> str:
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        budget = [MAX_TOTAL]
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sx = _member(z, "xl/sharedStrings.xml", budget)
            for si in re.findall(r"<si>(.*?)</si>", sx, flags=re.S):
                shared.append("".join(html.unescape(t) for t in re.findall(
                    r"<t(?:\s[^>]*)?>([^<]*)</t>", si)))
        names: dict[str, str] = {}
        if "xl/workbook.xml" in z.namelist():
            wx = _member(z, "xl/workbook.xml", budget)
            for i, m in enumerate(re.finditer(r'<sheet\b[^>]*\bname="([^"]*)"', wx), 1):
                names[f"xl/worksheets/sheet{i}.xml"] = html.unescape(m.group(1))
        sheets = sorted((n for n in z.namelist()
                         if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=_natural)
        for n in sheets:
            xml = _member(z, n, budget)
            rows = []
            for row in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, flags=re.S):
                cells: dict[int, str] = {}
                for m in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row, flags=re.S):
                    attrs, body = m.group(1), m.group(2) or ""
                    ref = re.search(r'\br="([A-Z]+)\d+"', attrs)
                    typ = re.search(r'\bt="([^"]+)"', attrs)
                    v = re.search(r"<v>([^<]*)</v>", body)
                    if typ and typ.group(1) == "inlineStr":
                        val = "".join(re.findall(r"<t(?:\s[^>]*)?>([^<]*)</t>", body))
                    elif v is None:
                        continue
                    elif typ and typ.group(1) == "s":
                        try:
                            val = shared[int(v.group(1))]
                        except (ValueError, IndexError):
                            val = ""
                    else:
                        val = v.group(1)
                    val = _WS.sub(" ", html.unescape(val)).strip()
                    if val:
                        cells[_col(ref.group(1)) if ref else len(cells) + 1] = val
                if cells:
                    width = max(cells)
                    rows.append(" | ".join(cells.get(i, "") for i in range(1, width + 1)))
            if rows:
                out.append(f"## {names.get(n) or n.rsplit('/', 1)[-1]}\n" + "\n".join(rows))
    return "\n\n".join(out)


def _pdf(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:  # noqa: BLE001 — optional dependency
        return "", "unavailable"
    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        return "", "failed"
    out: list[str] = []
    total = 0
    state = "ok"
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES or total > MAX_CHARS:
            state = "partial"
            break
        try:
            t = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 — one unreadable page does not lose the others
            t, state = "", "partial"
        if t:
            out.append(f"## Page {i + 1}\n{t}")
            total += len(t)
    return "\n\n".join(out), state


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


def extract(path: str | Path, ext: str) -> tuple[str, str]:
    """→ (text, state). Never raises: a document that cannot be read is still stored, the
    state tells the person and the agent why there is no text copy."""
    p = Path(path)
    ext = (ext or "").lower().lstrip(".")
    try:
        state = "ok"
        if ext in ("txt", "md"):
            text = decode_text(p.read_bytes()[: MAX_CHARS * 4])
        elif ext == "docx":
            text = _docx(p)
        elif ext == "pptx":
            text = _pptx(p)
        elif ext == "xlsx":
            text = _xlsx(p)
        elif ext == "pdf":
            text, state = _pdf(p)
            if state in ("unavailable", "failed"):
                return "", state
        else:
            return "", "unavailable"
    except Exception:  # noqa: BLE001 — damaged file, unexpected structure
        return "", "failed"
    text = text.replace("\x00", "").strip()
    if len(text) > MAX_CHARS:
        text, state = text[:MAX_CHARS], "partial"
    if not text:
        return "", "empty"
    return text, state
