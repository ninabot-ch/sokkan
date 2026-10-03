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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import psycopg
from pgvector import HalfVector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import search as rk
from .config import env, env_int
from .contract import DATE_SOURCES, ChunkRecord, NoteRecord, SeenRecord
from .search import Candidate, Hit, Reranker

__all__ = ["Store", "SearchConfig", "Generation", "NoteRecord", "ChunkRecord", "SeenRecord",
           "StoreError", "DimensionMismatch", "DATE_SOURCES"]

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_MIGRATION_LOCK = 0x50_4B_4B_33  # pg_advisory_xact_lock key ("SKK3")


# --------------------------------------------------------------------------- records
# NoteRecord / ChunkRecord / SeenRecord: contract.py (shared with the indexer).

@dataclass
class Generation:
    """contract.Generation plus the store's bookkeeping (dates as ISO 8601 strings)."""
    id: int
    embed_identity: str
    dim: int
    created_at: str
    status: str
    activated_at: str | None = None
    retired_at: str | None = None
    chunk_count: int = 0
    hnsw_m: int = 16
    hnsw_ef_construction: int = 64
    index_built: bool = False

    @property
    def table(self) -> str:
        return f"chunks_g{int(self.id)}"


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

    @classmethod
    def from_env(cls, **overrides) -> "SearchConfig":
        """``CORTHEXIS_<KNOB>`` (or the ``SOKKAN_<KNOB>`` of 2.x installs)."""
        def f(name, default):
            raw = env(name)
            try:
                return float(raw) if raw is not None else default
            except ValueError:
                return default
        d = cls()
        cfg = cls(
            lexical_weight=f("LEXICAL_WEIGHT", d.lexical_weight),
            head_share=f("HEAD_SHARE", d.head_share),
            fusion=(env("SEARCH_FUSION") or d.fusion).lower(),
            rrf_k=env_int("RRF_K", d.rrf_k),
            priority_boost=f("PRIORITY_BOOST", d.priority_boost),
            rerank_top=env_int("RERANK_TOP", d.rerank_top),
            exact_max_chunks=env_int("SEARCH_EXACT_MAX_CHUNKS", d.exact_max_chunks),
            dense_candidates=env_int("SEARCH_DENSE_CANDIDATES", d.dense_candidates),
            lexical_candidates=env_int("SEARCH_LEXICAL_CANDIDATES", d.lexical_candidates),
            ef_search=env_int("HNSW_EF_SEARCH", d.ef_search),
        )
        return cls(**{**cfg.__dict__, **overrides})


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


def _iso(v) -> str | None:
    """Dates are carried as ISO 8601 strings (contract); kept verbatim when already one."""
    if v is None or v == "":
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return str(v)


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
    if not row:
        return None
    row = dict(row)
    for k in ("created_at", "activated_at", "retired_at"):
        row[k] = _iso(row[k])
    return Generation(**row)


_SEEN_COLS = ("note_name AS name, content_hash, first_seen, seeded, date_source, description, "
              "body, seen_at, extra")


def _seen(row) -> SeenRecord | None:
    if not row:
        return None
    row = dict(row)
    row["extra"] = row["extra"] or {}
    return SeenRecord(**row)


# --------------------------------------------------------------------------- store

