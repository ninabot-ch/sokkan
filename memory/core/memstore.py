"""In-memory ``IndexStore``: the executable spec of what core.indexer expects.

Used by the tests; small enough to read as documentation of the semantics
(versions, rename inheritance, generations) the Postgres store has to honour.
"""
from __future__ import annotations

import copy
import datetime

from .contract import ChunkRecord, Generation, NoteRecord, SeenRecord


class InMemoryStore:
    def __init__(self) -> None:
        self.notes: dict[str, NoteRecord] = {}
        self.chunks: dict[tuple[int, str], list[ChunkRecord]] = {}
        self.versions: dict[str, list[SeenRecord]] = {}
        self.gens: list[Generation] = []
        self.links: dict[str, list[str]] = {}
        self.writes = 0  # chunk writes, for tests

    # --- notes
    def upsert_note(self, note, chunks, generation):
        self.notes[note.name] = copy.deepcopy(note)
        if chunks is not None:
            self._gen(generation)
            self.chunks[(generation, note.name)] = list(chunks)
            self.writes += 1

    def delete_note(self, name, generation):
        self.chunks.pop((generation, name), None)
        if not any(n == name for (_g, n) in self.chunks):
            self.notes.pop(name, None)
            self.links.pop(name, None)

    def get_note(self, name):
        n = self.notes.get(name)
        return copy.deepcopy(n) if n else None

    def note_names(self, generation):
        return {n for (g, n) in self.chunks if g == generation}

    def set_links(self, src, dsts):
        self.links[src] = list(dsts)

    # --- dates / versions
    def note_seen(self, name):
        v = self.versions.get(name)
        return copy.deepcopy(v[-1]) if v else None

    def record_seen(self, rec):
        hist = self.versions.setdefault(rec.name, [])
        if hist and (hist[-1].content_hash, hist[-1].description) == (rec.content_hash,
                                                                     rec.description):
            last = hist[-1]
            last.first_seen, last.seeded, last.date_source = (rec.first_seen, rec.seeded,
                                                              rec.date_source)
            last.body = rec.body
            return
        hist.append(copy.deepcopy(rec))

    def find_seen(self, content_hashes):
        cands = [v for hist in self.versions.values() for v in hist
                 if v.content_hash in content_hashes]
        return copy.deepcopy(min(cands, key=lambda v: v.first_seen)) if cands else None

    def seen_count(self):
        return len(self.versions)

    def note_versions(self, name, limit=20):
        return copy.deepcopy(self.versions.get(name, [])[-limit:])

    # --- generations
    def _gen(self, gid):
        for g in self.gens:
            if g.id == gid:
                return g
        raise KeyError(f"unknown generation {gid}")

    def generations(self):
        return list(self.gens)

    def active_generation(self):
        return next((g for g in self.gens if g.status == "active"), None)

    def create_generation(self, embed_identity, dim):
        g = Generation(id=len(self.gens) + 1, embed_identity=embed_identity, dim=dim,
                       created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       status="building")
        self.gens.append(g)
        return g

    def activate_generation(self, generation):
        target = self._gen(generation)
        for g in self.gens:
            if g.status == "active":
                g.status = "retired"
        target.status = "active"

    # --- helpers for tests
    def chunk_bodies(self, name, generation=None):
        gid = generation or self.active_generation().id
        return [c.body for c in self.chunks.get((gid, name), [])]
