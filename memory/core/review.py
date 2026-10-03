"""The memory reads itself back: the CortHeXis review engine.

Every check matches a failure that already happened silently on a real corpus:
a broken frontmatter that muted a note in the index, dates lost by a mechanical
rewrite, a stale alert served as fresh, a project closed in its description but
not in its body, ``[[links]]`` to notes that were renamed, repository paths that
moved, a key pasted into a note, two notes saying the same thing until they
contradict each other, and, upstream of all of it, a memory server that was no
longer declared in the sessions' configuration.

The engine only **reads**: notes (the markdown files are the source of truth), the
index (a ``ReviewSource``: the Postgres store, the 2.x SQLite file or the in-memory
reference store) and the session configuration for the chain checks. It names the
note and the remedy; repairs are proposed by ``core.repair`` and applied by the
caller after approval. The only thing it writes is its own history
(``PgHistory`` / ``SqliteHistory``).

    report = run_review(ReviewConfig.from_env(), source=PgSource(store),
                        chain=chain_checks(ChainConfig.from_env()))

Score = 100 − Σ weight(severity) × (1 + ln(1 + count) / 3), clamped to 0..100.
Secrets are never echoed: a finding says which kind of key and on which line, not
the value.

Configuration: ``CORTHEXIS_REVIEW_*`` (see ``ReviewConfig.from_env``), plus
``CORTHEXIS_MEMORY_DIR``, ``CORTHEXIS_DEAD_PATH_ROOTS`` / ``_PREFIXES`` shared with
the indexer.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .config import env, env_bool, env_int, env_list
from .contract import SeenRecord
from .dates import parse_dt, utcnow
from .drift import check_versions
from .indexer import find_dead_paths
from .notes import INDEX_FILENAME, LINK_RE, ParsedNote, filename_for, is_kebab, parse_note, slugify

SEVERITIES = ("crit", "warn", "info")
SEVERITY_WEIGHT = {"crit": 18, "warn": 5, "info": 1}
CATEGORIES = ("chain", "structure", "drift", "security", "graph", "dates")

CODE_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
OPEN_MARKERS = re.compile(
    r"⏳|\bEN ATTENTE\b|\ben attente de\b|\bTODO\b|\bà faire\b|\bà trancher\b|\breste ouvert\b|"
    r"\bpas encore\b|\bbloqué\b|\bin_progress\b|\bWIP\b|\bblocked\b|\bpending\b|\bnot yet\b|"
    r"\bstill open\b|\bto do\b|🟡|⛔", re.I)
# A note that says it is finished is not a dormant project, whatever it still lists.
CLOSED_MARKERS = re.compile(
    r"^\s*>?\s*\**\s*(?:✅\s*)?(?:closed|clos|close|fermé|abandoned|abandonné|done|fait|terminé|"
    r"livré|shipped|resolved|résolu)\b[^\n]{0,40}?\d{4}", re.I | re.M)
# Text addressed to the model rather than describing a fact: a note is injected into
# every session, an instruction hidden in it is a prompt injection.
INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions?|rules|prompts?)\b|"
    r"\b(?:ignore|oublie)\s+(?:toutes\s+)?(?:les\s+)?(?:instructions|consignes)\s+"
    r"(?:précédentes|ci-dessus)\b|"
    r"\byou\s+are\s+now\s+(?:in\s+)?(?:developer|dan|jailbreak|unrestricted)\b|"
    r"\breveal\s+(?:your|the)\s+system\s+prompt\b|"
    r"<\s*/?\s*(?:system|assistant)\s*>", re.I)

SECRET_PATTERNS: dict[str, re.Pattern] = {
    "API key (sk-…)": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),
    "xAI key": re.compile(r"\bxai-[A-Za-z0-9]{24,}"),
    "Resend key": re.compile(r"\bre_[A-Za-z0-9]{8,}_[A-Za-z0-9]{16,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "GitLab token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}"),
    "Stripe secret key": re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),
    "JSON Web Token": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    "Exoscale key": re.compile(r"\bEXO[0-9a-f]{24}\b"),
    "Cloudflare token": re.compile(r"\bcfk_[A-Za-z0-9]{20,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
}


def _looks_random(s: str) -> bool:
    """A placeholder (``sk-XXXXXXXX…``, ``sk-your-key-here-…``) is not a key."""
    tail = re.sub(r"^[a-z]+[-_](?:ant-|proj-|live_)?", "", s, flags=re.I)
    if "-----BEGIN" in s:
        return True
    if len(set(tail.lower())) < 10:
        return False
    if re.search(r"(?i)(x{6,}|\.{3}|your|example|placeholder|redacted|changeme|dummy|<|>)", tail):
        return False
    return sum(c.isdigit() for c in tail) >= 2


# ---------------------------------------------------------------------------- records

@dataclass
class Finding:
    id: str                 # check id, stable (used by the UI, the alerts, the history)
    severity: str           # crit | warn | info
    category: str           # chain | structure | drift | security | graph | dates
    title: str
    detail: str
    notes: list[str] = field(default_factory=list)
    remedy: str = ""
    count: int = 0
    # one row per problem, the material of the one-click actions:
    #   {"note": ..., ...check-specific keys...}
    items: list[dict] = field(default_factory=list)
    action: str | None = None   # relink | merge | rename | close (core.repair)
    judgement: bool = False     # a human (or a curation session) has to decide


@dataclass
class ReviewConfig:
    memory_dir: Path
    index_filename: str = INDEX_FILENAME
    undated_tolerated: int = 7
    stale_days: int = 60
    dup_cosine: float = 0.86
    dup_k: int = 5
    giant_words: int = 3500
    index_lag_min: int = 30
    grace_s: int = 300                  # a file younger than this may still be written
    dead_path_roots: list[Path] = field(default_factory=list)
    dead_path_prefixes: tuple[str, ...] = ()
    # 2.x writers (memory_write) name files "<name>.md": accepted besides the 3.0
    # "<name with _>.md" until the corpus is normalised.
    accept_kebab_files: bool = False
    max_items: int = 200

    @classmethod
    def from_env(cls, **overrides) -> "ReviewConfig":
        def f(name, default):
            try:
                return float(env(name) or default)
            except ValueError:
                return default
        cfg = cls(
            memory_dir=Path(env("MEMORY_DIR", "~/.corthexis/memory")).expanduser(),
            index_filename=env("INDEX_FILENAME", INDEX_FILENAME),
            undated_tolerated=env_int("REVIEW_UNDATED_TOLERATED", 7),
            stale_days=env_int("REVIEW_STALE_DAYS", 60),
            dup_cosine=f("REVIEW_DUP_COSINE", 0.86),
            dup_k=env_int("REVIEW_DUP_K", 5),
            giant_words=env_int("REVIEW_GIANT_WORDS", 3500),
            index_lag_min=env_int("REVIEW_INDEX_LAG_MIN", 30),
            grace_s=env_int("NORMALIZE_GRACE", 300),
            dead_path_roots=[Path(p).expanduser() for p in env_list("DEAD_PATH_ROOTS", ":")],
            dead_path_prefixes=tuple(env_list("DEAD_PATH_PREFIXES")),
            accept_kebab_files=env_bool("REVIEW_ACCEPT_KEBAB_FILES", False),
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg


@dataclass
class CorpusNote:
    """A note file as the review sees it."""
    name: str
    path: Path
    parsed: ParsedNote
    mtime: float
    words: int
    links: list[str]          # [[targets]] outside code, as written (stripped)

    @property
    def description(self) -> str:
        return self.parsed.description

    @property
    def type(self) -> str:
        return self.parsed.type

    @property
    def body(self) -> str:
        return self.parsed.body


def strip_code(text: str) -> str:
    return CODE_RE.sub(" ", text or "")


def note_links(body: str) -> list[str]:
    out: list[str] = []
    for m in LINK_RE.finditer(strip_code(body)):
        t = m.group(1).strip()
        if t and t not in out:
            out.append(t)
    return out


def load_corpus(memory_dir: Path, index_filename: str = INDEX_FILENAME) -> list[CorpusNote]:
    notes: list[CorpusNote] = []
    if not Path(memory_dir).is_dir():
        return notes
    for p in sorted(Path(memory_dir).glob("*.md")):
        if p.name == index_filename:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            st = p.stat()
        except OSError:
            continue
        parsed = parse_note(text, p.name)
        notes.append(CorpusNote(name=parsed.name, path=p, parsed=parsed, mtime=st.st_mtime,
                                words=len(parsed.body.split()), links=note_links(parsed.body)))
    return notes


class Resolver:
    """Every key under which a note may be cited: name, file stem, -/_ and case variants."""

    def __init__(self, notes: Iterable[CorpusNote]):
        self.by_key: dict[str, CorpusNote] = {}
        for n in notes:
            stem = n.path.stem
            for k in (n.name, stem, slugify(n.name), slugify(stem)):
                if k:
                    self.by_key.setdefault(k, n)

    def __call__(self, target: str) -> CorpusNote | None:
        t = target.strip().removesuffix(".md")
        for k in (t, slugify(t)):
            if k in self.by_key:
                return self.by_key[k]
        return None


# ---------------------------------------------------------------------------- sources

@dataclass
class IndexedNote:
    name: str
    description: str = ""
    modified: str | None = None
    modified_source: str | None = None
    body: str | None = None            # None: the index does not keep bodies (2.x SQLite)
    indexed_at: float | None = None    # epoch of the last write of this note in the index
    chunks: int = 0


class ReviewSource(Protocol):
    """What the review reads from an index. Every method may raise: the review turns an
    unreadable index into a finding instead of failing."""
    label: str

    def indexed(self) -> dict[str, IndexedNote]: ...
    def versions(self, name: str, limit: int = 20) -> list[SeenRecord]: ...
    def near_duplicates(self, min_cosine: float, k: int) -> list[tuple[str, str, float]]: ...
    def centroids(self) -> dict[str, list[float]]: ...
    def renames(self) -> dict[str, str]: ...


def _cos_pairs_exact(vecs: dict[str, list[float]], min_cosine: float
                     ) -> list[tuple[str, str, float]]:
    """Brute force, for small corpora and the reference stores (not for Postgres)."""
    import numpy as np
    names = sorted(vecs)
    if len(names) < 2:
        return []
    M = np.asarray([vecs[n] for n in names], dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True).clip(min=1e-9)
    S = M @ M.T
    iu = np.triu_indices(len(names), k=1)
    hit = np.where(S[iu] >= min_cosine)[0]
    return sorted(((names[iu[0][h]], names[iu[1][h]], float(S[iu[0][h], iu[1][h]]))
                   for h in hit), key=lambda t: -t[2])


def _mean(vectors: list[list[float]]) -> list[float]:
    import numpy as np
    v = np.mean(np.asarray(vectors, dtype=np.float32), axis=0)
    n = float(np.linalg.norm(v)) or 1.0
    return (v / n).tolist()


class MemorySource:
    """``core.memstore.InMemoryStore`` (tests, executable spec)."""
    label = "in-memory store"

    def __init__(self, store):
        self.store = store

    def _gen(self):
        g = self.store.active_generation()
        return g.id if g else None

    def indexed(self):
        gid = self._gen()
        out = {}
        for name, n in self.store.notes.items():
            out[name] = IndexedNote(name, n.description, n.modified, n.modified_source, n.body,
                                    None, len(self.store.chunks.get((gid, name), [])))
        return out

    def versions(self, name, limit=20):
        return self.store.note_versions(name, limit)

    def centroids(self):
        gid = self._gen()
        return {name: _mean([c.embedding for c in chunks])
                for (g, name), chunks in self.store.chunks.items() if g == gid and chunks}

    def near_duplicates(self, min_cosine, k):
        return _cos_pairs_exact(self.centroids(), min_cosine)

    def renames(self):
        return _renames_from_versions(self.store.versions, set(self.store.notes))


def _renames_from_versions(versions: dict[str, list[SeenRecord]], live: set[str]
                           ) -> dict[str, str]:
    by_hash: dict[str, set[str]] = {}
    for name, hist in versions.items():
        for v in hist:
            by_hash.setdefault(v.content_hash, set()).add(name)
    out = {}
    for name, hist in versions.items():
        if name in live:
            continue
        for v in reversed(hist):
            heirs = sorted(n for n in by_hash.get(v.content_hash, ()) if n in live and n != name)
            if len(heirs) == 1:
                out[name] = heirs[0]
                break
    return out


class SqliteSource:
    """The 2.x SQLite index (``memory.db``: notes / chunks with JSON embeddings / links).
    No version history: the drift check is skipped. Near duplicates are computed
    exactly — this path only serves small, not-yet-migrated corpora."""
    label = "SQLite index (2.x)"

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _con(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(f"index not found: {self.path}")
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)

    def indexed(self):
        con = self._con()
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(notes)")}
            sel = "name, description, mtime" + (
                ", modified, modified_source" if "modified" in cols else ", NULL, NULL")
            counts = dict(con.execute("SELECT note_name, count(*) FROM chunks GROUP BY note_name"))
            return {r[0]: IndexedNote(r[0], r[1] or "", r[3], r[4], None,
                                      float(r[2]) if r[2] is not None else None,
                                      counts.get(r[0], 0))
                    for r in con.execute(f"SELECT {sel} FROM notes")}
        finally:
            con.close()

    def versions(self, name, limit=20):
        return []

    def centroids(self):
        con = self._con()
        try:
            acc: dict[str, list[list[float]]] = {}
            for name, emb in con.execute("SELECT note_name, embedding FROM chunks"):
                acc.setdefault(name, []).append(json.loads(emb))
        finally:
            con.close()
        return {k: _mean(v) for k, v in acc.items()}

    def near_duplicates(self, min_cosine, k):
        return _cos_pairs_exact(self.centroids(), min_cosine)

    def renames(self):
        return {}


class PgSource:
    """``core.store.Store`` (Postgres + pgvector). Near duplicates = k nearest chunks of
    each note's centroid through the generation's HNSW index (exact scan below the
    store's ``exact_max_chunks``), then the exact centroid cosine of those candidate
    pairs: O(n·k) instead of O(n²)."""
    label = "Postgres index"

    def __init__(self, store, generation: int | None = None):
        self.store = store
        self.generation = generation

    def _g(self):
        return self.store._require_gen(self.generation)

    def indexed(self):
        g = self.store.active_generation()
        with self.store.pool.connection() as con:
            rows = con.execute(
                "SELECT n.name, n.description, n.modified, n.modified_source, n.body, "
                "extract(epoch FROM n.updated_at) AS at, "
                + (f"(SELECT count(*) FROM {g.table} c WHERE c.note_id = n.id)" if g else "0")
                + " AS chunks FROM notes n").fetchall()
        return {r["name"]: IndexedNote(r["name"], r["description"], r["modified"],
                                       r["modified_source"], r["body"], float(r["at"]),
                                       int(r["chunks"])) for r in rows}

    def versions(self, name, limit=20):
        return self.store.note_versions(name, limit)

    def centroids(self):
        g = self._g()
        with self.store.pool.connection() as con:
            rows = con.execute(
                f"SELECT n.name, avg(c.embedding::vector) AS v FROM {g.table} c "
                "JOIN notes n ON n.id = c.note_id GROUP BY n.name").fetchall()
        out = {}
        for r in rows:
            v = r["v"]
            out[r["name"]] = _mean([v.to_list() if hasattr(v, "to_list") else list(v)])
        return out

    def near_duplicates(self, min_cosine, k):
        g = self._g()
        dim = int(g.dim)
        sql = f"""
            WITH cen AS MATERIALIZED (
                SELECT note_id, avg(embedding::vector) AS v FROM {g.table} GROUP BY note_id),
            cand AS (
                SELECT DISTINCT least(a.note_id, nb.note_id) AS x,
                                greatest(a.note_id, nb.note_id) AS y
                FROM cen a CROSS JOIN LATERAL (
                    SELECT c.note_id FROM {g.table} c
                    WHERE c.note_id <> a.note_id
                    ORDER BY (c.embedding::halfvec({dim})) <#> (a.v::halfvec({dim}))
                    LIMIT %(lim)s) nb)
            SELECT nx.name AS a, ny.name AS b, 1 - (cx.v <=> cy.v) AS cos
            FROM cand
            JOIN cen cx ON cx.note_id = cand.x JOIN cen cy ON cy.note_id = cand.y
            JOIN notes nx ON nx.id = cand.x JOIN notes ny ON ny.id = cand.y
            WHERE 1 - (cx.v <=> cy.v) >= %(min)s
            ORDER BY cos DESC"""
        with self.store.pool.connection() as con, con.transaction():
            # the neighbours of a centroid include its own chunks (filtered after the
            # index scan): ask the HNSW scan for enough candidates
            con.execute(f"SET LOCAL hnsw.ef_search = {max(40, 8 * int(k))}")
            ver = con.execute("SELECT extversion AS v FROM pg_extension WHERE extname = 'vector'"
                              ).fetchone()
            if ver and tuple(int(x) for x in ver["v"].split(".")[:2]) >= (0, 8):
                con.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            rows = con.execute(sql, {"lim": int(k) * 3, "min": float(min_cosine)}).fetchall()
        return [(r["a"], r["b"], float(r["cos"])) for r in rows]

    def renames(self):
        with self.store.pool.connection() as con:
            rows = con.execute(
                "SELECT old.note_name AS old, array_agg(DISTINCT cur.note_name) AS heirs "
                "FROM note_versions old JOIN note_versions cur "
                "ON cur.content_hash = old.content_hash AND cur.note_name <> old.note_name "
                "JOIN notes live ON live.name = cur.note_name "
                "WHERE NOT EXISTS (SELECT 1 FROM notes n WHERE n.name = old.note_name) "
                "GROUP BY old.note_name").fetchall()
        return {r["old"]: r["heirs"][0] for r in rows if len(r["heirs"]) == 1}


# ---------------------------------------------------------------------------- chain checks

@dataclass
class ChainConfig:
    """Where the sessions get their memory from. The checks must run in the SAME context
    as the sessions (same user, same configuration directory, same network): a reviewer
    in a container that sees another filesystem reports false failures."""
    server_name: str = "corthexis-memory"
    # (label, path) of JSON files with an ``mcpServers`` map, at least one must declare it
    mcp_configs: list[tuple[str, Path]] = field(default_factory=list)
    # how a session launches the server: {"command": ..., "args": [...], "env": {...}}
    launch: dict | None = None
    required_tools: tuple[str, ...] = ("memory_search", "memory_get")
    handshake_timeout: float = 30.0
    # recall hook: settings files and the substring its command must contain
    hook_settings: list[Path] = field(default_factory=list)
    hook_event: str = "UserPromptSubmit"
    hook_pattern: str = ""
    expect_hook: bool = False
    embed_urls: list[str] = field(default_factory=list)    # primary first
    rerank_url: str | None = None
    health_timeout: float = 4.0

    @classmethod
    def from_env(cls, **overrides) -> "ChainConfig":
        cfg = cls(
            server_name=env("REVIEW_MCP_SERVER", "corthexis-memory"),
            mcp_configs=[(p, Path(p).expanduser()) for p in env_list("REVIEW_MCP_CONFIGS", ":")],
            hook_settings=[Path(p).expanduser() for p in env_list("REVIEW_HOOK_SETTINGS", ":")],
            hook_pattern=env("REVIEW_HOOK_PATTERN", "") or "",
            expect_hook=env_bool("REVIEW_EXPECT_HOOK", False),
            embed_urls=env_list("EMBED_URLS"),
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg


def _servers(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def mcp_handshake(launch: dict, timeout: float = 30.0) -> tuple[set[str], str]:
    """Start the server the way a session does, list its tools. → (tools, error)."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "corthexis-review", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    try:
        proc = subprocess.Popen(
            [launch["command"], *launch.get("args", [])], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, **(launch.get("env") or {})}, cwd=launch.get("cwd"))
    except (OSError, KeyError) as exc:
        return set(), f"cannot start it ({exc})"
    tools: set[str] = set()
    err = ""
    answer: list[dict] = []
    done = threading.Event()

    def reader() -> None:
        # a thread, not select(): the text wrapper buffers lines select() cannot see
        for line in proc.stdout:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if payload.get("id") == 2:
                answer.append(payload)
                break
        done.set()

    try:
        assert proc.stdin and proc.stdout
        threading.Thread(target=reader, daemon=True).start()
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        if not done.wait(timeout):
            err = f"no answer within {timeout:.0f} s"
        elif not answer:
            err = "it exited before answering"
        else:
            tools = {t.get("name") for t in (answer[0].get("result") or {}).get("tools") or []}
    except (OSError, ValueError) as exc:
        err = f"handshake failed ({exc})"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return tools, err


