"""Recall bench on the client's own corpus (P0-6): "does the memory work, here?".

A question is something someone asked, with the note(s) that answer it. Three sources:

* ``transcript`` — harvested from the session transcripts: a real prompt followed, in the
  same session, by ``memory_get(note)`` is a (question -> expected note) pair, with no
  annotation work (``harvest_lines`` / ``harvest``);
* ``client`` — written by a person, with the expected note (``add_question``);
* ``generated`` — paraphrased from a note's description by the instance's LLM
  (``generate``). The LLM just read the note, so these questions flatter the model: they
  get a reduced weight (``CORTHEXIS_EVAL_GENERATED_WEIGHT``, default 0.4).

``run`` searches every active question in one index generation with one embedder (= one
profile) and stores, per question, the rank of the first expected note and the top 10.
Metrics (weighted by question): hit@1, hit@5, MRR, nDCG@10 — the method of the 300-question
reference bench. ``compare`` puts two runs side by side on the questions they share (a
profile change is judged on the same questions), ``check_regression`` compares a run with
the previous one of the same generation and profile, and ``findings`` turns a regression
into a finding for the memory review.

Self-contained (CortHeXis memory-core): it talks to the ``Store`` (Postgres, tables of
``migrations/0006_eval.sql``) and to any embedder with ``embed_query`` (and optionally
``rerank`` / ``rerank_policy`` / ``identity``).

    python -m core.eval --dsn postgresql://… run            # bench the active generation
    python -m core.eval harvest ~/.claude/projects/<proj>/  # questions from transcripts
    python -m core.eval add "how do we restore a backup?" backup-runbook
    python -m core.eval report                               # last runs, by source
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import env

SOURCES = ("transcript", "client", "generated")
DEFAULT_WEIGHTS = {"transcript": 1.0, "client": 1.0, "generated": 0.4}
K = 10                     # results kept per question (nDCG@10)
MIN_COMMON = 5             # fewer shared questions than this: no verdict
DEFAULT_MAX_DROP = 0.02    # MRR drop tolerated before a regression is declared
QUESTION_MIN_CHARS = 8
QUESTION_MAX_CHARS = 500   # longer prompts are pasted material, not questions
MAX_EXPECTED = 3           # notes read after one prompt, at most, kept as expected
METRICS = ("h1", "h5", "mrr", "ndcg10")


def weights() -> dict[str, float]:
    w = dict(DEFAULT_WEIGHTS)
    raw = env("EVAL_GENERATED_WEIGHT")
    if raw:
        try:
            w["generated"] = min(1.0, max(0.01, float(raw)))
        except ValueError:
            pass
    return w


def max_drop() -> float:
    try:
        return float(env("EVAL_MAX_DROP") or DEFAULT_MAX_DROP)
    except ValueError:
        return DEFAULT_MAX_DROP


# --------------------------------------------------------------------------- metrics

def metrics(ranked: Sequence[str], relevant: Iterable[str]) -> dict:
    """Same definitions as the reference bench (bench.py): rank of the first relevant note,
    hit@1/3/5, reciprocal rank, nDCG@10 with binary relevance."""
    rel = set(relevant)
    rank = next((i + 1 for i, n in enumerate(ranked) if n in rel), None)
    dcg = sum(1 / math.log2(i + 2) for i, n in enumerate(ranked[:10]) if n in rel)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(rel), 10)))
    return {"rank": rank, "h1": int(bool(rank and rank <= 1)), "h3": int(bool(rank and rank <= 3)),
            "h5": int(bool(rank and rank <= 5)), "mrr": 1 / rank if rank else 0.0,
            "ndcg10": dcg / idcg if idcg else 0.0}


def summarize(rows: Sequence[dict]) -> dict:
    """Weighted means of the per-question metrics (``weight`` key, default 1)."""
    tot = sum(r.get("weight", 1.0) for r in rows)
    out = {"n": len(rows), "weight": round(tot, 3)}
    for k in METRICS:
        out[k] = round(sum(r[k] * r.get("weight", 1.0) for r in rows) / tot, 4) if tot else None
    return out


def _row_metrics(rank: int | None, ndcg10: float, weight: float) -> dict:
    return {"h1": int(bool(rank and rank <= 1)), "h5": int(bool(rank and rank <= 5)),
            "mrr": 1 / rank if rank else 0.0, "ndcg10": ndcg10, "weight": weight}


# --------------------------------------------------------------------------- questions

def fold(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(source: str, question: str) -> str:
    return hashlib.sha1(f"{source}|{fold(question)}".encode()).hexdigest()


@dataclass
class Question:
    id: int
    question: str
    expected: list[str]
    source: str
    weight: float
    status: str = "active"
    author: str | None = None
    session_id: str | None = None
    seen: int = 1
    created_at: str | None = None
    origin: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "question": self.question, "expected": self.expected,
                "source": self.source, "weight": self.weight, "status": self.status,
                "author": self.author, "seen": self.seen, "created_at": self.created_at}


_Q_COLS = ("id, question, expected, source, weight, status, author, session_id, seen, "
           "created_at, origin")


def _q(row) -> Question:
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
    d["weight"] = float(d["weight"])
    d["origin"] = d.get("origin") or {}
    return Question(**d)


def _clean_expected(expected: Iterable[str]) -> list[str]:
    out: list[str] = []
    for e in expected:
        e = (e or "").strip().removesuffix(".md")
        if e and e not in out:
            out.append(e)
    return out


def add_question(store, question: str, expected: Iterable[str], *, source: str = "client",
                 author: str | None = None, session_id: str | None = None,
                 weight: float | None = None, origin: dict | None = None,
                 check_notes: bool = True) -> Question:
    """Create (or merge into) a question. A transcript question seen again in another session
    gains that session and the newly read notes; a client question is replaced."""
    from psycopg.types.json import Jsonb

    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    question = re.sub(r"\s+", " ", question or "").strip()
    if not (QUESTION_MIN_CHARS <= len(question) <= QUESTION_MAX_CHARS * 2):
        raise ValueError("the question must be between 8 and 1000 characters")
    exp = _clean_expected(expected)
    if not exp:
        raise ValueError("at least one expected note is required")
    if check_notes:
        missing = [e for e in exp if store.get_note(e) is None]
        if missing:
            raise ValueError("unknown note(s): " + ", ".join(missing))
    w = weight if weight is not None else weights()[source]
    fp = fingerprint(source, question)
    origin = dict(origin or {})
    with store.pool.connection() as con, con.transaction():
        old = con.execute(f"SELECT {_Q_COLS} FROM eval_questions WHERE fingerprint = %s "
                          "FOR UPDATE", (fp,)).fetchone()
        if old is None:
            if session_id:
                origin.setdefault("sessions", [session_id])
            row = con.execute(
                "INSERT INTO eval_questions (question, expected, source, weight, fingerprint, "
                "author, session_id, origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_Q_COLS}",
                (question, exp[:10], source, w, fp, author, session_id, Jsonb(origin))).fetchone()
            return _q(row)
        old_q = _q(old)
        if source == "transcript":
            sessions = list(old_q.origin.get("sessions") or [])
            if session_id and session_id in sessions:
                return old_q                       # same prompt of the same session: idempotent
            merged = _clean_expected([*old_q.expected, *exp])[:5]
            if session_id:
                sessions = (sessions + [session_id])[-20:]
            row = con.execute(
                "UPDATE eval_questions SET expected = %s, seen = seen + 1, origin = %s, "
                f"updated_at = now() WHERE id = %s RETURNING {_Q_COLS}",
                (merged, Jsonb({**old_q.origin, "sessions": sessions}), old_q.id)).fetchone()
        else:
            row = con.execute(
                "UPDATE eval_questions SET expected = %s, weight = %s, status = 'active', "
                f"author = coalesce(%s, author), updated_at = now() WHERE id = %s "
                f"RETURNING {_Q_COLS}", (exp[:10], w, author, old_q.id)).fetchone()
        return _q(row)


def list_questions(store, *, source: str | None = None, status: str | None = "active",
                   ids: Iterable[int] | None = None, limit: int | None = None) -> list[Question]:
    where, params = [], []
    if source:
        where.append("source = %s")
        params.append(source)
    if status:
        where.append("status = %s")
        params.append(status)
    if ids is not None:
        where.append("id = ANY(%s)")
        params.append(list(ids))
    sql = f"SELECT {_Q_COLS} FROM eval_questions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with store.pool.connection() as con:
        return [_q(r) for r in con.execute(sql, params).fetchall()]


def set_question_status(store, qid: int, status: str) -> bool:
    if status not in ("active", "disabled"):
        raise ValueError(status)
    with store.pool.connection() as con:
        return con.execute("UPDATE eval_questions SET status = %s, updated_at = now() "
                           "WHERE id = %s", (status, int(qid))).rowcount > 0


def delete_question(store, qid: int) -> bool:
    with store.pool.connection() as con:
        return con.execute("DELETE FROM eval_questions WHERE id = %s", (int(qid),)).rowcount > 0


def question_counts(store) -> dict:
    with store.pool.connection() as con:
        rows = con.execute("SELECT source, status, count(*) AS n FROM eval_questions "
                           "GROUP BY source, status").fetchall()
    out = {s: {"active": 0, "disabled": 0} for s in SOURCES}
    for r in rows:
        out[r["source"]][r["status"]] = r["n"]
    out["total_active"] = sum(out[s]["active"] for s in SOURCES)
    return out


# --------------------------------------------------------------------------- harvest

_REMINDER = re.compile(r"<(system-reminder|local-command-stdout|command-[a-z-]+)>.*?</\1>",
                       re.S)
_GET_TOOL = re.compile(r"(^|__)memory_get$")


@dataclass
class Harvested:
    question: str
    expected: list[str]
    session_id: str | None
    at: str | None = None


def _prompt_text(rec: dict) -> str | None:
    """Text of a real user prompt, None for tool results, meta lines and commands."""
    if rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    if "<command-name>" in text or "<local-command-caveat>" in text:
        return ""                                  # a command: closes the previous prompt
    return _REMINDER.sub(" ", text)


def _clean_question(text: str) -> str | None:
    q = re.sub(r"\s+", " ", text or "").strip()
    if not (QUESTION_MIN_CHARS <= len(q) <= QUESTION_MAX_CHARS):
        return None
    if q.startswith(("<", "[Request interrupted", "Caveat:")):
        return None
    if not re.search(r"\w{3}", q):
        return None
    return q


def harvest_lines(lines: Iterable[str], session_id: str | None = None) -> list[Harvested]:
    """(prompt -> notes read with memory_get before the next prompt) pairs of one transcript.

    Only top-level prompts typed by a person count (sub-agent side chains, meta lines,
    commands, tool results are not prompts); a prompt with no ``memory_get`` after it gives
    nothing. Up to ``MAX_EXPECTED`` distinct notes per prompt."""
    out: list[Harvested] = []
    cur: dict | None = None

    def flush():
        if cur and cur["notes"]:
            q = _clean_question(cur["text"])
            if q:
                out.append(Harvested(q, cur["notes"][:MAX_EXPECTED], cur["sid"], cur["at"]))

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        sid = rec.get("sessionId") or session_id
        if rec.get("type") == "user":
            text = _prompt_text(rec)
            if text is None:
                continue
            flush()
            cur = {"text": text, "notes": [], "sid": sid, "at": rec.get("timestamp")} \
                if text else None
        elif rec.get("type") == "assistant" and cur is not None and not rec.get("isSidechain"):
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and _GET_TOOL.search(b.get("name") or "")):
                    inp = b.get("input") or {}
                    name = _clean_expected([inp.get("note_name") or inp.get("name") or ""])
                    if name and name[0] not in cur["notes"]:
                        cur["notes"].append(name[0])
    flush()
    return out


def harvest(store, paths: Iterable[Path | str], *, since: float | None = None,
            log: Callable[[str], None] | None = None) -> dict:
    """Harvest the transcripts (``*.jsonl`` files or directories of them) into the bank.

    Idempotent: a prompt is recorded once per session. ``since`` (epoch) skips files not
    modified after it; by default, the time of the previous harvest (``eval_state``)."""
    from psycopg.types.json import Jsonb

    if since is None:
        since = (get_state(store, "harvest") or {}).get("at")
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files.extend(sorted(p.glob("*.jsonl")))
        elif p.suffix == ".jsonl" and p.is_file():
            files.append(p)
    started = time.time()
    rep = {"files": 0, "pairs": 0, "new": 0, "merged": 0, "unknown_notes": 0}
    known = store.note_names(None)
    for f in files:
        try:
            if since and f.stat().st_mtime < since - 3600:
                continue
            with f.open(encoding="utf-8", errors="replace") as fh:
                pairs = harvest_lines(fh, session_id=f.stem)
        except OSError:
            continue
        rep["files"] += 1
        for h in pairs:
            exp = [e for e in h.expected if e in known]
            if not exp:
                rep["unknown_notes"] += 1
                continue
            rep["pairs"] += 1
            before = _count(store)
            add_question(store, h.question, exp, source="transcript", session_id=h.session_id,
                         origin={"first_at": h.at}, check_notes=False)
            if _count(store) > before:
                rep["new"] += 1
            else:
                rep["merged"] += 1
    with store.pool.connection() as con:
        con.execute("INSERT INTO eval_state (key, value) VALUES ('harvest', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
                    (Jsonb({"at": started, **rep}),))
    if log:
        log(f"harvest: {rep['files']} transcripts, {rep['pairs']} pairs, {rep['new']} new")
    return rep


def _count(store) -> int:
    with store.pool.connection() as con:
        return con.execute("SELECT count(*) AS n FROM eval_questions").fetchone()["n"]


def get_state(store, key: str) -> dict | None:
    with store.pool.connection() as con:
        r = con.execute("SELECT value FROM eval_state WHERE key = %s", (key,)).fetchone()
    return r["value"] if r else None


# --------------------------------------------------------------------------- generated

Paraphraser = Callable[[str, str, str], list[str]]

PARAPHRASE_SYSTEM = (
    "You write test questions for the search engine of a team's knowledge base. "
    "Answer with the questions only, one per line.")
PARAPHRASE_PROMPT = """Note title: {name}
Summary: {description}
Excerpt:
{excerpt}

