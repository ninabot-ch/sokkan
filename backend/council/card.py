"""card.py — the agreement card: ONE card the CEO reads before deciding.

Same conventions as `backend/approvalcard.py` (kind, subject, where, facts [{label, value}],
preview — in the person's words, no identifier in what is read), plus the structured
`synthesis` the room draws and the `findings` of the fact-check (« points to check »).
`kind` is a word of the UI's dictionary (`council.card.agreement`).
"""
from __future__ import annotations

import approvalcard

from . import schema

L = {
    "fr": {"where": "Le Conseil de lancement — votre accord est demandé", "perimetre": "Périmètre", "budget": "Budget",
           "total": "Total", "risks": "Risques", "kanban": "Cartes à créer", "agents": "Agents à recruter (brouillons)",
           "humans": "Postes à pourvoir", "calendar": "Calendrier", "check": "À vérifier",
           "after": "Si vous acceptez : {k}, {a}, {h}. Rien n'est lancé.",
           "poste": "Poste", "amount": "Montant CHF", "source": "Source", "owner": "Responsable", "done": "Critère de fin",
           "severity": "Gravité", "post": "de poste"},
    "en": {"where": "The launch council — your agreement is requested", "perimetre": "Scope", "budget": "Budget",
           "total": "Total", "risks": "Risks", "kanban": "Cards to create", "agents": "Agents to recruit (drafts)",
           "humans": "Open positions", "calendar": "Schedule", "check": "To check",
           "after": "If you accept: {k}, {a}, {h}. Nothing is launched.",
           "poste": "Item", "amount": "Amount CHF", "source": "Source", "owner": "Owner", "done": "Done when",
           "severity": "Severity", "post": "of a post"},
}


def _c(x) -> str:
    """A table cell: no pipe, no line break (they would cut the row)."""
    return " ".join(str(x).replace("|", "/").split())


def sections(syn: dict, findings: list[dict], lang: str) -> list[tuple[str, str]]:
    """The card as (title, Markdown body) sections — what the room draws as foldable parts."""
    t = L[lang if lang in L else "en"]
    b = syn["budget"]
    out = []
    per = [syn["perimetre"]["resume"]] + [f"- {x}" for x in syn["perimetre"]["inclus"]]
    if syn["perimetre"]["exclus"]:
        per += ["", ("Hors périmètre" if lang == "fr" else "Out of scope") + " :"] + [f"- {x}" for x in syn["perimetre"]["exclus"]]
    out.append((t["perimetre"], "\n".join(per)))
    rows = [f"| {t['poste']} | {t['amount']} | {t['source']} |", "|---|---:|---|"]
    rows += [f"| {_c(x['poste'])} | {x['montant_chf']:.2f} | {_c(x['source'])} |" for x in b["lignes"]]
    rows.append(f"| {t['total']} | {b['total_chf']:.2f} | |")
    out.append((t["budget"], "\n".join(rows)))
    out.append((t["risks"], "\n".join(f"- ({x['gravite']}) {x['risque']} — {x['responsable']}" for x in syn["risques"])))
    out.append((t["kanban"], "\n".join(f"- {k['titre']} — {k['responsable']} — {t['done']} : {k['critere_de_fin']}" for k in syn["kanban"])))
    out.append((t["agents"], "\n".join(f"- {a['nom']} ({a['palier']}) : {a['profil']} — {a['perimetre']}" for a in syn["agents_a_recruter"])))
    out.append((t["humans"], "\n".join(f"- {h['profil']} — {h['pourcentage_poste']:g} % {t['post']}" for h in syn["roles_humains"])))
    out.append((t["calendar"], "\n".join(f"- {c['echeance']} : {c['jalon']}" + (f" ({c['responsable']})" if c["responsable"] else "")
                                         for c in syn["calendrier"])))
    if findings:
        out.append((t["check"], "\n".join(f"- {f['text']}" for f in findings)))
    return out


