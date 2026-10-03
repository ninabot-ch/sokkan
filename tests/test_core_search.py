"""Unit tests of the pure ranking (memory/core/search.py) — no database needed."""
import datetime
import math

import pytest

from core import search as rk


def test_fold_and_tokens():
    assert rk.fold("parallèles") == "parallele"
    assert rk.fold("bus") == "bus"  # too short to lose its s
    t = rk.tokens("Les déploiements de la prod, avec Ninjob et le tunnel!")
    assert {"deploiement", "prod", "ninjob", "tunnel"} <= t
    assert not {"les", "de", "la", "et", "le"} & t


def test_head_tokens_split_kebab_name():
    assert {"deploy", "runbook", "prod"} <= rk.head_tokens("deploy-runbook", "prod steps")


def test_idf_weights_unknown_word_gets_max_weight():
    w = rk.idf_weights(["common", "rare", "absent"], {"common": 50, "rare": 1}, 100)
    assert w["common"] == pytest.approx(math.log(3))
    assert w["rare"] == pytest.approx(math.log(101))
    assert w["absent"] == pytest.approx(math.log(101))


def test_lexical_score_head_weighs_more_than_body():
    w = {"tunnel": 2.0, "deploy": 1.0}
    in_head = rk.lexical_score(w, frozenset({"tunnel"}), frozenset({"tunnel"}))
    in_body = rk.lexical_score(w, frozenset(), frozenset({"tunnel"}))
    assert in_head == pytest.approx(2 / 3)
    assert in_body == pytest.approx(1 / 3)
    assert rk.lexical_score({}, frozenset(), frozenset()) == 0.0


def _cands():
    return [
        rk.Candidate("a", "deploy runbook", "deploy with the tunnel", 0.90,
                     head=frozenset({"deploy", "runbook"}), body=frozenset({"deploy", "tunnel"})),
        rk.Candidate("b", "gemstone", "jewelry pipeline", 0.95,
                     head=frozenset({"gemstone"}), body=frozenset({"jewelry", "pipeline"})),
        rk.Candidate("c", "misc", "nothing", 0.10, priority=1),
    ]


def test_linear_blend_lexical_lifts_keyword_match():
    w = {"deploy": 1.0}
    hits = rk.rank("deploy", _cands(), weights=w, k=3, lexical_weight=0.3)
    assert [h.note_name for h in hits] == ["a", "b", "c"]
    assert hits[0].score == pytest.approx(0.7 * 0.9 + 0.3 * 1.0)
    assert hits[0].lexical == pytest.approx(1.0)
    # without the lexical signal, the dense order wins
    hits = rk.rank("deploy", _cands(), weights=w, k=3, lexical_weight=0.0)
    assert hits[0].note_name == "b"


def test_precomputed_lexical_is_used():
    hits = rk.rank("x", _cands(), weights={"x": 1.0}, k=3, lexical_weight=0.5,
                   lexical={"c": 1.0, "a": 0.0, "b": 0.0})
    # c: 0.5 * 0.10 + 0.5 * 1.0 = 0.55 > b: 0.5 * 0.95
    assert [h.note_name for h in hits] == ["c", "b", "a"]
    assert hits[0].lexical == 1.0 and hits[1].lexical == 0.0


def test_rrf_fusion():
    hits = rk.rank("deploy", _cands(), weights={"deploy": 1.0}, k=3, fusion="rrf",
                   lexical_weight=0.5, rrf_k=60)
    # a: dense rank 2, lexical rank 1 ; b: dense rank 1, no lexical signal
    assert hits[0].note_name == "a"
    assert hits[0].score == pytest.approx(0.5 / 62 + 0.5 / 61)
    with pytest.raises(ValueError):
        rk.rank("q", _cands(), weights={}, k=1, fusion="nope")


def test_priority_boost_is_multiplicative():
    c = [rk.Candidate("plain", "", "t", 0.5), rk.Candidate("star", "", "t", 0.5, priority=1)]
    hits = rk.rank("t", c, weights={}, k=2, priority_boost=0.08)
    assert hits[0].note_name == "star"
    assert hits[0].score == pytest.approx(0.5 * 0.7 * 1.08)


def test_reranker_reorders_head_only_and_failure_keeps_order():
    seen = {}

    def rr(q, docs):
        seen["docs"] = docs
        return [0.1, 0.9]

    hits = rk.rank("deploy", _cands(), weights={"deploy": 1.0}, k=3, rerank=rr, rerank_top=2)
    assert [h.note_name for h in hits] == ["b", "a", "c"]
    assert hits[0].rerank == 0.9 and hits[2].rerank is None
    assert seen["docs"][0] == "a. deploy runbook. deploy with the tunnel"

    for bad in (lambda q, d: None, lambda q, d: [1.0], lambda q, d: 1 / 0):
        hits = rk.rank("deploy", _cands(), weights={"deploy": 1.0}, k=3, rerank=bad,
                       rerank_top=2)
        assert [h.note_name for h in hits] == ["a", "b", "c"]
        assert all(h.rerank is None for h in hits)


def test_lexical_only_mode():
    c = [rk.Candidate("a", "", "x", None, body=frozenset({"gem"}), chunk_overlap=1.0),
         rk.Candidate("b", "", "y", None, body=frozenset({"gem"}), chunk_overlap=0.0)]
    hits = rk.rank("gem", c, weights={"gem": 1.0}, k=2, degraded="down",
                   rerank=lambda q, d: [9, 9])
    assert [h.note_name for h in hits] == ["a", "b"]
    assert hits[0].score == pytest.approx(0.7 * 0.5 + 0.3)
    assert hits[0].degraded == "down" and hits[0].rerank is None  # no rerank when degraded


def test_age_and_provenance():
    now = datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc)
    assert rk.age("2026-09-01", None, now) == ("2026-09-01", 32, "frontmatter")
    assert rk.age("2026-09-01T10:00:00Z", "migrated-mtime", now)[2] == "migrated-mtime"
    assert rk.age(None, None, now) == (None, None, "inconnue")
    assert rk.age("garbage", "indexed", now) == ("garbage", None, "indexed")


def test_hit_as_dict_shape():
    h = rk.Hit("n", 0.123456, 0.5, 0.25, None, "s" * 400, 3, "indexed", source_path="/x/n.md",
               generation=2)
    d = h.as_dict()
    assert d["score"] == 0.1235 and d["path"] == "n.md" and d["generation"] == 2
    assert len(d["snippet"]) == rk.SNIPPET_MAX - 2 and d["snippet"].endswith("…") and "rerank" not in d
    assert d["age_days"] == 3 and d["date_source"] == "indexed"
