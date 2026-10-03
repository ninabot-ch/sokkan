"""Memory profile switch (P0-1 b): build in the background, switch only if the bench agrees.

Changing profile (leger / standard / gpu), model or embedding servers goes through a
*switch job*:

1. **building** — the target embedder is checked (servers answer, serve the model they
   claim, right dimension). If the target embeds with *another* model than the active
   index generation (``identity()`` differs), a new generation is built by the indexer
   while the current one keeps serving every search; the same model on other servers (CPU
   to GPU, reranker added) reuses the active generation — its vectors are the same — unless
   a rebuild is asked for.
2. **evaluating** — the recall bench (``core.eval``) runs the same questions on the current
   setup and on the target.
3. **switched** — when the target does not lose more than ``max_drop`` of MRR on at least
   ``eval.MIN_COMMON`` shared questions, the generation is activated in one transaction
   (``Store.activate_generation``) and the target recorded as how that generation is
   served (``generation_profiles``). Otherwise **blocked**: nothing changes until a person
   approves (``approve``) or discards (``cancel``) it.

The previous generation is retired, kept ``retention_days`` (7) and restorable with
``rollback`` in one call; ``Store.purge_retired`` drops it afterwards (nightly bench).
Jobs, comparisons and decisions are stored in ``index_switches`` (history).

Self-contained (CortHeXis memory-core). The application runs ``Switcher.start`` from its
API and shows ``Switcher.status``; the query side asks ``serving_embedder(store)`` for the
embedder that matches the active generation.
"""
from __future__ import annotations

import datetime
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import embed as _embed
from . import eval as _eval
from . import models as _models
from .config import env, env_int

RETENTION_DAYS = 7
RUNNING = ("building", "evaluating")


class SwitchError(RuntimeError):
    pass


# --------------------------------------------------------------------------- targets

@dataclass
class Target:
    """How a generation is served: profile, embedding model, servers."""
    profile: str
    model: str | None = None            # registry key; None = the installed model
    urls: list[str] = field(default_factory=list)   # [] = configured / profile default
    rerank_url: str | None = None       # None = configured / profile default; "off" = none

    @classmethod
    def from_dict(cls, d: dict | None) -> "Target | None":
        if not d or not d.get("profile"):
            return None
        return cls(profile=d["profile"], model=d.get("model") or None,
                   urls=[u for u in (d.get("urls") or []) if u],
                   rerank_url=d.get("rerank_url") or None)

    def as_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        if self.profile not in _embed.PROFILES:
            raise SwitchError(f"unknown profile {self.profile!r}")
        if self.model and self.model not in _models.MODELS:
            raise SwitchError(f"unknown model {self.model!r}")
        if self.model and _models.MODELS[self.model]["kind"] != "embed":
            raise SwitchError(f"{self.model!r} is not an embedding model")
        for u in [*self.urls, *([self.rerank_url] if self.rerank_url not in (None, "off")
                                else [])]:
            if not u.startswith(("http://", "https://")):
                raise SwitchError(f"server address must start with http:// or https:// ({u})")


def build_embedder(target: Target):
    """Embedder for a target. Servers: the target's, else ``CORTHEXIS_EMBED_URLS``, else
    the profile's defaults; reranker likewise, only for profiles that rerank."""
    target.validate()
    if target.profile in ("legacy", "remote"):
        return _embed.legacy()
    model = target.model or _embed._active_model_key()
    if model not in _models.MODELS:
        raise SwitchError(f"unknown model {model!r}")
    urls = target.urls or _embed._split(
        env("EMBED_URLS") or env("EMBED_URL")
        or _embed.DEFAULT_URLS.get(target.profile, _embed.DEFAULT_URL))
    policy = _embed.RERANK_POLICY[target.profile]
    rr = target.rerank_url
    if rr is None:
        rr = env("RERANK_URL") or (_embed.DEFAULT_RERANK_URL if policy != "off" else None)
    if not rr or rr.lower() in ("off", "none", "0") or policy == "off":
        rr = None
    return _embed.LlamaCppEmbedder(model, urls, rr, policy, env_int("EMBED_MAX_TOKENS", 500))


