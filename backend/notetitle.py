#!/usr/bin/env python3
"""notetitle.py — the title of a memory note as a person reads it (3.6.4).

A note's name is a slug (« evaluation-des-acquis »): it has no accent and no capital. The Memory
page of the simple profile and the card « retain in memory » title a note, first that answers:
``title:`` of its front matter, the ``# heading`` its body opens with, a title out of its
description (before the first « : » when that is short, else the description when it is short),
the name made readable last.

Kept outside ``memory/core`` (the CortHeXis engine, compared with its own repository by
``scripts/check-corthexis-sync.sh``): reading a note's head uses the engine's parser only.
"""
from __future__ import annotations

import re
from pathlib import Path

_TITLE_MAX = 70


def _clean(s) -> str:
    s = re.sub(r"[*_`]", "", str(s or "")).strip().strip("\"'«»“”").strip()
    return " ".join(s.split())


def humanize_name(name: str) -> str:
    """« procedure-de-devis » → « Procedure de devis » (the last resort)."""
    s = re.sub(r"\.md$", "", str(name or ""), flags=re.I)
    s = " ".join(re.sub(r"[_\-]+", " ", s).split())
    return (s[:1].upper() + s[1:]) if s else str(name or "")


def from_description(description: str) -> str:
    """What comes before the first « : » when that is short, else the description itself when
    it is short; « » when neither is."""
    d = " ".join(str(description or "").split())
    if not d:
        return ""
    head = re.split(r"\s*:\s", d, maxsplit=1)[0] if re.search(r"\s*:\s", d) else ""
    for cand in (head, d):
        cand = _clean(cand).rstrip(" .;,—–-")
        if 2 < len(cand) <= _TITLE_MAX:
            return cand
    return ""


def display_title(name: str, description: str = "", fm: dict | None = None, body: str = "") -> str:
    t = _clean(fm.get("title") or "") if isinstance(fm, dict) else ""
    if t:
        return t[:120]
    text = re.sub(r"^\s*\[note [^\]\n]*\]\s*\n?", "", body or "")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    m = re.match(r"\s{0,3}#\s+(.+?)\s*#*\s*$", first)
    if m and _clean(m.group(1)):
        return _clean(m.group(1))[:120]
    return from_description(description) or humanize_name(name)


def of_file(path, name: str, description: str = "") -> str:
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
        return from_description(description) or humanize_name(name)
    from core import notes as _notes
    p = _notes.parse_note(text, Path(str(path)).name)
    return display_title(name, description or p.description, p.fm, p.body)
