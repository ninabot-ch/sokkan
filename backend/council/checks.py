"""checks.py — light, deterministic checks on what the finance lead hands back (no model).

  assess_amount   where a budget figure comes from in the document it cites: said plainly
                  (« sourced »), taken from a RANGE (« 6 à 11 kCHF »), carried by a hypothesis
                  marker ([H], [À VÉRIFIER], « hypothèse », « ≈ »…), or not in the document at all
                  (derived). Only the first one is « sourced »; the others stay « estimated ».
  references      an article of law, a standard, a regulation cited by the card that appears in no
                  attached document: reported « hypothesis to check ».
  scrub_people    a person's name in a human role or a recruit that is neither in the documents,
                  nor in the brief, nor in the organisation's directory: removed, reported.

Nothing here blocks the card: a check that finds something adds a line under « To check ».
"""
from __future__ import annotations

import re

_NUM = r"\d{1,3}(?:[ '’  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_SUF = r"(?:\s?(?P<{n}>[kKM])(?=CHF|EUR|USD|[$€]|\W|$))?"
_TOKEN = re.compile(r"(?<![\w.,])(?P<num>" + _NUM + r")" + _SUF.format(n="suf"))
_RANGE = re.compile(r"(?<![\w.,])(?P<a>" + _NUM + r")" + _SUF.format(n="sa") + r"\s*(?:à|–|—|-|…|to|bis)\s*(?P<b>" + _NUM + r")"
                    + _SUF.format(n="sb"))
MARKERS = re.compile(r"\[\s*H\s*\]|\[\s*[ÀA]\s*V[ÉE]RIFIER\s*\]|hypoth[èe]se|[àa] v[ée]rifier|to be verified|"
                     r"\bestim|≈|~|fourchette|\bassum", re.I)
_MULT = {"k": 1e3, "K": 1e3, "M": 1e6}


def _val(tok: str) -> float:
    t = re.sub(r"[ '’  ]", "", tok)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        t = re.sub(r"[.,]", "", t)
    return float(t.replace(",", "."))


def numbers(text: str) -> list[tuple[float, int, int]]:
    out = []
    for m in _TOKEN.finditer(text or ""):
        try:
            out.append((_val(m.group("num")) * _MULT.get(m.group("suf") or "", 1), m.start(), m.end()))
        except ValueError:
            continue
    return out


def ranges(text: str) -> list[tuple[float, float, int, int]]:
    out = []
    for m in _RANGE.finditer(text or ""):
        try:
            a, b = _val(m.group("a")), _val(m.group("b"))
        except ValueError:
            continue
        ma = _MULT.get(m.group("sa") or "", 1)
        mb = _MULT.get(m.group("sb") or "", 1)
        if ma == 1 and mb != 1 and a < b:          # « 6 à 11 kCHF »: the unit is said once
            ma = mb
        lo, hi = a * ma, b * mb
        if lo < hi:
            out.append((lo, hi, m.start(), m.end()))
    return out


def _line(text: str, pos: int) -> str:
    i = text.rfind("\n", 0, pos) + 1
    j = text.find("\n", pos)
    return text[i:j if j >= 0 else len(text)]


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.5, abs(b) * 0.002)


def assess_amount(amount: float, text: str) -> dict:
    """{status: plain|range|marked|derived, range: (lo, hi)|None} for a figure against ONE document."""
    rs = ranges(text)
    plain, marked = False, False
    for v, s, e in numbers(text):
        if not _close(v, amount):
            continue
        if any(rs_ <= s and e <= re_ for _lo, _hi, rs_, re_ in rs):
            continue                                  # a bound of a range is not a stated figure
        if MARKERS.search(_line(text, s)):
            marked = True
        else:
            plain = True
    if plain:
        return {"status": "plain", "range": None}
    if marked:                                        # stated, but under a hypothesis marker: not a measure
        return {"status": "marked", "range": None}
    inside = [(lo, hi) for lo, hi, _s, _e in rs if lo - 0.5 <= amount <= hi + 0.5]
    if inside:
        return {"status": "range", "range": min(inside, key=lambda r: r[1] - r[0])}
    return {"status": "derived", "range": None}


