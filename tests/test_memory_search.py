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
    assert res[0]["score"] == pytest.approx(res[1]["score"] + mem.PRIORITY_BOOST, abs=1e-6)


def test_empty_index_returns_info_not_error(mem, monkeypatch):
    # index vide sur instance neuve = état normal → info non-alarmant, pas une erreur
    monkeypatch.setattr(mem, "_load_chunks", lambda: [])
    res = mem.memory_search("anything")
    assert res[0].get("empty") is True and "info" in res[0]
    assert "error" not in res[0]
