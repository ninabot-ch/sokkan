#!/usr/bin/env python3
"""Verdict of one upgrade case (run.sh): every criterion with its evidence.

    report.py <case dir> <summary.json>      exit 1 when a criterion fails
"""
import datetime
import json
import sys
from pathlib import Path

W = Path(sys.argv[1])


def j(name, default=None):
    try:
        return json.loads((W / name).read_text())
    except (OSError, ValueError):
        return default


def lines(name):
    try:
        return [json.loads(x) for x in (W / name).read_text().splitlines() if x.strip()]
    except OSError:
        return []


def slug(s):
    return "-".join((s or "").lower().replace("_", "-").split())


case = dict(kv.split("=", 1) for kv in (W / "case.txt").read_text().split())
crit: dict[str, dict] = {}


def check(key, ok, evidence):
    crit[key] = {"ok": bool(ok), "evidence": evidence}


fatal = (W / "fatal.txt").read_text().strip() if (W / "fatal.txt").exists() else None
sa, sb, sr = j("snap-after.json", {}), j("snap-before.json", {}), j("snap-rollback.json", {})
st = j("state.json", {})

# 1. the instance starts
check("starts", (sa.get("health") or {}).get("status") == 200 and not fatal,
      f"health {(sa.get('health') or {}).get('status')}, upgrade in "
      f"{(W / 'upgrade-seconds.txt').read_text().strip() if (W / 'upgrade-seconds.txt').exists() else '?'} s"
      + (f"; FATAL {fatal}" if fatal else ""))

# 2/3. notes and dates (same rules as tests/e2e_migration/report.py)
n2, n3 = j("notes-before.json", {}), j("notes-after.json", {})
man, appl = j("manifest.json", {}), j("normalize-applied.json", {})
files = man.get("files", {})
renamed, merged = appl.get("renamed", {}), appl.get("merged", {})


def final(f):
    return renamed.get(merged.get(f, f), merged.get(f, f))


by_file3 = {v["file"]: (k, v) for k, v in (n3.get("notes") or {}).items()
            if k != "upg-after-switch"}
lost = [name for name, row in (n2.get("notes") or {}).items()
        if final(row["file"]) not in by_file3]
check("no_note_lost", n3.get("serving") == "store" and n2.get("notes") and not lost,
      f"2.x index {len(n2.get('notes') or {})} notes -> store {len(by_file3)} "
      f"({len(merged)} orphan merged into its note, {len(renamed)} renamed); lost: {lost or 'none'}")
sources: dict[str, list[str]] = {}
for f in files:
    sources.setdefault(final(f), []).append(f)
same, diff = 0, []
for f3, (name3, row3) in by_file3.items():
    srcs = sources.get(f3, [f3])
    prim = next((s for s in srcs if s not in merged), srcs[0])
    if prim not in files:
        diff.append(f"{name3}: not in the archive")
        continue
    declared = files[prim].get("declared")
    want = datetime.datetime.fromisoformat(declared) if declared else \
        datetime.datetime.fromtimestamp(max(files[s]["mtime"] for s in srcs), datetime.UTC)
    got = datetime.datetime.fromisoformat(row3["modified"])
    if abs((got - want).total_seconds()) <= 1:
        same += 1
    else:
        diff.append(f"{name3}: {got} != {want}")
v = (st.get("steps") or {}).get("verify", {})
check("dates_identical", by_file3 and not diff and st.get("status") == "done",
      f"{same}/{len(by_file3)} dates identical; migration {st.get('status')}, "
      f"verify dates_wrong={v.get('dates_wrong')} younger={v.get('younger')}"
      + (f"; different: {diff[:3]}" if diff else ""))

# 4. search served during the whole migration
w = lines("watch.jsonl")
bad = [x for x in w if x.get("error") or slug(x.get("top")) != "flour-supplier"]
served: dict[str, int] = {}
for x in w:
    served[f"{x['serving']}/{x.get('step') or '-'}"] = served.get(f"{x['serving']}/{x.get('step') or '-'}", 0) + 1
wh = lines("watch-http.jsonl")
down, streak, longest = 0, 0, 0
for x in wh:
    if x.get("status") == 200 and slug(x.get("top")) == "flour-supplier":
        streak = 0
    else:
        down += 1
        streak += 1
        longest = max(longest, streak)
# after the 3.0 api first answered, every HTTP search must succeed
first3 = next((i for i, x in enumerate(wh) if i and wh[i - 1].get("status") != 200
               and x.get("status") == 200), None)
http_bad_after = [x for x in (wh[first3:] if first3 is not None else []) if x.get("status") != 200
                  or slug(x.get("top")) != "flour-supplier"]
check("search_during_migration", w and not bad and w[-1].get("serving") == "store"
      and not http_bad_after,
      f"{len(w)} searches 1/s inside the api during the migration, {len(bad)} wrong/failed "
      f"({served}); through the web: {len(wh)} searches over the whole update, unanswered "
      f"{down} (longest gap {longest} s = the rebuild/restart), after 3.0 answered: "
      f"{len(http_bad_after)} failed")

