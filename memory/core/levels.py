"""Classification levels of a note (SOKKAN 3.4 « classification »).

Five ordered levels, stored as their rank (``notes.level`` smallint):

    0 public < 1 team < 2 project < 3 confidential < 4 restricted

``project`` (2) is the default: every note indexed before 3.4, every note without a
``classification:`` key. The ids are fixed (API, frontmatter, database); what a customer
sees can be relabelled to follow its own grid (``CORTHEXIS_CLASSIFICATION_LABELS``, or
``SOKKAN_CLASSIFICATION_LABELS``: five comma-separated labels in that order, e.g.
``Public,Internal,Project,Confidential,Secret``).
A frontmatter value may be the id, the customer label or the rank.

Fail-closed: a value that is set but not understood (a typo, a label of another grid) is
``restricted``, never ``public``.
"""
from __future__ import annotations

from .config import env

IDS = ("public", "team", "project", "confidential", "restricted")
DEFAULT = 2                       # project
MAX = len(IDS) - 1


def labels() -> tuple[str, ...]:
    raw = (env("CLASSIFICATION_LABELS") or "").split(",")
    lab = tuple(x.strip() for x in raw)
    return lab if len(lab) == len(IDS) and all(lab) else tuple(i.capitalize() for i in IDS)


def label(rank: int | None) -> str:
    return labels()[clamp(rank)]


def ident(rank: int | None) -> str:
    return IDS[clamp(rank)]


def clamp(rank) -> int:
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return DEFAULT
    return min(max(r, 0), MAX)


def parse(value) -> int | None:
    """Rank of a frontmatter / API value; None = not set; unknown = restricted."""
    if value is None:
        return None
    if isinstance(value, bool):
        return MAX
    if isinstance(value, int):
        return value if 0 <= value <= MAX else MAX
    v = str(value).strip().lower()
    if not v:
        return None
    if v.isdigit():
        n = int(v)
        return n if 0 <= n <= MAX else MAX
    if v in IDS:
        return IDS.index(v)
    low = [x.lower() for x in labels()]
    if v in low:
        return low.index(v)
    return MAX


def of(obj) -> int:
    """Level of a note / hit / dict; unknown = the default (a store that does not report
    levels predates 3.4: every note it holds is at the default level)."""
    v = obj.get("level") if isinstance(obj, dict) else getattr(obj, "level", None)
    if v is None:
        return DEFAULT
    r = parse(v)
    return DEFAULT if r is None else r


def highest(levels) -> int:
    """The derived inherits the highest level of its sources (default when no source)."""
    ls = [clamp(x) for x in levels if x is not None]
    return max(ls) if ls else DEFAULT


def scale() -> list[dict]:
    return [{"rank": i, "id": IDS[i], "label": labels()[i]} for i in range(len(IDS))]
