"""Real runs of the 4.0 native formats — the audit's cases (« Claire, finance, asks for an
Excel », « one reminder per client », « an offer whose totals hold ») played through the
harness contract on a real engine and a real model, the way SOKKAN Atelier starts a work
(`simple.brief()`, documents laid out like the product's, the cockpit's Atelier rules from
tests/parity/policy.py). Machinery shared with the parity bench (tests/parity/run.py, wire.py).

    PARITY_UPSTREAM=http://<model>/v1 python tests/formats/real_runs.py --src <tree> --out <dir>

``--src`` is the source tree whose brief and harness are played (the branch, or the base for
the « before » run); grading always uses THIS tree's `outputs` (the files a person would get).
A case is OK when the work ended by itself with ONE deliverable of the expected kind, the
native file opens, the totals SOKKAN computes are the expected ones, and no figure quoted in
the agent's message contradicts them.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OWN = HERE.parents[1]

CLIENTS = """# Factures émises — octobre 2026

| Facture | Client | Date | Montant (CHF) |
|---|---|---|---|
| F-2026-101 | Régie du Lac SA | 02.10.2026 | 18'350.00 |
| F-2026-102 | Les Cèdres SA | 05.10.2026 | 8'120.50 |
| F-2026-103 | Hôtel Bellerive | 09.10.2026 | 31'200.00 |
| F-2026-104 | Riviera Immobilier | 14.10.2026 | 4'200.00 |
| F-2026-105 | Chappuis Sàrl | 20.10.2026 | 2'760.00 |

Conditions : 30 jours net.
"""
BANK = ("Date;Texte;Crédit CHF\n"
        "05.11.2026;VIREMENT REGIE DU LAC SA REF F-2026-101 ACOMPTE;6000.00\n"
        "07.11.2026;LES CEDRES SA FACTURE 102;8120.50\n"
        "10.11.2026;HOTEL BELLERIVE ACOMPTE F-2026-103;10000.00\n"
        "12.11.2026;CHAPPUIS SARL;2760.00\n"
        "14.11.2026;VIREMENT DIVERS REF 88213;1250.00\n")
OPEN = """# Factures ouvertes au 17.11.2026 (rapprochement validé)

| Facture | Client | Adresse | Reste dû (CHF) | Échéance |
|---|---|---|---|---|
| F-2026-101 | Régie du Lac SA | Quai du Lac 4, 1820 Montreux | 12'350.00 | 01.11.2026 |
| F-2026-103 | Hôtel Bellerive | Av. de la Gare 12, 1003 Lausanne | 21'200.00 | 08.11.2026 |
| F-2026-104 | Riviera Immobilier | Rue du Centre 8, 1800 Vevey | 4'200.00 | 13.11.2026 |

Ton : courtois, ferme pour la Régie du Lac (2e rappel). Délai de paiement : 10 jours.
"""
TARIF = """# Tarif 2026 — domotique

| Article | Unité | Prix (CHF HT) |
|---|---|---|
| Capteur de présence KNX | pièce | 120.50 |
| Module de commande stores | pièce | 380.00 |
| Pose et câblage (forfait chantier) | forfait | 3'200.00 |
| Programmation | heure | 135.00 |

TVA 8.1 %.
"""
BRIEF = """# Demande — Hôtel Bellerive, rénovation de l'aile est

