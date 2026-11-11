"""python -m docingest — the operator's command line (the first pass of an archive is long:
run it in a terminal or a systemd unit rather than from the cockpit).

    python -m docingest sources                 what is declared, and whether it is readable
    python -m docingest check <source>          list what WOULD be read (nothing is opened)
    python -m docingest run <source> [--initial]   one pass (Ctrl-C = interrupted, resumable)
    python -m docingest passes [<source>]       the journal
    python -m docingest batches                 batches waiting for review
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading

from . import batches, llmclient, runner, sources, state
from . import config as cfgmod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m docingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources")
    c = sub.add_parser("check")
    c.add_argument("source")
    r = sub.add_parser("run")
    r.add_argument("source")
    r.add_argument("--initial", action="store_true")
    p = sub.add_parser("passes")
    p.add_argument("source", nargs="?")
    sub.add_parser("batches")
    a = ap.parse_args(argv)

    import features
    if not features.enabled("doc_ingest"):
        print("feature doc_ingest is off (SOKKAN_FEATURE_DOC_INGEST=1)", file=sys.stderr)
        return 2
    if a.cmd == "sources":
        for s in cfgmod.load():
            print(json.dumps(s.public() | {"trust": batches.trust(s)}, ensure_ascii=False))
        print("model:", llmclient.ready() or "ready", file=sys.stderr)
        return 0
    if a.cmd == "check":
        src = cfgmod.get(a.source)
        if src is None:
            print(f"unknown source {a.source}", file=sys.stderr)
            return 2
        s = sources.open_source(src)
        print(json.dumps(s.prepare()), file=sys.stderr)
        n = size = 0
        for f in s.iter_files():
            n, size = n + 1, size + f.size
            print(f.rel)
        print(f"{n} file(s), {size / 1e6:.1f} MB inside the allow-list", file=sys.stderr)
        return 0
    if a.cmd == "run":
        stop = threading.Event()

        def _sig(*_):
            print("stopping after the documents in progress…", file=sys.stderr)
            stop.set()
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
        out = runner.run_source(a.source, "initial" if a.initial else "manual", "cli", stop=stop,
                                on_progress=lambda d: print(d["rel"], file=sys.stderr))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("status") in ("done", "budget", "interrupted") else 1
    if a.cmd == "passes":
        for row in state.pass_list(source=a.source):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if a.cmd == "batches":
        for src in cfgmod.load():
            for b in batches.list_batches(src.project, source=src.id):
                print(json.dumps(b, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
