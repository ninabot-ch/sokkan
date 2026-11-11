#!/usr/bin/env python3
"""doc-ingest-bench.py — measure the memory agent on the gold set with a REAL local model.

    SOKKAN_DOC_INGEST_LLM_URL=http://127.0.0.1:8004/v1 SOKKAN_DOC_INGEST_LLM_MODEL=qwen3.8-27b \\
      python scripts/doc-ingest-bench.py --concurrency 8 --out results.json

Builds the 32 synthetic documents (tests/docingest_gold/gold.py) in a temporary directory,
runs one pass per source in an isolated data directory (nothing of a real instance is read
or written), scores the notes against the expectations, then runs a second pass to check
that nothing is read twice. Prints a summary; --out writes everything (including every
produced note) as JSON for a human to read the « extra » decisions.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("backend", "memory", "tests"):
    sys.path.insert(0, str(ROOT / p))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="")
    ap.add_argument("--docling", default="off", choices=["off", "auto", "always"])
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="docingest-bench-"))
    os.environ.update({
        "SOKKAN_DATA_DIR": str(tmp / "data"), "SOKKAN_MEMORY_DIR": str(tmp / "memory"),
        "SOKKAN_AUDIT_DB": str(tmp / "audit.db"),
        "SOKKAN_DOC_INGEST_CONCURRENCY": str(a.concurrency),
        "SOKKAN_DOC_INGEST_DOCLING": a.docling})
    for v in ("SOKKAN_DOC_INGEST_DIR", "SOKKAN_DOC_INGEST_CONFIG", "SOKKAN_MEMORY_QUARANTINE_DIR"):
        os.environ.pop(v, None)

    from docingest import config, llmclient, runner, state
    from docingest_gold import gold, score

    why = llmclient.ready()
    if why:
        print(why, file=sys.stderr)
        return 2
    gold.build(tmp / "gold")
    config.home().mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(json.dumps({"sources": [
        {"id": sid, "kind": "folder", "root": str(tmp / "gold" / sid), **s}
        for sid, s in gold.SOURCES.items()]}))

    t0 = time.time()
    passes = [runner.Pass(config.get(sid), "initial", "bench", memory_neighbours=lambda *x: []).run()
              for sid in gold.SOURCES]
    wall = time.time() - t0
    got = score.collect()
    ocr = a.docling != "off" and got["formation/Divers/scan-decision-fermeture.pdf"]["status"] != "error"
    s = score.score(got, ocr=ocr)

    t1 = time.time()
    again = [runner.Pass(config.get(sid), "incremental", "bench",
                         memory_neighbours=lambda *x: []).run() for sid in gold.SOURCES]
    wall2 = time.time() - t1

    cnt = {k: sum(p["counters"].get(k, 0) for p in passes) for k in state.COUNTERS}
    called = [e for p in passes for e in state.events(p["id"]) if e["tokens_in"]]
    per_doc = sorted(e["seconds"] for e in called)
    tin, tout = cnt["tokens_in"], cnt["tokens_out"]
    perf = {
        "model": llmclient.endpoint()["model"], "concurrency": a.concurrency,
        "docling": a.docling, "wall_seconds": round(wall, 1),
        "documents_listed": cnt["listed"], "documents_read": cnt["read"],
        "model_calls": len(called), "documents_per_hour": round(cnt["read"] / wall * 3600),
        "tokens_in": tin, "tokens_out": tout,
        "tokens_in_per_called_document": round(tin / max(1, len(called))),
        "tokens_out_per_called_document": round(tout / max(1, len(called))),
        "seconds_per_document_p50": per_doc[len(per_doc) // 2] if per_doc else 0,
        "seconds_per_document_max": per_doc[-1] if per_doc else 0,
        "notes": cnt["notes"], "decisions": cnt["decisions"], "flagged_notes": cnt["flagged"],
        "second_pass": {"wall_seconds": round(wall2, 1),
                        "read": sum(p["counters"]["read"] for p in again),
                        "unchanged": sum(p["counters"]["unchanged"] for p in again),
                        "tokens": sum(p["counters"]["tokens_in"] for p in again)},
        "pass_status": [p["status"] for p in passes],
    }
    out = {"perf": perf, "score": s,
           "documents": {k: {"status": v["status"], "detail": v["detail"], "tokens": v["tokens"],
                             "notes": v["notes"]} for k, v in got.items()}}
    if a.out:
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    brief = {k: v for k, v in s.items() if k not in ("flags",)}
    print(json.dumps({"perf": perf, "score": brief, "flags": s["flags"]}, ensure_ascii=False, indent=1))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"kept: {tmp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
