"""export.py — a council as JSON (everything, for a replay) and as Markdown (to read and file)."""
from __future__ import annotations

import time

from . import roles, store


def snapshot(cid: str) -> dict:
    c = store.get(cid)
    c.pop("pii_map", None)                       # the correspondence table never leaves the machine
    return {"council": c, "documents": store.documents(cid, with_text=False), "journal": store.turns(cid)}


def _ts(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + "Z"


def markdown(cid: str) -> str:
    s = snapshot(cid)
    c, lang = s["council"], s["council"]["lang"]
    fr = lang == "fr"
    out = [f"# {c['title']}", "",
           f"- {'État' if fr else 'State'} : {c['state']}",
           f"- {'Jetons' if fr else 'Tokens'} : {c['tokens_in'] + c['tokens_out']} / {c['max_tokens']}",
           f"- {'Coût' if fr else 'Cost'} : CHF {c['cost_chf']:.2f} / {c['max_cost_chf']:.2f}",
           f"- {'Projet' if fr else 'Project'} : {c['project']}", "", f"## {'Brief' if fr else 'Brief'}", "", c["brief"], ""]
    if s["documents"]:
        out += [f"## {'Documents joints' if fr else 'Attached documents'}", ""] + [f"- {d['name']}" for d in s["documents"]] + [""]
    out += [f"## {'Journal' if fr else 'Journal'}", ""]
    rnd = None
    for x in s["journal"]:
        if x["round"] != rnd:
            rnd = x["round"]
            out += [f"### {'Manche' if fr else 'Round'} {rnd}", ""]
        who = roles.text(x["role"], lang)["label"] if x["role"] in roles.ORDER else (
            "CEO" if x["kind"] == "ceo" else "SOKKAN")
        meta = f"{_ts(x['ts'])}" + (f" · {x['model']} · {x['tokens_in']}+{x['tokens_out']} tok · {x['ms']} ms" if x["model"] else "")
        to = f" → {x['addressed_to']}" if x["addressed_to"] and x["kind"] == "reply" else ""
        out += [f"**{who}{to}** — _{meta}_", "", x["text"], ""]
    if c.get("card"):
        out += [f"## {'Carte d’accord' if fr else 'Agreement card'}", "", c["card"]["preview"], ""]
    if c.get("accepted"):
        a = c["accepted"]
        out += [f"## {'Acceptée' if fr else 'Accepted'}", "",
                f"- {len(a['cards'])} {'cartes' if fr else 'cards'}, {len(a['agents'])} {'brouillons d’agents' if fr else 'agent drafts'}"]
        out += [f"- {'Poste à pourvoir' if fr else 'Open position'} : {p['profil']} ({p['pourcentage_poste']:g} %)" for p in c["positions"]] + [""]
    return "\n".join(out)