def _health(url: str, timeout: float) -> str:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            r.read(256)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:80]}"


def chain_checks(cfg: ChainConfig) -> list[Finding]:
    """Is the memory actually reachable from a session? (declared, answers, hooked, models)"""
    out: list[Finding] = []
    if cfg.mcp_configs:
        declared = [label for label, p in cfg.mcp_configs if cfg.server_name in _servers(p)]
        if not declared:
            out.append(Finding(
                "mcp_not_declared", "crit", "chain", "Sessions start without memory",
                f"The memory server `{cfg.server_name}` is declared in none of the session "
                f"configurations ({', '.join(lbl for lbl, _p in cfg.mcp_configs)}). Sessions start "
                "normally and simply know less: nothing else will tell you.",
                remedy="Declare the memory server again in the sessions' configuration "
                       "(the installer does it; a later edit of the file removed it).",
                count=1))
    if cfg.launch:
        tools, err = mcp_handshake(cfg.launch, cfg.handshake_timeout)
        missing = sorted(set(cfg.required_tools) - tools)
        if err or missing:
            out.append(Finding(
                "mcp_unreachable", "crit", "chain", "The memory server does not answer",
                (f"Started the way a session starts it, the server {err}." if err else
                 f"The server answers but does not offer {', '.join(missing)}."),
                remedy="Check the server's logs and its dependencies; until it answers, sessions "
                       "run without memory.", count=1))
    if cfg.expect_hook and cfg.hook_pattern:
        found = False
        for p in cfg.hook_settings:
            try:
                hooks = (json.loads(p.read_text(encoding="utf-8")).get("hooks") or {})
            except (OSError, ValueError, AttributeError):
                continue
            for group in hooks.get(cfg.hook_event) or []:
                for h in (group or {}).get("hooks") or []:
                    if cfg.hook_pattern in str(h.get("command", "")):
                        found = True
        if not found:
            out.append(Finding(
                "hook_missing", "warn", "chain", "Automatic recall is not installed",
                f"No `{cfg.hook_event}` hook runs the recall in the sessions' settings: the memory "
                "is only consulted when the agent thinks of searching it.",
                remedy="Reinstall the recall hook (it is installed with the memory).", count=1))
    if cfg.embed_urls:
        down = [(u, e) for u in cfg.embed_urls if (e := _health(u, cfg.health_timeout))]
        if len(down) == len(cfg.embed_urls):
            out.append(Finding(
                "embed_down", "crit", "chain", "The embedding model is unreachable",
                f"None of the {len(cfg.embed_urls)} embedding server(s) answers: searches fall back "
                "to keywords only and new notes are not indexed.",
                remedy="Restart the embedding service.", count=len(down),
                items=[{"url_index": i} for i, _ in enumerate(down)]))
        elif down and down[0][0] == cfg.embed_urls[0]:
            out.append(Finding(
                "embed_fallback", "warn", "chain", "The memory runs on its backup model server",
                "The primary embedding server does not answer; a backup serves the searches, "
                "slower and possibly without reranking.",
                remedy="Restart the primary embedding server.", count=len(down)))
    if cfg.rerank_url and (e := _health(cfg.rerank_url, cfg.health_timeout)):
        out.append(Finding(
            "rerank_down", "info", "chain", "The reranker is unreachable",
            "Searches keep their hybrid order, without the final reordering.",
            remedy="Restart the reranking service.", count=1))
    return out


