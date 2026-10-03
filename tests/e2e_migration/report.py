#!/usr/bin/env python3
"""Summary of an end-to-end migration run (run.sh): same notes, same dates, continuity
of the search, bench before / after, RAM. Exit 1 if a criterion fails."""
import datetime
import json
import sys
from pathlib import Path

W = Path(sys.argv[1])
j = lambda n: json.loads((W / n).read_text())  # noqa: E731
n2, n3 = j("notes-2x.json"), j("notes-3x.json")
st, man, appl = j("state.json"), j("manifest.json"), j("normalize-applied.json")
files = man["files"]
renamed, merged = appl.get("renamed", {}), appl.get("merged", {})
final = lambda f: renamed.get(merged.get(f, f), merged.get(f, f))  # noqa: E731
by_file3 = {v["file"]: (k, v) for k, v in n3["notes"].items()}
sources: dict[str, list[str]] = {}
for f in files:
    sources.setdefault(final(f), []).append(f)

ok = True
lost, same, diff = [], 0, []
for name2, row in n2["notes"].items():
    f3 = final(row["file"])
    if f3 not in by_file3:
        lost.append(name2)
for f3, (name3, row3) in by_file3.items():
    srcs = sources.get(f3, [f3])
    prim = next((s for s in srcs if s not in merged), srcs[0])
    declared = files[prim].get("declared")
    want = datetime.datetime.fromisoformat(declared) if declared else \
        datetime.datetime.fromtimestamp(max(files[s]["mtime"] for s in srcs), datetime.UTC)
    got = datetime.datetime.fromisoformat(row3["modified"])
    if abs((got - want).total_seconds()) <= 1:
        same += 1
    else:
        diff.append(f"{name3}: {got} != {want}")
v = st["steps"].get("verify", {})
print(f"notes: 2.x index {len(n2['notes'])}, store {len(n3['notes'])} "
      f"({len(merged)} orphan merged by normalize); 2.x notes missing: {lost or 'none'}")
print(f"dates: {same}/{len(by_file3)} identical to the 2.x date (frontmatter, else mtime); "
      f"different: {diff or 'none'}")
print(f"migration: status {st['status']}, verify: dates_wrong={v.get('dates_wrong')}, "
      f"younger={v.get('younger')}, dates equal to memory.db mtime: "
      f"{v.get('dates_equal_2x_index')}")
ok &= st["status"] == "done" and not lost and not diff

w = [json.loads(line) for line in (W / "watch.jsonl").read_text().splitlines() if line.strip()]
bad = [x for x in w if x["error"] or not x["top"]]
served = {}
for x in w:
    served.setdefault((x["serving"], x["step"]), 0)
    served[(x["serving"], x["step"])] += 1
print(f"continuity: {len(w)} searches (1/s) during the migration, {len(bad)} without a "
      f"result; top hit always the flour supplier note: "
      f"{all((x['top'] or '').replace('_', '-') == 'flour-supplier' for x in w)}")
for (srv, step), n in served.items():
    print(f"   served by {srv:9} during step {step or '-':12} : {n}")
ok &= not bad and any(x["serving"] == "memory.db" and x["step"] == "index" for x in w) \
    and w[-1]["serving"] == "store"

b2, b3, br = j("bench-2x.json"), j("bench-3x.json"), j("bench-rollback.json")
for label, b in (("2.3 (memory.db, MiniLM)", b2), ("3.0 (store)", b3), ("rollback 2.3", br)):
    print(f"bench {label:24}: hit@1 {b['hit@1']}/{b['questions']}, MRR@8 {b['mrr@8']}, "
          f"p50 {b['p50_ms']} ms, degraded {b['degraded']}")
ok &= b3["mrr@8"] >= b2["mrr@8"] and b3["hit@1"] >= b2["hit@1"]
sha1 = (W / "memorydb.sha256").read_text().split()[0]
sha2 = (W / "memorydb.after.sha256").read_text().split()[0]
print(f"memory.db untouched by 3.0: {sha1 == sha2}")
ok &= sha1 == sha2
print("RAM:", (W / "ram.txt").read_text().strip().replace("\n", " | "))
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
