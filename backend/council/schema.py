"""schema.py — the DAF's synthesis: STRICT JSON, validated, then fact-checked.

    {"perimetre": {"resume", "inclus": [], "exclus": []}   (a plain string is accepted)
     "budget": {"lignes": [{"poste", "montant_chf", "source"}], "total_chf"},
     "risques": [{"risque", "gravite": faible|moyenne|haute|critique, "responsable"}],
     "kanban": [{"titre", "responsable", "critere_de_fin"}],
     "agents_a_recruter": [{"nom", "profil", "perimetre", "outils": [], "palier": local|ship|deep}],
     "roles_humains": [{"profil", "pourcentage_poste": 0-100, "perimetre"}],
     "calendrier": [{"jalon", "echeance", "responsable"}]}

`parse` extracts the object from a model's answer; `validate` returns the normalised synthesis or
raises `SchemaError` with every problem (the council sends them back to the DAF ONCE);
`factcheck_budget` makes the total equal the sum of the lines and marks every figure that has
no source as « estimé » — a figure is never presented as sourced when it is not.
"""
from __future__ import annotations

import json
import re

from . import checks

GRAVITES = ("faible", "moyenne", "haute", "critique")
_GRAVITE_EN = {"low": "faible", "medium": "moyenne", "high": "haute", "critical": "critique"}
PALIERS = ("local", "ship", "deep")
ESTIMATED = ("estimé", "estime", "estimated", "estimate")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
MAX_ITEMS = 40


class SchemaError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def parse(content: str) -> dict:
    """The JSON object of a model's answer (thinking and code fences removed)."""
    s = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise SchemaError(["the answer holds no JSON object"])
    try:
        d = json.loads(s[i:j + 1])
    except ValueError as e:
        raise SchemaError([f"invalid JSON: {e}"]) from None
    if not isinstance(d, dict):
        raise SchemaError(["the JSON root must be an object"])
    return d


def _s(v, field: str, problems: list[str], *, required: bool = True, max_len: int = 600) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            problems.append(f"{field}: required")
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        problems.append(f"{field}: a text")
        return ""
    return str(v).strip()[:max_len]


def _list(d: dict, key: str, problems: list[str], required: bool = True) -> list:
    v = d.get(key)
    if v is None and not required:
        return []
    if not isinstance(v, list):
        problems.append(f"{key}: a list")
        return []
    if required and not v:
        problems.append(f"{key}: at least one item")
    if len(v) > MAX_ITEMS:
        problems.append(f"{key}: {MAX_ITEMS} items at most")
    return [x for x in v[:MAX_ITEMS]]


def _num(v, field: str, problems: list[str], lo: float = 0, hi: float = 1e9) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        problems.append(f"{field}: a number")
        return 0.0
    try:
        x = float(str(v).replace("'", "").replace(" ", "").replace(",", "."))
    except ValueError:
        problems.append(f"{field}: a number")
        return 0.0
    if not lo <= x <= hi:
        problems.append(f"{field}: between {lo:g} and {hi:g}")
    return x


