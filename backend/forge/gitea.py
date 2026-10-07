"""Gitea / Forgejo provider — SKELETON (3.2 ships GitLab only).

``GET /api/v1/repos/{owner}/{repo}/collaborators/{username}/permission`` answers
``permission``: none | read | write | admin | owner; mapped below. Every network method
raises `NotImplementedForge` (tests/test_forge_contract.py)."""
from __future__ import annotations

from forge import Provider

_MAP = {"read": "viewer", "write": "dev", "admin": "maintainer", "owner": "admin"}


class Gitea(Provider):
    name = "gitea"
    implemented = False
    scopes = ("read:user", "read:repository", "write:repository")

    @staticmethod
    def role_for(level) -> str | None:
        return _MAP.get(str(level or "").lower())
