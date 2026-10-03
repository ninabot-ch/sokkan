#!/usr/bin/env python3
"""memeval.py — SOKKAN : banc de recall (CortHeXis → Banc) et changement de profil mémoire
(carte « Mémoire » de Magnitude).

Glue SOKKAN autour de `memory/core/eval.py` et `memory/core/switch.py` : routes API,
audit des décisions, banc nocturne, questions générées par le LLM de l'instance
(celui de l'assistant), alerte `notify.py` sur une régression. Rien ne tourne tant que
le store 3.0 n'est pas activé (`CORTHEXIS_MEMORY_BACKEND=postgres`) : les routes le
disent, l'UI l'affiche.

    app.include_router(memeval.router(require, feature_magnitude, _transcripts))
    memeval.start_nightly(_transcripts)        # lifespan

Variables : CORTHEXIS_EVAL_NIGHTLY (heure locale HH:MM, défaut 03:40 ; `off`),
CORTHEXIS_EVAL_GENERATE (questions générées par nuit, défaut 20 ; 0 = jamais),
CORTHEXIS_EVAL_MAX_DROP (perte de MRR tolérée, défaut 0.02), CORTHEXIS_EVAL_MIN_QUESTIONS.
"""
from __future__ import annotations

import datetime
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import audit
import notify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
import store_backend  # noqa: E402

_lock = threading.Lock()
_switcher = None
_running: dict[str, threading.Thread] = {}


# --------------------------------------------------------------------------- plumbing
def available() -> bool:
    return store_backend.enabled()


def _store():
    if not available():
        raise HTTPException(409, "the 3.0 memory store is not enabled on this instance "
                                 "(CORTHEXIS_MEMORY_BACKEND=postgres)")
    try:
        return store_backend.get_store()
    except Exception as e:  # noqa: BLE001 — base injoignable : on le dit
        raise HTTPException(503, f"memory store unreachable: {e}")


def _index_config():
    from core.indexer import IndexConfig

    md = os.environ.get("CORTHEXIS_MEMORY_DIR") or os.environ.get("SOKKAN_MEMORY_DIR") \
        or os.path.expanduser("~/.sokkan/memory")
    return IndexConfig.from_env(memory_dir=Path(md))


def _on_switch_done(job: dict) -> None:
    if job.get("status") == "blocked":
        notify.send("Memory: a profile change waits for a decision",
                    job.get("phase") or "", f"{notify.PUBLIC_URL}/#magnitude", kind="warning")
    elif job.get("status") == "failed":
        notify.send("Memory: the profile change failed", job.get("detail") or "",
                    f"{notify.PUBLIC_URL}/#magnitude", kind="warning")


def switcher():
    global _switcher
    from core import switch

    st = _store()
    with _lock:
        if _switcher is None or _switcher.store is not st:
            _switcher = switch.Switcher(
                st, index_config=_index_config, on_done=_on_switch_done,
                log=lambda m: print(f"[sokkan] memory switch: {m}", file=sys.stderr))
            _switcher.recover()
    return _switcher


def embedder():
    """Embedder qui correspond à la génération active (celui de la configuration sinon)."""
    from core import embed, switch

    return switch.serving_embedder(_store()) or embed.get()


def paraphraser():
    """Le LLM de l'instance (celui de l'assistant), None s'il n'y en a pas."""
    import assistant
    from core import eval as ev

    cfg = assistant._llm_config() or assistant._fallback_config()
    if not cfg:
        return None
    return ev.llm_paraphraser(cfg["url"], cfg["token"], cfg["model"], api=cfg.get("api",
                                                                                  "anthropic"))


def _background(key: str, fn: Callable[[], object]) -> bool:
    """Une seule tâche de fond par clé (run, harvest, generate)."""
    with _lock:
        t = _running.get(key)
        if t and t.is_alive():
            return False

        def wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"[sokkan] memory bench {key} failed: {e!r}", file=sys.stderr)

        _running[key] = threading.Thread(target=wrap, daemon=True, name=f"memeval-{key}")
        _running[key].start()
    return True


def _busy() -> list[str]:
    return [k for k, t in _running.items() if t.is_alive()]


def _profile() -> str | None:
    from core import switch

    try:
        return switch.current_target(_store()).profile
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- nightly
def nightly_once(transcripts: Callable[[], list[Path]]) -> dict:
    from core import eval as ev

    st = _store()
    limit = int(os.environ.get("CORTHEXIS_EVAL_GENERATE", "20") or 0)
    rep = ev.nightly(st, embedder(), transcripts=transcripts(),
                     paraphrase=paraphraser() if limit else None, generate_limit=limit,
                     profile=_profile(),
                     log=lambda m: print(f"[sokkan] memory bench: {m}", file=sys.stderr))
    if rep.get("regression"):
        f = next((x for x in ev.findings(st) if x["check"] == "recall-regression"), None)
        if f:
            notify.send("Memory: " + f["title"], f["detail"],
                        f"{notify.PUBLIC_URL}/#memory", kind="warning")
    return rep


