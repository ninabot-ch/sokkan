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
def iban_ok(raw: str) -> bool:
    s = re.sub(r"\s", "", raw or "").upper()
    if not re.fullmatch(r"(CH|LI)\d{2}[0-9A-Z]{17}", s):
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1


def avs_ok(raw: str) -> bool:
    d = re.sub(r"\D", "", raw or "")
    if len(d) != 13 or not d.startswith("756"):
        return False
    total = sum(int(x) * (3 if i % 2 else 1) for i, x in enumerate(d[:12]))
    return (10 - total % 10) % 10 == int(d[12])


# ---- the patterns --------------------------------------------------------------------------
_UP = "A-ZÀ-ÖØ-Þ"
_LO = "a-zà-öø-ÿß"
_WORD = rf"[{_UP}][{_LO}'’]+(?:-[{_UP}][{_LO}]+)?"            # Dupont, D'Amico, Muller-Brandt
_PARTICLE = r"(?:de|du|des|von|van|der|di|da|dos|le|la|d['’])"
_NAME = rf"{_WORD}(?:\s+(?:{_PARTICLE}\s+)?{_WORD}){{0,2}}"

_IBAN = re.compile(r"(?<![0-9A-Za-z])(?:CH|LI)\d{2}(?:[ ]?[0-9A-Z]){17}(?![0-9A-Za-z])")
_AVS = re.compile(r"(?<!\d)756[.\s]?\d{4}[.\s]?\d{4}[.\s]?\d{2}(?!\d)")
_EMAIL = re.compile(r"(?<![\w.+\-])[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,24}(?![\w\-])")
_PHONE = re.compile(
    r"(?<![\w+])(?:"
    r"(?:\+|00)41([ .\-]?)(?:\(0\)[ ]?)?\d{2}\1\d{3}\1\d{2}\1\d{2}"           # +41 79 123 45 67
    r"|(?:\+|00)33([ .\-]?)(?:\(0\)[ ]?)?[1-9]\2\d{2}\2\d{2}\2\d{2}\2\d{2}"   # +33 6 12 34 56 78
    r"|0\d{2}([ .\-]?)\d{3}\3\d{2}\3\d{2}"                                     # 079 123 45 67
    r"|0[1-9]([ .\-]?)\d{2}\4\d{2}\4\d{2}\4\d{2}"                              # 06 12 34 56 78
    r")(?![\d])")
_MONTHS = (r"janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|"
           r"décembre|decembre|january|february|march|april|may|june|july|august|september|october|"
           r"november|december|januar|februar|märz|maerz|juni|juli|oktober|dezember")
_DATE = (rf"\d{{1,2}}[./\-]\d{{1,2}}[./\-](?:19|20)\d{{2}}|(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}"
         rf"|\d{{1,2}}(?:er|\.)?\s+(?:{_MONTHS})\s+(?:19|20)\d{{2}}")
_BIRTH = re.compile(
    r"(?i:\bn[ée]+(?:\(e\))?\s+le|\bdate\s+de\s+naissance|\bnaissance|\bborn(?:\s+on)?|\bdate\s+of\s+birth"
    r"|\bd\.?o\.?b\.?|\bgeburtsdatum|\bgeboren(?:\s+am)?)"
    rf"[^\d\n]{{0,15}}?({_DATE})", re.IGNORECASE)
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
_CIVILITY = re.compile(
    rf"(?<![\w.])(?:Monsieur|Madame|Mademoiselle|MM\.|M\.|Mme|Mlle|Me|Maître|Dr|Dre|Docteur|Herr|Frau"
    rf"|Mr\.?|Mrs\.?|Ms\.?|Prof\.?)\s+({_NAME})")
_LABELLED = re.compile(
    rf"(?i:\b(?:nom\s+et\s+prénom|nom\s+complet|prénom\s+et\s+nom|nom|prénom|full\s+name|name|vorname"
    rf"|employée?|collaborat(?:eur|rice)|candidate?|titulaire|signataire|destinataire|patient(?:e)?))"
    rf"[ \t]*:[ \t]*({_NAME})")


