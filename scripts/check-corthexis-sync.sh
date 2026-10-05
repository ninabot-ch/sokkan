#!/usr/bin/env bash
# memory/core is the CortHeXis engine (github.com/ninabot-ch/corthexis, package `corthexis`).
# This compares the CODE of both (Python syntax trees without docstrings, module paths
# normalised; SQL as is), so that a fix made on one side is not forgotten on the other.
#   scripts/check-corthexis-sync.sh /path/to/corthexis        exit 1 when the code differs
set -euo pipefail
CX=${1:?path to a corthexis checkout}
here=$(cd "$(dirname "$0")/.." && pwd)
python3 - "$here/memory/core" "$CX/corthexis" "$(git -C "$CX" describe --tags --always 2>/dev/null || echo "$CX")" <<'PY'
import ast, sys
from pathlib import Path

ours, theirs, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
# packaging, not engine: SOKKAN_* fallbacks, command names, install hints, standalone extras
KNOWN = {"__init__.py", "config.py", "profiles.py", "bench_store.py", "switch.py"}


def norm(p: Path) -> str:
    src = p.read_text(encoding="utf-8")
    if p.suffix != ".py":
        return src
    tree = ast.parse(src.replace("memory.core.", "core.").replace("corthexis.", "core."))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
        if isinstance(node, ast.Call):        # argparse prog= names the command, not the code
            node.keywords = [k for k in node.keywords if k.arg != "prog"]
    return ast.dump(tree)


bad = []
for f in sorted([*ours.glob("*.py"), *ours.glob("*.sql"), *ours.glob("migrations/*.sql")]):
    rel = f.relative_to(ours)
    if rel.name in KNOWN:
        continue
    o = theirs / rel
    if not o.exists():
        bad.append(f"only in SOKKAN: {rel}")
    elif norm(f) != norm(o):
        bad.append(f"differs: {rel}")
print("\n".join(bad) if bad else f"memory/core matches CortHeXis {label}")
sys.exit(1 if bad else 0)
PY
