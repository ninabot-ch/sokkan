#!/usr/bin/env python3
"""demo_captains.py — « Captains » on the PUBLIC READ-ONLY DEMO (3.2.2). Never anywhere else.

The 3.1.1 demo showed Crew; this one shows what 3.2 « Captains » brings — several
projects, teams, Helm — to the anonymous visitor (a viewer), with nothing real in it:

1. `seed <captains.json>` — writes fictional projects (2-3), teams of fictional people
   (`@example.com` only — anything else is refused), the visitor's viewer grant on each
   project, Helm cards in a hierarchy with their progress, one reframe suggestion (a card
   that contradicts a recorded decision) and a « Shared with me » share to the visitor.
   Idempotent: the seed remembers what it wrote (table `demo_captains` in board.db) and
   rewrites it; the agents seeded by demo_crew, and anything else, are left alone.

2. Read-only, enforced server-side when the feature is on (`demo_captains`, requires
   `demo_banner` + `multi_project`):
   * `guard(...)` — every non-read request under /api from someone below instance admin
     answers 403 « read-only demo » (Nina's chat excepted: it has its own daily cap);
   * Helm is readable by the members of a project (not only its managers) — read routes
     only (`helm.can_view`);
   * `org_view()` — Setup › Organization for the visitor: the fictional members, projects
     and teams only (an email that is not `@example.com` is never served);
   * Setup › Engines: no key tail, no « connected by », no base URL for a non-admin.

Spec: docs/enterprise/UI-FEATURES.md § Captains demo.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

FICTION_DOMAIN = "@example.com"
SEED_BY = "demo-captains-seed"
TEAM_PREFIX = "local:demo-"
# writes a visitor may still do on the demo: Nina (capped per day by SOKKAN_ASSISTANT_DAILY_LIMIT)
WRITE_ALLOWED = ("/api/assistant/",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
READ_ONLY = "read-only demo"


def requested() -> bool:
    import features
    return bool(features.requested("demo_captains"))


def enabled() -> bool:
    """Asked for (SOKKAN_DEMO_CAPTAINS=1) AND on the demo (banner) AND projects on."""
    try:
        import features
        return features.enabled("demo_captains")
    except Exception:  # noqa: BLE001 — registry unavailable: not the demo
        return False


def visitor() -> str:
    return (os.environ.get("SOKKAN_OWNER_EMAIL") or "demo@sokkan.ch").lower().strip()


def fictional(email: str) -> bool:
    e = (email or "").lower().strip()
    return e.endswith(FICTION_DOMAIN) and e.count("@") == 1 and len(e) > len(FICTION_DOMAIN)


# ---- the write guard (called by app.require_auth) -------------------------------------
def guard(method: str, path: str, user: dict) -> str | None:
    """None = let it through; otherwise the 403 detail."""
    if method.upper() in SAFE_METHODS or not path.startswith("/api/"):
        return None
    if path.startswith(WRITE_ALLOWED):
        return None
    if not enabled():
        return None
    import iam
    if iam.rank(user.get("role") or "") >= iam.rank("admin"):
        return None            # the demo's operator (never the anonymous visitor)
    return READ_ONLY


# ---- Setup › Organization for the visitor ---------------------------------------------
def org_view() -> dict:
    """Fictional people only: an email that is not @example.com is never in the answer
    (the visitor's own grant is shown as « you »)."""
    import iam
    import projects
    me = visitor()
    members = [{"email": u["email"], "name": u.get("name") or u["email"], "role": u["role"]}
               for u in iam.list_users() if fictional(u["email"]) and u["role"] in ("viewer", "dev")]
    teams = []
    for t in projects.list_teams():
        if not t["id"].startswith(TEAM_PREFIX):
            continue
        teams.append({"id": t["id"], "name": t["name"],
                      "members": [m for m in projects.team_members(t["id"]) if fictional(m)]})
    team_ids = {t["id"] for t in teams}
    out = []
    for p in projects.list_projects():
        if p["created_by"] != SEED_BY and p["slug"] != projects.DEFAULT_PROJECT:
            continue
        grants = []
        for g in projects.list_grants(p["slug"]):
            if g["principal_kind"] == "team" and g["principal"] in team_ids:
                name = next(t["name"] for t in teams if t["id"] == g["principal"])
                grants.append({"kind": "team", "principal": name, "role": g["role"]})
            elif g["principal_kind"] == "user" and fictional(g["principal"]):
                grants.append({"kind": "user", "principal": g["principal"], "role": g["role"]})
            elif g["principal_kind"] == "user" and g["principal"] == me:
                grants.append({"kind": "user", "principal": "you (demo visitor)", "role": g["role"]})
        out.append({"slug": p["slug"], "name": p["name"], "description": p["description"],
                    "access_source": p["access_source"], "grants": grants})
    return {"read_only": True, "members": members, "teams": teams, "projects": out,
            "note": "Fictional organization of the public demo — names and emails are made up."}


# ---- seed -----------------------------------------------------------------------------
class SeedError(ValueError):
    pass


_SCHEMA = """CREATE TABLE IF NOT EXISTS demo_captains (
    card_id INTEGER PRIMARY KEY, project TEXT NOT NULL)"""


def _bcon() -> sqlite3.Connection:
    import board
    con = board._con()
    con.execute(_SCHEMA)
    return con


def _walk(cards: list, depth: int = 0):
    for c in cards or []:
        yield c, depth
        yield from _walk(c.get("children") or [], depth + 1)


def check(spec: dict) -> dict:
    """Validate a seed file: 2-3 projects, fictional people only, a hierarchy."""
    import board
    import projects
    people = spec.get("people") or []
    if not people:
        raise SeedError("seed: 'people' must be a non-empty list")
    emails = set()
    for p in people:
        e = (p.get("email") or "").lower().strip()
        if not fictional(e):
            raise SeedError(f"{e!r}: demo people must be fictional ({FICTION_DOMAIN})")
        if p.get("role", "dev") not in ("viewer", "dev"):
            raise SeedError(f"{e}: role viewer or dev only (never admin on the demo)")
        emails.add(e)
    teams = spec.get("teams") or []
    tids = set()
    for t in teams:
        if not projects.valid_slug(t.get("id")):
            raise SeedError(f"team id {t.get('id')!r}: lowercase slug")
        for m in t.get("members") or []:
            if m.lower() not in emails:
                raise SeedError(f"team {t['id']}: {m} is not in 'people'")
        tids.add(t["id"])
    prj = spec.get("projects") or []
    if not 2 <= len(prj) <= 3:
        raise SeedError("seed: 2 or 3 projects")
    for p in prj:
        if not projects.valid_slug(p.get("slug")) or p["slug"] in (projects.DEFAULT_PROJECT,
                                                                   projects.SHARED_PROJECT):
            raise SeedError(f"project slug {p.get('slug')!r}")
        for team, role in (p.get("teams") or {}).items():
            if team not in tids or role not in projects.PROJECT_ROLES:
                raise SeedError(f"{p['slug']}: team {team} / role {role}")
        for c, depth in _walk(p.get("cards") or []):
            if depth > 3:
                raise SeedError(f"{p['slug']}: hierarchy deeper than 4 levels")
            if not (c.get("title") or "").strip():
                raise SeedError(f"{p['slug']}: a card without a title")
            if c.get("bucket", "Backlog") not in board.BUCKETS:
                raise SeedError(f"{p['slug']}: bucket {c.get('bucket')!r}")
            a = (c.get("assignee") or "").lower()
            if a and a not in emails:
                raise SeedError(f"{p['slug']}: assignee {a} is not in 'people'")
    for p in prj:
        if p.get("lead") and p["lead"].lower() not in emails:
            raise SeedError(f"{p['slug']}: lead {p['lead']} is not in 'people'")
    sh = spec.get("share")
    if sh and (sh.get("from") or "").lower() not in emails:
        raise SeedError("share.from must be one of 'people'")
    return {"people": len(people), "teams": len(teams), "projects": len(prj),
            "cards": sum(1 for p in prj for _ in _walk(p.get("cards") or []))}


def _forget_cards() -> int:
    """Remove the cards a previous seed wrote (deepest first) and their suggestions."""
    import board
    import helm
    con = _bcon()
    try:
        ids = [r["card_id"] for r in con.execute("SELECT card_id FROM demo_captains")]
    finally:
        con.close()
    known = [c for c in (board.get_card(i) for i in ids) if c]
    depth = {c["id"]: len(board.ancestors(c["id"])) for c in known}
    for c in sorted(known, key=lambda c: -depth[c["id"]]):
        board.delete_card(c["id"], user=SEED_BY)
    con = helm._con()
    try:
        if ids:
            q = ",".join("?" * len(ids))
            con.execute(f"DELETE FROM helm_suggestions WHERE card_id IN ({q}) OR target_id IN ({q})",
                        ids + ids)
            con.execute(f"DELETE FROM helm_rollup WHERE card_id IN ({q})", ids)
        con.execute("DELETE FROM demo_captains")
        con.commit()
    finally:
        con.close()
    return len(known)


def _backdate(card_id: int, days: float, now: float, done: bool) -> None:
    import board
    t = now - days * 86400
    con = board._con()
    try:
        con.execute("UPDATE cards SET created_at=?, sort=?, updated_at=?,"
                    " closed_at=CASE WHEN bucket='Done' THEN ? ELSE closed_at END WHERE id=?",
                    (t, t, t + (days * 0.4 * 86400 if done else 3600), t + days * 0.5 * 86400, card_id))
        con.execute("UPDATE card_events SET ts=? WHERE card_id=? AND action='created'", (t, card_id))
        con.commit()
    except sqlite3.OperationalError:
        con.rollback()           # an older board schema: the dates stay « now »
    finally:
        con.close()


def _cards(project: str, items: list, parent: int | None, now: float, made: list, by: str) -> None:
    import board
    for c in items or []:
        kind = "project" if parent is None else "task"
        row = board.add_card(
            c["title"][:200], c.get("description", ""), tag=c.get("tag", "research"),
            bucket=c.get("bucket", "Backlog"), priority=int(c.get("priority", 2)),
            due=c.get("due", ""), user=by, origin={"via": "demo"}, project=project,
            parent_id=parent, kind=kind, intent=c.get("intent", ""),
            constraints=c.get("constraints", ""), decisions=c.get("decisions") or [],
            assignee=(c.get("assignee") or "").lower())
        made.append(row["id"])
        con = _bcon()
        try:
            con.execute("INSERT OR REPLACE INTO demo_captains(card_id, project) VALUES(?,?)",
                        (row["id"], project))
            con.commit()
        finally:
            con.close()
        _backdate(row["id"], float(c.get("ago_days", 10)), now, c.get("bucket") == "Done")
        _cards(project, c.get("children") or [], row["id"], now, made, by)
        for body in c.get("comments") or []:
            board.add_comment(row["id"], body["body"], author=body.get("author", by),
                              origin={"via": "demo"})


def seed(spec: dict, now: float | None = None) -> dict:
    import board
    import helm
    import iam
    import projects
    now = time.time() if now is None else now
    check(spec)
    me = visitor()
    # people: instance role viewer/dev (they never log in: the demo has one identity)
    for p in spec["people"]:
        iam.upsert_user(p["email"], p.get("role", "dev"), p.get("name", ""))
    # teams (local, prefixed): membership replaced
    con = projects._con()
    with con:
        for t in spec.get("teams") or []:
            tid = TEAM_PREFIX + t["id"]
            con.execute("INSERT INTO teams(id, name, source, external_id, synced_at) VALUES(?,?,"
                        "'local','',?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                        " synced_at=excluded.synced_at", (tid, t.get("name") or t["id"], now))
            con.execute("DELETE FROM team_members WHERE team_id=?", (tid,))
            for m in t.get("members") or []:
                con.execute("INSERT INTO team_members(team_id, email, synced_at) VALUES(?,?,?)",
                            (tid, m.lower(), now))
    con.close()
    removed = _forget_cards()
    report = {"projects": [], "removed_cards": removed}
    for p in spec["projects"]:
        slug = p["slug"]
        if projects.get(slug) is None:
            projects.create(slug, p.get("name") or slug, access_source="sso_group",
                            created_by=SEED_BY, description=p.get("description", ""))
        elif projects.get(slug)["created_by"] != SEED_BY:
            raise SeedError(f"project {slug} exists and was not made by the demo seed — refused")
        else:
            projects.update(slug, name=p.get("name") or slug, description=p.get("description", ""))
        for g in projects.list_grants(slug):          # grants = exactly the seed's
            projects.revoke(slug, g["principal_kind"], g["principal"])
        for team, role in (p.get("teams") or {}).items():
            projects.grant(slug, "team", TEAM_PREFIX + team, role, created_by=SEED_BY)
        projects.grant(slug, "user", me, "viewer", created_by=SEED_BY)
        made: list[int] = []
        _cards(slug, p.get("cards") or [], None, now, made,
               (p.get("lead") or spec["people"][0]["email"]).lower())
        # the baseline is the agreed scope: after the children (as helm.create_project)
        con = board._con()
        try:
            for cid in made:
                c = board.get_card(cid)
                if c and c.get("kind") == "project":
                    con.execute("UPDATE cards SET baseline_at=? WHERE id=?", (now, cid))
            con.commit()
        finally:
            con.close()
        for cid in made:
            helm.refresh(cid)
        helm.suggest(slug, now)
        deck = [helm.rollup(c) for c in made if (board.get_card(c) or {}).get("kind") == "project"]
        report["projects"].append({
            "slug": slug, "cards": len(made),
            "progress": [round((r or {}).get("progress", 0), 2) for r in deck],
            "suggestions": len(helm.list_suggestions([slug]))})
    report["share"] = _seed_share(spec.get("share"), me, now)
    return report


def _seed_share(sh: dict | None, me: str, now: float) -> dict | None:
    """One « Shared with me » item for the visitor: the latest session of the default
    project (the demo has real recorded sessions), else the demo's own URL as a preview."""
    if not sh:
        return None
    import board
    import sharing
    frm = sh["from"].lower()
    con = sharing._con()
    with con:
        con.execute("DELETE FROM shares WHERE principal_kind='user' AND principal=? AND"
                    " created_by LIKE ?", (me, "%" + FICTION_DOMAIN))
    con.close()
    sid = next((s["session_id"] for s in reversed(board.list_sessions())
                if (s.get("project") or "default") == "default" and s.get("session_id")), None)
    who = {"email": frm, "role": "dev"}
    if sid:
        s = sharing.create(who, "session", sid, "user", me, "read",
                           note=sh.get("note", ""), title=sh.get("title", ""))
    else:
        url = (os.environ.get("SOKKAN_PUBLIC_URL") or "https://demo.sokkan.ch").rstrip("/") + "/"
        s = sharing.create(who, "preview", url, "user", me, "read", note=sh.get("note", ""),
                           title=sh.get("title", ""), preview={"label": sh.get("title", "")})
    con = sharing._con()
    with con:
        con.execute("UPDATE shares SET created_at=? WHERE id=?", (now - 2 * 3600, s["id"]))
    con.close()
    return {"id": s["id"], "kind": s["kind"], "from": frm}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("seed", "check"):
        print("usage: demo_captains.py check|seed <captains.json> [--force]", file=sys.stderr)
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if argv[0] == "check":
        print(json.dumps(check(spec)))
        return 0
    if not requested() and "--force" not in argv:
        print("refused: SOKKAN_DEMO_CAPTAINS is not set — this seeds a FICTIONAL organization, "
              "meant for the public demo only (--force to seed anyway)", file=sys.stderr)
        return 1
    print(json.dumps(seed(spec), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
