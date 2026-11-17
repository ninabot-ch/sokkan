"""Real runs of the job kits (4.0): each fixture of tests/roles/fixtures/<job>.<template>/ is
played through the harness contract on a real engine and a real model, the way SOKKAN Atelier
starts a work — the product's own `playbooks.render()` (the kit's mission, its deliverable
brief and the job's rules) inside `simple.brief()`, the documents laid out like the product's
(a .docx made from the fixture's text and its text copy), the cockpit's Atelier rules
(tests/parity/policy.py: the person approves the save, no shell) — then the deliverable is
graded. Machinery shared with the parity bench (tests/parity/run.py, wire.py).

    PARITY_UPSTREAM=http://127.0.0.1:8004/v1 python tests/roles/real_runs.py --out <dir>
    python tests/roles/real_runs.py --only hr.work-certificate,finance.budget-variance

A case is OK when: the work ended by itself, ONE deliverable was saved in the expected format
(a « # Title » Markdown for docx/md, a CSV of at least `min_rows` rows for xlsx), every fact of
`must_contain` is in it, a docx/md deliverable ends with its « Points à vérifier » section,
it is written in the person's language, and no failure line showed in the thread. Figures of
the deliverable that no document holds are listed for a human reader (a computed total can be
right): they are not a verdict.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests" / "parity"))

import policy as P  # noqa: E402
import run as R  # noqa: E402  (the parity bench's runner: Play, spec_for, grading helpers)
import wire as W  # noqa: E402

FIXTURES = HERE / "fixtures"

# the cockpit's own words when a document is saved a second time in one turn (agentchat.py,
# 3.6.7): the Atelier's thread says it quietly (plain.py: a rule of the space, not a failure).
# The parity bench words it its own way, which the thread would show as a failure line.
PRODUCT_ONE_CARD = ("The person already approved this document in this turn: it is saved. Do "
                    "not save it again and do not offer another version. Tell them in two or "
                    "three sentences what it contains and what remains to confirm (if a part is "
                    "still missing, say which one and ask whether to add it); they will ask for "
                    "a change if they want one. Do not mention this message.")
P.ONE_CARD = PRODUCT_ONE_CARD
BENCH_ONE_CARD = "This document was already saved in this step"


def regrade(path: Path) -> None:
    """Results of a run made before the cockpit's wording was used: a failure line that is
    only the bench's « already saved » refusal is the product's quiet refusal."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for r in data["results"]:
        g, v = r.get("grade") or {}, r.get("view_lines") or {}
        if not g or g["checks"].get("no_failure_line", True):
            continue
        real = [x for x in v.get("fail_samples", []) if BENCH_ONE_CARD not in x]
        if not real and v.get("fail", 0) == len(v.get("fail_samples", [])):
            g["checks"]["no_failure_line"] = True
            g["second_save_refused_quietly"] = v["fail"]
            g["ok"] = all(g["checks"].values())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (path.parent / "roles-runs.md").write_text(
        f"# Vrais runs des kits métiers — moteur hérité, {data['model']}\n\n" + table(data["results"]),
        encoding="utf-8")
    print(table(data["results"]))


def cases(only: list[str] | None) -> list[dict]:
    out = []
    for d in sorted(FIXTURES.iterdir()):
        if not (d / "case.json").is_file():
            continue
        c = json.loads((d / "case.json").read_text(encoding="utf-8"))
        c["dir"] = d
        c["id"] = d.name
        if only and d.name not in only:
            continue
        out.append(c)
    return out