def validate(d: dict) -> dict:
    p: list[str] = []
    out: dict = {}
    per = d.get("perimetre")
    if isinstance(per, str):
        per = {"resume": per}
    if not isinstance(per, dict):
        p.append("perimetre: a text or {resume, inclus, exclus}")
        per = {}
    out["perimetre"] = {
        "resume": _s(per.get("resume"), "perimetre.resume", p, max_len=1500),
        "inclus": [_s(x, "perimetre.inclus", p) for x in (per.get("inclus") or [])][:MAX_ITEMS],
        "exclus": [_s(x, "perimetre.exclus", p) for x in (per.get("exclus") or [])][:MAX_ITEMS]}

    b = d.get("budget")
    if not isinstance(b, dict):
        p.append("budget: {lignes, total_chf}")
        b = {}
    lignes = []
    for i, x in enumerate(_list(b, "lignes", p)):
        if not isinstance(x, dict):
            p.append(f"budget.lignes[{i}]: an object")
            continue
        lignes.append({"poste": _s(x.get("poste"), f"budget.lignes[{i}].poste", p, max_len=200),
                       "montant_chf": _num(x.get("montant_chf"), f"budget.lignes[{i}].montant_chf", p),
                       "source": _s(x.get("source"), f"budget.lignes[{i}].source", p, required=False, max_len=200)})
    out["budget"] = {"lignes": lignes,
                     "total_chf": _num(b.get("total_chf"), "budget.total_chf", p)}

    risques = []
    for i, x in enumerate(_list(d, "risques", p)):
        if not isinstance(x, dict):
            p.append(f"risques[{i}]: an object")
            continue
        g = _s(x.get("gravite"), f"risques[{i}].gravite", p).lower()
        g = _GRAVITE_EN.get(g, g)
        if g and g not in GRAVITES:
            p.append(f"risques[{i}].gravite: one of {', '.join(GRAVITES)}")
        risques.append({"risque": _s(x.get("risque"), f"risques[{i}].risque", p),
                        "gravite": g, "responsable": _s(x.get("responsable"), f"risques[{i}].responsable", p, max_len=80)})
    out["risques"] = risques

    kanban = []
    for i, x in enumerate(_list(d, "kanban", p)):
        if not isinstance(x, dict):
            p.append(f"kanban[{i}]: an object")
            continue
        kanban.append({"titre": _s(x.get("titre"), f"kanban[{i}].titre", p, max_len=200),
                       "responsable": _s(x.get("responsable"), f"kanban[{i}].responsable", p, max_len=80),
                       "critere_de_fin": _s(x.get("critere_de_fin"), f"kanban[{i}].critere_de_fin", p)})
    out["kanban"] = kanban

    agents = []
    for i, x in enumerate(_list(d, "agents_a_recruter", p, required=False)):
        if not isinstance(x, dict):
            p.append(f"agents_a_recruter[{i}]: an object")
            continue
        nom = _s(x.get("nom"), f"agents_a_recruter[{i}].nom", p, max_len=48).lower()
        if nom and not _NAME.match(nom):
            p.append(f"agents_a_recruter[{i}].nom: kebab-case slug, 2-48 chars")
        tools = x.get("outils") or []
        if not isinstance(tools, list):
            p.append(f"agents_a_recruter[{i}].outils: a list")
            tools = []
        pal = _s(x.get("palier"), f"agents_a_recruter[{i}].palier", p).lower()
        if pal and pal not in PALIERS:
            p.append(f"agents_a_recruter[{i}].palier: one of {', '.join(PALIERS)}")
        agents.append({"nom": nom, "profil": _s(x.get("profil"), f"agents_a_recruter[{i}].profil", p),
                       "perimetre": _s(x.get("perimetre"), f"agents_a_recruter[{i}].perimetre", p, max_len=1500),
                       "outils": [str(t)[:60] for t in tools][:20], "palier": pal})
    out["agents_a_recruter"] = agents

    humains = []
    for i, x in enumerate(_list(d, "roles_humains", p, required=False)):
        if not isinstance(x, dict):
            p.append(f"roles_humains[{i}]: an object")
            continue
        humains.append({"profil": _s(x.get("profil"), f"roles_humains[{i}].profil", p),
                        "pourcentage_poste": _num(x.get("pourcentage_poste"), f"roles_humains[{i}].pourcentage_poste", p, 0, 100),
                        "perimetre": _s(x.get("perimetre"), f"roles_humains[{i}].perimetre", p, required=False, max_len=800)})
    out["roles_humains"] = humains

    cal = []
    for i, x in enumerate(_list(d, "calendrier", p)):
        if not isinstance(x, dict):
            p.append(f"calendrier[{i}]: an object")
            continue
        cal.append({"jalon": _s(x.get("jalon"), f"calendrier[{i}].jalon", p, max_len=200),
                    "echeance": _s(x.get("echeance"), f"calendrier[{i}].echeance", p, max_len=80),
                    "responsable": _s(x.get("responsable"), f"calendrier[{i}].responsable", p, required=False, max_len=80)})
    out["calendrier"] = cal
    if p:
        raise SchemaError(p[:25])
    return out


def is_estimated(source: str) -> bool:
    """« estimé », « estimated », or « estimé (d'après … » — a source that says it is not a measure."""
    s = (source or "").strip().strip("«»\"“” ").lower()
    return s in ESTIMATED or any(s.startswith(e + " ") or s.startswith(e + "(") for e in ESTIMATED)


def _cites(source: str, doc_names: list[str]) -> bool:
    s = (source or "").lower()
    return any(n and n.lower() in s for n in doc_names)


def _md_budget(b: dict) -> str:
    rows = ["| Poste | Montant CHF | Source |", "|---|---|---|"]
    rows += [f"| {x['poste']} | {x['montant_chf']:.2f} | {x['source']} |" for x in b["lignes"]]
    rows.append(f"| Total | {b['total_chf']:.2f} | |")
    return "\n".join(rows)


