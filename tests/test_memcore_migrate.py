"""core.migrate — 2.x → 3.0 memory migration: archive first, plan, normalize, dates kept,
generation 1 built, verification, switch; resumable at every step."""
import datetime
import json
import os
import sqlite3
import tarfile

import pytest
from memcore_fixtures import T0, Clock, FakeEmbedder, fm, messy_corpus, snapshot, write

from core import migrate
from core.dates import parse_dt
from core.memstore import InMemoryStore
from core.notes import parse_note

UTC = datetime.timezone.utc
D = datetime.timedelta


def legacy_db(path, mem_dir, model="local:sentence-transformers/x"):
    """A 2.x memory.db as index_memory.py writes it (notes keyed by frontmatter name)."""
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE notes (name TEXT PRIMARY KEY, description TEXT, type TEXT, mtime REAL,"
        " source_path TEXT, priority INTEGER DEFAULT 0);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, note_name TEXT, chunk_idx INTEGER,"
        " body TEXT, embedding TEXT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
    for p in sorted(mem_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        note = parse_note(p.read_text(encoding="utf-8"), p.name)
        name = note.name if note.fm is not None else p.stem
        con.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,0)",
                    (name, note.description, note.type, p.stat().st_mtime, str(p)))
        con.execute("INSERT INTO chunks(note_name, chunk_idx, body, embedding) "
                    "VALUES (?,0,?,'[1]')", (name, note.body))
    con.execute("INSERT INTO meta VALUES ('model', ?)", (model,))
    con.commit()
    con.close()


def corpus(d):
    files = messy_corpus(d)
    # distinct dates: each file is as old as its index in the list
    for i, p in enumerate(sorted(files.values())):
        ts = (T0 - D(days=40 + i)).timestamp()
        os.utime(p, (ts, ts))
    return files


def setup_case(tmp_path, **kw):
    mem, work = tmp_path / "memory", tmp_path / "data" / "memory-migration"
    corpus(mem)
    db = tmp_path / "data" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    legacy_db(db, mem)
    store = kw.pop("store", None) or InMemoryStore()
    mig = migrate.Migration(mem, work, store, kw.pop("embedder", None) or FakeEmbedder(),
                            legacy_db=db, grace=0, clock=kw.pop("clock", Clock()),
                            log=lambda m: None, **kw)
    return mig, mem, work, store, db


def mtimes(d):
    return {p.name: p.stat().st_mtime for p in d.glob("*.md") if p.name != "MEMORY.md"}


def test_full_migration_keeps_every_note_and_every_date(tmp_path):
    mig, mem, work, store, db = setup_case(tmp_path)
    before = snapshot(mem)
    before_mtimes = mtimes(mem)
    db_before = db.read_bytes()
    st = mig.run()
    assert st["status"] == "done", st["message"]
    assert list(st["steps"]) == list(migrate.STEPS)

    # 1. the archive holds the corpus exactly as it was
    arch = work / st["steps"]["archive"]["path"]
    with tarfile.open(arch) as tar:
        got = {m.name[len("memory/"):]: tar.extractfile(m).read().decode()
               for m in tar.getmembers() if m.name.startswith("memory/")}
        manifest = json.load(tar.extractfile("manifest.json"))
    assert got == before
    assert set(manifest["files"]) == set(before)

    # 2. normalize ran (orphan merged, files renamed) and the plan was kept
    assert (work / "normalize-plan.txt").read_text().startswith("normalize plan (dry run)")
    assert "coffee_roaster_setup.md" in json.loads((work / "normalize-plan.json").read_text())[
        "merged"]
    assert not (mem / "coffee-roaster-setup.md").exists()
    assert (mem / "tide_tables.md").exists()

    # 3. the 2.x index is untouched (kept read-only for a rollback)
    assert db.read_bytes() == db_before

    # 4. dates: frontmatter first, else the 2.x mtime; nothing younger than before
    gen = store.active_generation()
    assert gen.id == st["steps"]["switch"]["generation"]
    backup = store.get_note("till-backup")
    assert (backup.modified_source, parse_dt(backup.modified)) == (
        "frontmatter", parse_dt("2026-02-10T08:00:00+00:00"))
    tides = store.get_note("tide-tables")
    assert tides.modified_source == "migrated-mtime"
    assert parse_dt(tides.modified).timestamp() == pytest.approx(before_mtimes["tides.md"])
    roaster = store.get_note("coffee-roaster-setup")   # merged: newest of the two files
    assert parse_dt(roaster.modified).timestamp() == pytest.approx(max(
        before_mtimes["coffee-roaster-setup.md"], before_mtimes["coffee_roaster_setup.md"]))
    started = parse_dt(st["started_at"])
    for name in store.note_names(gen.id):
        assert parse_dt(store.get_note(name).modified) < started, name
    # rewritten files got their old mtime back
    assert (mem / "tide_tables.md").stat().st_mtime == pytest.approx(before_mtimes["tides.md"])

    # 5. same notes: 9 files, 1 orphan merged -> 8 notes; every 2.x note is there
    v = st["steps"]["verify"]
    assert (v["notes_before"], v["merged"], v["notes_after"]) == (9, 1, 8)
    assert v["lost_files"] == [] and v["lost_2x"] == [] and v["younger"] == []
    assert v["dates_wrong"] == [] and v["dates_checked"] == 8
    assert len(store.note_names(gen.id)) == 8