def preflight(embedder, timeout: float = 5.0) -> list[str]:
    """Problems that make a target unusable (empty list = fine): no server answers, a
    server serves another model file, the dimension is wrong."""
    problems: list[str] = []
    if not isinstance(embedder, _embed.LlamaCppEmbedder):
        try:
            embedder.embed_query("probe")
        except Exception as e:  # noqa: BLE001
            problems.append(f"the embedding service does not answer ({e})")
        return problems
    import httpx

    want = embedder.spec["file"].lower()
    alive = 0
    for url in embedder.urls:
        try:
            r = httpx.get(f"{url}/v1/models", timeout=timeout)
            r.raise_for_status()
        except Exception:  # noqa: BLE001 — a server down is fine if another answers
            continue
        alive += 1
        served = [str(m.get("id") or m.get("model") or "") for m in r.json().get("data") or []]
        if served and not any(s.lower().rsplit("/", 1)[-1] == want for s in served):
            problems.append(f"{url} serves {served[0].rsplit('/', 1)[-1]}, not {want}")
    if not alive:
        problems.append("no memory server of this profile answers — start them first "
                        "(./scripts/memory-setup.sh --profile <profile>, then docker compose up -d)")
        return problems
    try:
        embedder.embed_query("probe", timeout=timeout * 2)
    except Exception as e:  # noqa: BLE001
        problems.append(f"the memory server refuses queries ({e})")
    return problems


def configured_target() -> Target:
    """The target of the instance configuration (environment)."""
    return Target(profile=_embed.current_profile())


def record_target(store, generation: int, target: Target) -> None:
    from psycopg.types.json import Jsonb

    with store.pool.connection() as con:
        con.execute(
            "INSERT INTO generation_profiles (generation_id, profile, target) VALUES (%s, %s, %s) "
            "ON CONFLICT (generation_id) DO UPDATE SET profile = excluded.profile, "
            "target = excluded.target, updated_at = now()",
            (int(generation), target.profile, Jsonb(target.as_dict())))


def target_of(store, generation: int) -> Target | None:
    with store.pool.connection() as con:
        r = con.execute("SELECT target FROM generation_profiles WHERE generation_id = %s",
                        (int(generation),)).fetchone()
    return Target.from_dict(r["target"]) if r else None


def current_target(store) -> Target:
    """How the active generation is served: recorded by the last switch, else the
    configuration."""
    g = store.active_generation()
    t = target_of(store, g.id) if g else None
    return t or configured_target()


def serving_embedder(store, generation=None, factory: Callable[[Target], object] | None = None):
    """Embedder whose identity matches ``generation`` (active by default), or None when
    neither the recorded target nor the configuration embeds with that model — querying
    a generation with another model would silently return noise."""
    g = store.active_generation() if generation is None else store.get_generation(int(generation))
    if g is None:
        return None
    for t in (target_of(store, g.id), configured_target()):
        if t is None:
            continue
        try:
            e = (factory or build_embedder)(t)
        except Exception:  # noqa: BLE001
            continue
        if e.identity() == g.embed_identity:
            return e
    return None


# --------------------------------------------------------------------------- progress

class _CountingEmbedder:
    """Wraps the target embedder during a build to report progress (texts embedded / the
    chunk count of the active generation)."""

    def __init__(self, inner, total: int, report: Callable[[float], None]):
        self.inner, self.total, self.report, self.done = inner, max(1, total), report, 0

    def identity(self) -> str:
        return self.inner.identity()

    def embed_docs(self, texts):
        out = self.inner.embed_docs(texts)
        if texts != ["dimension probe"]:
            self.done += len(texts)
            self.report(min(0.99, self.done / self.total))
        return out


# --------------------------------------------------------------------------- jobs

_SW_COLS = ("id, kind, status, phase, progress, detail, from_generation, from_target, "
            "to_generation, to_target, built, baseline_run, candidate_run, max_drop, comparison, "
            "requested_by, decided_by, started_at, updated_at, finished_at")


