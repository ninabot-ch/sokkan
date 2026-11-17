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


def _csv(path: Path) -> str:
    """A table (a bank export…) as a Markdown table the agent reads row by row."""
    import outputs
    rows = outputs.csv_rows(decode_text(path.read_bytes()[: MAX_CHARS * 2]))
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    esc = lambda c: c.replace("|", "\\|").replace("\n", " ")  # noqa: E731
    out = ["| " + " | ".join(esc(c) for c in rows[0] + [""] * (width - len(rows[0]))) + " |",
           "|" + "---|" * width]
    out += ["| " + " | ".join(esc(c) for c in r + [""] * (width - len(r))) + " |" for r in rows[1:]]
    return "\n".join(out)


_ATTACH_EXT = ("pdf", "docx", "pptx", "xlsx", "csv", "txt", "md")
MAX_ATTACH = 25 * 1024 * 1024


def _attachment_text(name: str, data: bytes, depth: int) -> str:
    """The text of a mail's attachment of a known type (a temporary copy, removed at once)."""
    import tempfile
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _ATTACH_EXT or len(data) > MAX_ATTACH or depth > 1:
        return ""
    with tempfile.TemporaryDirectory(prefix="sokkan-att-") as d:
        p = Path(d) / f"a.{ext}"
        p.write_bytes(data)
        if ext in ("txt", "md"):
            return decode_text(data)[:200_000]
        text, _state = extract(p, ext, _depth=depth + 1)
        return text[:200_000]


def _mail_out(headers: list[tuple[str, str]], body: str, atts: list[tuple[str, bytes | None]],
              depth: int) -> str:
    out = [f"{k}: {v}" for k, v in headers if v]
    out += ["", body.strip()]
    for name, data in atts:
        size = f" ({len(data) // 1024} KB)" if data is not None else ""
        out += ["", f"## Attachment: {name}{size}"]
        t = _attachment_text(name, data, depth) if data is not None else ""
        out.append(t.strip() if t.strip() else "(no text copy of this attachment)")
    return "\n".join(out)


def _html_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", h)
    t = html.unescape(_TAG.sub(" ", h))
    return "\n".join(_WS.sub(" ", ln).strip() for ln in t.splitlines() if ln.strip())


def _eml(path: Path, depth: int = 0) -> str:
    """A mail (.eml): who, to whom, when, the subject, the text, and its attachments'
    text. The standard library's parser, no network, no HTML rendering."""
    from email import policy
    from email.parser import BytesParser
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    headers = [(k, str(msg.get(k, "") or "")) for k in ("From", "To", "Cc", "Date", "Subject")]
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = _html_text(content) if part.get_content_type() == "text/html" else content
    except Exception:  # noqa: BLE001 — a malformed body: the headers and attachments still
        body = ""
    atts = []
    for a in msg.iter_attachments():
        try:
            data = a.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            data = None
        atts.append((a.get_filename() or "attachment", data))
    return _mail_out(headers, body, atts, depth)


def _msg(path: Path, depth: int = 0) -> str:
    """An Outlook message (.msg, OLE compound file) through `olefile` (BSD): subject, sender,
    recipients, date, text and attachments — the plain MAPI properties, no RTF rendering."""
    try:
        import olefile  # type: ignore
    except ImportError as e:
        raise _Unavailable() from e
    import struct
    with olefile.OleFileIO(str(path)) as ole:
        def prop(code: str, root: str = "") -> str:
            for typ, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
                name = f"{root}__substg1.0_{code}{typ}"
                if ole.exists(name):
                    return ole.openstream(name).read(MAX_MEMBER).decode(enc, errors="replace").rstrip("\x00")
            return ""
        sender = prop("0C1A")
        mail = prop("5D01") or prop("0C1F")
        date = ""
        if ole.exists("__properties_version1.0"):
            raw = ole.openstream("__properties_version1.0").read(1 << 20)
            for off in range(32, len(raw) - 15, 16):
                tag = struct.unpack_from("<I", raw, off)[0]
                if tag in (0x00390040, 0x0E060040):           # submit time, delivery time
                    ft = struct.unpack_from("<Q", raw, off + 8)[0]
                    if ft:
                        import datetime as dt
                        t = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=ft // 10)
                        date = t.strftime("%d.%m.%Y %H:%M UTC")
                        break
        headers = [("From", f"{sender} <{mail}>" if sender and mail else sender or mail),
                   ("To", prop("0E04")), ("Cc", prop("0E03")), ("Date", date), ("Subject", prop("0037"))]
        body = prop("1000")
        if not body.strip():
            for typ in ("0102", "001F"):
                n = f"__substg1.0_1013{typ}"
                if ole.exists(n):
                    body = _html_text(ole.openstream(n).read(MAX_MEMBER).decode(
                        "utf-8" if typ == "0102" else "utf-16-le", errors="replace"))
                    break
        atts = []
        roots = sorted({e[0] for e in ole.listdir() if e[0].startswith("__attach_version1.0_")})
        for r in roots[:50]:
            name = prop("3707", r + "/") or prop("3704", r + "/") or "attachment"
            data_name = f"{r}/__substg1.0_37010102"
            data = None
            if ole.exists(data_name) and ole.get_size(data_name) <= MAX_ATTACH:
                data = ole.openstream(data_name).read()
            atts.append((name, data))
    return _mail_out(headers, body, atts, depth)


class _Unavailable(Exception):
    pass


OCR_TIMEOUT = 90


def ocr_languages() -> str:
    """The OCR languages installed among French, German, Italian, English (« fra+deu+eng »)."""
    import shutil
    import subprocess
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    have = set(out.split())
    return "+".join(x for x in ("fra", "deu", "ita", "eng") if x in have) or ("eng" if "eng" in have else "")


def _image(path: Path) -> tuple[str, str]:
    """A photo (a receipt, a scanned letter): its text by OCR with Tesseract (Apache-2.0),
    on this machine — nothing leaves it, whatever the project's classification. Without
    Tesseract: « unavailable » (the person is told, the agent asks for the figures)."""
    import shutil
    import subprocess
    langs = ocr_languages()
    exe = shutil.which("tesseract")
    if not exe or not langs:
        return "", "unavailable"
    try:
        r = subprocess.run(["nice", "-n", "10", exe, str(path), "-", "-l", langs, "--psm", "3"],
                           capture_output=True, timeout=OCR_TIMEOUT,
                           env={"OMP_THREAD_LIMIT": "2", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return "", "failed"
    text = r.stdout.decode("utf-8", errors="replace").strip() if r.returncode == 0 else ""
    if not text:
        return "", "empty" if r.returncode == 0 else "failed"
    return ("(Text read from a photo by character recognition: figures may be misread.)\n\n"
            + text), "ok"


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


def extract(path: str | Path, ext: str, _depth: int = 0) -> tuple[str, str]:
    """→ (text, state). Never raises: a document that cannot be read is still stored, the
    state tells the person and the agent why there is no text copy."""
    p = Path(path)
    ext = (ext or "").lower().lstrip(".")
    try:
        state = "ok"
        if ext == "csv":
            text = _csv(p)
        elif ext == "eml":
            text = _eml(p, _depth)
        elif ext == "msg":
            try:
                text = _msg(p, _depth)
            except _Unavailable:
                return "", "unavailable"
        elif ext in ("jpg", "jpeg", "png"):
            text, state = _image(p)
            if state != "ok":
                return "", state
        elif ext in ("txt", "md"):
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
