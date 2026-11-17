"""Real runs of « the coworker knows the house » (4.0) — the cases of the coworker audit of
17.11.2026 replayed on a real engine and a real model, BEFORE (no head: the 3.6.8 Atelier) and
AFTER (the organisation's profile and the project's validated work in the run's head,
backend/househead.py), same documents, same requests, same model:

* ``relances``  — Claire asks for reminder letters for five invoices; the reconciliation of the
  same project, validated two weeks ago, shows two of them paid (audit fin1/fin2);
* ``odj``       — Sophie asks for the agenda of the committee of 14.12, without a document; the
  minutes of 10.11, validated in the project, fix that committee and its actions (audit sop4);
* ``pv``        — Sophie asks for the minutes from her notes, where « T. Bovet » and « SC » are
  written as initials (audit sop1: « Théo Bovet » invented, then copied);

and, for the « remember » step, the offer (backend/learn.py) built from a real thread where the
person corrected the agent.

    PARITY_UPSTREAM=http://127.0.0.1:8004/v1 python tests/orgmemory/real_runs.py --out <dir>
    python tests/orgmemory/real_runs.py --only pv,odj --modes after

The engine is the legacy one (Claude Code CLI) through the parity bench's machinery
(tests/parity: the meter counts every request's tokens, the cockpit's Atelier rules decide the
cards). Fictitious organisation and people; nothing leaves the machine but the model's calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = Path(os.environ.get("ORGMEM_DATA") or tempfile.mkdtemp(prefix="orgmem-"))
# every store of the product is resolved from the data dir AT IMPORT: set it first
os.environ["SOKKAN_DATA_DIR"] = str(DATA)
os.environ["SOKKAN_MEMORY_DIR"] = str(DATA / "memory")
os.environ["SOKKAN_MEMORY_QUARANTINE_DIR"] = str(DATA / "quarantine")
for f in ("MULTI_PROJECT", "DOCUMENTS", "SIMPLE_PROFILE"):
    os.environ[f"SOKKAN_FEATURE_{f}"] = "1"
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "memory"))
sys.path.insert(0, str(ROOT / "tests" / "parity"))

import policy as P  # noqa: E402
import run as R  # noqa: E402
import wire as W  # noqa: E402

PROJECT = "morand"
PEOPLE = {"sophie@morand.test": "Sophie Chevalley", "claire@morand.test": "Claire Rochat",
          "marc@morand.test": "Marc Morand"}
PROFILE = {
    "name": "Morand Domotique", "legal_name": "Morand Domotique SA", "legal_form": "SA",
    "sector": "Intégration domotique et électricité du bâtiment (48 personnes)",
    "street": "Chemin des Ateliers 12", "postcode": "1023", "city": "Crissier", "country": "CH",
    "uid": "CHE-116.281.710", "vat": "CHE-116.281.710 TVA",
    "iban": "CH93 0076 2011 6238 5295 7", "bank": "Banque Exemple, Lausanne",
    "phone": "+41 21 000 00 00", "email": "info@morand-domotique.test",
    "signatories": [{"name": "Marc Morand", "function": "Directeur général"},
                    {"name": "Claire Rochat", "function": "Responsable financière"}],
    "people": [
        {"initials": "MM", "name": "Marc Morand", "function": "Directeur général", "email": "marc@morand.test"},
        {"initials": "SC", "name": "Sophie Chevalley", "function": "Assistante de direction",
         "email": "sophie@morand.test"},
        {"initials": "TB", "name": "Thomas Bovet", "function": "Responsable commercial"},
        {"initials": "CR", "name": "Claire Rochat", "function": "Responsable financière",
         "email": "claire@morand.test"},
        {"initials": "DF", "name": "David Favre", "function": "Chef de projet"},
        {"initials": "KD", "name": "Kevin Dupuis", "function": "Technicien programmation"},
    ],
    "tone": "Sobre, précis et cordial ; jamais familier.",
}

PV_1011 = """# Procès-verbal du comité de direction du 10.11.2026

