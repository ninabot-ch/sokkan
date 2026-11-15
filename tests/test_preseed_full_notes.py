"""3.6.7 — a note that matches the task directly reaches the FIRST draft in full.

Seen on the pilot: a quiz asked in SOKKAN Atelier ignored the project's « quiz charter »
(no true/false, every question with its answer). The spawn recall put that note in the brief
as ONE line — description + a 320-character snippet that did not hold the rules — under
« use them when relevant »; it also logged the note as recalled, so the per-turn recall (the
one that says « read the body with memory_get ») never named it again. The local model
never opened the note. Now the Atelier's spawn gives the notes above the recall threshold
in full, before the list, as the team's conventions for this task."""
import os
import tempfile
from types import SimpleNamespace

_TMP = tempfile.mkdtemp()
os.environ.setdefault("SOKKAN_DATA_DIR", _TMP)
os.environ.setdefault("SOKKAN_MEMORY_DIR", f"{_TMP}/memory")
os.environ.setdefault("SOKKAN_UPDATE_CHECK", "0")

import app  # noqa: E402

RULES = ("# Charte des quiz\n\n- Jamais de vrai/faux : quatre choix, une seule bonne réponse.\n"
         "- Chaque question a son corrigé avec une phrase d'explication.\n"
         "- Le quiz est envoyé une semaine avant l'atelier.\n")
HITS = [
    {"note_name": "charte-des-quiz", "description": "Charte des quiz", "score": 0.61,
     "snippet": "# Charte des quiz Nos quiz servent à…"},
    {"note_name": "format-retroplanning", "description": "Format de nos rétroplannings",
     "score": 0.52, "snippet": "Un tableau par semaine"},
    {"note_name": "tarifs", "description": "Tarifs des ateliers", "score": 0.12,
     "snippet": "Exemple fictif."},
]
BODIES = {"charte-des-quiz": RULES, "format-retroplanning": "# Rétroplanning\n\nJ-30, J-15, J-7.",
          "tarifs": "# Tarifs\n\nSECRET-SHOULD-NOT-BE-FULL"}


def _wire(monkeypatch, threshold=0.35):
    monkeypatch.setattr(app.mem, "search_scoped", lambda q, k, scope: [dict(h) for h in HITS])
    monkeypatch.setattr(app, "_session_scope", lambda sid: ("atelier",))
    monkeypatch.setattr(app.store_backend, "enabled", lambda: True)
    monkeypatch.setattr(app.store_backend, "log_spawn_recall", lambda *a, **k: None)
    seen = []

    def get(name, projects=None):
        seen.append((name, projects))
        return SimpleNamespace(body=BODIES[name])
    monkeypatch.setattr(app.store_backend, "memory_get_record", get)
    monkeypatch.setattr(app, "_recall_threshold", lambda: threshold)
    return seen


def test_the_cockpit_spawn_keeps_one_line_per_note(monkeypatch):
    _wire(monkeypatch)
    seed = app._memory_preseed("Prépare un quiz de positionnement", session_id="s1")
    assert "Jamais de vrai/faux" not in seed and "[charte-des-quiz]" in seed


def test_the_atelier_spawn_gives_the_matching_notes_in_full(monkeypatch):
    seen = _wire(monkeypatch)
    seed = app._memory_preseed("Prépare un quiz de positionnement", session_id="s1", full=2)
    # the rule itself, not its first line
    assert "Jamais de vrai/faux" in seed and "corrigé" in seed
    assert "FIRST draft" in seed and seed.index("Jamais de vrai/faux") < seed.index("[tarifs]")
    # below the threshold: one line, never the body; read in the session's scope only
    assert "SECRET-SHOULD-NOT-BE-FULL" not in seed and "[tarifs]" in seed
    assert {n for n, _ in seen} == {"charte-des-quiz", "format-retroplanning"}
    assert all(p == ("atelier",) for _, p in seen)
    # a note given in full is not listed a second time
    assert seed.count("charte-des-quiz") == 2      # opening and closing markers


def test_a_long_note_is_cut_and_nothing_passes_the_threshold(monkeypatch):
    _wire(monkeypatch, threshold=0.9)
    seed = app._memory_preseed("Prépare un quiz", session_id="s1", full=2)
    assert "full text" not in seed and "Jamais de vrai/faux" not in seed
    _wire(monkeypatch)
    monkeypatch.setitem(BODIES, "charte-des-quiz", "x" * 9000)
    seed = app._memory_preseed("Prépare un quiz", session_id="s1", full=1)
    assert "note cut here" in seed and len(seed) < 9000


def test_the_brief_says_to_follow_a_note_given_in_full(tmp_path, monkeypatch):
    import documents
    import simple
    monkeypatch.setattr(documents, "project_root", lambda p: tmp_path)
    b = simple.brief(ask="Un quiz", lang="fr", project="atelier", sid="ab" * 16,
                     recall="=== Project memory (auto-recalled) ===\n--- note [x] (full text) ---")
    assert "follow it in the first draft" in b and "use them when relevant" not in b
