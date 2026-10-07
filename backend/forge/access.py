"""Access resolution for projects whose access source is ``forge`` (lot 5).

``resolve(email, project)`` = the person's SOKKAN role in the project, read with THEIR
token on every repository of the project, the LOWEST level wins (decision of 07.10), no
access on one repository = no access. Cached in ``access_cache``: positive 10 min,
negative 2 min (docs/MULTIUSER.md). ``projects.effective_role`` calls it when a forge
project has no fresh row (= the re-read at login, at session start, at the first request
after expiry); ``force=True`` is the "Refresh my access" button and the push 403 path.

Fail-closed: a forge that cannot be reached keeps the cached row until it expires, then
no access. A 401 from the forge revokes the link (tokens erased) and withdraws access.
"""
from __future__ import annotations

import sys
import time

import features
import projects
from forge import ForgeError, ForgeUnauthorized, ForgeUnavailable, NotImplementedForge, Repo
from forge import lowest
from forge import links

POSITIVE_TTL_S = 600
NEGATIVE_TTL_S = 120


def enabled() -> bool:
    try:
        return features.enabled("gitlab")
    except Exception:  # noqa: BLE001
        return False


def repos(project: str) -> list[Repo]:
    con = projects._con()
    rows = con.execute("SELECT * FROM project_repos WHERE project=? ORDER BY provider, base_url,"
                       " repo_path", (project,)).fetchall()
    con.close()
    return [Repo(r["provider"], r["base_url"], r["repo_path"], r["external_id"]) for r in rows]


def _cached(email: str, project: str) -> tuple[bool, str | None]:
    con = projects._con()
    r = con.execute("SELECT role FROM access_cache WHERE email=? AND project=? AND expires_at>?",
                    (email, project, time.time())).fetchone()
    con.close()
    return (True, r["role"]) if r else (False, None)


def _write(email: str, project: str, role: str | None, source: str) -> None:
    now = time.time()
    con = projects._con()
    with con:
        prev = con.execute("SELECT role FROM access_cache WHERE email=? AND project=?",
                           (email, project)).fetchone()
        con.execute("INSERT OR REPLACE INTO access_cache(email, project, role, source, computed_at,"
                    " expires_at) VALUES(?,?,?,?,?,?)",
                    (email, project, role, source, now,
                     now + (POSITIVE_TTL_S if role else NEGATIVE_TTL_S)))
    con.close()
    if prev is not None and prev["role"] != role:
        try:
            import audit
            audit.log(email, "forge.access_changed", project,
                      f"{prev['role'] or 'none'} → {role or 'none'}")
        except Exception:  # noqa: BLE001
            pass


def _repo_role(email: str, repo: Repo) -> str | None:
    p = links.provider_for(repo.provider, repo.base_url)
    if not p.implemented:
        return None
    token = links.access_token(email, repo.provider, repo.base_url)
    if not token:
        return None
    row = links.get(email, repo.provider, repo.base_url) or {}
    from forge import Identity
    who = Identity(user_id=row.get("forge_user_id") or "", username=row.get("forge_username") or "")
    try:
        return p.access_level(token, who, repo)
    except ForgeUnauthorized:
        links.revoke(email, repo.provider, repo.base_url, "unauthorized")
        return None


def compute(email: str, project: str) -> str | None:
    """Read the forge now (raises ForgeUnavailable when it cannot answer)."""
    rs = repos(project)
    if not rs:
        return None
    return lowest([_repo_role(email, r) for r in rs])


def resolve(email: str, project: str, force: bool = False) -> str | None:
    email = (email or "").lower().strip()
    p = projects.get(project)
    if not email or p is None or p["access_source"] != "forge" or not enabled():
        return None
    if not force:
        hit, role = _cached(email, project)
        if hit:
            return role
    try:
        role = compute(email, project)
    except ForgeUnavailable as e:
        print(f"[forge] {project}: forge unreachable for {email} ({e}); cached decision kept "
              "until it expires", file=sys.stderr)
        return _cached(email, project)[1]
    except NotImplementedForge:
        role = None
    except ForgeError as e:
        print(f"[forge] {project}: {e}", file=sys.stderr)
        role = None
    _write(email, project, role, "forge")
    return role


def refresh_all(email: str) -> dict[str, str | None]:
    """Every forge project, re-read now ("Refresh my access")."""
    return {p["slug"]: resolve(email, p["slug"], force=True)
            for p in projects.list_projects() if p["access_source"] == "forge"}


def protected_branches(email: str, project: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in repos(project):
        p = links.provider_for(r.provider, r.base_url)
        try:
            tok = links.access_token(email, r.provider, r.base_url)
            out[r.repo_path] = p.protected_branches(tok, r) if (tok and p.implemented) else []
        except ForgeError:
            out[r.repo_path] = []
    return out


# ---- the project's repositories (admin) -------------------------------------------------
def add_repo(project: str, provider: str, base_url: str, repo_path: str,
             default_branch: str = "") -> list[dict]:
    repo_path = (repo_path or "").strip().strip("/")
    if provider not in projects.FORGE_PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(projects.FORGE_PROVIDERS)}")
    if not repo_path or ".." in repo_path.split("/") or any(c in repo_path for c in " \t\n?#"):
        raise ValueError("repo_path: group/subgroup/repository")
    base = links.norm_url(base_url or (links.gitlab_url() if provider == "gitlab" else ""))
    if not base.startswith(("https://", "http://")):
        raise ValueError("base_url: https://gitlab.example.org")
    con = projects._con()
    with con:
        con.execute("INSERT OR REPLACE INTO project_repos(project, provider, base_url, repo_path,"
                    " external_id, default_branch) VALUES(?,?,?,?,'',?)",
                    (project, provider, base, repo_path, default_branch.strip()))
        # the set of repositories changed: every cached decision of the project is stale
        con.execute("DELETE FROM access_cache WHERE project=?", (project,))
    con.close()
    return list_repos(project)


def remove_repo(project: str, provider: str, base_url: str, repo_path: str) -> list[dict]:
    con = projects._con()
    with con:
        con.execute("DELETE FROM project_repos WHERE project=? AND provider=? AND base_url=? AND"
                    " repo_path=?", (project, provider, links.norm_url(base_url),
                                     repo_path.strip().strip("/")))
        con.execute("DELETE FROM access_cache WHERE project=?", (project,))
    con.close()
    return list_repos(project)


def list_repos(project: str) -> list[dict]:
    return [{"provider": r.provider, "base_url": r.base_url, "repo_path": r.repo_path}
            for r in repos(project)]
