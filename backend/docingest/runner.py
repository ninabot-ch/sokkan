"""runner.py — one pass over one source: list, read what changed, write notes into a batch.

    list (allow-list) -> changed? (size, mtime, then sha256) -> extract -> same text already
    read? -> closest existing notes -> model (strict JSON) -> validation -> batch quarantine

* Incremental: a file whose (size, mtime) did not change is not opened; a file whose
  content hash did not change is not read again.
* Resumable: every document is committed on its own. A pass that was interrupted (stop,
  crash, budget, reboot) is taken up again by the next run — same pass, same batch — and
  only does what is left.
* Bounded: a ceiling of tokens, documents and minutes per pass; what does not fit waits for
  the next pass.
* Honest: the journal counts what was listed, read, noted, abstained, duplicated, failed,
  the duration and the tokens — from the ledger, not from what the pass believes it did.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from . import agent, batches, llmclient, sources, state
from . import config as cfgmod
from .config import SourceConfig
from .extract import Extractor

RETRY = ("error",)                 # states read again at the next pass even when unchanged
COMPLETION_TOKENS = 3500


def default_memory_neighbours(project: str, query: str, max_level: int, limit: int = 4
                              ) -> list[dict]:
    """Closest notes of the project's memory (written by people or approved earlier), through
    the 3.0 store when it serves — scoped to the project AND capped at `max_level`."""
    try:
        import store_backend
        if not store_backend.enabled():
            return []
        hits = store_backend.memory_search(query[:600], limit, projects=[f"{project}@{max_level}"])
    except Exception:  # noqa: BLE001 — the memory being down never stops an ingestion
        return []
    out = []
    for h in hits:
        if h.get("error") or h.get("empty") or not h.get("name"):
            continue
        out.append({"name": h["name"], "type": h.get("type") or "",
                    "description": h.get("description") or "",
                    "body": h.get("snippet") or h.get("excerpt") or h.get("text") or "",
                    "level": h.get("level", 2)})
    return out


def _words(s: str) -> set[str]:
    return {w for w in agent.fold(s).split() if len(w) >= 4}


def _similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb))


class Pass:
    def __init__(self, src: SourceConfig, kind: str = "incremental", requested_by: str = "",
                 client=None, memory_neighbours: Callable | None = None,
                 stop: threading.Event | None = None, extractor_factory: Callable | None = None,
                 on_progress: Callable[[dict], None] | None = None):
        self.src = src
        self.kind = kind
        self.requested_by = requested_by
        self.limits = cfgmod.limits_for(src)
        self.client = client
        self.memory_neighbours = (default_memory_neighbours if memory_neighbours is None
                                  else memory_neighbours)
        self.stop = stop or threading.Event()
        self.extractor_factory = extractor_factory or (lambda: Extractor(self.limits))
        self.on_progress = on_progress
        self._lock = threading.Lock()
        self._tokens = [0, 0]
        self._done_docs = 0
        self._recheck: dict[str, sources.FileRef] = {}
        self._claims: dict[str, str] = {}      # text hash -> the file reading it right now
        self._budget_hit = ""
        self._unavailable = ""                 # the model stopped answering: stop, resume later
        self.row: dict = {}
        self.batch_id = 0
        self.read_date = time.strftime("%Y-%m-%d")
        self.stop_reason = ""

    # ---- budget ------------------------------------------------------------------------------
    def _over_budget(self, t0: float) -> str:
        lim = self.limits
        if lim.max_tokens_per_pass and sum(self._tokens) >= lim.max_tokens_per_pass:
            return f"token ceiling reached ({sum(self._tokens)} >= {lim.max_tokens_per_pass})"
        if lim.max_docs_per_pass and self._done_docs >= lim.max_docs_per_pass:
            return f"document ceiling reached ({lim.max_docs_per_pass})"
        if lim.max_minutes_per_pass and time.time() - t0 >= lim.max_minutes_per_pass * 60:
            return f"time ceiling reached ({lim.max_minutes_per_pass} min)"
        return ""

    # ---- the pass ------------------------------------------------------------------------------
    def run(self) -> dict:
        own_client = False
        if self.client is None:
            why = llmclient.ready()
            if why:
                raise RuntimeError(why)
            self.client = llmclient.Client()
            self.client.should_stop = self.stop.is_set
            own_client = True
        self.row = state.pass_open(self.src.id, self.src.project, self.kind, self.requested_by)
        pid = self.row["id"]
        t0 = time.time()
        base_seconds = float(self.row.get("seconds") or 0)
        base = self.row.get("counters") or {}
        self._tokens = [int(base.get("tokens_in") or 0), int(base.get("tokens_out") or 0)]
        status, error = "done", ""
        try:
            if not self.row.get("batch_id"):
                self.batch_id = batches.open_batch(self.src.id, self.src.project, pid)
                state.pass_update(pid, batch_id=self.batch_id)
            else:
                self.batch_id = self.row["batch_id"]
            source = sources.open_source(self.src)
            source.prepare()
            todo, listed, unchanged, skipped_ext = self._plan(source)
            self._listing = {"listed": listed, "unchanged": unchanged}
            self._work(todo, t0)
            if not self.stop.is_set() and not self._budget_hit and not self._unavailable:
                self._reconcile(t0)
            if self._unavailable:
                # not a failure of the documents: nothing is marked, the next tick resumes
                status, error = "interrupted", self._unavailable
            elif self.stop.is_set():
                status, error = "interrupted", self.stop_reason
            elif self._budget_hit:
                status, error = "budget", self._budget_hit
        except Exception as e:  # noqa: BLE001 — the pass ends in its journal, never in a trace
            status, error = "failed", f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            if own_client:
                self.client.close()
        counters = self._recount()
        final = status in ("done", "failed")
        state.pass_update(pid, status=status, error=error, counters=counters,
                          seconds=round(base_seconds + time.time() - t0, 1),
                          ended_at=time.time() if final or status == "budget" else None)
        if status in ("done", "budget"):
            # a budget stop still offers what was produced; the rest comes with the next pass
            batches.close_batch(self.batch_id)
            try:
                batches.apply_trust(self.batch_id, self.src)
            except Exception as e:  # noqa: BLE001
                print(f"[doc-ingest] trust policy failed on batch #{self.batch_id}: {e!r}",
                      flush=True)
        elif status == "failed":
            batches.close_batch(self.batch_id)
        return state.pass_get(pid) or {}

    def _plan(self, source) -> tuple[list, int, int, int]:
        known = state.files_of(self.src.id)
        todo, listed, unchanged, skipped = [], 0, 0, 0
        seen: set[str] = set()
        exts = tuple(e.lower() for e in self.src.extensions)
        for f in source.iter_files():
            if not sources.allowed(self.src, f.rel):
                continue           # a connector cannot widen the allow-list (defence in depth)
            listed += 1
            seen.add(f.rel)
            prev = known.get(f.rel)
            same = (prev is not None and prev["size"] == f.size
                    and abs(prev["mtime"] - f.mtime) < 1e-3)
            if same and prev["gone_at"]:
                state.file_touch(self.src.id, f.rel, f.size, f.mtime)   # it came back, as it was
            if not f.rel.lower().endswith(exts):
                if not same:
                    state.file_put(self.src.id, f.rel, size=f.size, mtime=f.mtime, sha256="",
                                   status="skipped", detail="extension not read",
                                   pass_id=self.row["id"])
                skipped += 1
                continue
            if same and prev["status"] not in RETRY:
                unchanged += 1
                continue
            todo.append(f)
        gone = [rel for rel, p in known.items() if rel not in seen and not p["gone_at"]]
        if known and not seen:
            # a source that had documents and lists NOTHING is a mount that fell or a sync
            # that failed, not an archive emptied overnight: stop, mark nothing as gone
            raise sources.SourceError(
                f"{self.src.id}: the source lists no file at all (mount missing? sync failed?)")
        if gone:
            # the notes stay (history is memory); the journal says their source disappeared
            state.file_mark_gone(self.src.id, gone)
            for rel in gone[:200]:
                state.event(self.row["id"], rel, "gone", "no longer in the source")
        todo.sort(key=lambda f: (f.mtime, f.rel))     # oldest first: history replays in order
        return todo, listed, unchanged, skipped

    def _work(self, todo: list, t0: float) -> None:
        q: queue.Queue = queue.Queue()
        for f in todo:
            q.put(f)

        pool = self._pool()

        def worker() -> None:
            try:
                while not self.stop.is_set() and not self._unavailable:
                    with self._lock:
                        if not self._budget_hit:
                            self._budget_hit = self._over_budget(t0)
                        if self._budget_hit:
                            return
                        self._done_docs += 1
                    try:
                        f = q.get_nowait()
                    except queue.Empty:
                        with self._lock:
                            self._done_docs -= 1
                        return
                    self._safe(f, pool)
            finally:
                pass

        n = max(1, min(self.limits.concurrency, len(todo) or 1))
        threads = [threading.Thread(target=worker, daemon=True, name=f"docingest-{i}")
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._close_pool(pool)

    def _pool(self, n: int | None = None) -> queue.Queue:
        """Extraction children are shared by the model streams: reading a file takes a second,
        the model ten — two children feed eight streams, and memory stays flat."""
        pool: queue.Queue = queue.Queue()
        for _ in range(n or max(1, min(self.limits.extract_workers, self.limits.concurrency))):
            pool.put(self.extractor_factory())
        return pool

    @staticmethod
    def _close_pool(pool: queue.Queue) -> None:
        while True:
            try:
                pool.get_nowait().close()
            except queue.Empty:
                return

    def _reconcile(self, t0: float) -> None:
        """Documents read at the same time could not see each other's notes. Those that look
        alike are read once more, one at a time, so that a duplicate or a contradiction
        between them is reported."""
        if not self._recheck:
            return
        pool = self._pool(1)
        try:
            for f in sorted(self._recheck.values(), key=lambda f: (f.mtime, f.rel)):
                if self.stop.is_set() or self._unavailable or self._over_budget(t0):
                    return
                self._safe(f, pool, serial=True)
        finally:
            self._close_pool(pool)

    def _safe(self, f, pool, serial: bool = False) -> None:
        t = time.time()
        try:
            self._process(f, pool, serial, t)
        except llmclient.LLMUnavailable as e:
            self._unavailable = str(e)
            state.event(self.row["id"], f.rel, "deferred", str(e), seconds=time.time() - t)
        except Exception as e:  # noqa: BLE001 — one document never takes the pass down
            detail = f"{type(e).__name__}: {str(e)[:200]}"
            state.file_put(self.src.id, f.rel, size=f.size, mtime=f.mtime, sha256="",
                           status="error", detail=detail, pass_id=self.row["id"])
            state.event(self.row["id"], f.rel, "error", detail, seconds=time.time() - t)
        state.pass_update(self.row["id"])                       # heartbeat
        if self.on_progress:
            try:
                self.on_progress({"rel": f.rel, "tokens": sum(self._tokens)})
            except Exception:  # noqa: BLE001
                pass

    def _put(self, f, sha: str, status: str, detail: str = "", **kw) -> None:
        state.file_put(self.src.id, f.rel, size=f.size, mtime=f.mtime, sha256=sha, status=status,
                       detail=detail, pass_id=self.row["id"], **kw)

    def _process(self, f, pool, serial: bool, t: float) -> None:
        pid, src = self.row["id"], self.src
        if f.size > self.limits.max_file_mb * 1024 * 1024:
            self._put(f, "", "skipped", "too big")        # never opened, not even hashed
            state.event(pid, f.rel, "skipped", f"{f.size // (1024 * 1024)} MB > "
                        f"{self.limits.max_file_mb:g} MB", seconds=time.time() - t)
            return
        sha = sources.sha256_file(Path(f.path))
        prev = state.file_get(src.id, f.rel)
        if not serial and prev and prev["sha256"] == sha and prev["status"] not in RETRY:
            state.file_touch(src.id, f.rel, f.size, f.mtime)     # same bytes, new date
            state.event(pid, f.rel, "unchanged", "same content hash", seconds=time.time() - t)
            return
        ex = pool.get()
        try:
            r = ex.extract(Path(f.path))
        finally:
            pool.put(ex)
        if not r.get("ok"):
            kind = r.get("kind") or "unreadable"
            status = "skipped" if kind in ("too_big", "refused") else "error"
            detail = f"{kind}: {r.get('error', '')}"
            self._put(f, sha, status, detail)
            state.event(pid, f.rel, status, detail, seconds=time.time() - t)
            return
        twin = state.same_text(r["text_sha"], src.id, f.rel)
        if twin is None and not serial:
            # two copies read at the same moment: the first one to arrive reads it, the other
            # is its duplicate (decided here, not by whichever answer comes back first)
            with self._lock:
                first = self._claims.setdefault(r["text_sha"], f.rel)
            if first != f.rel:
                twin = {"source": src.id, "rel": first}
        if twin is not None:
            batches.drop_pending_of(self.batch_id, src.project, src.id, f.rel)
            detail = f"same text as {twin['source']}:{twin['rel']}"
            self._put(f, sha, "duplicate", detail, text_sha=r["text_sha"])
            state.event(pid, f.rel, "duplicate", detail, seconds=time.time() - t)
            return
        doc = agent.Doc(source=src.id, rel=f.rel, sha256=sha, text=r["text"],
                        title=r.get("title") or "", headings=r.get("headings") or [],
                        meta=r.get("meta") or {}, mtime=f.mtime, floor=src.classification,
                        truncated=bool(r.get("truncated")), extractor=r.get("extractor") or "")
        started = time.time()
        query = " ".join([doc.title, " ".join(doc.headings[:12]), doc.text[:2500]])
        # notes of other sources: never above this source's level. Notes of THIS source: also
        # those the model raised above the folder's floor (else a revised decision would never
        # meet the one it revises) — and then the derived notes inherit the highest level shown
        neigh = state.neighbours(src.project, query, max_level=src.classification, limit=6,
                                 exclude_rel=(src.id, f.rel), same_source=src.id)
        have = {n["name"] for n in neigh}
        for m in self.memory_neighbours(src.project, query, src.classification):
            # capped again here: a neighbour above the source's level is never shown, so a
            # derived note cannot carry down what it says
            if m["name"] not in have and int(m.get("level", 2)) <= src.classification:
                neigh.append(m)
                have.add(m["name"])
        neigh = neigh[:8]
        shown = max([int(n.get("level", 2)) for n in neigh], default=src.classification)
        if shown > doc.floor:
            doc.floor = shown                  # the derived inherits the highest level it saw
        system, user = agent.build_prompt(doc, neigh, self.read_date)
        tin = tout = 0
        out = None
        last = ""
        for _attempt in (1, 2):
            try:
                out, use = self.client.complete(system, user, agent.SCHEMA,
                                                max_tokens=COMPLETION_TOKENS)
                tin, tout = tin + use["tokens_in"], tout + use["tokens_out"]
                break
            except llmclient.LLMInvalid as e:
                tin, tout = tin + e.usage["tokens_in"], tout + e.usage["tokens_out"]
                last = str(e)
        with self._lock:
            self._tokens[0] += tin
            self._tokens[1] += tout
        if out is None:
            self._put(f, sha, "error", f"model: {last}", text_sha=r["text_sha"],
                      tokens_in=tin, tokens_out=tout)
            state.event(pid, f.rel, "error", f"model: {last}", tin, tout, time.time() - t)
            return
        res = agent.validate(out, doc, [n["name"] for n in neigh],
                             {n["name"]: f"{n.get('description', '')} {n.get('body', '')}"
                              for n in neigh})
        if doc.floor > src.classification:
            for n in res.notes:
                n.flags.append("level_inherited")
        common = {"text_sha": r["text_sha"], "doc_date": res.document_date,
                  "tokens_in": tin, "tokens_out": tout}
        if res.verdict == "abstain":
            batches.drop_pending_of(self.batch_id, src.project, src.id, f.rel)
            detail = res.reason + (" [injection suspected]" if "injection_suspected" in res.flags
                                   else "")
            self._put(f, sha, "abstained", detail, **common)
            state.event(pid, f.rel, "abstained", detail, tin, tout, time.time() - t)
            return
        if res.verdict == "duplicate":
            batches.drop_pending_of(self.batch_id, src.project, src.id, f.rel)
            detail = f"same content as note {res.duplicate_of}"
            self._put(f, sha, "duplicate", detail, notes=[res.duplicate_of], **common)
            state.event(pid, f.rel, "duplicate", detail, tin, tout, time.time() - t)
            return
        names = self._store_notes(doc, res)
        self._put(f, sha, "noted", f"{len(names)} note(s)", notes=names, **common)
        flags = sorted({fl for n in res.notes for fl in n.flags})
        state.event(pid, f.rel, "noted", f"{len(names)} note(s)"
                    + (f"; flags: {', '.join(flags)}" if flags else "")
                    + (" (reconciled)" if serial else ""), tin, tout, time.time() - t)
        if not serial and self.limits.concurrency > 1:
            ref = res.notes[0]
            mine = f"{ref.title} {ref.description}"
            for late in state.neighbours(src.project, mine, src.classification, limit=3,
                                         exclude_rel=(src.id, f.rel), since=started,
                                         same_source=src.id):
                if late["type"] == "reference" and _similar(
                        mine, f"{late['title']} {late['description']}") >= 0.3:
                    with self._lock:
                        self._recheck[f.rel] = f
                    break

    def _store_notes(self, doc: agent.Doc, res: agent.Outcome) -> list[str]:
        src = self.src
        # this document's earlier notes: a new version of the document REPLACES its reference
        # note (same name) instead of adding a second one
        earlier = state.index_of_file(src.id, doc.rel)
        prev_ref = next((e["name"] for e in earlier if e["type"] == "reference"), None)
        approved_before = {e["name"] for e in earlier if e["status"] == "approved"}
        batches.drop_pending_of(self.batch_id, src.project, src.id, doc.rel)
        with self._lock:                  # name allocation is serial: no two notes, one name
            taken: set[str] = set()
            for n in res.notes:
                if n.kind == "reference":
                    n.name = prev_ref or batches.unique_name(src.project, n.slug, src.id,
                                                             doc.rel, taken)
                else:
                    n.name = batches.unique_name(src.project, "decision-" + n.slug[:44], src.id,
                                                 doc.rel, taken)
                taken.add(n.name)
            stale = sorted(approved_before - taken)
            for n in res.notes:
                if n.name in approved_before:
                    n.flags.append("updates_existing")
            if stale and res.notes:
                # decisions of the previous version this version no longer states: said, kept
                res.notes[0].flags.append("previous_version_notes")
                res.notes[0].links += [s for s in stale if s not in res.notes[0].links]
            for n in res.notes:
                text = agent.render(n, res, doc, self.read_date, res.notes, self.batch_id)
                batches.put_note(self.batch_id, src.project, src.id, doc.rel, name=n.name,
                                 kind=n.kind, level=n.level, title=n.title,
                                 description=n.description, text=text,
                                 body=(n.body or n.extra.get("statement", "")), flags=n.flags)
        for c in res.conflicts:
            # the other side of a contradiction learns about it too, when it still waits
            other = state.index_get(src.project, c["note"])
            if other and other["status"] == "pending" and other["batch_id"]:
                batches.add_flag(other["batch_id"], c["note"], "conflict")
        return [n.name for n in res.notes]

    def _recount(self) -> dict:
        """Counters from the ledger (what IS recorded for this pass), not from memory."""
        pid = self.row["id"]
        c = state.con()
        try:
            by = {r[0]: r[1] for r in c.execute(
                "SELECT status, COUNT(*) FROM files WHERE source=? AND pass_id=? GROUP BY status",
                (self.src.id, pid))}
            kinds = {r[0]: r[1] for r in c.execute(
                "SELECT type, COUNT(*) FROM batch_notes WHERE batch_id=? GROUP BY type",
                (self.batch_id,))}
            flagged = c.execute("SELECT COUNT(*) FROM batch_notes WHERE batch_id=? AND flags<>'[]'",
                                (self.batch_id,)).fetchone()[0]
            gone = c.execute("SELECT COUNT(*) FROM pass_events WHERE pass_id=? AND outcome='gone'",
                             (pid,)).fetchone()[0]
        finally:
            c.close()
        listing = getattr(self, "_listing", {})
        read = sum(by.get(k, 0) for k in ("noted", "abstained", "duplicate", "error"))
        return {"listed": listing.get("listed", 0), "unchanged": listing.get("unchanged", 0),
                "read": read, "noted": by.get("noted", 0), "notes": sum(kinds.values()),
                "decisions": kinds.get("decision", 0), "abstained": by.get("abstained", 0),
                "duplicates": by.get("duplicate", 0), "errors": by.get("error", 0),
                "skipped": by.get("skipped", 0), "gone": gone, "flagged": flagged,
                "tokens_in": self._tokens[0], "tokens_out": self._tokens[1]}


def run_source(source_id: str, kind: str = "incremental", requested_by: str = "", **kw) -> dict:
    src = cfgmod.get(source_id)
    if src is None:
        raise KeyError(source_id)
    if not src.enabled:
        raise RuntimeError(f"source {source_id} is disabled")
    return Pass(src, kind, requested_by, **kw).run()
