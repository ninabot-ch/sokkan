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
           "after": "Si vous acceptez : {k} cartes (Backlog), {a} brouillons d'agents, {h} postes à pourvoir. Rien n'est lancé.",
           "poste": "Poste", "amount": "Montant CHF", "source": "Source", "owner": "Responsable", "done": "Critère de fin",
           "severity": "Gravité", "post": "de poste"},
    "en": {"where": "The launch council — your agreement is requested", "perimetre": "Scope", "budget": "Budget",
           "total": "Total", "risks": "Risks", "kanban": "Cards to create", "agents": "Agents to recruit (drafts)",
           "humans": "Open positions", "calendar": "Schedule", "check": "To check",
           "after": "If you accept: {k} cards (Backlog), {a} agent drafts, {h} open positions. Nothing is launched.",
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


def summary(syn: dict, findings: list[dict], lang: str, version: int = 1) -> str:
    """The card in words, one paragraph: what the finance lead's long turn says before it folds."""
    b = syn["budget"]
    est = sum(1 for x in b["lignes"] if schema.is_estimated(x["source"]))
    n = {"k": len(syn["kanban"]), "r": len(syn["risques"]), "a": len(syn["agents_a_recruter"]),
         "h": len(syn["roles_humains"]), "c": len(findings)}
    total = f"{b['total_chf']:,.2f}".replace(",", "'")
    if lang == "fr":
        return (f"Synthèse v{version} : budget CHF {total} ({est} ligne(s) sur {len(b['lignes'])} estimée(s)), {n['r']} risque(s), "
                f"{n['k']} carte(s) kanban, {n['a']} agent(s) à recruter, {n['h']} poste(s) humain(s), {n['c']} point(s) à vérifier. "
                "Dépliez une partie pour la lire.")
    return (f"Synthesis v{version}: budget CHF {total} ({est} of {len(b['lignes'])} line(s) estimated), {n['r']} risk(s), "
            f"{n['k']} kanban card(s), {n['a']} agent(s) to recruit, {n['h']} human role(s), {n['c']} point(s) to check. "
            "Unfold a part to read it.")


def readable(syn: dict, findings: list[dict], lang: str, version: int = 1) -> str:
    """What the thread shows of the synthesis turn: the summary, then the sections (the room folds them)."""
    return summary(syn, findings, lang, version) + "\n\n" + preview(syn, findings, lang)


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
            "after": t["after"].format(k=len(syn["kanban"]), a=len(syn["agents_a_recruter"]), h=len(syn["roles_humains"])),
            "synthesis": syn, "findings": findings, "version": council["refusals"] + 1}
