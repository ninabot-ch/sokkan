#!/usr/bin/env python3
"""pii.py — the « PII sandwich » of « Write with Claude » (4.0, decision of 18.11.2026).

Claude may write a work of a project that is NOT confidential (backend/claudetier.py; a
confidential or restricted project never goes there, whatever is said here). Before anything of
such a work leaves the machine, the personal data in it is replaced, ON THE MACHINE, by stable
pseudonyms — ``[PERSONNE_1]``, ``[IBAN_1]`` … — and put back in what Claude answers: the text a
person reads and the files the agent writes. Anthropic only ever sees the pseudonyms.

Detection is deterministic (no model, no network), so that it can run on every request:

* ``iban``       Swiss and Liechtenstein IBANs (CH/LI), mod-97 checked;
* ``avs``        AVS/AHV numbers 756.xxxx.xxxx.xx, EAN-13 check digit checked;
* ``email``      e-mail addresses;
* ``phone``      Swiss and French numbers, national (0xx …) and international (+41, +33, 0041 …);
* ``birthdate``  a date that follows « né le », « date de naissance », « born », « geboren » …;
* ``address``    a simple postal address: a street with its number, optionally « 1201 Genève »;
* ``person``     the names the organisation knows (directory and signatories of the
                 organisation profile, `orgprofile`), a name after a civility (« Mme Dupont »,
                 « Herr Muster », « Dr Rossi ») or after a field label (« Nom : … », « Name: … »).

What it does not do — and the documentation says so: it is not a named-entity model. A name
written alone in running text that the directory does not know, a company name, a free-form
medical or salary detail are NOT found. The person is warned of what WAS found, and decides.

The mapping (`PseudoMap`) is per work and stable: the same value always gets the same pseudonym,
so a conversation of many turns stays coherent (and Anthropic's prompt cache keeps working).
`restore` puts the values back; `StreamRestorer` does it on a streamed answer, where a pseudonym
may arrive cut in two. Nothing here logs or returns a value in clear except `restore`.
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass

# ---- categories ----------------------------------------------------------------------------
# id → (pseudonym prefix, label fr, label en). The prefix is what Claude sees.
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "person": ("PERSONNE", "noms de personnes", "names of people"),
    "iban": ("IBAN", "IBAN", "IBANs"),
    "avs": ("AVS", "numéros AVS", "AHV/AVS numbers"),
    "email": ("EMAIL", "adresses e-mail", "e-mail addresses"),
    "phone": ("TELEPHONE", "numéros de téléphone", "phone numbers"),
    "birthdate": ("DATE_NAISSANCE", "dates de naissance", "dates of birth"),
    "address": ("ADRESSE", "adresses postales", "postal addresses"),
}
ORDER = tuple(CATEGORIES)            # priority when two findings overlap (earlier wins)
_PREFIX_TO_CAT = {v[0]: k for k, v in CATEGORIES.items()}
PSEUDONYM_RE = re.compile(r"\[(" + "|".join(v[0] for v in CATEGORIES.values()) + r")_(\d{1,5})\]")
_MAX_PSEUDO = max(len(v[0]) for v in CATEGORIES.values()) + 8      # « [DATE_NAISSANCE_12345] »

# what replaces an image or a PDF sent as bytes: nothing in it can be checked, so it does not go
WITHHELD = {"fr": "[Pièce non transmise : son contenu ne peut pas être anonymisé sur la machine.]",
            "en": "[Attachment not sent: its content cannot be anonymised on the machine.]"}


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    category: str
    value: str


# ---- the checks ----------------------------------------------------------------------------
# ISO 13616 lengths of the IBAN of each country (an IBAN of any country is found, mod-97 checked)
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
    "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}


def iban_ok(raw: str) -> bool:
    s = re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", raw or "")).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[0-9A-Z]{11,30}", s) or IBAN_LENGTHS.get(s[:2]) != len(s):
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1


def avs_ok(raw: str) -> bool:
    """The EAN-13 check digit of an AVS number. Detection does NOT require it (a typo in the
    key is still somebody's number): only the shape 756 + 10 digits does."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) != 13 or not d.startswith("756"):
        return False
    total = sum(int(x) * (3 if i % 2 else 1) for i, x in enumerate(d[:12]))
    return (10 - total % 10) % 10 == int(d[12])


# every Unicode blank (no-break space of Word and PDF, thin space, figure space…) is a plain space
# for the patterns — one character for one, so that positions stay those of the original text
_BLANKS = {c: " " for c in (0x00A0, 0x2007, 0x2009, 0x200A, 0x202F, 0x2002, 0x2003, 0x2004, 0x2005,
                            0x2006, 0x2008, 0x205F, 0x3000)}
_DASHES = {c: "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212)}


def _plain(text: str) -> str:
    return text.translate(_BLANKS).translate(_DASHES)


def _nfc_map(text: str) -> tuple[str, list[int], list[int]]:
    """The text in NFC (« Ève » written E + combining grave is one letter for the patterns), with,
    for each of its characters, where it starts and ends in the original — so that a finding is
    cut and replaced in the text as it was written."""
    if unicodedata.is_normalized("NFC", text):
        n = len(text)
        return text, list(range(n)), list(range(1, n + 1))
    out, starts, ends = [], [], []
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        piece = unicodedata.normalize("NFC", text[i:j])
        for ch in piece:
            out.append(ch)
            starts.append(i)
            ends.append(j)
        i = j
    return "".join(out), starts, ends


# ---- the patterns --------------------------------------------------------------------------
_UP = "A-ZÀ-ÖØ-Þ"
_LO = "a-zà-öø-ÿß"
# a name word: Dupont, D'Amico, Muller-Brandt, and in capitals DUPONT, MULLER-BRANDT
_WORD = rf"[{_UP}](?:[{_LO}'’]+|[{_UP}'’]+)(?:-[{_UP}](?:[{_LO}]+|[{_UP}]+))?"
_PARTICLE = r"(?:de|du|des|von|van|der|di|da|dos|le|la|d['’])"
_NAME = rf"{_WORD}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_WORD}){{0,2}}"
# words that follow a civility or a label without being a name
_STOP = {"le", "la", "les", "l", "un", "une", "des", "du", "de", "et", "ou", "the", "a", "an", "and", "der",
         "die", "das", "und", "est", "a", "directeur", "directrice", "président", "présidente", "maire",
         "responsable", "chef", "cheffe", "si", "vous", "votre", "your"}

_SEP = r"(?:[ \t]*[./\-][ \t]*|[ \t])"                       # « 79 123 », « 079/123 », « 022 / 345 »
_IBAN_HEAD = re.compile(r"(?<![0-9A-Za-z])([A-Za-z]{2})(\d{2})")
_AVS = re.compile(r"(?<!\d)756[.\s\-]?\d{4}[.\s\-]?\d{4}[.\s\-]?\d{2}(?!\d)")
_EMAIL = re.compile(r"(?<![\w.+\-])[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,24}(?![\w\-])")
_PHONE_INTL = re.compile(
    rf"(?<![\w+])(?:\+|00)(41|33|49|39|43|423)(?:{_SEP})?(?:\(0\)(?:{_SEP})?)?(\d(?:{_SEP}?\d){{5,12}})(?!\d)")
_PHONE_NAT = re.compile(rf"(?<![\w+./\-])0[1-9](?:{_SEP}?\d){{8}}(?!\d)(?![.\-/]\d)")
_INTL_DIGITS = {"41": (9, 9), "33": (9, 9), "423": (7, 9), "49": (6, 12), "39": (6, 11), "43": (4, 13)}
_DATE_LIKE = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}")
_MONTHS = (r"janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|"
           r"décembre|decembre|january|february|march|april|may|june|july|august|september|october|"
           r"november|december|januar|februar|märz|maerz|juni|juli|oktober|dezember")
_DATE = (rf"\d{{1,2}}[./\-]\d{{1,2}}[./\-](?:19|20)?\d{{2}}|(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}"
         rf"|\d{{1,2}}(?:er|\.)?\s+(?:{_MONTHS})\s+(?:19|20)\d{{2}}")
_BIRTH_LABEL = (r"\bn[ée]+(?:\(e\))?\s+le|\bdate\s+de\s+naissance|\bnaissance|\bborn(?:\s+on)?"
                r"|\bdate\s+of\s+birth|\bbirth\s*date|\bd\.?o\.?b\.?|\bgeburtsdatum|\bgeboren(?:\s+am)?")
_BIRTH = re.compile(rf"(?i:{_BIRTH_LABEL})[^\d\n]{{0,15}}?(?:\n[ \t]*)?({_DATE})(?!\d)", re.IGNORECASE)
_STREET = (r"rue|ruelle|chemin|ch\.|avenue|av\.|route|rte|boulevard|bd|place|pl\.|quai|allée|impasse"
           r"|sentier|promenade|passage|via|strada|street|road")
_CITY = rf"(?:CH-|F-)?\d{{4,5}}\s+{_WORD}(?:[\s\-]{_WORD})?"
_ADDRESS = re.compile(
    rf"(?:(?<!\w)\d{{1,4}}(?:\s?(?:bis|ter|[a-z]))?,?\s+)?"                      # 12, rue …  (fr)
    rf"(?i:\b(?:{_STREET}))\s+(?:(?:{_PARTICLE})\s*)*{_WORD}(?:\s+(?:(?:{_PARTICLE})\s*)*{_WORD}){{0,3}}"
    rf"(?:\s*,?\s*\d{{1,3}}[a-z]?(?!\d))?"                                       # … 12        (ch)
    rf"(?:\s*,\s*|\s+)?(?:{_CITY})?"
    rf"|(?<!\w)[{_UP}][{_LO}]+(?:strasse|straße|gasse|weg|platz|allee)\s+\d{{1,4}}[a-z]?"
    rf"(?:\s*,?\s*{_CITY})?")
_CIVIL_LONG = r"monsieur|madame|mademoiselle|mme|mlle|herr|frau|docteur|maître"
_CIVIL_SHORT = r"MM\.|M\.|Me|Dr\.?|Dre|Mr\.?|Mrs\.?|Ms\.?|Prof\.?"      # as written: « me » is a word
_CIVILITY = re.compile(rf"(?<![\w.])(?:(?i:{_CIVIL_LONG})|{_CIVIL_SHORT})[ \t]+({_NAME})")
_CIVILITY_LOW = re.compile(rf"(?<![\w.])(?i:{_CIVIL_LONG})[ \t]+([{_LO}][{_LO}'’\-]{{2,}})\b")
_LABELS = (r"nom\s+et\s+prénom|nom\s+complet|prénom\s+et\s+nom|nom\s+de\s+famille|nom|prénom|full\s+name"
           r"|first\s+name|last\s+name|surname|name|vorname|nachname|employée?|collaborat(?:eur|rice)"
           r"|candidate?|titulaire|signataire|destinataire|patient(?:e)?|employee")
# after a label, the line up to its separator: « Nom : DUPONT Jean », « Nom: Jean DUPONT; … »
_LABELLED = re.compile(rf"(?i:\b(?:{_LABELS}))[ \t]*:[ \t]*(?=([^\n;,|\t()\[\]<>]{{2,60}}))")
_HEADER_CELL = re.compile(
    rf"^(?i:(?P<person>{_LABELS}|nom\s*/\s*prénom|prénom\s*/\s*nom)"
    rf"|(?P<birthdate>{_BIRTH_LABEL[2:]}|date\s+naiss\.?|né\(e\)\s+le)"
    rf"|(?P<address>adresse|address|adresse\s+postale|anschrift|domicile|rue)"
    rf"|(?P<email>e-?mail|courriel|mail)|(?P<phone>t[ée]l[ée]?phone|t[ée]l\.?|mobile|natel|portable|phone|telefon)"
    rf"|(?P<avs>avs|ahv|no\s+avs|n°\s*avs)|(?P<iban>iban))$")


def _norm(category: str, value: str) -> str:
    """The key that makes two spellings of ONE value one value (spaces in an IBAN, case of a
    name) — never two values one (F2: « 061 234 56 78 » in Basel is not « 06 12 34 56 78 » in
    Lyon: a national number keeps the shape of its groups, an international one its country)."""
    v = _plain(unicodedata.normalize("NFC", value or "")).strip()
    if category in ("iban", "avs"):
        return re.sub(r"[^0-9A-Za-z]", "", v).upper()
    if category == "phone":
        intl = re.match(r"^(?:\+|00)(\d{2,3})", v)
        if intl:
            rest = re.sub(r"\D", "", v[intl.end():].replace("(0)", ""))
            return f"+{intl.group(1)}{rest.lstrip('0')}"
        groups = "-".join(str(len(g)) for g in re.findall(r"\d+", v))
        return re.sub(r"\D", "", v) + "|" + groups
    if category == "email":
        return v.lower()
    return re.sub(r"\s+", " ", v).casefold()


def _literal(value: str) -> re.Pattern:
    """A known name, anywhere: its words with any spacing, in any case."""
    body = r"\s+".join(re.escape(w) for w in value.split())
    return re.compile(rf"(?<![\w\-])(?i:{body})(?![\w\-])")


def _name_value(raw: str) -> str:
    """What follows a label, when it is a name: 1 to 5 words of letters, no digit, no @."""
    v = re.split(r"\.\s|\s{2,}", raw, maxsplit=1)[0].strip(" .:-'’\"")    # up to the end of the sentence
    if not v or any(ch.isdigit() for ch in v) or "@" in v or len(v.split()) > 5:
        return ""
    words = v.split()
    if not all(re.fullmatch(rf"[{_UP}{_LO}'’\-.]+", w) for w in words):
        return ""
    if words[0].lower() in _STOP or not any(w[:1].isupper() for w in words):
        return ""
    return v


def _table_cells(text: str):
    """(category, start, end) of the cells under a header that names personal data (a CSV, an
    Excel sheet as text, a Markdown table): the whole column is personal, written by anybody."""
    lines = text.split("\n")
    offs, at = [], 0
    for ln in lines:
        offs.append(at)
        at += len(ln) + 1
    i = 0
    while i < len(lines):
        ln = lines[i]
        sep = next((c for c in ("|", ";", "\t", ",") if ln.count(c) >= 1), None)
        cols: dict[int, str] = {}
        cells = ln.split(sep) if sep else []
        if sep and all(len(c.strip()) <= 40 for c in cells):
            for k, cell in enumerate(cells):
                m = _HEADER_CELL.match(cell.strip(" *_:\"'"))
                if m:
                    cols[k] = m.lastgroup
        if len(cols) < (2 if sep == "," else 1):          # a sentence with commas is not a header
            cols = {}
        if not cols:
            i += 1
            continue
        n = len(ln.split(sep))
        j = i + 1
        while j < len(lines) and lines[j].count(sep) and abs(len(lines[j].split(sep)) - n) <= 1:
            if not re.fullmatch(r"[\s|:\-]*", lines[j]):            # a Markdown rule line
                pos = offs[j]
                for k, cell in enumerate(lines[j].split(sep)):
                    v = cell.strip(" \"'")                 # « "Nom","Prénom" »: quotes are not the value
                    if k in cols and v:
                        lead = len(cell) - len(cell.lstrip(" \"'"))
                        yield cols[k], pos + lead, pos + lead + len(v)
                    pos += len(cell) + len(sep)
            j += 1
        i = j


def _iban_spans(text: str):
    for m in _IBAN_HEAD.finditer(text):
        want = IBAN_LENGTHS.get(m.group(1).upper())
        if not want:
            continue
        k, got, last = m.end(), 4, m.end()
        while k < len(text) and got < want:
            ch = text[k]
            if ch.isascii() and ch.isalnum():
                got += 1
                last = k + 1
            elif ch in " -" and k + 1 < len(text) and text[k + 1].isascii() and text[k + 1].isalnum() \
                    and text[k - 1] not in " -":
                pass
            else:
                break
            k += 1
        if got == want and not (last < len(text) and text[last].isascii() and text[last].isalnum()) \
                and iban_ok(text[m.start():last]):
            yield m.start(), last


def detect(text: str, names: list[str] | tuple[str, ...] = (), known: list[tuple[str, str]] = ()) -> list[Finding]:
    """Every piece of personal data found in ``text``, without overlap, in text order.
    ``names``: people the organisation knows (directory, signatories, the person at work).
    ``known``: (category, value) already in the mapping of the work, found again wherever they
    are written."""
    if not text:
        return []
    orig = text
    norm, starts, ends = _nfc_map(text)
    text = _plain(norm)
    raw: list[tuple[int, Finding]] = []

    def add(cat: str, start: int, end: int) -> None:
        seg = text[start:end]
        val = seg.strip(" ,.;:")
        if val:
            s = start + (len(seg) - len(seg.lstrip(" ,.;:")))
            a, b = starts[s], ends[s + len(val) - 1]          # back to the positions of the original
            raw.append((ORDER.index(cat), Finding(a, b, cat, orig[a:b])))

    for a, b in _iban_spans(text):
        add("iban", a, b)
    for m in _AVS.finditer(text):
        add("avs", m.start(), m.end())
    for m in _EMAIL.finditer(text):
        add("email", m.start(), m.end())
    for m in _PHONE_INTL.finditer(text):
        lo, hi = _INTL_DIGITS[m.group(1)]
        end, n = m.start(2), 0
        for k in range(m.start(2), m.end(2)):           # a number glued after it is not part of it
            if text[k].isdigit():
                n += 1
                end = k + 1
                if n == hi:
                    break
        if n >= lo:
            add("phone", m.start(), end)
    for m in _PHONE_NAT.finditer(text):
        if not _DATE_LIKE.match(m.group(0)):
            add("phone", m.start(), m.end())
    for m in _BIRTH.finditer(text):
        add("birthdate", m.start(1), m.end(1))
    for m in _ADDRESS.finditer(text):
        if re.search(r"\d", m.group(0)):         # a street without a number is not an address
            add("address", m.start(), m.end())
    for m in _CIVILITY.finditer(text):
        add("person", m.start(1), m.end(1))
    for m in _CIVILITY_LOW.finditer(text):
        if m.group(1).lower() not in _STOP:
            add("person", m.start(1), m.end(1))
    for m in _LABELLED.finditer(text):
        v = _name_value(m.group(1))
        if v:
            a = m.start(1) + m.group(1).index(v)
            add("person", a, a + len(v))
    for cat, a, b in _table_cells(text):
        add(cat, a, b)                        # under a personal header, the whole column (« bovet »)
    # a surname met with its first name or civility is found again alone (« Mme Claire Exemple »
    # … « Exemple viendra »), and each word of a known name too: its own pseudonym, so that no
    # part of a name leaks
    people = [f.value for _, f in raw if f.category == "person"] + [n for n in names if n]
    people += [v for c, v in known if c == "person"]
    parts = [w for p in people if len(p.split()) > 1 for w in re.split(r"\s+", p.strip())
             if len(w) >= 4 and w[:1].isupper() and w.lower() not in _STOP]
    seen_lit: set[str] = set()
    for cat, value in [*(("person", n) for n in names), *known, *(("person", w) for w in parts)]:
        value = (value or "").strip()
        key = f"{cat}\0{_norm(cat, value)}"
        if len(value) < 3 or key in seen_lit or cat not in ("person", "address"):
            continue
        seen_lit.add(key)
        for m in _literal(_plain(unicodedata.normalize("NFC", value))).finditer(text):
            add(cat, m.start(), m.end())
    # no overlap: by start, then the longest, then the category's priority
    raw.sort(key=lambda x: (x[1].start, -(x[1].end - x[1].start), x[0]))
    out: list[Finding] = []
    end = -1
    for _, f in raw:
        if f.start >= end:
            out.append(f)
            end = f.end
    return out


# ---- what a person is told -----------------------------------------------------------------
def mask(category: str, value: str) -> str:
    """A hint of the value, never the value: enough to recognise it, not to read it."""
    v = (value or "").strip()
    if category == "person":
        return " ".join(w[0] + "." for w in v.split() if w)
    if category == "email":
        local, _, dom = v.partition("@")
        return f"{local[:1]}•••@{dom}"
    if category in ("iban", "avs", "phone"):
        compact = re.sub(r"\s", "", v)
        return f"{compact[:4]}•••{compact[-2:]}"
    if category == "birthdate":
        return "••.••." + (re.findall(r"(?:19|20)\d{2}", v) or ["••••"])[-1][:2] + "••"
    if category == "address":
        return (v.split()[0] if v.split() else "") + " •••"
    return "•••"


def summary(findings: list[Finding], lang: str = "fr") -> dict:
    """Categories and counts (distinct values) with two masked hints each — what the card shows."""
    fr = lang == "fr"
    by: dict[str, dict[str, str]] = {}
    for f in findings:
        by.setdefault(f.category, {}).setdefault(_norm(f.category, f.value), f.value)
    cats = []
    for cat in ORDER:
        if cat in by:
            vals = list(by[cat].values())
            cats.append({"category": cat, "label": CATEGORIES[cat][1 if fr else 2], "count": len(vals),
                         "examples": [mask(cat, v) for v in vals[:2]]})
    return {"total": sum(c["count"] for c in cats), "categories": cats}


def counts(findings: list[Finding]) -> dict[str, int]:
    by: dict[str, set] = {}
    for f in findings:
        by.setdefault(f.category, set()).add(_norm(f.category, f.value))
    return {k: len(v) for k, v in by.items()}


# ---- the mapping ---------------------------------------------------------------------------
class PseudoMap:
    """value ↔ pseudonym for one work. Stable: a value keeps its pseudonym for the life of the
    work; numbers are given per category in the order values are first met."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.values: dict[str, str] = dict(data.get("values") or {})        # pseudonym → value
        self.keys: dict[str, str] = dict(data.get("keys") or {})            # cat\0norm → pseudonym
        self.next: dict[str, int] = {k: int(v) for k, v in (data.get("next") or {}).items()}

    def to_dict(self) -> dict:
        return {"values": self.values, "keys": self.keys, "next": self.next}

    def __len__(self) -> int:
        return len(self.values)

    def known(self) -> list[tuple[str, str]]:
        return [(_PREFIX_TO_CAT[PSEUDONYM_RE.fullmatch(p).group(1)], v) for p, v in self.values.items()
                if PSEUDONYM_RE.fullmatch(p)]

    def pseudonym(self, category: str, value: str) -> str:
        k = f"{category}\0{_norm(category, value)}"
        p = self.keys.get(k)
        if p is None:
            n = self.next.get(category, 0) + 1
            self.next[category] = n
            p = f"[{CATEGORIES[category][0]}_{n}]"
            self.keys[k] = p
            self.values[p] = value
        return p

    def anonymize(self, text: str, names: list[str] | tuple[str, ...] = ()) -> tuple[str, list[Finding]]:
        """(text with pseudonyms, what was replaced)."""
        if not text or not isinstance(text, str):
            return text, []
        # M2: a pseudonym already written in what comes in (a third party's mail that says
        # « [IBAN_1] ») is not one of ours: defused, same length, so that Claude cannot have it
        # restored into a real value
        text = PSEUDONYM_RE.sub(lambda m: f"[{m.group(1)}-{m.group(2)}]", text)
        found = detect(text, names, self.known())
        if not found:
            return text, []
        parts, at = [], 0
        for f in found:
            parts.append(text[at:f.start])
            parts.append(self.pseudonym(f.category, f.value))
            at = f.end
        parts.append(text[at:])
        return "".join(parts), found

    def restore(self, text: str) -> str:
        """The pseudonyms of this work put back; an unknown one is left as it is."""
        if not text or "[" not in text:
            return text
        return PSEUDONYM_RE.sub(lambda m: self.values.get(m.group(0), m.group(0)), text)

    def restore_json_fragment(self, fragment: str) -> str:
        """The same, inside a piece of JSON text (a tool's input streamed by Anthropic): the
        value is written as a JSON string body (quotes and backslashes escaped)."""
        if not fragment or "[" not in fragment:
            return fragment
        return PSEUDONYM_RE.sub(
            lambda m: json.dumps(self.values[m.group(0)], ensure_ascii=False)[1:-1]
            if m.group(0) in self.values else m.group(0), fragment)


class StreamRestorer:
    """Puts the values back in a text that arrives in pieces: a pseudonym cut between two
    pieces (« [PERSO » + « NNE_1] ») is held until it is whole."""

    def __init__(self, pmap: PseudoMap, json_mode: bool = False):
        self.pmap, self.json_mode, self.buf = pmap, json_mode, ""

    def _restore(self, s: str) -> str:
        return self.pmap.restore_json_fragment(s) if self.json_mode else self.pmap.restore(s)

    def feed(self, piece: str) -> str:
        self.buf += piece or ""
        cut = len(self.buf)
        i = self.buf.rfind("[")
        if i >= 0 and "]" not in self.buf[i:] and len(self.buf) - i <= _MAX_PSEUDO \
                and re.fullmatch(r"\[[A-Z_0-9]*", self.buf[i:]):
            cut = i
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return self._restore(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return self._restore(out)


# ---- an Anthropic Messages request ---------------------------------------------------------
# structural fields, never written by a person: kept as they are (F3: everything else is walked —
# a tool's description and schema, a citation's text, metadata…)
_KEEP_KEYS = {"type", "id", "tool_use_id", "signature", "media_type", "cache_control", "role", "model",
              "is_error", "required", "format", "pattern", "$ref", "$schema", "additionalProperties"}
_NAMED = ("tool_use", "server_tool_use", "mcp_tool_use", "custom")      # « name » is a tool's name


def anonymize_payload(payload: dict, pmap: PseudoMap, names: list[str] | tuple[str, ...] = (),
                      lang: str = "fr") -> tuple[dict, list[Finding]]:
    """A copy of a /v1/messages request with every text a person, a document or a tool wrote
    pseudonymised: the system prompt, the messages (tools' results and inputs, citations), the
    tool definitions (descriptions and schemas) and the metadata. A thinking block is left
    untouched (Claude wrote it from pseudonyms; it is signed). An image or a PDF sent as bytes
    cannot be checked: it is replaced by a sentence saying it was not sent. The name of a tool
    and the structural fields stay as they are."""
    out = copy.deepcopy(payload)
    found: list[Finding] = []

    def text(s: str) -> str:
        new, f = pmap.anonymize(s, names)
        found.extend(f)
        return new

    def walk(x, tool_def: bool = False):
        if isinstance(x, str):
            return text(x)
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            t = x.get("type")
            if t in ("thinking", "redacted_thinking"):
                return x
            if t in ("image", "document") and isinstance(x.get("source"), dict) \
                    and x["source"].get("type") != "text":
                return {"type": "text", "text": WITHHELD.get(lang, WITHHELD["en"])}
            keep_name = tool_def or t in _NAMED
            return {k: (v if k in _KEEP_KEYS or (k == "name" and keep_name)
                        else {pk: walk(pv) for pk, pv in v.items()} if k == "properties" and isinstance(v, dict)
                        else walk(v)) for k, v in x.items()}
        return x

    for k in ("system", "metadata"):
        if k in out:
            out[k] = walk(out[k])
    if isinstance(out.get("tools"), list):
        out["tools"] = [walk(t, tool_def=True) if isinstance(t, dict) else t for t in out["tools"]]
    if isinstance(out.get("messages"), list):
        out["messages"] = [{k: (walk(v) if k == "content" else v) for k, v in m.items()}
                           if isinstance(m, dict) else m for m in out["messages"]]
    return out, found


def restore_value(x, pmap: PseudoMap):
    """Every string of a JSON value with its pseudonyms put back (a tool's complete input)."""
    if isinstance(x, str):
        return pmap.restore(x)
    if isinstance(x, list):
        return [restore_value(i, pmap) for i in x]
    if isinstance(x, dict):
        return {k: restore_value(v, pmap) for k, v in x.items()}
    return x


def restore_message(msg: dict, pmap: PseudoMap) -> dict:
    """A complete (non-streamed) Messages response with the values put back in its text and
    tool inputs; thinking stays as Claude wrote it."""
    out = dict(msg)
    blocks = []
    for b in msg.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "text":
            b = {**b, "text": pmap.restore(b.get("text") or "")}
        elif isinstance(b, dict) and b.get("type") == "tool_use":
            b = {**b, "input": restore_value(b.get("input"), pmap)}
        blocks.append(b)
    if "content" in msg:
        out["content"] = blocks
    return out


class SSERestorer:
    """Rewrites an Anthropic event stream (`text/event-stream`) on its way to the CLI: the text
    deltas and the tool-input deltas get the values back, block by block; a pseudonym cut
    between two deltas is completed before the block ends."""

    def __init__(self, pmap: PseudoMap):
        self.pmap, self.buf, self.blocks = pmap, b"", {}

    def _event(self, name: str, data: dict) -> bytes:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()

    def _one(self, raw: bytes) -> bytes:
        lines = raw.decode("utf-8", "replace").split("\n")
        name = next((ln[6:].strip() for ln in lines if ln.startswith("event:")), "")
        data_txt = "\n".join(ln[5:].lstrip() for ln in lines if ln.startswith("data:"))
        try:
            data = json.loads(data_txt) if data_txt else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return raw + b"\n\n"
        t = data.get("type") or name
        name = name or t
        if t == "content_block_start":
            cb = data.get("content_block") or {}
            idx = data.get("index")
            if cb.get("type") == "text":
                self.blocks[idx] = StreamRestorer(self.pmap)
                data = {**data, "content_block": {**cb, "text": self.blocks[idx].feed(cb.get("text") or "")}}
            elif cb.get("type") == "tool_use":
                self.blocks[idx] = StreamRestorer(self.pmap, json_mode=True)
                data = {**data, "content_block": {**cb, "input": restore_value(cb.get("input"), self.pmap)}}
            else:
                return raw + b"\n\n"
            return self._event(name, data)
        if t == "content_block_delta":
            d = data.get("delta") or {}
            r = self.blocks.get(data.get("index"))
            if r is not None and d.get("type") == "text_delta":
                data = {**data, "delta": {**d, "text": r.feed(d.get("text") or "")}}
            elif r is not None and d.get("type") == "input_json_delta":
                data = {**data, "delta": {**d, "partial_json": r.feed(d.get("partial_json") or "")}}
            else:
                return raw + b"\n\n"   # thinking, signature…: as Claude wrote it
            return self._event(name, data)
        if t == "content_block_stop":
            idx = data.get("index")
            r = self.blocks.pop(idx, None)
            head = b""
            if r is not None:
                rest = r.flush()
                if rest:
                    kind = "input_json_delta" if r.json_mode else "text_delta"
                    key = "partial_json" if r.json_mode else "text"
                    head = self._event("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                               "delta": {"type": kind, key: rest}})
            return head + raw + b"\n\n"
        return raw + b"\n\n"          # any other event: relayed as it came

    def feed(self, chunk: bytes) -> bytes:
        self.buf += (chunk or b"").replace(b"\r\n", b"\n")
        out = []
        while b"\n\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n\n", 1)
            if raw.strip():
                out.append(self._one(raw))
        return b"".join(out)

    def close(self) -> bytes:
        raw, self.buf = self.buf, b""
        return self._one(raw) if raw.strip() else b""
