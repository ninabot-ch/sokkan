"""Note files: tolerant frontmatter parsing and the naming convention.

The convention, one rule in both directions::

    file = <name with "-" replaced by "_">.md        name = kebab-case

Parsing never fails on a hand-written note: the frontmatter is searched anywhere at
the top (people paste updates *above* the block), a block YAML refuses (typically an
unquoted ``description:`` containing ": ") falls back to a line parser, and every
deviation is reported in ``ParsedNote.warnings`` instead of being swallowed.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

import yaml

FM_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.S | re.M)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
# [[target]], [[target|label]], [[target#section]]
LINK_RE = re.compile(r"\[\[([^\]|#]+)([^\]]*)\]\]")
INDEX_FILENAME = "MEMORY.md"
# libyaml when available: a pass parses every note, the pure-Python loader is ~10x slower
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def slugify(text: str) -> str:
    """kebab-case slug: lower case, "_" and any run of other characters → "-"."""
    s = re.sub(r"[^a-z0-9.]+", "-", str(text).lower().replace("_", "-")).strip("-.")
    return re.sub(r"-{2,}", "-", s)


def is_kebab(name: str) -> bool:
    return bool(SLUG_RE.match(name or "")) and "_" not in name


def filename_for(name: str) -> str:
    """The file a note named ``name`` must live in."""
    return name.replace("-", "_") + ".md"


def line_parse(raw: str) -> dict:
    """Line-by-line fallback when YAML rejects the frontmatter block."""
    fm: dict = {}
    meta: dict = {}
    in_meta = False
    for line in raw.split("\n"):
        if not line.strip():
            continue
        if in_meta and re.match(r"^\s+\S", line):
            k, _, v = line.strip().partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
            continue
        k, _, v = line.partition(":")
        if k.strip() == "metadata":
            in_meta = True
            continue
        in_meta = False
        if k.strip():
            fm[k.strip()] = v.strip().strip("\"'")
    if meta:
        fm["metadata"] = meta
    return fm


@dataclass
class ParsedNote:
    filename: str
    name: str
    description: str = ""
    type: str = "unknown"
    priority: int = 0
    modified: str | None = None          # metadata.modified, ISO string
    modified_source: str | None = None   # metadata.modified_source
    level: int | None = None             # 3.4: `classification:` (core.levels); None = not set
    body: str = ""                       # stripped; text found above the frontmatter is prepended
    fm: dict | None = None               # None = no frontmatter at all
    yaml_ok: bool = False
    above: str = ""                      # text written above the frontmatter block
    raw_body: str = ""                   # text after the frontmatter, as written
    warnings: list[str] = field(default_factory=list)


def _as_str_date(raw) -> str | None:
    if isinstance(raw, (datetime.datetime, datetime.date)):
        return raw.isoformat()
    if raw in (None, ""):
        return None
    return str(raw).strip() or None


def parse_note(text: str, filename: str) -> ParsedNote:
    stem = filename[:-3] if filename.endswith(".md") else filename
    note = ParsedNote(filename=filename, name=stem.replace("_", "-"), body=text.strip(),
                      raw_body=text)
    m = FM_RE.search(text)
    if not m:
        note.warnings.append("no frontmatter")
        return note
    note.above = text[: m.start()].strip()
    note.raw_body = text[m.end():]
    body = note.raw_body.lstrip("\n")
    if note.above:
        note.warnings.append("text written above the frontmatter")
        body = note.above + "\n\n" + body
    note.body = body.strip()
    try:
        fm = yaml.load(m.group(1), Loader=_Loader) or {}
        note.yaml_ok = isinstance(fm, dict)
    except yaml.YAMLError as exc:
        fm = None
        note.warnings.append(f"invalid YAML ({str(exc).splitlines()[0]}), line fallback")
    if not note.yaml_ok:
        fm = line_parse(m.group(1))
    note.fm = fm
    note.name = str(fm.get("name") or "").strip() or note.name
    note.description = " ".join(str(fm.get("description") or "").split())
    meta = fm.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    note.type = str(meta.get("type") or meta.get("node_type") or "unknown")
    raw_prio = fm.get("priority", meta.get("priority", ""))
    note.priority = 1 if str(raw_prio).lower() in ("high", "true", "1") else 0
    from . import levels as _lv
    note.level = _lv.parse(fm.get("classification", meta.get("classification")))
    note.modified = _as_str_date(meta.get("modified"))
    note.modified_source = (str(meta.get("modified_source") or "").strip()) or None
    if not note.description:
        note.warnings.append("empty description")
    return note


def parse_links(body: str, self_name: str = "") -> list[str]:
    """[[wikilinks]] of a body as slugs, deduplicated, without self-links."""
    out: list[str] = []
    for m in LINK_RE.finditer(body or ""):
        dst = slugify(m.group(1).strip().removesuffix(".md"))
        if dst and dst != self_name and dst not in out:
            out.append(dst)
    return out


# ---- the title a person reads ----------------------------------------------------------------
_TITLE_MAX = 70


def _clean_title(s: str) -> str:
    s = re.sub(r"[*_`]", "", str(s or "")).strip().strip("\"'«»“”").strip()
    return " ".join(s.split())


def humanize_name(name: str) -> str:
    """« procedure-de-devis » → « Procedure de devis » (the last resort: a slug has no accent)."""
    s = re.sub(r"\.md$", "", str(name or ""), flags=re.I)
    s = " ".join(re.sub(r"[_\-]+", " ", s).split())
    return (s[:1].upper() + s[1:]) if s else str(name or "")


def title_from_description(description: str) -> str:
    """A title out of a description: what comes before the first « : » when that is short
    (« Évaluation des acquis à l'Atelier : trois questions… »), else the description itself
    when it is short. « » when neither is."""
    d = " ".join(str(description or "").split())
    if not d:
        return ""
    head = re.split(r"\s*:\s", d, maxsplit=1)[0] if re.search(r"\s*:\s", d) else ""
    for cand in (head, d):
        cand = _clean_title(cand).rstrip(" .;,—–-")
        if 2 < len(cand) <= _TITLE_MAX:
            return cand
    return ""


def display_title(name: str, description: str = "", fm: dict | None = None, body: str = "") -> str:
    """The title of a note as a person reads it, first that answers: ``title:`` of the
    frontmatter, the ``# heading`` the body opens with, a title out of the description, the
    name made readable. (A note's name is a slug: « Evaluation des acquis » lost its accents.)"""
    t = _clean_title((fm or {}).get("title") or "") if isinstance(fm, dict) else ""
    if t:
        return t[:120]
    text = re.sub(r"^\s*\[note [^\]\n]*\]\s*\n?", "", body or "")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    m = re.match(r"\s{0,3}#\s+(.+?)\s*#*\s*$", first)
    if m and _clean_title(m.group(1)):
        return _clean_title(m.group(1))[:120]
    return title_from_description(description) or humanize_name(name)


def title_of_file(path, name: str, description: str = "") -> str:
    """``display_title`` of a note file, reading its head only; an unreadable file is titled
    from its description and name."""
    text = ""
    if path:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(8192)
        except OSError:
            text = ""
    if not text:
        return title_from_description(description) or humanize_name(name)
    p = parse_note(text, str(path).rsplit("/", 1)[-1])
    return display_title(name, description or p.description, p.fm, p.body)