def _norm(category: str, value: str) -> str:
    """The key that makes two spellings of one value one value (spaces in an IBAN, case of a
    name)."""
    v = unicodedata.normalize("NFC", value or "").strip()
    if category in ("iban", "avs", "phone"):
        v = re.sub(r"[^0-9A-Za-z+]", "", v).upper()
        if category == "phone":       # +41 79…, 0041 79…, 079… are one number
            v = re.sub(r"^(?:\+|00)(41|33)(?:0)?", r"0", v)
            v = v.replace("(0)", "")
        return v
    if category == "email":
        return v.lower()
    return re.sub(r"\s+", " ", v).casefold()


def _literal(value: str) -> re.Pattern:
    """A known name, anywhere: its words with any spacing, as written or in capitals."""
    words = [re.escape(w) for w in value.split()]
    body = r"\s+".join(words)
    up = r"\s+".join(re.escape(w.upper()) for w in value.split())
    return re.compile(rf"(?<![\w\-])(?:{body}|{up})(?![\w\-])")


def detect(text: str, names: list[str] | tuple[str, ...] = (), known: list[tuple[str, str]] = ()) -> list[Finding]:
    """Every piece of personal data found in ``text``, without overlap, in text order.
    ``names``: people the organisation knows (directory, signatories). ``known``: (category,
    value) already in the mapping of the work, found again wherever they are written."""
    if not text:
        return []
    raw: list[tuple[int, Finding]] = []

    def add(cat: str, start: int, end: int) -> None:
        val = text[start:end].strip(" ,")
        if val:
            s = start + (len(text[start:end]) - len(text[start:end].lstrip(" ,")))
            raw.append((ORDER.index(cat), Finding(s, s + len(val), cat, val)))

    for m in _IBAN.finditer(text):
        if iban_ok(m.group(0)):
            add("iban", m.start(), m.end())
    for m in _AVS.finditer(text):
        if avs_ok(m.group(0)):
            add("avs", m.start(), m.end())
    for m in _EMAIL.finditer(text):
        add("email", m.start(), m.end())
    for m in _PHONE.finditer(text):
        add("phone", m.start(), m.end())
    for m in _BIRTH.finditer(text):
        add("birthdate", m.start(1), m.end(1))
    for m in _ADDRESS.finditer(text):
        if re.search(r"\d", m.group(0)):         # a street without a number is not an address
            add("address", m.start(), m.end())
    for m in _CIVILITY.finditer(text):
        add("person", m.start(1), m.end(1))
    for m in _LABELLED.finditer(text):
        add("person", m.start(1), m.end(1))
    # a surname met with its first name or civility is found again alone (« Mme Claire Exemple »
    # … « Exemple viendra »): its own pseudonym, so that no part of a name leaks
    people = [f.value for _, f in raw if f.category == "person"] + [n for n in names if n]
    people += [v for c, v in known if c == "person"]
    surnames = [w for w in (re.split(r"\s+", p.strip())[-1] for p in people if len(p.split()) > 1)
                if len(w) >= 4 and w[:1].isupper()]
    seen_lit: set[str] = set()
    for cat, value in [*(("person", n) for n in names), *known, *(("person", w) for w in surnames)]:
        value = (value or "").strip()
        key = f"{cat}\0{_norm(cat, value)}"
        if len(value) < 3 or key in seen_lit or cat not in ("person", "address"):
            continue
        seen_lit.add(key)
        for m in _literal(value).finditer(text):
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
_KEEP_KEYS = {"type", "id", "tool_use_id", "name", "signature", "media_type", "cache_control", "role",
              "model", "citations", "is_error"}


def anonymize_payload(payload: dict, pmap: PseudoMap, names: list[str] | tuple[str, ...] = (),
                      lang: str = "fr") -> tuple[dict, list[Finding]]:
    """A copy of a /v1/messages request with every text a person or a document wrote
    pseudonymised: the system prompt, the messages, the tools' results and inputs. A thinking
    block is left untouched (Claude wrote it from pseudonyms; it is signed). An image or a PDF
    sent as bytes cannot be checked: it is replaced by a sentence saying it was not sent. The
    tool definitions (the cockpit's own words) are not personal and stay as they are."""
    out = copy.deepcopy(payload)
    found: list[Finding] = []

    def text(s: str) -> str:
        new, f = pmap.anonymize(s, names)
        found.extend(f)
        return new

    def walk(x):
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
            return {k: (v if k in _KEEP_KEYS else walk(v)) for k, v in x.items()}
        return x

    if "system" in out:
        out["system"] = walk(out["system"])
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
