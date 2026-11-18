"""The parity table and the verdict, from a result file of run.py.

THE THRESHOLD (proposal of 16.11.2026, to be validated) — the kernel holds parity with the
legacy engine when ALL of these hold on the same day, same model, same meter:

Atelier (A*, P ≥ 3 passes each)
  T1  success: per journey, kernel successes ≥ legacy successes − ⌊P/3⌋
  T2  time to first draft: kernel median ≤ 1.25 × legacy median + 5 s
  T3  prompt tokens per journey (on the wire): kernel median ≤ legacy median
  T4  one card per document per turn (both), and cards shown to the person:
      kernel median ≤ legacy median + 1
  T5  parasitic failure lines: 0 on the kernel, over all Atelier runs
Refusals and resume (R*, S*)
  T6  100 % on the kernel, no tolerance (invariants of the product)
Repository (D*, at equal model)
  T7  kernel successes ≥ legacy successes − max(1, ⌈10 % of the tasks⌉)
  T8  wall time: kernel median ≤ 1.5 × legacy median
Conformance
  T9  C1-C8 all green on both engines, the kernel's restart test played (not skipped)
Gaps
  T10 no check « waits for kernel n°X » left — a cut with an open gap is not a cut

Output tokens are reported, not judged (they follow the deliverable's length).
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

GAP = "waits"
NOT_PLAYED = "not played"


def journal_waits(kj: dict | None) -> bool:
    """The decider check (n°3) waits for the kernel only when a card WAS decided and the
    kernel's journal does not name who decided it. No card decided in the pass: nothing to
    check — « non joué », never a kernel gap (the kernel carries on_behalf_of/via)."""
    kj = kj or {}
    if kj.get("status") == NOT_PLAYED:
        return False
    # results written before the « not played » status: no card found = not played
    return kj.get("status") == GAP and int(kj.get("cards_found") or 0) > 0


def _med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 1) if xs else None


def _fmt(x, unit=""):
    if x is None:
        return "—"
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    if isinstance(x, int) and x >= 10000:
        return f"{x / 1000:.1f} k{unit}"
    return f"{x}{unit}"


def group(data: dict) -> dict:
    g: dict = defaultdict(lambda: defaultdict(list))
    for r in data.get("runs", []):
        g[(r["kind"], r["journey"])][r["engine"]].append(r)
    return g


def stats(runs: list[dict]) -> dict:
    played = [r for r in runs if r.get("started")]
    waits = [r for r in runs if r.get("status") == GAP]
    gaps = ({x["gap"] for r in runs for x in r.get("gaps", [])}
                  | {r.get("kernel_journal", {}).get("gap") for r in runs
                     if journal_waits(r.get("kernel_journal"))}
                  | {r.get("unknown_thread", {}).get("gap") for r in runs
                     if r.get("unknown_thread", {}).get("status") == GAP}
                  | {r.get("mcp_headers", {}).get("gap") for r in runs
                     if r.get("mcp_headers", {}).get("status") == GAP}
                  | ({2} if waits else set()))
    gaps = sorted(g for g in gaps if g is not None)
    wire = [r.get("wire") or {} for r in played]
    cards = [r.get("cards") or {} for r in played]
    return {
        "n": len(runs), "played": len(played), "waits": len(waits),
        "success": sum(1 for r in played if (r.get("grade") or {}).get("success")),
        "first_draft_s": _med([r.get("first_draft_s") for r in played]),
        "first_card_s": _med([r.get("first_card_s") for r in played]),
        "end_s": _med([r.get("turn_end_s") or r.get("wall_s") or r.get("answer_s") for r in played]),
        "requests": _med([w.get("requests") for w in wire]),
        "prompt_total": _med([w.get("prompt_total") for w in wire]),
        "prompt_uncached": _med([w.get("prompt_uncached") for w in wire]),
        "prompt_cached": _med([w.get("prompt_cached") for w in wire]),
        "prompt_per_request": _med([w.get("prompt_per_request") for w in wire]),
        "output": _med([w.get("output") for w in wire]),
        "cards_shown": _med([c.get("cards_shown_to_person") for c in cards]),
        "cards_per_doc": max([c.get("max_cards_per_document_per_turn") or 0 for c in cards], default=0),
        "parasitic": sum((r.get("failures") or {}).get("unexpected_tool_failures", 0)
                         + (r.get("failures") or {}).get("hard_errors", 0) for r in played),
        "gaps": gaps,
        "start_errors": [r.get("start_error") for r in runs if not r.get("started")],
    }


def row_verdict(kind: str, L: dict | None, K: dict | None, passes: int) -> tuple[str, list[str]]:
    if K is None:
        return "non joué (noyau)", []
    if K["played"] == 0:
        if K["gaps"]:
            return "attend noyau " + ", ".join(f"n°{g}" for g in K["gaps"]), []
        return "non joué : " + "; ".join(str(e)[:80] for e in K["start_errors"][:1]), []
    why: list[str] = []
    if kind in ("refusal", "resume"):
        if K["success"] < K["played"]:
            why.append(f"T6 {K['success']}/{K['played']}")
    elif L is None or L["played"] == 0:
        return "non comparable (hérité non joué)", []
    elif kind == "atelier":
        if K["success"] < L["success"] - passes // 3:
            why.append(f"T1 {K['success']} < {L['success']}")
        if K["first_draft_s"] is not None and L["first_draft_s"] is not None and \
                K["first_draft_s"] > 1.25 * L["first_draft_s"] + 5:
            why.append(f"T2 {K['first_draft_s']} s > 1,25×{L['first_draft_s']} s")
        if K["first_draft_s"] is None and L["first_draft_s"] is not None:
            why.append("T2 pas de brouillon")
        if (K["prompt_total"] or 0) > (L["prompt_total"] or 0):
            why.append("T3 jetons")
        if K["cards_per_doc"] > 1 or (K["cards_shown"] or 0) > (L["cards_shown"] or 0) + 1:
            why.append("T4 cartes")
        if K["parasitic"]:
            why.append(f"T5 {K['parasitic']} ligne(s) d'échec")
    if K["gaps"]:
        why.append("T10 attend noyau " + ", ".join(f"n°{g}" for g in K["gaps"]))
    if not why:
        return "parité", []
    if all(w.startswith("T10") for w in why):
        return "parité sur ce qui se joue ; " + why[0][4:], why
    return "écart", why


def repo_verdict(g: dict) -> dict:
    tasks = [k for k in g if k[0] == "repo"]
    L = [r for k in tasks for r in g[k].get("legacy", []) if r.get("started")]
    K = [r for k in tasks for r in g[k].get("kernel", []) if r.get("started")]
    ls = sum(1 for r in L if r["grade"]["success"])
    ks = sum(1 for r in K if r["grade"]["success"])
    lw, kw = _med([r.get("wall_s") for r in L]), _med([r.get("wall_s") for r in K])
    why = []
    if K and L:
        tol = max(1, math.ceil(0.1 * len(tasks)))
        if ks < ls - tol:
            why.append(f"T7 {ks} < {ls} − {tol}")
        if kw and lw and kw > 1.5 * lw:
            why.append(f"T8 {kw} s > 1,5×{lw} s")
    return {"tasks": len(tasks), "legacy": f"{ls}/{len(L)}", "kernel": f"{ks}/{len(K)}",
            "legacy_wall_med_s": lw, "kernel_wall_med_s": kw, "why": why,
            "verdict": ("non joué" if not (K and L) else "parité" if not why else "écart")}


def conformance_verdict(c: dict | None) -> dict:
    if not c:
        return {"verdict": "non joué", "why": []}
    why = []
    for name, r in c.items():
        if r["failed"]:
            why.append(f"T9 {name} : {r['failed']} échec(s)")
        if name == "kernel" and r["skipped"]:
            why.append(f"T9 kernel : {r['skipped']} sauté(s) (noyau de test absent ?)")
        if r["passed"] == 0:
            why.append(f"T9 {name} : rien de joué")
    return {"verdict": "vert" if not why else "incomplet", "why": why}


def verdict(data: dict) -> dict:
    g = group(data)
    passes = int(data.get("passes") or 1)
    rows, gaps = [], set()
    for (kind, jid), by in sorted(g.items()):
        if kind == "repo":
            continue
        L = stats(by["legacy"]) if by.get("legacy") else None
        K = stats(by["kernel"]) if by.get("kernel") else None
        v, why = row_verdict(kind, L, K, passes)
        gaps |= set((K or {}).get("gaps") or [])
        rows.append({"journey": jid, "kind": kind, "verdict": v, "why": why})
    repo = repo_verdict(g)
    for k in g:
        if k[0] == "repo":
            for r in g[k].get("kernel", []):
                gaps |= {x["gap"] for x in r.get("gaps", [])}
    conf = conformance_verdict(data.get("conformance"))
    ecarts = [r for r in rows if r["verdict"] == "écart"] + ([repo] if repo["verdict"] == "écart" else [])
    if ecarts:
        overall = "PAS DE PARITÉ"
    elif gaps or conf["verdict"] != "vert" or any(r["verdict"].startswith(("non", "attend")) for r in rows):
        overall = "INCOMPLET"
    else:
        overall = "PARITÉ"
    return {"overall": overall, "gaps_open": sorted(gaps), "rows": rows, "repo": repo,
            "conformance": conf}


def markdown(data: dict) -> str:
    g = group(data)
    # always the current rule (a results file rendered again reads by today's thresholds)
    v = verdict(data)
    out = [f"# Banc de parité noyau / hérité — {data.get('started', '')[:16]}", "",
           f"Commit `{data.get('commit')}` · modèle `{data.get('model')}` · réflexion "
           f"{'active' if data.get('thinking') else 'coupée'} · {data.get('passes')} passe(s) · "
           f"bac du noyau `{data.get('kernel_sandbox')}`", "",
           f"**Verdict : {v['overall']}**"
           + (f" — attend noyau {', '.join('n°' + str(x) for x in v['gaps_open'])}"
              if v["gaps_open"] else ""), "",
           "| Parcours | Réussite hérité | Réussite noyau | 1er brouillon (méd.) h / n | Fin du tour h / n "
           "| Requêtes h / n | Prompt total h / n | dont cache h / n | Sortie h / n | Cartes montrées h / n "
           "| Échecs parasites h / n | Verdict |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    vrow = {r["journey"]: r for r in v["rows"]}
    for (kind, jid), by in sorted(g.items(), key=lambda kv: ("AR SD".find(kv[0][1][0]), kv[0][1])):
        L = stats(by["legacy"]) if by.get("legacy") else None
        K = stats(by["kernel"]) if by.get("kernel") else None

        def pair(key, unit=""):
            return f"{_fmt((L or {}).get(key), unit)} / {_fmt((K or {}).get(key), unit)}"

        def succ(s):
            if s is None:
                return "—"
            if s["played"] == 0:
                return "attend" if s["waits"] or s["gaps"] else "non joué"
            return f"{s['success']}/{s['played']}"
        verdict_txt = vrow.get(jid, {}).get("verdict", "voir dépôt" if kind == "repo" else "")
        why = vrow.get(jid, {}).get("why") or []
        out.append(f"| {jid} | {succ(L)} | {succ(K)} | {pair('first_draft_s', ' s')} | {pair('end_s', ' s')} "
                   f"| {pair('requests')} | {pair('prompt_total')} | {pair('prompt_cached')} | {pair('output')} "
                   f"| {pair('cards_shown')} | {pair('parasitic')} | {verdict_txt}"
                   + (f" ({'; '.join(w for w in why if not w.startswith('T10'))})"
                      if any(not w.startswith('T10') for w in why) else "") + " |")
    r = v["repo"]
    out += ["", f"**Dépôt (Cockpit)** : {r['tasks']} tâche(s) — hérité {r['legacy']}, noyau {r['kernel']}, "
            f"temps mur médian {_fmt(r['legacy_wall_med_s'], ' s')} / {_fmt(r['kernel_wall_med_s'], ' s')} "
            f"→ {r['verdict']}" + (f" ({'; '.join(r['why'])})" if r["why"] else "")]
    c = data.get("conformance") or {}
    if c:
        out += ["", "**Conformité C1-C8** : " + " · ".join(
            f"{k} {x['passed']} verts / {x['failed']} échecs / {x['skipped']} sautés" for k, x in c.items())
            + f" → {v['conformance']['verdict']}"
            + (f" ({'; '.join(v['conformance']['why'])})" if v["conformance"]["why"] else "")]
    waits, not_played = [], []
    for rr in data.get("runs", []):
        kj0 = rr.get("kernel_journal")
        if rr["engine"] == "kernel" and kj0 is not None and not journal_waits(kj0) \
                and kj0.get("status") != "pass":
            not_played.append(f"{rr['journey']} p{rr.get('pass_no')} — décideur dans le journal du "
                              "noyau : non joué (aucune carte décidée dans la passe)")
        if rr["engine"] != "kernel" or rr.get("pass_no") != 1:
            continue
        for x in rr.get("gaps", []):
            waits.append(f"{rr['journey']} — {x['check']} : attend noyau n°{x['gap']}")
        if rr.get("status") == GAP:
            waits.append(f"{rr['journey']} — non lancé ({rr.get('start_error', '')[:90]}) : attend noyau n°2")
        kj = rr.get("kernel_journal") or {}
        if journal_waits(kj):
            s = (kj.get("sample") or [{}])[0]
            waits.append(f"{rr['journey']} — décideur dans le journal du noyau "
                         f"(le noyau note {s.get('kernel_says', {}).get('by', '?')!r}) : attend noyau n°3")
        ut = rr.get("unknown_thread") or {}
        if ut.get("status") == GAP:
            waits.append(f"{rr['journey']} — fil inconnu repris : HTTP {ut.get('http')} au lieu de 404 : "
                         "attend noyau n°4")
        mh = rr.get("mcp_headers") or {}
        if mh.get("status") == GAP:
            waits.append(f"{rr['journey']} — mcp_headers refusé (HTTP {mh.get('http')}) : attend noyau n°16")
    if waits:
        out += ["", "**En attente du noyau** (sautés, motivés — jamais comptés comme réussis) :", ""]
        out += [f"- {w}" for w in sorted(set(waits))]
    if not_played:
        out += ["", "**Contrôles non joués** (rien à vérifier dans la passe — ni réussis, ni en attente) :", ""]
        out += [f"- {w}" for w in sorted(set(not_played))]
    out += ["", "h = hérité (CLI Claude Code), n = noyau. Jetons mesurés sur le fil (requêtes au modèle), "
            "médianes par parcours. Seuil : tests/parity/report.py (T1-T10)."]
    return "\n".join(out) + "\n"
