#!/usr/bin/env python3
"""teams-manifest.py — build the Microsoft Teams app package of a SOKKAN instance.

Same manifest as ``GET /api/admin/teams/manifest`` (one source: backend/teams/manifest.py),
from the environment of the instance or the flags:

    SOKKAN_TEAMS_APP_ID=<client id> SOKKAN_PUBLIC_URL=https://sokkan.example.ch \\
        scripts/teams-manifest.py --out ./teams-app

writes ``teams-app/manifest.json``, ``color.png`` (192×192), ``outline.png`` (32×32, white on
transparent) and ``sokkan-teams-app.zip`` (the three files, flat) to upload in the Teams
admin center (Manage apps → Upload new app) or with « Upload a custom app » in Teams.
``--check`` validates manifest.json against the vendored JSON schema (needs ``jsonschema``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from teams import manifest as M  # noqa: E402

SCHEMA = ROOT / "tests" / "fixtures" / "teams" / f"MicrosoftTeams.v{M.MANIFEST_VERSION}.schema.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app-id", default=os.environ.get("SOKKAN_TEAMS_APP_ID", ""),
                    help="Entra application (client) id = bot id [SOKKAN_TEAMS_APP_ID]")
    ap.add_argument("--public-url", default=(os.environ.get("SOKKAN_TEAMS_PUBLIC_URL")
                                             or os.environ.get("SOKKAN_PUBLIC_URL") or ""),
                    help="public https address of the instance [SOKKAN_TEAMS_PUBLIC_URL / "
                         "SOKKAN_PUBLIC_URL]")
    ap.add_argument("--name", default="Nina (SOKKAN)", help="short name shown in Teams (≤ 30)")
    ap.add_argument("--developer", default="SOKKAN", help="developer name (≤ 32)")
    ap.add_argument("--sso", action="store_true",
                    help="add webApplicationInfo (api://<host>/<app id> must be exposed in Entra)")
    ap.add_argument("--out", default="teams-app", help="output directory")
    ap.add_argument("--check", action="store_true", help="validate against the JSON schema")
    a = ap.parse_args(argv)
    try:
        m = M.build(a.app_id.strip(), a.public_url.strip(), name=a.name, developer=a.developer,
                    sso=a.sso)
    except M.ManifestError as e:
        print(f"teams-manifest: {e}", file=sys.stderr)
        return 2
    if a.check:
        try:
            import jsonschema
        except ImportError:
            print("teams-manifest: --check needs the jsonschema package", file=sys.stderr)
            return 2
        errs = list(jsonschema.Draft4Validator(json.loads(SCHEMA.read_text())).iter_errors(m))
        for e in errs:
            print(f"schema: {'/'.join(map(str, e.path))}: {e.message}", file=sys.stderr)
        if errs:
            return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    (out / "color.png").write_bytes(M.color_icon())
    (out / "outline.png").write_bytes(M.outline_icon())
    (out / "sokkan-teams-app.zip").write_bytes(M.package(m))
    print(f"{out / 'sokkan-teams-app.zip'}  (manifest v{M.MANIFEST_VERSION}, app {m['id']}, "
          f"domain {m['validDomains'][0]}{', sso' if a.sso else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
