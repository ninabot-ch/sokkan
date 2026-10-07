"""Project scope of a search or a recall (SOKKAN 3.2 multi-user).

A note belongs to exactly one project (``notes.project``, migration 0011; DEFAULT_PROJECT for
every note indexed before 3.2). A *scope* is the set of projects a caller may read:

* ``None``  = no scope: every note (standalone CortHeXis, single-project installs, tools);
* a tuple   = only the notes of these projects; an EMPTY tuple = nothing at all.

The rule is fail-closed: a note whose project is unknown (a 2.x index, a store that does not
report it) counts as DEFAULT_PROJECT, never as "visible everywhere"; an invalid project name
in a scope is dropped, it never widens the scope.

3.4 « classification »: an entry may carry a clearance — ``"radio@3"`` = the notes of
``radio`` up to level 3 (confidential, see ``core.levels``). A bare entry ``"radio"`` reads
up to the DEFAULT level (project): an entry that lost its clearance on the way (an env var,
an old caller) can only see LESS, never more. Two entries for one project keep the lower
clearance. The entries are strings, so a scope crosses process boundaries (MCP server env,
CLI hook env) unchanged.
"""
from __future__ import annotations

import re
from typing import Iterable

from . import levels as _lv
from .contract import DEFAULT_PROJECT

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

def valid_project(slug: str | None) -> bool:
    return bool(slug) and bool(PROJECT_RE.match(str(slug)))

def parse_entry(entry) -> tuple[str, int] | None:
    """``"p"`` → (p, DEFAULT); ``"p@3"`` → (p, 3); invalid → None (dropped)."""
    e = str(entry).strip()
    p, sep, cap = e.partition("@")
    if not valid_project(p):
        return None
    if not sep:
        return p, _lv.DEFAULT
    if not cap.isdigit() or int(cap) > _lv.MAX:
        return None
    return p, int(cap)


def entry(project: str, cap: int | None = None) -> str:
    """Canonical entry: bare at the default clearance, ``p@N`` otherwise."""
    cap = _lv.DEFAULT if cap is None else _lv.clamp(cap)
    return project if cap == _lv.DEFAULT else f"{project}@{cap}"


def normalize(projects: Iterable[str] | str | None) -> tuple[str, ...] | None:
    """``None`` stays None (no scope); anything else becomes a sorted tuple of valid
    entries (possibly empty = nothing visible). A bare string is ONE entry, not its letters.
    One entry per project: the LOWER clearance wins."""
    if projects is None:
        return None
    if isinstance(projects, str):
        projects = [projects]
    caps: dict[str, int] = {}
    for raw in projects:
        pe = parse_entry(raw)
        if pe is None:
            continue
        p, c = pe
        caps[p] = min(c, caps.get(p, c))
    return tuple(sorted(entry(p, c) for p, c in caps.items()))


def caps(scope: tuple[str, ...] | None) -> dict[str, int] | None:
    """{project: clearance} of a scope (None = no scope)."""
    if scope is None:
        return None
    out: dict[str, int] = {}
    for raw in scope:
        pe = parse_entry(raw)
        if pe is not None:
            out[pe[0]] = min(pe[1], out.get(pe[0], pe[1]))
    return out


def projects_of(scope: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """The project slugs of a scope, without their clearances."""
    c = caps(scope)
    return None if c is None else tuple(sorted(c))


def allows(scope: tuple[str, ...] | None, project: str, level: int | None = None) -> bool:
    """May a caller with ``scope`` read a note of ``project`` at ``level``?"""
    if scope is None:
        return True
    c = caps(scope).get(project)
    return c is not None and _lv.clamp(_lv.DEFAULT if level is None else level) <= c


def sql_args(scope: tuple[str, ...]) -> tuple[list[str], list[int]]:
    """Parallel arrays (projects, clearances) for the SQL filters of the store."""
    c = caps(scope) or {}
    ps = sorted(c)
    return ps, [c[p] for p in ps]


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
    return scope is None or allows(scope, project_of(obj), _lv.of(obj))

def filter_hits(hits: list, scope: tuple[str, ...] | None) -> list:
    """Defensive post-filter: drops what a store that ignores the scope returned."""
    if scope is None:
        return hits
    return [h for h in hits if visible(h, scope)]
