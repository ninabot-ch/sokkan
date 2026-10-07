#!/usr/bin/env python3
"""git credential helper of a SOKKAN session (lot 5) — standard library only.

git runs it as ``<helper> get|store|erase`` with the request on stdin (protocol, host,
path…). ``get`` asks the SOKKAN API on the loopback, with the session's ticket
(SOKKAN_FORGE_TICKET), for the PERSON's current token, and prints it to git — on stdout,
which git reads; it never reaches a terminal, a log or a file. ``erase`` (git was refused)
asks the API to re-read the person's access. ``store`` is a no-op: nothing is kept.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ""
    attrs: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            attrs[k] = v
    if action not in ("get", "erase"):
        return 0
    ticket = os.environ.get("SOKKAN_FORGE_TICKET", "")
    api = os.environ.get("SOKKAN_FORGE_API_URL", "http://127.0.0.1:8097").rstrip("/")
    if not ticket:
        print("sokkan: no forge ticket in this session — it cannot push", file=sys.stderr)
        return 0
    body = json.dumps({"ticket": ticket, "action": action,
                       "protocol": attrs.get("protocol", ""), "host": attrs.get("host", ""),
                       "path": attrs.get("path", "")}).encode()
    req = urllib.request.Request(api + "/api/forge/git-credential", data=body, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ans = json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"sokkan: credential request failed ({type(e).__name__})", file=sys.stderr)
        return 0
    if action == "get":
        if ans.get("username") and ans.get("password"):
            sys.stdout.write(f"username={ans['username']}\npassword={ans['password']}\n")
        elif ans.get("reason"):
            print(f"sokkan: no credentials for {attrs.get('host', '?')}: {ans['reason']}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