# ---------------------------------------------------------------------------- the review

def suggest_target(target: str, names: list[str], renames: dict[str, str]) -> str | None:
    """The note a broken ``[[target]]`` most likely meant, or None when it is not clear."""
    t = slugify(target.removesuffix(".md"))
    if t in renames:
        return renames[t]
    if target in renames:
        return renames[target]
    slugs = {slugify(n): n for n in names}
    contains = [n for s, n in slugs.items() if len(t) >= 6 and (s.startswith(t + "-")
                                                                   or s.endswith("-" + t))]
    if len(contains) == 1:
        return contains[0]
    close = difflib.get_close_matches(t, list(slugs), n=2, cutoff=0.82)
    if len(close) == 1 or (len(close) == 2 and difflib.SequenceMatcher(None, t, close[0]).ratio()
                           - difflib.SequenceMatcher(None, t, close[1]).ratio() > 0.08):
        return slugs[close[0]]
    return None


def _age_days(modified: str | None, now: datetime.datetime) -> int | None:
    d = parse_dt(modified)
    return None if d is None else max(0, (now - d).days)


def _norm_ws(s: str | None) -> str:
    return " ".join((s or "").split())


def _drift_is_correction(d) -> bool:
    """A description that only got SHORTER ("Visitors park in lot B" → "Lot B for visitors")
    corrected nothing: one dropped word still in the body is a rewording, not a stale fact.
    Kept: the description gained a fact, or several dropped words are still asserted."""
    from .drift import terms
    gained = terms(d.new_description) - terms(d.old_description)
    return bool(gained) or len(d.stale_terms) >= 2