Write {n} short questions (at most 20 words each) that a member of the team could ask and that
this note answers. Write them in the language of the note. Paraphrase: do not reuse the note
title nor its rare words. One question per line, no numbering, nothing else."""


def parse_questions(text: str, name: str = "") -> list[str]:
    out = []
    for line in (text or "").splitlines():
        q = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"')
        if not (QUESTION_MIN_CHARS <= len(q) <= 200):
            continue
        if name and fold(name) in fold(q):
            continue
        out.append(q)
    return out


def llm_paraphraser(url: str, token: str, model: str, api: str = "openai",
                    n: int = 2, timeout: float = 60) -> Paraphraser:
    """Paraphraser over an OpenAI-compatible (``/chat/completions``) or Anthropic-compatible
    (``/v1/messages``) endpoint — the instance's own LLM."""
    import httpx

    def ask(name: str, description: str, excerpt: str) -> list[str]:
        prompt = PARAPHRASE_PROMPT.format(name=name, description=description or "—",
                                          excerpt=(excerpt or "")[:1500], n=n)
        if api == "openai":
            r = httpx.post(f"{url.rstrip('/')}/chat/completions", timeout=timeout,
                           headers={"Authorization": f"Bearer {token}"},
                           json={"model": model, "max_tokens": 300, "messages": [
                               {"role": "system", "content": PARAPHRASE_SYSTEM},
                               {"role": "user", "content": prompt}]})
            r.raise_for_status()
            m = (r.json().get("choices") or [{}])[0].get("message") or {}
            text = m.get("content") or m.get("reasoning_content") or ""
        else:
            r = httpx.post(f"{url.rstrip('/')}/v1/messages", timeout=timeout,
                           headers={"x-api-key": token, "anthropic-version": "2023-06-01"},
                           json={"model": model, "max_tokens": 300, "system": PARAPHRASE_SYSTEM,
                                 "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", [])
                           if b.get("type") == "text")
        return parse_questions(text, name)[:n]

    return ask