def _next_run(hhmm: str, now: datetime.datetime) -> datetime.datetime:
    h, m = (int(x) for x in hhmm.split(":", 1))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + datetime.timedelta(days=1)


def start_nightly(transcripts: Callable[[], list[Path]]) -> bool:
    """Banc nocturne (thread). No-op sans store 3.0 ou avec CORTHEXIS_EVAL_NIGHTLY=off."""
    when = (os.environ.get("CORTHEXIS_EVAL_NIGHTLY") or "03:40").strip().lower()
    if not available() or when in ("off", "0", "no", "false"):
        return False
    tz = ZoneInfo(os.environ.get("SOKKAN_TZ", "UTC"))

    def loop():
        while True:
            now = datetime.datetime.now(tz)
            time.sleep(max(1.0, (_next_run(when, now) - now).total_seconds()))
            try:
                nightly_once(transcripts)
            except Exception as e:  # noqa: BLE001
                print(f"[sokkan] nightly memory bench failed: {e!r}", file=sys.stderr)

    threading.Thread(target=loop, daemon=True, name="memeval-nightly").start()
    return True


# --------------------------------------------------------------------------- API
class QuestionIn(BaseModel):
    question: str
    notes: list[str]


class StatusIn(BaseModel):
    status: str


class SwitchIn(BaseModel):
    profile: str
    model: str | None = None
    urls: list[str] = []
    rerank_url: str | None = None
    rebuild: bool = False


class LicenceIn(BaseModel):
    decision: str           # accepted | declined
    download: bool = True   # accepted → fetch the model now (if the models volume is writable)