def test_run_twice_is_a_no_op(tmp_path):
    mig, mem, work, store, _db = setup_case(tmp_path)
    assert mig.run()["status"] == "done"
    snap = snapshot(mem)
    again = migrate.Migration(mem, work, store, FakeEmbedder(), log=lambda m: None)
    assert not again.needed()
    assert again.run()["status"] == "done"
    assert snapshot(mem) == snap and len(list(work.glob("corpus-2x-*.tar.gz"))) == 1


class FlakyEmbedder(FakeEmbedder):
    """Fails after `ok` calls, like a model server that goes away mid-import."""

    def __init__(self, ok):
        super().__init__()
        self.ok = ok

    def embed_docs(self, texts):
        if self.calls >= self.ok:
            raise ConnectionError("embedding server unreachable")
        return super().embed_docs(texts)


def test_interrupted_import_resumes_without_making_notes_younger(tmp_path):
    clock = Clock()
    mig, mem, work, store, _db = setup_case(tmp_path, embedder=FlakyEmbedder(ok=4),
                                            clock=clock)
    st = mig.run()
    assert st["status"] == "waiting" and st["step"] == "index"
    assert "unreachable" in st["message"]
    assert store.active_generation() is None          # the 2.x index keeps serving
    clock.advance(hours=3)                            # the server comes back later
    mig2 = migrate.Migration(mem, work, store, FakeEmbedder(), clock=clock,
                             log=lambda m: None, grace=0)
    st = mig2.run()
    assert st["status"] == "done", st["message"]
    v = st["steps"]["verify"]
    assert v["younger"] == [] and v["dates_wrong"] == []
    for name in store.note_names(store.active_generation().id):
        assert store.get_note(name).modified_source in ("frontmatter", "migrated-mtime")


def test_embedder_not_ready_waits_before_touching_the_index(tmp_path):
    def not_ready():
        raise RuntimeError("model download in progress")

    mig, *_ = setup_case(tmp_path, embedder=not_ready)
    st = mig.run()
    assert (st["status"], st["step"]) == ("waiting", "index")
    assert "not ready" in st["message"]


def test_policy_ask_waits_for_approval_and_a_session_edit_keeps_its_date(tmp_path):
    clock = Clock()
    mig, mem, work, store, _db = setup_case(tmp_path, policy="ask", clock=clock)
    st = mig.run()
    assert (st["status"], st["step"]) == ("waiting", "normalize")
    assert (mem / "coffee_roaster_setup.md").exists()       # nothing changed yet
    # a session edits a note normalize does not touch, while the plan waits
    clock.advance(minutes=10)
    edited = mem / "feedback_no_espresso_after_six.md"
    text = edited.read_text().replace("Rule from the owner.", "Rule from the owner (2026).")
    write(mem, edited.name, text, when=clock())
    mig.approve("normalize")
    st = mig.run()
    assert st["status"] == "done", st["message"]
    assert "feedback_no_espresso_after_six.md" not in st["steps"]["dates-files"]["younger"]
    rec = store.get_note("feedback-no-espresso-after-six")
    assert parse_dt(rec.modified) == clock()      # a real edit keeps its (new) date
    assert "feedback-no-espresso-after-six" in st["steps"]["verify"]["edited_meanwhile"]


