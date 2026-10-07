"""forge — SOKKAN 3.2 lot 5: rights and pushes from the person's own forge account.

One interface, `Provider`, for every forge (docs/MULTIUSER.md, "Sources of rights"):

* ``authorize_url(state, code_challenge, redirect_uri)`` — the OAuth 2 consent page (PKCE);
* ``exchange(code, code_verifier, redirect_uri)`` → `Tokens` — the person's tokens;
* ``refresh(refresh_token, redirect_uri)`` → `Tokens`;
* ``revoke(token)`` — best effort, at unlink;
* ``whoami(token)`` → `Identity`;
* ``access_level(token, identity, repo)`` → SOKKAN project role or None — the person's level
  on ONE repository, inherited group memberships included;
* ``protected_branches(token, repo)`` → branch names (UI hints only: the push decides);
* ``git_credentials(token)`` → (username, password) for git over HTTPS.

GitLab (self-hosted and gitlab.com, API v4) is implemented (`forge.gitlab`). GitHub and
Gitea/Forgejo are skeletons: their level → role mapping is defined (pure, tested), every
network method raises `NotImplementedForge` with a clear message. Bitbucket and Azure
DevOps are not even skeletons yet (docs/MULTIUSER.md, "Out of 3.2").

Tokens never leave the API process except as git credentials handed to the person's own
session through the credential helper (`forge.gitcred`). Nothing here logs a token: errors
carry the HTTP status and the endpoint, never a header or a body.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

# SOKKAN project roles, increasing (= projects.PROJECT_ROLES; repeated to keep this
# package importable without the projects DB)
ROLES = ("viewer", "dev", "maintainer", "admin")


class ForgeError(Exception):
    """A forge call failed. The message never contains a token."""


class ForgeUnavailable(ForgeError):
    """Network error, timeout, 5xx: the forge could not answer. Keep the cached decision
    until it expires, then no access (fail-closed). Never a revocation."""


class ForgeUnauthorized(ForgeError):
    """401 / invalid_grant: the person's token is no longer valid (revoked at the forge,
    user blocked, refresh token reused…). The link is revoked and access withdrawn."""


class NotImplementedForge(ForgeError, NotImplementedError):
    """This provider's network side is not implemented yet (skeleton)."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str = ""
    expires_at: float | None = None      # epoch seconds; None = does not expire
    scopes: str = ""

    def __repr__(self) -> str:           # never print a token, even in a traceback
        return f"Tokens(expires_at={self.expires_at!r}, scopes={self.scopes!r}, <redacted>)"


@dataclass
class Identity:
    user_id: str
    username: str
    name: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Repo:
    provider: str
    base_url: str
    repo_path: str                         # group/subgroup/repo
    external_id: str = ""                  # numeric id when known

    @property
    def ref(self) -> str:
        return self.external_id or self.repo_path


def lowest(roles: list[str | None]) -> str | None:
    """Decision of 07.10: a project with several repositories gets the LOWEST role over
    them; no access on one of them = no access to the project."""
    if not roles or any(r not in ROLES for r in roles):
        return None
    return min(roles, key=ROLES.index)  # type: ignore[arg-type]


class Provider:
    """Base class: every method a provider must offer. A skeleton keeps these."""
    name = "?"
    implemented = False
    # scopes asked at consent (minimal)
    scopes: tuple[str, ...] = ()

    def __init__(self, base_url: str, client_id: str = "", client_secret: str = "",
                 ca_bundle: str = "", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.ca_bundle = ca_bundle
        self.timeout = timeout

    def _todo(self, what: str):
        raise NotImplementedForge(
            f"{self.name}: {what} is not implemented yet — only GitLab ships in 3.2 "
            "(docs/MULTIUSER.md, forge abstraction)")

    # ---- OAuth -------------------------------------------------------------------------
    def authorize_url(self, state: str, code_challenge: str, redirect_uri: str) -> str:
        self._todo("authorize_url")

    def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> Tokens:
        self._todo("exchange")

    def refresh(self, refresh_token: str, redirect_uri: str) -> Tokens:
        self._todo("refresh")

    def revoke(self, token: str) -> None:
        self._todo("revoke")

    # ---- reads with the person's token -------------------------------------------------
    def whoami(self, token: str) -> Identity:
        self._todo("whoami")

    def access_level(self, token: str, who: Identity, repo: Repo) -> str | None:
        self._todo("access_level")

    def protected_branches(self, token: str, repo: Repo) -> list[str]:
        self._todo("protected_branches")

    def git_credentials(self, token: str) -> tuple[str, str]:
        self._todo("git_credentials")

    # ---- pure: the forge's own level → SOKKAN project role ------------------------------
    @staticmethod
    def role_for(level) -> str | None:
        raise NotImplementedError


PROVIDER_METHODS = ("authorize_url", "exchange", "refresh", "revoke", "whoami",
                    "access_level", "protected_branches", "git_credentials", "role_for")


def provider_class(name: str) -> type[Provider]:
    from forge import gitea, github, gitlab
    classes = {"gitlab": gitlab.GitLab, "github": github.GitHub, "gitea": gitea.Gitea,
               "forgejo": gitea.Gitea}
    if name not in classes:
        raise NotImplementedForge(f"no forge provider {name!r}")
    return classes[name]


def revoke_link(row: dict) -> bool:
    """Revoke a ``forge_links`` row's grant AT THE FORGE (best effort), for the revocation of
    a person (lot 6, backend/revocation.py). The local erasure stays the caller's job: this
    only tells the forge, so a token copied before the revocation stops working there too.
    True when the forge accepted the call; never raises for a forge that cannot be reached
    or a provider that does not implement it."""
    from forge import links
    provider = row.get("provider") or ""
    tok = links._dec(row.get("token_enc") or "") or links._dec(row.get("refresh_enc") or "")
    if not tok:
        return False
    try:
        links.provider_for(provider, row.get("base_url") or "").revoke(tok)
        return True
    except (ForgeError, NotImplementedError, OSError) as e:
        print(f"[forge] revoke at {provider} failed: {type(e).__name__}", file=sys.stderr)
        return False
