"""core.indexer — incremental indexing against the Store interface, generations,
chunking, MEMORY.md, and the rule "a mechanical rewrite makes no note younger"."""
import datetime

import pytest
from memcore_fixtures import T0, Clock, FakeEmbedder, fm, messy_corpus, write

from core import indexer as ix
from core.contract import IndexStore, NoteRecord
from core.memstore import InMemoryStore
from core.notes import parse_note

D = datetime.timedelta


def make(tmp_path, clock=None, embedder=None, store=None, **cfg):
    store = store or InMemoryStore()
    cfg.setdefault("normalize", False)
    return ix.Indexer(store, embedder or FakeEmbedder(),
                      ix.IndexConfig(memory_dir=tmp_path, **cfg),
                      clock=clock or Clock(), log=lambda m: None), store


def clean_corpus(d):
    write(d, "oven_schedule.md", fm("oven-schedule", "Bread oven firing schedule") +
          "Fire the oven at 5:00. See [[flour-supplier]].\n")
    write(d, "flour_supplier.md", fm("flour-supplier", "Flour quota alert at 90 % of the contract")
          + "Supplier: the mill.\n\nAlert: the flour quota is at 90 % of the contract.\n")
    write(d, "feedback_wash_hands.md", fm("feedback-wash-hands", "Wash hands before dough",
                                          type_="feedback") + "Always.\n")


def test_memstore_honours_the_protocol():
    assert isinstance(InMemoryStore(), IndexStore)


def test_first_pass_creates_and_activates_a_generation(tmp_path):
    clean_corpus(tmp_path)
    ind, st = make(tmp_path)
    rep = ind.run()
    gen = st.active_generation()
    assert gen.id == rep.generation and rep.activated and rep.built_generation
    assert gen.embed_identity == "fake:tiny@8" and gen.dim == 8
    assert rep.reindexed == 3 and rep.bootstrap
    assert st.note_names(gen.id) == {"oven-schedule", "flour-supplier", "feedback-wash-hands"}
    assert st.links["oven-schedule"] == ["flour-supplier"]
    assert rep.date_sources == {"inconnue": 3}


def test_second_pass_is_incremental(tmp_path):
    clean_corpus(tmp_path)
    clock = Clock()
    ind, st = make(tmp_path, clock=clock)
    ind.run()
    writes, calls = st.writes, ind.embedder.calls
    clock.advance(days=1)
    rep = ind.run()
    assert (rep.reindexed, rep.unchanged, rep.pruned) == (0, 3, 0)
    assert st.writes == writes and ind.embedder.calls == calls
    assert not rep.built_generation and not rep.index_written


def test_edit_reembeds_one_note_and_delete_prunes(tmp_path):
    clean_corpus(tmp_path)
    clock = Clock()
    ind, st = make(tmp_path, clock=clock)
    ind.run()
    clock.advance(days=2)
    write(tmp_path, "oven_schedule.md", fm("oven-schedule", "Bread oven firing schedule") +
          "Fire the oven at 4:30 now.\n")
    (tmp_path / "feedback_wash_hands.md").unlink()
    rep = ind.run()
    assert (rep.reindexed, rep.unchanged, rep.pruned) == (1, 1, 1)
    note = st.get_note("oven-schedule")
    assert (note.modified, note.modified_source) == (clock.t.isoformat(), "indexed")
    assert rep.rejuvenated == ["oven-schedule"]
    assert "feedback-wash-hands" not in st.note_names(rep.generation)


def test_metadata_only_update_does_not_reembed(tmp_path):
    clean_corpus(tmp_path)
    clock = Clock()
    ind, st = make(tmp_path, clock=clock)
    ind.run()
    calls = ind.embedder.calls
    text = (tmp_path / "oven_schedule.md").read_text().replace(
        "  type: project\n", "  type: project\n  modified: '2026-03-05T00:00:00+00:00'\n")
    write(tmp_path, "oven_schedule.md", text)
    clock.advance(days=10)
    rep = ind.run()
    assert rep.metadata_only == 1 and rep.reindexed == 0 and ind.embedder.calls == calls
    assert st.get_note("oven-schedule").modified_source == "frontmatter"


