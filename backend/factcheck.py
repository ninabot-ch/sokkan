#!/usr/bin/env python3
"""factcheck.py — the check BEFORE the approval card of a deliverable (4.0, « fiabilité »).

The audit of 17.11.2026 (one agent per job, the 27B of the box) found the most dangerous
failure of the Atelier: a deliverable that LOOKS finished and is wrong — an offer with three
different totals for the same variant (310'700 / 119'880 / 93'960), « T. Bovet » become
« Théo Bovet » and copied into the next minutes, a Swiss register written « under article 30
of the GDPR », a work certificate with a coded reservation and an invented reason for leaving.
The person validates it: nothing told them.

This module reads the document an agent is about to save and compares it with itself and with
the sources of the work (the project's documents, the recalled memory; the earlier deliverables
of the project as SECOND-HAND sources). Deterministic and cheap — no model call, no token:

* figures — the same total said with different amounts; a table whose total is not the sum of
  its lines; quantity × unit price ≠ amount; VAT ≠ rate × base, total incl. VAT ≠ base + VAT;
  the old Swiss VAT rate;
* dates — a weekday that does not match its date (« jeudi 14.01.2027 » is a Thursday?);
* people and organisations — a first name the sources never give (« T. Bovet » → « Théo »),
  a full name found only in an earlier deliverable, an organisation named with another word
  than in the sources (« résidence Bellerive » for « Hôtel Bellerive »);
* Swiss law by default — the GDPR (and its words: DPO, « hors UE ») in a Swiss document, an
  nLPD article cited for the wrong subject, an article cited without a source and without
  « à vérifier »; in a work certificate, a coded reservation or a reason for leaving that no
  source gives;
* tone and form (style, never blocking) — « tu » in a deliverable when the organisation says
  « vous », amounts written « 8 120,50 » instead of 8'120.50, straight quotes.

`check(text, sources, ...)` → a list of findings ``{kind, severity, fr, en, data}``;
severity: ``block`` (the agent is asked to correct before the card; the card warns if it
remains), ``warn`` (shown on the card), ``style`` (given to the agent with a correction,
never shown alone). The targeted re-read is the agent's own: the session sends it the
findings once (agentchat.py), not a second deliverable to write from scratch.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import unicodedata

# ---- the organisation's defaults (4.0: until the organisation profile carries them) -------


def org_country() -> str:
    """The organisation's country — Switzerland unless said otherwise (SOKKAN_ORG_COUNTRY)."""
    return (os.environ.get("SOKKAN_ORG_COUNTRY") or "CH").strip().upper()[:2] or "CH"


def org_address_form() -> str:
    """How the organisation addresses people in French: « vous » unless it chose « tu »
    (SOKKAN_ORG_ADDRESS_FORM=tu)."""
    v = (os.environ.get("SOKKAN_ORG_ADDRESS_FORM") or "vous").strip().lower()
    return "tu" if v in ("tu", "tutoiement", "du") else "vous"


