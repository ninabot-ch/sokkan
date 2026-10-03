"""Pure ranking logic of the SOKKAN memory search (no database, no network).

The store fetches candidates from Postgres (dense HNSW + lexical GIN); this module turns
them into a ranking. It is a port of the ranking used by the internal reference
implementation that the 300-question bench validated (MRR 0.82 with EmbeddingGemma,
0.88 with a reranker on the top 10):

* **dense**: cosine of the query against each chunk; a note is represented by its best
  chunk (which is also the snippet);
* **lexical**: IDF-weighted overlap between the query keywords and the note, computed on
  two fields — the *head* (name + description: what the author said the note is about)
  and the *body* (name + description + every chunk). Accents and plural ``-s`` are
  folded. A query word found in the head weighs much more than one lost in a huge body;
* **blend**: ``(1 - w) * cosine + w * lexical`` (``w`` depends on the model: ~0.30 for
  EmbeddingGemma, ~0.10 for e5, 0.50 for MiniLM), or reciprocal rank fusion;
* optional multiplicative ``priority`` boost, then an optional **reranker** that reorders
  the top N (``None`` from the reranker = unavailable → hybrid order kept).
"""
from __future__ import annotations

import datetime
import functools
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

DEFAULT_LEXICAL_WEIGHT = 0.30   # EmbeddingGemma (bench 03.10.2026)
# The right dense/lexical blend depends on the embedding MODEL (300-question bench,
# 03.10.2026): a generation that does not carry its own weight gets the one of its model
# family, matched on the embed identity (e.g. "llamacpp:embeddinggemma-300m-q8@768").
MODEL_LEXICAL_WEIGHTS = (
    ("embeddinggemma", 0.30),
    ("e5", 0.10),
    ("minilm", 0.50),
    ("ninjob-ml", 0.50),   # 2.x remote MiniLM service
)


def default_lexical_weight(embed_identity: str | None) -> float:
    ident = (embed_identity or "").lower()
    for key, w in MODEL_LEXICAL_WEIGHTS:
        if key in ident:
            return w
    return DEFAULT_LEXICAL_WEIGHT
DEFAULT_HEAD_SHARE = 0.5        # half head, half body
DEFAULT_RERANK_TOP = 10
DEFAULT_RRF_K = 60
SNIPPET_MAX = 320

STOPWORDS = frozenset({
    "les", "des", "sur", "de", "la", "le", "du", "un", "une", "pour", "dans",
    "avec", "et", "the", "to", "and", "of", "on", "in", "how", "que", "qui",
})

Reranker = Callable[[str, list[str]], "list[float] | None"]


# --------------------------------------------------------------------------- tokens

@functools.lru_cache(maxsize=262_144)
def fold(word: str) -> str:
    """« parallèles » = « paralleles » = « parallele » : accents and plural -s removed."""
    w = unicodedata.normalize("NFKD", word)
    w = "".join(c for c in w if not unicodedata.combining(c))
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


_WORD = re.compile(r"[^\W_]+")  # runs of str.isalnum() characters


def raw_tokens(text: str) -> set[str]:
    """Lower-cased alphanumeric words of 3+ characters, stopwords removed."""
    return {w for w in _WORD.findall((text or "").lower()) if len(w) >= 3 and w not in STOPWORDS}


def tokens(text: str) -> set[str]:
    """Folded keyword set — the unit of every lexical computation."""
    return {t for t in (fold(t) for t in raw_tokens(text)) if t}


def head_tokens(name: str, description: str | None) -> set[str]:
    return tokens((name or "").replace("-", " ") + " " + (description or ""))


def body_tokens(name: str, description: str | None, chunk_bodies: Iterable[str]) -> set[str]:
    return tokens(" ".join([name or "", description or "", *chunk_bodies]))


# --------------------------------------------------------------------------- lexical

def idf_weights(query_tokens: Iterable[str], df: dict[str, int], n_notes: int) -> dict[str, float]:
    """``log(1 + N / df)``; a word absent from the corpus gets ``log(1 + N)``."""
    nn = max(n_notes, 1)
    out = {}
    for t in query_tokens:
        c = df.get(t, 0)
        out[t] = math.log(1 + nn / c) if c > 0 else math.log(1 + nn)
    return out


def lexical_score(weights: dict[str, float], head: set[str] | frozenset[str],
                  body: set[str] | frozenset[str], head_share: float = DEFAULT_HEAD_SHARE) -> float:
    """IDF-weighted share of the query words found in the body and in the head (0..1)."""
    if not weights:
        return 0.0
    tot = sum(weights.values()) or 1.0
    body_lex = sum(w for t, w in weights.items() if t in body) / tot
    head_lex = sum(w for t, w in weights.items() if t in head) / tot
    return (1 - head_share) * body_lex + head_share * head_lex


# --------------------------------------------------------------------------- results

@dataclass
class Hit:
    """One ranked note. The first eight fields are the interface contract of the plan."""
    note_name: str
    score: float
    cosine: float | None
    lexical: float
    rerank: float | None
    snippet: str
    age_days: int | None
    date_source: str
    # additive fields (not in the contract, all optional)
    description: str = ""
    modified: str | None = None
    source_path: str | None = None
    priority: int = 0
    generation: int | None = None
    chunk_idx: int | None = None
    degraded: str | None = None

    def as_dict(self) -> dict:
        """Shape of a ``memory_search`` MCP result (2.x keys + the 3.0 additions)."""
        d = {
            "note_name": self.note_name,
            "modified": self.modified,
            "age_days": self.age_days,
            "date_source": self.date_source,
            "description": self.description,
            "score": round(self.score, 4),
            "cosine": None if self.cosine is None else round(self.cosine, 4),
            "lexical": round(self.lexical, 4),
            "snippet": short_snippet(self.snippet),
            "path": (self.source_path or "").rsplit("/", 1)[-1],
            "generation": self.generation,
        }
        if self.rerank is not None:
            d["rerank"] = round(self.rerank, 4)
        if self.priority:
            d["priority"] = True
        if self.degraded:
            d["degraded"] = self.degraded
        return d


