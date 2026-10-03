"""Migrate a memory from SOKKAN 2.x (notes + ``memory.db``) to the 3.0 store, without loss.

Runs on its own at the first start of 3.0 and can be interrupted at any point: every step
is idempotent and the progress lives in ``state.json`` next to the archive, so a restart
resumes where it stopped. The notes (``.md`` files) stay the source of truth.

Steps::

    archive      tar.gz of the whole memory directory + manifest (SHA-256, mtime and
                 declared date of every file), written BEFORE anything is changed
    plan         normalize dry run (renames, merges, YAML repairs), kept as plan.txt
    normalize    the plan applied (policy ``auto``), or after approve("normalize")
                 (``ask``), or skipped (``off``); the mtimes of the files it rewrote
                 are put back
    dates-files  date test on the files: no declared date changed, no file newer than
                 it was in the archive
    seed         date history seeded from the manifest: frontmatter ``metadata.modified``
                 first, else the 2.x mtime (``migrated-mtime``)
    index        generation 1 built with the configured embedding model (not activated:
                 the 2.x index keeps serving meanwhile)
    verify       every 2.x note is in the store; date test in the store: same dates as
                 before, none younger than the start of the migration
    switch       generation 1 activated: searches now read the store

A failed check blocks the switch (status ``blocked``) and the 2.x index keeps serving;
``approve("override")`` goes on in spite of it, on purpose. Rolling back = the previous image
and the untouched ``memory.db`` (plus the archive if normalize renamed files).

    python -m core.migrate status|run|approve <step> --memory-dir … --work-dir … [--legacy-db …]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

from . import dates
from . import indexer as indexer_mod
from . import normalize as normalize_mod
from .config import env, env_int
from .notes import INDEX_FILENAME, parse_note

STEPS = ("archive", "plan", "normalize", "dates-files", "seed", "index", "verify", "switch")
POLICIES = ("auto", "ask", "off")
STATE_VERSION = 1
DATE_TOLERANCE_S = 1.0     # mtimes are compared to the second (tar, filesystems)
LOG_KEEP = 200


@contextlib.contextmanager
def _lock(path: Path):
    """Non-blocking exclusive lock: one migration run at a time across processes."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover — not POSIX
        yield True
        return
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Waiting(Exception):
    """A step cannot go on yet (approval, embedding server): retry later."""