def test_model_change_builds_a_new_generation_and_never_mixes(tmp_path):
    clean_corpus(tmp_path)
    ind, st = make(tmp_path)
    g1 = ind.run().generation
    old_chunks = dict(st.chunks)
    ind2, _ = make(tmp_path, store=st, embedder=FakeEmbedder("fake:bigger@16", 16))
    rep = ind2.run()
    assert rep.generation != g1 and rep.built_generation and rep.activated
    assert rep.reindexed == 3                     # everything re-encoded into the new one
    assert {k: v for k, v in st.chunks.items() if k[0] == g1} == old_chunks
    assert st.active_generation().embed_identity == "fake:bigger@16"
    assert all(len(c.embedding) == 16 for (g, _n), cs in st.chunks.items() if g == rep.generation
               for c in cs)
    assert [g.status for g in st.generations()] == ["retired", "active"]


def test_without_auto_activate_the_building_generation_is_resumed(tmp_path):
    clean_corpus(tmp_path)
    ind, st = make(tmp_path)
    g1 = ind.run().generation
    ind2, _ = make(tmp_path, store=st, embedder=FakeEmbedder("fake:other@8"), auto_activate=False)
    r1 = ind2.run()
    r2 = ind2.run()
    assert r1.generation == r2.generation != g1
    assert not r1.activated and r2.reindexed == 0 and r2.unchanged == 3
    assert st.active_generation().id == g1
    assert len(st.generations()) == 2


def test_rebuild_creates_a_fresh_generation(tmp_path):
    clean_corpus(tmp_path)
    ind, st = make(tmp_path)
    g1 = ind.run().generation
    rep = ind.run(rebuild=True)
    assert rep.generation != g1 and rep.reindexed == 3 and st.active_generation().id == rep.generation


def test_wrong_vector_size_is_refused(tmp_path):
    clean_corpus(tmp_path)

    class Liar(FakeEmbedder):
        def embed_docs(self, texts):
            out = super().embed_docs(texts)
            return out if texts == ["dimension probe"] else [v[:4] for v in out]

    ind, _ = make(tmp_path, embedder=Liar())
    with pytest.raises(RuntimeError, match="wrong size"):
        ind.run()


def test_description_drift_is_reported_by_the_indexer(tmp_path):
    clean_corpus(tmp_path)
    clock = Clock()
    ind, _ = make(tmp_path, clock=clock)
    assert ind.run().drift == []
    text = (tmp_path / "flour_supplier.md").read_text().replace(
        "Flour quota alert at 90 % of the contract", "Flour quota resolved, contract unlimited")
    write(tmp_path, "flour_supplier.md", text)
    clock.advance(days=1)
    rep = ind.run()
    assert [f.note for f in rep.drift] == ["flour-supplier"]
    assert "90" in rep.drift[0].stale_terms


# ---------------------------------------------------------------- the date rule

def test_mechanical_rewrite_makes_no_note_younger(tmp_path):
    """Normalization (renames, merged orphan, YAML, links healed, file mentions
    rewritten) must leave every effective date as it was — except the note that
    really received new content (the merged orphan)."""
    messy_corpus(tmp_path)
    clock = Clock(T0)
    ind, st = make(tmp_path, clock=clock)
    ind.run()                                       # bootstrap on the messy corpus
    clock.advance(days=5)                           # a real edit, measured
    p = tmp_path / "harbor_log.md"
    write(tmp_path, "harbor_log.md", p.read_text() + "Pier 3 is closed.\n", when=clock.t)
    ind.run()
    before = {n.name: (n.modified, n.modified_source) for n in st.notes.values()}
    assert before["harbor-log"] == (clock.t.isoformat(), "indexed")
    assert before["till-backup"][1] == "frontmatter"

    clock.advance(days=30)
    ind.cfg.normalize = True
    rep = ind.run()
    assert rep.normalize.applied and rep.normalize.renamed
    after = {n.name: (n.modified, n.modified_source) for n in st.notes.values()
             if n.name in st.note_names(rep.generation)}
    renamed = {"Garden Watering Plan": "garden"}
    for name, date in before.items():
        new = renamed.get(name, name)
        if new not in after:
            continue                                # the merged orphan is gone
        if new == "coffee-roaster-setup":
            continue                                # really received content: younger, rightly
        assert after[new] == date, f"{name} got younger: {date} -> {after[new]}"
    assert after["coffee-roaster-setup"] == (clock.t.isoformat(), "indexed")
    assert rep.rejuvenated == ["coffee-roaster-setup"]
    assert rep.out_of_convention == ["random_thoughts.md"]


