"""core.dates — effective date, provenance, link-insensitive hash, rename inheritance."""
import datetime

from memcore_fixtures import T0

from core import dates
from core.memstore import InMemoryStore
from core.notes import parse_note

D = datetime.timedelta


def note(name, body, modified=None, source=None, desc="d"):
    meta = ""
    if modified:
        meta += f"  modified: '{modified}'\n"
    if source:
        meta += f"  modified_source: {source}\n"
    text = f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project\n{meta}---\n{body}\n"
    return parse_note(text, name.replace("-", "_") + ".md")


def test_link_form_does_not_change_the_hash():
    a = "See [[Foo Bar]] and [[foo_bar|the label]] then [[foo-bar#part]]."
    b = "See [[foo-bar]] and [[foo-bar|the label]] then [[foo-bar#part]]."
    assert dates.content_hash(a) == dates.content_hash(b)
    assert dates.content_hash(a) != dates.content_hash(a.replace("See", "Read"))


def test_bootstrap_without_date_is_unknown_not_today():
    st = InMemoryStore()
    eff = dates.effective_modified(st, note("a", "body"), bootstrap=True, now=T0)
    assert (eff.modified, eff.source) == (None, "inconnue")
    # still unknown on the next pass: nothing changed
    eff = dates.effective_modified(st, note("a", "body"), now=T0 + D(days=3))
    assert (eff.modified, eff.source) == (None, "inconnue")


def test_bootstrap_with_declared_date_and_reconstruction_label():
    st = InMemoryStore()
    eff = dates.effective_modified(st, note("a", "x", "2026-01-05T00:00:00+00:00"),
                                   bootstrap=True, now=T0)
    assert (eff.modified[:10], eff.source) == ("2026-01-05", "frontmatter")
    eff = dates.effective_modified(st, note("b", "y", "2026-01-06", "inferred"),
                                   bootstrap=True, now=T0)
    assert eff.source == "inferred"
    eff = dates.effective_modified(st, note("c", "z", "2026-01-07", "unknown"),
                                   bootstrap=True, now=T0)
    assert eff.source == "inconnue"


def test_migration_imports_mtime_with_its_provenance():
    st = InMemoryStore()
    mtime = (T0 - D(days=40)).timestamp()
    eff = dates.effective_modified(st, note("a", "x"), bootstrap=True, now=T0, mtime=mtime,
                                   migrate_mtime=True)
    assert eff.source == "migrated-mtime" and eff.modified.startswith("2026-01-20")
    assert dates.age_label(eff.modified, eff.source, T0) == \
        "updated 40 d ago, reconstructed date (migrated-mtime)"


def test_new_note_after_bootstrap_is_indexed_now():
    st = InMemoryStore()
    dates.effective_modified(st, note("a", "x"), bootstrap=True, now=T0)
    eff = dates.effective_modified(st, note("b", "new"), now=T0 + D(days=1))
    assert eff.source == "indexed" and eff.modified == (T0 + D(days=1)).isoformat()
    assert dates.age_label(eff.modified, eff.source, T0 + D(days=4)) == "updated 3 d ago"


def test_body_change_makes_the_note_younger_never_older():
    st = InMemoryStore()
    dates.effective_modified(st, note("a", "v1", "2026-01-01"), bootstrap=True, now=T0)
    later = T0 + D(days=10)
    eff = dates.effective_modified(st, note("a", "v2", "2026-01-01"), now=later)
    assert (eff.modified, eff.source, eff.changed) == (later.isoformat(), "indexed", True)
    # declared date bumped past the observed change: the frontmatter wins again
    eff = dates.effective_modified(st, note("a", "v2", "2026-03-20"), now=later + D(days=30))
    assert (eff.modified[:10], eff.source) == ("2026-03-20", "frontmatter")


def test_description_only_change_does_not_touch_the_date():
    st = InMemoryStore()
    dates.effective_modified(st, note("a", "same body"), bootstrap=True, now=T0)
    dates.effective_modified(st, note("a", "x2"), now=T0 + D(days=1))
    eff = dates.effective_modified(st, note("a", "x2", desc="other"), now=T0 + D(days=9))
    assert eff.modified == (T0 + D(days=1)).isoformat() and not eff.changed
    assert len(st.note_versions("a")) == 3      # but the version is kept for the drift check


def test_rename_inherits_the_date():
    st = InMemoryStore()
    dates.effective_modified(st, note("old-name", "content"), bootstrap=True, now=T0)
    dates.effective_modified(st, note("old-name", "content v2"), now=T0 + D(days=2))
    eff = dates.effective_modified(st, note("new-name", "content v2"), now=T0 + D(days=20))
    assert eff.inherited_from == "old-name"
    assert eff.modified == (T0 + D(days=2)).isoformat() and not eff.changed


def test_hash_alias_carries_the_date_over_a_mechanical_rewrite():
    st = InMemoryStore()
    dates.effective_modified(st, note("a", "see old_file.md"), bootstrap=True, now=T0)
    dates.effective_modified(st, note("a", "see old_file.md now"), now=T0 + D(days=1))
    new_body = "see new_file.md now"
    alias = {dates.content_hash(new_body): dates.content_hash("see old_file.md now")}
    eff = dates.effective_modified(st, note("a", new_body), now=T0 + D(days=5),
                                   hash_aliases=alias)
    assert eff.modified == (T0 + D(days=1)).isoformat() and not eff.changed
    # without the alias it would have been a (wrong) update
    st2 = InMemoryStore()
    dates.effective_modified(st2, note("a", "see old_file.md"), bootstrap=True, now=T0)
    assert dates.effective_modified(st2, note("a", "see new_file.md"), now=T0 + D(days=5)).changed


def test_legacy_raw_hash_is_accepted():
    st = InMemoryStore()
    from core.contract import SeenRecord
    body = "see [[Foo Bar]]"
    st.record_seen(SeenRecord("a", dates.raw_hash(body), (T0 - D(days=50)).isoformat(),
                              seeded=False, date_source="indexed"))
    eff = dates.effective_modified(st, note("a", body), now=T0)
    assert eff.modified == (T0 - D(days=50)).isoformat()


def test_age_helpers():
    assert dates.age_days(None) is None
    assert dates.age_label(None, "inconnue") == "last update unknown"
    assert dates.parse_dt("2026-01-01").tzinfo is not None
    assert dates.parse_dt("garbage") is None