def run_review(cfg: ReviewConfig, source: ReviewSource | None = None, *,
               chain: list[Finding] | None = None, notes: list[CorpusNote] | None = None,
               now: datetime.datetime | None = None) -> dict:
    """Every check in one pass. Returns a JSON-serialisable report."""
    t0 = time.monotonic()
    now = now or utcnow()
    notes = notes if notes is not None else load_corpus(cfg.memory_dir, cfg.index_filename)
    resolve = Resolver(notes)
    by_name = {n.name: n for n in notes}
    findings: list[Finding] = list(chain or [])
    skipped: list[str] = []
    cap = cfg.max_items

    def add(f: Finding) -> None:
        f.items = f.items[:cap]
        findings.append(f)

    # --- the index: readable, in sync, up to date
    indexed: dict[str, IndexedNote] = {}
    if source is None:
        skipped.append("index")
    else:
        try:
            indexed = source.indexed()
        except Exception as exc:  # noqa: BLE001
            add(Finding(
                "index_unreadable", "crit", "chain", "The memory index cannot be read",
                f"{source.label}: {str(exc)[:200]}. Searches run degraded or not at all.",
                remedy="Check that the database is running, then rebuild the index.", count=1))
            source = None
    if source is not None:
        missing = sorted(n.name for n in notes if n.name not in indexed
                         and now.timestamp() - n.mtime > cfg.grace_s)
        gone = sorted(k for k in indexed if k not in by_name)
        if missing or gone:
            add(Finding(
                "index_desync", "crit", "chain", "Index out of sync with the notes",
                f"{len(missing)} note(s) on disk are missing from the index, {len(gone)} indexed "
                "note(s) have no file any more.",
                notes=missing + gone, count=len(missing) + len(gone),
                items=[{"note": n, "state": "not indexed"} for n in missing]
                + [{"note": n, "state": "file gone"} for n in gone],
                remedy="The indexer has stopped or fails on a note: check its log, then reindex."))
        lag = []
        for n in notes:
            ix = indexed.get(n.name)
            if not ix or now.timestamp() - n.mtime < cfg.index_lag_min * 60:
                continue
            if ix.body is not None:
                stale = (_norm_ws(ix.body) != _norm_ws(n.body)
                         or _norm_ws(ix.description) != _norm_ws(n.description))
            else:
                stale = ix.indexed_at is not None and n.mtime - ix.indexed_at > cfg.index_lag_min * 60
            if stale:
                lag.append(n.name)
        if lag:
            add(Finding(
                "index_lag", "warn", "chain", "Indexer behind",
                f"{len(lag)} note(s) changed more than {cfg.index_lag_min} min ago and the index "
                "still serves the previous version.",
                notes=sorted(lag), count=len(lag), items=[{"note": n} for n in sorted(lag)],
                remedy="The indexer is not watching the folder any more: restart it."))

    # --- structure: frontmatter, description, type, naming
    fm_bad = [(n.name, w) for n in notes for w in n.parsed.warnings if w != "empty description"]
    if fm_bad:
        names = sorted({x for x, _ in fm_bad})
        add(Finding(
            "frontmatter", "warn", "structure", "Damaged note header",
            f"{len(names)} note(s) with an unreadable header, text above it or no header at all: "
            "the parser falls back and recall drops.",
            notes=names, count=len(names), items=[{"note": a, "problem": w} for a, w in fm_bad],
            remedy="Quote the description when it contains « : », put the `---` block back at the "
                   "top of the file.", judgement=True))
    no_desc = sorted(n.name for n in notes if not n.description)
    if no_desc:
        add(Finding(
            "no_description", "crit", "structure", "Notes without a description",
            f"{len(no_desc)} note(s) are silent in the index and get no recall boost.",
            notes=no_desc, count=len(no_desc), items=[{"note": n} for n in no_desc],
            remedy="Write a one-line `description:` — it is both the index entry and the main "
                   "recall lever.", judgement=True))
    no_type = sorted(n.name for n in notes if n.type in ("unknown", "", "None"))
    if no_type:
        add(Finding(
            "no_type", "info", "structure", "Notes without a type",
            f"{len(no_type)} note(s) have no `metadata.type` (user / feedback / project / "
            "reference) and escape the sorting of rules and projects.",
            notes=no_type, count=len(no_type), items=[{"note": n} for n in no_type],
            remedy="Add `metadata: type: …` to the header."))
    naming = []
    for n in notes:
        if now.timestamp() - n.mtime < cfg.grace_s:
            continue
        want_name = n.name if is_kebab(n.name) else slugify(n.name)
        ok_files = {filename_for(want_name)}
        if cfg.accept_kebab_files:
            ok_files.add(want_name + ".md")
        if want_name != n.name or n.path.name not in ok_files:
            if want_name in by_name and want_name != n.name:
                continue  # the conventional name is taken: a merge, not a rename
            naming.append({"note": n.name, "file": n.path.name, "name": want_name,
                           "expected": filename_for(want_name)})
    if naming:
        add(Finding(
            "naming", "warn", "structure", "Notes outside the naming convention",
            f"{len(naming)} note(s) whose name is not in kebab-case or whose file does not follow "
            "its name: links to them break and duplicates appear.",
            notes=[x["note"] for x in naming], count=len(naming), items=naming, action="rename",
            remedy="Rename the file after the note (`my-note` lives in `my_note.md`); links are "
                   "rewritten in the whole memory."))
    giants = sorted(((n.words, n.name) for n in notes if n.words > cfg.giant_words), reverse=True)
    if giants:
        add(Finding(
            "giant_notes", "info", "structure", "Very long notes",
            f"{len(giants)} note(s) exceed {cfg.giant_words} words: "
            + ", ".join(f"{n} ({w})" for w, n in giants[:5]),
            notes=[n for _w, n in giants], count=len(giants),
            items=[{"note": n, "words": w} for w, n in giants],
            remedy="Split by sub-topic: recall works per passage and dilutes beyond a few thousand "
                   "words.", judgement=True))

    # --- graph: broken links, orphans
    renames: dict[str, str] = {}
    if source is not None:
        try:
            renames = source.renames()
        except Exception:  # noqa: BLE001 — suggestions are a bonus
            renames = {}
    names = [n.name for n in notes]
    broken: dict[str, list[str]] = {}
    for n in notes:
        for t in n.links:
            if not resolve(t):
                broken.setdefault(t, []).append(n.name)
    if broken:
        items = []
        for t, srcs in sorted(broken.items()):
            sug = suggest_target(t, names, renames)
            for s in srcs:
                items.append({"note": s, "target": t, "suggestion": sug})
        cited = sorted({s for v in broken.values() for s in v})
        add(Finding(
            "broken_links", "warn", "graph", "Links to notes that do not exist",
            f"{len(broken)} target(s) not found, cited by {len(cited)} note(s): "
            + ", ".join(f"[[{k}]]" for k in sorted(broken)[:12]) + ("…" if len(broken) > 12 else ""),
            notes=cited, count=len(broken), items=items, action="relink",
            judgement=any(i["suggestion"] is None for i in items),
            remedy="Point the link to the note that replaced the old one, or write the missing note "
                   "if the subject was handled without one."))
    inbound: dict[str, int] = {}
    for n in notes:
        for t in n.links:
            r = resolve(t)
            if r and r.name != n.name:
                inbound[r.name] = inbound.get(r.name, 0) + 1
    orphans = sorted(n.name for n in notes if not any(resolve(t) for t in n.links)
                     and not inbound.get(n.name) and n.type not in ("feedback", "user"))
    if orphans:
        add(Finding(
            "orphans", "info", "graph", "Isolated notes",
            f"{len(orphans)} note(s) with no link in or out: they are found by search only, never "
            "by following a link.",
            notes=orphans, count=len(orphans), items=[{"note": n} for n in orphans],
            remedy="Link the note from its parent project note, and back.", judgement=True))

    # --- dates
    if source is not None and indexed:
        undated = sorted(k for k, v in indexed.items() if k in by_name and not v.modified)
        if len(undated) > cfg.undated_tolerated:
            add(Finding(
                "undated", "warn", "dates", "Notes without a date",
                f"{len(undated)} note(s) come out of the index with no age (tolerated: "
                f"{cfg.undated_tolerated}): an old note reads like yesterday's.",
                notes=undated, count=len(undated), items=[{"note": n} for n in undated],
                remedy="Add `metadata.modified` (ISO date) to the header, or let the indexer date "
                       "the next change."))
        approx = sorted(k for k, v in indexed.items() if k in by_name
                        and (v.modified_source or "") in ("inferred", "migrated-mtime"))
        if approx:
            add(Finding(
                "dates_reconstructed", "info", "dates", "Reconstructed dates",
                f"{len(approx)} note(s) carry a date deduced from their content or file (an order "
                "of magnitude, the exact day is not guaranteed).",
                notes=approx, count=len(approx), items=[{"note": n} for n in approx],
                remedy="Stamp `metadata.modified` for real the next time the note is reviewed."))
    if len(notes) > 20 and len({round(n.mtime) for n in notes}) <= 2:
        add(Finding(
            "mass_rewrite", "warn", "dates", "The whole memory was rewritten at once",
            "Every note has the same modification time: a script went over the folder.",
            remedy="Check that the notes' dates (`metadata.modified`) were not overwritten.",
            count=1, judgement=True))

    # --- drift: dormant projects, description != body, dead paths, near duplicates
    stale = []
    for n in notes:
        if n.type != "project":
            continue
        ix = indexed.get(n.name)
        age = _age_days((ix.modified if ix else None) or n.parsed.modified, now)
        body = strip_code(n.body)
        if age is not None and age > cfg.stale_days and OPEN_MARKERS.search(body) \
                and not CLOSED_MARKERS.search(body):
            stale.append({"note": n.name, "age_days": age,
                          "marker": OPEN_MARKERS.search(body).group(0)})
    if stale:
        stale.sort(key=lambda x: -x["age_days"])
        add(Finding(
            "stale_open", "warn", "drift", "Dormant projects still marked as open",
            f"{len(stale)} project note(s) older than {cfg.stale_days} days still say “to do” / "
            "“pending”: either it is done and the note says otherwise, or it was dropped and "
            "nothing says so.",
            notes=[x["note"] for x in stale], count=len(stale), items=stale, action="close",
            remedy="Read it, then close it (“closed on …”) or state that it was abandoned — in the "
                   "body, not only in the description."))
    if source is not None:
        drifts = []
        for n in notes:
            try:
                versions = source.versions(n.name, 20)
            except Exception:  # noqa: BLE001
                versions = []
            d = check_versions(n.name, versions) if versions else None
            if d and _drift_is_correction(d):
                drifts.append(d)
        if drifts:
            sev = "warn" if any(d.severity in ("high", "warning") for d in drifts) else "info"
            add(Finding(
                "description_drift", sev, "drift", "Description corrected, body left behind",
                f"{len(drifts)} note(s) whose description changed while the paragraph carrying the "
                "same fact did not: recall serves the old paragraph with confidence.",
                notes=[d.note for d in drifts], count=len(drifts), judgement=True,
                items=[{"note": d.note, "severity": d.severity, "message": d.message,
                        "paragraph_index": d.paragraph_index, "stale_terms": d.stale_terms}
                       for d in drifts],
                remedy="Correct the paragraph in the body too (or remove it if it is obsolete)."))
    if cfg.dead_path_roots and cfg.dead_path_prefixes:
        dead = []
        for n in notes:
            for p in find_dead_paths(n.body, cfg.dead_path_roots, cfg.dead_path_prefixes):
                dead.append({"note": n.name, "path": p})
        if dead:
            cited = sorted({d["note"] for d in dead})
            add(Finding(
                "dead_paths", "info", "drift", "Files cited by notes have disappeared",
                f"{len(dead)} path(s) cited in {len(cited)} note(s) exist in none of the "
                f"{len(cfg.dead_path_roots)} folder(s) checked: the code moved, the note did not.",
                notes=cited, count=len(dead), items=dead, judgement=True,
                remedy="Update the path in the note, or write “(removed)” after it if the component "
                       "is gone."))
    else:
        skipped.append("dead_paths")
    if source is not None:
        try:
            pairs = source.near_duplicates(cfg.dup_cosine, cfg.dup_k)
        except Exception as exc:  # noqa: BLE001
            pairs = []
            skipped.append(f"near_duplicates ({str(exc)[:80]})")
        pairs = [(a, b, c) for a, b, c in pairs if a in by_name and b in by_name]
        if pairs:
            add(Finding(
                "near_duplicates", "info", "drift", "Notes that say almost the same thing",
                f"{len(pairs)} pair(s) above {cfg.dup_cosine:.2f} similarity: "
                + "; ".join(f"{a} ≈ {b} ({c:.2f})" for a, b, c in pairs[:6])
                + ("…" if len(pairs) > 6 else ""),
                notes=sorted({x for a, b, _c in pairs for x in (a, b)}), count=len(pairs),
                items=[{"note": a, "other": b, "cosine": round(c, 3)} for a, b, c in pairs],
                action="merge",
                remedy="Merge them, or make one point to the other: two notes that say the same "
                       "thing end up contradicting each other."))

    # --- security
    leaks = []
    injections = []
    for n in notes:
        text = n.parsed.description + "\n" + n.body
        for label, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                if _looks_random(m.group(0)):
                    line = text.count("\n", 0, m.start())   # 0 = the description
                    leaks.append({"note": n.name, "kind": label, "line": line})
                    break
        for m in INJECTION_RE.finditer(strip_code(n.body)):
            injections.append({"note": n.name, "line": n.body.count("\n", 0, m.start()) + 1})
            break
    if leaks:
        cited = sorted({x["note"] for x in leaks})
        add(Finding(
            "secrets", "crit", "security", "Probable secret written in a note",
            f"{len(cited)} note(s) contain a string that looks like a key: "
            + "; ".join(f"{x['note']} ({x['kind']})" for x in leaks[:6]),
            notes=cited, count=len(cited), items=leaks, judgement=True,
            remedy="Replace the value by a pointer to your secret store, then revoke and rotate "
                   "the key: it has been readable by every session."))
    if injections:
        cited = sorted({x["note"] for x in injections})
        add(Finding(
            "injection", "warn", "security", "Instructions addressed to the agent",
            f"{len(cited)} note(s) contain text that speaks to the model (“ignore the previous "
            "instructions”…). Notes are injected into every session: such text is obeyed, not read.",
            notes=cited, count=len(cited), items=injections, judgement=True,
            remedy="Rewrite it as a fact about the project, or quote it in a code block if the note "
                   "documents an attack."))

    order = {"crit": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.severity], -f.count))
    score = health_score(findings)
    sig = signature(findings)
    flags: dict[str, list[str]] = {}
    for f in findings:
        for n in f.notes:
            flags.setdefault(n, []).append(f.id)
    return {
        "at": now.isoformat(timespec="seconds"),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "score": score,
        "signature": sig,
        "counts": {k: sum(1 for f in findings if f.severity == k) for k in SEVERITIES},
        "notes_total": len(notes),
        "source": source.label if source is not None else None,
        "skipped": skipped,
        "findings": [asdict(f) for f in findings],
        "flags": flags,
    }


