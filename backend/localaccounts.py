#!/usr/bin/env python3
"""localaccounts.py — named accounts for the LOCAL auth mode (no identity provider).

`SOKKAN_AUTH_MODE=local` knew one identity: whoever typed `SOKKAN_LOCAL_TOKEN` was the owner,
so three people sharing a box were one person in the audit journal. An on-prem appliance has
no IdP to delegate to (OIDC would mean running and operating a Keycloak/Authentik next to
SOKKAN for three people), so each person gets a PERSONAL ACCESS CODE:

* an admin creates the account (IAM user, role) and asks for a code — shown ONCE;
* the login screen is unchanged (one field): the code a person types says who they are;
* the instance stores only a salted SHA-256 of the code (256-bit random codes: a slow KDF
  would add nothing), who created it and when it was last used;
* a new code for the same person replaces the old one; revoking removes it.

`SOKKAN_LOCAL_TOKEN` keeps working and remains the OWNER's code (existing installs).
With `multi_project` on, `add --project <slug>` also creates the person's project and grants
them `admin` on it: one instance, three accounts, one project each, three names in the journal.

CLI (in the api container: `docker compose exec -u 1000 -w /app/backend api python3 -m localaccounts …`):

    add <email> [--name N] [--role viewer|dev|admin] [--project slug] [--project-role R]
    code <email>          new code for an existing account (replaces the previous one)
    revoke <email>        remove the code (the account stays; it can no longer sign in)
    list                  accounts with a code, creation date, last sign-in — never a code
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

import iam

PREFIX = "sk-local-"          # recognisable in a password manager, never a model key


def _db() -> Path:
    return Path(os.environ.get("SOKKAN_LOCAL_ACCOUNTS_DB") or iam.DB)


def _con() -> sqlite3.Connection:
    iam.init()
    p = _db()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS local_codes (email TEXT PRIMARY KEY, salt TEXT NOT NULL,"
                " hash TEXT NOT NULL, created_at REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',"
                " last_login REAL)")
    if "name" not in {r[1] for r in con.execute("PRAGMA table_info(local_codes)")}:
        con.execute("ALTER TABLE local_codes ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    return con


def _hash(code: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + code).encode()).hexdigest()


def any_account() -> bool:
    con = _con()
    try:
        return con.execute("SELECT 1 FROM local_codes LIMIT 1").fetchone() is not None
    finally:
        con.close()


def has(email: str) -> bool:
    con = _con()
    try:
        return con.execute("SELECT 1 FROM local_codes WHERE email=?",
                           ((email or "").lower().strip(),)).fetchone() is not None
    finally:
        con.close()


def issue(email: str, by: str = "", name: str = "", create: bool = False) -> str:
    """A new code for an account that exists — an IAM user, or a person who already has a
    code (project-only accounts have no instance role, hence no IAM row). It replaces the
    previous code and is returned once. `create` = the caller is creating the account."""
    email = (email or "").lower().strip()
    if not (create or iam.get_user(email).get("known") or has(email)):
        raise ValueError(f"unknown account: {email} (create the user first)")
    code = PREFIX + secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    con = _con()
    with con:
        con.execute("INSERT INTO local_codes(email, salt, hash, created_at, created_by, last_login, name)"
                    " VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(email) DO UPDATE SET salt=excluded.salt,"
                    " hash=excluded.hash, created_at=excluded.created_at,"
                    " created_by=excluded.created_by, last_login=NULL,"
                    " name=CASE WHEN excluded.name != '' THEN excluded.name ELSE local_codes.name END",
                    (email, salt, _hash(code, salt), time.time(), by or "", (name or "").strip()[:120]))
    con.close()
    return code


def revoke(email: str) -> bool:
    con = _con()
    with con:
        n = con.execute("DELETE FROM local_codes WHERE email=?", ((email or "").lower().strip(),)).rowcount
    con.close()
    return n > 0


def match(code: str) -> str | None:
    """The e-mail whose code this is, else None. Every row is compared (no early exit)."""
    code = (code or "").strip()
    if not code.startswith(PREFIX) or len(code) > 200:
        return None
    con = _con()
    rows = con.execute("SELECT email, salt, hash FROM local_codes").fetchall()
    found = None
    for r in rows:
        if hmac.compare_digest(_hash(code, r["salt"]), r["hash"]):
            found = r["email"]
    if found:
        with con:
            con.execute("UPDATE local_codes SET last_login=? WHERE email=?", (time.time(), found))
    con.close()
    return found


def display_name(email: str) -> str:
    """The IAM name when there is one, else the name given when the code was created."""
    n = iam.display_name(email)
    if n:
        return n
    con = _con()
    try:
        r = con.execute("SELECT name FROM local_codes WHERE email=?",
                        ((email or "").lower().strip(),)).fetchone()
        return (r["name"] if r else "") or ""
    finally:
        con.close()


def accounts() -> list[dict]:
    """Who can sign in with a code — never the code, never its hash."""
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT email, name, created_at, created_by, last_login FROM local_codes ORDER BY created_at")]
    con.close()
    for r in rows:
        u = iam.get_user(r["email"])
        r["role"] = u.get("role") if u.get("known") else "none"
        r["name"] = iam.display_name(r["email"]) or r["name"] or r["email"]
    return rows


def add(email: str, name: str = "", role: str = "none", project: str = "",
        project_role: str = "admin", by: str = "cli") -> dict:
    """Create (or update) the account, its code and — optionally — its own project.

    role = the INSTANCE role. `none` (default) = no instance role: the person sees only the
    projects that grant them one (needs SOKKAN_DEFAULT_ROLE=none, which the appliance profile
    sets — with the community default `viewer` they would also read the shared project).
    viewer | dev | admin = an IAM user with that role on the whole instance."""
    email = (email or "").lower().strip()
    if "@" not in email or " " in email:
        raise ValueError("email: a full address (it is the name shown in the journal)")
    if role == "owner":
        raise ValueError("the owner is SOKKAN_OWNER_EMAIL; give admin at most")
    if role != "none":
        iam.upsert_user(email, role, name)
    elif not project:
        raise ValueError("an account with no instance role needs --project (it would see nothing)")
    out = {"email": email, "role": role, "project": ""}
    if project:
        import projects
        if not projects.get(project):
            # access by GRANT (the `sso_group` source reads user and team grants); the
            # `instance` source would give every signed-in person their instance role on it
            projects.create(project, name or project, access_source="sso_group", created_by=by)
        projects.grant(project, "user", email, project_role, created_by=by)
        out["project"] = project
    out["code"] = issue(email, by, name=name, create=True)
    return out


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="localaccounts", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("email")
    a.add_argument("--name", default="")
    a.add_argument("--role", default="none", choices=["none", "viewer", "dev", "admin"])
    a.add_argument("--project", default="")
    a.add_argument("--project-role", default="admin")
    for c in ("code", "revoke"):
        sub.add_parser(c).add_argument("email")
    sub.add_parser("list")
    ns = ap.parse_args(argv)
    import audit
    try:
        if ns.cmd == "add":
            r = add(ns.email, ns.name, ns.role, ns.project, ns.project_role)
            audit.log("cli", "iam.local_account.add", r["email"],
                      f"role={r['role']} project={r['project'] or '-'}", project="")
            print(f"{r['email']}\t{r['code']}")
        elif ns.cmd == "code":
            print(f"{ns.email}\t{issue(ns.email, 'cli')}")
            audit.log("cli", "iam.access_code.create", ns.email, project="")
        elif ns.cmd == "revoke":
            ok = revoke(ns.email)
            audit.log("cli", "iam.access_code.revoke", ns.email, project="")
            print("revoked" if ok else "no code for that account")
        else:
            for r in accounts():
                last = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_login"])) \
                    if r["last_login"] else "never"
                print(f"{r['email']}\t{r['role']}\tlast sign-in: {last}")
    except ValueError as e:
        print(f"localaccounts: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
