"""Run the truth chantier's indexer / dates / drift tests against the Postgres store.

Those tests are written against ``core.memstore.InMemoryStore`` (the executable spec of the
store semantics). Here every ``InMemoryStore()`` they create becomes a fresh Postgres
``Store``, extended with the few inspection helpers the tests read (``links``, ``writes``,
``chunks``, ``notes``, ``chunk_bodies``). Skipped without SOKKAN_TEST_PG_DSN, or when the
truth tests are not in the tree.
"""
import importlib
import inspect
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")

from core.store import Store  # noqa: E402

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
SUITES = ("test_memcore_indexer", "test_memcore_dates", "test_memcore_drift")


class PgSpecStore(Store):
    """Store + the read-only helpers of InMemoryStore used by the truth tests."""

    writes = 0

    def upsert_note(self, note, chunks, generation):
        super().upsert_note(note, chunks, generation)
        if chunks is not None:
            self.writes += 1

    @property
    def links_map(self):
        with self.pool.connection() as con:
            out = {}
            for r in con.execute("SELECT src, dst FROM links ORDER BY src, dst"):
                out.setdefault(r["src"], []).append(r["dst"])
            return out

    @property
    def chunks(self):
        return {(g.id, n): self.get_chunks(n, g.id)
                for g in self.generations() for n in sorted(self.note_names(g.id))}

    @property
    def notes(self):
        return {n: self.get_note(n) for n in sorted(self.note_names())}

    def chunk_bodies(self, name, generation=None):
        return [c.body for c in self.get_chunks(name, generation)]


def _cases():
    for modname in SUITES:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        if not hasattr(mod, "InMemoryStore"):
            continue
        for name, fn in sorted(vars(mod).items()):
            if not (name.startswith("test_") and inspect.isfunction(fn)):
                continue
            src = inspect.getsource(fn)
            # tests that build their store through the module helper make(...) count too
            if "InMemoryStore" in src or "make(" in src:
                yield pytest.param(mod, fn, id=f"{modname}::{name}")


CASES = list(_cases())


@pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")
@pytest.mark.skipif(not CASES, reason="truth tests (test_memcore_*) not in the tree")
@pytest.mark.parametrize("mod,fn", CASES)
def test_truth_suite_on_postgres(mod, fn, monkeypatch, tmp_path):
    created = []

    def factory():
        name = "sokkan_spec_" + uuid.uuid4().hex[:10]
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"CREATE DATABASE {name}")
        st = PgSpecStore(DSN.rsplit("/", 1)[0] + "/" + name, max_size=2)
        # InMemoryStore exposes links as a dict attribute; Store.links is a method
        st.__dict__["links"] = _LiveLinks(st)
        created.append((name, st))
        return st

    monkeypatch.setattr(mod, "InMemoryStore", factory)
    kwargs = {}
    for p in inspect.signature(fn).parameters:
        kwargs[p] = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}[p]
    try:
        fn(**kwargs)
    finally:
        for name, st in created:
            st.close()
            with psycopg.connect(DSN, autocommit=True) as con:
                con.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


class _LiveLinks:
    """dict-like view of the links table, callable like Store.links(name)."""

    def __init__(self, st):
        self.st = st

    def __getitem__(self, src):
        return self.st.links_map[src]

    def get(self, src, default=None):
        return self.st.links_map.get(src, default)

    def __contains__(self, src):
        return src in self.st.links_map

    def __call__(self, name):
        return Store.links(self.st, name)
