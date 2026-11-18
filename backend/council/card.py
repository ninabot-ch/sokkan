"""card.py — the agreement card: ONE card the CEO reads before deciding.

Same conventions as `backend/approvalcard.py` (kind, subject, where, facts [{label, value}],
preview — in the person's words, no identifier in what is read), plus the structured
`synthesis` the room draws and the `findings` of the fact-check (« points to check »).
`kind` is a word of the UI's dictionary (`council.card.agreement`).
"""
from __future__ import annotations

import approvalcard

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


def preview(syn: dict, findings: list[dict], lang: str) -> str:
    t = L[lang if lang in L else "en"]
    b = syn["budget"]
    out = [f"## {t['perimetre']}", syn["perimetre"]["resume"]]
    out += [f"- {x}" for x in syn["perimetre"]["inclus"]]
    out += ["", f"## {t['budget']}", f"| {t['poste']} | {t['amount']} | {t['source']} |", "|---|---:|---|"]
    out += [f"| {x['poste']} | {x['montant_chf']:.2f} | {x['source']} |" for x in b["lignes"]]
    out += [f"| **{t['total']}** | **{b['total_chf']:.2f}** | |", "", f"## {t['risks']}"]
    out += [f"- ({x['gravite']}) {x['risque']} — {x['responsable']}" for x in syn["risques"]]
    out += ["", f"## {t['kanban']}"]
    out += [f"- {k['titre']} — {k['responsable']} — {t['done']} : {k['critere_de_fin']}" for k in syn["kanban"]]
    out += ["", f"## {t['agents']}"]
    out += [f"- {a['nom']} ({a['palier']}) : {a['profil']} — {a['perimetre']}" for a in syn["agents_a_recruter"]]
    out += ["", f"## {t['humans']}"]
    out += [f"- {h['profil']} — {h['pourcentage_poste']:g} % {t['post']}" for h in syn["roles_humains"]]
    out += ["", f"## {t['calendar']}"]
    out += [f"- {c['echeance']} : {c['jalon']}" + (f" ({c['responsable']})" if c["responsable"] else "") for c in syn["calendrier"]]
    if findings:
        out += ["", f"## {t['check']}"] + [f"- {f['text']}" for f in findings]
    return "\n".join(out)


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