def generate(store, paraphrase: Paraphraser, *, limit: int = 20,
             weight: float | None = None, log: Callable[[str], None] | None = None) -> dict:
    """Generated questions for up to ``limit`` notes that have none yet (notes with a
    description first). An LLM failure on one note skips that note."""
    with store.pool.connection() as con:
        rows = con.execute(
            "SELECT n.name, n.description, left(n.body, 1500) AS body FROM notes n "
            "WHERE NOT EXISTS (SELECT 1 FROM eval_questions q WHERE q.source = 'generated' "
            "AND n.name = ANY(q.expected)) "
            "ORDER BY (n.description = '') ASC, n.updated_at DESC LIMIT %s",
            (int(limit),)).fetchall()
    rep = {"notes": 0, "questions": 0, "errors": 0}
    for r in rows:
        try:
            qs = paraphrase(r["name"], r["description"], r["body"])
        except Exception as e:  # noqa: BLE001 — one note failing never stops the batch
            rep["errors"] += 1
            if log:
                log(f"generate: {r['name']}: {e!r}")
            continue
        rep["notes"] += 1
        for q in qs:
            try:
                add_question(store, q, [r["name"]], source="generated", weight=weight,
                             author="llm", check_notes=False)
                rep["questions"] += 1
            except ValueError:
                pass
    return rep


