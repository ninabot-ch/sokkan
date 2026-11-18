#!/usr/bin/env python3
"""outputs.py — the person's own files from a deliverable (4.0 « native formats »).

An agent saves a deliverable as a TEXT source (``.md``, or the ``.csv`` of a table): that is
what the person approves on the card, what is versioned and what the validation seals
(SHA-256). This module turns the source into the file the person asked for, when they
download it — the same source always gives the same file:

* **Word** (.docx) — titles, paragraphs, lists, tables (header row repeated, figures aligned
  right), page header and footer with page numbers; on the organisation's own Word template
  when an administrator gave one (`wordtemplates`), else a clean default;
* **Excel** (.xlsx) — a source that starts with the front matter ``format: xlsx`` (or a
  ``.csv`` table): each ``## Heading`` is a sheet, each Markdown table a block of rows,
  figures are numbers (« 8'120.50 », « CHF 1'250 », « 12 % », « 17.11.2026 » are read as
  such), cells that start with ``=`` are FORMULAS that Excel recalculates — written by the
  agent with the table's own coordinates (row 1 = the table's header), by column name
  (``=[Montant]-[Payé]``) or as ``=SUM(ABOVE)`` for a total. The values are also computed
  here (`_Eval`): stored in the file for viewers that do not recalculate, shown in the
  preview;
* **one document per recipient** (``format: letters``) — a mail merge: a template with
  ``{{Field}}`` and a table of recipients, or letters separated by a line ``---``; delivered
  as a .zip of Word files (or PDF), plus all the letters in one file to print;
* **PDF** — any text deliverable, with the template's logo and footer when there is one.

Front matter (optional, first lines of the source)::

    ---
    format: xlsx | docx | pdf | letters
    template: lettre        # one of the organisation's Word templates (default otherwise)
    ---

Dependencies (all permissive licences, Apache-2.0 compatible): python-docx (MIT), openpyxl
(MIT), reportlab (BSD). Without them the Word export falls back to `simple.markdown_to_docx`
and the other formats answer « unavailable » — nothing is sent to an outside service.
"""
from __future__ import annotations

import datetime as _dt
import io
import math
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

# ---- front matter --------------------------------------------------------------------------
_FORMAT_ALIASES = {
    "xlsx": "xlsx", "excel": "xlsx", "spreadsheet": "xlsx", "tableur": "xlsx", "xls": "xlsx",
    "docx": "docx", "word": "docx", "doc": "docx",
    "pdf": "pdf",
    "letters": "letters", "lettres": "letters", "letter": "letters", "publipostage": "letters",
    "mailmerge": "letters", "mail-merge": "letters", "merge": "letters",
}
MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "zip": "application/zip",
}


class Unavailable(Exception):
    """The library a format needs is not installed on this instance."""


def front_matter(text: str) -> tuple[dict, str]:
    """→ (meta, body). Only ``key: value`` lines between two ``---`` at the very top; an
    unknown key is kept, a malformed block is left in the body (it was not front matter)."""
    t = (text or "").lstrip("﻿")
    if not re.match(r"---[ \t]*\r?\n", t):
        return {}, t
    lines = t.splitlines()
    meta: dict = {}
    for i, ln in enumerate(lines[1:41], 1):
        if re.fullmatch(r"---\s*|\.\.\.\s*", ln):
            return meta, "\n".join(lines[i + 1:]).lstrip("\n")
        m = re.fullmatch(r"\s*([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*", ln)
        if not m:
            if not ln.strip():
                continue
            return {}, t
        meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    return {}, t


def kind_of(name: str, text: str) -> str:
    """document | spreadsheet | letters"""
    if (name or "").lower().endswith(".csv"):
        return "spreadsheet"
    meta, _ = front_matter(text)
    f = _FORMAT_ALIASES.get((meta.get("format") or "").lower().strip(), "")
    return {"xlsx": "spreadsheet", "letters": "letters"}.get(f, "document")


def formats_for(name: str, text: str) -> list[str]:
    """The downloads offered, the one the agent was asked for first."""
    kind = kind_of(name, text)
    if kind == "spreadsheet":
        return ["xlsx", "pdf", "csv"]
    if kind == "letters":
        return ["letters_docx", "letters_pdf", "docx"]
    meta, _ = front_matter(text)
    if _FORMAT_ALIASES.get((meta.get("format") or "").lower()) == "pdf":
        return ["pdf", "docx"]
    return ["docx", "pdf"]


# ---- Markdown blocks -----------------------------------------------------------------------
@dataclass
class Block:
    kind: str                     # title | heading | para | item | table | pagebreak | code
    text: str = ""
    level: int = 0                # heading level (1-3) / list depth
    mark: str = ""                # « • » or « 3. »
    rows: list = field(default_factory=list)


