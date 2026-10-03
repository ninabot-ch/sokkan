"""The fictional corpus of the review tests (tests/fixtures/review_corpus) and how to index it.

``build(tmp)`` copies the notes, fills the placeholders (a random key generated at run
time — no key-shaped string is committed), gives every file its own mtime, and returns
the paths. ``index(store, …)`` runs the real indexer twice, changing two descriptions in
between, then adds a note the index never sees.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import random
import shutil
import string
from pathlib import Path

from core import drift
from core.indexer import IndexConfig, Indexer

FIXTURE = Path(__file__).parent / "fixtures" / "review_corpus"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


class BowEmbedder:
    """Bag of words hashed into 256 dimensions: similar texts → similar vectors."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def identity(self) -> str:
        return f"test:bow@{self.dim}"

    def embed_docs(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in drift.terms(t):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def fake_key() -> str:
    rnd = random.Random(7)
    return "sk-" + "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(40))


def long_text(words: int = 900) -> str:
    rnd = random.Random(3)
    vocab = ("restaurant menu terrace lunch dinner owner chef wine season tourists locals "
             "price review booking delivery brand logo sign window street quarter").split()
    paras = []
    for _ in range(words // 60):
        paras.append(" ".join(rnd.choice(vocab) for _ in range(60)).capitalize() + ".")
    return "\n\n".join(paras)


def build(tmp: Path) -> tuple[Path, Path]:
    mem = tmp / "memory"
    shutil.copytree(FIXTURE / "notes", mem)
    repo = tmp / "repo"
    shutil.copytree(FIXTURE / "repo", repo)
    for p in mem.glob("*.md"):
        t = p.read_text(encoding="utf-8")
        t = t.replace("{{FAKE_SECRET}}", fake_key()).replace("{{LONG_TEXT}}", long_text())
        p.write_text(t, encoding="utf-8")
    for i, p in enumerate(sorted(mem.glob("*.md"))):
        ts = (NOW - datetime.timedelta(days=2, hours=i)).timestamp()
        os.utime(p, (ts, ts))
    return mem, repo


def expected() -> set[tuple[str, str]]:
    return {tuple(x) for x in json.loads((FIXTURE / "expected.json").read_text())["expected"]}


def index(store, mem: Path) -> None:
    t = [NOW - datetime.timedelta(days=3)]
    cfg = IndexConfig(memory_dir=mem, normalize=False, write_index=False)
    Indexer(store, BowEmbedder(), cfg, clock=lambda: t[0], log=lambda m: None).run()
    # the description changes, the body stays (office-wifi: the fact moved; parking-rules:
    # only reworded)
    for fname, old, new in (
            ("office_wifi.md", "The wifi router sits at the front desk, password rotates monthly",
             "The wifi router moved to the back office, password rotates monthly"),
            ("parking_rules.md", "Visitors park in lot B, staff in lot A",
             "Lot B for visitors, lot A for the staff")):
        p = mem / fname
        p.write_text(p.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        ts = (NOW - datetime.timedelta(days=1)).timestamp()
        os.utime(p, (ts, ts))
    t[0] = NOW - datetime.timedelta(days=1)
    Indexer(store, BowEmbedder(), cfg, clock=lambda: t[0], log=lambda m: None).run()
    late = mem / "late_note.md"
    late.write_text("---\nname: late-note\ndescription: Written after the last index pass\n"
                    "metadata:\n  type: reference\n  modified: 2026-10-01\n---\n"
                    "The kitchen fridge is cleaned on Fridays. See [[studio-overview]].\n",
                    encoding="utf-8")
    ts = (NOW - datetime.timedelta(hours=2)).timestamp()
    os.utime(late, (ts, ts))


def flagged(report: dict) -> set[tuple[str, str]]:
    return {(f["id"], n) for f in report["findings"] for n in f["notes"]}


def false_positive_rate(report: dict) -> tuple[float, set, set]:
    got, exp = flagged(report), expected()
    fp = got - exp
    missed = exp - got
    return (len(fp) / len(got) if got else 0.0), fp, missed