def preview(syn: dict, findings: list[dict], lang: str) -> str:
    return "\n\n".join(f"## {title}\n\n{body}" for title, body in sections(syn, findings, lang) if body.strip())


def _n(n: int, one: str, many: str) -> str:
    """« 1 poste », « 2 postes »: no (s) in a sentence a CEO reads."""
    return f"{n} {one if n in (0, 1) else many}"      # French: 0 and 1 are singular


def _en(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary(syn: dict, findings: list[dict], lang: str, version: int = 1) -> str:
    """The card in words, one paragraph: what the finance lead's long turn says before it folds."""
    b = syn["budget"]
    est = sum(1 for x in b["lignes"] if schema.is_estimated(x["source"]))
    n = {"k": len(syn["kanban"]), "r": len(syn["risques"]), "a": len(syn["agents_a_recruter"]),
         "h": len(syn["roles_humains"]), "c": len(findings)}
    total = f"{b['total_chf']:,.2f}".replace(",", "'")
    if lang == "fr":
        return (f"Synthèse v{version} : budget CHF {total} ({est} ligne{'s' if len(b['lignes']) > 1 else ''} estimée{'s' if est > 1 else ''} "
                f"sur {len(b['lignes'])}), {_n(n['r'], 'risque', 'risques')}, {_n(n['k'], 'carte kanban', 'cartes kanban')}, "
                f"{_n(n['a'], 'agent à recruter', 'agents à recruter')}, {_n(n['h'], 'poste humain', 'postes humains')}, "
                f"{_n(n['c'], 'point à vérifier', 'points à vérifier')}. Dépliez une partie pour la lire.")
    return (f"Synthesis v{version}: budget CHF {total} ({est} of {_en(len(b['lignes']), 'line', 'lines')} estimated), "
            f"{_en(n['r'], 'risk', 'risks')}, {_en(n['k'], 'kanban card', 'kanban cards')}, {_en(n['a'], 'agent to recruit', 'agents to recruit')}, "
            f"{_en(n['h'], 'human role', 'human roles')}, {_en(n['c'], 'point to check', 'points to check')}. Unfold a part to read it.")


def readable(syn: dict, findings: list[dict], lang: str, version: int = 1) -> str:
    """What the thread shows of the synthesis turn: the summary, then the sections (the room folds them)."""
    return summary(syn, findings, lang, version) + "\n\n" + preview(syn, findings, lang)


def _after_counts(lang: str, k: int, a: int, h: int) -> dict:
    if lang == "fr":
        return {"k": _n(k, "carte (Backlog)", "cartes (Backlog)"), "a": _n(a, "brouillon d'agent", "brouillons d'agents"),
                "h": _n(h, "poste à pourvoir", "postes à pourvoir")}
    return {"k": _en(k, "card (Backlog)", "cards (Backlog)"), "a": _en(a, "agent draft", "agent drafts"),
            "h": _en(h, "open position", "open positions")}


def build(council: dict, syn: dict, findings: list[dict]) -> dict:
    lang = council["lang"] if council["lang"] in L else "en"
    t = L[lang]
    b = syn["budget"]
    facts = approvalcard._facts(
        (t["perimetre"], syn["perimetre"]["resume"]),
        (t["budget"], f"CHF {b['total_chf']:,.2f}".replace(",", "'")),
        (t["risks"], str(len(syn["risques"]))), (t["kanban"], str(len(syn["kanban"]))),
        (t["agents"], str(len(syn["agents_a_recruter"]))), (t["humans"], str(len(syn["roles_humains"]))),
        (t["check"], str(len(findings))))
    return {"kind": "council_agreement", "subject": council["title"], "where": t["where"], "facts": facts,
            "preview": preview(syn, findings, lang),
            "after": t["after"].format(**_after_counts(lang, len(syn["kanban"]), len(syn["agents_a_recruter"]), len(syn["roles_humains"]))),
            "synthesis": syn, "findings": findings, "version": council["refusals"] + 1}