_HR = re.compile(r"\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, i = [], "", 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur += "|"
            i += 2
            continue
        if s[i] == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += s[i]
        i += 1
    cells.append(cur.strip())
    return cells


def blocks(md: str, hr_breaks: bool = False) -> list[Block]:
    """Markdown → blocks. ``hr_breaks``: a horizontal rule is a page break (letters)."""
    out: list[Block] = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    i, seen_title, in_code = 0, False, False
    para: list[str] = []
    code: list[str] = []

    def flush():
        if para:
            out.append(Block("para", " ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            flush()
            if in_code:
                out.append(Block("code", "\n".join(code)))
                code.clear()
            in_code = not in_code
            i += 1
            continue
        if in_code:
            code.append(ln)
            i += 1
            continue
        if not ln.strip():
            flush()
            i += 1
            continue
        if re.fullmatch(r"\s*<!--\s*(page|pagebreak|saut de page)\s*-->\s*", ln, flags=re.I):
            flush()
            out.append(Block("pagebreak"))
            i += 1
            continue
        m = re.match(r"\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", ln)
        if m:
            flush()
            lvl = len(m.group(1))
            txt = re.sub(r"[*_`]", "", m.group(2)).strip()
            if lvl == 1 and not seen_title:
                seen_title = True
                out.append(Block("title", txt))
            else:
                out.append(Block("heading", txt, level=min(3, max(1, lvl - 1 if seen_title else lvl))))
            i += 1
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not _TABLE_SEP.match(lines[i]):
                    rows.append(split_row(lines[i]))
                i += 1
            if rows:
                out.append(Block("table", rows=rows))
            continue
        m = re.match(r"(\s*)([-*+]|\d+[.)])\s+(.+)$", ln)
        if m and not _HR.match(ln):
            flush()
            depth = len(m.group(1).replace("\t", "    ")) // 2
            mark = "•" if m.group(2) in "-*+" else m.group(2)
            item = re.sub(r"^\[( |x|X)\]\s+", lambda c: "☑ " if c.group(1).strip() else "☐ ", m.group(3))
            out.append(Block("item", item, level=min(depth, 4), mark=mark))
            i += 1
            continue
        if _HR.match(ln):
            flush()
            if hr_breaks:
                out.append(Block("pagebreak"))
            i += 1
            continue
        para.append(re.sub(r"^\s*>\s?", "", ln))
        i += 1
    flush()
    if in_code and code:
        out.append(Block("code", "\n".join(code)))
    return out


_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|\*[^*\s][^*]*?\*|`[^`]+`)")


def inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """→ [(text, bold, italic)] — links become « label (url) »."""
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text or "")
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        b = i = False
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            part, b = part[2:-2], True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, i = part[1:-1], True
        elif part.startswith("`") and part.endswith("`"):
            part = part[1:-1]
        out.append((part, b, i))
    return out


def plain_text(text: str) -> str:
    return "".join(p for p, _b, _i in inline_runs(text))


# ---- Swiss figures -------------------------------------------------------------------------
def fmt_number(v: float, decimals: int = 2) -> str:
    """8120.5 → « 8'120.50 » (Swiss: apostrophe for thousands, point for decimals)."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return ""
    neg = v < 0
    s = f"{abs(v):,.{decimals}f}".replace(",", "'")
    return ("-" if neg else "") + s


_CURRENCY = r"(?:CHF|Fr\.|EUR|€|USD|\$)"
_NUM_CH = re.compile(rf"^\s*(?P<cur1>{_CURRENCY})?\s*(?P<num>[-−+]?\s*\d{{1,3}}(?:['’   ]\d{{3}})+(?:[.,]\d+)?|[-−+]?\s*\d+(?:[.,]\d+)?)"
                     rf"\s*(?:\.[-–—])?\s*(?P<cur2>{_CURRENCY})?\s*(?P<pct>%)?\s*$")
_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")


@dataclass
class Cell:
    value: object = None          # str | float | int | datetime.date | None
    formula: str = ""             # Excel formula (sheet coordinates), without the « = »
    numfmt: str = ""
    bold: bool = False
    computed: object = None       # the formula's value, computed here
    bad: bool = False             # a formula SOKKAN cannot place (an unknown [name]): kept as text


def parse_value(raw: str) -> Cell:
    """A cell as the agent wrote it → a typed cell. Identifiers with leading zeros, phone
    numbers and references stay text."""
    s = (raw or "").strip()
    bold = False
    m = re.fullmatch(r"(\*\*|__)(.+)\1", s)
    if m:
        s, bold = m.group(2).strip(), True
    if not s:
        return Cell(None, bold=bold)
    if s.startswith("="):
        return Cell(None, formula=s[1:].strip(), bold=bold)
    d = _DATE.match(s)
    if d:
        try:
            return Cell(_dt.date(int(d.group(3)), int(d.group(2)), int(d.group(1))), numfmt="DD.MM.YYYY", bold=bold)
        except ValueError:
            return Cell(s, bold=bold)
    m = _NUM_CH.match(s)
    if m:
        num = re.sub(r"[\s'’  ]", "", m.group("num")).replace("−", "-")
        digits = num.lstrip("+-")
        if re.fullmatch(r"0\d+", digits.split(".")[0].split(",")[0]):
            return Cell(s, bold=bold)                 # 0041…, 007: an identifier
        if "," in num and "." not in num:
            ip, dp = num.split(",", 1)
            if len(dp) == 3 and not m.group("cur1") and not m.group("cur2"):
                num = ip + dp                         # « 1,250 » = thousands (English)
            else:
                num = ip + "." + dp                   # « 8 120,50 » = French decimals
        else:
            num = num.replace(",", "")
        try:
            v = float(num)
        except ValueError:
            return Cell(s, bold=bold)
        dec = len(num.split(".", 1)[1]) if "." in num else 0
        cur = m.group("cur1") or m.group("cur2")
        if m.group("pct"):
            return Cell(v / 100, numfmt="0.0%" if dec else "0%", bold=bold)
        if cur:
            return Cell(v, numfmt=f'"{cur.replace(chr(34), "")}" #,##0.00' if m.group("cur1") else f'#,##0.00 "{cur}"',
                        bold=bold)
        if dec:
            return Cell(v, numfmt="#,##0.00", bold=bold)
        iv = int(v)
        return Cell(iv, numfmt="#,##0" if abs(iv) >= 1000 else "0", bold=bold)
    return Cell(s, bold=bold)


# ---- spreadsheet model ---------------------------------------------------------------------
def col_letter(i: int) -> str:
    """0 → A"""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


@dataclass
class Table:
    header: list
    rows: list                     # list[list[Cell]]
    top: int = 0                   # sheet row (1-based) of the header


@dataclass
class Sheet:
    name: str
    items: list = field(default_factory=list)   # ("text", str, bold) | ("table", Table)
    cells: dict = field(default_factory=dict)   # (row, col0) → Cell, filled by layout()
    widths: dict = field(default_factory=dict)
    header_rows: list = field(default_factory=list)
    total_rows: list = field(default_factory=list)
    freeze: int = 0
    formulas: list = field(default_factory=list)  # (table, row, col) of each formula, as written


_TOTAL_WORDS = re.compile(r"^\s*(\*\*)?\s*(total|sous-total|subtotal|totaux|solde|somme|summe|gesamt)", re.I)


def _sheet_name(s: str, used: set) -> str:
    n = re.sub(r"[\[\]:*?/\\]", " ", plain_text(s)).strip()[:31] or "Feuille"
    base, k = n, 2
    while n.lower() in used:
        suffix = f" ({k})"
        n, k = base[: 31 - len(suffix)] + suffix, k + 1
    used.add(n.lower())
    return n


def workbook_model(name: str, text: str) -> tuple[str, list[Sheet]]:
    """The source → (title, sheets) with every cell placed and every formula translated to
    sheet coordinates."""
    used: set = set()
    if (name or "").lower().endswith(".csv"):
        rows = csv_rows(text)
        title = re.sub(r"\.csv$", "", name or "", flags=re.I)
        sh = Sheet(_sheet_name(title or "Tableau", used))
        if rows:
            sh.items.append(("table", Table(rows[0], [[parse_value(c) for c in r] for r in rows[1:]])))
        sheets = [sh]
    else:
        _meta, body = front_matter(text)
        title, sheets, cur = "", [], None
        for b in blocks(body):
            if b.kind == "title":
                title = b.text
                continue
            if b.kind == "heading" and b.level == 1:
                cur = Sheet(_sheet_name(b.text, used))
                sheets.append(cur)
                continue
            if cur is None:
                cur = Sheet(_sheet_name(title or "Feuille 1", used))
                sheets.append(cur)
            if b.kind == "table":
                cur.items.append(("table", Table(b.rows[0], [[parse_value(c) for c in r] for r in b.rows[1:]])))
            elif b.kind == "heading":
                cur.items.append(("text", b.text, True))
            elif b.kind in ("para", "item", "code"):
                cur.items.append(("text", (f"{b.mark} " if b.kind == "item" else "") + plain_text(b.text), False))
        sheets = [s for s in sheets if s.items] or [Sheet(_sheet_name(title or "Feuille 1", set()))]
    is_csv = (name or "").lower().endswith(".csv")
    for sh in sheets:
        # a CSV table stays a table: no title row above its header
        layout(sh, title if sh is sheets[0] and not is_csv else "")
    finish(sheets)
    return title, sheets


def finish(sheets: list[Sheet]) -> None:
    """Every sheet laid out: formulas to sheet coordinates (another sheet's table included),
    then computed."""
    for sh in sheets:
        for tb, r, c in sh.formulas:
            cell = sh.cells[(r, c)]
            cell.formula = translate(cell.formula, tb, r, c, sheets)
            if "[" in cell.formula or "{{" in cell.formula:
                cell.bad = True
        sh.formulas = []
    evaluate(sheets)


def layout(sh: Sheet, title: str) -> None:
    r = 1
    if title:
        sh.cells[(r, 0)] = Cell(title, bold=True)
        sh.title_row = r
        r += 2
    first_table = True
    for it in sh.items:
        if it[0] == "text":
            sh.cells[(r, 0)] = Cell(it[1], bold=it[2])
            r += 1
            continue
        tb: Table = it[1]
        tb.top = r
        for ci, h in enumerate(tb.header):
            sh.cells[(r, ci)] = Cell(plain_text(h), bold=True)
        sh.header_rows.append(r)
        if first_table:
            sh.freeze = r + 1
            first_table = False
        for ri, row in enumerate(tb.rows):
            rr = r + 1 + ri
            for ci, c in enumerate(row):
                if c.formula:
                    sh.formulas.append((tb, rr, ci))
                sh.cells[(rr, ci)] = c
            first = row[0] if row else None
            if (first is not None and isinstance(first.value, str) and _TOTAL_WORDS.match(first.value)) \
                    or any(re.search(r"\bSUM\(\s*(ABOVE|↑)\s*\)", c.formula or "", re.I) for c in row):
                sh.total_rows.append(rr)
        r += 1 + len(tb.rows) + 1
    # widths
    for (_row, col), c in sh.cells.items():
        v = c.value
        ln = len(fmt_number(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v or ""))
        if (_row, col) == (getattr(sh, "title_row", -1), 0):
            continue
        sh.widths[col] = max(sh.widths.get(col, 8), min(60, ln + 2))


def _first_table(sh: Sheet) -> Table | None:
    return next((it[1] for it in sh.items if it[0] == "table"), None)


def translate(f: str, tb: Table, row: int, col: int, sheets: list | None = None) -> str:
    """A formula as the agent wrote it, in the TABLE's coordinates (row 1 = header, columns
    A.. = the table's columns; ``[Column]`` = this row's cell of that column;
    ``SUM(ABOVE)`` = the cells above in this column) → sheet coordinates. What is produced
    by ABOVE / [Column] is already in sheet coordinates and is kept out of the A1 pass."""
    hdr = {plain_text(h).strip().lower(): i for i, h in enumerate(tb.header)}
    first_data = tb.top + 1
    marks: list[str] = []

    def keep(s: str) -> str:
        marks.append(s)
        return f"\x00{len(marks) - 1}\x00"

    def above(m):
        fn = {"SOMME": "SUM", "MOYENNE": "AVERAGE"}.get(m.group(1).upper(), m.group(1).upper())
        if row - 1 < first_data:
            return keep(f"{fn}(0)")
        return keep(f"{fn}({col_letter(col)}{first_data}:{col_letter(col)}{row - 1})")

    f = re.sub(r"\b(SUM|SOMME|AVERAGE|MOYENNE|MIN|MAX|COUNT)\(\s*(?:ABOVE|↑|DESSUS|CI-DESSUS)\s*\)",
               above, f, flags=re.I)

    labels = {}
    for i, r in enumerate(tb.rows):
        if r and isinstance(r[0].value, str) and r[0].value.strip():
            labels.setdefault(plain_text(r[0].value).strip().lower(), tb.top + 1 + i)

    def by_name(m):
        key = plain_text(m.group(1)).strip().lower()
        if key in hdr:                       # a column: this row's cell of it
            return keep(f"{col_letter(hdr[key])}{row}")
        if key in labels:                    # a row (« [Total facturé] »): its cell in this column
            return keep(f"{col_letter(col)}{labels[key]}")
        return m.group(0)

    f = re.sub(r"\[([^\[\]]+)\]", by_name, f)

    def other_sheet(m):
        """'Sheet'!D2:D6 — counted in THAT sheet's first table, like the agent's own refs."""
        name = m.group(1).strip("'")
        target = next((x for x in (sheets or []) if x.name.lower() == name.lower()), None)
        t2 = _first_table(target) if target else None
        if t2 is None:
            return keep(m.group(0))
        shift = t2.top - 1
        quoted = "'" + target.name.replace("'", "''") + "'"
        ref = re.sub(r"(\$?)([A-Z]{1,3})(\$?)(\d+)",
                     lambda r: f"{r.group(1)}{r.group(2)}{r.group(3)}{int(r.group(4)) + shift}", m.group(2))
        return keep(f"{quoted}!{ref}")

    f = re.sub(r"('[^']+'|[A-Za-zÀ-ÿ_][\wÀ-ÿ.]*)!(\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)",
               other_sheet, f)
    f = re.sub(r"\bSOMME\(", "SUM(", f, flags=re.I)
    f = re.sub(r"\bMOYENNE\(", "AVERAGE(", f, flags=re.I)
    f = re.sub(r"\bARRONDI\(", "ROUND(", f, flags=re.I)
    f = re.sub(r"\bSI\(", "IF(", f, flags=re.I)
    f = re.sub(r"\b([a-z][a-z0-9.]*)\(", lambda m: m.group(1).upper() + "(", f)

    def a1(m):
        if m.group(1):
            return m.group(0)
        return f"{m.group(2)}{m.group(3)}{m.group(4)}{tb.top + int(m.group(5)) - 1}"

    f = re.sub(r"((?:'[^']+'|[A-Za-z_][\w.]*)!)?(\$?)\b([A-Z]{1,3})(\$?)(\d{1,6})\b(?!\()", a1, f)
    f = re.sub(r"\x00(\d+)\x00", lambda m: marks[int(m.group(1))], f)
    return f


# ---- evaluation (the values stored with the formulas, and shown in the preview) ------------
class _Err(Exception):
    pass


_TOKEN = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
                    r"|(?P<sheet>'(?:[^']|'')+'|[A-Za-zÀ-ÿ_][\wÀ-ÿ.]*)!(?P<xref>\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)"
                    r"|(?P<ref>\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)"
                    r"|(?P<fn>[A-Z][A-Z0-9.]*)\s*\(|(?P<op><=|>=|<>|[-+*/^(),;<>=&%]))")


class _Eval:
    """A small, safe evaluator: + - * / ^ %, comparisons, cell references and ranges of the
    same sheet, SUM AVERAGE MIN MAX COUNT ROUND ABS IF. Anything else → not computed here
    (Excel computes it when the file opens)."""

    def __init__(self, sheet: Sheet, book: dict | None = None):
        self.sh = sheet
        self.book = book or {sheet.name.lower(): sheet}
        self.busy: set = set()

    def other(self, name: str) -> "_Eval":
        sh = self.book.get(name.strip("'").replace("''", "'").lower())
        if sh is None:
            raise _Err("#REF! unknown sheet")
        ev = _Eval(sh, self.book)
        ev.busy = self.busy
        return ev

    def cell(self, ref: str):
        m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d+)", ref)
        key = (int(m.group(2)), col_index(m.group(1)))
        c = self.sh.cells.get(key)
        key = (id(self.sh),) + key
        if c is None:
            return None
        if c.formula:
            if c.computed is None:
                if key in self.busy:
                    raise _Err("cycle")
                self.busy.add(key)
                sub = _Eval(self.sh, self.book)   # run() is not reentrant: one evaluator per formula
                sub.busy = self.busy
                try:
                    c.computed = sub.run(c.formula)
                finally:
                    self.busy.discard(key)
            return c.computed
        v = c.value
        if isinstance(v, _dt.date):
            return float((v - _dt.date(1899, 12, 30)).days)
        return v

    def rng(self, ref: str) -> list:
        a, b = ref.replace("$", "").split(":")
        ma, mb = re.fullmatch(r"([A-Z]{1,3})(\d+)", a), re.fullmatch(r"([A-Z]{1,3})(\d+)", b)
        c1, c2 = sorted((col_index(ma.group(1)), col_index(mb.group(1))))
        r1, r2 = sorted((int(ma.group(2)), int(mb.group(2))))
        if (r2 - r1 + 1) * (c2 - c1 + 1) > 100_000:
            raise _Err("range too large")
        return [self.cell(f"{col_letter(c)}{r}") for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]

    def run(self, f: str):
        self.toks = []
        pos = 0
        f = f.strip()
        while pos < len(f):
            m = _TOKEN.match(f, pos)
            if not m or m.end() == pos:
                raise _Err(f"cannot read {f[pos:pos + 10]!r}")
            pos = m.end()
            if m.group("num"):
                self.toks.append(("num", float(m.group("num"))))
            elif m.group("xref"):
                self.toks.append(("xref", (m.group("sheet"), m.group("xref"))))
            elif m.group("ref"):
                self.toks.append(("ref", m.group("ref")))
            elif m.group("fn"):
                self.toks.append(("fn", m.group("fn")))
            else:
                self.toks.append(("op", m.group("op")))
        self.i = 0
        v = self.compare()
        if self.i != len(self.toks):
            raise _Err("trailing")
        return v

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, op=None):
        t = self.peek()
        if op is not None and t != ("op", op):
            raise _Err(f"expected {op}")
        self.i += 1
        return t

    @staticmethod
    def num(v) -> float:
        if v is None or v == "":
            return 0.0
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        raise _Err("text in a calculation")

    def compare(self):
        a = self.concat()
        while self.peek()[0] == "op" and self.peek()[1] in ("<", ">", "<=", ">=", "=", "<>"):
            op = self.take()[1]
            b = self.concat()
            x, y = (self.num(a), self.num(b)) if not (isinstance(a, str) or isinstance(b, str)) else (str(a), str(b))
            a = {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y, "=": x == y, "<>": x != y}[op]
        return a

    def concat(self):
        a = self.add()
        while self.peek() == ("op", "&"):
            self.take()
            b = self.add()
            a = f"{'' if a is None else a}{'' if b is None else b}"
        return a

    def add(self):
        a = self.mul()
        while self.peek()[0] == "op" and self.peek()[1] in "+-" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            b = self.mul()
            a = self.num(a) + self.num(b) if op == "+" else self.num(a) - self.num(b)
        return a

    def mul(self):
        a = self.power()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            b = self.power()
            if op == "*":
                a = self.num(a) * self.num(b)
            else:
                d = self.num(b)
                if d == 0:
                    raise _Err("#DIV/0!")
                a = self.num(a) / d
        return a

    def power(self):
        a = self.unary()
        while self.peek() == ("op", "^"):
            self.take()
            a = self.num(a) ** self.num(self.unary())
        return a

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return -self.num(self.unary())
        if self.peek() == ("op", "+"):
            self.take()
            return self.num(self.unary())
        v = self.atom()
        while self.peek() == ("op", "%"):
            self.take()
            v = self.num(v) / 100
        return v

    def args(self) -> list:
        out = []
        if self.peek() == ("op", ")"):
            self.take()
            return out
        while True:
            t = self.peek()
            if t[0] == "ref" and ":" in t[1]:
                self.take()
                out.append(self.rng(t[1]))
            elif t[0] == "xref" and ":" in t[1][1]:
                self.take()
                out.append(self.other(t[1][0]).rng(t[1][1]))
            else:
                out.append(self.compare())
            t = self.take()
            if t == ("op", ")"):
                return out
            if t not in (("op", ","), ("op", ";")):
                raise _Err("expected , or )")

    def atom(self):
        t = self.take()
        if t[0] == "num":
            return t[1]
        if t[0] == "xref":
            if ":" in t[1][1]:
                raise _Err("range outside a function")
            return self.other(t[1][0]).cell(t[1][1])
        if t[0] == "ref":
            if ":" in t[1]:
                raise _Err("range outside a function")
            return self.cell(t[1])
        if t == ("op", "("):
            v = self.compare()
            self.take(")")
            return v
        if t[0] == "fn":
            name = t[1].upper()
            args = self.args()
            flat = []
            for a in args:
                flat.extend(a if isinstance(a, list) else [a])
            nums = [float(x) for x in flat if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if name == "SUM":
                return sum(nums)
            if name == "AVERAGE":
                if not nums:
                    raise _Err("#DIV/0!")
                return sum(nums) / len(nums)
            if name == "MIN":
                return min(nums) if nums else 0.0
            if name == "MAX":
                return max(nums) if nums else 0.0
            if name == "COUNT":
                return float(len(nums))
            if name == "ABS":
                return abs(self.num(args[0]))
            if name == "ROUND":
                d = int(self.num(args[1])) if len(args) > 1 else 0
                x = self.num(args[0]) * (10 ** d)
                return (math.floor(abs(x) + 0.5) * (1 if x >= 0 else -1)) / (10 ** d)
            if name == "IF":
                return args[1] if args[0] else (args[2] if len(args) > 2 else False)
            raise _Err(f"function {name}")
        raise _Err("unexpected")


def evaluate(sheets: list[Sheet]) -> None:
    for sh in sheets:
        ev = _Eval(sh, {x.name.lower(): x for x in sheets})
        for (_r, _c), cell in sorted(sh.cells.items()):
            if cell.formula and cell.computed is None and not cell.bad:
                try:
                    v = ev.run(cell.formula)
                    cell.computed = round(v, 10) if isinstance(v, float) else v
                except (_Err, RecursionError, OverflowError, ValueError, TypeError, ZeroDivisionError,
                        AttributeError, IndexError):
                    cell.computed = None
    for sh in sheets:
        for it in sh.items:
            if it[0] == "table":
                _column_formats(sh, it[1], {x.name.lower(): x for x in sheets})


def _referenced_format(sh: Sheet, formula: str, book: dict) -> str:
    """The number format of the first cell a formula reads that has one (a balance computed
    from amounts shows two decimals like them)."""
    for m in re.finditer(r"(?:('(?:[^']|'')+'|[A-Za-zÀ-ÿ_][\wÀ-ÿ.]*)!)?\$?([A-Z]{1,3})\$?(\d+)", formula):
        target = book.get(m.group(1).strip("'").replace("''", "'").lower()) if m.group(1) else sh
        c = target.cells.get((int(m.group(3)), col_index(m.group(2)))) if target else None
        if c is not None and c.numfmt and "0.00" in c.numfmt:
            return "#,##0.00"
    return ""


def _column_formats(sh: Sheet, tb: Table, book: dict | None = None) -> None:
    """One format per column of a table: amounts written « 18'350.00 », « 31'200 » and
    « CHF 8'120.50 » in one column all show two decimals; a column of formulas takes two
    decimals when the table holds amounts (a balance), none for a count."""
    money_table = False
    cols: dict[int, list[Cell]] = {}
    for ri in range(len(tb.rows)):
        for ci in range(len(tb.header)):
            c = sh.cells.get((tb.top + 1 + ri, ci))
            if c is not None:
                cols.setdefault(ci, []).append(c)
    fmts: dict[int, str] = {}
    book = book or {sh.name.lower(): sh}
    for ci, cells in cols.items():
        nums = [c for c in cells if not c.formula and isinstance(c.value, (int, float))
                and not isinstance(c.value, bool) and "%" not in (c.numfmt or "")
                and "YY" not in (c.numfmt or "")]
        if not nums:
            continue
        curs = {re.search(r'"([^"]+)"', c.numfmt or "").group(0) for c in nums if '"' in (c.numfmt or "")}
        dec = any("0.00" in (c.numfmt or "") for c in nums)
        if dec:
            money_table = True
            f = "#,##0.00"
            if len(curs) == 1 and all('"' in (c.numfmt or "") for c in nums):
                f = nums[0].numfmt
        else:
            f = "#,##0"
        fmts[ci] = f
        for c in nums:
            c.numfmt = f
    for ci, cells in cols.items():
        for c in cells:
            if c.formula and not c.numfmt:
                inherited = _referenced_format(sh, c.formula, book)
                if re.match(r"\s*COUNT\(", c.formula, re.I):
                    c.numfmt = "#,##0"
                elif inherited and not fmts.get(ci):
                    c.numfmt = inherited
                elif isinstance(c.computed, (int, float)) and not isinstance(c.computed, bool):
                    c.numfmt = fmts.get(ci) or ("#,##0.00" if money_table or c.computed != int(c.computed)
                                                else "#,##0")


def show(cell: Cell) -> str:
    """A cell as the person reads it (preview, PDF): Swiss figures."""
    v = cell.computed if cell.formula else cell.value
    if cell.bad:
        return "(formule à revoir)"
    if cell.formula and v is None:
        return "(calculé à l'ouverture)"
    if v is None:
        return ""
    if isinstance(v, bool):
        return "VRAI" if v else "FAUX"
    if isinstance(v, _dt.date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, (int, float)):
        f = cell.numfmt or ""
        if "%" in f:
            return fmt_number(v * 100, 1 if "0.0%" in f else 0) + " %"
        dec = 2 if "0.00" in f else (0 if f else (2 if isinstance(v, float) and v != int(v) else 0))
        s = fmt_number(float(v), dec)
        cur = re.search(r'"([^"]+)"', f)
        if cur:
            s = f"{cur.group(1)} {s}" if f.startswith('"') else f"{s} {cur.group(1)}"
        return s
    return str(v)


# ---- xlsx ----------------------------------------------------------------------------------
def to_xlsx(name: str, text: str) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError as e:
        raise Unavailable("openpyxl") from e
    title, sheets = workbook_model(name, text)
    wb = Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="E8ECF1")
    thin = Side(style="thin", color="7F8C99")
    for sh in sheets:
        ws = wb.create_sheet(sh.name)
        for (r, c), cell in sorted(sh.cells.items()):
            x = ws.cell(row=r, column=c + 1)
            if cell.bad:
                x.value = "=" + cell.formula
                x.data_type = "s"        # shown as written: never an invalid formula in the file
            elif cell.formula:
                x.value = "=" + cell.formula
            else:
                x.value = cell.value
            if cell.numfmt:
                x.number_format = cell.numfmt
            bold = cell.bold or r in sh.header_rows or r in sh.total_rows
            if (r, c) == (getattr(sh, "title_row", -1), 0):
                x.font = Font(bold=True, size=14)
            elif bold:
                x.font = Font(bold=True)
            if r in sh.header_rows:
                x.fill = head_fill
                x.border = Border(bottom=thin)
                x.alignment = Alignment(vertical="center", wrap_text=True)
            if r in sh.total_rows:
                x.border = Border(top=thin)
        for c, w in sh.widths.items():
            ws.column_dimensions[col_letter(c)].width = w
        if sh.freeze:
            ws.freeze_panes = f"A{sh.freeze}"
        ws.sheet_view.zoomScale = 100
        ws.page_setup.orientation = "landscape" if max(sh.widths or {0: 0}) >= 6 else "portrait"
        ws.page_setup.fitToWidth = 1
    if title:
        wb.properties.title = title
    wb.properties.creator = "SOKKAN"
    try:
        wb.calculation.fullCalcOnLoad = True     # Excel / LibreOffice recompute on opening
    except AttributeError:
        pass
    buf = io.BytesIO()
    wb.save(buf)
    return _with_cached_values(buf.getvalue(), sheets)


def _with_cached_values(data: bytes, sheets: list[Sheet]) -> bytes:
    """openpyxl writes a formula without its value: a viewer that does not recalculate (a mail
    preview, a phone) shows an empty cell. Put the value computed here next to each formula."""
    vals: dict[int, dict[str, object]] = {}
    for i, sh in enumerate(sheets, 1):
        for (r, c), cell in sh.cells.items():
            if cell.formula and cell.computed is not None:
                vals.setdefault(i, {})[f"{col_letter(c)}{r}"] = cell.computed
    if not vals:
        return data
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            raw = src.read(info.filename)
            m = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", info.filename)
            if m and int(m.group(1)) in vals:
                table = vals[int(m.group(1))]
                xml = raw.decode("utf-8")

                def put(mm, table=table):
                    attrs, f = mm.group(1), mm.group(2)
                    ref = re.search(r'\br="([A-Z]+\d+)"', attrs)
                    v = table.get(ref.group(1)) if ref else None
                    if v is None:
                        return mm.group(0)
                    if isinstance(v, bool):
                        return f'<c{attrs} t="b"><f>{f}</f><v>{int(v)}</v></c>'
                    if isinstance(v, (int, float)):
                        return f"<c{attrs}><f>{f}</f><v>{repr(float(v)) if v != int(v) else int(v)}</v></c>"
                    return f'<c{attrs} t="str"><f>{f}</f><v>{escape(str(v))}</v></c>'

                xml = re.sub(r"<c((?:\s+[a-z]+=\"[^\"]*\")*)\s*><f>(.*?)</f>(?:<v\s*/>|<v></v>)?</c>", put, xml)
                raw = xml.encode("utf-8")
            z.writestr(info, raw)
    return out.getvalue()


# ---- csv (a table the agent saved as such) -------------------------------------------------
def csv_rows(text: str, limit: int = 20000) -> list[list[str]]:
    """The rows of a CSV table — « ; » (Swiss Excel), « , » or tab, whichever the header uses;
    quoted cells as usual. A row with MORE cells than the header had a separator inside a
    cell that was not quoted (seen on real runs: « Repas; boissons » in a ; table): the
    surplus is joined back into the text column where the row then lines up best with the
    others (figures under figures) — `_repair`."""
    import csv
    t = (text or "").lstrip("\ufeff")
    first = t.split("\n", 1)[0]
    counts = {d: first.count(d) for d in (";", ",", "\t")}
    delim = max(counts, key=lambda d: (counts[d], d == ";"))
    if not counts[delim]:
        delim = ";"
    rows = []
    for row in csv.reader(io.StringIO(t), delimiter=delim):
        if any(c.strip() for c in row):
            rows.append([c.strip() for c in row])
        if len(rows) >= limit:
            break
    return _repair(rows, delim)


def _is_num(s: str) -> bool:
    c = parse_value(s)
    return bool(c.formula) or (isinstance(c.value, (int, float)) and not isinstance(c.value, bool))


def _repair(rows: list[list[str]], delim: str) -> list[list[str]]:
    if len(rows) < 2:
        return rows
    width = len(rows[0])
    good = [r for r in rows[1:] if len(r) == width]

    def col_numeric(c: int) -> bool:
        filled = [r[c] for r in good if r[c].strip()]
        return bool(filled) and sum(1 for x in filled if _is_num(x)) > len(filled) / 2
    numeric = [col_numeric(c) for c in range(width)]
    out = [rows[0]]
    for r in rows[1:]:
        k = len(r) - width
        if k <= 0:
            out.append(r + [""] * (-k))
            continue
        best, best_score = None, None
        for j in range(width):
            merged = r[:j] + [(delim + " ").join(x.strip() for x in r[j:j + k + 1])] + r[j + k + 1:]
            score = sum(1 for c in range(width) if _is_num(merged[c]) == numeric[c])
            score = (score, not numeric[j], j)
            if best_score is None or score > best_score:
                best, best_score = merged, score
        out.append(best)
    return out


def to_csv(name: str, text: str) -> bytes:
    """A table as CSV for Excel in Switzerland: « ; » separator, quotes where needed, UTF-8
    with BOM (Excel then reads the accents), the computed VALUES (a CSV has no formula)."""
    import csv
    _title, sheets = workbook_model(name, text)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    for k, sh in enumerate(sheets):
        if k:
            w.writerow([])
            w.writerow([sh.name])
        if not sh.cells:
            continue
        last = max(r for r, _c in sh.cells)
        ncol = max(c for _r, c in sh.cells) + 1
        for r in range(1, last + 1):
            row = []
            for c in range(ncol):
                cell = sh.cells.get((r, c))
                if cell is None:
                    row.append("")
                    continue
                v = cell.computed if cell.formula else cell.value
                if isinstance(v, float):
                    row.append(f"{v:.2f}" if "0.00" in (cell.numfmt or "") else repr(round(v, 10)).rstrip("0").rstrip(".") if v != int(v) else str(int(v)))
                elif isinstance(v, _dt.date):
                    row.append(v.strftime("%d.%m.%Y"))
                elif v is None:
                    row.append("")
                else:
                    row.append(str(v))
            while row and row[-1] == "":
                row.pop()
            w.writerow(row)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ---- tables inside a document (Word / PDF / preview): formulas computed too -----------------
def computed_rows(rows: list[list[str]]) -> list[list[str]]:
    """A Markdown table of a document with its formula cells replaced by their value (Swiss
    format), the other cells as written."""
    if not any(c.strip().startswith("=") or re.fullmatch(r"(\*\*|__)=.*\1", c.strip())
               for r in rows[1:] for c in r):
        return rows
    sh = Sheet("t")
    tb = Table(rows[0], [[parse_value(c) for c in r] for r in rows[1:]])
    sh.items.append(("table", tb))
    layout(sh, "")
    finish([sh])
    out = [rows[0]]
    for ri, r in enumerate(rows[1:]):
        line = []
        for ci, raw in enumerate(r):
            cell = sh.cells.get((tb.top + 1 + ri, ci))
            if cell is not None and cell.formula:
                v = show(cell)
                line.append(f"**{v}**" if cell.bold else v)
            else:
                line.append(raw)
        out.append(line)
    return out


def _computed_blocks(bl: list[Block]) -> list[Block]:
    for b in bl:
        if b.kind == "table":
            b.rows = computed_rows(b.rows)
    return bl


def calc_summary(name: str, text: str, limit: int = 60) -> str:
    """What SOKKAN computed in a saved document, for the AGENT (its figures come from here,
    never from its own arithmetic): one line per formula cell, labelled by its row and
    column. '' when the document has no formula."""
    lines: list[str] = []
    kind = kind_of(name, text)
    if kind == "spreadsheet":
        _t, sheets = workbook_model(name, text)
        tables = [(sh, it[1]) for sh in sheets for it in sh.items if it[0] == "table"]
    else:
        _m, body = front_matter(text)
        tables = []
        for b in blocks(body):
            if b.kind == "table" and len(b.rows) > 1 and any(
                    c.strip().lstrip("*_").startswith("=") for r in b.rows[1:] for c in r):
                sh = Sheet("Tableau")
                tb = Table(b.rows[0], [[parse_value(c) for c in r] for r in b.rows[1:]])
                sh.items.append(("table", tb))
                layout(sh, "")
                finish([sh])
                tables.append((sh, tb))
    for sh, tb in tables:
        for ri in range(len(tb.rows)):
            r = tb.top + 1 + ri
            label = show(sh.cells.get((r, 0), Cell())) or f"ligne {ri + 1}"
            for ci in range(len(tb.header)):
                cell = sh.cells.get((r, ci))
                if cell is None or not cell.formula:
                    continue
                v = (f"FORMULA NOT UNDERSTOOD (={cell.formula}): use a column name, a row label of "
                     "this table, SUM(ABOVE) or A1 counted in the table" if cell.bad else
                     show(cell) if cell.computed is not None else "not computed (Excel computes it)")
                lines.append(f"- {sh.name if sh.name != 't' else 'Tableau'} · {label} · "
                             f"{plain_text(tb.header[ci]) if ci < len(tb.header) else col_letter(ci)} = {v}")
                if len(lines) >= limit:
                    return "\n".join(lines + ["- …"])
    return "\n".join(lines)


CALC_DIR = ".calc"
# 4.0 (bench n°7): the brief tells the agent to read `.calc/<name>.txt` after every save; a
# document without a formula had no such file, and the model read a file that did not exist
# (a failure line in the thread, both engines). The file is always written.
NO_FORMULA = "No computation: this document has no formula (nothing for SOKKAN to compute).\n"


def calc_text(name: str, text: str) -> str:
    summ = calc_summary(name, text)
    if not summ:
        return ""
    return ("SOKKAN computed the formulas of « " + name + " » (these are the figures to quote; "
            "the person sees the same in the preview and in the file):\n" + summ + "\n")


def write_calc(path) -> str:
    """After a deliverable is saved: its computed figures, next to it, for the agent
    (``.calc/<name>.txt``), ALWAYS written — a document without a formula says so
    (`NO_FORMULA`). Returns the computed figures ("" without a formula: nothing to add to
    the tool's context)."""
    from pathlib import Path
    p = Path(path)
    if not p.is_file() or p.is_symlink() or p.stat().st_size > 5 * 1024 * 1024:
        return ""
    out = calc_text(p.name, p.read_text(encoding="utf-8", errors="replace"))
    d = p.parent / CALC_DIR
    target = d / (p.name + ".txt")
    d.mkdir(exist_ok=True)
    tmp = d / f".{p.name}.tmp"
    tmp.write_text(out or NO_FORMULA, encoding="utf-8")
    tmp.replace(target)
    return out


# ---- letters (one document per recipient) --------------------------------------------------
_FIELD = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_RECIPIENT_HEAD = re.compile(r"(destinataires?|recipients?|adressaten?|empfänger|liste|donn[ée]es)", re.I)


@dataclass
class Letter:
    name: str
    body: str                      # Markdown of this one letter


def letters(text: str) -> tuple[str, list[Letter]]:
    """→ (title of the batch, letters). A part of the source separated by a line ``---`` is
    either a TEMPLATE — ``{{Field}}`` in the text and a table whose header names those fields
    (under « Destinataires »/« Recipients », or its last table): one letter per row — or ONE
    letter. Several templates in one source are fine (a courteous reminder for two clients, a
    firmer one for a third: seen on a real run)."""
    _meta, body = front_matter(text)
    lines = body.replace("\r\n", "\n").split("\n")
    title = ""
    for k, ln in enumerate(lines):
        if ln.strip():
            m = re.match(r"\s{0,3}#\s+(.+?)\s*#*\s*$", ln)
            if m:
                title = re.sub(r"[*_`]", "", m.group(1)).strip()
                lines = lines[k + 1:]
            break
    groups, cur, in_code = [], [], False
    for ln in lines:
        if ln.strip().startswith("```"):
            in_code = not in_code
        if not in_code and _HR.match(ln):
            groups.append("\n".join(cur))
            cur = []
        else:
            cur.append(ln)
    groups.append("\n".join(cur))
    groups = [g for g in groups if g.strip()]
    out: list[Letter] = []
    i = 0
    while i < len(groups):
        g = groups[i]
        fields = {f.strip().lower() for f in _FIELD.findall(g)}
        if fields and _pick_table(g, fields) is None and i + 1 < len(groups) \
                and _pick_table(groups[i + 1], fields) is not None and not _FIELD.search(groups[i + 1]):
            g = g + "\n\n" + groups[i + 1]            # « template --- recipients table »
            i += 1
        made = _merge(g, fields) if fields else []
        if made:
            out += made
        else:
            out.append(Letter(_chunk_name(g), g.strip()))
        i += 1
    return title, out


def _pick_table(body: str, fields: set):
    pick = None
    for start, end, rows, heading in reversed(list(_md_tables(body))):
        hdr = {plain_text(h).strip().lower() for h in rows[0]}
        if fields & hdr and (pick is None or (heading and _RECIPIENT_HEAD.search(heading))):
            pick = (start, end, rows, heading)
            if heading and _RECIPIENT_HEAD.search(heading):
                break
    return pick


def _merge(body: str, fields: set) -> list[Letter]:
    pick = _pick_table(body, fields)
    if not pick:
        return []
    start, end, rows, _heading = pick
    blines = body.split("\n")
    hstart, j = start, start - 1
    while j >= 0 and not blines[j].strip():
        j -= 1
    if j >= 0 and re.match(r"\s{0,3}#{1,6}\s", blines[j]):
        hstart = j
    template = "\n".join(blines[:hstart] + blines[end:]).strip()
    template = re.sub(r"^\s{0,3}#\s+.*\n", "", template, count=1).strip() \
        if re.match(r"\s{0,3}#\s", template) else template      # a group's own « # heading »
    hdr = [plain_text(h).strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        vals = {hdr[i].lower(): plain_text(r[i]) if i < len(r) else "" for i in range(len(hdr))}
        if not any(vals.values()):
            continue
        doc = _FIELD.sub(lambda m, vals=vals: vals.get(m.group(1).strip().lower(), m.group(0)), template)
        out.append(Letter(_recipient_name(vals, hdr, doc), doc))
    return out


def _md_tables(body: str):
    lines = body.split("\n")
    i, heading = 0, ""
    while i < len(lines):
        m = re.match(r"\s{0,3}#{1,6}\s+(.+)$", lines[i])
        if m:
            heading = m.group(1)
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            start, rows = i, []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not _TABLE_SEP.match(lines[i]):
                    rows.append(split_row(lines[i]))
                i += 1
            yield start, i, rows, heading
            continue
        i += 1


def _recipient_name(vals: dict, hdr: list, doc: str) -> str:
    for k in ("destinataire", "recipient", "nom", "name", "client", "société", "societe", "entreprise",
              "company", "raison sociale", "empfänger", "firma"):
        if vals.get(k):
            return vals[k]
    return next((v for v in vals.values() if v), "") or _chunk_name(doc)


def _chunk_name(chunk: str) -> str:
    for ln in chunk.split("\n"):
        s = plain_text(re.sub(r"^\s{0,3}#{1,6}\s+", "", ln)).strip()
        s = re.sub(r"^(lettre|letter|brief)\s*\d*\s*[:—–-]\s*", "", s, flags=re.I)
        s = re.sub(r"^(à|a|to|an)\s*:\s*", "", s, flags=re.I)
        if s:
            return s[:60]
    return "Lettre"


def file_stem(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = re.sub(r'[<>:"|?*\\/]', " ", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(". ").rstrip(". ")
    return s[:80] or "Document"


# ---- docx (python-docx, on the organisation's template when there is one) -----------------
def _open_template(path):
    """A Document from a .docx or .dotx template, its body emptied (styles, page header and
    footer, logo, margins kept)."""
    from docx import Document
    if path is None:
        return Document(), False
    raw = open(path, "rb").read()
    if b"wordprocessingml.template.main+xml" in raw[:200_000] or True:
        # a .dotx declares its main part as a « template »: python-docx only opens documents
        src = zipfile.ZipFile(io.BytesIO(raw))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for info in src.infolist():
                d = src.read(info.filename)
                if info.filename == "[Content_Types].xml":
                    d = d.replace(b"wordprocessingml.template.main+xml", b"wordprocessingml.document.main+xml")
                z.writestr(info, d)
        buf.seek(0)
        doc = Document(buf)
    body = doc.element.body
    for child in list(body):
        if not child.tag.endswith("}sectPr"):
            body.remove(child)
    return doc, True


def _style(doc, *names):
    for n in names:
        try:
            return doc.styles[n]
        except KeyError:
            continue
    return None


def _add_runs(p, text: str, bold: bool = False):
    for part, b, i in inline_runs(text):
        r = p.add_run(part)
        if b or bold:
            r.bold = True
        if i:
            r.italic = True


def _is_figure(s: str) -> bool:
    c = parse_value(s)
    return isinstance(c.value, (int, float)) and not isinstance(c.value, bool) and not c.formula


def _field(run, instr: str):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    for kind, text in (("begin", None), (None, instr), ("separate", None), (None, "1"), ("end", None)):
        if kind:
            e = OxmlElement("w:fldChar")
            e.set(qn("w:fldCharType"), kind)
            run._r.append(e)
        elif text == instr:
            e = OxmlElement("w:instrText")
            e.set(qn("xml:space"), "preserve")
            e.text = f" {instr} "
            run._r.append(e)
        else:
            e = OxmlElement("w:t")
            e.text = text
            run._r.append(e)


def _default_frame(doc, org: str, title: str, logo: bytes | None = None):
    """No template: a clean page — A4, margins, Calibri 11, the organisation in the header,
    « title · page X / Y » in the footer."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.left_margin = sec.right_margin = Cm(2.5)
    sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2.0)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    if logo:
        # 4.0: the organisation profile's logo (orgprofile.logo_path), left of the header
        try:
            lp = sec.header.paragraphs[0]
            lp.text = ""
            lp.add_run().add_picture(io.BytesIO(logo), height=Cm(1.2))
            lp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            if org:
                sec.header.add_paragraph()
        except Exception:  # noqa: BLE001 — an image docx cannot read: no logo, never a broken file
            pass
    if org:
        hp = sec.header.paragraphs[-1]
        hp.text = ""
        r = hp.add_run(org)
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x55, 0x5F, 0x6B)
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    fp = sec.footer.paragraphs[0]
    fp.text = ""
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for txt, instr in (((title + " · ") if title else "", None), ("Page ", None), (None, "PAGE"),
                       (" / ", None), (None, "NUMPAGES")):
        r = fp.add_run(txt or "")
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x55, 0x5F, 0x6B)
        if instr:
            _field(r, instr)


def _render_blocks(doc, bl: list[Block]):
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt
    for b in bl:
        if b.kind == "title":
            st = _style(doc, "Title")
            p = doc.add_paragraph(style=st) if st is not None else doc.add_paragraph()
            _add_runs(p, b.text, bold=st is None)
        elif b.kind == "heading":
            st = _style(doc, f"Heading {b.level}", "Heading 1")
            p = doc.add_paragraph(style=st) if st is not None else doc.add_paragraph()
            _add_runs(p, b.text, bold=st is None)
        elif b.kind == "para":
            p = doc.add_paragraph()
            _add_runs(p, b.text)
        elif b.kind == "code":
            for ln in b.text.split("\n"):
                p = doc.add_paragraph()
                r = p.add_run(ln)
                r.font.name = "Consolas"
                r.font.size = Pt(9)
        elif b.kind == "item":
            if b.mark == "•":
                st = _style(doc, "List Bullet" + (f" {b.level + 1}" if b.level else ""), "List Bullet")
                if st is not None:
                    p = doc.add_paragraph(style=st)
                    if b.level and st.name == "List Bullet":
                        p.paragraph_format.left_indent = Cm(0.63 * (b.level + 1))
                    _add_runs(p, b.text)
                    continue
            p = doc.add_paragraph(style=_style(doc, "List Paragraph"))
            p.paragraph_format.left_indent = Cm(0.75 + 0.63 * b.level)
            p.paragraph_format.first_line_indent = Cm(-0.6)
            p.add_run(f"{b.mark}\t")
            _add_runs(p, b.text)
        elif b.kind == "pagebreak":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        elif b.kind == "table":
            rows = b.rows
            ncol = max(len(r) for r in rows)
            t = doc.add_table(rows=len(rows), cols=ncol)
            st = _style(doc, "Table Grid")
            if st is not None:
                t.style = st
            figures = [all(_is_figure(r[c]) for r in rows[1:] if c < len(r) and r[c].strip())
                       and any(c < len(r) and r[c].strip() for r in rows[1:]) for c in range(ncol)]
            for ri, r in enumerate(rows):
                for ci in range(ncol):
                    cell = t.cell(ri, ci)
                    p = cell.paragraphs[0]
                    txt = r[ci] if ci < len(r) else ""
                    total = bool(_TOTAL_WORDS.match(r[0] if r else ""))
                    _add_runs(p, txt, bold=ri == 0 or total)
                    if figures[ci] and ri > 0:
                        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                if ri == 0:
                    trpr = t.rows[0]._tr.get_or_add_trPr()
                    h = OxmlElement("w:tblHeader")
                    h.set(qn("w:val"), "true")
                    trpr.append(h)
                    for ci in range(ncol):
                        tcpr = t.cell(0, ci)._tc.get_or_add_tcPr()
                        shd = OxmlElement("w:shd")
                        shd.set(qn("w:val"), "clear")
                        shd.set(qn("w:color"), "auto")
                        shd.set(qn("w:fill"), "E8ECF1")
                        tcpr.append(shd)
            doc.add_paragraph()


def to_docx(text: str, template=None, org: str = "", hr_breaks: bool = False, logo: bytes | None = None) -> bytes:
    try:
        import docx  # noqa: F401
    except ImportError:
        import simple
        _meta, body = front_matter(text)
        return simple.markdown_to_docx(body)
    _meta, body = front_matter(text)
    doc, templated = _open_template(template)
    bl = blocks(body, hr_breaks=hr_breaks)
    title = next((b.text for b in bl if b.kind == "title"), "")
    if not templated:
        _default_frame(doc, org, plain_text(title), logo)
    if title:
        doc.core_properties.title = plain_text(title)[:200]
    doc.core_properties.author = org or "SOKKAN"
    _render_blocks(doc, _computed_blocks(bl))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---- pdf (reportlab) -----------------------------------------------------------------------
_FONTS_DONE = False


def _fonts() -> tuple[str, str, str, str]:
    """Bitstream Vera (shipped with reportlab): every Latin character, « » ’ € included."""
    global _FONTS_DONE
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        if not _FONTS_DONE:
            for n, f in (("Vera", "Vera.ttf"), ("Vera-Bold", "VeraBd.ttf"), ("Vera-Italic", "VeraIt.ttf"),
                         ("Vera-BoldItalic", "VeraBI.ttf")):
                pdfmetrics.registerFont(TTFont(n, f))
            pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="Vera-Bold", italic="Vera-Italic",
                                          boldItalic="Vera-BoldItalic")
            _FONTS_DONE = True
        return "Vera", "Vera-Bold", "Vera-Italic", "Vera-BoldItalic"
    except Exception:  # noqa: BLE001 — the standard fonts then (Latin-1)
        return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"


def _rl(text: str) -> str:
    out = []
    for part, b, i in inline_runs(text):
        s = escape(part)
        if b:
            s = f"<b>{s}</b>"
        if i:
            s = f"<i>{s}</i>"
        out.append(s)
    return "".join(out)


def template_frame(template) -> dict:
    """What a PDF takes from the Word template: the first image of a page header (the logo)
    and the text of the footer."""
    out: dict = {"logo": None, "footer": ""}
    if template is None:
        return out
    try:
        with zipfile.ZipFile(template) as z:
            names = z.namelist()
            for n in sorted(x for x in names if re.fullmatch(r"word/_rels/header\d*\.xml\.rels", x)):
                rels = z.read(n).decode("utf-8", "replace")
                m = re.search(r'Type="[^"]+/image"\s+Target="([^"]+)"', rels) or \
                    re.search(r'Target="(media/[^"]+)"', rels)
                if m:
                    target = "word/" + m.group(1).lstrip("/").replace("../", "")
                    if target in names and z.getinfo(target).file_size < 5_000_000:
                        out["logo"] = z.read(target)
                        break
            for n in sorted(x for x in names if re.fullmatch(r"word/footer\d*\.xml", x)):
                xml = z.read(n).decode("utf-8", "replace")
                xml = re.sub(r"<w:instrText[^>]*>.*?</w:instrText>", "", xml, flags=re.S)
                paras = []
                for para in xml.split("</w:p>"):
                    t = "".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", para)).strip()
                    if t:
                        paras.append(t)
                if paras:
                    import html as _html
                    out["footer"] = _html.unescape(" · ".join(paras))[:300]
                    break
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return out


def to_pdf(text: str, template=None, org: str = "", name: str = "", hr_breaks: bool = False,
           logo: bytes | None = None) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (PageBreak, Paragraph, Preformatted, SimpleDocTemplate,
                                        Spacer, Table as RLTable, TableStyle)
    except ImportError as e:
        raise Unavailable("reportlab") from e
    regular, bold, _it, _bi = _fonts()
    ss = getSampleStyleSheet()
    base = ParagraphStyle("b", parent=ss["Normal"], fontName=regular, fontSize=10.5, leading=14, spaceAfter=6)
    st = {
        "title": ParagraphStyle("t", parent=base, fontName=bold, fontSize=18, leading=22, spaceAfter=12),
        1: ParagraphStyle("h1", parent=base, fontName=bold, fontSize=14, leading=18, spaceBefore=10, spaceAfter=6),
        2: ParagraphStyle("h2", parent=base, fontName=bold, fontSize=12, leading=16, spaceBefore=8, spaceAfter=4),
        3: ParagraphStyle("h3", parent=base, fontName=bold, fontSize=11, leading=14, spaceBefore=6, spaceAfter=3),
        "cell": ParagraphStyle("c", parent=base, fontSize=9, leading=11, spaceAfter=0),
        "cellr": ParagraphStyle("cr", parent=base, fontSize=9, leading=11, spaceAfter=0, alignment=TA_RIGHT),
        "code": ParagraphStyle("code", parent=base, fontName="Courier", fontSize=8.5, leading=10),
    }
    frame = template_frame(template)
    if not frame["logo"] and logo:
        frame["logo"] = logo          # 4.0: the organisation profile's logo when the template has none
    spreadsheet = kind_of(name, text) == "spreadsheet"
    flow = []
    wide = False
    if spreadsheet:
        title, sheets = workbook_model(name, text)
        if title:
            flow.append(Paragraph(_rl(title), st["title"]))
        for sh in sheets:
            if len(sheets) > 1:
                flow.append(Paragraph(_rl(sh.name), st[1]))
            ncols = (max(c for (_r, c) in sh.cells) + 1) if sh.cells else 1
            wide = wide or ncols >= 6
            for it in sh.items:
                if it[0] == "text":
                    flow.append(Paragraph(_rl(it[1]), st[2] if it[2] else base))
                    continue
                tb = it[1]
                data, nc = [], len(tb.header)
                for ri in range(len(tb.rows) + 1):
                    r = tb.top + ri
                    row = []
                    for c in range(nc):
                        cell = sh.cells.get((r, c), Cell())
                        s = show(cell)
                        num = isinstance(cell.computed if cell.formula else cell.value, (int, float))
                        row.append(Paragraph(escape(s), st["cellr"] if num and ri else st["cell"]))
                    data.append(row)
                flow.append(_rl_table(data, tb.top, sh, colors, TableStyle, RLTable, bold))
                flow.append(Spacer(1, 8))
    else:
        _meta, body = front_matter(text)
        for b in _computed_blocks(blocks(body, hr_breaks=hr_breaks)):
            if b.kind == "title":
                flow.append(Paragraph(_rl(b.text), st["title"]))
            elif b.kind == "heading":
                flow.append(Paragraph(_rl(b.text), st[b.level]))
            elif b.kind == "para":
                flow.append(Paragraph(_rl(b.text), base))
            elif b.kind == "item":
                flow.append(Paragraph(_rl(b.text), ParagraphStyle("li", parent=base, leftIndent=14 + 12 * b.level,
                                                                  bulletIndent=2 + 12 * b.level),
                                      bulletText=b.mark))
            elif b.kind == "code":
                flow.append(Preformatted(b.text, st["code"]))
            elif b.kind == "pagebreak":
                flow.append(PageBreak())
            elif b.kind == "table":
                nc = max(len(r) for r in b.rows)
                wide = wide or nc >= 6
                figures = [all(_is_figure(r[c]) for r in b.rows[1:] if c < len(r) and r[c].strip())
                           and any(c < len(r) and r[c].strip() for r in b.rows[1:]) for c in range(nc)]
                data = [[Paragraph(_rl(r[c] if c < len(r) else ""),
                                   st["cellr"] if (figures[c] and ri) else st["cell"]) for c in range(nc)]
                        for ri, r in enumerate(b.rows)]
                flow.append(_rl_table(data, None, None, colors, TableStyle, RLTable, bold,
                                      totals=[ri for ri, r in enumerate(b.rows) if ri and _TOTAL_WORDS.match(r[0] if r else "")]))
                flow.append(Spacer(1, 8))
    size = landscape(A4) if wide else A4
    footer = frame["footer"] or org
    logo = frame["logo"]

    def deco(canvas, doc):
        canvas.saveState()
        w, h = size
        if logo:
            try:
                from reportlab.lib.utils import ImageReader
                img = ImageReader(io.BytesIO(logo))
                iw, ih = img.getSize()
                hh = 1.3 * cm
                canvas.drawImage(img, 2 * cm, h - 1.0 * cm - hh, width=hh * iw / ih, height=hh, mask="auto")
            except Exception:  # noqa: BLE001 — a logo that cannot be read: no logo
                pass
        elif org:
            canvas.setFont(regular, 8)
            canvas.setFillColor(colors.HexColor("#555F6B"))
            canvas.drawRightString(w - 2 * cm, h - 1.2 * cm, org[:120])
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(colors.HexColor("#555F6B"))
        if footer:
            canvas.drawString(2 * cm, 1.1 * cm, footer[:150])
        canvas.drawRightString(w - 2 * cm, 1.1 * cm, f"{doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    top = 3.0 if logo else 2.2
    doc = SimpleDocTemplate(buf, pagesize=size, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=top * cm,
                            bottomMargin=2 * cm, title=_pdf_title(text, name), author=org or "SOKKAN")
    if not flow:
        flow = [Paragraph("", base)]
    doc.build(flow, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def _pdf_title(text: str, name: str) -> str:
    _m, body = front_matter(text)
    for b in blocks(body)[:3]:
        if b.kind == "title":
            return plain_text(b.text)
    return re.sub(r"\.[a-z]+$", "", name or "")


def _rl_table(data, top, sh, colors, TableStyle, RLTable, bold, totals=None):
    t = RLTable(data, repeatRows=1, hAlign="LEFT")
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#7F8C99")),
             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECF1")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("FONTNAME", (0, 0), (-1, 0), bold)]
    rows = totals or []
    if sh is not None and top is not None:
        rows = [r - top for r in sh.total_rows if 0 < r - top < len(data)]
    for ri in rows:
        style.append(("LINEABOVE", (0, ri), (-1, ri), 1.0, colors.HexColor("#333B44")))
    t.setStyle(TableStyle(style))
    for ri in rows:
        for p in data[ri]:
            p.text = f"<b>{p.text}</b>" if hasattr(p, "text") else p
    return t


# ---- preview (the text shown in the thread and in the panel) -------------------------------
def preview(name: str, text: str) -> str:
    """What the person reads in the Atelier: no front matter, figures computed and in Swiss
    format for a spreadsheet, one section per letter for a mail merge."""
    kind = kind_of(name, text)
    if kind == "spreadsheet":
        try:
            title, sheets = workbook_model(name, text)
        except Exception:  # noqa: BLE001 — a preview never breaks: the source as written
            return front_matter(text)[1]
        out = [f"# {title}", ""] if title else []
        for sh in sheets:
            if len(sheets) > 1 or not title:
                out += [f"## {sh.name}", ""]
            for it in sh.items:
                if it[0] == "text":
                    out += [f"**{it[1]}**" if it[2] else it[1], ""]
                    continue
                tb = it[1]
                nc = len(tb.header)
                esc = lambda s: str(s).replace("|", "\\|")  # noqa: E731
                out.append("| " + " | ".join(esc(plain_text(h)) for h in tb.header) + " |")
                out.append("|" + "".join("---:|" if _numeric_col(sh, tb, c) else "---|" for c in range(nc)))
                for ri in range(len(tb.rows)):
                    r = tb.top + 1 + ri
                    vals = [esc(show(sh.cells.get((r, c), Cell()))) for c in range(nc)]
                    if r in sh.total_rows:
                        vals = [f"**{v}**" if v else v for v in vals]
                    out.append("| " + " | ".join(vals) + " |")
                out.append("")
        return "\n".join(out).strip() + "\n"
    if kind == "letters":
        title, ls = letters(text)
        out = [f"# {title}" if title else "", ""]
        out.append(f"*{len(ls)} " + ("lettres" if len(ls) != 1 else "lettre") + " : "
                   + ", ".join(x.name for x in ls) + "*")
        for k, x in enumerate(ls, 1):
            out += ["", "---", "", f"**{k} / {len(ls)} — {x.name}**", "", computed_markdown(x.body)]
        return "\n".join(out).strip() + "\n"
    return computed_markdown(front_matter(text)[1])


def computed_markdown(body: str) -> str:
    """A Markdown text whose tables have their formula cells replaced by their values."""
    if "=" not in body:
        return body
    lines = body.split("\n")
    out, last = [], 0
    for start, end, rows, _heading in _md_tables(body):
        new = computed_rows(rows)
        if new is rows:
            continue
        out += lines[last:start]
        sep = lines[start + 1] if start + 1 < end else "|---|"
        esc = lambda c: str(c).replace("|", "\\|")  # noqa: E731
        out.append("| " + " | ".join(esc(c) for c in new[0]) + " |")
        out.append(sep)
        out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in new[1:]]
        last = end
    return "\n".join(out + lines[last:])


def _numeric_col(sh: Sheet, tb: Table, c: int) -> bool:
    vals = [sh.cells.get((tb.top + 1 + i, c)) for i in range(len(tb.rows))]
    vals = [v for v in vals if v is not None and (v.formula or v.value not in (None, ""))]
    return bool(vals) and all(v.formula or isinstance(v.value, (int, float)) for v in vals)


# ---- one entry point for the download ------------------------------------------------------
def render(fmt: str, name: str, text: str, *, template_for=None, org: str = "",
           logo: bytes | None = None) -> tuple[bytes, str, str]:
    """→ (bytes, file name, mime). ``template_for(kind)`` → path of the organisation's Word
    template for that kind of document (or None). Raises Unavailable / ValueError."""
    meta, _ = front_matter(text)
    stem = file_stem(re.sub(r"\.(md|markdown|txt|csv)$", "", name or "", flags=re.I))
    kind = kind_of(name, text)
    tkind = (meta.get("template") or ("lettre" if kind == "letters" else "")).strip().lower()
    tpl = template_for(tkind) if template_for else None
    if fmt == "xlsx":
        if kind != "spreadsheet":
            raise ValueError("not a table")
        return to_xlsx(name, text), f"{stem}.xlsx", MIME["xlsx"]
    if fmt == "csv":
        if kind != "spreadsheet":
            raise ValueError("not a table")
        return to_csv(name, text), f"{stem}.csv", "text/csv; charset=utf-8"
    if fmt == "docx":
        return to_docx(text, tpl, org, hr_breaks=kind == "letters", logo=logo), f"{stem}.docx", MIME["docx"]
    if fmt == "pdf":
        return to_pdf(text, tpl, org, name, hr_breaks=kind == "letters", logo=logo), f"{stem}.pdf", MIME["pdf"]
    if fmt in ("letters_docx", "letters_pdf", "zip"):
        if kind != "letters":
            raise ValueError("not a mail merge")
        title, ls = letters(text)
        as_pdf = fmt == "letters_pdf"
        buf = io.BytesIO()
        used: set = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            all_md = []
            for k, x in enumerate(ls, 1):
                fn = f"{k:02d} - {file_stem(x.name)[:60]}"
                while fn.lower() in used:
                    fn += "+"
                used.add(fn.lower())
                if as_pdf:
                    z.writestr(f"{fn}.pdf", to_pdf(x.body, tpl, org, fn, logo=logo))
                else:
                    z.writestr(f"{fn}.docx", to_docx(x.body, tpl, org, logo=logo))
                all_md.append(x.body)
            together = "\n\n<!-- page -->\n\n".join(all_md)
            label = "Toutes les lettres"
            if as_pdf:
                z.writestr(f"{label}.pdf", to_pdf(together, tpl, org, label, logo=logo))
            else:
                z.writestr(f"{label}.docx", to_docx(together, tpl, org, logo=logo))
        return buf.getvalue(), f"{stem}.zip", MIME["zip"]
    raise ValueError(f"unknown format {fmt}")


def brief_lines(lang: str = "fr", templates: list[str] | None = None) -> list[str]:
    """What the agent is told about the files the person can get (simple.brief)."""
    out = [
        "- The person gets the file they need from the document you save — you do NOT convert "
        "anything and never tell them you can only save Markdown (do not mention Markdown or "
        "front matter to them; say « the Excel file », « the Word document », « the letters »):",
        "  · any document downloads as Word (.docx) and as PDF;",
        "  · an Excel file / a spreadsheet / a table to calculate: start the file with the three "
        "lines `---`, `format: xlsx`, `---`, then `# Title`; each `## Heading` is a sheet, each "
        "Markdown table a block of rows. Write figures plainly (8120.50, 12 %, 17.11.2026). Write "
        "every total and every calculation as a FORMULA starting with `=`, never as a number you "
        "computed: `=SUM(ABOVE)` for a column total, `=[Montant]-[Payé]` with the column names "
        "for a calculation on the same row (or A1 references counted inside that table, row 1 "
        "= its header). Excel recalculates; the person sees the results in the preview;",
        "  · one document per recipient (reminders, invitations, certificates…): start with "
        "`---`, `format: letters`, `---`, then `# Title`; either ONE letter using `{{Field}}` "
        "placeholders followed by a `## Destinataires` table whose column names are those "
        "fields (one row per recipient), or the letters one after the other separated by a line "
        "`---`. The person downloads one Word (or PDF) file per recipient, in one go;",
        "  · a PDF: any document; add `format: pdf` in the same three-line header when the PDF "
        "is what they asked for.",
    ]
    if templates:
        named = [t for t in templates if t != "default"]
        out.append("  · Word files use the organisation's own Word template"
                   + (f"; to use a specific one add `template: <name>` to the header, names: "
                      f"{', '.join(named)}" if named else "") + ".")
    return out
