"""HTTP routes of lot 5 (feature `gitlab`): link / unlink a GitLab account, see it, refresh
one's access, the project repositories (admin), and the git credential endpoint used by
the sessions' credential helper (loopback only, ticket-authenticated).

No route ever returns or logs a token."""
from __future__ import annotations

import ipaddress
import os
import sys
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

import features
import projects
from forge import ForgeError, gitcred, links
from forge import access as faccess

TX_COOKIE = "sokkan_forge_tx"
CRED_PATH = "/api/forge/git-credential"     # auth: ticket + loopback (app._AUTH_FREE)


class RepoIn(BaseModel):
    provider: str = "gitlab"
    base_url: str = ""
    repo_path: str
    default_branch: str = ""


class CredIn(BaseModel):
    ticket: str
    action: str = "get"
    protocol: str = ""
    host: str = ""
    path: str = ""


def _gate() -> None:
    if not features.enabled("gitlab"):
        raise HTTPException(404, "feature `gitlab` is off on this instance")


def is_loopback(request: Request) -> bool:
    """The credential endpoint answers only a process of this host (the session's git),
    never through the reverse proxy (a forwarded request carries X-Forwarded-For)."""
    if any(h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded",
                                          "cf-connecting-ip")):
        return False
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_url() -> str:
    return (os.environ.get("SOKKAN_PUBLIC_URL") or "http://localhost:3009").rstrip("/")


def _audit(user: str, action: str, resource: str, detail: str = "") -> None:
    try:
        import audit
        audit.log(user, action, resource, detail)
    except Exception:  # noqa: BLE001
        pass


