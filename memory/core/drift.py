"""Level-1 check "description != body": a fact corrected in the description only.

The failure it catches: a note's ``description:`` is corrected ("quota alert
resolved") while the paragraph of the body that carries the same fact is left as
it was ("alert: quota at 90 %"). Recall then serves the stale paragraph with
confidence.

No LLM. From the version history (``note_versions``) we take the last version whose
description differs from the current one, and compare, per paragraph of the current
body that is *unchanged* since that version:

* ``stale``   = terms the description dropped (old − new) that the paragraph still has;
* ``missing`` = terms the description gained (new − old) that the paragraph lacks;
* ``anchor``  = terms shared by the paragraph and the old description (same topic).

A paragraph that kept at least half of the dropped terms, shares at least two terms
with the old description and took less than half of the gained ones is reported.
Rewordings are not: terms are accent-folded, plural-folded, and a dropped term that
is a prefix of a gained one ("deploy" / "deployment") counts as kept.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field

_WORD = re.compile(r"[a-z0-9][a-z0-9%]*")
STOPWORDS = frozenset("""
a an and are as at be but by for from has have in into is it its not of on or so that the
their then there these this to was were will with without when where which who why how
au aux avec ce ces cet cette dans de des du elle en est et etc il ils je la le les leur
mais ne nos notre nous on ou par pas plus pour que qui sa se ses son sont sur ta te tes
un une vos votre vous ete etre fait faire tout tous toute toutes comme aussi dont
""".split())


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def terms(text: str) -> set[str]:
    out: set[str] = set()
    for w in _WORD.findall(fold(text or "")):
        has_digit = any(c.isdigit() for c in w)
        if w in STOPWORDS or (len(w) < 3 and not has_digit):
            continue
        if not has_digit and len(w) > 4 and w[-1] in "sx":
            w = w[:-1]
        out.add(w)
    return out


def paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]


def _norm_para(p: str) -> str:
    return " ".join(p.split())


def _same_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and long_.startswith(short)


@dataclass
class DriftFinding:
    note: str
    kind: str = "description-body-drift"
    severity: str = "warning"           # warning | high (figures) | low (fact only added)
    message: str = ""
    paragraph_index: int | None = None
    paragraph: str = ""
    stale_terms: list[str] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    old_description: str = ""
    new_description: str = ""
    description_changed_at: str = ""
    paragraph_unchanged_since: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_versions(note: str, versions: list) -> DriftFinding | None:
    """``versions``: oldest first, the last one is the current state. Each item has
    ``description``, ``body`` and ``seen_at`` (core.contract.SeenRecord)."""
    if len(versions) < 2:
        return None
    cur = versions[-1]
    cur_desc = " ".join((cur.description or "").split())
    j = next((i for i in range(len(versions) - 2, -1, -1)
              if " ".join((versions[i].description or "").split()) != cur_desc), None)
    if j is None:
        return None
    old = versions[j]
    old_t, new_t = terms(old.description), terms(cur.description)
    removed = old_t - new_t
    added = new_t - old_t
    # rewordings: a dropped term with the same stem as a gained one is not dropped
    pairs = {(r, a) for r in removed for a in added if _same_stem(r, a)}
    removed -= {r for r, _ in pairs}
    added -= {a for _, a in pairs}
    if not removed and not added:
        return None

    old_paras = {_norm_para(p) for p in paragraphs(old.body)}
    cur_paras = paragraphs(cur.body)
    changed_at = versions[j + 1].seen_at
    best: tuple[float, int, set, set] | None = None
    for i, p in enumerate(cur_paras):
        if _norm_para(p) not in old_paras:
            continue  # this paragraph was edited after the description changed: fine
        tp = terms(p)
        stale = tp & removed
        anchor = tp & old_t
        took = tp & added
        if not removed or len(anchor) < 2:
            continue
        if len(stale) < max(1, math.ceil(len(removed) / 2)):
            continue
        if added and len(took) >= max(1, math.ceil(len(added) / 2)):
            continue
        score = len(stale) / len(removed) + len(anchor) / (10 * max(1, len(old_t)))
        if best is None or score > best[0]:
            best = (score, i, stale, added - tp)
    if best is not None:
        _s, i, stale, missing = best
        figures = any(any(c.isdigit() for c in t) for t in stale)
        return DriftFinding(
            note=note, severity="high" if figures else "warning",
            message=("description corrected, but paragraph %d of the body still states the "
                     "old fact (%s)" % (i + 1, ", ".join(sorted(stale)))),
            paragraph_index=i, paragraph=cur_paras[i][:400],
            stale_terms=sorted(stale), missing_terms=sorted(missing),
            old_description=old.description, new_description=cur.description,
            description_changed_at=changed_at, paragraph_unchanged_since=old.seen_at,
        )
    # the description gained a fact the body does not carry anywhere, and the body
    # has not moved at all since: weaker signal
    if added and not removed and [_norm_para(p) for p in cur_paras] == \
            [_norm_para(p) for p in paragraphs(old.body)]:
        body_t = terms(cur.body)
        missing = {a for a in added if not any(_same_stem(a, b) for b in body_t)}
        if len(missing) >= max(1, math.ceil(len(added) / 2)):
            return DriftFinding(
                note=note, severity="low",
                message="description gained facts the body does not mention (%s)"
                        % ", ".join(sorted(missing)),
                missing_terms=sorted(missing), old_description=old.description,
                new_description=cur.description, description_changed_at=changed_at,
                paragraph_unchanged_since=old.seen_at,
            )
    return None


def check_note(store, name: str, limit: int = 20) -> DriftFinding | None:
    return check_versions(name, store.note_versions(name, limit))