def health_score(findings: Iterable[Finding | dict]) -> int:
    penalty = 0.0
    for f in findings:
        sev = f["severity"] if isinstance(f, dict) else f.severity
        cnt = f["count"] if isinstance(f, dict) else f.count
        penalty += SEVERITY_WEIGHT[sev] * (1 + math.log1p(max(cnt, 1)) / 3)
    return max(0, min(100, round(100 - penalty)))


def signature(findings: Iterable[Finding | dict]) -> str:
    rows = []
    for f in findings:
        d = f if isinstance(f, dict) else asdict(f)
        rows.append((d["id"], d["severity"], d["count"], sorted(d["notes"])))
    return hashlib.sha1(json.dumps(sorted(rows)).encode()).hexdigest()[:12]


def finding_keys(report: dict) -> dict[str, dict]:
    """One key per (check, note) problem — what the history tracks over time."""
    out = {}
    for f in report["findings"]:
        for n in (f["notes"] or ["*"]):
            out[f"{f['id']}:{n}"] = {"check": f["id"], "note": n, "severity": f["severity"]}
    return out


# ---------------------------------------------------------------------------- history

class _HistoryBase:
    """Score over time + open problems with their age + key/value state (alerts)."""

    def summary(self, now: datetime.datetime | None = None) -> dict:
        now = now or utcnow()
        open_ = self.open_problems()
        week = sum(1 for p in open_ if (now - parse_dt(p["first_seen"])).days >= 7)
        fixed = self.fixed_durations()
        return {"open": len(open_), "open_over_7d": week,
                "fixed": len(fixed),
                "mean_fix_hours": round(sum(fixed) / len(fixed) / 3600, 1) if fixed else None}


