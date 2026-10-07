"""GitLab provider — self-hosted or gitlab.com, REST API v4, OAuth 2 with PKCE.

Scopes (minimal, docs/MULTIUSER.md): ``read_user`` (who am I), ``read_api`` (membership
and protected branches), ``read_repository`` + ``write_repository`` (git over HTTPS). Not
``api``: a merge request is opened by ``git push -o merge_request.create``.

Access level of a person on a project = ``GET /projects/:id/members/all/:user_id`` (direct,
inherited from ancestor groups and from groups the project is shared with), mapped:
Guest/Planner/Reporter → viewer, Developer → dev, Maintainer → maintainer, Owner → admin.
"""
from __future__ import annotations

import time
from urllib.parse import quote, urlencode

import httpx

from forge import (ForgeError, ForgeUnauthorized, ForgeUnavailable, Identity, Provider, Repo,
                   Tokens)

API = "/api/v4"


class GitLab(Provider):
    name = "gitlab"
    implemented = True
    scopes = ("read_user", "read_api", "read_repository", "write_repository")

    @staticmethod
    def role_for(level) -> str | None:
        try:
            lv = int(level)
        except (TypeError, ValueError):
            return None
        if lv >= 50:
            return "admin"         # Owner (50); 60 = instance admin in some payloads
        if lv >= 40:
            return "maintainer"
        if lv >= 30:
            return "dev"
        if lv >= 10:
            return "viewer"        # Guest 10, Planner 15, Reporter 20
        return None                # No access 0, Minimal access 5

    # ---- HTTP --------------------------------------------------------------------------
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, verify=self.ca_bundle or True,
                            follow_redirects=False)

    def _call(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with self._client() as c:
                r = c.request(method, self.base_url + path, headers=headers, **kw)
        except httpx.HTTPError as e:
            # the exception text can hold the URL — never a header; keep only its type
            raise ForgeUnavailable(f"gitlab {method} {path.split('?')[0]}: "
                                   f"{type(e).__name__}") from None
        if r.status_code >= 500:
            raise ForgeUnavailable(f"gitlab {method} {path.split('?')[0]}: {r.status_code}")
        return r

    def _token_call(self, data: dict) -> Tokens:
        data = {**data, "client_id": self.client_id}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        r = self._call("POST", "/oauth/token", data=data,
                       headers={"Accept": "application/json"})
        if r.status_code in (400, 401):
            err = ""
            try:
                err = str(r.json().get("error") or "")
            except ValueError:
                pass
            # invalid_grant = refresh token reused/revoked, code expired: the link is dead
            raise ForgeUnauthorized(f"gitlab token endpoint: {r.status_code} {err}".strip())
        if r.status_code != 200:
            raise ForgeError(f"gitlab token endpoint: {r.status_code}")
        j = r.json()
        if not j.get("access_token"):
            raise ForgeError("gitlab token endpoint: no access_token in the answer")
        exp = j.get("expires_in")
        created = float(j.get("created_at") or time.time())
        return Tokens(access_token=j["access_token"], refresh_token=j.get("refresh_token") or "",
                      expires_at=(created + float(exp)) if exp else None,
                      scopes=str(j.get("scope") or " ".join(self.scopes)))

    # ---- OAuth -------------------------------------------------------------------------
    def authorize_url(self, state: str, code_challenge: str, redirect_uri: str) -> str:
        q = urlencode({"client_id": self.client_id, "redirect_uri": redirect_uri,
                       "response_type": "code", "state": state,
                       "scope": " ".join(self.scopes), "code_challenge": code_challenge,
                       "code_challenge_method": "S256"})
        return f"{self.base_url}/oauth/authorize?{q}"

    def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> Tokens:
        return self._token_call({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": redirect_uri, "code_verifier": code_verifier})

    def refresh(self, refresh_token: str, redirect_uri: str) -> Tokens:
        if not refresh_token:
            raise ForgeUnauthorized("gitlab: no refresh token")
        return self._token_call({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                 "redirect_uri": redirect_uri})

    def revoke(self, token: str) -> None:
        data = {"client_id": self.client_id, "token": token}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        self._call("POST", "/oauth/revoke", data=data)   # 200 even for an unknown token

    # ---- reads -------------------------------------------------------------------------
    def _get(self, token: str, path: str) -> httpx.Response:
        r = self._call("GET", API + path, token=token)
        if r.status_code == 401:
            raise ForgeUnauthorized(f"gitlab GET {path.split('?')[0]}: 401")
        return r

    def whoami(self, token: str) -> Identity:
        r = self._get(token, "/user")
        if r.status_code == 403:   # blocked / deactivated user
            raise ForgeUnauthorized("gitlab GET /user: 403 (user blocked?)")
        if r.status_code != 200:
            raise ForgeError(f"gitlab GET /user: {r.status_code}")
        j = r.json()
        return Identity(user_id=str(j["id"]), username=str(j.get("username") or ""),
                        name=str(j.get("name") or ""),
                        extra={"state": j.get("state"), "web_url": j.get("web_url")})

    @staticmethod
    def _pid(repo: Repo) -> str:
        return quote(repo.ref, safe="")

    def access_level(self, token: str, who: Identity, repo: Repo) -> str | None:
        r = self._get(token, f"/projects/{self._pid(repo)}/members/all/{quote(who.user_id)}")
        if r.status_code in (403, 404):   # not a member (or the project is hidden from them)
            return None
        if r.status_code != 200:
            raise ForgeError(f"gitlab members/all: {r.status_code}")
        j = r.json()
        if j.get("state") not in (None, "active"):     # awaiting / blocked membership
            return None
        return self.role_for(j.get("access_level"))

    def protected_branches(self, token: str, repo: Repo) -> list[str]:
        r = self._get(token, f"/projects/{self._pid(repo)}/protected_branches?per_page=100")
        if r.status_code != 200:
            return []
        return [str(b.get("name")) for b in r.json() if b.get("name")]

    def git_credentials(self, token: str) -> tuple[str, str]:
        # GitLab: any username with an OAuth token as the password; `oauth2` is the
        # documented one
        return ("oauth2", token)