@dataclass
class Candidate:
    """A note eligible for the ranking, with its exact signals."""
    note_name: str
    description: str
    best_chunk: str
    cosine: float | None           # best chunk cosine (None in lexical-only mode)
    head: frozenset[str] = field(default_factory=frozenset)
    body: frozenset[str] = field(default_factory=frozenset)
    chunk_overlap: float = 0.0     # lexical-only mode: best chunk keyword overlap (0..1)
    priority: int = 0
    modified: datetime.datetime | str | None = None
    modified_source: str | None = None
    source_path: str | None = None
    chunk_idx: int | None = None


def short_snippet(text: str) -> str:
    return text if len(text) <= SNIPPET_MAX else text[: SNIPPET_MAX - 3] + "…"


def age(modified, source: str | None, now: datetime.datetime | None = None
        ) -> tuple[str | None, int | None, str]:
    """(modified ISO date, age in days, date source). ``source`` other than ``frontmatter``
    flags a RECONSTRUCTED date (order of magnitude right, exact day not guaranteed)."""
    if not modified:
        return None, None, "inconnue" if not source else source
    if isinstance(modified, datetime.datetime):
        d = modified
    else:
        try:
            d = datetime.datetime.fromisoformat(str(modified).replace("Z", "+00:00"))
        except ValueError:
            return str(modified), None, source or "inconnue"
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return d.date().isoformat(), (now - d).days, source or "frontmatter"


def rerank_text(c: Candidate) -> str:
    """Same text as the one that was embedded (name + description + chunk), as in the bench."""
    return f"{c.note_name}. {c.description or ''}. {c.best_chunk}"


def rank(
    query_text: str,
    candidates: Sequence[Candidate],
    *,
    weights: dict[str, float],
    k: int,
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
    head_share: float = DEFAULT_HEAD_SHARE,
    fusion: str = "linear",
    rrf_k: int = DEFAULT_RRF_K,
    priority_boost: float = 0.0,
    rerank: Reranker | None = None,
    rerank_top: int = DEFAULT_RERANK_TOP,
    generation: int | None = None,
    degraded: str | None = None,
    now: datetime.datetime | None = None,
    lexical: dict[str, float] | None = None,
) -> list[Hit]:
    """Score candidates, sort, rerank the head, return the top ``k`` hits.

    ``lexical`` = lexical scores already computed (by the database) per note name; when
    absent they are computed here from the candidates' ``head`` / ``body`` keyword sets."""
    if fusion not in ("linear", "rrf"):
        raise ValueError(f"unknown fusion {fusion!r} (linear|rrf)")
    dense = all(c.cosine is not None for c in candidates)
    lex = [lexical[c.note_name] if lexical is not None and c.note_name in lexical
           else lexical_score(weights, c.head, c.body, head_share) for c in candidates]

    if not dense:
        # lexical-only: note-wide overlap + chunk concentration (breaks ties between notes
        # that all contain every keyword somewhere)
        base = [0.7 * lx + 0.3 * c.chunk_overlap for lx, c in zip(lex, candidates)]
    elif fusion == "linear":
        base = [(1 - lexical_weight) * c.cosine + lexical_weight * lx  # type: ignore[operator]
                for lx, c in zip(lex, candidates)]
    else:
        base = _rrf([c.cosine for c in candidates], lex, rrf_k, lexical_weight)  # type: ignore[arg-type]

    hits: list[Hit] = []
    for c, lx, s in zip(candidates, lex, base):
        if c.priority and priority_boost:
            s *= 1 + priority_boost
        mod, age_days, src = age(c.modified, c.modified_source, now)
        hits.append(Hit(
            note_name=c.note_name, score=s, cosine=c.cosine, lexical=lx, rerank=None,
            snippet=c.best_chunk, age_days=age_days, date_source=src,
            description=c.description or "", modified=mod, source_path=c.source_path,
            priority=c.priority, generation=generation, chunk_idx=c.chunk_idx, degraded=degraded,
        ))
    hits.sort(key=lambda h: (-h.score, h.note_name))

    if rerank is not None and dense and rerank_top > 1 and hits:
        by_name = {c.note_name: c for c in candidates}
        head = hits[:rerank_top]
        try:
            scores = rerank(query_text, [rerank_text(by_name[h.note_name]) for h in head])
        except Exception:  # noqa: BLE001 — a reranker failure never breaks the search
            scores = None
        if scores is not None and len(scores) == len(head):
            for h, v in zip(head, scores):
                h.rerank = float(v)
            head.sort(key=lambda h: -h.rerank)  # type: ignore[operator]
            hits[: len(head)] = head
    return hits[: max(1, k)]


def _rrf(cos: list[float], lex: list[float], rrf_k: int, lexical_weight: float) -> list[float]:
    """Weighted reciprocal rank fusion; ``lexical_weight`` keeps its meaning (share of the
    lexical list). Notes without any lexical signal get no lexical contribution."""
    def ranks(vals: list[float]) -> list[int]:
        order = sorted(range(len(vals)), key=lambda i: -vals[i])
        r = [0] * len(vals)
        for pos, i in enumerate(order):
            r[i] = pos + 1
        return r

    rd, rl = ranks(cos), ranks(lex)
    return [(1 - lexical_weight) / (rrf_k + rd[i])
            + (lexical_weight / (rrf_k + rl[i]) if lex[i] > 0 else 0.0)
            for i in range(len(cos))]