# --------------------------------------------------------------------------- runs

def _gen_of(store, generation):
    if generation is None:
        g = store.active_generation()
        if g is None:
            raise RuntimeError("no active index generation: nothing to evaluate")
        return g
    gid = generation.id if hasattr(generation, "id") else int(generation)
    g = store.get_generation(gid)
    if g is None:
        raise RuntimeError(f"unknown index generation {gid}")
    return g


def _identity(embedder) -> str | None:
    try:
        return embedder.identity()
    except Exception:  # noqa: BLE001
        return None


def run(store, embedder, *, generation=None, profile: str | None = None,
        trigger: str = "manual", rerank: bool | None = None, k: int = K,
        question_ids: Iterable[int] | None = None,
        progress: Callable[[float], None] | None = None) -> dict:
    """Bench one generation with one embedder. ``rerank`` None = as the profile serves
    searches (inline reranker only for an ``interactive`` rerank policy).

    Returns the stored run: ``{"id", "metrics": {"overall", "by_source", "stale",
    "errors", "search_ms_p50", "search_ms_p95"}, …}``."""
    from psycopg.types.json import Jsonb

    g = _gen_of(store, generation)
    if rerank is None:
        rerank = getattr(embedder, "rerank_policy", "off") == "interactive"
    rr = getattr(embedder, "rerank", None) if rerank else None
    qs = list_questions(store, ids=question_ids) if question_ids is not None \
        else list_questions(store)
    cfg = {"k": k, "rerank": bool(rr), "weights": weights()}
    with store.pool.connection() as con:
        run_id = con.execute(
            "INSERT INTO eval_runs (generation_id, embed_identity, profile, trigger, config) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (g.id, _identity(embedder) or g.embed_identity, profile, trigger,
             Jsonb(cfg))).fetchone()["id"]
    try:
        present = store.note_names(g.id)
        rows, results, stale, errors, lat = [], [], 0, 0, []
        for i, q in enumerate(qs):
            if progress and qs:
                progress(i / len(qs))
            relevant = [e for e in q.expected if e in present]
            if not relevant:
                stale += 1
                continue
            try:
                qv = embedder.embed_query(q.question)
            except Exception:  # noqa: BLE001 — embedding down: the run is not a measure
                errors += 1
                if errors >= 3 and errors > len(rows):
                    raise RuntimeError("the embedding server does not answer")
                continue
            t0 = time.perf_counter()
            hits = store.search(qv, q.question, k, generation=g.id, rerank=rr)
            lat.append((time.perf_counter() - t0) * 1000)
            ranked = [h.note_name for h in hits][:k]
            m = metrics(ranked, relevant)
            rows.append({**_row_metrics(m["rank"], m["ndcg10"], q.weight), "source": q.source})
            results.append((run_id, q.id, q.weight, m["rank"], ranked, relevant, m["ndcg10"]))
        by_source = {s: summarize([r for r in rows if r["source"] == s]) for s in SOURCES}
        out = {"overall": summarize(rows),
               "by_source": {s: v for s, v in by_source.items() if v["n"]},
               "stale": stale, "errors": errors,
               "search_ms_p50": round(statistics.median(lat), 1) if lat else None,
               "search_ms_p95": round(_pct(lat, 95), 1) if lat else None}
        with store.pool.connection() as con, con.transaction():
            if results:
                with con.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO eval_results (run_id, question_id, weight, rank, ranked, "
                        "relevant, ndcg10) VALUES (%s, %s, %s, %s, %s, %s, %s)", results)
            con.execute("UPDATE eval_runs SET status = 'done', finished_at = now(), "
                        "n_questions = %s, metrics = %s WHERE id = %s",
                        (len(rows), Jsonb(out), run_id))
    except Exception as e:
        with store.pool.connection() as con:
            con.execute("UPDATE eval_runs SET status = 'failed', finished_at = now(), error = %s "
                        "WHERE id = %s", (str(e)[:500], run_id))
        raise
    if progress:
        progress(1.0)
    return get_run(store, run_id)


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


