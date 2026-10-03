"""SOKKAN 3.0 memory store — Postgres + pgvector (P0-2).

The store holds the index of the markdown notes (the files stay the source of truth) and
answers the two-stage search: hybrid candidates from Postgres (dense HNSW + lexical GIN),
exact re-scoring of those candidates, then an optional reranker (see ``search.py`` for the
ranking itself and ``schema.sql`` for the layout, including why chunks are partitioned by
index generation).

Interface contract (docs: v3 implementation plan)::

    store = Store(dsn)                      # psycopg 3 connection pool, migrates on open
    gen = store.create_generation("llamacpp:embeddinggemma-300m-q8@768", 768)
    store.upsert_note(NoteRecord(...), [ChunkRecord(0, "...", vec)], gen.id)
    store.activate_generation(gen.id)       # builds the HNSW index if needed
    hits = store.search(qvec, "query text", 8, rerank=embed.rerank)

Dependencies: ``psycopg[binary]>=3.2``, ``psycopg-pool``, ``pgvector``, ``numpy``.
"""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import psycopg
from pgvector import HalfVector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import search as rk
from .search import Candidate, Hit, Reranker

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_MIGRATION_LOCK = 0x50_4B_4B_33  # pg_advisory_xact_lock key ("SKK3")


# --------------------------------------------------------------------------- records

@dataclass
class NoteRecord:
    name: str
    description: str = ""
    type: str | None = None
    priority: int = 0
    source_path: str | None = None
    modified: datetime.datetime | str | None = None
    modified_source: str | None = None
    body: str = ""
    # extension to the contract: [[wikilinks]] of the note. None = leave links untouched.
    links: Sequence[str] | None = None


@dataclass
class ChunkRecord:
    idx: int
    body: str
    embedding: Sequence[float]


@dataclass
class Generation:
    id: int
    embed_identity: str
    dim: int
    created_at: datetime.datetime
    status: str
    activated_at: datetime.datetime | None = None
    retired_at: datetime.datetime | None = None
    chunk_count: int = 0
    hnsw_m: int = 16
    hnsw_ef_construction: int = 64
    index_built: bool = False

    @property
    def table(self) -> str:
        return f"chunks_g{int(self.id)}"


@dataclass
class NoteVersion:
    note_name: str
    fingerprint: str
    first_seen: datetime.datetime
    last_seen: datetime.datetime
    date_source: str | None
    seeded: bool


@dataclass
class SearchConfig:
    """Knobs of the search. Defaults = EmbeddingGemma, as tuned on the 300-question bench."""
    lexical_weight: float = rk.DEFAULT_LEXICAL_WEIGHT
    head_share: float = rk.DEFAULT_HEAD_SHARE
    fusion: str = "linear"            # linear | rrf
    rrf_k: int = rk.DEFAULT_RRF_K
    priority_boost: float = 0.0       # multiplicative, e.g. 0.08 (SOKKAN 2.x default)
    rerank_top: int = rk.DEFAULT_RERANK_TOP
    # Below this many chunks the generation is scanned exactly (no HNSW): small corpora get
    # exactly the ranking of a brute-force search, at a cost of a few ms.
    exact_max_chunks: int = 20_000
    dense_candidates: int = 100       # chunks taken from HNSW
    lexical_candidates: int = 50      # notes taken from the lexical index
    ef_search: int = 100              # hnsw.ef_search (>= dense_candidates is sensible)
    # a query word present in more than this share of the notes is not used to PICK
    # lexical candidates (it still counts in their score): it would select half the corpus
    lexical_filter_df: float = 0.05


class StoreError(RuntimeError):
    pass


class DimensionMismatch(StoreError):
    pass


# --------------------------------------------------------------------------- helpers

def _unit(vec: Sequence[float], dim: int) -> HalfVector:
    a = np.asarray(vec, dtype=np.float32)
    if a.ndim != 1 or a.shape[0] != dim:
        raise DimensionMismatch(f"vector of dimension {a.shape[-1] if a.ndim else 0}, "
                                f"generation expects {dim}")
    n = float(np.linalg.norm(a))
    if n > 0:
        a = a / n
    return HalfVector(a)


