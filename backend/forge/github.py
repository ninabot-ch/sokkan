"""GitHub provider — SKELETON (3.2 ships GitLab only).

Defined now so the contract is agreed: the collaborator permission of
``GET /repos/{owner}/{repo}/collaborators/{username}/permission`` (``role_name``:
read | triage | write | maintain | admin) maps to the SOKKAN project role below. Every
network method raises `NotImplementedForge` (tests/test_forge_contract.py)."""
from __future__ import annotations

from forge import Provider

_MAP = {"read": "viewer", "triage": "viewer", "write": "dev", "maintain": "maintainer",
        "admin": "admin"}


class GitHub(Provider):
    name = "github"
    implemented = False
    scopes = ("read:user", "repo")

    @staticmethod
    def role_for(level) -> str | None:
        return _MAP.get(str(level or "").lower())

    def git_credentials(self, token: str) -> tuple[str, str]:
        return ("x-access-token", token)   # documented; pure, safe to define now