class Store:
    def __init__(self, dsn: str | None = None, *, min_size: int = 1, max_size: int = 4,
                 migrate: bool = True, config: SearchConfig | None = None,
                 connect_timeout: float = 10.0):
        self.dsn = dsn or env("DATABASE_URL") or ""
        if not self.dsn:
            raise StoreError("no database: pass a DSN or set CORTHEXIS_DATABASE_URL")
        self.config = config or SearchConfig.from_env()
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
    def upsert_note(self, note: NoteRecord, chunks: list[ChunkRecord] | None,
                    generation: int | Generation) -> None:
        """Create or replace a note and ITS chunks in ``generation`` (other generations'
        chunks are untouched), in one transaction. ``chunks=None``: metadata-only update,
        the chunks stay as they are."""
        if chunks is None:
            self._upsert_meta(note, generation)
        else:
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
            prepared.append((note, chunks, vecs, [c.body for c in chunks]))
        written = 0
        with self.pool.connection() as con, con.transaction():
            ids = self._write_notes(con, [(p[0], p[3]) for p in prepared])
            removed = con.execute(
                f"DELETE FROM {g.table} WHERE note_id = ANY(%s)", (list(ids.values()),)).rowcount
            con.execute("CREATE TEMP TABLE IF NOT EXISTS _chunk_load (note_id bigint, idx int, "
                        "body text, embedding halfvec, toks text[]) ON COMMIT DELETE ROWS")
            with con.cursor().copy(
                    "COPY _chunk_load (note_id, idx, body, embedding, toks) FROM STDIN "
                    "WITH (FORMAT BINARY)") as cp:
                cp.set_types(["int8", "int4", "text", "halfvec", "text[]"])
                for note, chunks, vecs, _bodies in prepared:
                    nid = ids[note.name]
                    for c, v in zip(chunks, vecs):
                        cp.write_row((nid, int(c.idx), c.body, v, _tsv_tokens(c.body)))
                        written += 1
            con.execute(
                f"INSERT INTO {g.table} (generation_id, note_id, idx, body, embedding, tsv) "
                f"SELECT {g.id}, note_id, idx, body, embedding, array_to_tsvector(toks) "
                "FROM _chunk_load")
            con.execute("UPDATE index_generations SET chunk_count = chunk_count + %s "
                        "WHERE id = %s", (written - removed, g.id))
        return written

    def _upsert_meta(self, note: NoteRecord, generation: int | Generation) -> None:
        g = self._require_gen(generation)
        with self.pool.connection() as con, con.transaction():
            bodies = [r["body"] for r in con.execute(
                f"SELECT c.body FROM {g.table} c JOIN notes n ON n.id = c.note_id "
                "WHERE n.name = %s ORDER BY c.idx", (note.name,)).fetchall()]
            self._write_notes(con, [(note, bodies)])

    def _write_notes(self, con, items: list[tuple[NoteRecord, list[str]]]) -> dict[str, int]:
        """Upsert the notes rows and keep the IDF statistics (lex_df) in step.
        ``items`` = (note, chunk bodies) ; the lexical body = name + description + chunks
        (the note body when there is no chunk)."""
        names = sorted({n.name for n, _ in items})
        old = {r["name"]: set(r["lex_tokens"]) for r in con.execute(
            "SELECT name, lex_tokens FROM notes WHERE name = ANY(%s) ORDER BY name FOR UPDATE",
            (names,)).fetchall()}
        delta: dict[str, int] = {}
        ids: dict[str, int] = {}
        for note, bodies in items:
            head = sorted(rk.head_tokens(note.name, note.description))
            lex = sorted(rk.body_tokens(note.name, note.description, bodies or [note.body or ""]))
            prev = old.get(note.name)
            new = set(lex)
            for t in new - (prev or set()):
                delta[t] = delta.get(t, 0) + 1
            for t in (prev or set()) - new:
                delta[t] = delta.get(t, 0) - 1
            if prev is None:
                delta[""] = delta.get("", 0) + 1
            old[note.name] = new
            ids[note.name] = con.execute(
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
                 note.source_path, _iso(note.modified), note.modified_source,
                 note.body or "", head, lex)).fetchone()["id"]
        self._apply_df(con, delta)
        return ids

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
        neg = [t for t in toks if delta[t] < 0]
        if neg:
            con.execute("DELETE FROM lex_df WHERE token = ANY(%s) AND df <= 0", (neg,))

    def get_note(self, name: str) -> NoteRecord | None:
        with self.pool.connection() as con:
            r = con.execute(
                "SELECT name, description, type, priority, source_path, modified, "
                "modified_source, body FROM notes WHERE name = %s", (name,)).fetchone()
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

    def get_chunks(self, name: str, generation: int | Generation | None = None
                   ) -> list[ChunkRecord]:
        """Chunks of a note in ``generation`` (active by default), in order. Embeddings come
        back unit-normalised and rounded to half precision."""
        g = self._require_gen(generation)
        with self.pool.connection() as con:
            return [ChunkRecord(r["idx"], r["body"], r["embedding"].to_list())
                    for r in con.execute(
                        f"SELECT c.idx, c.body, c.embedding FROM {g.table} c "
                        "JOIN notes n ON n.id = c.note_id WHERE n.name = %s ORDER BY c.idx",
                        (name,)).fetchall()]

    def note_names(self, generation: int | Generation | None = None) -> set[str]:
        """Notes that have chunks in ``generation``; every known note when None."""
        with self.pool.connection() as con:
            if generation is None:
                return {r["name"] for r in con.execute("SELECT name FROM notes")}
            g = self._require_gen(generation)
            return {r["name"] for r in con.execute(
                f"SELECT n.name FROM notes n WHERE EXISTS "
                f"(SELECT 1 FROM {g.table} c WHERE c.note_id = n.id)")}

    def generations(self) -> list[Generation]:
        return self.list_generations()

    # ------------------------------------------------------------------ dates (truth)
    # One row of note_versions = one SeenRecord. Semantics = memstore.InMemoryStore.
    def note_seen(self, name: str) -> SeenRecord | None:
        """Latest version recorded under this name."""
        with self.pool.connection() as con:
            return _seen(con.execute(
                f"SELECT {_SEEN_COLS} FROM note_versions WHERE note_name = %s "
                "ORDER BY id DESC LIMIT 1", (name,)).fetchone())

    def record_seen(self, rec: SeenRecord) -> None:
        """Append a version when (content_hash, description) changed, else refresh the
        latest one (first_seen, seeded, date_source, body)."""
        if rec.date_source not in DATE_SOURCES:
            raise StoreError(f"unknown date_source {rec.date_source!r} ({', '.join(DATE_SOURCES)})")
        with self.pool.connection() as con, con.transaction():
            con.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("note_versions:" + rec.name,))
            last = con.execute(
                "SELECT id, content_hash, description FROM note_versions WHERE note_name = %s "
                "ORDER BY id DESC LIMIT 1", (rec.name,)).fetchone()
            if last and (last["content_hash"], last["description"]) == (rec.content_hash,
                                                                         rec.description or ""):
                con.execute(
                    "UPDATE note_versions SET first_seen = %s, seeded = %s, date_source = %s, "
                    "body = %s, last_seen = now() WHERE id = %s",
                    (_iso(rec.first_seen), bool(rec.seeded), rec.date_source, rec.body or "",
                     last["id"]))
                return
            con.execute(
                "INSERT INTO note_versions(note_name, content_hash, first_seen, seeded, "
                "date_source, description, body, seen_at, extra) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (rec.name, rec.content_hash, _iso(rec.first_seen), bool(rec.seeded),
                 rec.date_source, rec.description or "", rec.body or "",
                 _iso(rec.seen_at) or "", Jsonb(rec.extra or {})))

    def find_seen(self, content_hashes: Sequence[str]) -> SeenRecord | None:
        """Oldest version, under any name, carrying one of these hashes (a renamed note
        inherits the date of its previous name)."""
        with self.pool.connection() as con:
            return _seen(con.execute(
                f"SELECT {_SEEN_COLS} FROM note_versions WHERE content_hash = ANY(%s) "
                "ORDER BY first_seen::timestamptz, id LIMIT 1",
                (list(content_hashes),)).fetchone())

    def seen_count(self) -> int:
        """Number of notes with a recorded history (0 = bootstrap pass)."""
        with self.pool.connection() as con:
            return con.execute("SELECT count(DISTINCT note_name) AS n FROM note_versions"
                               ).fetchone()["n"]

    def note_versions(self, name: str, limit: int = 20) -> list[SeenRecord]:
        """The last ``limit`` versions of a note, oldest first."""
        with self.pool.connection() as con:
            rows = con.execute(
                f"SELECT {_SEEN_COLS} FROM note_versions WHERE note_name = %s "
                "ORDER BY id DESC LIMIT %s", (name, int(limit))).fetchall()
        return [_seen(r) for r in reversed(rows)]

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
                    " rank, score, rerank, content_hash, generation_id)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                    " (SELECT content_hash FROM note_versions WHERE note_name = %s"
                    "  ORDER BY id DESC LIMIT 1), %s)",
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