# 5. recall injected
rd, ra = j("recall-during.json", {}), j("recall-after.json", {})
check("recall_injected", rd.get("injected") and ra.get("injected"),
      f"during the migration {rd.get('injected')}, after {ra.get('injected')}")

# 6. CortHeXis tab + API + user data
g, web, mig = sa.get("corthexis_graph") or {}, sa.get("web") or {}, sa.get("migration") or {}
seed = j("seed.json", {})
sess_ok = seed.get("session") in ((sa.get("sessions") or {}).get("ids") or [])
board_ok = set(seed.get("cards") or []) <= set((sa.get("board") or {}).get("titles") or [])
ir = j("indexrunner.json", {})
check("corthexis_and_api", g.get("status") == 200 and (g.get("nodes") or 0) >= len(by_file3) > 0
      and web.get("corthexis_tab") and mig.get("state") == "done" and sess_ok and board_ok
      and ir.get("indexed"),
      f"graph {g.get('status')} ({g.get('nodes')} nodes), CortHeXis tab in the web bundle "
      f"{web.get('corthexis_tab')}, /api/memory/migration {mig.get('status')} {mig.get('state')}, "
      f"session kept {sess_ok}, board cards kept {board_ok}, new note indexed after the switch "
      f"{ir.get('indexed')}")

# 7. bench
b0, b1, br = j("bench-before.json", {}), j("bench-after.json", {}), j("bench-rollback.json", {})
check("bench_not_worse", b1 and b0 and b1.get("hit@1", -1) >= b0.get("hit@1", 99)
      and b1.get("mrr@8", -1) >= b0.get("mrr@8", 9),
      f"before hit@1 {b0.get('hit@1')}/{b0.get('questions')} MRR {b0.get('mrr@8')} -> after "
      f"{b1.get('hit@1')}/{b1.get('questions')} MRR {b1.get('mrr@8')}")

# 8. rollback
sha = [(W / f).read_text().split()[0] if (W / f).exists() else None
       for f in ("memorydb.before.sha256", "memorydb.after.sha256")]
rb_sess = seed.get("session") in ((sr.get("sessions") or {}).get("ids") or [])
rb_board = set(seed.get("cards") or []) <= set((sr.get("board") or {}).get("titles") or [])
check("rollback", (sr.get("health") or {}).get("status") == 200 and rb_sess and rb_board
      and br and br.get("hit@1", -1) >= b0.get("hit@1", 99) - 1 and sha[0] == sha[1] and sha[0],
      f"{case['src']} back up {(sr.get('health') or {}).get('status')}, bench hit@1 "
      f"{br.get('hit@1')}/{br.get('questions')} MRR {br.get('mrr@8')}, session {rb_sess}, "
      f"cards {rb_board}, memory.db untouched by 3.0 {sha[0] == sha[1]}")

# RAM of the whole machine (cgroup): working set = current - inactive_file
phases = []
for line in (W / "phases.log").read_text().splitlines() if (W / "phases.log").exists() else []:
    t, _, name = line.partition(" ")
    phases.append((float(t), name))
mem = []
for line in (W / "mem.tsv").read_text().splitlines() if (W / "mem.tsv").exists() else []:
    p = line.split()
    if len(p) >= 5 and p[1].isdigit():
        mem.append((float(p[0]), int(p[1]) - int(p[2] or 0), int(p[3] or 0), int(p[4] or 0)))
peaks = {}
for i, (t0, name) in enumerate(phases):
    t1 = phases[i + 1][0] if i + 1 < len(phases) else float("inf")
    ws = [m[1] for m in mem if t0 <= m[0] < t1]
    if ws:
        peaks[name] = round(max(ws) / 2**20)
ooms = max((m[3] for m in mem), default=0)
ram = {"peak_mb_by_phase": peaks, "oom_kills": ooms,
       "peak_mb": round(max((m[1] for m in mem), default=0) / 2**20)}
check("no_oom", ooms == 0, f"OOM kills in the machine: {ooms}; peak working set "
      f"{ram['peak_mb']} MB; migration phase {peaks.get('migration')} MB")

ok = all(c["ok"] for c in crit.values())
summary = {"case": case, "ok": ok, "criteria": crit, "ram": ram,
           "compose": (W / "compose-version.txt").read_text().strip()
           if (W / "compose-version.txt").exists() else None,
           "engine": (W / "engine-version.txt").read_text().strip()
           if (W / "engine-version.txt").exists() else None,
           "tree": (W / "tree.txt").read_text().strip() if (W / "tree.txt").exists() else None}
Path(sys.argv[2]).write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
for k, c in crit.items():
    print(f"{'PASS' if c['ok'] else 'FAIL'}  {k:26} {c['evidence']}")
print(f"RAM peaks (MB) by phase: {peaks}")
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