class Blocked(Exception):
    """A check failed: the migration stops before the switch, the 2.x index keeps serving."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts: float | datetime.datetime | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        ts = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return ts.isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, doc) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- 2.x index
def legacy_index(db_path: Path | str | None) -> dict | None:
    """What a SOKKAN 2.x ``memory.db`` holds, read-only: notes (name, file, mtime), model."""
    if not db_path or not Path(db_path).is_file():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        notes = {r[0]: {"file": Path(r[1] or "").name, "mtime": r[2]} for r in con.execute(
            "SELECT name, source_path, mtime FROM notes")}
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        except sqlite3.OperationalError:
            row = None
    finally:
        con.close()
    return {"path": str(db_path), "notes": notes, "chunks": chunks,
            "model": row[0] if row else None}


# ----------------------------------------------------------------------------- migration
class Migration:
    """One migration of ``memory_dir`` into ``store``; state and archive in ``work_dir``.

    ``store``     an IndexStore (core.contract) with ``activate_generation``: the Postgres
                  store in production, ``core.memstore.InMemoryStore`` in tests.
    ``embedder``  the 3.0 embedder (``core.embed.get()``) — a callable returning it is
                  accepted, so a server that is not up yet only delays the ``index`` step.
    ``legacy_db`` the 2.x ``memory.db`` (read-only, never written).
    """

    def __init__(self, memory_dir: Path | str, work_dir: Path | str, store, embedder, *,
                 legacy_db: Path | str | None = None, policy: str | None = None,
                 grace: int | None = None, clock=_now, log=None,
                 index_overrides: dict | None = None, context: dict | None = None) -> None:
        self.memory_dir = Path(memory_dir)
        self.work_dir = Path(work_dir)
        self.store = store
        self._embedder = embedder
        self.legacy_db = Path(legacy_db) if legacy_db else None
        self.policy = (policy or env("MIGRATION_NORMALIZE") or "auto").lower()
        if self.policy not in POLICIES:
            raise ValueError(f"normalize policy {self.policy!r}: expected {', '.join(POLICIES)}")
        self.grace = normalize_mod.DEFAULT_GRACE if grace is None else grace
        self.clock = clock
        self._log = log or (lambda m: print(f"[memory-migration] {m}", file=sys.stderr))
        self.index_overrides = index_overrides or {}
        self._mapping_override: dict | None = None
        self.context = context      # recorded once in the state, e.g. the config translation
        self._cache: dict = {}
        self.state_path = self.work_dir / "state.json"
        self.state = self._load()

    # -------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            if st.get("version") == STATE_VERSION:
                return st
        except (OSError, ValueError):
            pass
        return {"version": STATE_VERSION, "status": "pending", "step": None, "steps": {},
                "log": []}

    def _save(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = _iso(self.clock())
        _write_json(self.state_path, self.state)

    def log(self, msg: str) -> None:
        self._log(msg)
        self.state.setdefault("log", []).append(f"{_iso(self.clock())} {msg}")
        del self.state["log"][:-LOG_KEEP]

    def _done(self, step: str, **info) -> None:
        self._cache.clear()
        self.state["steps"][step] = {"done_at": _iso(self.clock()), **info}
        self._save()

    def status(self) -> dict:
        return self.state

    @property
    def embedder(self):
        e = self._embedder
        return e() if callable(e) and not hasattr(e, "embed_docs") else e

    def approved(self) -> dict:
        """Approvals, kept in their own file so another process (the API) can write one
        while a run holds the state."""
        try:
            return json.loads((self.work_dir / "approved.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def approve(self, what: str, who: str = "") -> dict:
        """Explicit go: ``normalize`` (policy ask) or ``override`` (switch in spite of a
        failed check, after reading it)."""
        what = "override" if what == "switch" else what
        if what not in ("normalize", "override"):
            raise ValueError("approve: normalize or override")
        doc = self.approved()
        doc[what] = {"at": _iso(self.clock()), "by": who}
        self.work_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.work_dir / "approved.json", doc)
        return doc

    # -------------------------------------------------------------- decision
    def needed(self) -> bool:
        """False when done, or when there is nothing to migrate (no note, no 2.x index),
        or when the store already serves an index (migrated by other means)."""
        if self.state.get("status") in ("done", "not-needed"):
            return False
        if self.state.get("step"):
            return True
        has_notes = self.memory_dir.is_dir() and any(self._note_files())
        has_legacy = bool(self.legacy_db and self.legacy_db.is_file())
        if not has_notes and not has_legacy:
            return False
        return self.store.active_generation() is None

    def run(self) -> dict:
        """Run the remaining steps. Returns the state; status is one of done, waiting,
        blocked, failed, not-needed."""
        if not self.needed():
            if self.state.get("status") != "done":
                self.state["status"] = "not-needed"
                self.state["message"] = ("nothing to migrate: no 2.x index or notes, or the "
                                         "store already serves an index")
                self._save()
            return self.state
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with _lock(self.work_dir / "lock") as got:
            if not got:
                return self.state     # another process runs it
            self.state = self._load()
            if self.state.get("status") in ("done", "not-needed"):
                return self.state
            return self._run_locked()

    def _run_locked(self) -> dict:
        st = self.state
        if "started_at" not in st:
            st["started_at"] = _iso(self.clock())
            if self.context:
                st["context"] = self.context
            self.log("migration started" + (f": {self.context}" if self.context else ""))
        st["status"] = "running"
        try:
            for step in STEPS:
                if step in st["steps"]:
                    continue
                st["step"] = step
                st["message"] = None
                self._save()
                getattr(self, "_step_" + step.replace("-", "_"))()
            st["status"], st["step"] = "done", None
            st["finished_at"] = _iso(self.clock())
            st["message"] = "migration complete: searches read the 3.0 store"
            self.log(st["message"])
        except Waiting as e:
            st["status"], st["message"] = "waiting", str(e)
            self.log(f"waiting at {st['step']}: {e}")
        except Blocked as e:
            st["status"], st["message"] = "blocked", str(e)
            self.log(f"BLOCKED at {st['step']}: {e}")
        except Exception as e:  # noqa: BLE001 — recorded, the 2.x index keeps serving
            st["status"], st["message"] = "failed", f"{type(e).__name__}: {e}"
            self.log(f"FAILED at {st['step']}: {type(e).__name__}: {e}")
        self._save()
        return st

    # -------------------------------------------------------------- helpers
    def _note_files(self) -> list[Path]:
        return sorted(p for p in self.memory_dir.glob("*.md") if p.name != INDEX_FILENAME)

    def _manifest(self) -> dict:
        if "manifest" not in self._cache:
            self._cache["manifest"] = json.loads(
                (self.work_dir / "manifest.json").read_text(encoding="utf-8"))
        return self._cache["manifest"]

    def _final_of(self, orig: str, mapping: dict) -> str:
        """File name an original file ended up in (merge, then rename)."""
        merged, renamed = mapping.get("merged", {}), mapping.get("renamed", {})
        target = merged.get(orig, orig)
        final = renamed.get(target, target)
        if not (self.memory_dir / final).exists() and (self.memory_dir / orig).exists():
            return orig    # the action was not applied (conflict, skipped): still there
        return final

    def _mapping(self) -> dict:
        """renamed / merged / touched files: what normalize applied, else what it planned."""
        if self._mapping_override is not None:
            return self._mapping_override
        if "mapping" not in self._cache:
            self._cache["mapping"] = {}
            for name in ("normalize-applied.json", "normalize-plan.json"):
                p = self.work_dir / name
                if p.exists():
                    self._cache["mapping"] = json.loads(p.read_text(encoding="utf-8"))
                    break
        return self._cache["mapping"]

    def _sources(self) -> dict[str, list[str]]:
        """final file -> original files whose content it carries."""
        mapping = self._mapping()
        out: dict[str, list[str]] = {}
        for orig in self._manifest()["files"]:
            if orig == INDEX_FILENAME:
                continue
            out.setdefault(self._final_of(orig, mapping), []).append(orig)
        return out

    def _expected(self) -> dict[str, dict]:
        """final file -> expected date: declared (frontmatter) else newest source mtime."""
        if self._mapping_override is None and "expected" in self._cache:
            return self._cache["expected"]
        files = self._manifest()["files"]
        merged = self._mapping().get("merged", {})
        out = {}
        for final, srcs in self._sources().items():
            primary = next((s for s in srcs if s not in merged), srcs[0])
            mtime = max(files[s]["mtime"] for s in srcs)
            out[final] = {"sources": srcs, "mtime": mtime,
                          "declared": files[primary].get("declared")}
        if self._mapping_override is None:
            self._cache["expected"] = out
        return out

    def _edited_meanwhile(self, final: str, exp: dict) -> bool:
        """The note changed after the archive and normalize did not touch it: a session
        wrote it during the migration (a real update, not an artefact)."""
        p = self.memory_dir / final
        if final in self._mapping().get("edited", {}):
            return True
        if "touched" not in self._cache:
            self._cache["touched"] = set(self._mapping().get("touched", [])) \
                if "normalize" in self.state["steps"] else set()
        touched = self._cache["touched"]
        if final in touched or not p.exists():
            return False
        files = self._manifest()["files"]
        return len(exp["sources"]) == 1 and \
            _sha256(p.read_bytes()) != files[exp["sources"][0]]["sha256"]

    # -------------------------------------------------------------- 1. archive
    def _step_archive(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in self.memory_dir.rglob("*") if p.is_file()) \
            if self.memory_dir.is_dir() else []
        size = sum(p.stat().st_size for p in files)
        free = shutil.disk_usage(self.work_dir).free
        if free < 2 * size + 50 * 2**20:
            raise Blocked(f"not enough disk space for the archive of the notes "
                          f"({size // 2**20} MB to archive, {free // 2**20} MB free) — "
                          "nothing was changed")
        manifest: dict = {"memory_dir": str(self.memory_dir), "created_at": _iso(self.clock()),
                          "files": {}, "others": []}
        for p in files:
            rel = p.relative_to(self.memory_dir).as_posix()
            if p.parent != self.memory_dir or p.suffix != ".md":
                manifest["others"].append(rel)
                continue
            data = p.read_bytes()
            note = parse_note(data.decode("utf-8", errors="replace"), p.name)
            manifest["files"][p.name] = {
                "sha256": _sha256(data), "size": len(data), "mtime": p.stat().st_mtime,
                "name": note.name, "declared": note.modified,
                "declared_source": note.modified_source}
        legacy = legacy_index(self.legacy_db)
        if legacy:
            manifest["legacy"] = {"model": legacy["model"], "notes": len(legacy["notes"]),
                                  "chunks": legacy["chunks"]}
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        name = f"corpus-2x-{stamp}.tar.gz"
        tmp = self.work_dir / (name + ".tmp")
        with tarfile.open(tmp, "w:gz") as tar:
            for p in files:
                tar.add(p, arcname="memory/" + p.relative_to(self.memory_dir).as_posix(),
                        recursive=False)
            blob = json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime = len(blob), time.time()
            tar.addfile(info, io.BytesIO(blob))
        with tarfile.open(tmp, "r:gz") as tar:   # readable, complete
            members = [m for m in tar.getmembers() if m.name.startswith("memory/")]
        if len(members) != len(files):
            raise RuntimeError(f"archive incomplete: {len(members)} of {len(files)} files")
        os.replace(tmp, self.work_dir / name)
        _write_json(self.work_dir / "manifest.json", manifest)
        sha = _sha256((self.work_dir / name).read_bytes())
        self.log(f"archive {name}: {len(files)} files, {size} bytes, sha256 {sha[:12]}")
        self._done("archive", path=name, files=len(files), notes=len(manifest["files"]),
                   bytes=size, sha256=sha, legacy=manifest.get("legacy"))

    # -------------------------------------------------------------- 2. plan
    def _step_plan(self) -> None:
        now = self.clock().timestamp()
        plan = normalize_mod.run(self.memory_dir, dry_run=True, grace=self.grace, now=now)
        text = plan.render()
        (self.work_dir / "normalize-plan.txt").write_text(text + "\n", encoding="utf-8")
        for line in text.splitlines()[:40]:
            self.log("plan: " + line)
        _write_json(self.work_dir / "normalize-plan.json",
                    {"renamed": plan.renamed, "merged": plan.merged, "touched": [],
                     "actions": [{"kind": a.kind, "path": a.path, "target": a.target,
                                  "detail": a.detail} for a in plan.actions]})
        self._done("plan", now=now, summary=plan.summary(), changes=len(plan.changes),
                   text="normalize-plan.txt", detail="normalize-plan.json")

    # -------------------------------------------------------------- 3. normalize
    def _step_normalize(self) -> None:
        planned = self.state["steps"]["plan"]
        if self.policy == "off" or not planned["changes"]:
            why = "policy off" if self.policy == "off" else "nothing to repair"
            _write_json(self.work_dir / "normalize-applied.json",
                        {"renamed": {}, "merged": {}, "touched": []})
            self._done("normalize", applied=False, reason=why)
            return
        if self.policy == "ask" and "normalize" not in self.approved():
            raise Waiting(f"{planned['changes']} repairs of the notes are waiting for "
                          "approval (see the plan)")
        files = self._manifest()["files"]
        # notes a session changed since the archive: their new date is real, keep it
        edited_pre = {p.name: p.stat().st_mtime for p in self._note_files()
                      if p.name in files and _sha256(p.read_bytes()) != files[p.name]["sha256"]}
        plan = normalize_mod.run(self.memory_dir, dry_run=False, grace=self.grace,
                                 now=planned["now"])
        # planned ∪ applied: after a crash in the middle of an apply, the second pass no
        # longer sees the renames the first one made (_final_of checks the disk anyway)
        planned_map = json.loads((self.work_dir / "normalize-plan.json").read_text("utf-8"))
        mapping = {"renamed": {**planned_map["renamed"], **plan.renamed},
                   "merged": {**planned_map["merged"], **plan.merged}}
        self._mapping_override = mapping
        renamed = mapping["renamed"]
        touched = {renamed.get(a.path, a.path) for a in plan.changes}
        touched |= {renamed.get(t, t) for t in mapping["merged"].values()}
        touched |= set(renamed.values())
        touched -= set(mapping["merged"])
        # a file changed on disk during the pass was left alone by normalize: not ours
        touched -= {renamed.get(c, c) for c in plan.conflicts if " -> " not in c}
        # A rewrite is not an update: the files keep the date they had in the archive.
        restored, edited = 0, {}
        for final, srcs in self._sources().items():
            p = self.memory_dir / final
            if any(s in edited_pre for s in srcs):
                edited[final] = max(edited_pre.get(s, 0) for s in srcs)
            if final in touched and p.exists():
                ts = max([files[s]["mtime"] for s in srcs] + [edited.get(final, 0)])
                os.utime(p, (ts, ts))
                restored += 1
        self._mapping_override = None
        mapping["touched"] = sorted(touched)
        mapping["edited"] = edited
        _write_json(self.work_dir / "normalize-applied.json", mapping)
        for line in plan.render().splitlines()[:60]:
            self.log("normalize: " + line)
        (self.work_dir / "normalize-applied.txt").write_text(plan.render() + "\n",
                                                            encoding="utf-8")
        self._done("normalize", applied=True, summary=plan.summary(),
                   conflicts=plan.conflicts, mtimes_restored=restored,
                   renamed=len(mapping["renamed"]), merged=len(mapping["merged"]),
                   detail="normalize-applied.json")

    # -------------------------------------------------------------- 4. date test (files)
    def _step_dates_files(self) -> None:
        younger, declared_changed, edited = [], [], []
        touched = set(self._mapping().get("touched", []))
        for final, exp in self._expected().items():
            p = self.memory_dir / final
            if not p.exists():
                continue
            if self._edited_meanwhile(final, exp):
                edited.append(final)
                continue
            if final in touched and p.stat().st_mtime > exp["mtime"] + DATE_TOLERANCE_S:
                younger.append(final)
            note = parse_note(p.read_text(encoding="utf-8", errors="replace"), final)
            if dates.parse_dt(note.modified) != dates.parse_dt(exp["declared"]):
                declared_changed.append(final)
        info = {"checked": len(self._expected()), "younger": younger,
                "declared_changed": declared_changed, "edited_meanwhile": edited}
        if (younger or declared_changed) and "override" not in self.approved():
            self.state["steps"]["dates-files-failed"] = info
            raise Blocked(f"date test failed on the files: {len(younger)} newer than in the "
                          f"archive, {len(declared_changed)} declared dates changed "
                          f"({', '.join((younger + declared_changed)[:5])})")
        self.log(f"date test (files): {info['checked']} notes, none younger"
                 + (f"; {len(edited)} edited by a session meanwhile" if edited else ""))
        self._done("dates-files", **info)

    # -------------------------------------------------------------- 5. seed dates
    def _step_seed(self) -> None:
        now = self.clock()
        counts: dict[str, int] = {}
        seen: set[str] = set()
        expected = self._expected()
        for final in sorted(expected):
            exp = expected[final]
            p = self.memory_dir / final
            if not p.exists():
                continue
            note = parse_note(p.read_text(encoding="utf-8", errors="replace"), final)
            if note.name in seen:      # shadowed duplicate: the indexer ignores it too
                continue
            seen.add(note.name)
            mtime = exp["mtime"]
            if self._edited_meanwhile(final, exp):
                mtime = max(mtime, p.stat().st_mtime)   # a real edit keeps its date
            eff = dates.effective_modified(self.store, note, bootstrap=True, now=now,
                                           mtime=mtime, migrate_mtime=True)
            counts[eff.source] = counts.get(eff.source, 0) + 1
        self.log("dates imported: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
        self._done("seed", sources=counts)

    # -------------------------------------------------------------- 6. index
    def _step_index(self) -> None:
        try:
            emb = self.embedder
            ident = emb.identity()
        except Exception as e:  # noqa: BLE001
            raise Waiting(f"embedding model not ready ({e})") from e
        cfg = indexer_mod.IndexConfig.from_env(
            memory_dir=self.memory_dir, normalize=False, auto_activate=False,
            migrate_mtime=True, **self.index_overrides)
        t0 = time.monotonic()
        ind = indexer_mod.Indexer(self.store, _Progress(emb, self), cfg, clock=self.clock,
                                  log=self.log)
        try:
            rep = ind.run()
        except (OSError, ConnectionError) as e:
            raise Waiting(f"embedding server unavailable ({e}); indexing resumes "
                          "where it stopped") from e
        except Exception as e:  # noqa: BLE001 — httpx errors are not OSError
            if type(e).__module__.startswith(("httpx", "httpcore")):
                raise Waiting(f"embedding server unavailable ({e}); indexing resumes "
                              "where it stopped") from e
            raise
        self.log(f"generation {rep.generation} built ({ident}): {rep.reindexed} notes "
                 f"embedded, {rep.unchanged} already done, {rep.chunks_written} chunks, "
                 f"{time.monotonic() - t0:.1f} s")
        self._done("index", generation=rep.generation, identity=ident, notes=rep.notes,
                   reindexed=rep.reindexed, chunks=rep.chunks_written,
                   date_sources=dict(rep.date_sources), warnings=rep.warnings[:50],
                   seconds=round(time.monotonic() - t0, 1))

    # -------------------------------------------------------------- 7. verify
    def _step_verify(self) -> None:
        gen = self.state["steps"]["index"]["generation"]
        started = dates.parse_dt(self.state["started_at"])
        names = self.store.note_names(gen)
        files = self._manifest()["files"]
        expected = self._expected()

        # same notes: every file of the archive is a note of the store
        final_name: dict[str, str] = {}
        lost_files, by_name = [], {}
        for final in sorted(expected):          # the indexer keeps the first file of a name
            exp = expected[final]
            p = self.memory_dir / final
            name = parse_note(p.read_text(encoding="utf-8", errors="replace"), final).name \
                if p.exists() else None
            if name is not None:
                by_name.setdefault(name, []).append(final)
            if name is None or name not in names or len(by_name[name]) > 1:
                lost_files.extend(exp["sources"])
            else:
                final_name[final] = name
        duplicates = {n: fs for n, fs in by_name.items() if len(fs) > 1}
        legacy = legacy_index(self.legacy_db)
        lost_2x, stale_2x, dates_vs_2x = [], [], 0
        if legacy:
            by_file = {s: f for f, exp in expected.items() for s in exp["sources"]}
            for n, row in legacy["notes"].items():
                if row["file"] not in files:
                    stale_2x.append(n)      # the file was already gone before the migration
                    continue
                final = by_file.get(row["file"])
                if final is None or final not in final_name:
                    lost_2x.append(n)
                elif row["mtime"] and abs(row["mtime"] - files[row["file"]]["mtime"]) \
                        <= DATE_TOLERANCE_S:
                    dates_vs_2x += 1

        # date test in the store: identical to the expectation, none younger
        wrong, younger, edited = [], [], []
        for final, name in final_name.items():
            if self._edited_meanwhile(final, expected[final]):
                edited.append(name)
                continue
            rec = self.store.get_note(name)
            got = dates.parse_dt(rec.modified if rec else None)
            exp = expected[final]
            want = dates.parse_dt(exp["declared"]) or datetime.datetime.fromtimestamp(
                exp["mtime"], datetime.timezone.utc)
            if got is None or abs((got - want).total_seconds()) > DATE_TOLERANCE_S:
                wrong.append(f"{name}: {_iso(got)} instead of {_iso(want)}")
            if got is not None and started and got > started and not exp["declared"]:
                younger.append(name)
            seen = self.store.note_seen(name)
            first = dates.parse_dt(seen.first_seen) if seen else None
            if first is not None and started and first > started and not exp["declared"]:
                younger.append(name)
        info = {"notes_before": len([f for f in files if f != INDEX_FILENAME]),
                "notes_after": len(final_name),
                "merged": len(self._mapping().get("merged", {})),
                "legacy_notes": len(legacy["notes"]) if legacy else None,
                "store_notes": len(names), "lost_files": lost_files, "lost_2x": lost_2x,
                "stale_2x": stale_2x, "duplicate_names": duplicates,
                "dates_checked": len(final_name) - len(edited), "dates_wrong": wrong[:50],
                "younger": sorted(set(younger)), "edited_meanwhile": edited,
                "dates_equal_2x_index": dates_vs_2x}
        self.log(f"verify: {info['notes_before']} notes before, {info['notes_after']} after "
                 f"({info['merged']} merged), {info['store_notes']} in the store; "
                 f"{info['dates_checked']} dates checked, {len(wrong)} wrong, "
                 f"{len(info['younger'])} younger")
        problems = []
        if lost_files or lost_2x:
            problems.append(f"{len(set(lost_files)) + len(lost_2x)} notes missing from the "
                            f"store ({', '.join((lost_files + lost_2x)[:5])})")
        if wrong or info["younger"]:
            problems.append(f"date test failed: {len(wrong)} different, "
                            f"{len(info['younger'])} younger")
        if problems and "override" not in self.approved():
            self.state["steps"]["verify-failed"] = info
            raise Blocked("; ".join(problems) + " — the 2.x index keeps serving")
        self._done("verify", **info, overridden=bool(problems))

    # -------------------------------------------------------------- 8. switch
    def _step_switch(self) -> None:
        gen = self.state["steps"]["index"]["generation"]
        self.store.activate_generation(gen)
        self.log(f"generation {gen} active: searches read the 3.0 store "
                 "(the 2.x memory.db is kept, read-only, for a rollback)")
        self._done("switch", generation=gen)


class _Progress:
    """Embedder wrapper that records the indexing progress in the state (every 5 s)."""

    def __init__(self, inner, mig: Migration) -> None:
        self.inner, self.mig, self.texts, self._last = inner, mig, 0, 0.0

    def identity(self) -> str:
        return self.inner.identity()

    def embed_docs(self, texts):
        out = self.inner.embed_docs(texts)
        self.texts += len(texts)
        if time.monotonic() - self._last > 5:
            self._last = time.monotonic()
            self.mig.state["progress"] = {"chunks_embedded": self.texts,
                                          "at": _iso(self.mig.clock())}
            with contextlib.suppress(OSError):
                self.mig._save()
        return out

    def __getattr__(self, item):
        return getattr(self.inner, item)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("status", "run", "approve"))
    ap.add_argument("what", nargs="?", help="approve: normalize | override")
    ap.add_argument("--memory-dir", default=env("MEMORY_DIR"))
    ap.add_argument("--work-dir", default=env("MIGRATION_DIR"))
    ap.add_argument("--legacy-db", default=env("MEMORY_DB"))
    ap.add_argument("--policy", default=None, choices=POLICIES)
    ap.add_argument("--grace", type=int, default=env_int("NORMALIZE_GRACE",
                                                         normalize_mod.DEFAULT_GRACE))
    args = ap.parse_args(argv)
    if not args.memory_dir or not args.work_dir:
        print("--memory-dir and --work-dir (or CORTHEXIS_MEMORY_DIR / "
              "CORTHEXIS_MIGRATION_DIR) are required", file=sys.stderr)
        return 2
    if args.command == "status":
        p = Path(args.work_dir) / "state.json"
        print(p.read_text(encoding="utf-8") if p.exists() else '{"status": "pending"}')
        return 0
    from . import embed
    from .store import Store

    with Store() as store:
        mig = Migration(args.memory_dir, args.work_dir, store, embed.get,
                        legacy_db=args.legacy_db, policy=args.policy, grace=args.grace)
        if args.command == "approve":
            mig.approve(args.what or "", who="cli")
            return 0
        st = mig.run()
    print(json.dumps({k: st.get(k) for k in ("status", "step", "message")}, indent=1))
    return 0 if st["status"] in ("done", "not-needed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