def space_for(base: Path, case: dict) -> dict:
    """<base>/projects/roles/work/{documents/<hex16>/<name>, deliverables/<hex32>/} — the
    product's layout (documents.py, simple.py), as the parity bench builds it."""
    os.environ["SOKKAN_DATA_DIR"] = str(base)
    import doctext
    import documents
    import simple
    project = "roles"
    work = documents.project_root(project)
    work.mkdir(parents=True, exist_ok=True)
    memory = base / "projects" / project / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    docs, sources = [], []
    for d in case["documents"]:
        src = (case["dir"] / d["source"]).read_text(encoding="utf-8")
        sources.append(src)
        doc_id = uuid.uuid4().hex[:16]
        folder = documents.docs_dir(project) / doc_id
        folder.mkdir(parents=True)
        name = d["name"]
        if d.get("as_docx"):
            if not name.lower().endswith(".docx"):
                name = name.rsplit(".", 1)[0] + ".docx"
            (folder / name).write_bytes(simple.markdown_to_docx(src))
            text, state = doctext.extract(folder / name, "docx")
            if text:
                (folder / (name + ".txt")).write_text(text, encoding="utf-8")
        else:
            (folder / name).write_text(src, encoding="utf-8")
            state = "ok"
        docs.append({"id": doc_id, "name": name, "ext": name.rsplit(".", 1)[-1].lower(),
                     "project": project, "text_state": state})
    sid = uuid.uuid4().hex
    return {"work": work, "memory": memory, "docs": docs, "sources": sources, "sid": sid,
            "deliverables": simple.deliverable_dir(project, sid), "project": project}


def prompt_for(case: dict, sp: dict) -> str:
    import playbooks
    import simple
    mission = (playbooks.render(case["template"], case["ask"], project=sp["project"]) or ("", ""))[0]
    assert mission, f"unknown template {case['template']}"
    return simple.brief(ask=case["ask"], lang=case.get("lang", "fr"), project=sp["project"],
                        sid=sp["sid"], mission=mission, docs=sp["docs"])


def grade(case: dict, play, sp: dict, files: dict[str, str]) -> dict:
    import roles
    import simple
    _job, tpl = roles.template(case["template"])
    fmt = tpl["format"]
    want_ext = ".csv" if fmt == "xlsx" else ".md"
    good = {k: v for k, v in files.items() if k.lower().endswith(want_ext)}
    text = "\n".join(good.values())
    ftext = R.fold(text)
    facts = {" | ".join(g): any(R.fold(x) in ftext for x in g) for g in case.get("must_contain", [])}
    chat = "\n".join(t.get("text", "") for t in play.turns)
    lang = R.language(chat + "\n" + text)
    checks = {
        "finished": not play.timed_out and not any(t["is_error"] for t in play.turns),
        "one_deliverable_in_format": len(good) == 1,
        "facts_present": all(facts.values()) if facts else True,
        "in_the_persons_language": lang["french"] if case.get("lang", "fr") == "fr" else True,
        "no_failure_line": play.view_lines()["fail"] == 0,
    }
    rows = None
    if fmt == "xlsx":
        rows = len(simple.csv_rows(text)) - 1 if text else 0
        checks["enough_rows"] = rows >= int(case.get("min_rows", 1))
        checks["is_a_table"] = bool(text) and not text.lstrip().startswith("#") and rows > 0 \
            and len(simple.csv_rows(text)[0]) >= 3
    else:
        checks["titled"] = bool(text) and text.lstrip().startswith("# ")
        checks["points_to_check"] = bool(re.search(r"^#+ .*(points? (a|à) v[ée]rifier|points? (a|à) confirmer|"
                                                    r"points to (check|confirm))", text, re.I | re.M))
    words = len(re.findall(r"\w+", text))
    cited = [d["name"] for d in sp["docs"] if R.fold(d["name"].rsplit(".", 1)[0]) in R.fold(text + chat)]
    return {"ok": all(checks.values()), "checks": checks,
            "missing_facts": [k for k, v in facts.items() if not v],
            "format": fmt, "rows": rows, "words": words, "sources_cited": cited,
            "invented_numbers": R.invented_numbers(text, sp["sources"])[:20],
            "deliverables": {k: len(v) for k, v in files.items()}, "language": lang}