class SqliteHistory(_HistoryBase):
    def __init__(self, path: Path | str, retention_days: int = 90):
        self.path = Path(path)
        self.retention = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS review_runs(
                    at TEXT PRIMARY KEY, score INTEGER, crit INTEGER, warn INTEGER, info INTEGER,
                    signature TEXT, notes INTEGER, report TEXT);
                CREATE TABLE IF NOT EXISTS review_problems(
                    key TEXT PRIMARY KEY, check_id TEXT, note TEXT, severity TEXT,
                    first_seen TEXT, last_seen TEXT, resolved_at TEXT);
                CREATE TABLE IF NOT EXISTS review_state(key TEXT PRIMARY KEY, value TEXT);
            """)

    def _con(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, report: dict) -> None:
        keys = finding_keys(report)
        at = report["at"]
        with self._con() as con:
            c = report["counts"]
            con.execute("INSERT OR REPLACE INTO review_runs VALUES(?,?,?,?,?,?,?,?)",
                        (at, report["score"], c["crit"], c["warn"], c["info"], report["signature"],
                         report["notes_total"], json.dumps(report)))
            cutoff = (parse_dt(at) - datetime.timedelta(days=self.retention)).isoformat()
            con.execute("DELETE FROM review_runs WHERE at < ?", (cutoff,))
            open_ = {r[0] for r in con.execute(
                "SELECT key FROM review_problems WHERE resolved_at IS NULL")}
            for k, v in keys.items():
                if k in open_:
                    con.execute("UPDATE review_problems SET last_seen=?, severity=? WHERE key=?",
                                (at, v["severity"], k))
                else:
                    con.execute("INSERT OR REPLACE INTO review_problems VALUES(?,?,?,?,?,?,NULL)",
                                (k, v["check"], v["note"], v["severity"], at, at))
            for k in open_ - set(keys):
                con.execute("UPDATE review_problems SET resolved_at=? WHERE key=?", (at, k))

    def series(self, limit: int = 400) -> list[dict]:
        with self._con() as con:
            rows = con.execute("SELECT at, score, crit, warn, info, notes FROM review_runs "
                               "ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("at", "score", "crit", "warn", "info", "notes"), r))
                for r in reversed(rows)]

    def last_report(self) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT report FROM review_runs ORDER BY at DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else None

    def open_problems(self) -> list[dict]:
        with self._con() as con:
            rows = con.execute("SELECT key, check_id, note, severity, first_seen FROM "
                               "review_problems WHERE resolved_at IS NULL").fetchall()
        return [dict(zip(("key", "check", "note", "severity", "first_seen"), r)) for r in rows]

    def fixed_durations(self) -> list[float]:
        with self._con() as con:
            rows = con.execute("SELECT first_seen, resolved_at FROM review_problems "
                               "WHERE resolved_at IS NOT NULL").fetchall()
        return [(parse_dt(b) - parse_dt(a)).total_seconds() for a, b in rows]

    def get(self, key: str) -> str | None:
        with self._con() as con:
            r = con.execute("SELECT value FROM review_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set(self, key: str, value: str) -> None:
        with self._con() as con:
            con.execute("INSERT OR REPLACE INTO review_state VALUES(?,?)", (key, value))


class PgHistory(_HistoryBase):
    """Same thing in the memory store (tables of migration 0005_review)."""

    def __init__(self, store, retention_days: int = 90):
        self.store = store
        self.retention = retention_days

    def record(self, report: dict) -> None:
        from psycopg.types.json import Jsonb
        keys = finding_keys(report)
        at = parse_dt(report["at"])
        c = report["counts"]
        with self.store.pool.connection() as con, con.transaction():
            con.execute(
                "INSERT INTO review_runs(at, score, crit, warn, info, signature, notes, report) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (at) DO UPDATE SET "
                "score = EXCLUDED.score, report = EXCLUDED.report",
                (at, report["score"], c["crit"], c["warn"], c["info"], report["signature"],
                 report["notes_total"], Jsonb(report)))
            con.execute("DELETE FROM review_runs WHERE at < %s",
                        (at - datetime.timedelta(days=self.retention),))
            open_ = {r["key"] for r in con.execute(
                "SELECT key FROM review_problems WHERE resolved_at IS NULL").fetchall()}
            for k, v in keys.items():
                con.execute(
                    "INSERT INTO review_problems(key, check_id, note, severity, first_seen, "
                    "last_seen) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE SET "
                    "last_seen = EXCLUDED.last_seen, severity = EXCLUDED.severity, "
                    "first_seen = CASE WHEN review_problems.resolved_at IS NULL "
                    "THEN review_problems.first_seen ELSE EXCLUDED.first_seen END, "
                    "resolved_at = NULL", (k, v["check"], v["note"], v["severity"], at, at))
            gone = sorted(open_ - set(keys))
            if gone:
                con.execute("UPDATE review_problems SET resolved_at = %s WHERE key = ANY(%s)",
                            (at, gone))

    def series(self, limit: int = 400) -> list[dict]:
        with self.store.pool.connection() as con:
            rows = con.execute("SELECT at, score, crit, warn, info, notes FROM review_runs "
                               "ORDER BY at DESC LIMIT %s", (limit,)).fetchall()
        return [{**r, "at": r["at"].isoformat(timespec="seconds")} for r in reversed(rows)]

    def last_report(self) -> dict | None:
        with self.store.pool.connection() as con:
            r = con.execute("SELECT report FROM review_runs ORDER BY at DESC LIMIT 1").fetchone()
        return r["report"] if r else None

    def open_problems(self) -> list[dict]:
        with self.store.pool.connection() as con:
            rows = con.execute("SELECT key, check_id AS check, note, severity, first_seen "
                               "FROM review_problems WHERE resolved_at IS NULL").fetchall()
        return [{**r, "first_seen": r["first_seen"].isoformat()} for r in rows]

    def fixed_durations(self) -> list[float]:
        with self.store.pool.connection() as con:
            rows = con.execute("SELECT extract(epoch FROM resolved_at - first_seen) AS s "
                               "FROM review_problems WHERE resolved_at IS NOT NULL").fetchall()
        return [float(r["s"]) for r in rows]

    def get(self, key: str) -> str | None:
        with self.store.pool.connection() as con:
            r = con.execute("SELECT value FROM review_state WHERE key = %s", (key,)).fetchone()
        return r["value"] if r else None

    def set(self, key: str, value: str) -> None:
        with self.store.pool.connection() as con, con.transaction():
            con.execute("INSERT INTO review_state(key, value) VALUES (%s, %s) ON CONFLICT (key) "
                        "DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (key, value))


# ---------------------------------------------------------------------------- alerts

@dataclass
class AlertPolicy:
    digest_hour: int = 8
    digest_minute: int = 20
    critical_cooldown_h: float = 6.0
    tz: str = "UTC"

    @classmethod
    def from_env(cls) -> "AlertPolicy":
        h, _, m = (env("REVIEW_DIGEST_AT", "08:20") or "08:20").partition(":")
        try:
            hour, minute = int(h), int(m or 0)
        except ValueError:
            hour, minute = 8, 20
        try:
            cool = float(env("REVIEW_CRIT_COOLDOWN_H", "6") or 6)
        except ValueError:
            cool = 6.0
        return cls(hour, minute, cool, env("REVIEW_TZ") or os.environ.get("TZ") or "UTC")


def alert_decision(report: dict, state, policy: AlertPolicy,
                   now: datetime.datetime | None = None) -> str | None:
    """"digest" once a day at the digest time when the findings changed since the last
    message; "critical" right away when a critical finding is new, at most once per
    cooldown; None otherwise. ``state`` = a history (get/set)."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(policy.tz)
    except Exception:  # noqa: BLE001
        tz = datetime.timezone.utc
    now = (now or utcnow()).astimezone(tz)
    last_sig = state.get("alert_signature")
    last_at = parse_dt(state.get("alert_at"))
    last_digest = parse_dt(state.get("digest_at"))
    slot = now.replace(hour=policy.digest_hour, minute=policy.digest_minute, second=0,
                       microsecond=0)
    if report["signature"] == last_sig:
        return None
    if now >= slot and (last_digest is None or last_digest.astimezone(tz).date() < now.date()):
        return "digest"
    crit_ids = sorted(f["id"] for f in report["findings"] if f["severity"] == "crit")
    known = set(json.loads(state.get("alert_crit_ids") or "[]"))
    if crit_ids and set(crit_ids) - known:
        if last_at is None or now - last_at >= datetime.timedelta(hours=policy.critical_cooldown_h):
            return "critical"
    return None


