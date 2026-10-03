#!/usr/bin/env python3
"""CLI SOKKAN Magnitude.

  python3 -m magnitude --cockpit URL --token TOK   # agent (boucle de sync)
  python3 -m magnitude --profile                   # one-shot : profil JSON stdout
  python3 -m magnitude --memory-profile [--env]    # profil mémoire recommandé (leger|standard|gpu)
"""
import argparse
import json

from . import __version__, hw


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m magnitude",
        description="SOKKAN Magnitude host agent — profile, benchmark and serve "
                    "local models on this machine (pure stdlib, no install).")
    ap.add_argument("--cockpit", metavar="URL",
                    help="cockpit base URL (e.g. http://localhost:3009)")
    ap.add_argument("--token", metavar="TOK",
                    help="pairing token from the cockpit's Magnitude tab")
    ap.add_argument("--profile", action="store_true",
                    help="print the hardware profile as JSON and exit")
    ap.add_argument("--memory-profile", action="store_true",
                    help="recommend the SOKKAN memory profile (leger|standard|gpu) "
                         "for this machine, as JSON, and exit")
    ap.add_argument("--env", action="store_true",
                    help="with --memory-profile: print KEY=VALUE lines for .env")
    ap.add_argument("--version", action="version",
                    version=f"magnitude {__version__}")
    args = ap.parse_args()

    if args.memory_profile:
        # vendored copy of the CortHeXis memory-core `profiles.py` (stdlib only)
        from . import memprofile
        rec = memprofile.detect()
        if args.env:
            print(memprofile.env_lines(rec, "SOKKAN"), end="")
        else:
            print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    if args.profile:
        print(json.dumps(hw.build_profile(), indent=2))
        return 0
    if not args.cockpit or not args.token:
        ap.error("--cockpit and --token are required (or use --profile)")
    from .agent import Agent  # import tardif : --profile reste instantané
    Agent(args.cockpit, args.token).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
