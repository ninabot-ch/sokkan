"""Project scope of a search or a recall (SOKKAN 3.2 multi-user).

A note belongs to exactly one project (``notes.project``, migration 0011; DEFAULT_PROJECT for
every note indexed before 3.2). A *scope* is the set of projects a caller may read:

* ``None``  = no scope: every note (standalone CortHeXis, single-project installs, tools);
* a tuple   = only the notes of these projects; an EMPTY tuple = nothing at all.

The rule is fail-closed: a note whose project is unknown (a 2.x index, a store that does not
report it) counts as DEFAULT_PROJECT, never as "visible everywhere"; an invalid project name
in a scope is dropped, it never widens the scope.
"""
from __future__ import annotations

import re
from typing import Iterable

from .contract import DEFAULT_PROJECT

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

def valid_project(slug: str | None) -> bool:
    return bool(slug) and bool(PROJECT_RE.match(str(slug)))

def normalize(projects: Iterable[str] | str | None) -> tuple[str, ...] | None:
    """``None`` stays None (no scope); anything else becomes a sorted tuple of valid slugs
    (possibly empty = nothing visible). A bare string is ONE project, not its letters."""
    if projects is None:
        return None
    if isinstance(projects, str):
        projects = [projects]
    return tuple(sorted({str(p).strip() for p in projects if valid_project(str(p).strip())}))

def from_env(raw: str | None) -> tuple[str, ...] | None:
    """``CORTHEXIS_RECALL_PROJECTS=a,b`` → ("a", "b"); unset or blank → None."""
    if raw is None or not raw.strip():
        return None
    return normalize(p for p in raw.split(","))

def project_of(obj) -> str:
    """Project of a note / hit / dict; unknown = DEFAULT_PROJECT (fail-closed)."""
    p = obj.get("project") if isinstance(obj, dict) else getattr(obj, "project", None)
    return p if valid_project(p) else DEFAULT_PROJECT

def visible(obj, scope: tuple[str, ...] | None) -> bool:
    return scope is None or project_of(obj) in scope

def filter_hits(hits: list, scope: tuple[str, ...] | None) -> list:
    """Defensive post-filter: drops what a store that ignores the scope returned."""
    if scope is None:
        return hits
    return [h for h in hits if visible(h, scope)]