def _job(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    for k in ("started_at", "updated_at", "finished_at"):
        d[k] = d[k].isoformat() if d.get(k) else None
    return d


class Switcher:
    def __init__(self, store, *, index_config=None, max_drop: float | None = None,
                 min_questions: int | None = None, retention_days: int = RETENTION_DAYS,
                 log: Callable[[str], None] | None = None,
                 on_done: Callable[[dict], None] | None = None,
                 embedder_factory: Callable[[Target], object] = build_embedder,
                 check: Callable[[object], list[str]] = preflight):
        self.store = store
        self.index_config = index_config
        self.max_drop = _eval.max_drop() if max_drop is None else max_drop
        self.min_questions = (env_int("EVAL_MIN_QUESTIONS", _eval.MIN_COMMON)
                              if min_questions is None else min_questions)
        self.retention_days = retention_days
        self.log = log or (lambda m: None)
        self.on_done = on_done
        self.embedder_factory = embedder_factory
        self.check = check
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- state
    def get(self, job_id: int) -> dict | None:
        with self.store.pool.connection() as con:
            return _job(con.execute(f"SELECT {_SW_COLS} FROM index_switches WHERE id = %s",
                                    (int(job_id),)).fetchone())

    def history(self, limit: int = 10) -> list[dict]:
        with self.store.pool.connection() as con:
            return [_job(r) for r in con.execute(
                f"SELECT {_SW_COLS} FROM index_switches ORDER BY id DESC LIMIT %s",
                (int(limit),)).fetchall()]

    def _update(self, job_id: int, **fields) -> None:
        from psycopg.types.json import Jsonb

        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            vals.append(Jsonb(v) if isinstance(v, (dict, list)) else v)
        if fields.get("status") in ("switched", "blocked", "failed", "cancelled", "rolled_back"):
            sets.append("finished_at = coalesce(finished_at, now())")
        with self.store.pool.connection() as con:
            con.execute(f"UPDATE index_switches SET {', '.join(sets)}, updated_at = now() "
                        "WHERE id = %s", (*vals, job_id))

    def recover(self) -> int:
        """Jobs left running by a restarted process are marked failed (a new start resumes
        the generation that was being built: the indexer picks it up)."""
        with self.store.pool.connection() as con:
            return con.execute(
                "UPDATE index_switches SET status = 'failed', finished_at = now(), "
                "detail = 'interrupted (restart) — start the change again, the work done is kept' "
                "WHERE status IN ('building', 'evaluating')").rowcount

    def status(self) -> dict:
        """Current serving setup, the latest jobs, and the generations with their rollback
        deadline."""
        g = self.store.active_generation()
        cur = current_target(self.store)
        gens = []
        for x in self.store.list_generations():
            until = None
            if x.status == "retired" and x.retired_at:
                ra = datetime.datetime.fromisoformat(x.retired_at)
                until = (ra + datetime.timedelta(days=self.retention_days)).isoformat()
            t = target_of(self.store, x.id)
            gens.append({"id": x.id, "status": x.status, "identity": x.embed_identity,
                         "dim": x.dim, "chunks": x.chunk_count, "created_at": x.created_at,
                         "activated_at": x.activated_at, "retired_at": x.retired_at,
                         "rollback_until": until, "profile": t.profile if t else None})
        hist = self.history(10)
        return {"active_generation": g.id if g else None,
                "identity": g.embed_identity if g else None,
                "current": cur.as_dict(), "job": hist[0] if hist else None,
                "history": hist, "generations": gens, "max_drop": self.max_drop,
                "min_questions": self.min_questions, "retention_days": self.retention_days,
                "rollback": self.rollback_candidate()}

    # ---------------------------------------------------------------- start
    def start(self, target: Target, requested_by: str = "", *, rebuild: bool = False,
              background: bool = True) -> dict:
        """Create a switch job and run it (in a thread unless ``background=False``)."""
        target.validate()
        g = self.store.active_generation()
        cur = current_target(self.store)
        from psycopg import errors
        from psycopg.types.json import Jsonb

        try:
            with self.store.pool.connection() as con:
                jid = con.execute(
                    "INSERT INTO index_switches (from_generation, from_target, to_target, "
                    "max_drop, requested_by, phase) VALUES (%s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (g.id if g else None, Jsonb(cur.as_dict()),
                     Jsonb({**target.as_dict(), "rebuild": bool(rebuild)}),
                     self.max_drop, requested_by, "checking the memory servers")
                ).fetchone()["id"]
        except errors.UniqueViolation:
            raise SwitchError("a change is already in progress") from None
        if background:
            self._thread = threading.Thread(target=self.run_job, args=(jid,), daemon=True,
                                            name=f"memory-switch-{jid}")
            self._thread.start()
        else:
            self.run_job(jid)
        return self.get(jid)

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ---------------------------------------------------------------- the job
    def run_job(self, job_id: int) -> dict:
        job = self.get(job_id)
        target = Target.from_dict(job["to_target"])
        rebuild = bool(job["to_target"].get("rebuild"))
        try:
            self._run(job_id, job, target, rebuild)
        except Exception as e:  # noqa: BLE001 — the job records why it stopped
            self.log(f"memory switch {job_id} failed: {e!r}")
            self._update(job_id, status="failed", detail=str(e)[:500])
        done = self.get(job_id)
        if self.on_done:
            try:
                self.on_done(done)
            except Exception:  # noqa: BLE001
                pass
        return done

    def _run(self, job_id: int, job: dict, target: Target, rebuild: bool) -> None:
        store = self.store
        embedder = self.embedder_factory(target)
        problems = self.check(embedder)
        if problems:
            raise SwitchError("; ".join(problems))
        ident = embedder.identity()
        active = store.active_generation()
        from_target = Target.from_dict(job["from_target"]) or configured_target()

        # 1. candidate generation
        if active is not None and active.embed_identity == ident and not rebuild:
            cand = active.id
            self._update(job_id, to_generation=cand, progress=0.5,
                         phase="same model: the current index is reused")
        else:
            if self.index_config is None:
                raise SwitchError("no notes folder configured: cannot build a new index")
            from .indexer import Indexer

            total = active.chunk_count if active else 0
            if not total:
                with store.pool.connection() as con:
                    total = int(con.execute("SELECT count(*) AS n FROM notes").fetchone()["n"]
                                * 1.5)
            self._update(job_id, phase="building the new index (the current one keeps serving)",
                         progress=0.0)
            last = [0.0]

            def report(p: float) -> None:
                if p - last[0] >= 0.01:
                    last[0] = p
                    self._update(job_id, progress=round(p * 0.7, 3))

            cfg = self.index_config() if callable(self.index_config) else self.index_config
            for k, v in (("auto_activate", False), ("write_index", False), ("normalize", False)):
                setattr(cfg, k, v)
            rep = Indexer(store, _CountingEmbedder(embedder, total, report), cfg,
                          log=self.log).run(rebuild=rebuild and active is not None
                                            and active.embed_identity == ident)
            cand = rep.generation
            store.build_index(cand)
            self._update(job_id, to_generation=cand, built=True, progress=0.7)

        # 2. bench: same questions, current setup vs target
        self._update(job_id, status="evaluating", phase="measuring recall on your questions")
        qids = [q.id for q in _eval.list_questions(store)]
        base_run = None
        if active is not None and qids:
            cur_emb = serving_embedder(store, active.id, self.embedder_factory)
            if cur_emb is not None:
                try:
                    base_run = _eval.run(store, cur_emb, generation=active.id,
                                         profile=from_target.profile, trigger="switch",
                                         question_ids=qids,
                                         progress=lambda p: self._prog(job_id, 0.7, 0.15, p)
                                         )["id"]
                except Exception as e:  # noqa: BLE001 — fall back on the last measure
                    self.log(f"baseline run failed: {e!r}")
            if base_run is None:
                base_run = self._last_run(active.id, from_target.profile)
        cand_run = _eval.run(store, embedder, generation=cand, profile=target.profile,
                             trigger="switch", question_ids=qids,
                             progress=lambda p: self._prog(job_id, 0.85, 0.15, p))["id"] \
            if qids else None
        self._update(job_id, baseline_run=base_run, candidate_run=cand_run)

        # 3. decision
        if active is None:
            cmp = {"enough": True, "regressed": False, "reason": "first index"}
        elif base_run is None or cand_run is None:
            cmp = {"enough": False, "regressed": False, "common": 0,
                   "reason": "no question to compare on"}
        else:
            cmp = _eval.compare(store, base_run, cand_run, max_drop_=self.max_drop)
            cmp["enough"] = cmp["common"] >= self.min_questions
            d = cmp["delta"]["mrr"]
            cmp["regressed"] = bool(cmp["enough"] and d is not None and d < -self.max_drop)
        if cmp["enough"] and not cmp["regressed"]:
            self._activate(cand, target)
            self._update(job_id, status="switched", progress=1.0, comparison=cmp,
                         decided_by="bench", phase="switched")
        else:
            why = ("recall would drop" if cmp.get("regressed")
                   else f"not enough questions to judge (needs {self.min_questions})")
            self._update(job_id, status="blocked", progress=1.0, comparison=cmp,
                         phase=f"waiting for a decision: {why}")

    def _prog(self, job_id: int, start: float, span: float, p: float) -> None:
        if int(p * 20) != int((p - 0.0001) * 20):
            self._update(job_id, progress=round(start + span * p, 3))

    def _last_run(self, generation: int, profile: str | None) -> int | None:
        with self.store.pool.connection() as con:
            r = con.execute(
                "SELECT id FROM eval_runs WHERE status = 'done' AND generation_id = %s "
                "AND profile IS NOT DISTINCT FROM %s ORDER BY id DESC LIMIT 1",
                (generation, profile)).fetchone()
        return r["id"] if r else None

    def _activate(self, generation: int, target: Target) -> None:
        g = self.store.get_generation(generation)
        if g.status != "active":
            self.store.activate_generation(generation)   # atomic; the old one is retired
        record_target(self.store, generation, target)
        _embed.reset()

    # ---------------------------------------------------------------- decisions
    def approve(self, job_id: int, by: str) -> dict:
        """Switch a blocked job anyway (explicit decision of a person)."""
        job = self.get(job_id)
        if not job or job["status"] != "blocked":
            raise SwitchError("only a change waiting for a decision can be approved")
        active = self.store.active_generation()
        if (active.id if active else None) != job["from_generation"]:
            raise SwitchError("the memory changed since this measure: start the change again")
        self._activate(job["to_generation"], Target.from_dict(job["to_target"]))
        self._update(job_id, status="switched", decided_by=by, phase="switched (approved)")
        return self.get(job_id)

    def cancel(self, job_id: int, by: str) -> dict:
        """Discard a blocked (or failed) job; a generation built for it is dropped."""
        job = self.get(job_id)
        if not job or job["status"] not in ("blocked", "failed"):
            raise SwitchError("only a blocked or failed change can be discarded")
        gid = job["to_generation"]
        if job["built"] and gid:
            g = self.store.get_generation(gid)
            if g and g.status == "building":
                self.store.drop_generation(gid)
        self._update(job_id, status="cancelled", decided_by=by, phase="discarded")
        return self.get(job_id)

    def rollback_candidate(self) -> dict | None:
        """The last switch that can still be undone (within the retention window)."""
        with self.store.pool.connection() as con:
            r = con.execute(
                f"SELECT {_SW_COLS} FROM index_switches WHERE status = 'switched' "
                "AND kind = 'switch' AND finished_at > now() - %s ORDER BY id DESC LIMIT 1",
                (datetime.timedelta(days=self.retention_days),)).fetchone()
        job = _job(r)
        if not job or job["from_generation"] is None:
            return None
        active = self.store.active_generation()
        if not active or active.id != job["to_generation"]:
            return None                      # something else happened since
        prev = self.store.get_generation(job["from_generation"])
        if prev is None:
            return None                      # purged
        return {"switch": job["id"], "to_generation": prev.id,
                "to_target": job["from_target"], "since": job["finished_at"]}

    def rollback(self, by: str) -> dict:
        """Back to the generation and setup in service before the last switch (one call;
        no bench: it is the state that was measured before)."""
        from psycopg.types.json import Jsonb

        cand = self.rollback_candidate()
        if not cand:
            raise SwitchError("nothing to roll back (no recent change, or its index was purged)")
        target = Target.from_dict(cand["to_target"]) or configured_target()
        active = self.store.active_generation()
        with self.store.pool.connection() as con:
            jid = con.execute(
                "INSERT INTO index_switches (kind, status, from_generation, from_target, "
                "to_generation, to_target, requested_by, decided_by, phase, progress, "
                "finished_at) VALUES ('rollback', 'switched', %s, %s, %s, %s, %s, %s, "
                "'rolled back', 1, now()) RETURNING id",
                (active.id, Jsonb(current_target(self.store).as_dict()), cand["to_generation"],
                 Jsonb(target.as_dict()), by, by)).fetchone()["id"]
        self._activate(cand["to_generation"], target)
        self._update(cand["switch"], status="rolled_back", decided_by=by)
        return self.get(jid)


def wait_job(switcher: Switcher, job_id: int, timeout: float = 60.0) -> dict:
    """Poll a job until it leaves building / evaluating (tests, CLI)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = switcher.get(job_id)
        if j["status"] not in RUNNING:
            return j
        time.sleep(0.05)
    raise TimeoutError(f"switch {job_id} still running")
