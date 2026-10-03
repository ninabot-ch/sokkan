"""core.normalize — convention file = name with "_", lossless repairs, dry-run plan."""
import yaml
from memcore_fixtures import T0, messy_corpus, snapshot, write

from core import normalize
from core.notes import FM_RE, parse_note

NOW = T0.timestamp()


def test_dry_run_changes_nothing_and_shows_the_plan(tmp_path):
    messy_corpus(tmp_path)
    before = snapshot(tmp_path)
    plan = normalize.run(tmp_path, dry_run=True, now=NOW)
    assert snapshot(tmp_path) == before
    text = plan.render()
    assert "merge     coffee_roaster_setup.md -> coffee-roaster-setup.md" in text
    assert "rename    coffee-roaster-setup.md -> coffee_roaster_setup.md" in text
    assert "rename    tides.md -> tide_tables.md" in text
    assert "MANUAL    random_thoughts.md" in text
    assert plan.summary()["merge"] == 1
    assert not plan.applied


def test_apply_matches_the_dry_run_plan(tmp_path):
    messy_corpus(tmp_path)
    dry = normalize.run(tmp_path, dry_run=True, now=NOW)
    real = normalize.run(tmp_path, now=NOW)
    assert real.applied and not real.conflicts
    assert [a.render() for a in dry.actions] == [a.render() for a in real.actions]
    assert dry.renamed == real.renamed and dry.merged == real.merged


def test_orphan_is_merged_into_its_namesake_without_loss(tmp_path):
    messy_corpus(tmp_path)
    normalize.run(tmp_path, now=NOW)
    names = sorted(p.name for p in tmp_path.glob("*.md"))
    assert "coffee-roaster-setup.md" not in names        # renamed onto the orphan's name
    note = parse_note((tmp_path / "coffee_roaster_setup.md").read_text(), "coffee_roaster_setup.md")
    assert note.name == "coffee-roaster-setup"
    assert "first crack at 9 minutes" in note.body
    assert "drum now preheats to 200 C" in note.body


def test_broken_yaml_rewritten_with_folded_description(tmp_path):
    messy_corpus(tmp_path)
    normalize.run(tmp_path, now=NOW)
    text = (tmp_path / "till_backup.md").read_text()
    fm = yaml.safe_load(FM_RE.search(text).group(1))
    assert fm["description"] == "Till backup: nightly at 02:00"
    assert "description: >-" in text
    assert str(fm["metadata"]["modified"]).startswith("2026-02-10")


def test_text_above_frontmatter_moved_below(tmp_path):
    messy_corpus(tmp_path)
    before = parse_note((tmp_path / "menu_board.md").read_text(), "menu_board.md").body
    normalize.run(tmp_path, now=NOW)
    text = (tmp_path / "menu_board.md").read_text()
    assert text.startswith("---\n")
    after = parse_note(text, "menu_board.md")
    assert after.body == before and not after.above


def test_names_files_links_and_types(tmp_path):
    messy_corpus(tmp_path)
    normalize.run(tmp_path, now=NOW)
    files = {p.name for p in tmp_path.glob("*.md")}
    assert "tide_tables.md" in files and "tides.md" not in files
    garden = parse_note((tmp_path / "garden.md").read_text(), "garden.md")
    assert garden.name == "garden"
    assert "tide_tables.md" in garden.body                 # file mention rewritten
    backup = (tmp_path / "till_backup.md").read_text()
    assert "[[garden]]" in backup                          # [[Garden Watering Plan]]
    harbor = (tmp_path / "harbor_log.md").read_text()
    assert "[[tide-tables]]" in harbor and "[[coffee-roaster-setup]]" in harbor
    rule = parse_note((tmp_path / "feedback_no_espresso_after_six.md").read_text(), "x.md")
    assert rule.type == "feedback"
    # every note with a frontmatter now follows file = name with "_"
    for p in tmp_path.glob("*.md"):
        n = parse_note(p.read_text(), p.name)
        if n.fm is not None:
            assert p.name == n.name.replace("-", "_") + ".md"


def test_second_pass_is_a_no_op(tmp_path):
    messy_corpus(tmp_path)
    normalize.run(tmp_path, now=NOW)
    before = snapshot(tmp_path)
    plan = normalize.run(tmp_path, now=NOW)
    assert snapshot(tmp_path) == before
    assert plan.changes == []
    assert "random_thoughts.md" in plan.render()           # still reported for a human