def router(require: Callable, feature_magnitude: Callable,
           transcripts: Callable[[], list[Path]]) -> APIRouter:
    r = APIRouter()
    viewer, dev, admin = Depends(require("viewer")), Depends(require("dev")), \
        Depends(require("admin"))

    # ---- banc (CortHeXis → Banc)
    @r.get("/api/memory/eval")
    def eval_overview(_u: dict = viewer) -> dict:
        from core import eval as ev

        if not available():
            return {"available": False,
                    "reason": "The recall bench needs the 3.0 memory store "
                              "(CORTHEXIS_MEMORY_BACKEND=postgres)."}
        st = _store()
        return {"available": True, **ev.overview(st), "busy": _busy(),
                "llm": paraphraser() is not None}

    @r.get("/api/memory/eval/questions")
    def eval_questions(source: str | None = None, status: str | None = None,
                       _u: dict = viewer) -> list[dict]:
        from core import eval as ev

        return [q.as_dict() for q in ev.list_questions(_store(), source=source, status=status,
                                                        limit=500)]

    @r.post("/api/memory/eval/questions")
    def eval_add(body: QuestionIn, u: dict = dev) -> dict:
        from core import eval as ev

        try:
            q = ev.add_question(_store(), body.question, body.notes, source="client",
                                author=u["email"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.log(u["email"], "memory.eval.question", str(q.id), q.question[:200])
        return q.as_dict()

    @r.post("/api/memory/eval/questions/{qid}/status")
    def eval_status(qid: int, body: StatusIn, u: dict = dev) -> dict:
        from core import eval as ev

        try:
            ok = ev.set_question_status(_store(), qid, body.status)
        except ValueError:
            raise HTTPException(400, "status must be active or disabled")
        if not ok:
            raise HTTPException(404, "unknown question")
        audit.log(u["email"], f"memory.eval.question.{body.status}", str(qid))
        return {"ok": True}

    @r.delete("/api/memory/eval/questions/{qid}")
    def eval_delete(qid: int, u: dict = dev) -> dict:
        from core import eval as ev

        if not ev.delete_question(_store(), qid):
            raise HTTPException(404, "unknown question")
        audit.log(u["email"], "memory.eval.question.delete", str(qid))
        return {"ok": True}

    @r.post("/api/memory/eval/run")
    def eval_run(u: dict = dev) -> dict:
        from core import eval as ev

        st = _store()
        emb = embedder()

        def job():
            res = ev.run(st, emb, profile=_profile(), trigger="manual")
            ev.check_regression(st, res["id"])

        if not _background("run", job):
            raise HTTPException(409, "a bench run is already in progress")
        audit.log(u["email"], "memory.eval.run")
        return {"started": True}

    @r.post("/api/memory/eval/harvest")
    def eval_harvest(u: dict = dev) -> dict:
        from core import eval as ev

        rep = ev.harvest(_store(), transcripts())
        audit.log(u["email"], "memory.eval.harvest", "", f"{rep['new']} new")
        return rep

    @r.post("/api/memory/eval/generate")
    def eval_generate(u: dict = dev) -> dict:
        from core import eval as ev

        para = paraphraser()
        if para is None:
            raise HTTPException(409, "no language model configured on this instance")
        st = _store()
        if not _background("generate", lambda: ev.generate(st, para, limit=20)):
            raise HTTPException(409, "questions are already being written")
        audit.log(u["email"], "memory.eval.generate")
        return {"started": True}

    @r.get("/api/memory/eval/compare")
    def eval_compare(base: int, new: int, _u: dict = viewer) -> dict:
        from core import eval as ev

        return ev.compare(_store(), base, new)

    @r.get("/api/memory/eval/findings")
    def eval_findings(_u: dict = viewer) -> list[dict]:
        from core import eval as ev

        return ev.findings(_store()) if available() else []

    # ---- carte « Mémoire » de Magnitude : changement de profil, retour arrière, licence
    @r.get("/api/magnitude/memory/switch")
    def switch_state(_u: dict = viewer, _f: None = Depends(feature_magnitude)) -> dict:
        if not available():
            return {"available": False}
        return {"available": True, **switcher().status()}

    @r.post("/api/magnitude/memory/switch")
    def switch_start(body: SwitchIn, u: dict = admin,
                     _f: None = Depends(feature_magnitude)) -> dict:
        from core import switch

        t = switch.Target(profile=body.profile, model=body.model or None,
                          urls=[x.strip() for x in body.urls if x.strip()],
                          rerank_url=body.rerank_url or None)
        try:
            job = switcher().start(t, u["email"], rebuild=body.rebuild)
        except switch.SwitchError as e:
            raise HTTPException(409, str(e))
        audit.log(u["email"], "memory.profile.switch", str(job["id"]),
                  f"{body.profile} model={body.model or 'installed'} rebuild={body.rebuild}")
        return job

    def _decide(fn: str, job_id: int, u: dict) -> dict:
        from core import switch

        try:
            job = getattr(switcher(), fn)(job_id, u["email"])
        except switch.SwitchError as e:
            raise HTTPException(409, str(e))
        audit.log(u["email"], f"memory.profile.{fn}", str(job_id))
        return job

    @r.post("/api/magnitude/memory/switch/{job_id}/approve")
    def switch_approve(job_id: int, u: dict = admin,
                       _f: None = Depends(feature_magnitude)) -> dict:
        return _decide("approve", job_id, u)

    @r.post("/api/magnitude/memory/switch/{job_id}/cancel")
    def switch_cancel(job_id: int, u: dict = admin,
                      _f: None = Depends(feature_magnitude)) -> dict:
        return _decide("cancel", job_id, u)

    @r.post("/api/magnitude/memory/rollback")
    def switch_rollback(u: dict = admin, _f: None = Depends(feature_magnitude)) -> dict:
        from core import switch

        try:
            job = switcher().rollback(u["email"])
        except switch.SwitchError as e:
            raise HTTPException(409, str(e))
        audit.log(u["email"], "memory.profile.rollback", str(job["id"]),
                  f"back to generation {job['to_generation']}")
        return job

    @r.post("/api/magnitude/memory/licence")
    def licence(body: LicenceIn, u: dict = admin,
                _f: None = Depends(feature_magnitude)) -> dict:
        """Décision sur les Gemma Terms of Use depuis l'UI : licence.json (historique) +
        journal d'audit SOKKAN. Acceptée → téléchargement du modèle en tâche de fond."""
        from core import models

        if body.decision not in ("accepted", "declined"):
            raise HTTPException(400, "decision must be accepted or declined")
        root = models.models_dir()
        try:
            root.mkdir(parents=True, exist_ok=True)
            st = models.record_decision(body.decision, u["email"], "ui", root)
        except OSError as e:
            raise HTTPException(409, f"the models folder is read-only here ({e}); answer with "
                                     "./scripts/memory-setup.sh --accept or --decline")
        audit.log(u["email"], f"memory.licence.gemma.{body.decision}", "gemma",
                  f"terms {models.GEMMA_TERMS_VERSION}")
        fetching = False
        if body.decision == "accepted" and body.download \
                and not models.present(models.DEFAULT_EMBED, root):
            fetching = _background("download", lambda: models.download(
                models.DEFAULT_EMBED, root,
                log=lambda m: print(f"[sokkan] {m}", file=sys.stderr)))
        return {"licence": {k: v for k, v in st.items() if k != "history"},
                "downloading": fetching or "download" in _busy(),
                "installed": models.present(models.DEFAULT_EMBED, root)}

    return r
