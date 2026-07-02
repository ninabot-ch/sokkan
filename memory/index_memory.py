#!/usr/bin/env python3
"""index_memory.py — SOKKAN P0 : index the Claude Code memory notes for semantic recall.

Reads every memory note (`<memory_dir>/*.md`, excluding the MEMORY.md index),
splits each into chunks, embeds them via the ninjob ML service
(``POST {ML_SERVICE_URL}/api/v1/embed/text`` — multilingual MiniLM, 384-dim,
cross-lingual so FR-authored notes match queries in any language), and stores
unit-normalized vectors in a local SQLite DB. The MCP server
(``memory_search_server.py``) then does cosine = dot-product top-k at query time.

Incremental: a note is re-embedded only when its file mtime changes; notes whose
files disappeared are pruned. Tiny corpus (~150 notes / a few hundred chunks) so
a full run takes a few seconds.

    ML_SERVICE_URL=http://rog1:8001 SOKKAN_MEMORY_DB=/root/.local/share/sokkan/memory.db \
        /opt/sokkan/venv/bin/python index_memory.py [--rebuild]

Re-run on a timer (sokkan-memory-index.timer) or after editing notes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

import yaml

import embeddings

MEMORY_DIR = Path(
    os.environ.get(
        "SOKKAN_MEMORY_DIR",
        os.path.expanduser("~/.sokkan/memory"),
    )
)
DB_PATH = Path(os.environ.get("SOKKAN_MEMORY_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "memory.db")))
MODEL = os.environ.get("SOKKAN_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
CHUNK_TARGET = 1200  # chars; notes longer than this are split on paragraph boundaries
BATCH = 64
# MEMORY.md is loaded whole into every session's context; the harness truncates
# past ~24.4KB, so the generated index must stay under budget (UTF-8 bytes).
INDEX_BUDGET = 24_000

FRONTMATTER_RE = "---"


def parse_note(path: Path) -> dict:
    """Return {name, description, type, body} for a memory note."""
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    ntype = "unknown"
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_raw = text[3:end]
            body = text[end + 4 :].lstrip("\n")
            try:
                fm = yaml.safe_load(fm_raw) or {}
                name = fm.get("name", name)
                description = fm.get("description", "") or ""
                meta = fm.get("metadata", {}) or {}
                ntype = meta.get("type", meta.get("node_type", "unknown"))
            except yaml.YAMLError:
                pass
    return {"name": name, "description": description, "type": ntype, "body": body.strip()}


def chunk_body(body: str) -> list[str]:
    """Split a note body into ~CHUNK_TARGET-char chunks on paragraph boundaries."""
    if len(body) <= CHUNK_TARGET:
        return [body] if body else []
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > CHUNK_TARGET:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def embed_text(name: str, description: str, chunk: str) -> str:
    """Prepend name + description (keywords lift recall, cf. build_nina_kb.py)."""
    return f"{name}. {description}. {chunk}"


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed_batch(texts: list[str]) -> list[list[float]]:
    return embeddings.embed_texts(texts)  # remote (ML_SERVICE_URL) ou fastembed local


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes (
            name TEXT PRIMARY KEY,
            description TEXT,
            type TEXT,
            mtime REAL,
            source_path TEXT
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_name TEXT NOT NULL REFERENCES notes(name) ON DELETE CASCADE,
            chunk_idx INTEGER NOT NULL,
            body TEXT NOT NULL,
            embedding TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_note ON chunks(note_name);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    con.commit()


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[: cap - 1]
    sp = cut.rfind(" ")
    if sp > cap // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:—-") + "…"


def write_memory_index(con: sqlite3.Connection) -> None:
    """Regenerate MEMORY.md from the notes' frontmatter descriptions.

    MEMORY.md becomes a derived artifact: one line per note, descriptions
    truncated with the largest cap that keeps the whole file under
    INDEX_BUDGET bytes. Written only when the content actually changed, so
    the inotify .path unit doesn't retrigger the service in a loop.
    """
    rows = con.execute(
        "SELECT name, description, source_path FROM notes ORDER BY name COLLATE NOCASE"
    ).fetchall()
    header = (
        "<!-- Auto-generated by sokkan memory/index_memory.py from the notes' "
        "frontmatter descriptions — do not edit by hand. Details: memory_search / "
        "memory_get (sokkan-memory MCP) or the notes themselves. -->\n\n"
    )
    for cap in range(200, 39, -10):
        lines = []
        for name, description, source_path in rows:
            fname = Path(source_path).name
            desc = _truncate(" ".join(description.split()), cap)
            lines.append(f"- [{name}]({fname}) — {desc}" if desc else f"- [{name}]({fname})")
        content = header + "\n".join(lines) + "\n"
        if len(content.encode("utf-8")) <= INDEX_BUDGET:
            break

    index_path = MEMORY_DIR / "MEMORY.md"
    if index_path.exists() and index_path.read_text(encoding="utf-8") == content:
        return
    index_path.write_text(content, encoding="utf-8")
    print(f"MEMORY.md régénéré: {len(rows)} entrées, cap {cap}, {len(content.encode('utf-8'))} bytes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="drop and re-embed everything")
    args = ap.parse_args()

    if not MEMORY_DIR.is_dir():
        print(f"memory dir not found: {MEMORY_DIR}", file=sys.stderr)
        return 1
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    init_db(con)
    if args.rebuild:
        con.executescript("DELETE FROM chunks; DELETE FROM notes;")
        con.commit()

    files = sorted(p for p in MEMORY_DIR.glob("*.md") if p.name != "MEMORY.md")
    seen: set[str] = set()
    known = {row[0]: row[1] for row in con.execute("SELECT name, mtime FROM notes")}
    reindexed = skipped = 0

    for path in files:
        note = parse_note(path)
        name = note["name"]
        seen.add(name)
        mtime = path.stat().st_mtime
        if known.get(name) == mtime:
            skipped += 1
            continue

        chunks = chunk_body(note["body"])
        if not chunks:
            continue
        texts = [embed_text(name, note["description"], c) for c in chunks]
        vecs = embed_batch(texts)

        con.execute("DELETE FROM chunks WHERE note_name = ?", (name,))
        con.execute(
            "INSERT INTO notes(name, description, type, mtime, source_path) "
            "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "description=excluded.description, type=excluded.type, "
            "mtime=excluded.mtime, source_path=excluded.source_path",
            (name, note["description"], note["type"], mtime, str(path)),
        )
        con.executemany(
            "INSERT INTO chunks(note_name, chunk_idx, body, embedding) VALUES(?,?,?,?)",
            [(name, i, c, json.dumps(v)) for i, (c, v) in enumerate(zip(chunks, vecs))],
        )
        con.commit()
        reindexed += 1
        print(f"  · {name}: {len(chunks)} chunk(s)")

    # prune notes whose files disappeared
    pruned = [n for n in known if n not in seen]
    for n in pruned:
        con.execute("DELETE FROM chunks WHERE note_name = ?", (n,))
        con.execute("DELETE FROM notes WHERE name = ?", (n,))
    con.execute(
        "INSERT INTO meta(key,value) VALUES('model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (MODEL,),
    )
    con.commit()

    write_memory_index(con)

    n_notes = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    print(
        f"done: {reindexed} reindexed, {skipped} unchanged, {len(pruned)} pruned "
        f"→ {n_notes} notes / {n_chunks} chunks @ {DB_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
