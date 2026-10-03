#!/usr/bin/env python3
"""Markdown table of tests/e2e_upgrade/results/*.json (the summaries written by report.py).

    table.py [results dir]      -> stdout (pasted into RESULTS.md)
"""
import json
import sys
from pathlib import Path

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "results"
ORDER = ["2.3.0-installer", "2.3.0-manual", "2.2.0-installer", "2.2.0-manual",
         "2.0.1-installer", "2.0.1-manual", "0.1.0-installer", "0.1.0-manual",
         "old-compose", "vm4g"]
COLS = [("starts", "starts"), ("no_note_lost", "notes"), ("dates_identical", "dates"),
        ("search_during_migration", "search during"), ("recall_injected", "recall"),
        ("corthexis_and_api", "CortHeXis + API"), ("bench_not_worse", "bench ≥"),
        ("rollback", "rollback"), ("no_oom", "no OOM")]
rows = {p.stem: json.loads(p.read_text()) for p in D.glob("*.json")}
print("| case | " + " | ".join(c for _, c in COLS) + " | bench hit@1 / MRR before → after | "
      "web gap | RAM peak (migration) | Compose / Engine |")
print("|" + "---|" * (len(COLS) + 5))
for name in [n for n in ORDER if n in rows] + sorted(set(rows) - set(ORDER)):
    r = rows[name]
    c = r["criteria"]
    cells = ["✅" if c.get(k, {}).get("ok") else "❌" for k, _ in COLS]
    bench = c.get("bench_not_worse", {}).get("evidence", "")
    b = bench.replace("before hit@1 ", "").replace(" -> after ", " → ")
    gap = c.get("search_during_migration", {}).get("evidence", "")
    g = gap.split("longest gap ")[-1].split(" s")[0] + " s" if "longest gap" in gap else "?"
    ram = r.get("ram", {})
    peak = f"{ram.get('peak_mb')} MB ({ram.get('peak_mb_by_phase', {}).get('migration')} MB)"
    cv = (r.get("compose") or "").replace("Docker Compose version ", "")
    print(f"| {name} | " + " | ".join(cells) + f" | {b} | {g} | {peak} | {cv} / {r.get('engine')} |")
