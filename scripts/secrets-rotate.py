#!/usr/bin/env python3
"""scripts/secrets-rotate.py — see backend/secrets_provider/cli.py and docs/enterprise/SECRETS.md.

Runs with the instance's environment (SOKKAN_DATA_DIR, SOKKAN_SECRETS_PROVIDER, SOKKAN_OPENBAO_*).
In the api container: docker compose exec -w /app/backend api python3 -m secrets_provider.cli rotate …
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
from secrets_provider.cli import main  # noqa: E402

args = sys.argv[1:]
if not args or args[0].startswith("-"):
    args = ["rotate", *args]
sys.exit(main(args))