def test_grace_period_protects_files_being_written(tmp_path):
    messy_corpus(tmp_path)
    write(tmp_path, "tides.md", (tmp_path / "tides.md").read_text(), when=T0)  # just written
    plan = normalize.run(tmp_path, now=NOW + 60)
    assert (tmp_path / "tides.md").exists()
    assert any(a.kind == "skipped-fresh" and a.path == "tides.md" for a in plan.actions)
    plan = normalize.run(tmp_path, now=NOW + 600)
    assert (tmp_path / "tide_tables.md").exists() and not (tmp_path / "tides.md").exists()


def test_fresh_orphan_is_not_merged_yet(tmp_path):
    messy_corpus(tmp_path)
    write(tmp_path, "coffee_roaster_setup.md", "UPDATE: still typing\n", when=T0)
    normalize.run(tmp_path, now=NOW + 10)
    assert (tmp_path / "coffee_roaster_setup.md").read_text() == "UPDATE: still typing\n"
    assert (tmp_path / "coffee-roaster-setup.md").exists()


def test_duplicate_names_are_left_for_a_human(tmp_path):
    write(tmp_path, "alpha.md", "---\nname: alpha\ndescription: one\nmetadata:\n  type: project\n"
          "---\nfirst\n")
    write(tmp_path, "alpha_copy.md", "---\nname: alpha\ndescription: two\nmetadata:\n"
          "  type: project\n---\nsecond\n")
    plan = normalize.run(tmp_path, now=NOW)
    assert any(a.kind == "manual" and "duplicate" in a.detail for a in plan.actions)
    assert (tmp_path / "alpha.md").read_text().endswith("first\n")
    assert (tmp_path / "alpha_copy.md").exists()


def test_conflicting_write_during_the_pass_is_left_untouched(tmp_path, monkeypatch):
    messy_corpus(tmp_path)
    real_apply = normalize._apply

    def racing_apply(mem_dir, plan, original, mtimes, text, origin):
        write(tmp_path, "tides.md", original["tides.md"] + "late line\n", when=T0)
        return real_apply(mem_dir, plan, original, mtimes, text, origin)

    monkeypatch.setattr(normalize, "_apply", racing_apply)
    plan = normalize.run(tmp_path, now=NOW)
    assert "tides.md" in plan.conflicts
    assert (tmp_path / "tides.md").read_text().endswith("late line\n")
    assert "conflict" in plan.render().lower()


def test_cli_dry_run(tmp_path, capsys):
    messy_corpus(tmp_path)
    assert normalize.main([str(tmp_path), "--dry-run"]) == 0
    assert "plan (dry run)" in capsys.readouterr().out
    assert (tmp_path / "tides.md").exists()


def _note(name: str, desc: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project\n---\n\n{body}\n"


def test_rename_from_the_file_rewrites_links_to_the_old_name_and_its_slug_variants(tmp_path):
    # "Team Calendar" is not kebab-case: the name is derived from the file (teamcalendar).
    # Every link that designated the note — by its old name or a slug variant of it —
    # must follow, or the rename breaks them.
    write(tmp_path, "TeamCalendar.md", _note("Team Calendar", "Shared team calendar",
                                             "Holidays go in the shared calendar."))
    write(tmp_path, "rota.md", _note("rota", "Staff rota", "See [[team-calendar]], "
                                     "[[Team Calendar|the calendar]], [[team_calendar#June]] "
                                     "and [[TeamCalendar]]."))
    dry = normalize.run(tmp_path, dry_run=True, now=NOW)
    plan = normalize.run(tmp_path, now=NOW)
    assert [a.render() for a in dry.actions] == [a.render() for a in plan.actions]
    assert plan.renamed == {"TeamCalendar.md": "teamcalendar.md"}
    rota = (tmp_path / "rota.md").read_text()
    assert "[[teamcalendar]]" in rota
    assert "[[teamcalendar|the calendar]]" in rota
    assert "[[teamcalendar#June]]" in rota
    assert "team-calendar" not in rota and "Team Calendar" not in rota
    # nothing left to repair, every link resolves
    names = {parse_note(p.read_text(), p.name).name for p in tmp_path.glob("*.md")}
    assert names == {"teamcalendar", "rota"}
    assert normalize.run(tmp_path, now=NOW).actions == []


def test_slug_variant_link_is_not_stolen_from_a_real_note(tmp_path):
    # a link that names an EXISTING note stays on it, even when it is also a slug
    # variant of a renamed note's old name
    write(tmp_path, "TeamCalendar.md", _note("Team Calendar", "Team calendar", "Calendar."))
    write(tmp_path, "team_calendar.md", _note("team-calendar", "Another note", "Other."))
    write(tmp_path, "rota.md", _note("rota", "Staff rota", "See [[team-calendar]]."))
    normalize.run(tmp_path, now=NOW)
    assert "[[team-calendar]]" in (tmp_path / "rota.md").read_text()
