"""What the truth/indexer code needs from the store and the embedder.

The records mirror the interface contract between the memory engine's parts
(store / embed / truth). The indexer only reads attributes, so the store may define
its own classes with the same fields; these are the reference definitions.

``IndexStore`` lists every store method the indexer calls. The methods marked
*extension* are not in the written contract yet: they are what incremental
indexing, generations and the date history need (see the truth report).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Where a note's date comes from.
#   frontmatter     metadata.modified, written on purpose
#   indexed         measured: the first time the indexer saw this content
#   migrated-mtime  file mtime imported once from a 2.x install (an approximation)
#   inferred        reconstructed (e.g. last date quoted in the body), never a measure
#   transcript      reconstructed from the session transcripts (a real write, day exact)
#   inconnue        unknown: content found at bootstrap with no declared date
DATE_SOURCES = ("frontmatter", "indexed", "migrated-mtime", "inferred", "transcript", "inconnue")
MEASURED_SOURCES = ("frontmatter", "indexed", "transcript")

# Project a note belongs to (SOKKAN 3.2 multi-user). Every note indexed before 3.2, and every
# note of a single-project install, is in DEFAULT_PROJECT. A recall / search given a project
# scope never returns a note of another project (fail-closed: a note without a project counts
# as DEFAULT_PROJECT, never as "everywhere").
DEFAULT_PROJECT = "default"


@dataclass
class NoteRecord:
    name: str
    description: str
    type: str
    priority: int
    source_path: str
    modified: str | None          # ISO 8601, effective date (see core.dates)
    modified_source: str          # one of DATE_SOURCES
    body: str
    project: str = DEFAULT_PROJECT  # 3.2: project scope of the note (migration 0011)
    level: int = 2                  # 3.4: classification rank (core.levels; 2 = project)


@dataclass
class ChunkRecord:
    idx: int
    body: str
    embedding: list[float]


@dataclass
class Generation:
    id: int
    embed_identity: str
    dim: int
    created_at: str
    status: str                   # building | active | retired


@dataclass
class Hit:
    note_name: str
    score: float
    cosine: float | None
    lexical: float | None
    rerank: float | None
    snippet: str
    age_days: int | None
    date_source: str


@dataclass
class SeenRecord:
    """One row of ``note_versions``: a version of a note as the indexer saw it.

    ``content_hash`` is the link-insensitive body hash (core.dates.content_hash);
    ``first_seen`` is when that *body* was first seen, carried over across
    description-only changes and renames; ``seen_at`` is when this version (body
    and description) was recorded.
    """
    name: str
    content_hash: str
    first_seen: str
    seeded: bool = False          # found at bootstrap, never seen changing
    date_source: str = "indexed"  # provenance of first_seen
    description: str = ""
    body: str = ""
    seen_at: str = ""
    extra: dict = field(default_factory=dict)


@runtime_checkable
class Embedder(Protocol):
    def identity(self) -> str: ...
    def embed_docs(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class IndexStore(Protocol):
    # --- contract
    def upsert_note(self, note: NoteRecord, chunks: list[ChunkRecord] | None,
                    generation: int) -> None:
        """chunks=None (*extension*): metadata-only update, the chunks stay."""
    def delete_note(self, name: str, generation: int) -> None: ...
    def get_note(self, name: str) -> NoteRecord | None: ...
    def active_generation(self) -> Generation | None: ...
    def note_seen(self, name: str) -> SeenRecord | None:
        """Latest version recorded under this name."""
    def record_seen(self, rec: SeenRecord) -> None:
        """Append a version when (content_hash, description) changed, else refresh it."""
    # --- extensions
    def find_seen(self, content_hashes: list[str]) -> SeenRecord | None:
        """Oldest version, under any name, with one of these hashes (rename inheritance)."""
    def seen_count(self) -> int: ...
    def note_versions(self, name: str, limit: int = 20) -> list[SeenRecord]:
        """Versions of a note, oldest first (description drift check)."""
    def generations(self) -> list[Generation]: ...
    def create_generation(self, embed_identity: str, dim: int) -> Generation: ...
    def activate_generation(self, generation: int) -> None: ...
    def note_names(self, generation: int, *, project: str | None = None) -> set[str]:
        """Notes that have chunks in this generation (of ``project`` only when given)."""
    def set_links(self, src: str, dsts: list[str]) -> None: ...