Présents : Marc Morand (président), Sophie Chevalley (procès-verbal), Thomas Bovet, Claire Rochat, David Favre.
Lieu : Hôtel Bellerive, Lausanne.

## Décisions
1. Le budget 2027 est préparé en deux temps : **budget v1 présenté au comité du 14.12.2026**, version finale en janvier.
2. Le recrutement d'un second technicien de programmation est validé (départ annoncé de Kevin Dupuis au 31.03.2027).
3. Le chantier Bellerive (stores) reste prioritaire jusqu'à la réception du 04.12.2026.

## Actions
| Qui | Action | Échéance |
|---|---|---|
| Thomas Bovet | Remonter les besoins commerciaux 2027 à Claire Rochat | 20.11.2026 |
| Claire Rochat | Préparer le budget v1 | 14.12.2026 |
| David Favre | Plan de reprise de la programmation (départ de Kevin Dupuis) | 14.12.2026 |
| Sophie Chevalley | Réserver la salle du comité du 14.12.2026 | 20.11.2026 |

## Prochain comité
Lundi 14.12.2026, 08:30, salle du conseil, Crissier.
"""

RAPPROCHEMENT = """# Rapprochement bancaire — octobre 2026

Relevé du compte courant au 31.10.2026, rapproché des factures clients ouvertes.

## Factures soldées
- F-2026-101 — Les Cèdres SA : 8'120.50 CHF, **payée le 12.10.2026** (virement reçu, montant exact).
- F-2026-104 — Chappuis & Fils : 4'860.00 CHF, **payée le 20.10.2026**.

## Paiements partiels
- F-2026-107 — Régie du Lac : 18'350.00 CHF facturés, 6'000.00 reçus le 25.10, **12'350.00 CHF restants**.

## Toujours ouvertes
- F-2026-109 — Résidence Bellerive : 31'200.00 CHF (acompte), échéance 30.10.2026 dépassée.
- F-2026-112 — Riviera Immobilier : 2'940.00 CHF, échéance 31.10.2026.

## À identifier
- Virement de 1'250.00 CHF du 28.10 sans référence.
"""

FACTURES = """# Factures clients ouvertes (export de la comptabilité du 02.10.2026)

| Facture | Client | Montant CHF | Échéance |
|---|---|---|---|
| F-2026-101 | Les Cèdres SA | 8'120.50 | 30.09.2026 |
| F-2026-104 | Chappuis & Fils | 4'860.00 | 30.09.2026 |
| F-2026-107 | Régie du Lac | 18'350.00 | 15.10.2026 |
| F-2026-109 | Résidence Bellerive | 31'200.00 | 30.10.2026 |
| F-2026-112 | Riviera Immobilier | 2'940.00 | 31.10.2026 |
"""

NOTES_COMITE = """Notes comité direction 17.11.2026 (SC)