_RUN_COLS = ("id, generation_id, embed_identity, profile, trigger, status, started_at, "
             "finished_at, n_questions, metrics, config, regression, error")


def _run(row) -> dict:
    d = dict(row)
    for k in ("started_at", "finished_at"):
        d[k] = d[k].isoformat() if d.get(k) else None
    return d


def get_run(store, run_id: int) -> dict | None:
    with store.pool.connection() as con:
        r = con.execute(f"SELECT {_RUN_COLS} FROM eval_runs WHERE id = %s",
                        (int(run_id),)).fetchone()
    return _run(r) if r else None


def list_runs(store, limit: int = 30, generation: int | None = None) -> list[dict]:
    sql, params = f"SELECT {_RUN_COLS} FROM eval_runs", []
    if generation is not None:
        sql += " WHERE generation_id = %s"
        params.append(int(generation))
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))
    with store.pool.connection() as con:
        return [_run(r) for r in con.execute(sql, params).fetchall()]


def _results(store, run_id: int) -> dict[int, dict]:
    with store.pool.connection() as con:
        return {r["question_id"]: dict(r) for r in con.execute(
            "SELECT question_id, weight, rank, ranked, relevant, ndcg10 FROM eval_results "
            "WHERE run_id = %s", (int(run_id),)).fetchall()}