def fmt_amount(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


# ---- references ----------------------------------------------------------------------------
_DASH = re.compile(r"[‑–—−]")
_ART = re.compile(r"\b[Aa]rt(?:icles?|s?\.)\s*(\d+[a-z]?)\b")
_NORM = re.compile(r"\b(?:EN|IEC|ISO|CISPR|ETSI|UL|ANSI)\b(?:[\s/]+(?:EN|IEC|ISO|CEI))*\s*(\d{3,6}(?:-\d+)*)")
_UN = re.compile(r"\bUN\s?(\d{2}\.\d)\b")
_REG = re.compile(r"(?:\((?:UE|EU|CE|EC)\)|r[èe]glement|regulation|directive)\s*(?:\((?:UE|EU|CE|EC)\)\s*)?(?:n[°o]\s*)?(\d{4}/\d{2,4})", re.I)
_RS = re.compile(r"\bRS\s*(\d{3}(?:\.\d+)*)")


def _flat(text: str) -> str:
    return _DASH.sub("-", (text or "").replace(" ", " ")).lower()


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def references(syn: dict) -> list[tuple[str, str]]:
    """(label, kind) of every article / standard / regulation the card cites, once each."""
    seen: dict[str, tuple[str, str]] = {}
    for s in _strings(syn):
        s = _DASH.sub("-", s)
        for rx, kind in ((_ART, "article"), (_NORM, "norm"), (_UN, "un"), (_REG, "regulation"), (_RS, "rs")):
            for m in rx.finditer(s):
                seen.setdefault(f"{kind}:{m.group(1).lower()}", (m.group(0).strip(), kind))
    return list(seen.values())


def _in_sources(label: str, kind: str, flat: str) -> bool:
    key = _flat(label)
    if kind == "article":
        num = re.search(r"\d+[a-z]?", key).group(0)
        return bool(re.search(rf"art(?:icles?|s?\.)\s*{re.escape(num)}\b", flat))
    if kind == "un":
        return bool(re.search(r"\bun\s?" + re.escape(re.search(r"\d{2}\.\d", key).group(0)), flat))
    num = re.search(r"\d[\d./-]*\d|\d", key).group(0)
    return num in flat


def unverified_references(syn: dict, sources: str, lang: str = "fr") -> list[dict]:
    flat = _flat(sources)
    out = []
    for label, kind in references(syn):
        if not _in_sources(label, kind, flat):
            out.append({"kind": "unverified_reference", "severity": "warn", "ref": label,
                        "text": (f"« {label} » est cité mais n'apparaît dans aucun document joint : hypothèse à vérifier." if lang == "fr"
                                 else f"“{label}” is cited but appears in no attached document: hypothesis to check.")})
    return out


# ---- people ---------------------------------------------------------------------------------
_CAP = r"[A-ZÀ-ÝŒ][a-zà-ÿœ'’-]{2,}"
_PAREN = re.compile(r"\s*\(\s*(" + _CAP + r"(?:\s+" + _CAP + r"){0,2})\s*\)")
_AFTER = re.compile(r"\s+(?:pour|avec|par|chez|for|with|by)\s+(" + _CAP + r"(?:\s+" + _CAP + r")?)\b")
_PAIR = re.compile(r"(?<!^)\b(" + _CAP + r"\s+" + _CAP + r")\b")
# words of a job title or a field that are not a person
_NOT_A_NAME = set("""produit produits électronique electronique mécanique mecanique logiciel sécurité securite qualité qualite
juridique conformité conformite achats finance financier comptable commercial marketing technique technicien ingénieur ingenieur
responsable lead senior junior chef directeur directrice manager product engineer backend frontend embarqué embarque
fullstack devops data testeur designer opérations operations projet programme prototype industriel industrielle
fournisseurs fournisseur support qualification certification hardware software firmware système systeme mécatronique
mecatronique opérateur operateur assistant assistante conseiller conseillère légal legal rh ressources humaines
développeur developpeur développeuse analyste architecte expert experte spécialiste specialiste coordinateur coordinatrice
temps partiel plein année mois semaine""".split())


def _known(token: str, flat: str, names: list[str]) -> bool:
    t = token.lower()
    if t in _NOT_A_NAME:
        return True
    if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", flat):
        return True
    return any(t in n.lower().split() for n in names)


def _scrub_field(value: str, flat: str, names: list[str]) -> tuple[str, list[str]]:
    removed: list[str] = []

    def any_unknown(m):
        toks = m.group(1).split()
        if all(t.isupper() or _known(t, flat, names) for t in toks):
            return m.group(0)
        removed.append(m.group(1))
        return ""

    def all_unknown(m):
        if any(_known(t, flat, names) for t in m.group(1).split()):
            return m.group(0)
        removed.append(m.group(1))
        return ""

    out = _AFTER.sub(any_unknown, _PAREN.sub(any_unknown, value))
    out = _PAIR.sub(all_unknown, out)
    out = re.sub(r"\s{2,}", " ", out).strip(" -—–,;:")
    out = re.sub(r"\s+(?:pour|avec|par|chez|for|with|by)$", "", out) if removed else out
    return out, removed


def scrub_people(syn: dict, sources: str, names: list[str] | tuple[str, ...] = (), lang: str = "fr") -> tuple[dict, list[dict]]:
    """A person's name in a human role or a recruit that figures neither in the documents, the
    brief, nor the directory: removed (the job title stays), reported."""
    import json
    out = json.loads(json.dumps(syn))
    flat = _flat(sources)
    names = list(names)
    findings: list[dict] = []
    for key, fields in (("roles_humains", ("profil", "perimetre")), ("agents_a_recruter", ("profil", "perimetre"))):
        for x in out.get(key) or []:
            for f in fields:
                new, removed = _scrub_field(x.get(f) or "", flat, names)
                if removed:
                    if f == "profil" and not new:
                        new = "Poste à définir" if lang == "fr" else "Post to define"
                    x[f] = new
                    for r in dict.fromkeys(removed):
                        findings.append({"kind": "person_name_removed", "severity": "warn", "name": r,
                                         "text": (f"Le nom « {r} » ne figure ni dans les documents ni dans l'annuaire : retiré, intitulé de poste seulement ({new})." if lang == "fr"
                                                  else f"The name “{r}” is in neither the documents nor the directory: removed, job title only ({new}).")})
    return out, findings