def factcheck_budget(syn: dict, doc_names: list[str], doc_text: str = "", lang: str = "fr",
                     docs: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """→ (synthesis with the budget corrected, findings). Findings are {kind, severity, text, …}:
    `total_corrected` (the declared total was not the sum of the lines: the sum replaces it),
    `unsourced` (no source, or one that is not an attached document: marked « estimé »),
    `estimated_from_doc` (the cited document does not state the figure: it comes from a RANGE, a
    hypothesis marked [H] / [À VÉRIFIER], or is derived — « estimé (d'après doc, fourchette a–b) »,
    never « sourcé »), `estimated` (the finance lead herself said « estimé »), and whatever
    `backend/factcheck.py` finds in the rendered table. EVERY estimated or unsourced line is one
    finding: the card's « To check » counter counts them."""
    fr = lang == "fr"
    est = "estimé" if fr else "estimated"
    out = json.loads(json.dumps(syn))
    b = out["budget"]
    findings: list[dict] = []
    texts = {d["name"]: d.get("text", "") for d in (docs or [])}
    for x in b["lignes"]:
        src = x["source"]
        if not src:
            x["source"] = est
            findings.append({"kind": "unsourced", "severity": "warn", "poste": x["poste"],
                             "text": (f"« {x['poste']} » : aucune source, marqué estimé." if fr
                                      else f"“{x['poste']}”: no source, marked estimated.")})
        elif is_estimated(src):
            findings.append({"kind": "estimated", "severity": "info", "poste": x["poste"],
                             "text": (f"« {x['poste']} » : montant estimé, pas sourcé." if fr
                                      else f"“{x['poste']}”: estimated amount, not sourced.")})
        elif not _cites(src, doc_names):
            findings.append({"kind": "unsourced", "severity": "warn", "poste": x["poste"],
                             "text": (f"« {x['poste']} » : la source « {src} » n'est pas un document joint, marqué estimé." if fr
                                      else f"“{x['poste']}”: source “{src}” is not an attached document, marked estimated.")})
            x["source"] = est + f" ({src})"
        elif texts and x["montant_chf"]:
            cited = [n for n in doc_names if n and n.lower() in src.lower() and n in texts]
            got = [(n, checks.assess_amount(x["montant_chf"], texts[n])) for n in cited]
            if got and not any(a["status"] == "plain" for _n, a in got):
                name, a = min(got, key=lambda t: ("range", "marked", "derived").index(t[1]["status"]))
                if a["status"] == "range":
                    lo, hi = a["range"]
                    how = (f"fourchette {checks.fmt_amount(lo)}–{checks.fmt_amount(hi)}" if fr
                           else f"range {checks.fmt_amount(lo)}–{checks.fmt_amount(hi)}")
                elif a["status"] == "marked":
                    how = "hypothèse marquée [H] ou à vérifier" if fr else "hypothesis marked [H] or to verify"
                else:
                    how = "chiffre non repris tel quel dans le document" if fr else "figure not stated as such in the document"
                x["source"] = f"{est} ({'d’après' if fr else 'from'} {name}, {how})"
                findings.append({"kind": "estimated_from_doc", "severity": "warn", "poste": x["poste"], "document": name,
                                 "text": (f"« {x['poste']} » : {x['montant_chf']:.0f} CHF n'est pas un chiffre posé par {name} ({how}) : estimé, pas sourcé." if fr
                                          else f"“{x['poste']}”: CHF {x['montant_chf']:.0f} is not a figure stated by {name} ({how}): estimated, not sourced.")})
    total = round(sum(x["montant_chf"] for x in b["lignes"]), 2)
    if abs(total - b["total_chf"]) > 0.005:
        findings.append({"kind": "total_corrected", "severity": "block-fixed", "declared": b["total_chf"], "computed": total,
                         "text": (f"Le total annoncé ({b['total_chf']:.2f}) n'était pas la somme des lignes : remplacé par {total:.2f}." if fr
                                  else f"The declared total ({b['total_chf']:.2f}) was not the sum of the lines: replaced by {total:.2f}.")})
        b["total_chf"] = total
    try:
        import factcheck
        for f in factcheck.check(_md_budget(b), primary=doc_text, ask="", lang=lang, name="budget.md"):
            if f.get("severity") in ("block", "warn") and f.get("kind") not in ("style",):
                findings.append({"kind": "factcheck:" + f["kind"], "severity": f["severity"],
                                 "text": f.get("fr" if fr else "en") or f.get("fr") or ""})
    except Exception:  # noqa: BLE001 — a check that breaks never blocks the card
        pass
    return out, findings