def mark_alerted(report: dict, state, kind: str, now: datetime.datetime | None = None) -> None:
    now = now or utcnow()
    state.set("alert_signature", report["signature"])
    state.set("alert_at", now.isoformat())
    state.set("alert_crit_ids", json.dumps(sorted(
        f["id"] for f in report["findings"] if f["severity"] == "crit")))
    if kind == "digest":
        state.set("digest_at", now.isoformat())


def format_digest(report: dict, link: str = "", kind: str = "digest") -> tuple[str, str]:
    """(title, body) in plain text, for any channel."""
    c = report["counts"]
    head = "critical memory problem" if kind == "critical" else "daily memory review"
    title = f"CortHeXis — {head}: health {report['score']}/100"
    lines = [f"{report['notes_total']} notes · {c['crit']} critical · {c['warn']} warnings · "
             f"{c['info']} to watch"]
    for f in report["findings"]:
        if f["severity"] == "info":
            continue
        mark = "[critical]" if f["severity"] == "crit" else "[warning]"
        lines.append(f"{mark} {f['title']} — {f['detail'][:200]}")
    if len(lines) == 1:
        lines.append("Nothing critical; the details are in the CortHeXis tab.")
    body = "\n".join(lines[:9])
    return title, body + (f"\n{link}" if link else "")


def main(argv: list[str] | None = None) -> int:
    """``python -m core.review [--chain]``: review the configured memory, JSON on stdout,
    exit 1 when there is a critical finding. Run it in the sessions' context."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--chain", action="store_true", help="also check the session chain")
    ap.add_argument("--sqlite", help="2.x SQLite index to read instead of Postgres")
    args = ap.parse_args(argv)
    cfg = ReviewConfig.from_env()
    source = None
    if args.sqlite:
        source = SqliteSource(args.sqlite)
    elif env("DATABASE_URL"):
        from .store import Store
        source = PgSource(Store())
    chain = chain_checks(ChainConfig.from_env()) if args.chain else []
    rep = run_review(cfg, source, chain=chain)
    json.dump(rep, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 1 if rep["counts"]["crit"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