async def play_case(case: dict, base: Path, timeout_s: float) -> dict:
    sp = space_for(base, case)
    prompt = prompt_for(case, sp)
    pol = P.Policy("atelier", deliverables=sp["deliverables"], memory=sp["memory"],
                   has_documents=bool(sp["docs"]))
    play = R.Play("legacy", R.new_harness("legacy"),
                  R.spec_for("legacy", run_id="run-" + uuid.uuid4().hex[:8], cwd=sp["work"],
                             profile="atelier"), pol, timeout_s)
    play.space = sp
    t0 = time.monotonic()
    if not await play.start():
        return {"started": False, "start_error": play.start_error}
    await play.turn(prompt)
    await play.stop()
    files = R.deliverable_files(sp["deliverables"])
    out = {"started": True, "wall_s": round(time.monotonic() - t0, 1),
           "first_draft_s": play.turns[0]["first_deliverable_s"], "turn_end_s": play.turns[0]["end_s"],
           "cards": pol.summary(), "tools": play.tools(), "failures": play.failures(),
           "view_lines": play.view_lines(), "shell_ran": play.shell_ran(),
           "answer": play.turns[0]["text"][:1500]}
    out["grade"] = grade(case, play, sp, files)
    out["deliverable_texts"] = {k: v[:12000] for k, v in files.items()}
    return out


def table(results: list[dict]) -> str:
    lines = ["| Cas | Format | Verdict | 1er brouillon (s) | Fin (s) | Mots/lignes | Faits manquants | Échecs de contrôle |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        g = r.get("grade") or {}
        bad = [k for k, v in (g.get("checks") or {}).items() if not v]
        size = f"{g.get('rows')} lignes" if g.get("format") == "xlsx" else f"{g.get('words', 0)} mots"
        lines.append(f"| {r['case']} | {g.get('format', '?')} | {'OK' if g.get('ok') else 'à reprendre'} | "
                     f"{r.get('first_draft_s')} | {r.get('turn_end_s')} | {size} | "
                     f"{'; '.join(g.get('missing_facts') or []) or '—'} | {', '.join(bad) or (r.get('start_error') or '—')} |")
    ok = sum(1 for r in results if (r.get("grade") or {}).get("ok"))
    return f"**{ok}/{len(results)} OK**\n\n" + "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(ROOT / "tests" / "roles" / "results"))
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--regrade", default="", help="a roles-runs.json to grade again")
    a = ap.parse_args()
    if a.regrade:
        regrade(Path(a.regrade))
        return 0
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    upstream = R.env("PARITY_UPSTREAM")
    if not upstream:
        print("PARITY_UPSTREAM is not set", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(a.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(R.env("PARITY_WORKDIR", str(out_dir / "ws")))
    model = R.pick_model(upstream)
    results = []
    meter = W.Wire(upstream, str(out_dir / "wire.jsonl"), bind="127.0.0.1",
                   port=int(R.env("PARITY_WIRE_PORT", "18795")), thinking=False,
                   max_concurrent=int(R.env("PARITY_MAX_CONCURRENT", "2")), model=model)
    with meter as wire:
        R.legacy_env(out_dir / "claude-cfg", wire.url)
        for c in cases(only):
            tag = f"roles-{stamp}-{c['id']}"
            wire.tag = tag
            print(f"== {c['id']}", flush=True)
            try:
                r = asyncio.run(play_case(c, work / c["id"], a.timeout))
            except Exception as e:  # noqa: BLE001 — one broken case never stops the others
                r = {"started": False, "start_error": f"{type(e).__name__}: {e}"[:300]}
            wire.tag = ""
            r["wire"] = W.totals(wire.of(tag))
            wire.forget(tag)
            r["case"] = c["id"]
            r["template"] = c["template"]
            results.append(r)
            g = r.get("grade") or {}
            print(f"   {'OK' if g.get('ok') else 'KO'} {[k for k, v in (g.get('checks') or {}).items() if not v]}"
                  f" missing={g.get('missing_facts')} {r.get('start_error') or ''}", flush=True)
            (out_dir / "roles-runs.json").write_text(json.dumps(
                {"model": model, "engine": "legacy", "results": results}, ensure_ascii=False, indent=1),
                encoding="utf-8")
    (out_dir / "roles-runs.md").write_text(f"# Vrais runs des kits métiers — moteur hérité, {model}\n\n"
                                           + table(results), encoding="utf-8")
    print(table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