def compare(store, base_run: int, new_run: int, *, max_drop_: float | None = None,
            examples: int = 8) -> dict:
    """Two runs on the questions they share: metrics of each, deltas, and the questions
    lost (found in the top 5 before, not after) or gained. ``regressed`` = MRR dropped by
    more than ``max_drop_`` on at least ``MIN_COMMON`` shared questions."""
    a, b = _results(store, base_run), _results(store, new_run)
    common = sorted(set(a) & set(b))
    rows_a = [_row_metrics(a[i]["rank"], a[i]["ndcg10"], a[i]["weight"]) for i in common]
    rows_b = [_row_metrics(b[i]["rank"], b[i]["ndcg10"], b[i]["weight"]) for i in common]
    sa, sb = summarize(rows_a), summarize(rows_b)
    delta = {k: (round(sb[k] - sa[k], 4) if sa[k] is not None and sb[k] is not None else None)
             for k in METRICS}
    lost, gained, worse = [], [], []
    miss = K + 1
    for i in common:
        ra, rb = a[i]["rank"], b[i]["rank"]
        if ra and ra <= 5 and not (rb and rb <= 5):
            lost.append(i)
        elif rb and rb <= 5 and not (ra and ra <= 5):
            gained.append(i)
        if (rb or miss) > (ra or miss):
            worse.append(i)
    worse.sort(key=lambda i: (a[i]["rank"] or miss) - (b[i]["rank"] or miss))
    show = lost[:examples] + gained[:examples] + worse[:examples]
    texts = {q.id: q for q in list_questions(store, status=None, ids=show)} if show else {}

    def ex(ids):
        return [{"id": i, "question": texts[i].question if i in texts else None,
                 "expected": a[i]["relevant"], "rank_before": a[i]["rank"],
                 "rank_after": b[i]["rank"], "top_after": b[i]["ranked"][:3]}
                for i in ids[:examples]]

    tol = max_drop() if max_drop_ is None else max_drop_
    enough = len(common) >= MIN_COMMON
    regressed = bool(enough and delta["mrr"] is not None and delta["mrr"] < -tol)
    return {"base_run": base_run, "new_run": new_run, "common": len(common),
            "enough": enough, "max_drop": tol, "base": sa, "new": sb, "delta": delta,
            "regressed": regressed, "lost": len(lost), "gained": len(gained),
            "worse": len(worse), "lost_examples": ex(lost), "gained_examples": ex(gained),
            "worse_examples": ex(worse)}