# ---- numbers -------------------------------------------------------------------------------
_SEP = "'’\u2019 \u00a0\u202f"
_NUM = re.compile(
    r"(?<![\w.,/'’-])(\d{1,3}(?:[" + _SEP + r"]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"(?:\s?(k|mio|m)(?=\s|$|[^\w]))?(?![\w'’]|[.,]\d)", re.I)


def parse_number(s: str) -> float | None:
    s = s.strip()
    m = _NUM.fullmatch(s) or _NUM.match(s)
    if not m:
        return None
    raw, mult = m.group(1), (m.group(2) or "").lower()
    raw = re.sub(r"[" + _SEP + r"]", "", raw).replace(",", ".")
    try:
        v = float(raw)
    except ValueError:
        return None
    return v * {"k": 1e3, "m": 1e6, "mio": 1e6}.get(mult, 1)


def numbers(s: str) -> list[float]:
    """The numbers of a text, Swiss and French writings alike: 310'700 · 8 120,50 · 93960 ·
    31.2k. A percentage, a date (14.01.2027) or a reference (KNX-ACT8) is not an amount."""
    out = []
    for m in _NUM.finditer(s or ""):
        after = s[m.end():m.end() + 3]
        if after.lstrip().startswith("%"):
            continue
        v = parse_number(m.group(0))
        if v is not None:
            out.append(v)
    return out


def _amounts(s: str) -> list[float]:
    """Numbers that read as amounts (not a year, not a lone small count)."""
    s = re.sub(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b", " ", s or "")          # dates
    s = re.sub(r"\b(19|20)\d{2}\b(?![" + _SEP + r"]\d)", " ", s)          # years
    s = re.sub(r"\b\d+(?:[.,]\d+)?\s?%", " ", s)                          # rates
    s = re.sub(r"\b[A-Z]{2,}[-_]?\w*\d\w*\b", " ", s)                     # references
    return numbers(s)


def swiss(v: float) -> str:
    """8120.5 → 8'120.50 ; 93960 → 93'960."""
    neg = v < 0
    v = abs(v)
    whole = int(v)
    cents = round((v - whole) * 100)
    if cents == 100:
        whole, cents = whole + 1, 0
    s = f"{whole:,}".replace(",", "'")
    if cents:
        s += f".{cents:02d}"
    return ("-" if neg else "") + s


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, 0.005 * max(abs(a), abs(b)))


# ---- text helpers ------------------------------------------------------------------------
def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("\u2019", "'").replace("\u2018", "'").lower()


def _strip_md(s: str) -> str:
    return re.sub(r"[*_`#>]+", "", s or "")


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}")


def _tables(lines: list[str]) -> list[tuple[int, list[list[str]]]]:
    """Markdown tables → (first line index, rows); the header is rows[0]."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            start, rows = i, []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.match(r"\s*\|?[\s:\-|]+\|?\s*$", lines[i]):
                    rows.append([_strip_md(c).strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if len(rows) >= 2:
                out.append((start, rows))
            continue
        i += 1
    return out


def _flat(text: str) -> list[str]:
    """The text as lines, a table row read as « cell cell cell »."""
    out = []
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        if _TABLE_SEP.match(ln):
            continue
        if ln.lstrip().startswith("|"):
            ln = "  ".join(c.strip() for c in ln.strip().strip("|").split("|"))
        out.append(_strip_md(ln))
    return out


# ---- findings ----------------------------------------------------------------------------
def _f(kind: str, severity: str, fr: str, en: str, **data) -> dict:
    return {"kind": kind, "severity": severity, "fr": fr, "en": en, "data": data}


# ---- 1. totals said several times with different amounts --------------------------------
_TOTAL = re.compile(r"\b(total|totaux|gesamt\w*|summe)\b", re.I)
_SUBTOTAL = re.compile(r"\b(sous[- ]?tota(?:l|ux)|subtotal|sub-total|zwischensumme|total partiel)\b", re.I)
_KEY_STOP = {"total", "totaux", "montant", "prix", "ht", "hors", "taxe", "taxes", "chf", "fr",
             "eur", "net", "brut", "general", "globale", "global", "le", "la", "les", "de", "du",
             "des", "d", "l", "en", "final", "finale", "definitif", "definitive", "offre", "cout",
             "couts", "sum", "summe", "gesamt", "amount", "the", "of", "excl", "vat", "tva",
             "non", "incluse", "inclus", "comprise", "soit", "et", "pour", "voir", "est", "sont",
             "a:", "par", "an", "annuel", "mensuel"}


def _key(label: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", fold(label))
    ttc = "ttc" in words or "inkl" in words or "incl" in words
    words = [w for w in words if w not in _KEY_STOP and w not in ("ttc", "inkl", "incl")
             and not w.isdigit()]
    return tuple(words) + (("ttc",) if ttc else ())


def _total_statements(text: str) -> list[tuple[tuple[str, ...], float, str]]:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[tuple[tuple[str, ...], float, str]] = []
    in_table = set()
    for start, rows in _tables(lines):
        n = len(rows) + 1
        in_table.update(range(start, start + n + 1))
        head = rows[0]
        for r in rows[1:]:
            label = r[0] if r else ""
            if not _TOTAL.search(label) or _SUBTOTAL.search(label):
                continue
            for j, cell in enumerate(r[1:], 1):
                vals = _amounts(cell)
                if len(vals) != 1:
                    continue
                col = head[j] if j < len(head) else ""
                if re.search(r"qt|quant|unit|taux|%", fold(col)):
                    continue
                k = _key(label + " " + col)
                if k:
                    out.append((k, vals[0], f"{label} · {col}".strip(" ·")))
    for i, ln in enumerate(lines):
        if i in in_table or ln.lstrip().startswith("|"):
            continue
        s = _strip_md(ln)
        m = _TOTAL.search(s)
        if not m or _SUBTOTAL.search(s):
            continue
        rest = s[m.start():]
        first = _NUM.search(re.sub(r"\b\d+(?:[.,]\d+)?\s?%", " ", rest))
        if not first:
            continue
        label = rest[:first.start()]
        if len(label) > 90:      # a sentence that says « total » somewhere, not a total line
            continue
        vals = _amounts(rest[first.start():first.start() + 40])
        if not vals:
            continue
        k = _key(label)
        if k:
            out.append((k, vals[0], label.strip(" :—–-")))
    return out


def _totals(text: str) -> list[dict]:
    stm = _total_statements(text)
    keys = sorted({k for k, _, _ in stm}, key=len)
    # a short key merges into the one longer key that contains it (« variante a » into
    # « variante a complete »), never into two (« variante » stays apart from A and B)
    canon: dict[tuple, tuple] = {}
    for k in keys:
        longer = [o for o in keys if len(o) > len(k) and set(k) <= set(o)]
        roots = {canon.get(o, o) for o in longer}
        canon[k] = roots.pop() if len(roots) == 1 else k
    groups: dict[tuple, list[tuple[float, str]]] = {}
    for k, v, label in stm:
        c = canon.get(k, k)
        while canon.get(c, c) != c:
            c = canon[c]
        groups.setdefault(c, []).append((v, label))
    out = []
    for k, vals in groups.items():
        distinct: list[float] = []
        for v, _ in vals:
            if not any(_close(v, d) for d in distinct):
                distinct.append(v)
        if len(distinct) > 1:
            label = max((lb for _, lb in vals), key=len)
            said = " / ".join(swiss(v) for v in distinct)
            out.append(_f("totals", "block",
                          f"{len(distinct)} totaux différents pour « {label} » : {said}",
                          f"{len(distinct)} different totals for « {label} »: {said}",
                          label=label, values=distinct))
    return out


# ---- 2. tables: lines and sums -----------------------------------------------------------
_QTY = re.compile(r"\b(qte|qt|quantite|quant|nombre|nb|menge|qty|quantity|heures)\b")
_UNIT = re.compile(r"(unit|\bp\.?\s?u\b|prix unitaire|tarif|einzel|rate\b)")
_AMOUNT = re.compile(r"(montant|total|amount|betrag|prix total|cout)")


def _tables_check(text: str) -> list[dict]:
    out = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for _, rows in _tables(lines):
        head = [fold(h) for h in rows[0]]
        q = next((j for j, h in enumerate(head) if _QTY.search(h)), None)
        u = next((j for j, h in enumerate(head) if _UNIT.search(h)), None)
        a = next((j for j, h in reversed(list(enumerate(head)))
                  if _AMOUNT.search(h) and j not in (q, u)), None)
        if q is not None and u is not None and a is not None:
            for r in rows[1:]:
                if max(q, u, a) >= len(r) or _TOTAL.search(r[0] or ""):
                    continue
                qv, uv, av = numbers(r[q]), numbers(r[u]), numbers(r[a])
                if len(qv) == 1 and len(uv) == 1 and len(av) == 1 and not _close(qv[0] * uv[0], av[0]):
                    what = next((c for c in r if c and not numbers(c)), r[0])
                    out.append(_f("line", "block",
                                  f"« {what} » : {swiss(qv[0])} × {swiss(uv[0])} = {swiss(qv[0] * uv[0])}, "
                                  f"le document dit {swiss(av[0])}",
                                  f"« {what} »: {swiss(qv[0])} × {swiss(uv[0])} = {swiss(qv[0] * uv[0])}, "
                                  f"the document says {swiss(av[0])}",
                                  expected=qv[0] * uv[0], found=av[0]))
        # a total row = the sum of the lines above it (from the last subtotal, if any)
        for j in range(1, len(head)):
            if re.search(r"qt|quant|unit|taux|%|date|ref|n°|no\b|heure", head[j]):
                continue
            acc: list[float] = []
            sub_at: int | None = None
            for ri, r in enumerate(rows[1:], 1):
                label = r[0] if r else ""
                vals = _amounts(r[j]) if j < len(r) else []
                if _SUBTOTAL.search(label):
                    if len(vals) == 1:
                        sub_at = len(acc)
                        acc.append(vals[0])
                    continue
                if _TOTAL.search(label):
                    if len(vals) != 1 or len(acc) < 2:
                        break
                    whole = sum(v for i2, v in enumerate(acc) if i2 != sub_at)
                    from_sub = sum(acc[sub_at:]) if sub_at is not None else whole
                    if not (_close(vals[0], whole) or _close(vals[0], from_sub)):
                        out.append(_f("sum", "block",
                                      f"« {label} » ({rows[0][j] or 'montant'}) : la somme des lignes donne "
                                      f"{swiss(whole)}, le document dit {swiss(vals[0])}",
                                      f"« {label} » ({rows[0][j] or 'amount'}): the lines add up to "
                                      f"{swiss(whole)}, the document says {swiss(vals[0])}",
                                      expected=whole, found=vals[0]))
                    break
                if len(vals) == 1:
                    acc.append(vals[0])
    return out


# ---- 3. VAT ------------------------------------------------------------------------------
_VAT_RATE = re.compile(r"\b(?:tva|mwst|vat|taxe sur la valeur ajout[ée]e)\b[^\n%]{0,25}?(\d{1,2}(?:[.,]\d{1,2})?)\s?%", re.I)
CH_VAT = (8.1, 3.8, 2.6)          # since 01.01.2024 (normal, accommodation, reduced)
CH_VAT_OLD = (7.7, 3.7, 2.5)


def _vat(text: str, country: str) -> list[dict]:
    out = []
    flat = _flat(text)
    rate = None
    for ln in flat:
        m = _VAT_RATE.search(ln)
        if m:
            rate = float(m.group(1).replace(",", "."))
            if country == "CH" and rate in CH_VAT_OLD:
                new = CH_VAT[CH_VAT_OLD.index(rate)]
                out.append(_f("vat_rate", "block",
                              f"Taux de TVA {str(rate).replace('.', ',')} % : en Suisse c'est "
                              f"{str(new).replace('.', ',')} % depuis le 01.01.2024",
                              f"VAT rate {rate} %: in Switzerland it is {new} % since 01.01.2024",
                              rate=rate))
            break
    if rate is None:
        return out

    def first(rx: str, exclude: str = "") -> float | None:
        for ln in flat:
            f = fold(ln)
            if re.search(rx, f) and not (exclude and re.search(exclude, f)):
                vals = _amounts(re.sub(r"\d+(?:[.,]\d+)?\s?%", " ", ln))
                if vals:
                    return vals[-1]
        return None
    base = first(r"(total|sous-total|montant|subtotal)\b.*\b(ht|hors tax|excl|netto|net)\b", r"ttc|incl")
    vat = first(r"^\s*(?:[-•*]\s*)?(?:montant de (?:la )?)?(tva|mwst|vat)\b", r"\b(ht|hors|excl|non (?:incluse|comprise))\b")
    ttc = first(r"\b(ttc|tva (?:incluse|comprise)|incl\.? (?:tva|vat|mwst)|inkl)\b")
    if base and vat and not _close(base * rate / 100, vat):
        out.append(_f("vat", "block",
                      f"TVA {str(rate).replace('.', ',')} % de {swiss(base)} = {swiss(base * rate / 100)}, "
                      f"le document dit {swiss(vat)}",
                      f"VAT {rate} % of {swiss(base)} = {swiss(base * rate / 100)}, the document says {swiss(vat)}",
                      expected=base * rate / 100, found=vat))
    if base and vat and ttc and not _close(base + vat, ttc):
        out.append(_f("vat_total", "block",
                      f"Total TTC : {swiss(base)} + {swiss(vat)} = {swiss(base + vat)}, le document dit {swiss(ttc)}",
                      f"Total incl. VAT: {swiss(base)} + {swiss(vat)} = {swiss(base + vat)}, the document says {swiss(ttc)}",
                      expected=base + vat, found=ttc))
    return out


# ---- 4. weekday ↔ date -------------------------------------------------------------------
_DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_DAYS_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAYS_DE = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre",
     "octobre", "novembre", "decembre"])}
_MONTHS.update({m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])})
_DAY_RX = re.compile(
    r"\b(" + "|".join(_DAYS_FR + _DAYS_EN + _DAYS_DE) + r")\s*,?\s+(?:le\s+|the\s+|den\s+)?"
    r"(\d{1,2})(?:er|st|nd|rd|th|\.)?\s*(?:(\d{1,2})\.(\d{4}|\d{2})\b|([a-z]+)\s+(\d{4})\b)")


def _weekdays(text: str) -> list[dict]:
    out = []
    for m in _DAY_RX.finditer(fold(text)):
        day = m.group(1)
        idx = next(lst.index(day) for lst in (_DAYS_FR, _DAYS_EN, _DAYS_DE) if day in lst)
        try:
            if m.group(3):
                y = int(m.group(4))
                y = y + 2000 if y < 100 else y
                d = _dt.date(y, int(m.group(3)), int(m.group(2)))
            else:
                month = _MONTHS.get(m.group(5) or "")
                if not month:
                    continue
                d = _dt.date(int(m.group(6)), month, int(m.group(2)))
        except ValueError:
            out.append(_f("date", "block", f"Date impossible : « {m.group(0)} »",
                          f"Impossible date: « {m.group(0)} »", said=m.group(0)))
            continue
        if d.weekday() != idx:
            out.append(_f("weekday", "block",
                          f"« {m.group(0)} » : le {d.strftime('%d.%m.%Y')} est un {_DAYS_FR[d.weekday()]}",
                          f"« {m.group(0)} »: {d.isoformat()} is a {_DAYS_EN[d.weekday()].capitalize()}",
                          said=m.group(0), weekday=d.weekday()))
    return out


# ---- 5. people and organisations ---------------------------------------------------------
_UP = "A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜ"
_LOW = "a-zàâäçéèêëîïôöùûüÿœæ"
_NAME = re.compile(rf"(?<![\w.])([{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)?)\s+([{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)?)(?![\w])")
_ENTITY_WORDS = {
    "hotel", "residence", "immeuble", "societe", "restaurant", "clinique", "regie", "commune",
    "ecole", "maison", "villa", "domaine", "centre", "garage", "promotion", "fondation",
    "association", "banque", "groupe", "cafe", "hopital", "ems", "fiduciaire", "cabinet",
    "entreprise", "boulangerie", "pharmacie", "salle", "projet", "chantier", "dossier",
    "client", "variante", "rue", "avenue", "chemin", "route", "place", "quai", "lac", "parc",
    "monsieur", "madame", "points", "point", "comite", "direction", "service", "conseil",
    "budget", "objet", "lieu", "date", "total", "montant", "contrat", "article", "annexe",
}
_NOT_FIRST = {"le", "la", "les", "un", "une", "des", "du", "de", "pour", "avec", "dans", "sur",
              "selon", "chez", "par", "et", "ou", "en", "au", "aux", "ce", "cet", "cette",
              "nous", "vous", "notre", "votre", "nos", "vos", "son", "sa", "ses", "leur",
              "the", "our", "your", "dear", "cher", "chere", "bonjour", "merci", "monsieur",
              "madame", "mme", "mlle", "herr", "frau", "objet", "lieu", "heure", "presents",
              "excuses", "responsable", "decision", "discussion", "echeance", "prochaine",
              "prochain", "signature", "valider", "rediger", "verifier", "confirmer", "suisse",
              "geneve", "lausanne", "vaud", "berne", "zurich", "bale", "fribourg", "neuchatel",
              "valais", "jura", "tessin", "crissier", "vevey", "morges", "nyon", "sion"}


def _name_pairs(text: str) -> list[tuple[str, str]]:
    seen, out = set(), []
    for line in _flat(text):
        for m in _NAME.finditer(line):
            first, last = m.group(1), m.group(2)
            if re.match(rf"\s+(?:[{_UP}][\w-]+|SA\b|AG\b)", line[m.end():]):
                continue       # « Morand Domotique SA », « Newsletter Morand Domotique »: a firm
            if fold(first) in _NOT_FIRST or fold(first) in _ENTITY_WORDS:
                continue
            if fold(last) in _NOT_FIRST or fold(last) in _ENTITY_WORDS:
                continue
            if (first, last) not in seen:
                seen.add((first, last))
                out.append((first, last))
    return out


def _word_in(word: str, folded: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(fold(word))}(?![a-z0-9])", folded) is not None


def _people(text: str, primary: str, secondary: str) -> list[dict]:
    """A « First Last » of the deliverable whose LAST name the sources know but whose first
    name they never give with it."""
    out = []
    p, s = fold(primary), fold(secondary)
    for first, last in _name_pairs(text):
        full = f"{first} {last}"
        if not (_word_in(last, p) or _word_in(last, s)):
            continue           # a name the sources do not know at all: not ours to judge
        if _word_in(full, p) or re.search(
                rf"(?<![a-z]){re.escape(fold(last))}\s*,?\s+{re.escape(fold(first))}(?![a-z])", p):
            continue           # said so in a primary source (« Bovet Thomas » too)
        initials = sorted({m.group(1) for m in re.finditer(
            rf"(?<![\w.])([{_UP}])\.?\s*{re.escape(last)}(?![\w])", primary)})
        others = sorted({m.group(1) for m in re.finditer(
            rf"(?<![\w])([{_UP}][{_LOW}]+)\s+{re.escape(last)}(?![\w])", primary)
            if fold(m.group(1)) not in _NOT_FIRST and fold(m.group(1)) not in _ENTITY_WORDS
            and m.group(1) != first})
        if others:
            out.append(_f("name", "block",
                          f"« {full} » : les pièces disent « {others[0]} {last} »",
                          f"« {full} »: the sources say « {others[0]} {last} »",
                          name=full, sources=f"{others[0]} {last}"))
        elif initials and _word_in(first, p) and fold(first)[0] in {fold(x) for x in initials}:
            continue           # « C. Favre » and « Claire » in the sources: plausible enough
        elif initials:
            said = f"{initials[0]}. {last}"
            out.append(_f("name", "block",
                          f"« {full} » : les pièces disent seulement « {said} », le prénom « {first} » "
                          "n'y figure pas",
                          f"« {full} »: the sources only say « {said} », the first name « {first} » "
                          "is not in them", name=full, sources=said))
        elif _word_in(full, s):
            out.append(_f("name_second_hand", "warn",
                          f"« {full} » ne vient que d'un livrable précédent, pas d'une pièce : à vérifier",
                          f"« {full} » comes only from an earlier deliverable, not from a source: to check",
                          name=full))
        elif not initials and re.search(rf"\b(?:m\.|mme|monsieur|madame|herr|frau)\s+{re.escape(fold(last))}\b", p) \
                and not _word_in(first, p):
            out.append(_f("name", "block",
                          f"« {full} » : le prénom « {first} » ne figure dans aucune pièce",
                          f"« {full} »: the first name « {first} » is in no source",
                          name=full))
    return out


_ORG_NOUN = re.compile(
    r"\b(h[oô]tel|r[ée]sidence|immeuble|soci[ée]t[ée]|restaurant|clinique|r[ée]gie|commune|"
    r"[ée]cole|maison|villa|domaine|centre|garage|fondation|association|banque|groupe|caf[ée]|"
    r"h[oô]pital|ems|fiduciaire|cabinet|boulangerie|pharmacie)\s+(?:(?:du|de la|des|de|les|le|la|l')\s*)?"
    rf"([{_UP}][\w'-]+)", re.I)


def _orgs(text: str, primary: str) -> list[dict]:
    out, seen = [], set()
    src: dict[str, set[str]] = {}
    for m in _ORG_NOUN.finditer(primary or ""):
        src.setdefault(fold(m.group(2)), set()).add(fold(m.group(1)))
    for m in _ORG_NOUN.finditer(text or ""):
        noun, name = fold(m.group(1)), fold(m.group(2))
        nouns = src.get(name)
        if not nouns or noun in nouns or (noun, name) in seen:
            continue
        seen.add((noun, name))
        said = next(iter(sorted(nouns)))
        orig = re.search(rf"\b({said[:3]}\w*)\s+(?:(?:du|de la|des|de|les|le|la|l')\s*)?{re.escape(fold(m.group(2)))}",
                         fold(primary))
        said_txt = (primary[orig.start():orig.end()] if orig else f"{said} {m.group(2)}")
        out.append(_f("org", "block",
                      f"« {m.group(0)} » : les pièces disent « {said_txt} »",
                      f"« {m.group(0)} »: the sources say « {said_txt} »",
                      name=m.group(0), sources=said_txt))
    return out


# ---- 6. Swiss law by default -------------------------------------------------------------
# What a deterministic rule can know for sure. Federal Act on Data Protection (nLPD, RS 235.1,
# in force since 01.09.2023) and Code of Obligations (CO, RS 220): the article that carries a
# subject — an nLPD article cited on that subject with another number is wrong.
LAW_SOURCES = {"nLPD": "RS 235.1 — https://www.fedlex.admin.ch/eli/cc/2022/491/fr",
               "CO": "RS 220 — https://www.fedlex.admin.ch/eli/cc/27/317_321_377/fr"}
NLPD_TOPICS = [
    (re.compile(r"registre des (?:activites de )?traitement|registre des traitements"), {"12"}, "registre des activités de traitement"),
    (re.compile(r"conseiller a la protection|\bdpo\b|delegue a la protection"), {"10"}, "conseiller à la protection des données"),
    (re.compile(r"securite des donnees|mesures de securite"), {"8"}, "sécurité des données"),
    (re.compile(r"des la conception|par defaut"), {"7"}, "protection dès la conception et par défaut"),
    (re.compile(r"sous-trait"), {"9"}, "sous-traitance"),
    (re.compile(r"analyse d.impact"), {"22", "23"}, "analyse d'impact"),
    (re.compile(r"violations? de (?:la )?securite|annonce des violations"), {"24"}, "annonce des violations de la sécurité"),
    (re.compile(r"droit d.acces"), {"25", "26"}, "droit d'accès"),
    (re.compile(r"devoir d.inform"), {"19", "20"}, "devoir d'informer"),
    (re.compile(r"communication .{0,20}etranger|hors de suisse|transferts? .{0,20}etranger"), {"16", "17"}, "communication à l'étranger"),
]
NLPD_ARTICLES = {str(n) for n in range(1, 75)}      # the nLPD has 74 articles, no « bis »
CO_KNOWN = {"330a": "certificat de travail", "958f": "conservation des livres",
            "335c": "délais de congé", "336": "congé abusif", "336c": "congé en temps inopportun"}
_ART = re.compile(
    r"\b(?:art(?:icle)?s?\.?|§)\s*(\d+[a-z]?(?:\s?(?:bis|ter|quater))?)"
    r"(?:\s*(?:al(?:in[ée]a)?\.?|abs\.?|para(?:graph)?\.?)\s*\d+)?(?:\s*(?:let\.|lit\.)\s*\w)?"
    r"(?:[.\d]*)\s*(?:,|de la|du|de l'|des|of the|der|des)?\s*"
    r"(nLPD|LPD|nDSG|DSG|FADP|RGPD|GDPR|DSGVO|CO|OR|CC|ZGB|LTr|OLPD|LCD|LP)\b", re.I)
_EU_TERMS = re.compile(r"\b(RGPD|GDPR|DSGVO)\b|\bDPO\b|d[ée]l[ée]gu[ée] [àa] la protection des donn[ée]es|"
                       r"\bhors (?:de l')?UE\b|\bpays tiers\b", re.I)


def _law(text: str, ask: str, sources: str, country: str) -> list[dict]:
    out: list[dict] = []
    if country != "CH":
        return out
    t, a, s = text or "", fold(ask), fold(sources)
    eu_asked = re.search(r"\b(rgpd|gdpr|dsgvo|ue|union europeenne|europe)\b", a) is not None
    if not eu_asked:
        terms = sorted({m.group(0) for m in _EU_TERMS.finditer(t)}, key=str.lower)
        if terms:
            out.append(_f("law_eu", "block",
                          "Termes du droit européen dans un document suisse : " + ", ".join(f"« {x} »" for x in terms)
                          + " — la loi applicable est la nLPD (conseiller à la protection des données, "
                          "communication à l'étranger), sauf si la demande vise l'UE",
                          "EU-law terms in a Swiss document: " + ", ".join(f"« {x} »" for x in terms)
                          + " — the law that applies is the nLPD (FADP), unless the request is about the EU",
                          terms=terms))
    unverified: list[str] = []
    for line in _flat(t):
        fl = fold(line)
        for m in _ART.finditer(line):
            num = re.sub(r"\s+", "", m.group(1).lower())
            law = m.group(2)
            lawf = {"lpd": "nLPD", "nlpd": "nLPD", "ndsg": "nLPD", "dsg": "nLPD", "fadp": "nLPD",
                    "or": "CO"}.get(law.lower(), law.upper() if law.lower() != "nlpd" else "nLPD")
            cited = f"art. {num} {lawf}"
            if lawf == "nLPD":
                wrong = next(((topic, arts) for rx, arts, topic in NLPD_TOPICS
                              if rx.search(fl) and num not in arts), None)
                if wrong:
                    out.append(_f("law_article", "block",
                                  f"« {m.group(0).strip()} » cité pour « {wrong[0]} » : c'est l'art. "
                                  f"{' / '.join(sorted(wrong[1], key=int))} nLPD (RS 235.1)",
                                  f"« {m.group(0).strip()} » cited for « {wrong[0]} »: it is art. "
                                  f"{' / '.join(sorted(wrong[1], key=int))} nLPD (RS 235.1)",
                                  cited=cited))
                    continue
                if num not in NLPD_ARTICLES:
                    out.append(_f("law_article", "block",
                                  f"« {m.group(0).strip()} » : cet article n'existe pas dans la nLPD (RS 235.1)",
                                  f"« {m.group(0).strip()} »: no such article in the nLPD (RS 235.1)",
                                  cited=cited))
                    continue
            if lawf in ("RGPD", "GDPR", "DSGVO"):
                continue        # said by law_eu
            known = (lawf == "CO" and num in CO_KNOWN) or (lawf == "nLPD" and any(
                num in arts for _, arts, _ in NLPD_TOPICS))
            in_src = re.search(rf"\bart(?:icle)?s?\.?\s*{re.escape(num)}\b", s) is not None
            hedged = re.search(r"verifi|a confirmer|to be checked|zu pruefen|zu prufen", fl)
            if not (known or in_src or hedged):
                unverified.append(cited)
    if unverified:
        uniq = sorted(set(unverified))
        out.append(_f("law_unsourced", "block",
                      "Articles de loi cités sans source : " + ", ".join(uniq)
                      + " — les sourcer (texte fourni, fedlex) ou écrire « à vérifier par un juriste »",
                      "Articles cited without a source: " + ", ".join(uniq)
                      + " — give the source or write « to be checked by a lawyer »", cited=uniq))
    return out


_CERT = re.compile(r"certificats? de travail|attestations? de travail|arbeitszeugnis|zwischenzeugnis|work certificate|employment reference", re.I)
_CODED = [
    (re.compile(r"\bparfois\b"), "parfois"),
    (re.compile(r"\bdans l'ensemble\b"), "dans l'ensemble"),
    (re.compile(r"\bs'est (?:toujours )?efforce|\bs'effor[cç]ait\b"), "s'est efforcé"),
    (re.compile(r"\bfait preuve de comprehension\b"), "a fait preuve de compréhension"),
    (re.compile(r"(?<!entiere )(?<!pleine )(?<!totale )\ba notre satisfaction\b"), "à notre satisfaction (sans « entière »)"),
    (re.compile(r"\ben general\b|\bla plupart du temps\b|\bpas toujours\b"), "en général / pas toujours"),
    (re.compile(r"\bplus (?:longue?|lent(?:e)?) a\b|\bplus de temps\b"), "plus long à…"),
    (re.compile(r"\bdans la mesure de ses (?:moyens|possibilites)\b"), "dans la mesure de ses moyens"),
    (re.compile(r"\bquelques difficultes\b|\bdes difficultes\b"), "difficultés"),
    (re.compile(r"\ba progresse\b|\ben progres\b"), "a progressé"),
    (re.compile(r"\bsociable\b|\bconvivial(?:e)?\b"), "sociable / convivial"),
]
_LEAVING = re.compile(
    r"(?:son|leur) depart (?:s'inscrit|est motive|fait suite|est du|resulte)|"
    r"quitte (?:notre|l')(?:entreprise|societe)[^.]{0,30}\b(?:pour|afin|a sa demande|de son plein gre)|"
    r"\ba sa (?:propre )?demande\b|\bd'un commun accord\b|\blicenci|\bresiliation\b|\bvolonte de (?:se )?\w+",
    re.I)
_LEAVING_SRC = re.compile(r"motif (?:du|de|de son) depart|raison (?:du|de son) depart|souhaite que le motif|"
                          r"a donne sa demission|a demissionne|a resilie son contrat|licencie|"
                          r"depart a (?:sa|la) demande|retraite", re.I)


def _certificate(text: str, ask: str, sources: str) -> list[dict]:
    if not (_CERT.search(ask or "") or _CERT.search((text or "")[:400])):
        return []
    out = []
    t, s = fold(text), fold(sources)
    hits = []
    for line in _flat(text):
        fl = fold(line)
        if re.search(r"points? a confirmer|a verifier|formule|alternative", fl):
            continue        # the agent's own list of wordings to check
        for rx, label in _CODED:
            m = rx.search(fl)
            if m:
                hits.append((label, line.strip()[:140]))
    if hits:
        out.append(_f("certificate_coded", "block",
                      "Formule réservée ou codée dans un certificat de travail : "
                      + "; ".join(f"« {h[0]} »" for h in hits[:4])
                      + " — le certificat suisse est véridique et bienveillant (art. 330a CO) : "
                      "dire le fait positivement ou le taire",
                      "Reserved or coded wording in a work certificate: "
                      + "; ".join(f"« {h[0]} »" for h in hits[:4])
                      + " — a Swiss certificate is truthful and benevolent (art. 330a CO)",
                      lines=[h[1] for h in hits]))
    for line in _flat(text):
        fl = fold(line)
        if re.search(r"points? a confirmer|preciser|a verifier|si (?:c'est|le motif)", fl):
            continue
        m = _LEAVING.search(fl)
        if m and not _LEAVING_SRC.search(s):
            out.append(_f("certificate_leaving", "block",
                          f"Motif de départ sans source : « {line.strip()[:120]} » — en Suisse il ne figure "
                          "qu'à la demande de l'employé·e",
                          f"Reason for leaving without a source: « {line.strip()[:120]} » — in Switzerland it "
                          "is given only at the employee's request", line=line.strip()))
            break
    _ = t
    return out


# ---- 7. tone and form (style) -------------------------------------------------------------
_TU = re.compile(r"\btu (?:as|es|peux|veux|dois|vas|sais|trouveras|recevras|pourras)\b|\btoi\b|"
                 r"\b(?:dis|fais|sache|n'h[eé]site)-moi\b|\b(?:veux|peux|dois)-tu\b|"
                 r"\bt'(?:ai|en|y|invite|envoie)\b|\bte (?:remercie|propose|souhaite|prie|rappelle|joins)\b|"
                 r"\bn'h[eé]site pas\b", re.I)
_FR_AMOUNT = re.compile(r"\b\d{1,3}(?:[ \u00a0\u202f]\d{3})+,\d{2}\b|\b\d+,\d{2}\s?(?:CHF|Fr\.?|francs)")
_STRAIGHT_Q = re.compile(r'(?<![=\w])"([^"\n]{2,80})"(?![\w=])')


def _style(text: str, lang: str, country: str, form: str) -> list[dict]:
    if lang != "fr":
        return []
    out = []
    body = "\n".join(ln for ln in _flat(text))
    if form == "vous":
        m = _TU.search(body)
        if m:
            out.append(_f("tone_tu", "warn",
                          f"Tutoiement dans le document (« {m.group(0)} ») : l'organisation vouvoie",
                          f"« tu » in the document (« {m.group(0)} »): the organisation says « vous »",
                          said=m.group(0)))
    if country == "CH":
        m = _FR_AMOUNT.search(body)
        if m:
            v = parse_number(m.group(0).split("CHF")[0].split("Fr")[0].split("francs")[0])
            out.append(_f("format_amount", "style",
                          f"Montant écrit « {m.group(0)} » : en Suisse « {swiss(v) if v is not None else '8’120.50'} »",
                          f"Amount written « {m.group(0)} »: Swiss form « {swiss(v) if v is not None else '8’120.50'} »",
                          said=m.group(0)))
        m = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", body)
        if m:
            out.append(_f("format_date", "style",
                          f"Date écrite « {m.group(0)} » : en Suisse « {m.group(0).replace('/', '.')} »",
                          f"Date written « {m.group(0)} »: Swiss form « {m.group(0).replace('/', '.')} »",
                          said=m.group(0)))
    m = _STRAIGHT_Q.search(body)
    if m:
        out.append(_f("typo_quotes", "style",
                      f"Guillemets droits « \"{m.group(1)[:30]}\" » : en français « … » avec espaces insécables",
                      f"Straight quotes « \"{m.group(1)[:30]}\" »: French uses « … »", said=m.group(0)))
    return out


# ---- entry point -------------------------------------------------------------------------
def _csv(text: str) -> tuple[str, list[dict]]:
    """A CSV deliverable (the kits' « Excel » tables) → (the same table in Markdown, structure
    findings): every row must have as many fields as the header."""
    import csv
    import io
    body = (text or "").lstrip("\ufeff")
    first = body.split("\n", 1)[0]
    delim = ";" if first.count(";") >= first.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(body), delimiter=delim) if any(c.strip() for c in r)]
    out: list[dict] = []
    if not rows:
        return "", out
    n = len(rows[0])
    bad = [i + 1 for i, r in enumerate(rows) if len(r) != n]
    if bad:
        out.append(_f("csv_fields", "block",
                      f"Tableau cassé : {len(bad)} ligne(s) n'ont pas {n} colonnes comme l'en-tête "
                      f"(lignes {', '.join(map(str, bad[:6]))}) — un « {delim} » dans une cellule doit être "
                      "entre guillemets",
                      f"Broken table: {len(bad)} row(s) do not have the header's {n} fields "
                      f"(rows {', '.join(map(str, bad[:6]))})", rows=bad))
    esc = lambda c: c.replace("|", "/").replace("\n", " ")   # noqa: E731
    md = ["| " + " | ".join(esc(c) for c in rows[0]) + " |", "|" + "---|" * n]
    md += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(md) + "\n", out


def with_computed(name: str, text: str) -> tuple[str, list[dict]]:
    """4.0 native formats (backend/outputs.py, when present): a table total is a FORMULA that
    SOKKAN computes. The checks read the document with each formula replaced by its value, so
    a total CITED in the text is compared with the computed one; a formula SOKKAN could not
    understand is a defect (the file would carry it as text)."""
    try:
        import outputs
    except ImportError:
        return text, []
    out: list[dict] = []
    try:
        summ = outputs.calc_summary(name or "document.md", text)
        bad = [ln[2:] for ln in summ.splitlines() if "FORMULA NOT UNDERSTOOD" in ln]
        if bad:
            out.append(_f("formula", "block",
                          f"{len(bad)} formule(s) que SOKKAN ne comprend pas : "
                          + "; ".join(b.split(" (=")[0] for b in bad[:4])
                          + " — nom de colonne, libellé de ligne, SUM(ABOVE) ou A1 du tableau",
                          f"{len(bad)} formula(s) SOKKAN does not understand: "
                          + "; ".join(b.split(" (=")[0] for b in bad[:4]), cells=bad))
        _meta, body = outputs.front_matter(text)
        return outputs.computed_markdown(body), out
    except Exception:  # noqa: BLE001 — the computation never breaks the check
        return text, out


def check(text: str, *, primary: str = "", secondary: str = "", ask: str = "", lang: str = "fr",
          country: str | None = None, form: str | None = None, name: str = "") -> list[dict]:
    """The findings on one deliverable. ``primary`` = the text of the sources a person gave or
    approved (documents of the project, memory notes, the request); ``secondary`` = earlier
    deliverables of the project (an agent wrote them: an error there must not be confirmed by
    being copied)."""
    country = (country or org_country()).upper()
    form = form or org_address_form()
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text or "")
    primary = re.sub(r"[\u2010-\u2015\u2212]", "-", primary or "")
    if len(text) > 400_000:
        text = text[:400_000]
    primary_all = (primary or "") + "\n" + (ask or "")
    findings: list[dict] = []
    if name.lower().endswith(".csv"):
        text, findings = _csv(text)
    text, computed = with_computed(name, text)
    findings += computed
    for fn in (lambda: _totals(text), lambda: _tables_check(text), lambda: _vat(text, country),
               lambda: _weekdays(text), lambda: _people(text, primary_all, secondary or ""),
               lambda: _orgs(text, primary_all), lambda: _law(text, ask, primary_all, country),
               lambda: _certificate(text, ask, primary_all), lambda: _style(text, lang, country, form)):
        try:
            findings += fn()
        except Exception:  # noqa: BLE001 — a check that breaks never blocks a save
            continue
    # one finding per (kind, message)
    seen, out = set(), []
    for f in findings:
        k = (f["kind"], f["fr"])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f.get("severity") == "block"]


def shown(findings: list[dict]) -> list[dict]:
    """What the approval card shows: blocking and warning findings, never style alone."""
    return [f for f in findings if f.get("severity") in ("block", "warn")]


def agent_message(findings: list[dict]) -> str:
    """What the session tells the agent ONCE, instead of the card: the findings, and to
    correct them in the same document (a targeted re-read, not a new document)."""
    lines = ["SOKKAN checked this document against the sources before showing it to the person "
             "and found these inconsistencies:"]
    lines += [f"- {f['fr'] if f.get('fr') else f['en']}" for f in findings[:12]]
    lines += ["Correct them in the SAME document and save it again in one complete save: take "
              "each figure, name and date from the sources (recompute totals line by line), and "
              "where a source does not settle it, write the fact under « Points à confirmer » "
              "instead of choosing one. Do not mention this check to the person. "
              "(Check before the card.)"]
    return "\n".join(lines)


# the phrase the thread uses to tell this refusal from a failure (plain.POLICY_DENIED)
AGENT_MARK = "(Check before the card.)"