def test_policy_off_leaves_the_files_and_a_shadowed_note_blocks(tmp_path):
    # without normalize the orphan `coffee_roaster_setup.md` (a separate note in 2.x) has
    # the same name as the real note: 3.0 would serve only one of them -> loss -> blocked
    mig, mem, work, store, _db = setup_case(tmp_path, policy="off")
    snap = snapshot(mem)
    st = mig.run()
    assert st["steps"]["normalize"]["applied"] is False
    assert (st["status"], st["step"]) == ("blocked", "verify")
    failed = st["steps"]["verify-failed"]
    assert failed["lost_files"] == ["coffee_roaster_setup.md"]
    assert failed["dates_wrong"] == [] and failed["younger"] == []
    assert {k: v for k, v in snapshot(mem).items() if k != "MEMORY.md"} == snap
    assert store.active_generation() is None


class LyingStore(InMemoryStore):
    """A store that rejuvenates one note: the date test must catch it."""

    def get_note(self, name):
        n = super().get_note(name)
        if n and name == "harbor-log":
            n.modified = datetime.datetime.now(UTC).isoformat()
        return n


def test_a_rejuvenated_note_blocks_the_switch_until_override(tmp_path):
    mig, mem, work, store, _db = setup_case(tmp_path, store=LyingStore())
    st = mig.run()
    assert (st["status"], st["step"]) == ("blocked", "verify")
    assert "date test failed" in st["message"] and store.active_generation() is None
    assert "harbor-log" in st["steps"]["verify-failed"]["younger"]
    mig.approve("override")
    st = mig.run()
    assert st["status"] == "done" and st["steps"]["verify"]["overridden"]


def test_nothing_to_migrate(tmp_path):
    mig = migrate.Migration(tmp_path / "none", tmp_path / "work", InMemoryStore(),
                            FakeEmbedder(), legacy_db=tmp_path / "memory.db",
                            log=lambda m: None)
    assert not mig.needed()
    assert mig.run()["status"] == "not-needed"


def test_store_already_serving_is_not_migrated_again(tmp_path):
    store = InMemoryStore()
    g = store.create_generation("fake:tiny@8", 8)
    store.activate_generation(g.id)
    mig, *_ = setup_case(tmp_path, store=store)
    assert mig.run()["status"] == "not-needed"


def test_no_space_for_the_archive_changes_nothing(tmp_path, monkeypatch):
    mig, mem, work, store, _db = setup_case(tmp_path)
    snap = snapshot(mem)
    monkeypatch.setattr(migrate.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 1000})())
    st = mig.run()
    assert (st["status"], st["step"]) == ("blocked", "archive")
    assert snapshot(mem) == snap and not list(work.glob("*.tar.gz"))


def test_legacy_index_reader(tmp_path):
    mem = tmp_path / "m"
    mem.mkdir()
    write(mem, "a_note.md", fm("a-note", "A") + "body\n")
    legacy_db(tmp_path / "memory.db", mem, model="remote:http://ml:8001")
    info = migrate.legacy_index(tmp_path / "memory.db")
    assert info["model"] == "remote:http://ml:8001" and info["chunks"] == 1
    assert info["notes"]["a-note"]["file"] == "a_note.md"
    assert migrate.legacy_index(tmp_path / "missing.db") is None


# ------------------------------------------------------------------ against Postgres
PG_DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")


@pytest.mark.skipif(not PG_DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")
def test_migration_into_the_postgres_store(tmp_path):
    import uuid

    psycopg = pytest.importorskip("psycopg")
    from core.store import Store

    name = "sokkan_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(PG_DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    store = Store(PG_DSN.rsplit("/", 1)[0] + "/" + name)
    try:
        clock = Clock()
        mig, mem, work, _st, _db = setup_case(tmp_path, store=store,
                                              embedder=FlakyEmbedder(ok=3), clock=clock)
        assert mig.run()["status"] == "waiting"
        clock.advance(hours=1)
        mig = migrate.Migration(mem, work, store, FakeEmbedder(), clock=clock, grace=0,
                                log=lambda m: None)
        st = mig.run()
        assert st["status"] == "done", st["message"]
        gen = store.active_generation()
        assert gen.id == st["steps"]["switch"]["generation"] and gen.status == "active"
        assert len(store.note_names(gen.id)) == 8
        v = st["steps"]["verify"]
        assert v["dates_wrong"] == [] and v["younger"] == [] and v["lost_files"] == []
        hits = store.search(None, "tide tables deliveries", 3)
        assert hits and hits[0].note_name == "tide-tables"
        assert hits[0].date_source == "migrated-mtime" and hits[0].age_days >= 40
    finally:
        store.close()
        with psycopg.connect(PG_DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")