def check_regression(store, run_id: int, *, max_drop_: float | None = None) -> dict | None:
    """Compare a run with the previous finished run of the same generation and profile.
    Stores and returns the comparison when it is a regression, else None."""
    from psycopg.types.json import Jsonb

    r = get_run(store, run_id)
    if not r or r["status"] != "done":
        return None
    with store.pool.connection() as con:
        prev = con.execute(
            "SELECT id FROM eval_runs WHERE status = 'done' AND id < %s AND generation_id = %s "
            "AND embed_identity IS NOT DISTINCT FROM %s AND profile IS NOT DISTINCT FROM %s "
            "AND (config->>'rerank') IS NOT DISTINCT FROM %s ORDER BY id DESC LIMIT 1",
            (run_id, r["generation_id"], r["embed_identity"], r["profile"],
             str(bool(r["config"].get("rerank"))).lower())).fetchone()
    if not prev:
        return None
    cmp = compare(store, prev["id"], run_id, max_drop_=max_drop_)
    if not cmp["regressed"]:
        return None
    cmp["severity"] = "critical" if cmp["delta"]["mrr"] < -3 * cmp["max_drop"] else "warning"
    with store.pool.connection() as con:
        con.execute("UPDATE eval_runs SET regression = %s WHERE id = %s",
                    (Jsonb(cmp), run_id))
    return cmp


# --------------------------------------------------------------------------- review

def findings(store, *, min_questions: int = 10) -> list[dict]:
    """Findings for the memory review (P0-4): a regression of the last bench run on the
    active generation, or a bench too small to say anything. Shape::

        {"check": "recall-regression" | "recall-bench-small", "severity": "critical" |
         "warning" | "info", "title", "detail", "remedy", "notes": [...], "run_id", "at"}
    """
    out: list[dict] = []
    g = store.active_generation()
    if g is None:
        return out
    with store.pool.connection() as con:
        last = con.execute(
            f"SELECT {_RUN_COLS} FROM eval_runs WHERE status = 'done' AND generation_id = %s "
            "AND trigger <> 'switch' ORDER BY id DESC LIMIT 1", (g.id,)).fetchone()
    if last:
        last = _run(last)
        reg = last.get("regression")
        if reg:
            notes = sorted({n for e in reg.get("lost_examples", []) + reg.get("worse_examples", [])
                            for n in e["expected"]})
            d = reg["delta"]
            out.append({
                "check": "recall-regression", "severity": reg.get("severity", "warning"),
                "title": "Memory recall dropped",
                "detail": (f"MRR {reg['base']['mrr']:.3f} → {reg['new']['mrr']:.3f} "
                           f"({d['mrr']:+.3f}) on {reg['common']} questions; "
                           f"{reg['worse']} question(s) find their note lower, "
                           f"{reg['lost']} no longer in the top 5."),
                "remedy": ("Open the notes that are no longer found: a rename, a merge or a "
                           "rewritten description usually explains it. If the model or profile "
                           "changed, roll back from the Magnitude memory card."),
                "notes": notes, "run_id": last["id"], "at": last["finished_at"]})
    counts = question_counts(store)
    if counts["total_active"] < min_questions:
        out.append({
            "check": "recall-bench-small", "severity": "info",
            "title": "The recall bench has few questions",
            "detail": (f"{counts['total_active']} question(s); at least {min_questions} are "
                       "needed for a profile change to be judged."),
            "remedy": ("Questions are collected from the sessions automatically; you can also "
                       "add your own in CortHeXis → Bench."),
            "notes": [], "run_id": last["id"] if last else None,
            "at": last["finished_at"] if last else None})
    return out


def overview(store, runs: int = 20) -> dict:
    """What the bench panel shows: question counts, the recent runs (newest first), the
    last run of the active generation, the findings."""
    g = store.active_generation()
    recent = list_runs(store, runs)
    last = next((r for r in recent if g and r["generation_id"] == g.id
                 and r["status"] == "done" and r["trigger"] != "switch"), None)
    return {"questions": question_counts(store), "runs": recent, "last": last,
            "active_generation": g.id if g else None,
            "harvest": get_state(store, "harvest"), "findings": findings(store),
            "max_drop": max_drop(), "weights": weights()}