présents : MM, TB, CR, DF, SC
- CR présente budget v1 : CA 2027 visé 9,4 M, marge brute 31 %. MM ok sous réserve achats.
- T. Bovet : besoins co 2027 = 1 commercial junior au T2 + salon Habitat Lausanne. accord MM pour salon, junior à revoir en mars.
- DF : plan reprise programmation après départ KD : formation interne + prestataire Domelec (9'800.-) en attendant recrutement. validé.
- divers : prochain comité 14.12, Hôtel Bellerive (dîner annuel ensuite).
actions : CR budget final 14.12 / TB offre salon avant 15.12 / DF contrat Domelec cette semaine / SC réserver Bellerive
"""

CASES = {
    "relances": {"who": "claire@morand.test",
                 "ask": "Prépare les lettres de relance pour nos factures clients ouvertes de la liste jointe "
                        "(une lettre par client, ton adapté au retard).",
                 "docs": [("Factures ouvertes au 02.10.md", FACTURES)]},
    "odj": {"who": "sophie@morand.test",
            "ask": "Prépare l'ordre du jour du comité de direction du 14.12.2026.",
            "docs": []},
    "pv": {"who": "sophie@morand.test",
           "ask": "Rédige le procès-verbal du comité de direction de ce matin à partir de mes notes.",
           "docs": [("Notes comité 17.11.md", NOTES_COMITE)]},
}


# ---- the product's stores, laid out once ------------------------------------------------------
def setup() -> dict:
    import board
    import iam
    import orgprofile
    import projects
    import simple
    iam.init(force=True)
    for e, n in PEOPLE.items():
        iam.upsert_user(e, "dev", n)
    board.init(force=True)
    if not projects.get(PROJECT):
        projects.create(PROJECT, "Direction", created_by="marc@morand.test")
    for e in PEOPLE:
        projects.grant(PROJECT, "user", e, "dev")
    orgprofile.update(PROFILE, "marc@morand.test")
    seeded = {}
    for key, (who, title, text, days) in {
            "pv": ("sophie@morand.test", "PV du comité du 10.11", PV_1011, 7),
            "rapprochement": ("claire@morand.test", "Rapprochement bancaire octobre", RAPPROCHEMENT, 14)}.items():
        sid = uuid.uuid4().hex
        board.add_sdk_session(sid, "docs", title=title, project=PROJECT, owner=who)
        simple.add_work(sid, PROJECT, who, title, "", "fr")
        d = simple.deliverable_dir(PROJECT, sid)
        d.mkdir(parents=True, exist_ok=True)
        name = f"{title}.md"
        (d / name).write_text(text, encoding="utf-8")
        simple.validate(PROJECT, sid, name, who)
        con = simple._con()      # validated on its real day (the order of the list)
        con.execute("UPDATE validations SET at=? WHERE sid=?", (time.time() - days * 86400, sid))
        con.commit()
        con.close()
        seeded[key] = sid
    return seeded


def docs_for(case: dict) -> list[dict]:
    import doctext
    import documents
    import simple
    out = []
    for name, src in case["docs"]:
        doc_id = uuid.uuid4().hex[:16]
        folder = documents.docs_dir(PROJECT) / doc_id
        folder.mkdir(parents=True)
        dname = name.rsplit(".", 1)[0] + ".docx"
        (folder / dname).write_bytes(simple.markdown_to_docx(src))
        text, state = doctext.extract(folder / dname, "docx")
        if text:
            (folder / (dname + ".txt")).write_text(text, encoding="utf-8")
        out.append({"id": doc_id, "name": dname, "ext": "docx", "project": PROJECT, "text_state": state})
    return out


# ---- grading ---------------------------------------------------------------------------------
PAID = ("Cèdres", "Chappuis")
# what says « this invoice is paid, do not remind » — not the words of a reminder itself
# (« n'a pas encore été réglée », « si le paiement a déjà été effectué »)
FLAG = re.compile(r"(?:pay[ée]e?s?|sold[ée]e?s?|r[ée]gl[ée]e?s?|encaiss[ée]e?s?)\s+(?:le|au|en|depuis|selon|"
                  r"d'apr[èe]s|int[ée]gralement)\b|d[ée]j[àa]\s+(?:pay|sold|r[ée]gl|encaiss)|rapprochement|"
                  r"ne pas (?:l')?envoyer|à ne pas envoyer|retir[ée]|exclu|paid|settled", re.I)


def sections(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(#{1,3} .*)$", text)
    out, head = [], ""
    for p in parts:
        if re.match(r"^#{1,3} ", p or ""):
            head = p
        elif p and p.strip():
            out.append((head, p))
    return out


def grade(case_id: str, text: str, chat: str) -> dict:
    t = text or ""
    both = t + "\n" + (chat or "")
    org = {"org_name": "Morand Domotique SA" in t, "org_city": "Crissier" in t,
           # a gap about the ORGANISATION (its name, address, contact, signatory, bank) — a
           # client's address the documents do not hold is an honest gap, not this one
           "no_placeholder": not re.search(r"(?i)\[\s*(vos|votre|notre|nos)\b[^\]]*\]|\[[^\]]*(raison sociale|"
                                           r"nom de (l'|la )?(entreprise|soci[ée]t[ée])|signataire|iban|"
                                           r"coordonn[ée]es bancaires)[^\]]*\]|\{[a-z_ ]+\}", t)}
    if case_id == "relances":
        letters_to_paid, flagged = [], []
        for name in PAID:
            for head, body in sections(t):
                if name.lower() in (head + body[:300]).lower() and re.search(
                        r"(?i)madame|monsieur|relance|rappel|nous nous permettons", head + body):
                    if not FLAG.search(head + body[:600]):
                        letters_to_paid.append(name)
            if re.search(rf"(?is){name}.{{0,240}}{FLAG.pattern}|{FLAG.pattern}.{{0,240}}{name}", both):
                flagged.append(name)
        checks = {**org, "no_letter_to_paid_invoice": not letters_to_paid,
                  "paid_invoices_flagged": len(set(flagged)) == len(PAID),
                  "iban_in_letters": "CH93 0076 2011 6238 5295 7" in t,
                  "partial_amount_right": "12'350" in t or "12 350" in t or "12350" in t}
        return {"checks": checks, "letters_to_paid": letters_to_paid, "flagged": sorted(set(flagged))}
    # an internal agenda or minutes need not carry the company's name: said, not graded
    info = {k: v for k, v in org.items() if k != "no_placeholder"}
    if case_id == "odj":
        checks = {"no_placeholder": org["no_placeholder"],
                  "uses_pv_1011": bool(re.search(r"(?i)budget\s*(v1|2027)", t)),
                  "pv_actions": sum(bool(re.search(p, t, re.I)) for p in
                                    (r"besoins", r"reprise|programmation|Kevin", r"recrutement|technicien")) >= 2,
                  # the audit's failure: « I find no document about this committee » (a detail
                  # « no document says where the hiring stands » is honest, not a failure)
                  "no_no_document": not re.search(r"(?i)(je ne trouve|je n'ai trouv[ée]|il n'y a) (aucun|pas de) "
                                                  r"document|aucun document (sur|concernant|à propos de) (ce|le) comit", both)}
        return {"checks": checks, "info": info}
    if case_id == "pv":
        checks = {"no_placeholder": org["no_placeholder"], "thomas_bovet": "Thomas Bovet" in t,
                  "no_invented_first_name": not re.search(r"\b(Théo|Thierry|Tom|Timothée|Tristan) Bovet", t),
                  "initials_resolved": all(n in t for n in ("Claire Rochat", "David Favre", "Marc Morand")),
                  "hotel_bellerive": "Hôtel Bellerive" in t or "Hotel Bellerive" in t,
                  "swiss_amount_format": not re.search(r"\b\d{1,3} \d{3}(?:[.,]-|\b)", t)}
        return {"checks": checks, "info": info}
    return {"checks": {}}


# ---- one play --------------------------------------------------------------------------------
async def play(case_id: str, mode: str, timeout_s: float) -> dict:
    import board
    import househead
    import simple
    case = CASES[case_id]
    who = case["who"]
    sid = uuid.uuid4().hex
    board.add_sdk_session(sid, "docs", title=case["ask"][:60], project=PROJECT, owner=who)
    simple.add_work(sid, PROJECT, who, case["ask"], "", "fr")
    docs = docs_for(case)
    prompt = simple.brief(ask=case["ask"], lang="fr", project=PROJECT, sid=sid, docs=docs)
    if mode == "after":
        import orgprofile
        prompt = orgprofile.person_line(who, PEOPLE[who]) + "\n" + prompt
    head = tuple(househead.blocks(sid, email=who, project=PROJECT)) if mode == "after" else ()
    import documents
    work = documents.project_root(PROJECT)
    deliv = simple.deliverable_dir(PROJECT, sid)
    pol = P.Policy("atelier", deliverables=deliv, memory=DATA / "projects" / PROJECT / "memory",
                   has_documents=bool(docs))
    spec = R.spec_for("legacy", run_id=sid, cwd=work, profile="atelier")
    spec.project, spec.user, spec.head = PROJECT, who, head
    pl = R.Play("legacy", R.new_harness("legacy"), spec, pol, timeout_s)
    t0 = time.monotonic()
    if not await pl.start():
        return {"started": False, "start_error": pl.start_error}
    await pl.turn(prompt)
    await pl.stop()
    files = R.deliverable_files(deliv)
    text = "\n".join(v for k, v in files.items() if k.endswith(".md"))
    chat = "\n".join(t.get("text", "") for t in pl.turns)
    g = grade(case_id, text, chat)
    g["ok"] = all(g["checks"].values())
    reads = [str((d.get("input") or {}).get("file_path") or (d.get("input") or {}).get("path") or "")
             for d in pl.of("tool.called")]
    return {"started": True, "sid": sid, "wall_s": round(time.monotonic() - t0, 1),
            "first_card_s": pl.turns[0]["first_card_s"], "end_s": pl.turns[0]["end_s"],
            "head_chars": sum(len(b.text) for b in head),
            "head_tokens_est": househead.tokens(househead.text_of(head)),
            "read_validated_work": [r for r in reads if "/deliverables/" in r and sid not in r],
            "cards": pol.summary(), "grade": g, "answer": chat[:2000],
            "deliverables": {k: v[:15000] for k, v in files.items()}}


async def learn_case(timeout_s: float) -> dict:
    """The audit's sop3 in short: a real PV, the person corrects it in the thread, validates it,
    and the offer of « remember for next time » comes from what she wrote."""
    import board
    import househead
    import learn
    import simple
    case = CASES["pv"]
    who = case["who"]
    sid = uuid.uuid4().hex
    board.add_sdk_session(sid, "docs", title="PV comité 17.11", project=PROJECT, owner=who)
    simple.add_work(sid, PROJECT, who, case["ask"], "", "fr")
    docs = docs_for(case)
    import documents
    work = documents.project_root(PROJECT)
    deliv = simple.deliverable_dir(PROJECT, sid)
    pol = P.Policy("atelier", deliverables=deliv, memory=DATA / "projects" / PROJECT / "memory",
                   has_documents=True)
    spec = R.spec_for("legacy", run_id=sid, cwd=work, profile="atelier")
    spec.project, spec.user = PROJECT, who
    spec.head = tuple(househead.blocks(sid, email=who, project=PROJECT))
    pl = R.Play("legacy", R.new_harness("legacy"), spec, pol, timeout_s)
    if not await pl.start():
        return {"started": False, "start_error": pl.start_error}
    import orgprofile
    await pl.turn(orgprofile.person_line(who, PEOPLE[who]) + "\n"
                  + simple.brief(ask=case["ask"], lang="fr", project=PROJECT, sid=sid, docs=docs))
    correction = ("Dans le procès-verbal, écris toujours les décisions en gras et termine par le tableau "
                  "des actions (qui, quoi, quand), pas par une liste.")
    learn.note_message(sid, who, correction)
    await pl.turn(simple.follow_up(correction, [], "fr"))
    await pl.stop()
    files = R.deliverable_files(deliv)
    md = [k for k in files if k.endswith(".md")]
    if not md:
        return {"started": True, "error": "no deliverable"}
    simple.validate(PROJECT, sid, md[0], who)
    t0 = time.monotonic()
    offer = learn.offer(PROJECT, sid, md[0], "fr")
    offer_ms = round((time.monotonic() - t0) * 1000)
    kept: dict = {}
    if offer["facts"]:
        try:
            kept = learn.retain(PROJECT, sid, md[0], offer["facts"], who, clearance=None)
        except learn.LearnError as e:
            # a project other than `default` keeps its notes in the 3.0 store only (Postgres):
            # the product refuses before writing anything — the same refusal as memory_write
            kept = {"refused": e.code, "status": e.status, "message": str(e)[:200]}
    notes = {n: (DATA / "projects" / PROJECT / "memory" / f"{n}.md").read_text(encoding="utf-8")
             for n in kept.get("notes", [])}
    return {"started": True, "offer": offer, "offer_ms": offer_ms, "kept": kept, "notes": notes,
            "clicks": 1, "turns": [t["end_s"] for t in pl.turns]}


def table(results: list[dict]) -> str:
    rows = ["| Cas | Mode | Verdict | Contrôles en échec | 1re carte (s) | Fin (s) | Requêtes | Jetons prompt (dont cache) | Tête (jetons est.) | Travaux validés lus |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        g = r.get("grade") or {}
        w = r.get("wire") or {}
        bad = [k for k, v in (g.get("checks") or {}).items() if not v]
        rows.append(f"| {r['case']} | {r['mode']} | {'OK' if g.get('ok') else 'KO'} | {', '.join(bad) or '—'} | "
                    f"{r.get('first_card_s')} | {r.get('end_s')} | {w.get('requests')} | "
                    f"{w.get('prompt_total')} ({w.get('prompt_cached')}) | {r.get('head_tokens_est', 0)} | "
                    f"{len(r.get('read_validated_work') or [])} |")
    return "\n".join(rows) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="relances,odj,pv", help="'' = none (with --learn)")
    ap.add_argument("--modes", default="before,after")
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--regrade", default="", help="a runs.json to grade again (no model call)")
    a = ap.parse_args()
    if a.regrade:
        path = Path(a.regrade)
        data = json.loads(path.read_text(encoding="utf-8"))
        for r in data["results"]:
            if not r.get("started"):
                continue
            text = "\n".join(v for k, v in (r.get("deliverables") or {}).items() if k.endswith(".md"))
            g = grade(r["case"], text, r.get("answer") or "")
            g["ok"] = all(g["checks"].values())
            r["grade"] = g
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        md = f"# Vrais runs « le coworker connaît la maison » — moteur hérité, {data['model']}\n\n" + table(data["results"])
        (path.parent / "runs.md").write_text(md, encoding="utf-8")
        print(md)
        return 0
    upstream = R.env("PARITY_UPSTREAM")
    if not upstream:
        print("PARITY_UPSTREAM is not set", file=sys.stderr)
        return 2
    out_dir = Path(a.out) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    seeded = setup()
    model = R.pick_model(upstream)
    results: list[dict] = []
    meter = W.Wire(upstream, str(out_dir / "wire.jsonl"), bind="127.0.0.1",
                   port=int(R.env("PARITY_WIRE_PORT", "18797")), thinking=False,
                   max_concurrent=int(R.env("PARITY_MAX_CONCURRENT", "1")), model=model)
    with meter as wire:
        R.legacy_env(out_dir / "claude-cfg", wire.url)
        for case_id in [c for c in a.only.split(",") if c]:
            for mode in [m for m in a.modes.split(",") if m]:
                tag = f"{case_id}/{mode}"
                wire.tag = tag
                print(f"== {tag}", flush=True)
                try:
                    r = asyncio.run(play(case_id, mode, a.timeout))
                except Exception as e:  # noqa: BLE001 — one broken case never stops the others
                    r = {"started": False, "start_error": f"{type(e).__name__}: {e}"[:300]}
                wire.tag = ""
                r["wire"] = W.totals(wire.of(tag))
                wire.forget(tag)
                r.update(case=case_id, mode=mode)
                results.append(r)
                g = r.get("grade") or {}
                print(f"   {'OK' if g.get('ok') else 'KO'} {g.get('checks')} {r.get('start_error') or ''}"
                      f" wire={r['wire']}", flush=True)
                (out_dir / "runs.json").write_text(json.dumps(
                    {"model": model, "engine": "legacy", "seeded": seeded, "results": results},
                    ensure_ascii=False, indent=1), encoding="utf-8")
        learned = None
        if a.learn:
            wire.tag = "learn"
            print("== learn", flush=True)
            try:
                learned = asyncio.run(learn_case(a.timeout))
            except Exception as e:  # noqa: BLE001
                learned = {"started": False, "error": f"{type(e).__name__}: {e}"[:300]}
            wire.tag = ""
            learned["wire"] = W.totals(wire.of("learn"))
            print(json.dumps(learned, ensure_ascii=False)[:2000], flush=True)
            (out_dir / "learn.json").write_text(json.dumps(learned, ensure_ascii=False, indent=1), encoding="utf-8")
    md = f"# Vrais runs « le coworker connaît la maison » — moteur hérité, {model}\n\n" + table(results)
    (out_dir / "runs.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"results: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
