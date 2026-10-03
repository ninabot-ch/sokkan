"""When was a note last really updated, and how do we know?

``metadata.modified`` in the frontmatter is trusted as long as it is not behind the
content: as soon as the body changes, the date that change was first seen wins. A
note can therefore get younger on its own, never older by mistake, and the indexer
never rewrites a file to say so.

Two rules keep mechanical rewrites from making the corpus look fresh:

* the content hash ignores the *form* of ``[[links]]`` (``[[Foo Bar]]``,
  ``[[foo_bar]]`` and ``[[foo-bar|label]]`` hash the same), so re-linking a corpus
  does not touch any date;
* a note seen for the first time under a name inherits the date of the same content
  seen under another name (rename), and normalize can declare *hash aliases*
  (old body hash → new body hash) for the files it rewrote itself.

Each date carries its provenance (``core.contract.DATE_SOURCES``).
"""
from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass

from .contract import SeenRecord
from .notes import ParsedNote, slugify

_LINK_HEAD = re.compile(r"\[\[([^\]|#]+)")
_KNOWN_DECLARED_SOURCES = {"frontmatter", "inferred", "transcript", "migrated-mtime"}


def link_canonical(body: str) -> str:
    """Body with every ``[[target`` reduced to its slug (labels kept)."""
    return _LINK_HEAD.sub(lambda m: "[[" + slugify(m.group(1).strip().removesuffix(".md")), body)


def content_hash(body: str) -> str:
    return hashlib.sha256(link_canonical(body.strip()).encode("utf-8")).hexdigest()


def raw_hash(body: str) -> str:
    """Hash of the body as written (what the internal 2.x index stored)."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def parse_dt(raw) -> datetime.datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime.datetime):
        d = raw
    elif isinstance(raw, datetime.date):
        d = datetime.datetime(raw.year, raw.month, raw.day)
    else:
        try:
            d = datetime.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _declared_source(raw: str | None) -> str:
    src = (raw or "").strip().lower()
    if src == "unknown":
        return "inconnue"
    return src if src in _KNOWN_DECLARED_SOURCES else "frontmatter"


@dataclass
class EffectiveDate:
    modified: str | None      # ISO 8601, None when unknown
    source: str               # provenance, see contract.DATE_SOURCES
    content_hash: str
    inherited_from: str | None = None   # previous name when the date came from a rename
    changed: bool = False               # body changed since the last pass


def effective_modified(
    store,
    note: ParsedNote,
    *,
    bootstrap: bool = False,
    now: datetime.datetime | None = None,
    mtime: float | None = None,
    migrate_mtime: bool = False,
    hash_aliases: dict[str, str] | None = None,
) -> EffectiveDate:
    """Effective date of ``note`` + provenance; records the version seen in ``store``.

    ``bootstrap``     the date history is empty (first pass of a new install): content
                      found now is *not* "from today". Without a declared date it is
                      ``inconnue`` — or, with ``migrate_mtime``, the file mtime labelled
                      ``migrated-mtime`` (2.x → 3.0 migration).
    ``hash_aliases``  new hash → old hash for bodies a normalize pass rewrote
                      mechanically in this run.
    """
    now = now or utcnow()
    h = content_hash(note.body)
    equivalent = {h, raw_hash(note.body)}
    if hash_aliases and h in hash_aliases:
        equivalent.add(hash_aliases[h])
    declared = parse_dt(note.modified)

    prev = store.note_seen(note.name)
    inherited = None
    if prev is None:
        prev = store.find_seen(sorted(equivalent))
        if prev is not None:
            inherited = prev.name
    changed = False
    if prev is None:
        if bootstrap:
            if declared:
                first_seen, seeded, seed_src = declared, True, "frontmatter"
            elif migrate_mtime and mtime is not None:
                first_seen = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
                seeded, seed_src = True, "migrated-mtime"
            else:
                first_seen, seeded, seed_src = now, True, "inconnue"
        else:
            # A note unknown once the history exists is a note that was just born:
            # its date is now, and it is measured.
            first_seen, seeded, seed_src = (declared or now), False, "indexed"
            changed = True
    elif prev.content_hash not in equivalent:
        first_seen, seeded, seed_src, changed = now, False, "indexed", True
    else:
        first_seen = parse_dt(prev.first_seen) or now
        seeded, seed_src = prev.seeded, prev.date_source

    store.record_seen(SeenRecord(
        name=note.name, content_hash=h, first_seen=first_seen.isoformat(), seeded=seeded,
        date_source=seed_src, description=note.description, body=note.body,
        seen_at=now.isoformat(),
    ))

    def out(mod, src):
        return EffectiveDate(mod, src, h, inherited, changed)

    if declared and declared > first_seen:
        # The declared date moved after the content was first seen: someone rewrote it
        # on purpose. A reconstruction label still lying in the file is stale.
        return out(declared.isoformat(), "frontmatter")
    if declared and declared == first_seen:
        return out(declared.isoformat(), _declared_source(note.modified_source))
    if seeded:
        if seed_src == "migrated-mtime":
            return out(first_seen.isoformat(), "migrated-mtime")
        # Neither a declared date nor an observed change: say we do not know rather
        # than pass the note off as fresh.
        return out(None, "inconnue")
    return out(first_seen.isoformat(), "indexed")


def age_days(modified: str | None, now: datetime.datetime | None = None) -> int | None:
    d = parse_dt(modified)
    if d is None:
        return None
    return max(0, ((now or utcnow()) - d).days)


def age_label(modified: str | None, source: str, now: datetime.datetime | None = None) -> str:
    """Short prefix for a recalled note: "updated 23 d ago, reconstructed date"."""
    age = age_days(modified, now)
    if age is None:
        return "last update unknown"
    txt = f"updated {age} d ago"
    if source not in ("frontmatter", "indexed", "transcript"):
        txt += f", reconstructed date ({source})"
    return txt
