import os

import pytest


@pytest.fixture()
def mem():
    import memory_search_server as m

    return m


def test_tokens_drop_stopwords_and_short_words(mem):
    t = mem._tokens("Les déploiements de la prod, avec ninjob et le tunnel!")
    assert "ninjob" in t and "tunnel" in t and "prod" in t
    for stop in ("les", "de", "la", "et", "le"):
        assert stop not in t
    assert "a" not in t  # < 3 chars


def _fake_chunks():
    # two notes, one chunk each, orthogonal unit embeddings
    return [
        ("note-deploy", "deployment note", "/x/note-deploy.md",
         "deploy ninjob with the cloudflare tunnel", [1.0, 0.0], 0),
        ("note-jewelry", "jewelry note", "/x/note-jewelry.md",
         "jewelry pipeline gemstones parametric seed", [0.0, 1.0], 0),
    ]


def test_blend_dense_plus_lexical(mem, monkeypatch):
    monkeypatch.setattr(mem, "_load_chunks", _fake_chunks)
    monkeypatch.setattr(mem, "_embed_query", lambda q: [1.0, 0.0])
    res = mem.memory_search("deploy ninjob", top_k=2)
    assert [r["note_name"] for r in res] == ["note-deploy", "note-jewelry"]
    # cosine 1.0 and full keyword overlap → blended score = 1.0
    assert res[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert res[0]["cosine"] == pytest.approx(1.0, abs=1e-6)
    assert "degraded" not in res[0]


def test_degraded_lexical_only_mode(mem, monkeypatch):
    monkeypatch.setattr(mem, "_load_chunks", _fake_chunks)

    def boom(_q):
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(mem, "_embed_query", boom)
    res = mem.memory_search("gemstones jewelry pipeline", top_k=2)
    assert res[0]["note_name"] == "note-jewelry"
    assert res[0]["degraded"]
    assert res[0]["cosine"] is None
    assert res[0]["score"] > res[1]["score"]


def test_degraded_without_keywords_errors(mem, monkeypatch):
    monkeypatch.setattr(mem, "_load_chunks", _fake_chunks)

    def boom(_q):
        raise RuntimeError("down")

    monkeypatch.setattr(mem, "_embed_query", boom)
    # query with only stopwords → no lexical signal to degrade to
    res = mem.memory_search("de la et", top_k=2)
    assert "error" in res[0]


def test_priority_note_gets_boost(mem, monkeypatch):
    # same embedding + same text → identical base score; priority flips the order
    def twins():
        return [
            ("note-plain", "d", "/x/a.md", "shared convention text", [1.0, 0.0], 0),
            ("note-starred", "d", "/x/b.md", "shared convention text", [1.0, 0.0], 1),
        ]

    monkeypatch.setattr(mem, "_load_chunks", twins)
    monkeypatch.setattr(mem, "_embed_query", lambda q: [1.0, 0.0])
    res = mem.memory_search("shared convention", top_k=2)
    assert res[0]["note_name"] == "note-starred"
    assert res[0].get("priority") is True
    assert "priority" not in res[1]
    # boost MULTIPLICATIF : proportionnel au score de base (un match faible
    # n'est plus propulsé par un +flat), borné par construction
    assert res[0]["score"] == pytest.approx(res[1]["score"] * (1 + mem.PRIORITY_BOOST), abs=1e-6)


def test_empty_index_returns_info_not_error(mem, monkeypatch):
    # index vide sur instance neuve = état normal → info non-alarmant, pas une erreur
    monkeypatch.setattr(mem, "_load_chunks", lambda: [])
    res = mem.memory_search("anything")
    assert res[0].get("empty") is True and "info" in res[0]
    assert "error" not in res[0]


# ---- memory_write : le seul chemin d'écriture de la mémoire ------------------

@pytest.fixture()
def memdir(mem, tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path / "memory")
    return tmp_path / "memory"


def test_write_creates_a_note_with_frontmatter(mem, memdir):
    r = mem.memory_write("decision-delete-404",
                         "DELETE on a missing id answers 404 with the error envelope",
                         "Decided after the bugfix card. See [[conventions-api]].")
    assert r["ok"] and r["note"] == "decision-delete-404"
    body = (memdir / "decision-delete-404.md").read_text()
    assert body.startswith("---\nname: decision-delete-404\n")
    assert "description: \"DELETE on a missing id" in body
    assert "priority:" not in body  # non épinglée par défaut
    assert body.rstrip().endswith("[[conventions-api]].")


def test_write_pins_and_types(mem, memdir):
    mem.memory_write("team-workflow", "how we work", "Plan first.",
                     priority=True, type="feedback")
    body = (memdir / "team-workflow.md").read_text()
    assert "priority: high" in body and "type: feedback" in body


def test_description_with_quotes_and_colons_stays_valid_yaml(mem, memdir):
    import yaml
    mem.memory_write("conventions-api", 'the "envelope": {ok, data} — always', "body")
    fm = (memdir / "conventions-api.md").read_text().split("---")[1]
    assert yaml.safe_load(fm)["description"] == 'the "envelope": {ok, data} — always'


def test_write_refuses_bad_names_and_empty_fields(mem, memdir):
    for bad in ("../escape", "Not-Kebab", "a", "sub/dir", ""):
        assert mem.memory_write(bad, "d", "b")["ok"] is False
    assert mem.memory_write("ok-name", "", "b")["ok"] is False
    assert mem.memory_write("ok-name", "d", "  ")["ok"] is False
    assert not memdir.exists() or not list(memdir.glob("*.md"))


def test_write_does_not_clobber_without_overwrite(mem, memdir):
    mem.memory_write("note", "first", "one")
    r = mem.memory_write("note", "second", "two")
    assert r["ok"] is False and "already exists" in r["error"]
    assert "one" in (memdir / "note.md").read_text()
    r = mem.memory_write("note", "second", "two", overwrite=True)
    assert r["ok"] and r["updated"] is True
    assert "two" in (memdir / "note.md").read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignore les permissions du dossier")
def test_write_reports_an_unwritable_directory_instead_of_raising(mem, memdir, monkeypatch):
    memdir.mkdir(parents=True)
    memdir.chmod(0o500)
    try:
        r = mem.memory_write("note", "d", "b")
    finally:
        memdir.chmod(0o700)
    assert r["ok"] is False and "cannot write" in r["error"] and "uid" in r["hint"]


def test_the_name_is_the_wikilink_target(mem, memdir):
    """Écrire `foo` puis y lier depuis une autre note doit résoudre : même slug."""
    mem.memory_write("foo", "d", "b")
    mem.memory_write("bar", "d", "see [[foo]]")
    assert (memdir / "foo.md").exists()
    assert "[[foo]]" in (memdir / "bar.md").read_text()