def test_out_of_convention_and_duplicates_are_reported(tmp_path):
    write(tmp_path, "a.md", fm("a", "first") + "one\n")
    write(tmp_path, "a_bis.md", fm("a", "second") + "two\n")
    write(tmp_path, "b-dash.md", fm("b-dash", "dashed") + "three\n")
    ind, st = make(tmp_path)
    rep = ind.run()
    assert sorted(rep.out_of_convention) == ["a_bis.md", "b-dash.md"]
    assert any("already used" in w for w in rep.warnings)
    assert st.get_note("a").description == "first"


def test_dead_paths(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "here.md").write_text("x")
    mem = tmp_path / "mem"
    mem.mkdir()
    write(mem, "paths.md", fm("paths", "paths") +
          "See docs/here.md, docs/gone.md, docs/old.md (removed) and docs/*.md.\n")
    ind, _ = make(mem, dead_path_roots=[repo], dead_path_prefixes=("docs",))
    assert ind.run().dead_paths == {"paths": ["docs/gone.md"]}


# ---------------------------------------------------------------- chunking

def test_long_paragraph_is_cut_on_sentences():
    sent = "The proofing room stays at 26 degrees during the night shift. "
    para = sent * 80                                  # ~5 000 chars, no blank line
    chunks = ix.chunk_body("Intro.\n\n" + para, target=1200)
    assert all(len(c) <= 1200 for c in chunks)
    assert len(chunks) >= 4
    for c in chunks[1:]:
        assert c.startswith("The proofing room") and c.endswith("night shift.")
    assert " ".join(" ".join(chunks).split()) == " ".join(("Intro.\n\n" + para).split())


def test_lines_words_and_unbreakable_tokens():
    lines = "\n".join(f"- item {i} " + "x" * 50 for i in range(60))
    assert all(len(c) <= 500 for c in ix.chunk_body(lines, target=500))
    blob = "a" * 3000
    chunks = ix.chunk_body(blob, target=1000)
    assert all(len(c) <= 1000 for c in chunks) and "".join(chunks) == blob
    assert ix.chunk_body("") == [] and ix.chunk_body("short") == ["short"]


def test_paragraphs_are_packed_up_to_the_target():
    body = "\n\n".join(f"Paragraph {i} " + "y" * 200 for i in range(20))
    chunks = ix.chunk_body(body, target=1200)
    assert all(len(c) <= 1200 for c in chunks) and len(chunks) == 4


# ---------------------------------------------------------------- MEMORY.md

def _rec(name, ntype="project", modified=None, desc=None, prio=0):
    return NoteRecord(name, desc or f"description of {name} " + "z" * 120, ntype, prio,
                      f"/m/{name.replace('-', '_')}.md", modified, "indexed", "body")


def test_memory_index_three_tiers_and_budget():
    now = T0
    notes = [_rec(f"fam{i % 40}-note-{i}", modified=(now - D(days=i % 60)).isoformat())
             for i in range(400)]
    notes += [_rec(f"feedback-rule-{i}", "feedback") for i in range(30)]
    notes.append(_rec("menu-star", prio=1))
    content, stats = ix.render_memory_index(notes, now=now)
    assert stats["fits"] and content.count("\n") <= ix.INDEX_MAX_LINES
    assert len(content.encode()) <= ix.INDEX_MAX_BYTES
    rules_at = content.index("## Working rules (31)")
    families_at = content.index("## All notes by family (431)")
    assert rules_at < families_at and "★ [menu-star]" in content
    for n in notes:                                   # exhaustive overview
        assert n.name in content[families_at:]
    if "## Updated in the last" in content:
        assert rules_at < content.index("## Updated in the last") < families_at