def router(current_user: Callable, require: Callable,
           live_user: Callable[[str], str | None],
           close_sessions: Callable[[str], object]) -> APIRouter:
    """``live_user(sid)`` = email of the person of a LIVE session (None = none);
    ``close_sessions(email)`` = coroutine closing that person's live sessions of forge
    projects (unlink = immediate revocation)."""
    r = APIRouter()
    gitcred.LIVE_USER = live_user   # the runner relay and the sandbox socket ask it too

    @r.get("/api/forge/status")
    def status(user: dict = Depends(current_user)) -> dict:
        on = features.enabled("gitlab")
        return {"enabled": on, "providers": [{
            "provider": "gitlab", "base_url": links.gitlab_url(),
            "configured": links.configured("gitlab"), "scopes": list(_gitlab_scopes()),
            "redirect_uri": links.redirect_uri("gitlab")}] if on else []}

    @r.get("/api/forge/links", dependencies=[Depends(_gate)])
    def my_links(user: dict = Depends(current_user)) -> dict:
        email = user["email"]
        mine = [links.public(x) for x in links.list_for(email)]
        fprojects = []
        for p in projects.list_projects():
            if p["access_source"] != "forge":
                continue
            con = projects._con()
            c = con.execute("SELECT role, computed_at, expires_at FROM access_cache WHERE "
                            "email=? AND project=?", (email, p["slug"])).fetchone()
            con.close()
            if c is None and not any(x["state"] == "active" for x in mine):
                continue          # nothing to say about a project they never touched
            fprojects.append({"slug": p["slug"], "name": p["name"],
                              "role": c["role"] if c else None,
                              "computed_at": c["computed_at"] if c else None,
                              "expires_at": c["expires_at"] if c else None,
                              "repos": faccess.list_repos(p["slug"])})
        return {"links": mine, "projects": fprojects}

    @r.get("/api/forge/gitlab/link", dependencies=[Depends(_gate)])
    def link(user: dict = Depends(current_user)):
        if not links.configured("gitlab"):
            raise HTTPException(409, "GitLab is not configured on this instance "
                                     "(SOKKAN_GITLAB_CLIENT_ID, docs/enterprise/OPERATIONS.md)")
        state, url = links.begin(user["email"], "gitlab")
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(TX_COOKIE, state, max_age=links.TX_TTL_S, httponly=True,
                        secure=_public_url().startswith("https://"), samesite="lax",
                        path="/api/forge/")
        return resp

    @r.get("/api/forge/gitlab/callback", dependencies=[Depends(_gate)])
    def callback(request: Request, code: str = "", state: str = "", error: str = "",
                 user: dict = Depends(current_user)):
        back = f"{_public_url()}/"
        if error:
            return RedirectResponse(f"{back}?forge=error&reason={quote(error[:60])}",
                                    status_code=302)
        # the state must be the one THIS browser started, for THIS person (CSRF / mix-up)
        if not state or request.cookies.get(TX_COOKIE) != state:
            raise HTTPException(400, "OAuth state mismatch: start again from Profile")
        tx = links.take(state, user["email"])
        if tx is None:
            raise HTTPException(400, "OAuth state unknown or expired: start again from Profile")
        p = links.provider_for("gitlab", links.gitlab_url())
        try:
            tokens = p.exchange(code, tx["verifier"], links.redirect_uri("gitlab"))
            who = p.whoami(tokens.access_token)
        except ForgeError as e:
            print(f"[forge] link failed for {user['email']}: {e}", file=sys.stderr)
            raise HTTPException(502, f"GitLab refused the link: {e}")
        links.save(user["email"], "gitlab", links.gitlab_url(), tokens, who.user_id, who.username)
        _audit(user["email"], "forge.link", f"gitlab {links.gitlab_url()}",
               f"as {who.username} (id {who.user_id}), scopes {tokens.scopes}")
        try:
            faccess.refresh_all(user["email"])
        except Exception as e:  # noqa: BLE001 — the next request re-reads anyway
            print(f"[forge] access refresh after link failed: {type(e).__name__}", file=sys.stderr)
        resp = RedirectResponse(f"{back}?forge=linked", status_code=302)
        resp.delete_cookie(TX_COOKIE, path="/api/forge/")
        return resp

    @r.delete("/api/forge/links/{provider}", dependencies=[Depends(_gate)])
    async def unlink(provider: str, user: dict = Depends(current_user)) -> dict:
        base = links.gitlab_url() if provider == "gitlab" else ""
        row = links.get(user["email"], provider, base)
        if row is None:
            raise HTTPException(404, "no such linked account")
        tok = links._dec(row.get("token_enc") or "")
        if tok:
            try:
                links.provider_for(provider, base).revoke(tok)   # best effort at the forge
            except ForgeError:
                pass
        links.revoke(user["email"], provider, base, "unlink", by=user["email"])
        links.delete(user["email"], provider, base)
        closed = await close_sessions(user["email"])
        return {"ok": True, "sessions_closed": closed}

    @r.post("/api/forge/refresh", dependencies=[Depends(_gate)])
    def refresh(user: dict = Depends(current_user)) -> dict:
        return {"projects": faccess.refresh_all(user["email"])}

    @r.get("/api/forge/projects/{slug}/protected-branches", dependencies=[Depends(_gate)])
    def protected(slug: str, user: dict = Depends(current_user)) -> dict:
        if projects.effective_role(user, slug) is None:
            raise HTTPException(404, "unknown project")
        return {"branches": faccess.protected_branches(user["email"], slug)}

    # ---- admin: the project's repositories ----------------------------------------------
    @r.get("/api/admin/projects/{slug}/repos")
    def repos(slug: str, _u: dict = Depends(require("admin"))) -> dict:
        return {"repos": faccess.list_repos(slug)}

    @r.post("/api/admin/projects/{slug}/repos")
    def add_repo(slug: str, body: RepoIn, u: dict = Depends(require("admin"))) -> dict:
        if projects.get(slug) is None:
            raise HTTPException(404, "unknown project")
        try:
            out = faccess.add_repo(slug, body.provider, body.base_url, body.repo_path,
                                   body.default_branch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _audit(u["email"], "project.repo.add", slug, f"{body.provider} {body.repo_path}")
        return {"repos": out}

    @r.delete("/api/admin/projects/{slug}/repos")
    def remove_repo(slug: str, body: RepoIn, u: dict = Depends(require("admin"))) -> dict:
        base = body.base_url or (links.gitlab_url() if body.provider == "gitlab" else "")
        out = faccess.remove_repo(slug, body.provider, base, body.repo_path)
        _audit(u["email"], "project.repo.remove", slug, f"{body.provider} {body.repo_path}")
        return {"repos": out}

    # ---- the sessions' credential helper ------------------------------------------------
    @r.post(CRED_PATH)
    def git_credential(body: CredIn, request: Request):
        if not features.enabled("gitlab"):
            return JSONResponse({"reason": "feature gitlab is off"}, status_code=404)
        if not is_loopback(request):
            return JSONResponse({"reason": "loopback only"}, status_code=403)
        return gitcred.answer(body.model_dump(), live_user=live_user)

    return r


def _gitlab_scopes():
    from forge.gitlab import GitLab
    return GitLab.scopes