def _tsv_tokens(text: str) -> list[str]:
    return sorted(rk.tokens(text))


def _to_dt(v) -> datetime.datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime.datetime):
        return v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)
    if isinstance(v, datetime.date):
        return datetime.datetime(v.year, v.month, v.day, tzinfo=datetime.timezone.utc)
    try:
        d = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


_GEN_COLS = ("id, embed_identity, dim, created_at, status, activated_at, retired_at, "
             "chunk_count, hnsw_m, hnsw_ef_construction, index_built")


def _gen(row) -> Generation | None:
    return Generation(**row) if row else None


# --------------------------------------------------------------------------- store

class Store:
    def __init__(self, dsn: str | None = None, *, min_size: int = 1, max_size: int = 4,
                 migrate: bool = True, config: SearchConfig | None = None,
                 connect_timeout: float = 10.0):
        self.dsn = dsn or os.environ.get("SOKKAN_DATABASE_URL", "")
        if not self.dsn:
            raise StoreError("no database: pass a DSN or set SOKKAN_DATABASE_URL")
        self.config = config or SearchConfig()
        if migrate:
            self.migrate()
        self.pool = ConnectionPool(
            self.dsn, min_size=min_size, max_size=max_size, open=True,
            configure=register_vector, timeout=connect_timeout,
            kwargs={"row_factory": dict_row},
        )

    def close(self) -> None:
        self.pool.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ migrations
    @staticmethod
    def migrations() -> list[tuple[int, str, str]]:
        """(version, name, sql) in order: schema.sql is version 1."""
        out = [(1, "init", SCHEMA_FILE.read_text(encoding="utf-8"))]
        if MIGRATIONS_DIR.is_dir():
            for p in sorted(MIGRATIONS_DIR.iterdir()):
                m = _MIGRATION_RE.match(p.name)
                if m and int(m.group(1)) > 1:
                    out.append((int(m.group(1)), m.group(2), p.read_text(encoding="utf-8")))
        return out

    def migrate(self) -> list[int]:
        """Apply the missing migrations, each in its own transaction. Safe to run from
        several processes at once (advisory lock). Returns the versions applied."""
        applied: list[int] = []
        with psycopg.connect(self.dsn) as con:
            for version, name, sql in self.migrations():
                with con.transaction():
                    con.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
                    exists = con.execute(
                        "SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
                    if exists and con.execute(
                            "SELECT 1 FROM schema_migrations WHERE version = %s",
                            (version,)).fetchone():
                        continue
                    con.execute(sql)
                    con.execute("INSERT INTO schema_migrations(version, name) VALUES (%s, %s)",
                                (version, name))
                    applied.append(version)
        return applied

    def schema_version(self) -> int:
        with self.pool.connection() as con:
            return con.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"]

    # ------------------------------------------------------------------ generations
    def create_generation(self, embed_identity: str, dim: int, *, hnsw_m: int = 16,
                          hnsw_ef_construction: int = 64, build_index: bool = False) -> Generation:
        """New generation in status ``building`` with its own chunk partition.

        For a bulk (re)index, leave ``build_index=False``: the HNSW index is built once at
        the end (``build_index`` / ``activate_generation``), which is several times faster
        than maintaining it row by row. Incremental upserts after that keep it up to date.
        """
        if not 1 <= int(dim) <= 4000:
            raise StoreError(f"dimension {dim} out of range (1..4000 for halfvec HNSW)")
        with self.pool.connection() as con, con.transaction():
            g = _gen(con.execute(
                "INSERT INTO index_generations(embed_identity, dim, hnsw_m, hnsw_ef_construction)"
                f" VALUES (%s, %s, %s, %s) RETURNING {_GEN_COLS}",
                (embed_identity, int(dim), int(hnsw_m), int(hnsw_ef_construction))).fetchone())
            con.execute(f"CREATE TABLE {g.table} PARTITION OF chunks FOR VALUES IN ({g.id})")
            con.execute(f"ALTER TABLE {g.table} ADD CONSTRAINT {g.table}_dim "
                        f"CHECK (vector_dims(embedding) = {g.dim})")
        if build_index:
            self.build_index(g.id)
            g = self.get_generation(g.id)
        return g

    def build_index(self, generation: int | Generation, *,
                    maintenance_work_mem: str | None = None) -> None:
        g = self._require_gen(generation)
        if g.index_built:
            return
        with self.pool.connection() as con, con.transaction():
            if maintenance_work_mem:
                con.execute("SELECT set_config('maintenance_work_mem', %s, true)",
                            (maintenance_work_mem,))
            con.execute(
                f"CREATE INDEX IF NOT EXISTS {g.table}_hnsw ON {g.table} USING hnsw "
                f"((embedding::halfvec({g.dim})) halfvec_ip_ops) "
                f"WITH (m = {g.hnsw_m}, ef_construction = {g.hnsw_ef_construction})")
            con.execute("UPDATE index_generations SET index_built = true WHERE id = %s", (g.id,))
        with self.pool.connection() as con:
            con.execute(f"ANALYZE {g.table}")
            con.commit()

    def activate_generation(self, generation: int | Generation) -> Generation:
        """Atomic switch: this generation becomes the one served by default, the previous
        active one is retired (still searchable with ``generation=`` until purged)."""
        g = self._require_gen(generation)
        if g.status == "active":
            return g
        self.build_index(g.id)
        with self.pool.connection() as con, con.transaction():
            con.execute("LOCK TABLE index_generations IN SHARE ROW EXCLUSIVE MODE")
            con.execute("UPDATE index_generations SET status = 'retired', retired_at = now() "
                        "WHERE status = 'active'")
            con.execute("UPDATE index_generations SET status = 'active', activated_at = now(), "
                        "retired_at = NULL WHERE id = %s", (g.id,))
        return self.get_generation(g.id)

    def retire_generation(self, generation: int | Generation) -> None:
        g = self._require_gen(generation)
        with self.pool.connection() as con, con.transaction():
            con.execute("UPDATE index_generations SET status = 'retired', retired_at = now() "
                        "WHERE id = %s", (g.id,))

    def drop_generation(self, generation: int | Generation) -> None:
        """Remove a non-active generation and its chunks (DETACH + DROP: instant)."""
        g = self._require_gen(generation)
        if g.status == "active":
            raise StoreError("refusing to drop the active generation; activate another first")
        with self.pool.connection() as con, con.transaction():
            con.execute(f"ALTER TABLE chunks DETACH PARTITION {g.table}")
            con.execute(f"DROP TABLE {g.table}")
            con.execute("DELETE FROM index_generations WHERE id = %s", (g.id,))
            self._purge_orphan_notes(con)

    def purge_retired(self, older_than: datetime.timedelta = datetime.timedelta(days=7)
                      ) -> list[int]:
        """Drop the generations retired for longer than ``older_than`` (rollback window)."""
        with self.pool.connection() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM index_generations WHERE status = 'retired' "
                "AND retired_at < now() - %s", (older_than,)).fetchall()]
        for i in ids:
            self.drop_generation(i)
        return ids

    def active_generation(self) -> Generation | None:
        with self.pool.connection() as con:
            return _gen(con.execute(f"SELECT {_GEN_COLS} FROM index_generations "
                                    "WHERE status = 'active'").fetchone())

    def get_generation(self, gen_id: int) -> Generation | None:
        with self.pool.connection() as con:
            return _gen(con.execute(f"SELECT {_GEN_COLS} FROM index_generations WHERE id = %s",
                                    (int(gen_id),)).fetchone())

    def list_generations(self) -> list[Generation]:
        with self.pool.connection() as con:
            return [_gen(r) for r in con.execute(
                f"SELECT {_GEN_COLS} FROM index_generations ORDER BY id").fetchall()]

    def _require_gen(self, generation: int | Generation | None) -> Generation:
        if generation is None:
            g = self.active_generation()
            if g is None:
                raise StoreError("no active index generation")
            return g
        gid = generation.id if isinstance(generation, Generation) else int(generation)
        g = self.get_generation(gid)
        if g is None:
            raise StoreError(f"unknown index generation {gid}")
        return g

    # ------------------------------------------------------------------ notes
    def upsert_note(self, note: NoteRecord, chunks: list[ChunkRecord],
                    generation: int | Generation) -> None:
        """Create or replace a note and ITS chunks in ``generation`` (other generations'
        chunks are untouched), in one transaction."""
        self.upsert_notes([(note, chunks)], generation)

    def upsert_notes(self, items: Iterable[tuple[NoteRecord, list[ChunkRecord]]],
                     generation: int | Generation) -> int:
        """Bulk form of ``upsert_note``: one transaction, chunks loaded with COPY.
        Returns the number of chunks written."""
        g = self._require_gen(generation)
        items = list(items)
        if not items:
            return 0
        prepared = []
        for note, chunks in items:
            vecs = [_unit(c.embedding, g.dim) for c in chunks]  # validate before writing
            bodies = [c.body for c in chunks] or [note.body]
            head = sorted(rk.head_tokens(note.name, note.description))
            lex = sorted(rk.body_tokens(note.name, note.description, bodies))
            prepared.append((note, chunks, vecs, head, lex))
        written = 0
        with self.pool.connection() as con, con.transaction():
            names = [p[0].name for p in prepared]
            old = {r["name"]: (r["id"], set(r["lex_tokens"])) for r in con.execute(
                "SELECT id, name, lex_tokens FROM notes WHERE name = ANY(%s) ORDER BY name "
                "FOR UPDATE", (names,)).fetchall()}
            delta: dict[str, int] = {}
            ids: dict[str, int] = {}
            for note, _chunks, _vecs, head, lex in prepared:
                prev = old.get(note.name)
                prev_tokens = prev[1] if prev else set()
                new_tokens = set(lex)
                for t in new_tokens - prev_tokens:
                    delta[t] = delta.get(t, 0) + 1
                for t in prev_tokens - new_tokens:
                    delta[t] = delta.get(t, 0) - 1
                if prev is None:
                    delta[""] = delta.get("", 0) + 1
                row = con.execute(
                    "INSERT INTO notes(name, description, type, priority, source_path, modified,"
                    " modified_source, body, head_tokens, lex_tokens, updated_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())"
                    " ON CONFLICT (name) DO UPDATE SET description = excluded.description,"
                    " type = excluded.type, priority = excluded.priority,"
                    " source_path = excluded.source_path, modified = excluded.modified,"
                    " modified_source = excluded.modified_source, body = excluded.body,"
                    " head_tokens = excluded.head_tokens, lex_tokens = excluded.lex_tokens,"
                    " updated_at = now() RETURNING id",
                    (note.name, note.description or "", note.type, int(note.priority or 0),
                     note.source_path, _to_dt(note.modified), note.modified_source,
                     note.body or "", head, lex)).fetchone()
                ids[note.name] = row["id"]
                if note.links is not None:
                    con.execute("DELETE FROM links WHERE src = %s", (note.name,))
                    dsts = sorted({d for d in note.links if d and d != note.name})
                    if dsts:
                        con.execute("INSERT INTO links(src, dst) SELECT %s, unnest(%s::text[]) "
                                    "ON CONFLICT DO NOTHING", (note.name, dsts))
            self._apply_df(con, delta)
            removed = con.execute(
                f"DELETE FROM {g.table} WHERE note_id = ANY(%s)", (list(ids.values()),)).rowcount
            con.execute("CREATE TEMP TABLE IF NOT EXISTS _sokkan_chunk_load ("
                        "note_id bigint, idx int, body text, embedding halfvec, toks text[])"
                        " ON COMMIT DELETE ROWS")
            with con.cursor().copy(
                    "COPY _sokkan_chunk_load (note_id, idx, body, embedding, toks) FROM STDIN "
                    "WITH (FORMAT BINARY)") as cp:
                cp.set_types(["int8", "int4", "text", "halfvec", "text[]"])
                for note, chunks, vecs, _head, _lex in prepared:
                    nid = ids[note.name]
                    for c, v in zip(chunks, vecs):
                        cp.write_row((nid, int(c.idx), c.body, v, _tsv_tokens(c.body)))
                        written += 1
            con.execute(
                f"INSERT INTO {g.table} (generation_id, note_id, idx, body, embedding, tsv) "
                f"SELECT {g.id}, note_id, idx, body, embedding, array_to_tsvector(toks) "
                "FROM _sokkan_chunk_load")
            con.execute("UPDATE index_generations SET chunk_count = chunk_count + %s "
                        "WHERE id = %s", (written - removed, g.id))
        return written

    def delete_note(self, name: str, generation: int | Generation | None = None) -> None:
        """Delete the note's chunks in ``generation`` (all generations if None). The note
        itself (text, links, IDF stats) goes once no generation holds it any more.
        ``note_versions`` history is kept."""
        gens = [self._require_gen(generation)] if generation is not None \
            else self.list_generations()
        with self.pool.connection() as con, con.transaction():
            row = con.execute("SELECT id FROM notes WHERE name = %s FOR UPDATE",
                              (name,)).fetchone()
            if row is None:
                return
            for g in gens:
                n = con.execute(f"DELETE FROM {g.table} WHERE note_id = %s",
                                (row["id"],)).rowcount
                if n:
                    con.execute("UPDATE index_generations SET chunk_count = chunk_count - %s "
                                "WHERE id = %s", (n, g.id))
            if not con.execute("SELECT 1 FROM chunks WHERE note_id = %s LIMIT 1",
                               (row["id"],)).fetchone():
                self._delete_note_row(con, row["id"])

    def _delete_note_row(self, con, note_id: int) -> None:
        r = con.execute("DELETE FROM notes WHERE id = %s RETURNING name, lex_tokens",
                        (note_id,)).fetchone()
        if r is None:
            return
        delta = {t: -1 for t in r["lex_tokens"]}
        delta[""] = -1
        self._apply_df(con, delta)
        con.execute("DELETE FROM links WHERE src = %s", (r["name"],))

    def _purge_orphan_notes(self, con) -> None:
        for r in con.execute("SELECT id FROM notes n WHERE NOT EXISTS "
                             "(SELECT 1 FROM chunks c WHERE c.note_id = n.id)").fetchall():
            self._delete_note_row(con, r["id"])

    @staticmethod
    def _apply_df(con, delta: dict[str, int]) -> None:
        delta = {t: d for t, d in delta.items() if d}
        if not delta:
            return
        toks = sorted(delta)  # fixed lock order: concurrent writers cannot deadlock
        con.execute(
            "INSERT INTO lex_df(token, df) SELECT t, d FROM unnest(%s::text[], %s::int[]) "
            "AS x(t, d) ORDER BY t ON CONFLICT (token) DO UPDATE SET df = lex_df.df + excluded.df",
            (toks, [delta[t] for t in toks]))
        con.execute("DELETE FROM lex_df WHERE token = ANY(%s) AND df <= 0",
                    ([t for t in toks if delta[t] < 0],))

    def get_note(self, name: str) -> NoteRecord | None:
        with self.pool.connection() as con:
            r = con.execute(
                "SELECT name, description, type, priority, source_path, modified, "
                "modified_source, body, ARRAY(SELECT dst FROM links WHERE src = n.name "
                "ORDER BY dst) AS links FROM notes n WHERE name = %s", (name,)).fetchone()
        return NoteRecord(**r) if r else None

    def find_note_by_path(self, stem: str) -> str | None:
        """Note name whose file is ``<stem>.md`` (or ``<stem with _>.md``)."""
        stem = stem.removesuffix(".md")
        with self.pool.connection() as con:
            for like in (f"%/{stem}.md", f"%/{stem.replace('-', '_')}.md"):
                r = con.execute("SELECT name FROM notes WHERE source_path LIKE %s LIMIT 1",
                                (like,)).fetchone()
                if r:
                    return r["name"]
        return None

    def get_chunks(self, name: str, generation: int | Generation | None = None) -> list[str]:
        g = self._require_gen(generation)
        with self.pool.connection() as con:
            return [r["body"] for r in con.execute(
                f"SELECT c.body FROM {g.table} c JOIN notes n ON n.id = c.note_id "
                "WHERE n.name = %s ORDER BY c.idx", (name,)).fetchall()]

    def note_names(self) -> list[str]:
        with self.pool.connection() as con:
            return [r["name"] for r in con.execute("SELECT name FROM notes ORDER BY name")]

    # ------------------------------------------------------------------ dates (truth)
    def note_seen(self, name: str) -> NoteVersion | None:
        """Latest known version of a note (by last_seen)."""
        with self.pool.connection() as con:
            r = con.execute(
                "SELECT note_name, fingerprint, first_seen, last_seen, date_source, seeded "
                "FROM note_versions WHERE note_name = %s ORDER BY last_seen DESC, id DESC "
                "LIMIT 1", (name,)).fetchone()
        return NoteVersion(**r) if r else None

    def versions_by_fingerprint(self, fingerprints: Sequence[str]) -> list[NoteVersion]:
        """Every version carrying one of these fingerprints, oldest first (rename
        inheritance: a renamed note finds the date of its previous name)."""
        with self.pool.connection() as con:
            return [NoteVersion(**r) for r in con.execute(
                "SELECT note_name, fingerprint, first_seen, last_seen, date_source, seeded "
                "FROM note_versions WHERE fingerprint = ANY(%s) ORDER BY first_seen, id",
                (list(fingerprints),)).fetchall()]

    def has_versions(self) -> bool:
        """False on a fresh store (the bootstrap pass of effective_modified)."""
        with self.pool.connection() as con:
            return con.execute("SELECT EXISTS (SELECT 1 FROM note_versions) AS e"
                               ).fetchone()["e"]

    def record_seen(self, name: str, fingerprint: str, *, first_seen=None,
                    date_source: str | None = None, seeded: bool = False,
                    seen_at=None, reset_first_seen: bool = False) -> NoteVersion:
        """Record that ``name`` was indexed with content ``fingerprint``.

        A new (name, fingerprint) pair is inserted with ``first_seen`` (default: now). A known
        pair only moves ``last_seen`` — unless ``reset_first_seen`` (content that came back
        after a change is new again, as in the reference effective_modified)."""
        now = _to_dt(seen_at) or datetime.datetime.now(datetime.timezone.utc)
        fs = _to_dt(first_seen) or now
        with self.pool.connection() as con, con.transaction():
            r = con.execute(
                "INSERT INTO note_versions(note_name, fingerprint, first_seen, last_seen,"
                " date_source, seeded) VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (note_name, fingerprint) DO UPDATE SET"
                " last_seen = GREATEST(note_versions.last_seen, excluded.last_seen),"
                " first_seen = CASE WHEN %s THEN excluded.first_seen"
                "                   ELSE note_versions.first_seen END,"
                " seeded = CASE WHEN %s THEN excluded.seeded ELSE note_versions.seeded END,"
                " date_source = COALESCE(excluded.date_source, note_versions.date_source)"
                " RETURNING note_name, fingerprint, first_seen, last_seen, date_source, seeded",
                (name, fingerprint, fs, now, date_source, bool(seeded),
                 bool(reset_first_seen), bool(reset_first_seen))).fetchone()
        return NoteVersion(**r)

    # ------------------------------------------------------------------ links / recall
    def set_links(self, src: str, dsts: Iterable[str]) -> None:
        dsts = sorted({d for d in dsts if d and d != src})
        with self.pool.connection() as con, con.transaction():
            con.execute("DELETE FROM links WHERE src = %s", (src,))
            if dsts:
                con.execute("INSERT INTO links(src, dst) SELECT %s, unnest(%s::text[])",
                            (src, dsts))

    def links(self, name: str) -> dict:
        """Outgoing [[links]] and backlinks of a note, with descriptions."""
        with self.pool.connection() as con:
            out = con.execute(
                "SELECT l.dst AS name, coalesce(n.description, '') AS description, "
                "n.id IS NOT NULL AS exists FROM links l LEFT JOIN notes n ON n.name = l.dst "
                "WHERE l.src = %s ORDER BY l.dst", (name,)).fetchall()
            back = con.execute(
                "SELECT l.src AS name, n.description FROM links l JOIN notes n "
                "ON n.name = l.src WHERE l.dst = %s ORDER BY l.src", (name,)).fetchall()
        return {"note": name, "links": out, "backlinks": back}

    def log_recall(self, channel: str, hits: Sequence[Hit], *, session_id: str | None = None,
                   agent_id: str | None = None, query: str | None = None) -> int:
        """Record which notes were injected where (P0-3). Returns the number of rows."""
        if not hits:
            return 0
        with self.pool.connection() as con, con.transaction():
            with con.cursor() as cur:
                cur.executemany(
                    "INSERT INTO recall_log(channel, session_id, agent_id, query, note_name,"
                    " rank, score, rerank, fingerprint, generation_id)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                    " (SELECT fingerprint FROM note_versions WHERE note_name = %s"
                    "  ORDER BY last_seen DESC LIMIT 1), %s)",
                    [(channel, session_id, agent_id, query, h.note_name, i + 1, h.score,
                      h.rerank, h.note_name, h.generation) for i, h in enumerate(hits)])
        return len(hits)

    # ------------------------------------------------------------------ search
    def search(self, query_vec: Sequence[float] | None, query_text: str, k: int = 8, *,
               generation: int | Generation | None = None, rerank: Reranker | None = None,
               config: SearchConfig | None = None, **overrides) -> list[Hit]:
        """Two-stage search. ``query_vec`` None = lexical-only (embedding backend down):
        results then carry ``degraded``. Keyword overrides patch ``config`` for this call
        (e.g. ``fusion="rrf"``, ``lexical_weight=0.5``)."""
        cfg = config or self.config
        if overrides:
            cfg = SearchConfig(**{**cfg.__dict__, **overrides})
        try:
            g = self._require_gen(generation)
        except StoreError:
            if generation is not None:
                raise
            return []
        qv = _unit(query_vec, g.dim) if query_vec is not None else None
        qtok = sorted(rk.tokens(query_text or ""))
        if qv is None and not qtok:
            raise StoreError("lexical-only search needs at least one keyword")
        exact = g.chunk_count <= cfg.exact_max_chunks
        with self.pool.connection() as con, con.transaction():
            weights, n_notes, df = self._weights(con, qtok)
            toks, ws = list(weights), [weights[t] for t in weights]
            tot = sum(ws) or 1.0
            if qv is not None:
                if exact:
                    ids = None  # every note of the generation
                else:
                    con.execute("SELECT set_config('hnsw.ef_search', %s, true)",
                                (str(int(max(cfg.ef_search, cfg.dense_candidates))),))
                    ids = {r["note_id"] for r in con.execute(
                        f"SELECT note_id FROM {g.table} ORDER BY "
                        f"(embedding::halfvec({g.dim})) <#> %s::halfvec({g.dim}) LIMIT %s",
                        (qv, int(cfg.dense_candidates))).fetchall()}
                    if toks:
                        ids |= self._lexical_candidates(con, g, toks, ws, tot, df, n_notes, cfg)
                best = self._best_chunks_dense(con, g, qv, ids)
            else:
                ids = self._lexical_candidates(con, g, toks, ws, tot, df, n_notes, cfg,
                                               limit=None if exact else cfg.lexical_candidates)
                best = self._best_chunks_lexical(con, g, qtok, ids)
            meta = self._note_meta(con, list(best), toks, ws, tot, cfg.head_share)
        cands, lex = [], {}
        for nid, b in best.items():
            m = meta[nid]
            lex[m["name"]] = m["lex"]
            cands.append(Candidate(
                note_name=m["name"], description=m["description"], best_chunk=b["body"],
                cosine=b.get("cos"), chunk_overlap=b.get("ov", 0.0), priority=m["priority"],
                modified=m["modified"], modified_source=m["modified_source"],
                source_path=m["source_path"], chunk_idx=b["idx"]))
        degraded = None if qv is not None else (
            "embedding unavailable: lexical-only scoring, degraded recall (no cross-lingual)")
        return rk.rank(
            query_text, cands, weights=weights, k=k, lexical_weight=cfg.lexical_weight,
            head_share=cfg.head_share, fusion=cfg.fusion, rrf_k=cfg.rrf_k,
            priority_boost=cfg.priority_boost, rerank=rerank, rerank_top=cfg.rerank_top,
            generation=g.id, degraded=degraded, lexical=lex)

    # -- search stages ---------------------------------------------------------
    @staticmethod
    def _weights(con, qtok: list[str]) -> tuple[dict[str, float], int, dict[str, int]]:
        rows = con.execute("SELECT token, df FROM lex_df WHERE token = ANY(%s)",
                           (qtok + [""],)).fetchall()
        df = {r["token"]: r["df"] for r in rows}
        n_notes = df.pop("", 0)
        return rk.idf_weights(qtok, df, n_notes), n_notes, df

    @staticmethod
    def _lex_expr(alias: str = "n") -> str:
        return (f"((1 - %(hs)s) * coalesce((SELECT sum(q.w) FROM q WHERE q.t = ANY({alias}.lex_tokens)), 0)"
                f" + %(hs)s * coalesce((SELECT sum(q.w) FROM q WHERE q.t = ANY({alias}.head_tokens)), 0)"
                ") / %(tot)s")

    def _lexical_candidates(self, con, g: Generation, toks, ws, tot, df, n_notes,
                            cfg: SearchConfig, limit: int | None = -1) -> set[int]:
        """Notes ranked by the lexical score, picked through the GIN index on lex_tokens.
        ``limit=-1``: cfg.lexical_candidates ; None: no limit."""
        if limit == -1:
            limit = cfg.lexical_candidates
        present = [t for t in toks if df.get(t, 0) > 0]
        if not present:
            return set()
        cap = max(1000, int(cfg.lexical_filter_df * n_notes))
        pick = [t for t in present if df[t] <= cap] if limit is not None else present
        if not pick:
            pick = sorted(present, key=lambda t: df[t])[:2]
        sql = (f"WITH q AS (SELECT * FROM unnest(%(toks)s::text[], %(ws)s::float8[]) AS q(t, w)) "
               f"SELECT n.id FROM notes n WHERE n.lex_tokens && %(pick)s::text[] "
               f"AND EXISTS (SELECT 1 FROM {g.table} c WHERE c.note_id = n.id) "
               f"ORDER BY {self._lex_expr()} DESC, n.name")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = con.execute(sql, {"toks": toks, "ws": ws, "pick": pick,
                                 "hs": cfg.head_share, "tot": tot}).fetchall()
        return {r["id"] for r in rows}

    @staticmethod
    def _best_chunks_dense(con, g: Generation, qv: HalfVector, ids: set[int] | None) -> dict:
        """Exact best chunk per note (cosine = inner product of unit vectors)."""
        where = "" if ids is None else "WHERE note_id = ANY(%(ids)s)"
        rows = con.execute(
            f"SELECT DISTINCT ON (note_id) note_id, idx, body, "
            f"-(embedding <#> %(q)s::halfvec) AS cos FROM {g.table} {where} "
            f"ORDER BY note_id, embedding <#> %(q)s::halfvec, idx",
            {"q": qv, "ids": list(ids or [])}).fetchall()
        return {r["note_id"]: r for r in rows}

    @staticmethod
    def _best_chunks_lexical(con, g: Generation, qtok: list[str], ids: set[int]) -> dict:
        if not ids:
            return {}
        rows = con.execute(
            f"SELECT DISTINCT ON (note_id) note_id, idx, body, "
            f"(SELECT count(*) FROM unnest(tsvector_to_array(tsv)) l WHERE l = ANY(%(t)s))"
            f"::float8 / %(n)s AS ov FROM {g.table} WHERE note_id = ANY(%(ids)s) "
            f"ORDER BY note_id, ov DESC, idx",
            {"t": qtok, "n": len(qtok), "ids": list(ids)}).fetchall()
        return {r["note_id"]: r for r in rows}

    def _note_meta(self, con, ids: list[int], toks, ws, tot, head_share) -> dict:
        if not ids:
            return {}
        lex = self._lex_expr() if toks else "0.0::float8"
        rows = con.execute(
            "WITH q AS (SELECT * FROM unnest(%(toks)s::text[], %(ws)s::float8[]) AS q(t, w)) "
            "SELECT n.id, n.name, n.description, n.priority, n.modified, n.modified_source, "
            f"n.source_path, {lex} AS lex FROM notes n WHERE n.id = ANY(%(ids)s)",
            {"toks": toks, "ws": ws, "hs": head_share, "tot": tot, "ids": ids}).fetchall()
        return {r["id"]: r for r in rows}