Besoin : 40 capteurs de présence, 12 modules de commande de stores, la pose (un forfait) et
24 heures de programmation. Le client veut le total HT, la TVA et le total TTC.
"""

CASES = [
    {"id": "finance.reconciliation-excel",
     "ask": "Fais le rapprochement des factures d'octobre avec l'export bancaire de novembre et "
            "donne-moi le résultat dans un fichier Excel, avec le total encore ouvert.",
     "documents": [("Factures octobre.md", CLIENTS), ("Export banque novembre.csv", BANK)],
     "kind": "spreadsheet", "expect_totals": [37750.00], "expect_any": [64630.50, 26880.50]},
    {"id": "finance.reminders-per-client",
     "ask": "Prépare une lettre de relance par client pour les factures encore ouvertes, une lettre "
            "par destinataire, que je puisse les imprimer.",
     "documents": [("Factures ouvertes.md", OPEN)],
     "kind": "letters", "letters": {"F-2026-101": "12'350", "F-2026-103": "21'200", "F-2026-104": "4'200"}},
    {"id": "sales.offer-totals-word",
     "ask": "Rédige l'offre pour l'Hôtel Bellerive en Word, avec le tableau des prix, le total HT, "
            "la TVA et le total TTC.",
     "documents": [("Tarif 2026.md", TARIF), ("Demande Bellerive.md", BRIEF)],
     "kind": "document", "expect_totals": [15820.00], "expect_any": [17101.42, 1281.42]},
]


def _own_outputs():
    spec = importlib.util.spec_from_file_location("fmt_outputs", OWN / "backend" / "outputs.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["fmt_outputs"] = m          # dataclasses look their module up
    spec.loader.exec_module(m)
    return m


def space_for(base: Path, case: dict) -> dict:
    os.environ["SOKKAN_DATA_DIR"] = str(base)
    import doctext
    import documents
    import simple
    project = "formats"
    work = documents.project_root(project)
    work.mkdir(parents=True, exist_ok=True)
    memory = base / "projects" / project / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    docs, sources = [], []
    for name, src in case["documents"]:
        sources.append(src)
        doc_id = uuid.uuid4().hex[:16]
        folder = documents.docs_dir(project) / doc_id
        folder.mkdir(parents=True)
        ext = name.rsplit(".", 1)[-1].lower()
        (folder / name).write_text(src, encoding="utf-8")
        state = "ok"
        if ext not in ("md", "txt"):
            text, state = doctext.extract(folder / name, ext)
            if text:
                (folder / (name + ".txt")).write_text(text, encoding="utf-8")
        docs.append({"id": doc_id, "name": name, "ext": ext, "project": project, "text_state": state,
                     "created_at": time.time(), "modified_at": time.time()})
    sid = uuid.uuid4().hex
    return {"work": work, "memory": memory, "docs": docs, "sources": sources, "sid": sid,
            "deliverables": simple.deliverable_dir(project, sid), "project": project}


def _numbers(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"(?<![\w.'’])(\d{1,3}(?:['’  ]\d{3})+(?:[.,]\d{2})?|\d+[.,]\d{2})(?![\d])", text):
        s = re.sub(r"['’  ]", "", m.group(1)).replace(",", ".")
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def grade(case: dict, play, sp: dict, files: dict[str, str], O) -> dict:
    chat = "\n".join(t.get("text", "") for t in play.turns)
    mds = {k: v for k, v in files.items() if k.lower().endswith((".md", ".csv", ".markdown", ".txt"))}
    kinds = {k: O.kind_of(k, v) for k, v in mds.items()}
    good = [k for k, kd in kinds.items() if kd == case["kind"]]
    checks = {"finished": not play.timed_out and not any(t["is_error"] for t in play.turns),
              "one_deliverable_of_the_kind": len(good) == 1,
              "no_markdown_only_excuse": not re.search(r"(uniquement|seulement|qu'un).{0,40}markdown", chat, re.I)}
    detail: dict = {"kinds": kinds}
    computed: list[float] = []
    if good:
        name, text = good[0], mds[good[0]]
        try:
            if case["kind"] == "spreadsheet":
                data = O.to_xlsx(name, text)
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(data))
                wv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
                formulas = sum(1 for ws in wb for row in ws.iter_rows() for c in row
                               if isinstance(c.value, str) and c.value.startswith("="))
                computed = [float(c.value) for ws in wv for row in ws.iter_rows() for c in row
                            if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)]
                detail.update(sheets=wb.sheetnames, formulas=formulas)
                checks["native_file_opens"] = True
                checks["totals_are_formulas"] = formulas >= 2
            elif case["kind"] == "letters":
                z = zipfile.ZipFile(io.BytesIO(O.render("letters_docx", name, text)[0]))
                import docx
                per = {}
                for n in z.namelist():
                    if n.startswith("Toutes"):
                        continue
                    d = docx.Document(io.BytesIO(z.read(n)))
                    per[n] = "\n".join(p.text for p in d.paragraphs) + "\n".join(
                        c.text for t in d.tables for r in t.rows for c in r.cells)
                detail["letters"] = list(per)
                checks["native_file_opens"] = True
                checks["one_letter_per_open_invoice"] = len(per) == len(case["letters"])
                checks["each_letter_has_its_invoice_and_amount"] = all(
                    any(inv in t and amt in t for t in per.values()) for inv, amt in case["letters"].items())
                checks["no_letter_mixes_invoices"] = all(
                    sum(1 for inv in case["letters"] if inv in t) <= 1 for t in per.values())
            else:
                import docx
                d = docx.Document(io.BytesIO(O.render("docx", name, text)[0]))
                cells = [c.text for t in d.tables for r in t.rows for c in r.cells]
                body = "\n".join(p.text for p in d.paragraphs) + "\n" + "\n".join(cells)
                computed = _numbers(body)
                detail["formula_cells"] = len(re.findall(r"\|\s*(\*\*)?=", text))
                checks["native_file_opens"] = True
        except Exception as e:  # noqa: BLE001
            checks["native_file_opens"] = False
            detail["render_error"] = f"{type(e).__name__}: {e}"[:300]
        if case.get("expect_totals"):
            hits = [t for t in case["expect_totals"] if any(abs(t - v) < 0.006 for v in computed)]
            checks["expected_totals_in_file"] = len(hits) == len(case["expect_totals"])
            detail["expected_any_found"] = [t for t in case.get("expect_any", [])
                                            if any(abs(t - v) < 0.006 for v in computed)]
        if case["kind"] == "document":
            text_nums = _numbers(O.preview(name, text))
            want = case["expect_totals"] + case.get("expect_any", [])
            # a total-looking figure close to an expected one but not equal: a wrong total
            wrong = sorted({v for v in text_nums if v > 1000 and not any(abs(v - t) < 0.006 for t in want)
                            and any(abs(v - t) < 0.2 * t for t in want)})
            detail["near_miss_figures_in_file"] = wrong[:10]
    else:
        detail["note"] = "no deliverable of the expected kind"
    # figures of the message close to an expected total but different: told wrong
    want = case.get("expect_totals", []) + case.get("expect_any", [])
    said = _numbers(chat)
    bad_said = sorted({v for v in said if v > 1000 and not any(abs(v - t) < 0.006 for t in want)
                       and any(abs(v - t) < 0.2 * t for t in want)})
    checks["message_quotes_no_wrong_total"] = not bad_said
    detail["message_wrong_totals"] = bad_said[:10]
    return {"ok": all(checks.values()), "checks": checks, "detail": detail}


async def play_case(case: dict, base: Path, timeout_s: float, R, P, O) -> dict:
    import simple
    sp = space_for(base, case)
    prompt = simple.brief(ask=case["ask"], lang="fr", project=sp["project"], sid=sp["sid"], docs=sp["docs"])
    pol = P.Policy("atelier", deliverables=sp["deliverables"], memory=sp["memory"],
                   has_documents=bool(sp["docs"]))
    play = R.Play("legacy", R.new_harness("legacy"),
                  R.spec_for("legacy", run_id="run-" + uuid.uuid4().hex[:8], cwd=sp["work"], profile="atelier"),
                  pol, timeout_s)
    t0 = time.monotonic()
    if not await play.start():
        return {"started": False, "start_error": play.start_error}
    await play.turn(prompt)
    await play.stop()
    files = R.deliverable_files(sp["deliverables"])
    calc = sp["deliverables"] / ".calc"
    out = {"started": True, "wall_s": round(time.monotonic() - t0, 1),
           "first_draft_s": play.turns[0]["first_deliverable_s"], "turn_end_s": play.turns[0]["end_s"],
           "cards": pol.summary(), "answer": play.turns[0]["text"][:2500],
           "calc_files": sorted(p.name for p in calc.iterdir()) if calc.is_dir() else []}
    out["grade"] = grade(case, play, sp, files, O)
    out["deliverable_texts"] = {k: v[:15000] for k, v in files.items()}
    return out


def table(results: list[dict]) -> str:
    lines = ["| Cas | Verdict | 1er brouillon (s) | Fin (s) | Contrôles en échec | Détail |", "|---|---|---|---|---|---|"]
    for r in results:
        g = r.get("grade") or {}
        bad = [k for k, v in (g.get("checks") or {}).items() if not v]
        det = g.get("detail") or {}
        short = {k: det[k] for k in ("kinds", "formulas", "letters", "message_wrong_totals",
                                     "near_miss_figures_in_file", "expected_any_found", "render_error") if k in det}
        lines.append(f"| {r['case']} | {'OK' if g.get('ok') else 'KO'} | {r.get('first_draft_s')} | "
                     f"{r.get('turn_end_s')} | {', '.join(bad) or (r.get('start_error') or '—')} | "
                     f"{json.dumps(short, ensure_ascii=False)[:400]} |")
    ok = sum(1 for r in results if (r.get("grade") or {}).get("ok"))
    return f"**{ok}/{len(results)} OK**\n\n" + "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(OWN))
    ap.add_argument("--only", default="")
    ap.add_argument("--label", default="after")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args()
    src = Path(a.src).resolve()
    sys.path.insert(0, str(src / "tests" / "parity"))
    sys.path.insert(0, str(src / "backend"))
    import policy as P
    import run as R
    import wire as W
    O = _own_outputs()
    upstream = R.env("PARITY_UPSTREAM")
    if not upstream:
        print("PARITY_UPSTREAM is not set", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(a.out) / f"{stamp}-{a.label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    model = R.pick_model(upstream)
    only = [x.strip() for x in a.only.split(",") if x.strip()]
    results = []
    meter = W.Wire(upstream, str(out_dir / "wire.jsonl"), bind="127.0.0.1",
                   port=int(R.env("PARITY_WIRE_PORT", "18796")), thinking=False,
                   max_concurrent=int(R.env("PARITY_MAX_CONCURRENT", "2")), model=model)
    with meter as wire:
        R.legacy_env(out_dir / "claude-cfg", wire.url)
        for c in CASES:
            if only and c["id"] not in only:
                continue
            tag = f"fmt-{stamp}-{c['id']}"
            wire.tag = tag
            print(f"== {c['id']}", flush=True)
            try:
                r = asyncio.run(play_case(c, out_dir / "ws" / c["id"], a.timeout, R, P, O))
            except Exception as e:  # noqa: BLE001
                r = {"started": False, "start_error": f"{type(e).__name__}: {e}"[:300]}
            wire.tag = ""
            r["wire"] = W.totals(wire.of(tag))
            wire.forget(tag)
            r["case"] = c["id"]
            results.append(r)
            g = r.get("grade") or {}
            print(f"   {'OK' if g.get('ok') else 'KO'} {[k for k, v in (g.get('checks') or {}).items() if not v]} "
                  f"{r.get('start_error') or ''}", flush=True)
            (out_dir / "runs.json").write_text(json.dumps({"model": model, "label": a.label, "src": str(src),
                                                           "results": results}, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    (out_dir / "runs.md").write_text(f"# Vrais runs formats natifs — {a.label}, moteur hérité, {model}\n\n"
                                     + table(results), encoding="utf-8")
    print(table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