def test_memory_index_small_corpus_keeps_full_hooks_and_links():
    notes = [_rec("feedback-x", "feedback", desc="Rule born on 2026-01-02 (kitchen) — keep "
                  "the knives sharp"), _rec("odd", modified=T0.isoformat())]
    notes[1].source_path = "/m/Odd File.md"
    content, stats = ix.render_memory_index(notes, now=T0)
    assert "— Keep the knives sharp" in content          # dated preamble dropped
    assert "## Updated in the last 21 days (1)" in content
    assert "[odd](Odd File.md)" in content and stats["recent"] == 1


def test_indexer_writes_memory_md_only_when_it_changes(tmp_path):
    clean_corpus(tmp_path)
    ind, _ = make(tmp_path)
    assert ind.run().index_written
    text = (tmp_path / "MEMORY.md").read_text()
    assert "[feedback-wash-hands](feedback_wash_hands.md)" in text
    assert not ind.run().index_written
    assert parse_note(text, "MEMORY.md").fm is None   # never indexed as a note


def test_config_from_env_prefers_corthexis_and_reads_sokkan(monkeypatch, tmp_path):
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", "/legacy")
    monkeypatch.setenv("SOKKAN_RECENT_DAYS", "7")
    monkeypatch.setenv("CORTHEXIS_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("CORTHEXIS_NORMALIZE", "false")
    monkeypatch.setenv("CORTHEXIS_DEAD_PATH_PREFIXES", "docs, scripts")
    cfg = ix.IndexConfig.from_env(auto_activate=False)
    assert cfg.memory_dir == tmp_path and cfg.recent_days == 7 and not cfg.normalize
    assert cfg.dead_path_prefixes == ("docs", "scripts") and not cfg.auto_activate


def test_core_package_is_standalone():
    """memory/core moves to its own package: no import of a SOKKAN module."""
    import ast
    import pathlib
    core = pathlib.Path(ix.__file__).parent
    root = core.parent.parent
    sokkan = {"backend", "magnitude", "memory", "cli", "core"}
    for d in ("backend", "magnitude", "memory", "cli"):
        sokkan |= {p.stem for p in (root / d).glob("*.py")}
    for f in core.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                assert m not in sokkan, f"{f.name} imports SOKKAN module {m}"


def test_memory_index_packs_single_note_families_as_a_last_resort():
    notes = [_rec(f"solo{i}-note") for i in range(150)]
    notes += [_rec(f"feedback-rule-{i}", "feedback", desc="short rule") for i in range(60)]
    content, stats = ix.render_memory_index(notes, now=T0, max_lines=100)
    assert stats["fits"] and content.count("\n") <= 100
    for n in notes:
        assert n.name in content


def test_a_directory_is_one_project_and_never_prunes_another(tmp_path):
    """3.2: every note of the indexed directory gets the configured project; a pass over
    one project's directory leaves the notes of the other projects alone."""
    radio, tv = tmp_path / "radio", tmp_path / "tv"
    radio.mkdir()
    tv.mkdir()
    clean_corpus(radio)
    write(tv, "tv_rundown.md", fm("tv-rundown", "Evening rundown checklist") + "Check.\n")
    ind, st = make(radio, project="radio", write_index=False)
    ind.run()
    ind_tv, _ = make(tv, store=st, project="tv", write_index=False)
    ind_tv.run()
    gen = st.active_generation()
    assert st.get_note("tv-rundown", "tv").project == "tv"
    assert st.get_note("oven-schedule", "radio").project == "radio"
    assert st.get_note("oven-schedule", "tv") is None
    assert st.note_names(gen.id) == {"oven-schedule", "flour-supplier",
                                     "feedback-wash-hands", "tv-rundown"}
    ind.run()                                   # radio again: tv-rundown is not pruned
    assert "tv-rundown" in st.note_names(gen.id)
    assert st.note_names(gen.id, project="tv") == {"tv-rundown"}
    with pytest.raises(ValueError):
        make(radio, store=st, project="Not A Slug", write_index=False)[0].run()


def test_default_project_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("CORTHEXIS_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("SOKKAN_MEMORY_PROJECT", raising=False)
    assert ix.IndexConfig.from_env().project == "default"
    clean_corpus(tmp_path)
    ind, st = make(tmp_path)
    ind.run()
    assert st.get_note("oven-schedule", "default").project == "default"