def nightly(store, embedder, *, transcripts: Iterable[Path | str] = (),
            paraphrase: Paraphraser | None = None, generate_limit: int = 20,
            profile: str | None = None, retention_days: int | None = 7,
            log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> dict:
    """The nightly bench: harvest, generate (when an LLM is available), run on the active
    generation, check for a regression, purge the generations past their rollback window."""
    rep: dict = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    paths = list(transcripts)
    if paths:
        rep["harvest"] = harvest(store, paths, log=log)
    if paraphrase is not None and generate_limit > 0:
        rep["generate"] = generate(store, paraphrase, limit=generate_limit, log=log)
    if store.active_generation() is not None and question_counts(store)["total_active"]:
        r = run(store, embedder, profile=profile, trigger="nightly")
        rep["run"] = r["id"]
        rep["metrics"] = r["metrics"]["overall"]
        reg = check_regression(store, r["id"])
        rep["regression"] = bool(reg)
        if reg:
            log(f"recall regression: MRR {reg['delta']['mrr']:+.3f} on {reg['common']} questions")
    if retention_days:
        rep["purged"] = store.purge_retired(datetime.timedelta(days=retention_days))
    return rep


# --------------------------------------------------------------------------- CLI

def _fmt(m: dict | None) -> str:
    if not m or not m.get("n"):
        return "no question"
    return (f"hit@1 {m['h1']:.3f}  hit@5 {m['h5']:.3f}  MRR {m['mrr']:.3f}  "
            f"nDCG@10 {m['ndcg10']:.3f}  ({m['n']} questions)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.eval",
                                 description="recall bench on your own notes")
    ap.add_argument("--dsn", default=None, help="Postgres DSN (default CORTHEXIS_DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="bench the active generation (or --generation)")
    s.add_argument("--generation", type=int)
    s.add_argument("--profile", help="embedder of this profile (default: configured)")
    s.add_argument("--rerank", choices=("auto", "on", "off"), default="auto")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("harvest", help="collect questions from session transcripts")
    s.add_argument("paths", nargs="+")
    s.add_argument("--all", action="store_true", help="re-read every transcript")
    s = sub.add_parser("add", help="add a question and its expected note(s)")
    s.add_argument("question")
    s.add_argument("notes", nargs="+")
    s = sub.add_parser("list", help="list the questions")
    s.add_argument("--source", choices=SOURCES)
    s = sub.add_parser("report", help="last runs")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("compare", help="compare two runs on their shared questions")
    s.add_argument("base", type=int)
    s.add_argument("new", type=int)
    a = ap.parse_args(argv)

    from .store import Store

    store = Store(a.dsn)
    try:
        if a.cmd == "run":
            from . import embed

            e = embed.build(a.profile) if a.profile else embed.get()
            rr = None if a.rerank == "auto" else a.rerank == "on"
            r = run(store, e, generation=a.generation, profile=a.profile or
                    embed.current_profile(), trigger="cli", rerank=rr)
            reg = check_regression(store, r["id"])
            if a.json:
                print(json.dumps({**r, "regression": reg}, indent=2, default=str))
            else:
                print(f"run {r['id']} — generation {r['generation_id']} ({r['embed_identity']})")
                print("  all        " + _fmt(r["metrics"]["overall"]))
                for src, m in r["metrics"]["by_source"].items():
                    print(f"  {src:10} " + _fmt(m))
                if r["metrics"]["stale"]:
                    print(f"  {r['metrics']['stale']} question(s) skipped: expected note gone")
                if reg:
                    print(f"  REGRESSION: MRR {reg['delta']['mrr']:+.3f} vs run {reg['base_run']}")
            return 1 if reg else 0
        if a.cmd == "harvest":
            rep = harvest(store, a.paths, since=0 if a.all else None)
            print(json.dumps(rep))
        elif a.cmd == "add":
            q = add_question(store, a.question, a.notes, source="client", author="cli")
            print(f"question {q.id} added ({', '.join(q.expected)})")
        elif a.cmd == "list":
            for q in list_questions(store, source=a.source):
                print(f"{q.id:5} {q.source:10} {q.weight:.2f} {q.question[:70]!r} → "
                      f"{', '.join(q.expected)}")
        elif a.cmd == "report":
            runs = list_runs(store)
            if a.json:
                print(json.dumps(runs, indent=2, default=str))
            for r in runs:
                flag = "  REGRESSION" if r.get("regression") else ""
                print(f"run {r['id']:4} {r['started_at'][:16]} gen {r['generation_id']} "
                      f"{r['profile'] or '-':8} {r['trigger']:8} "
                      f"{_fmt(r['metrics'].get('overall'))}{flag}")
        elif a.cmd == "compare":
            print(json.dumps(compare(store, a.base, a.new), indent=2, default=str))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
