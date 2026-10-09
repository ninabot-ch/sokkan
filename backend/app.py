#!/usr/bin/env python3
"""app.py — SOKKAN P1 backend (FastAPI, gmk1).

Read-only API over the Claude Code session transcripts so the web cockpit can
render every session as a clean live chat (the "Sessions" tab). Also exposes the
live tmux session/window topology (for labelling and the future terminal toggle).

  GET /api/health
  GET /api/sessions?limit=&active_within=   → rail list (quick summaries, mtime desc)
  GET /api/sessions/{session_id}            → full parsed chat messages
  GET /api/tmux                             → live tmux sessions/windows

Run (dev):  /opt/sokkan/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8097 --reload
"""
from __future__ import annotations

import asyncio
import json
import os
from features import env_num
import re
import secrets
from contextlib import asynccontextmanager
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jwt

# logique de recherche RAG partagée avec le serveur MCP (une seule source de ranking)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
import index_memory  # noqa: E402
import memory_migration  # noqa: E402 — 2.x -> 3.0 memory migration (store_backend auto)
import missions  # noqa: E402
import memory_search_server as mem  # noqa: E402
import store_backend  # noqa: E402 — 3.0 store (CortHeXis)

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from pydantic import BaseModel

import assistant
import audit
import features
import auth
import board
import budgets
import cfaccess  # noqa: F401 — utilisé via auth.py (mode cf-access)
import demo_captains  # 3.2.2 public demo « Captains » (write guard, org view)
import iam
import infra
import oidc
import oidc_logout  # 3.4: OIDC back-channel / front-channel logout
import revocation  # 3.2 lot 6: SCIM, « Revoke now », owner checks
import sandbox  # 3.2 lot 8: per-project confinement of sessions
import agentchat
import agentcost
import agents
import agents_runtime
import session as sess
import termproxy
import memorykb
import corthexis
import memrecall
import playbooks
import edge
import notify
import observability
import vault
import fleet
import fleetterm
import instance
import llm
import hostnames
import magnitude
import memeval
import panestate
import preview
import previewenv
import classification
import projectgate
import projects
import forge.routes as forge_routes
import helm  # 3.3 Helm : hiérarchie, avancement, suggestions, brief
import helm_api
import provision
import transcript as T
import updatecheck
import usage as usage_mod

_claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
_cwd_slug = (os.environ.get("SOKKAN_AGENT_CWD")
             or ("/workspace" if os.path.isdir("/workspace") else os.getcwd())).replace("/", "-")
PROJECT_DIR = Path(
    os.environ.get("SOKKAN_PROJECT_DIR", os.path.join(_claude_dir, "projects", _cwd_slug))
)
ACTIVE_WINDOW_S = 120  # a session whose transcript changed within this is "active"

REINDEX_S = env_num("SOKKAN_REINDEX_S", 120.0, float)


_index_runner = None  # 3.0: core.indexer.IndexRunner (store backend)


def _start_store_indexer() -> None:
    """3.0 : l'indexeur CortHeXis écrit dans le store Postgres — au démarrage, sur
    changement de fichiers (signature du dossier, toutes les CORTHEXIS_WATCH_S) et
    périodiquement (CORTHEXIS_REINDEX_S), normalisation des notes comprise."""
    global _index_runner
    from core.indexer import IndexConfig, IndexRunner

    # Écrit avec l'embedder qui sert la génération active (profil choisi dans l'UI) ;
    # n'active QUE la première génération (instance neuve) : un changement de modèle passe
    # par la bascule mesurée (core.switch, banc avant bascule), jamais par cette boucle.
    cfg = IndexConfig.from_env(memory_dir=index_memory.MEMORY_DIR)
    _index_runner = IndexRunner(store_backend.get_store, store_backend.index_embedder, cfg,
                                activate_first_only=True)
    _index_runner.start()
    _sync_project_indexers()
    threading.Thread(target=_warm_query_embedder, daemon=True, name="sokkan-embed-warm").start()


def _usage_refresher() -> None:
    """3.4.4 : garde le cache de Costs à jour en fond (SOKKAN_USAGE_REFRESH_S, 120 s ;
    0 = off). Sans ça, le 1er affichage après un redémarrage re-parsait tous les
    transcripts modifiés dans la requête ; au-delà de 30 s le proxy Next coupait
    (« socket hang up ») → Costs en 500 au premier chargement."""
    every = features.env_num("SOKKAN_USAGE_REFRESH_S", 120)
    if every <= 0:
        return
    while True:
        try:
            usage_mod.refresh()
        except Exception as e:  # noqa: BLE001 — the request path still refreshes
            print(f"[sokkan] usage refresh: {e!r}", file=sys.stderr)
        time.sleep(every)


def _warm_query_embedder() -> None:
    """3.4.4 : charge le modèle d'embedding de requête dès le démarrage. Un fastembed local
    met 4-13 s à se charger ; payé au premier prompt, il dépassait le délai du hook de
    rappel (5 s) et bloquait les premiers tours d'une session."""
    try:
        store_backend.embedder().embed_query("warm-up")
    except Exception as e:  # noqa: BLE001 — never fatal: the first recall pays it instead
        print(f"[sokkan] embedder warm-up skipped: {e!r}", file=sys.stderr)


_project_runners: dict = {}


def _sync_project_indexers() -> None:
    """3.2 lot 3 : un indexeur par dossier mémoire de projet (shared compris). Le projet
    default garde l'indexeur ci-dessus ; les autres n'écrivent qu'une fois la première
    génération active (jamais deux générations créées en course au premier démarrage)."""
    if _index_runner is None or not store_backend.enabled():
        return
    from core.indexer import IndexConfig, IndexRunner

    for p in projects.list_projects():
        slug = p["slug"]
        if slug == projects.DEFAULT_PROJECT or slug in _project_runners:
            continue
        d = store_backend.memory_dir_for(slug)
        d.mkdir(parents=True, exist_ok=True)
        cfg = IndexConfig.from_env(memory_dir=d, project=slug, normalize=False)

        def _start(cfg=cfg, slug=slug) -> None:
            while True:
                try:
                    if store_backend.get_store().active_generation() is not None:
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(10)
            r = IndexRunner(store_backend.get_store, store_backend.index_embedder, cfg,
                            activate_first_only=True)
            _project_runners[slug] = r
            r.start()

        _project_runners[slug] = None
        threading.Thread(target=_start, daemon=True, name=f"sokkan-index-{slug}").start()


def _reindex_loop() -> None:
    """Réindexation mémoire in-process (remplace la boucle shell qui respawnait
    un python à chaque tick) : le modèle d'embeddings reste chaud dans le module
    `embeddings`, et on ne réindexe que si le corpus a changé (count + max mtime).
    1re itération = l'index de boot."""
    last_sig: tuple | None = None
    while True:
        try:
            sig = index_memory.corpus_signature()
            if sig != last_sig:
                index_memory.run_index()
                last_sig = sig
        except FileNotFoundError:
            pass  # memory dir pas encore créé (aucune note écrite) → retenter
        except Exception as e:  # noqa: BLE001
            print(f"[sokkan] memory reindex failed: {e}", file=sys.stderr)
        time.sleep(REINDEX_S)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 3.3: which secrets provider, why; an unreachable OpenBao is said, never fatal here
    # (secret reads then answer 503 — fail-closed — until it comes back)
    import secrets_provider as _sp
    for _line in _sp.startup_report():
        print(_line, file=sys.stderr)
    try:
        notice = vault.upgrade_notice()   # 3.2 : secrets nommés par défaut — le dire au démarrage
    except _sp.SecretsError:
        notice = None
    if notice:
        print(f"[sokkan] {notice}", file=sys.stderr)
    # 3.2 multi-user : tables projets + projet « default » (idempotent, rien n'est déplacé)
    try:
        projects.init()
    except Exception as e:  # noqa: BLE001 — le cockpit démarre ; le rappel reste fail-closed
        print(f"[sokkan] projects migration failed: {e!r}", file=sys.stderr)
    if store_backend.enabled():
        _start_store_indexer()
    elif memory_migration.active():
        # 2.x -> 3.0: memory.db (read-only) serves while the memory is migrated in the
        # background; the store indexer takes over once generation 1 is switched on
        memory_migration.start(on_done=_start_store_indexer)
    else:
        threading.Thread(target=_reindex_loop, daemon=True, name="sokkan-reindex").start()
    fleet.start_sync()  # managé : maintient `<name>.fleet` dans /etc/hosts (no-op sinon)
    updatecheck.start()  # 1 GET/jour sur dist/VERSION — opt-out SOKKAN_UPDATE_CHECK=0
    threading.Thread(target=_usage_refresher, daemon=True, name="sokkan-usage").start()
    corthexis.start()  # revue de la mémoire (onglet CortHeXis) — CORTHEXIS_REVIEW_EVERY_S=0 coupe
    memeval.start_nightly(_transcripts)  # banc de recall nocturne (store 3.0 seulement)
    # 3.1 « Crew up » : ordonnanceur des agents (SOKKAN_FEATURE_AGENTS=0 le coupe)
    features.startup_report()  # a switch asked for but not honoured: logged, feature OFF
    sandbox.detect()  # 3.2 lot 8: off | hooks-only | bwrap, probed once
    revocation.bind_loop(asyncio.get_running_loop())  # 3.2 lot 6: closes from sync routes
    global _main_loop
    _main_loop = asyncio.get_running_loop()  # 3.4.4: _bg() from sync routes
    rt = agents_runtime.start(recall=lambda q, sid: _memory_preseed(q, session_id=sid))
    # 3.3 Helm : avancement + suggestions de recadrage (job périodique, SOKKAN_HELM_TICK_S)
    helm_stop = asyncio.Event()
    helm_task = asyncio.create_task(helm.loop(helm_stop)) if features.enabled("helm") else None
    # 3.4 Teams : approbations en attente poussées dans le canal du projet (filet périodique)
    from teams import proactive as teams_proactive
    teams_task = (asyncio.create_task(teams_proactive.loop(helm_stop))
                  if features.enabled("teams") else None)
    yield
    helm_stop.set()
    for t in (helm_task, teams_task):
        if t:
            t.cancel()
    if rt:
        await rt.stop()


app = FastAPI(title="SOKKAN P1 backend", lifespan=_lifespan)
# Pas de CORSMiddleware : le navigateur ne parle qu'à l'origine Next (proxy /api),
# le CORS est donc inutile — et un wildcard avec cookie d'auth serait un footgun.


def _origin_ok(ws: WebSocket) -> bool:
    """WS anti cross-site : si le navigateur envoie un Origin, il doit correspondre
    à SOKKAN_PUBLIC_URL, ou au Host vu par la requête (accès LAN/IP sans
    SOKKAN_PUBLIC_URL configuré). Les clients non-navigateur (pas d'Origin) passent."""
    origin = ws.headers.get("origin")
    if not origin:
        return True

    def norm(scheme: str, hostname: str | None, port: int | None) -> tuple[str, int]:
        return (hostname or "", port or (443 if scheme == "https" else 80))

    try:
        o = urlparse(origin)
    except ValueError:
        return False
    if not o.hostname:
        return False
    opair = norm(o.scheme, o.hostname, o.port)
    pu = urlparse(PUBLIC_URL)
    if opair == norm(pu.scheme, pu.hostname, pu.port):
        return True
    host_hdr = ws.headers.get("x-forwarded-host") or ws.headers.get("host") or ""
    try:
        h = urlparse(f"//{host_hdr}")
        return bool(h.hostname) and opair == norm(o.scheme, h.hostname, h.port)
    except ValueError:
        return False


# --- IAM : identité résolue par le provider d'auth actif (cf. auth.py) + gating ---
current_user = auth.current_user


def require(min_role: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if iam.rank(user["role"]) < iam.rank(min_role):
            raise HTTPException(403, f"role {min_role!r} required (you are {user['role']!r})")
        return user
    return dep


def _feature(fid: str):
    """Server-side feature gate (registry: backend/features.py): the route 404s when the
    feature is off. /api/features is only a UI hint — enforcement happens here."""
    features.BY_ID[fid]  # unknown id = error at import, not at the first request

    def dep() -> None:
        if not features.enabled(fid):
            raise HTTPException(404, "feature disabled on this instance")
    return dep


feature_preview = _feature("preview")
feature_tmux = _feature("tmux")
# Nina : OFF par défaut (cloud-only v1 — le provisioner pose le flag + les creds LLM)
feature_assistant = _feature("assistant")
feature_magnitude = _feature("magnitude")

# référence forte sur les tâches fire-and-forget (asyncio ne garde qu'une weakref :
# sans ça, un tour d'agent peut être garbage-collecté en plein vol)
_bg_tasks: set[asyncio.Task] = set()


_main_loop: asyncio.AbstractEventLoop | None = None  # posée par le lifespan


def _bg(coro):
    """Lance `coro` en tâche de fond. Depuis une route SYNC (`def`, exécutée dans le
    pool de threads), il n'y a pas de boucle courante : on la poste sur la boucle de
    l'API (3.4.4 — Memory digest et Runbook run répondaient 500 « no running event
    loop » et laissaient une session orpheline)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _main_loop is None or _main_loop.is_closed():
            coro.close()
            raise RuntimeError("no event loop to run the background task on") from None
        return asyncio.run_coroutine_threadsafe(coro, _main_loop)
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def _shown_name(request: Request, u: dict) -> str:
    """3.4.4 : le nom affiché — l'IAM s'il en a un, sinon celui du login SSO (cookie),
    sinon celui de Teams ; l'adresse en dernier recours. Une personne sans rôle d'instance
    (accès par grants) n'a pas de ligne IAM : son nom était son e-mail."""
    email = u.get("email") or ""
    n = (u.get("name") or "").strip()
    if n and "@" not in n:
        return n
    try:
        n = iam.display_name(email) or sess.name_from_request(request)
        if not n:
            import teams
            if teams.enabled():
                from teams import store as teams_store
                n = teams_store.display_name_of(email, teams.tenant_id())
    except Exception:  # noqa: BLE001 — a name is cosmetic, /api/me always answers
        n = ""
    return n or email


@app.get("/api/me")
def me(request: Request, user: dict = Depends(current_user)) -> dict:
    """Who am I — IN THE SELECTED PROJECT (3.2): `role` is the role there (mapped onto the
    instance scale the UI checks), `project_role` the project's own name for it,
    `instance_role` the instance one. No role in that project = role "none" (the cockpit
    then switches to a project the person can read)."""
    slug = projectgate.requested_project(request)
    pu = projectgate.project_user(user, slug)
    shown = pu or {**user, "role": "none", "project": slug, "project_role": None,
                   "instance_role": user.get("role")}
    shown = {**shown, "name": _shown_name(request, shown),
             # 3.4.4 : `role` reste l'échelle d'instance que l'UI vérifie (maintainer → admin) ;
             # le bandeau affiche `role_label` — Léa, maintainer, se voyait « admin »
             "role_label": shown.get("project_role") or shown.get("role")}
    return {**shown, "source": auth.MODE, "ops": projects.is_ops(user),
            # 3.2 (B1) : bandeau du cockpit quand les sessions reçoivent tout le coffre
            "secrets_warning": vault.mode_warning() if iam.rank(user["role"]) >= iam.rank("dev")
            else None}


class NavIn(BaseModel):
    last_plane: str


@app.get("/api/me/nav")
def me_nav(user: dict = Depends(current_user)) -> dict:
    """3.2.2 — the plane this person was on last (the cockpit lands an admin there)."""
    import navprefs
    return navprefs.get(user["email"])


@app.put("/api/me/nav")
def me_nav_set(body: NavIn, user: dict = Depends(current_user)) -> dict:
    import navprefs
    try:
        return navprefs.set_last(user["email"], body.last_plane)
    except ValueError as e:
        raise HTTPException(400, str(e))


# --- 3.2 lot 3 : projets, équipes, attributions -----------------------------------------
@app.get("/api/projects")
def my_projects(user: dict = Depends(current_user)) -> dict:
    """Projects the person can read, with their role — the cockpit's project selector.
    `shared` is listed apart (read by everyone, never selected as a work project)."""
    mine = []
    for p in projects.list_projects():
        role = projects.effective_role(user, p["slug"])
        if role is not None:
            mine.append({"slug": p["slug"], "name": p["name"], "role": role,
                         "access_source": p["access_source"],
                         "shared": p["slug"] == projects.SHARED_PROJECT})
    return {"projects": mine, "multi": projects.multi_project(),
            "default": projects.DEFAULT_PROJECT, "ops": projects.is_ops(user),
            "instance_admin": iam.rank(user["role"]) >= iam.rank("admin")}


class ProjectIn(BaseModel):
    slug: str
    name: str = ""
    description: str = ""
    access_source: str = "sso_group"


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None


class GrantIn(BaseModel):
    principal_kind: str      # user | team
    principal: str           # email | team id (sso:<group>)
    role: str                # viewer | dev | maintainer | admin


class OpsIn(BaseModel):
    group: str = ""


@app.get("/api/admin/projects")
def admin_projects(_u: dict = Depends(require("admin"))) -> dict:
    """Instance admin: every project, its grants and the teams seen at login. No project
    CONTENT here (sessions, cards, notes): an admin adds themself to see it (audited)."""
    out = []
    for p in projects.list_projects(include_archived=True):
        out.append({**p, "grants": projects.list_grants(p["slug"])})
    return {"projects": out, "teams": projects.list_teams(), "ops_group": projects.ops_group(),
            "roles": projects.PROJECT_ROLES, "sources": list(projects.ACCESS_SOURCES)}


@app.post("/api/admin/projects")
def admin_project_create(body: ProjectIn, u: dict = Depends(require("admin"))) -> dict:
    if not features.enabled("multi_project"):
        raise HTTPException(409, "feature `multi_project` is off on this instance "
                                 "(SOKKAN_FEATURE_MULTI_PROJECT=1, see Setup › Organization › Features)")
    if body.access_source == "forge" and not features.enabled("gitlab"):
        raise HTTPException(400, "forge access arrives with lot 5 (GitLab); use sso_group")
    try:
        p = projects.create(body.slug, body.name or body.slug, access_source=body.access_source,
                            created_by=u["email"], description=body.description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "project.create", p["slug"], p["access_source"])
    _sync_project_indexers()
    return p


@app.patch("/api/admin/projects/{slug}")
def admin_project_update(slug: str, body: ProjectPatch, u: dict = Depends(require("admin"))) -> dict:
    try:
        p = projects.update(slug, name=body.name, description=body.description,
                            archived=body.archived)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "project.archive" if body.archived else "project.update", slug, "")
    return p


@app.post("/api/admin/projects/{slug}/grants")
def admin_grant(slug: str, body: GrantIn, u: dict = Depends(require("admin"))) -> dict:
    if projects.get(slug) is None:
        raise HTTPException(404, "unknown project")
    try:
        projects.grant(slug, body.principal_kind, body.principal, body.role, created_by=u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    me_ = body.principal_kind == "user" and body.principal.lower().strip() == u["email"]
    # décision 07.10 : un admin d'instance n'accède au contenu d'un projet qu'en s'y ajoutant
    # — et ça se voit dans le journal
    audit.log(u["email"], "project.grant.self" if me_ else "project.grant", slug,
              f"{body.principal_kind} {body.principal} → {body.role}")
    return {"grants": projects.list_grants(slug)}


@app.delete("/api/admin/projects/{slug}/grants/{kind}/{principal}")
def admin_revoke(slug: str, kind: str, principal: str, u: dict = Depends(require("admin"))) -> dict:
    projects.revoke(slug, kind, principal)
    audit.log(u["email"], "project.revoke", slug, f"{kind} {principal}")
    return {"grants": projects.list_grants(slug)}


@app.get("/api/admin/explain")
def admin_explain(email: str, project: str, _u: dict = Depends(require("admin"))) -> dict:
    """Who has access to a project and why (source by source)."""
    target = iam.get_user(email)
    return projects.explain(target, project)


@app.put("/api/admin/ops-group")
def admin_ops_group(body: OpsIn, u: dict = Depends(require("admin"))) -> dict:
    projects.set_ops_group(body.group)
    audit.log(u["email"], "ops.group", body.group or "(none)", "")
    return {"ops_group": projects.ops_group()}


@app.get("/api/auth/info")
def auth_info() -> dict:
    return auth.auth_info()


@app.get("/api/instance")
def instance_info(_u: dict = Depends(current_user)) -> dict:
    return {**instance.info(), "update": updatecheck.state()}


@app.get("/api/missions/stats")
def missions_stats(_u: dict = Depends(current_user)) -> dict:
    """Open-missions counter, fetched by this instance (cached 6 h) so the
    browser never talks to a third party. See backend/missions.py."""
    return missions.stats()


@app.get("/api/fleet")
def fleet_view(u: dict = Depends(current_user)):
    """Flotte du client (managé) : catalogue + ressources + état. null si self-hosted.
    Les connection strings DB (creds) ne sortent que pour admin+."""
    if not fleet.ENABLED:
        return None
    try:
        v = fleet.view()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    if iam.rank(u["role"]) < iam.rank("admin"):
        for r in v.get("resources") or []:
            r.pop("uri", None)
    v["can_term"] = fleetterm.allowed(u, iam.rank, iam.rank("admin"))
    return v


@app.delete("/api/fleet/resource/{rid}")
def fleet_remove(rid: int, u: dict = Depends(require("admin"))):
    """Résiliation self-service d'une ressource de flotte (admin) : crédit du
    prorata restant + destruction — les données de la ressource sont perdues."""
    if not fleet.ENABLED:
        raise HTTPException(404, "fleet management is unavailable on this instance")
    try:
        r = fleet.remove_resource(rid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    audit.log(u["email"], "fleet.remove", str(rid), "résiliation + destroy")
    return r


class CreditPack(BaseModel):
    pack: int  # 25 | 100 | 500 CHF


@app.post("/api/llm/credit")
def llm_credit(body: CreditPack, u: dict = Depends(require("admin"))):
    """Achat d'un pack de crédits d'inférence (admin) → URL Stripe Checkout.
    Managé uniquement (le portail tient le wallet)."""
    if not fleet.ENABLED:
        raise HTTPException(404, "inference credits are not available on this instance")
    try:
        r = fleet.credit_checkout(body.pack)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    audit.log(u["email"], "llm.credit.checkout", f"{body.pack} CHF", "")
    return r


@app.get("/api/fleet/grants")
def fleet_grants(_u: dict = Depends(require("admin"))) -> dict:
    """Accès terminal de maintenance : liste des users autorisés (hors admins,
    qui l'ont d'office)."""
    return {"grants": fleetterm.grants()}


class GrantsBody(BaseModel):
    emails: list[str]


@app.post("/api/fleet/grants")
def fleet_grants_set(body: GrantsBody, u: dict = Depends(require("admin"))) -> dict:
    g = fleetterm.set_grants(body.emails)
    audit.log(u["email"], "fleet.term.grants", ",".join(g), "")
    return {"grants": g}


@app.websocket("/api/fleet/term/{name}")
async def fleet_term(websocket: WebSocket, name: str, cols: int = 120, rows: int = 32):
    """Terminal de MAINTENANCE (root) vers une instance de la flotte.
    Admin/owner, ou user autorisé via les grants — jamais en self-hosted pur."""
    if not _origin_ok(websocket) or not fleet.ENABLED:
        await websocket.close(code=4403)
        return
    try:
        user = auth.current_user(websocket)  # type: ignore[arg-type]
    except HTTPException:
        user = None
    if user is None or not fleetterm.allowed(user, iam.rank, iam.rank("admin")):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    audit.log(user["email"], "fleet.term.open", name, "maintenance root")
    await fleetterm.bridge(websocket, name, cols, rows)


class FleetReq(BaseModel):
    sku: str
    name: str = ""


@app.post("/api/fleet/request")
def fleet_request(body: FleetReq, u: dict = Depends(require("admin"))):
    """Demande une ressource pour la flotte (admin de l'instance). Facturé (proration)
    puis provisionné au paiement, côté NINABOT."""
    if not fleet.ENABLED:
        raise HTTPException(404, "fleet management is unavailable on this instance")
    try:
        r = fleet.request_resource(body.sku, body.name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    audit.log(u["email"], "fleet.request", body.sku, body.name)
    return r


class RouteReq(BaseModel):
    kind: str            # subdomain | custom
    name: str = ""       # label du sous-domaine (kind=subdomain)
    hostname: str = ""   # FQDN du client (kind=custom)
    target: str = "cockpit"
    port: int = 80


@app.post("/api/fleet/routes")
def fleet_route_add(body: RouteReq, u: dict = Depends(require("admin"))):
    """Route d'exposition web (gratuite, admin) : sous-domaine sokkan.ch via le
    tunnel, ou domaine du client via le caddy edge de cette VM."""
    if not fleet.ENABLED:
        raise HTTPException(404, "fleet management is unavailable on this instance")
    try:
        r = fleet.add_route(body.kind, body.name, body.hostname, body.target, body.port)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    fleet.refresh_edge()  # Caddyfile à jour sans attendre le tick de 120 s
    audit.log(u["email"], "fleet.route.add", r.get("hostname", ""),
              f"{body.kind} → {body.target}:{body.port}")
    return r


class FleetDeployReq(BaseModel):
    addon: str
    image: str


@app.post("/api/fleet/deploy")
def fleet_deploy(body: FleetDeployReq, u: dict = Depends(require("admin"))):
    """Deploy a Docker image as the `app` container on a fleet worker add-on
    (admin). The control plane runs the SSH op and records the tag for rollback."""
    if not fleet.ENABLED:
        raise HTTPException(404, "fleet management is unavailable on this instance")
    try:
        r = fleet.deploy(body.addon, body.image)
    except httpx.HTTPStatusError as e:  # surface validation errors (addon/image)
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    audit.log(u["email"], "fleet.deploy", body.addon, body.image)
    return r


class FleetRollbackReq(BaseModel):
    addon: str


@app.post("/api/fleet/rollback")
def fleet_rollback(body: FleetRollbackReq, u: dict = Depends(require("admin"))):
    """Roll a fleet worker add-on back to its previously deployed image (admin)."""
    if not fleet.ENABLED:
        raise HTTPException(404, "fleet management is unavailable on this instance")
    try:
        r = fleet.rollback(body.addon)
    except httpx.HTTPStatusError as e:  # surface "nothing to roll back" / unknown addon
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    audit.log(u["email"], "fleet.rollback", body.addon, r.get("image", ""))
    return r


@app.delete("/api/fleet/routes/{rid}")
def fleet_route_del(rid: int, u: dict = Depends(require("admin"))):
    if not fleet.ENABLED:
        raise HTTPException(404, "fleet management is unavailable on this instance")
    try:
        r = fleet.remove_route(rid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    fleet.refresh_edge()
    audit.log(u["email"], "fleet.route.remove", str(rid), "")
    return r


@app.post("/api/fleet/upgrade")
def fleet_upgrade_self(u: dict = Depends(require("admin"))):
    """Met à jour cette instance managée vers la release courante (admin).
    Courte interruption : les conteneurs sont reconstruits puis redémarrés."""
    if not fleet.ENABLED:
        raise HTTPException(404, "unavailable on this instance (self-hosted: re-run install.sh)")
    try:
        r = fleet.upgrade_cockpit()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"fleet: {e}")
    audit.log(u["email"], "fleet.upgrade", updatecheck.state().get("latest") or "", "")
    return r


@app.get("/api/notify")
def notify_status(_u: dict = Depends(current_user)) -> dict:
    """Canaux de notification configurés (sans secrets) + délai HITL."""
    return {**notify.status(), "hitl_delay_s": notify.HITL_DELAY_S}


class NotifyConfig(BaseModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    hitl_enabled: bool | None = None


@app.post("/api/notify")
def notify_set(body: NotifyConfig, u: dict = Depends(require("admin"))) -> dict:
    """Configure les canaux (admin). Les secrets restent sur CETTE instance."""
    cfg: dict = {}
    if body.telegram_bot_token is not None or body.telegram_chat_id is not None:
        cfg["telegram"] = {"bot_token": (body.telegram_bot_token or "").strip(),
                           "chat_id": (body.telegram_chat_id or "").strip()}
    if body.webhook_url is not None:
        cfg["webhook"] = {"url": body.webhook_url.strip()}
    if body.hitl_enabled is not None:
        cfg["hitl_enabled"] = body.hitl_enabled
    r = notify.save(cfg)
    audit.log(u["email"], "notify.config", ",".join(k for k, v in r.items() if v is True), "")
    return {**r, "hitl_delay_s": notify.HITL_DELAY_S}


@app.post("/api/notify/test")
def notify_test(u: dict = Depends(require("admin"))) -> dict:
    """Envoie une notification de test sur les canaux configurés."""
    if not notify.enabled():
        raise HTTPException(400, "no channel configured")
    r = notify.send("SOKKAN — test", "Notifications are wired up ✅",
                    notify.session_link("test"), "test")
    audit.log(u["email"], "notify.test", ",".join(r.keys()), "")
    return {"sent": r}


# --- observabilité : opérer la prod depuis le cockpit ------------------------
@app.get("/api/observability")
def observability_status(_u: dict = Depends(current_user)) -> dict:
    """État de la stack obs (Prom/Grafana/Loki) + fil d'incidents (+ les runs
    d'agent qu'un incident a déclenchés, pour le lien Ops → Crew)."""
    incs = observability.incidents(30)
    if agents_runtime.enabled():
        try:
            by = agents.runs_by_incident(_u, [i["id"] for i in incs])
        except Exception:  # noqa: BLE001 — the incident feed never breaks on agents
            by = {}
        for i in incs:
            i["agent_runs"] = by.get(i["id"], [])
            # incident opened BY a failed agent run (SOKKAN_AGENTS_INCIDENTS=1): the
            # link to Crew only for whoever may see that agent
            ag = agents.get(i["agent_id"]) if i.get("agent_id") else None
            i["agent_visible"] = bool(ag and agents.can_read(_u, ag))
    return {**observability.status(), "incidents": incs}


@app.get("/api/observability/dashboards")
def observability_dashboards(_u: dict = Depends(current_user)) -> list[dict]:
    try:
        return observability.list_dashboards()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"grafana: {e}")


class IncidentStatus(BaseModel):
    status: str  # open | resolved


@app.post("/api/observability/incident/{rid}")
def observability_incident_set(rid: int, body: IncidentStatus,
                               u: dict = Depends(require("dev"))) -> dict:
    observability.set_incident_status(rid, body.status)
    audit.log(u["email"], "incident.status", str(rid), body.status)
    return {"ok": True}


_OBS_ALERT_TOKEN = os.environ.get("SOKKAN_OBS_ALERT_TOKEN", "")


@app.post("/api/observability/alert")
async def observability_alert(request: Request) -> dict:
    """Récepteur d'alertes prod (webhook Grafana alerting de l'add-on obs). LE
    killer : chaque alerte devient un incident + SPAWN une session de diagnostic
    pré-seedée (métrique + contexte + mémoire), puis te notifie. Authentifié par
    un token dédié (l'add-on Grafana l'envoie) — jamais la session user."""
    if not _OBS_ALERT_TOKEN:
        raise HTTPException(503, "alert receiver disabled (SOKKAN_OBS_ALERT_TOKEN unset)")
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(got, _OBS_ALERT_TOKEN):
        raise HTTPException(401, "invalid alert token")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    # format Grafana alerting : {alerts:[{labels, annotations, valueString, status}]}
    alerts = payload.get("alerts") or [payload]
    spawned = []
    agent_runs: list[int] = []
    for a in alerts:
        labels = a.get("labels", {}) if isinstance(a, dict) else {}
        ann = a.get("annotations", {}) if isinstance(a, dict) else {}
        title = labels.get("alertname") or ann.get("summary") or "Production alert"
        severity = labels.get("severity", "warning")
        summary = ann.get("description") or ann.get("summary") or a.get("valueString", "")
        if a.get("status") == "resolved":
            continue  # on ne spawn que sur firing
        # 3.2.3: the incident names the host, not only its address
        summary = hostnames.annotate_text(summary)
        inst = labels.get("instance") if isinstance(labels, dict) else None
        if inst and inst not in summary:
            summary = f"{summary} — on {hostnames.describe(str(inst))['label']}".lstrip(" —")
        rid = observability.record_incident(title, summary, severity)
        # 3.1.2 : the payload is external input — framed as untrusted data
        prompt = (
            "A production alert just fired. Its content, as sent by the alert source:\n"
            + agents.untrusted_block("alert", {"alertname": title, "severity": severity,
                                               "summary": summary, "labels": labels})
            + "\n\nYou are the on-call engineer. First search the project memory for anything "
            "related, then use mcp__sokkan-observability__query_metrics and query_logs to "
            "investigate, check the most recent deploy, and identify the likely cause. "
            "Propose a concrete fix and wait for my go-ahead before applying anything. "
            "When resolved, write a short post-mortem note to memory so next time is faster.")
        try:
            s = _spawn_sdk("ops", prompt=prompt, title=f"incident: {title}", user="alert@sokkan")
            observability.link_incident_session(rid, s["session_id"])
            spawned.append({"incident": rid, "session": s["session_id"]})
        except Exception as e:  # noqa: BLE001
            print(f"[obs alert] spawn failed: {e}", file=sys.stderr)
        _bg(asyncio.to_thread(
            notify.send, f"SOKKAN — 🚨 {title}", summary,
            notify.session_link(spawned[-1]["session"]) if spawned else notify.PUBLIC_URL, "alert"))
        # 3.1 : les agents déclenchés par une alerte (trigger event « alert[:nom] »)
        rt = agents_runtime.get_runtime()
        if rt:
            try:
                runs = rt.fire_event("alert", title, {"alertname": title, "severity": severity,
                                                      "summary": summary, "labels": labels,
                                                      "incident": rid})
                agent_runs.extend(runs)
            except Exception as e:  # noqa: BLE001 — une alerte ne casse jamais sur un agent
                print(f"[obs alert] agents event failed: {e}", file=sys.stderr)
    return {"ok": True, "spawned": spawned, "agent_runs": agent_runs}


# --- runbooks : procédures d'ops mémorisées, rejouables ---------------------
# Un runbook = une note mémoire nommée `runbook-*` (les agents en écrivent au fil
# de l'eau, comme la mémoire). « Rejouer » spawn une session guidée par le
# runbook, avec la mémoire — l'ops devient reproductible et supervisée.
@app.get("/api/runbooks")
def runbooks_list(_u: dict = Depends(current_user)) -> list[dict]:
    return [{"name": n["name"], "description": n["description"], "mtime": n["mtime"]}
            for n in memorykb.list_notes(_ctx_scope()) if n["name"].startswith("runbook-")]


@app.post("/api/runbooks/{name}/run")
def runbook_run(name: str, u: dict = Depends(require("dev"))) -> dict:
    """Spawn une session qui exécute le runbook pas à pas (HITL sur l'irréversible)."""
    if "/" in name or ".." in name or not name.startswith("runbook-"):
        raise HTTPException(400, "invalid runbook")
    from core import scope as _sc
    rec = (store_backend.memory_get_record(name, projects=_ctx_scope())
           if store_backend.enabled() else None)
    body = (store_backend.render_note(rec) if rec is not None else None) if \
        store_backend.enabled() else (mem.memory_get(name)
                                      if _sc.allows(_ctx_scope(), projects.DEFAULT_PROJECT)
                                      else None)
    if not body or body.startswith("note not found"):
        raise HTTPException(404, "runbook introuvable")
    prompt = (
        f"Run the runbook **{name}** step by step. Here it is:\n\n{body}\n\n"
        "Search the project memory for related context first. Execute the steps in "
        "order, explaining each before you run it, and stop for my approval before "
        "anything irreversible. If a step fails, diagnose before continuing.")
    s = _spawn_sdk("ops", prompt=prompt, title=f"runbook: {name.removeprefix('runbook-')}",
                   user=u["email"], project=_ctx_project())
    if rec is not None:  # 3.4: the session inherits the runbook's level (audited)
        classification.log_access("cockpit", [rec], actor=u["email"],
                                  session_id=s["session_id"])
    audit.log(u["email"], "runbook.run", name, s["session_id"])
    return s


# --- coffre de secrets (injectés en env des sessions) -----------------------
@app.get("/api/vault/session")
def vault_session(_u: dict = Depends(require("dev"))) -> dict:
    """Pour le formulaire d'ouverture de session : le mode et les NOMS du coffre
    (jamais les valeurs) — un dev choisit ce que sa session reçoit."""
    return {"mode": vault.session_mode(), "names": _vault_names()}


@app.get("/api/vault")
def vault_list(_u: dict = Depends(require("admin"))) -> dict:
    """Noms des secrets du projet sélectionné (JAMAIS les valeurs). 3.2 lot 4 : un admin
    (ou maintainer) DU PROJET gère son coffre ; un projet sans coffre → liste vide."""
    p = _ctx_project()
    return {"names": vault.names(p), "project": p, "enabled": vault.namespace(p) is not None}


class SecretIn(BaseModel):
    name: str
    value: str


@app.post("/api/vault")
def vault_set(body: SecretIn, u: dict = Depends(require("admin"))) -> dict:
    p = _ctx_project()
    try:
        vault.set_secret(body.name.strip(), body.value, project=p)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "vault.set", body.name.strip(), "")  # nom only, jamais la valeur
    return {"names": vault.names(p), "project": p, "enabled": True}


@app.delete("/api/vault/{name}")
def vault_delete(name: str, u: dict = Depends(require("admin"))) -> dict:
    p = _ctx_project()
    try:
        vault.delete_secret(name, project=p)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "vault.delete", name, "")
    return {"names": vault.names(p), "project": p, "enabled": True}


# --- agents (3.1 « Crew up ») — spec docs/AGENTS.md --------------------------
feature_agents = _feature("agents")


def crew_reader(user: dict = Depends(current_user)) -> dict:
    """Read routes of Crew: dev+, or viewer+ when SOKKAN_CREW_VIEWER_READONLY=1
    (public demo). Every write route keeps require("dev") AND the owner/admin
    check of agents._need — a viewer gets 403 on all of them."""
    need = "viewer" if agents.viewer_readonly() else "dev"
    if iam.rank(user["role"]) < iam.rank(need):
        raise HTTPException(403, f"role {need!r} required (you are {user['role']!r})")
    return user


def _agent_http(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except agents.NotFound as e:
        raise HTTPException(404, str(e))
    except agents.Forbidden as e:
        raise HTTPException(403, str(e))
    except agents.AgentError as e:
        raise HTTPException(400, str(e))


def _agent_full(u: dict, aid: int) -> dict:
    a = _agent_http(lambda: agents._need(u, agents.get(aid), write=False))
    full = next((x for x in agents.list_agents(u, include_archived=True) if x["id"] == a["id"]), a)
    return agents.public(full) | {k: full.get(k) for k in ("deck", "needs_approval",
                                                           "waiting_for_human", "created_at",
                                                           "updated_at")} | {
        "approval_mode": agents.approval_mode(),
        # 3.1.2 : how its runs are metered (SDK cost, or SOKKAN price table / token cap)
        "metering": agentcost.metering(full.get("model"))}


class AgentBody(BaseModel):
    model_config = {"extra": "allow"}
    activate: bool = False


@app.get("/api/agents/meta")
def agents_meta(_u: dict = Depends(crew_reader), _f: None = Depends(feature_agents)) -> dict:
    """What the form needs: vault NAMES (never values), choices, playbooks."""
    return {"secrets": _vault_names(), "tools": agents.KNOWN_TOOLS,
            "default_tools": agents.DEFAULT_TOOLS, "mcp": list(agents.MCP_CHOICES),
            "outputs": list(agents.OUTPUTS), "notify_on": list(agents.NOTIFY_ON),
            "models": ["", "haiku", "sonnet", "opus"], "triggers": list(agents.TRIGGERS),
            "playbooks": [p for p in playbooks.catalog() if p["id"] != "new-agent"],
            "timezone": "Europe/Zurich", "approval_mode": agents.approval_mode(),
            "self_activation": (iam.rank(_u["role"]) >= iam.rank("dev")
                                and agents.self_activation_allowed(_u)),
            "read_only": iam.rank(_u["role"]) < iam.rank("dev"),
            "is_admin": iam.rank(_u["role"]) >= iam.rank("admin"),
            "metering": agentcost.metering(None)}


@app.get("/api/agents")
def agents_list(archived: bool = False, u: dict = Depends(crew_reader),
                _f: None = Depends(feature_agents)) -> dict:
    items = [agents.public(a) | {k: a.get(k) for k in ("deck", "needs_approval",
                                                       "waiting_for_human")}
             for a in agents.list_agents(u, include_archived=archived)]
    return {"agents": items, "pending": agents.pending_approvals(u),
            "scheduler": _scheduler_state()}


def _scheduler_state() -> dict:
    """3.2: is the scheduler really starting runs? Held = no model credentials explicitly
    configured for this instance (the Crew tab says so instead of a silent idle deck)."""
    rt = agents_runtime.get_runtime()
    if rt is None:
        return {"running": False, "held": False, "reason": "agents scheduler not started"}
    if not hasattr(rt, "state"):          # public demo: the simulator, no model
        return {"running": True, "held": False, "reason": None, "simulated": True}
    st = rt.state()
    st.pop("credentials", None)           # the source of the credentials is not for the UI
    return st


@app.post("/api/agents")
def agents_create(body: AgentBody, u: dict = Depends(require("dev")),
                  _f: None = Depends(feature_agents)) -> dict:
    fields = body.model_dump(exclude={"activate"})
    override = bool(fields.pop("override_alert_writes", False))
    a = _agent_http(agents.create, u, fields, created_by=f"user:{u['email']}",
                    activate=body.activate, known_secrets=_vault_names(),
                    override_alert_writes=override)
    audit.log(u["email"], "agent.create", a["name"], a["status"])
    _audit_alert_override(u, a, override)
    agents_runtime.poke()
    return _agent_full(u, a["id"])


@app.post("/api/agents/proposals")
def agents_propose(body: AgentBody, u: dict = Depends(require("dev")),
                   _f: None = Depends(feature_agents)) -> dict:
    """A proposal built in Nina's chat → a pending card (a human approves it)."""
    fields = body.model_dump(exclude={"activate"})
    a = _agent_http(agents.create, u, fields, created_by=f"nina:{u['email']}", proposal=True,
                    known_secrets=_vault_names())
    audit.log(u["email"], "agent.propose", a["name"], "from Nina")
    return _agent_full(u, a["id"])


@app.get("/api/agents/runs/{run_id}")
def agents_run(run_id: int, u: dict = Depends(crew_reader),
               _f: None = Depends(feature_agents)) -> dict:
    return classification.redact_run(_agent_http(agents.get_run_for, u, run_id),
                                      classification.ctx_clearance())


@app.post("/api/agents/runs/{run_id}/cancel")
def agents_run_cancel(run_id: int, u: dict = Depends(require("dev")),
                      _f: None = Depends(feature_agents)) -> dict:
    r = _agent_http(agents.get_run_for, u, run_id)
    _agent_http(lambda: agents._need(u, agents.get(r["agent_id"])))  # write: owner or admin
    rt = agents_runtime.get_runtime()
    ok = rt.cancel(run_id) if rt else False
    if not ok and r["status"] == "queued":
        agents.update_run(run_id, status="cancelled", error="cancelled before start")
        ok = True
    audit.log(u["email"], "agent.run.cancel", r["agent_name"], f"run #{run_id}")
    return {"ok": ok}


@app.get("/api/agents/{aid}")
def agents_get(aid: int, u: dict = Depends(crew_reader),
               _f: None = Depends(feature_agents)) -> dict:
    return _agent_full(u, aid)


@app.patch("/api/agents/{aid}")
def agents_patch(aid: int, body: AgentBody, u: dict = Depends(require("dev")),
                 _f: None = Depends(feature_agents)) -> dict:
    fields = body.model_dump(exclude={"activate"}, exclude_unset=True)
    override = bool(fields.pop("override_alert_writes", False))
    before = agents.get(aid) or {}
    a = _agent_http(agents.update, u, aid, fields, known_secrets=_vault_names(),
                    override_alert_writes=override)
    audit.log(u["email"], "agent.update", a["name"], ", ".join(sorted(fields)))
    _audit_alert_override(u, a, override, before)
    agents_runtime.poke()
    return _agent_full(u, aid)


def _audit_alert_override(u: dict, a: dict, asked: bool, before: dict | None = None) -> None:
    """Journal an admin override of the alert write rule (3.1.2) when it was used."""
    ov = a.get("alert_write_override") or {}
    old = (before or {}).get("alert_write_override") or {}
    if asked and ov and ov != old:
        audit.log(u["email"], "agent.alert_write_override", a["name"],
                  "auto-approved writes on an alert-triggered agent: "
                  + ", ".join(ov.get("rules") or []))


@app.post("/api/agents/{aid}/{action}")
def agents_action(aid: int, action: str, override_alert_writes: bool = False,
                  u: dict = Depends(require("dev")),
                  _f: None = Depends(feature_agents)) -> dict:
    before = agents.get(aid) or {}
    ops = {
        "approve": lambda: agents.approve(u, aid, override_alert_writes=override_alert_writes),
        "reject": lambda: agents.reject(u, aid),
        "pause": lambda: agents.set_status(u, aid, "paused"),
        "resume": lambda: agents.set_status(u, aid, "active"),
        "archive": lambda: agents.set_status(u, aid, "archived"),
        "run": lambda: agents.request_run(u, aid, "manual", u["email"]),
    }
    if action not in ops:
        raise HTTPException(404, f"unknown action: {action}")
    out = _agent_http(ops[action])
    name = (agents.get(aid) or {}).get("name", str(aid))
    audit.log(u["email"], f"agent.{action}", name, f"run #{out['id']}" if action == "run" else "")
    if action == "approve":
        _audit_alert_override(u, agents.get(aid) or {}, override_alert_writes, before)
    agents_runtime.poke()
    return {"agent": _agent_full(u, aid), **({"run": out} if action == "run" else {})}


@app.get("/api/agents/{aid}/runs")
def agents_runs(aid: int, limit: int = 50, u: dict = Depends(crew_reader),
                _f: None = Depends(feature_agents)) -> list[dict]:
    cap = classification.ctx_clearance()
    return [classification.redact_run(r, cap) for r in _agent_http(agents.list_runs, u, aid, limit)]


# --- quarantaine mémoire (3.1) : notes écrites par des runs d'agent ------------
import quarantine  # noqa: E402 — memory/ est sur le path (cf. imports du haut)


def _require_store_or_409(project: str, level: int | None = None) -> None:
    """3.4.2 — a memory write for a project other than `default`, or above the default
    level, needs the 3.0 store: in sqlite mode it is refused (409 `memory_store_required`)
    rather than written where nothing indexes it."""
    try:
        store_backend.require_store(project, level)
    except store_backend.StoreRequired as e:
        raise HTTPException(409, {"code": e.code, "message": str(e),
                                  "store": store_backend.store_info()})


@app.get("/api/memory/quarantine")
def memory_quarantine(_u: dict = Depends(require("dev"))) -> list[dict]:
    """Notes écrites par des runs d'agent, en attente de relecture humaine. Elles ne
    sont PAS dans le dossier mémoire : aucun rappel (spawn, recherche, hooks)."""
    return quarantine.list_notes(_ctx_project(), classification.ctx_clearance())


@app.get("/api/memory/quarantine/{name}")
def memory_quarantine_get(name: str, _u: dict = Depends(require("dev"))) -> dict:
    q = quarantine.get(name, _ctx_project(), classification.ctx_clearance())
    if q is None:
        raise HTTPException(404, "not in quarantine")
    return q


class QuarantineDecision(BaseModel):
    delete: bool = False


@app.post("/api/memory/quarantine/{name}/approve")
def memory_quarantine_approve(name: str, u: dict = Depends(require("dev"))) -> dict:
    q = quarantine.get(name, _ctx_project(), classification.ctx_clearance())
    if q is None:
        raise HTTPException(404, "not in quarantine")
    _require_store_or_409(_ctx_project(), q.get("level"))   # 3.4.2: never an unindexed note
    try:
        out = quarantine.approve(name, u["email"], _ctx_project(),
                                 classification.ctx_clearance())
    except KeyError:
        raise HTTPException(404, "not in quarantine")
    if out.get("level", 2) > 2 and store_backend.enabled():
        try:  # 3.4: the deliverable keeps its inherited level whatever its file says later
            store_backend.get_store().set_level(name, out["level"], project=_ctx_project(),
                                                by=u["email"], reason="agent deliverable")
        except Exception as e:  # noqa: BLE001
            print(f"[sokkan] level floor of {name} not recorded: {e!r}", file=sys.stderr)
    audit.log(u["email"], "memory.quarantine.approve", name, "")
    return out


@app.post("/api/memory/quarantine/{name}/reject")
def memory_quarantine_reject(name: str, body: QuarantineDecision | None = None,
                             u: dict = Depends(require("dev"))) -> dict:
    if quarantine.get(name, _ctx_project(), classification.ctx_clearance()) is None:
        raise HTTPException(404, "not in quarantine")
    try:
        out = quarantine.reject(name, u["email"], delete=bool(body and body.delete),
                                project=_ctx_project())
    except KeyError:
        raise HTTPException(404, "not in quarantine")
    audit.log(u["email"], "memory.quarantine.reject", name,
              "deleted" if body and body.delete else "archived")
    return out


@app.get("/api/edge/ask")
def edge_ask(domain: str = ""):
    """Gate d'émission de certificat du caddy edge (on_demand_tls `ask`) :
    200 si le hostname est une route custom enregistrée, 404 sinon. Sans auth
    (appelé par caddy) — ne divulgue rien : booléen sur un hostname public."""
    if not edge.allowed(domain):
        raise HTTPException(404, "unknown host")
    return {"ok": True}


class InstanceSettings(BaseModel):
    org_name: str = ""
    budget_session_usd: float | None = None  # 0 = désactivé
    budget_day_usd: float | None = None


@app.post("/api/instance")
def instance_set(body: InstanceSettings, u: dict = Depends(require("admin"))) -> dict:
    if body.org_name.strip():
        instance.set_org_name(body.org_name)
        audit.log(u["email"], "instance.rename", body.org_name)
    if body.budget_session_usd is not None or body.budget_day_usd is not None:
        instance.set_budgets(body.budget_session_usd, body.budget_day_usd)
        audit.log(u["email"], "instance.budgets",
                  f"session={body.budget_session_usd} day={body.budget_day_usd}")
    return instance.info()


@app.get("/api/llm")
def llm_status(_u: dict = Depends(current_user)) -> dict:
    """Config LLM de l'instance (mode + modèle) — jamais la clé."""
    return llm.status()


class LlmConfig(BaseModel):
    mode: str  # 'byok' | 'custom'
    anthropic_api_key: str = ""
    claude_oauth_token: str = ""  # abonnement Claude Pro/Max (`claude setup-token`)
    # mode 'custom' : endpoint compatible API Anthropic (Kimi, GLM, DeepSeek, LiteLLM…)
    base_url: str = ""
    auth_token: str = ""
    model: str = ""
    small_model: str = ""


@app.get("/api/llm/usage")
def llm_usage(_u: dict = Depends(current_user)):
    """Usage/quota du jour (mode inférence incluse) ; null en BYOK."""
    return llm.usage()


@app.get("/api/llm/tiers")
def llm_tiers(_u: dict = Depends(current_user)) -> dict:
    """Grille des tiers d'inférence incluse (Ship/Fast/Deep) pour le sélecteur."""
    return {"tiers": llm.tier_catalog(), "current": llm.status().get("model")}


class TierSel(BaseModel):
    tier: str


@app.post("/api/llm/tier")
def llm_set_tier(body: TierSel, u: dict = Depends(require("admin"))) -> dict:
    """Choisit le tier white-label des sessions (instance en inférence incluse)."""
    try:
        llm.set_tier(body.tier)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "llm.tier", body.tier)
    return {"ok": True, "tier": body.tier}


@app.post("/api/llm")
def llm_set(body: LlmConfig, u: dict = Depends(require("admin"))) -> dict:
    """Règle la clé LLM de l'instance (admin). La clé/le token reste sur CETTE VM,
    jamais transmis à NINABOT. Une instance en « inférence incluse » est opérée
    par NINABOT → on n'autorise pas de la basculer en BYOK depuis le cockpit."""
    if llm.status().get("operator_managed"):
        raise HTTPException(403, "this instance uses managed inference (operated by NINABOT)")
    if body.mode == "byok":
        if body.anthropic_api_key.strip():
            llm.save({"mode": "byok", "anthropic_api_key": body.anthropic_api_key.strip()})
        elif body.claude_oauth_token.strip():
            llm.save({"mode": "byok", "claude_oauth_token": body.claude_oauth_token.strip()})
        else:
            raise HTTPException(400, "anthropic_api_key or claude_oauth_token required")
        audit.log(u["email"], "llm.config", f"byok:{llm.status().get('byok_kind')}")
    elif body.mode == "custom":
        base = body.base_url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise HTTPException(400, "base_url must start with http(s)://")
        if not (body.auth_token.strip() and body.model.strip()):
            raise HTTPException(400, "auth_token and model required")
        llm.save({"mode": "custom", "base_url": base,
                  "auth_token": body.auth_token.strip(), "model": body.model.strip(),
                  "small_model": body.small_model.strip()})
        audit.log(u["email"], "llm.config", f"custom:{base} model={body.model.strip()}")
    else:
        raise HTTPException(400, "mode must be 'byok' or 'custom'")
    return llm.status()


# --- Magnitude : LLM local (registry de nodes, bench + serve llama.cpp) ------
@app.get("/api/magnitude")
def magnitude_state(_u: dict = Depends(require("viewer")),
                    _f: None = Depends(feature_magnitude)) -> dict:
    """Registry Magnitude (nodes : profil, bench, serving, catalogue annoté du
    fit par machine). Les serve_tokens n'en sortent jamais."""
    return magnitude.view()


@app.get("/api/magnitude/memory")
def magnitude_memory(_u: dict = Depends(require("viewer")),
                     _f: None = Depends(feature_magnitude)) -> dict:
    """Profil mémoire (CortHeXis) : courant, recommandé (cockpit + nodes), coûts
    par profil, modèle actif et décision sur la licence Gemma."""
    return magnitude.memory_view()


@app.post("/api/magnitude/pair")
def magnitude_pair(u: dict = Depends(require("admin")),
                   _f: None = Depends(feature_magnitude)) -> dict:
    """Appaire une NOUVELLE machine (les nodes existants ne bougent pas).
    Token montré UNE fois, avec la commande à lancer sur la machine."""
    nid, token = magnitude.pair()
    audit.log(u["email"], "magnitude.pair", nid, "new node token issued")
    # one-liner universel : le bootstrap pose un Python si besoin (macOS sans
    # Command Line Tools), récupère l'agent et paire. Forme --opt=val en repli
    # manuel : un token token_urlsafe peut commencer par '-' (sinon pris pour un
    # flag par argparse).
    return {"node": nid, "token": token,
            "command": f'curl -fsSL "{PUBLIC_URL}/api/magnitude/install.sh?token={token}" | sh',
            "manual_command": f"python3 -m magnitude --cockpit={PUBLIC_URL} --token={token}"}


# Python standalone (astral python-build-standalone) posé par le bootstrap quand
# la machine n'a pas de python utilisable — version épinglée (releases gardées
# indéfiniment), surchargeable par l'agent via env si besoin.
_MAGNITUDE_PY_VER = "3.12.13"
_MAGNITUDE_PY_DATE = "20260807"


@app.get("/api/magnitude/install.sh")
def magnitude_install_sh(token: str = "", ref: str = "refs/heads/main",
                         _f: None = Depends(feature_magnitude)):
    """Bootstrap host servi avec cockpit+token pré-remplis (pas d'auth cookie :
    c'est un script public, le token est le secret de pairing montré une fois)."""
    tpl = (Path(__file__).resolve().parent.parent / "magnitude" / "install.sh").read_text()
    script = (tpl.replace("@COCKPIT@", PUBLIC_URL)
                 .replace("@TOKEN@", token)
                 .replace("@REF@", ref)
                 .replace("@PYVER@", _MAGNITUDE_PY_VER)
                 .replace("@PYDATE@", _MAGNITUDE_PY_DATE))
    return PlainTextResponse(script, media_type="text/x-shellscript")


@app.delete("/api/magnitude/node/{nid}")
def magnitude_unpair(nid: str, u: dict = Depends(require("admin")),
                     _f: None = Depends(feature_magnitude)) -> dict:
    """Désappaire UN node : token révoqué, profil/bench/serving oubliés."""
    if not magnitude.unpair(nid):
        raise HTTPException(404, f"unknown node: {nid!r}")
    audit.log(u["email"], "magnitude.unpair", nid, "")
    return magnitude.view()


class MagnitudeNodeBody(BaseModel):
    """Config explicite d'un node ('' = revenir au défaut/auto)."""
    name: str | None = None
    shim_url: str | None = None  # URL du shim vue des sessions (node distant)


@app.post("/api/magnitude/node/{nid}")
def magnitude_node_config(nid: str, body: MagnitudeNodeBody,
                          u: dict = Depends(require("admin")),
                          _f: None = Depends(feature_magnitude)) -> dict:
    if body.shim_url and body.shim_url.strip() \
            and not body.shim_url.strip().startswith(("http://", "https://")):
        raise HTTPException(400, "shim_url must start with http(s)://")
    if not magnitude.node_config(nid, name=body.name, shim_url=body.shim_url):
        raise HTTPException(404, f"unknown node: {nid!r}")
    audit.log(u["email"], "magnitude.node", nid,
              f"name={body.name!r} shim_url={body.shim_url!r}")
    return magnitude.view()


class MagnitudeCmdBody(BaseModel):
    node: str
    action: str  # 'bench' | 'run' | 'stop' | 'attach' (engine already running, 3.2.3)
    model: str = ""
    port: int | None = None   # attach: the port of the engine on the node


@app.post("/api/magnitude/cmd")
def magnitude_cmd(body: MagnitudeCmdBody, u: dict = Depends(require("admin")),
                  _f: None = Depends(feature_magnitude)) -> dict:
    """Pose une commande pour un node (livrée à son prochain sync, ≤ 2 s)."""
    action, model = body.action.strip(), body.model.strip()
    if action not in ("bench", "run", "stop", "attach"):
        raise HTTPException(400, "action must be 'bench', 'run', 'stop' or 'attach'")
    if action in ("bench", "run"):
        if not model:
            raise HTTPException(400, f"model required for {action!r}")
        if not magnitude.catalog_get(model):
            raise HTTPException(400, f"unknown model: {model!r}")
    node = magnitude.get_node(body.node)
    if node is None:
        raise HTTPException(404, f"unknown node: {body.node!r}")
    if action == "attach" and not magnitude.find_engine(node, model, body.port):
        raise HTTPException(400, f"no engine {model!r} on port {body.port} reported by this node")
    if not magnitude.online(node):
        raise HTTPException(409, "agent is offline on this node")
    if action in ("bench", "run"):
        # 3.2.3: the fit is computed where the run would happen (the cards allowed by
        # MAGNITUDE_GPU_DEVICES, their FREE memory) — never load a model that cannot fit
        tgt = magnitude.run_target_of(body.node)
        fit = next((m["fit"] for m in magnitude.catalog_view(node.get("profile"),
                                                             magnitude.metrics_of(body.node))
                    if m["id"] == model), "unknown")
        if fit in ("no", "unknown"):
            where = ("the CPU" if (tgt or {}).get("where") == "cpu" else
                     "card(s) " + ", ".join(f"#{c}" for c in (tgt or {}).get("cards") or []))
            raise HTTPException(409, f"{model} does not fit on {where} "
                                     f"({(tgt or {}).get('usable_gb')} GB available)")
    magnitude.set_pending(body.node, action, model, port=body.port if action == "attach" else None)
    audit.log(u["email"], "magnitude.cmd", f"{body.node}:{action}",
              f"{model}:{body.port}" if action == "attach" else model)
    return magnitude.view()


class MagnitudeConnectBody(BaseModel):
    node: str


@app.post("/api/magnitude/connect")
def magnitude_connect(body: MagnitudeConnectBody, u: dict = Depends(require("admin")),
                      _f: None = Depends(feature_magnitude)) -> dict:
    """Branche le router LLM de l'instance sur le shim d'UN node (mode custom) :
    toute nouvelle session tourne sur le modèle servi par cette machine."""
    if llm.status().get("operator_managed"):
        raise HTTPException(403, "this instance uses managed inference (operated by NINABOT)")
    node = magnitude.get_node(body.node)
    if node is None:
        raise HTTPException(404, f"unknown node: {body.node!r}")
    serving = node.get("serving") or {}
    if not serving.get("serve_token") or not magnitude.online(node):
        raise HTTPException(409, "no model served (or agent offline) — run one first")
    model = serving.get("model") or ""
    shim = magnitude.shim_url_of(node)
    llm.save({"mode": "custom", "base_url": shim,
              "auth_token": serving["serve_token"], "model": model, "small_model": model})
    audit.log(u["email"], "magnitude.connect", f"{body.node}:{model}", shim)
    return magnitude.view()


class MagnitudeSyncBody(BaseModel):
    """Sync agent — tous les champs optionnels ; `serving: null` (stop effectif)
    se distingue du champ absent via model_fields_set."""
    profile: dict | None = None
    status: dict | None = None
    bench_result: dict | None = None
    serving: dict | None = None
    error: str | None = None
    engines: list | None = None   # agent ≥ 0.2: engines already running on the node
    metrics: dict | None = None   # agent ≥ 0.3: live load (cards, CPU, RAM)


@app.post("/api/magnitude/agent/sync")
def magnitude_agent_sync(body: MagnitudeSyncBody, request: Request,
                         _f: None = Depends(feature_magnitude)) -> dict:
    """Poll d'un agent host (HTTP sortant, jamais de connexion entrante chez le
    client). PAS d'auth cookie : header `x-magnitude-token`, résolu vers son
    node en constant-time. Livre la commande pending du node et l'efface."""
    nid = magnitude.resolve_token(request.headers.get("x-magnitude-token") or "")
    if nid is None:
        raise HTTPException(401, "invalid magnitude token")
    cmd = magnitude.sync(nid, body.model_dump(), "serving" in body.model_fields_set)
    resend = magnitude.resend_needed(nid)
    return {"command": cmd, **({"resend": resend} if resend else {})}


def _metrics_allowed(request: Request) -> bool:
    """Prometheus scrape: `Authorization: Bearer $SOKKAN_METRICS_TOKEN` when the token is
    set; without one, only a direct loopback client (no proxy header: the web front
    forwards /api from 127.0.0.1 too, with x-forwarded-for)."""
    tok = os.environ.get("SOKKAN_METRICS_TOKEN", "")
    if tok:
        got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        return secrets.compare_digest(got.encode(), tok.encode())
    host = request.client.host if request.client else ""
    proxied = any(request.headers.get(h) for h in ("x-forwarded-for", "x-real-ip",
                                                   "cf-connecting-ip", "forwarded"))
    return host in ("127.0.0.1", "::1") and not proxied


@app.get("/metrics", include_in_schema=False)
@app.get("/api/magnitude/metrics", include_in_schema=False)
def magnitude_prometheus(request: Request, _f: None = Depends(feature_magnitude)):
    """`sokkan_magnitude_*` gauges (cards, CPU/RAM, engines) for Prometheus / Operate."""
    if not _metrics_allowed(request):
        raise HTTPException(401, "metrics: bearer SOKKAN_METRICS_TOKEN required")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(magnitude.prometheus(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/features")
def features_flags() -> dict:
    """Onglets/capacités actifs sur cette instance — le front masque le reste. The flat
    keys are kept for the UI (and older front-ends); `registry` = every feature of
    backend/features.py with its effective state and WHY (Setup › Organization › Features)."""
    on = features.enabled
    return {
        # l'onglet Infra existe dès qu'il a quelque chose à montrer : topologie
        # (Prometheus) et/ou flotte managée (SOKKAN_FLEET_*, VMs clients).
        "infra": infra.ENABLED or fleet.ENABLED,
        "infra_topo": infra.ENABLED,
        "fleet": fleet.ENABLED,
        # onglet Operate : dès qu'une stack d'observabilité est branchée
        "observe": observability.ENABLED,
        "preview": on("preview"),
        "tmux": on("tmux"),
        # Nina (agente d'assistance) : flag serveur + un LLM joignable
        "assistant": on("assistant") and assistant.configured(),
        # SOKKAN Missions link in the header. The counter is fetched by this
        # instance, not by the browser (backend/missions.py), and cached 6 h.
        # Opt out of link and fetch alike: SOKKAN_FEATURE_MISSIONS_LINK=0
        "missions_link": on("missions_link"),
        # Magnitude : LLM local (profil hardware + bench + serve llama.cpp)
        "magnitude": on("magnitude"),
        # bannière de visite guidée (instance de démo publique read-only)
        "demo": on("demo_banner"),
        # onglet Crew (agents, 3.1)
        "agents": agents_runtime.enabled(),
        # 3.1.1 : Crew visible en lecture seule pour un viewer (démo publique)
        "agents_viewer_readonly": agents.viewer_readonly(),
        # 3.1.1 : runs simulés de la démo publique (aucune inférence)
        "demo_crew": agents_runtime.demo_mode(),
        "multi_project": on("multi_project"),
        # 3.2 lot 8: how sessions of a project other than `default` are confined
        "sandbox": sandbox.mode(),
        # 3.2 lot 6: SCIM endpoint + « Revoke now »
        "revocation": on("revocation"),
        # 3.3 Helm (onglet réservé aux managers : /api/helm/access le dit par personne)
        "helm": on("helm"),
        # 3.2.2 : démo publique « Captains » (Helm, projets, Setup en lecture seule)
        "demo_captains": demo_captains.enabled(),
        "registry": features.as_api(),
    }


class AssistantMsg(BaseModel):
    message: str


@app.get("/api/assistant/history")
def assistant_history(user: dict = Depends(require("viewer")),
                      _f: None = Depends(feature_assistant)) -> list[dict]:
    return assistant.history(user["email"])


@app.post("/api/assistant/chat/stream")
def assistant_chat_stream(body: AssistantMsg, user: dict = Depends(require("viewer")),
                          _f: None = Depends(feature_assistant)):
    """Un tour de chat avec Nina, en flux SSE.

    Le débit du modèle ne change pas — ce qui change est qu'on lise pendant que
    ça s'écrit : sur silicium maison le 1er token arrive en ~2 s là où la
    réponse complète met 20-40 s. Événements : `delta` (fragment de texte) et
    `done` (réponse complète), plus `error` si le tour échoue avant le 1er octet.
    """
    def events():
        try:
            for kind, payload in assistant.chat_stream(user["email"], body.message):
                yield f"event: {kind}\ndata: {json.dumps({'text': payload})}\n\n"
        except ValueError as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"
        except Exception as e:  # noqa: BLE001
            audit.log(user["email"], "assistant.error", detail=str(e)[:200])
            yield ("event: error\ndata: "
                   + json.dumps({"detail": "Nina est momentanément indisponible — "
                                           "réessayez, ou écrivez à hello@sokkan.ch"})
                   + "\n\n")
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/assistant/chat")
def assistant_chat(body: AssistantMsg, user: dict = Depends(require("viewer")),
                   _f: None = Depends(feature_assistant)) -> dict:
    """Un tour de chat avec Nina, réponse complète (repli si le flux échoue)."""
    try:
        return assistant.chat(user["email"], body.message)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 — LLM upstream KO : message actionnable
        audit.log(user["email"], "assistant.error", detail=str(e)[:200])
        raise HTTPException(502, "Nina est momentanément indisponible — "
                                 "réessayez, ou écrivez à hello@sokkan.ch")


class LocalLogin(BaseModel):
    token: str


# rate-limit du login local : fenêtre glissante en mémoire, par IP client.
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_S = 60.0
_login_fails: dict[str, list[float]] = {}


def _login_throttled(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _login_fails[ip] = fails
    return len(fails) >= _LOGIN_MAX_FAILS


@app.post("/api/auth/local")
def auth_local(body: LocalLogin, request: Request):
    """Login single-user (mode local avec SOKKAN_LOCAL_TOKEN) → cookie de session."""
    if auth.MODE != "local" or not auth.LOCAL_TOKEN:
        raise HTTPException(400, "local login not applicable on this instance")
    # vraie IP client derrière le proxy (cloudflared/caddy/Next) : sans ça tous
    # les clients partagent le bucket de l'IP du proxy → 5 échecs verrouillent
    # le login pour tout le monde. CF-Connecting-IP est réécrit par l'edge de
    # confiance ; XFF en repli ; l'IP socket en dernier ressort.
    ip = (request.headers.get("cf-connecting-ip")
          or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    if _login_throttled(ip):
        raise HTTPException(429, "too many failed attempts — retry in a minute")
    if not secrets.compare_digest(body.token.strip(), auth.LOCAL_TOKEN):
        _login_fails.setdefault(ip, []).append(time.time())
        raise HTTPException(401, "invalid token")
    _login_fails.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(sess.COOKIE, sess.make(auth.OWNER_EMAIL, auth.OWNER_NAME),
                    max_age=sess.TTL, httponly=True,
                    secure=PUBLIC_URL.startswith("https"), samesite="lax")
    return resp


# --- terminal ttyd, proxifié + authentifié par SOKKAN (remplace l'ingress CF Access) ---
# Gate feature = tmux (le shell brut est la même famille) : 404 si désactivé.
@app.websocket("/term/ws")
async def term_ws(websocket: WebSocket):
    if not features.enabled("tmux") or not _origin_ok(websocket):
        await websocket.close(code=4403)
        return
    try:  # 3.2 lot 6 : « Revoke now » ferme aussi les terminaux ouverts
        who = auth.resolve_email(websocket)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — termproxy refuses it itself
        who = ""
    if who:
        revocation.track(who, websocket)
    try:
        await termproxy.ws(websocket, "ws")
    finally:
        if who:
            revocation.untrack(who, websocket)


@app.api_route("/term", methods=["GET"])
@app.api_route("/term/{path:path}", methods=["GET", "POST"])
async def term_http(request: Request, path: str = "", _f: None = Depends(feature_tmux)):
    return await termproxy.http(request, path)


# --- Chantier B : chat interactif piloté par le Claude Agent SDK -------------
def _ws_user(websocket: WebSocket) -> dict | None:
    """Identité d'une connexion WS (auth.current_user est duck-typé : WS a
    .headers et .cookies comme Request). None si non authentifié / rôle insuffisant.
    Un viewer est accepté (rôle = « lecture seule (chat/…) ») : il reçoit le flux
    d'events ; toutes les mutations sont gatées ≥ dev dans la boucle WS."""
    try:
        user = auth.instance_user(websocket)  # type: ignore[arg-type]
    except HTTPException:
        return None
    # 3.2 : le rôle qui compte est celui du projet de la session (agent_ws le projette)
    return user


@app.post("/api/agent/session")
def agent_session_new(_u: dict = Depends(require("dev"))) -> dict:
    """Alloue un nouvel identifiant de session de chat SDK."""
    return {"sid": agentchat.new_sid()}


@app.get("/api/agent/commands")
def agent_commands() -> list[dict]:
    """Slash commands disponibles (palette web quand l'utilisateur tape « / »)."""
    return agentchat.list_commands()


@app.websocket("/api/agent/ws/{sid}")
async def agent_ws(websocket: WebSocket, sid: str):
    if not _origin_ok(websocket):
        await websocket.close(code=4403)
        return
    if "/" in sid or ".." in sid:
        await websocket.close(code=4400)
        return
    wsu = _ws_user(websocket)
    if wsu is None:
        await websocket.close(code=4401)
        return
    # 3.2 lot 3 : la personne telle que la voit le PROJET de la session (rôle de ce projet) ;
    # aucun rôle dans ce projet = la session n'existe pas pour elle
    wsu = projectgate.ws_user(wsu, board.get_session_project(sid), sid)
    if wsu is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    revocation.track(wsu["email"], websocket)  # 3.2 lot 6 : fermé par « Revoke now »
    resume = websocket.query_params.get("resume") or None
    session = agentchat.get_or_create(sid, resume=resume, user=wsu["email"])
    queue = session.subscribe()

    async def pump() -> None:
        # replay du buffer (refresh) puis flux temps réel
        for ev in list(session.events):
            await websocket.send_json(ev)
        while True:
            ev = await queue.get()
            await websocket.send_json(ev)

    pump_task = asyncio.create_task(pump())
    can_drive = iam.rank(wsu["role"]) >= iam.rank("dev")
    try:
        while True:
            msg = await websocket.receive_json()
            t = msg.get("type")
            if revocation.is_disabled(wsu["email"]):  # révoqué pendant la connexion
                await websocket.close(code=4401)
                break
            if not can_drive:
                # viewer : flux en lecture seule — aucune mutation acceptée
                if t == "user":
                    await websocket.send_json({
                        "type": "error",
                        "message": "Lecture seule — l'historique est rejoué ; "
                                   "interagir demande un rôle dev.",
                    })
                continue
            if t == "user" and msg.get("text", "").strip():
                _bg(session.handle_user(msg["text"]))
            elif t == "permission":
                session.resolve_permission(msg.get("id", ""), {
                    "decision": msg.get("decision", "deny"),
                    "updated_input": msg.get("updated_input"),
                    "message": msg.get("message"),
                })
            elif t == "answer":
                session.resolve_question(msg.get("id", ""), msg.get("answers", {}))
            elif t == "interrupt":
                await session.interrupt()
            elif t == "mode":
                await session.set_mode(msg.get("mode", "default"))
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        session.unsubscribe(queue)
        revocation.untrack(wsu["email"], websocket)


# --- flow OIDC (login → Authentik → callback → session cookie) ---
PUBLIC_URL = os.environ.get("SOKKAN_PUBLIC_URL", "http://localhost:3009").rstrip("/")
_REDIRECT = f"{PUBLIC_URL}/api/auth/callback"


def _safe_next(nxt: str) -> str:
    """3.4.4 : où revenir après le login — un chemin LOCAL seulement (pas `//hôte`, pas de
    schéma, pas `/api/auth/…`), sinon « / ». Un lien Teams ouvert sans session perdait sa cible."""
    nxt = (nxt or "").strip()
    if (not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt
            or nxt.startswith("/api/auth") or any(c in nxt for c in "\r\n") or len(nxt) > 2000):
        return "/"
    return nxt


@app.get("/api/auth/login")
def auth_oidc_login(next: str = "/"):  # noqa: A002 — the query parameter's name
    if not oidc.ENABLED:
        raise HTTPException(501, "OIDC not configured")
    verifier, challenge = oidc.new_pkce()
    state = secrets.token_urlsafe(16)
    url = oidc.authorize_url(_REDIRECT, state, challenge)
    tx = jwt.encode({"s": state, "v": verifier, "n": _safe_next(next),
                     "exp": int(time.time()) + 600},
                    sess.SECRET, algorithm="HS256")
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("sokkan_oidc_tx", tx, max_age=600, httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/api/auth/callback")
def auth_oidc_callback(request: Request, code: str = "", state: str = ""):
    tx = request.cookies.get("sokkan_oidc_tx")
    if not tx:
        raise HTTPException(400, "missing OIDC transaction")
    try:
        txd = jwt.decode(tx, sess.SECRET, algorithms=["HS256"])
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "invalid OIDC transaction")
    if not code or txd.get("s") != state:
        raise HTTPException(400, "invalid OIDC state")
    try:
        tokens = oidc.exchange(code, _REDIRECT, txd["v"])
        claims = oidc.verify_id_token(tokens["id_token"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(401, f"OIDC exchange failed: {e}")
    email = (claims.get("email") or "").lower()
    if email and revocation.is_disabled(email):  # 3.2 lot 6 : compte révoqué / désactivé (SCIM)
        audit.log(email, "auth.login.refused", "account disabled", "")
        raise HTTPException(403, "account disabled on this instance")
    try:  # 3.2 lot 3 : équipes = groupes de l'IdP (claim `groups`), resynchronisées au login
        groups = claims.get(os.environ.get("SOKKAN_OIDC_GROUPS_CLAIM", "groups")) or []
        if email and isinstance(groups, list) and features.enabled("sso_teams"):
            projects.sync_sso_groups(email, [str(g) for g in groups])
            audit.log(email, "team.sync", ",".join(str(g) for g in groups)[:300], "")
            # 3.2 lot 6 : attributions perdues → sessions fermées, agents en pause
            revocation.run_soon(revocation.reconcile(email, by=f"login:{email}"))
    except Exception as e:  # noqa: BLE001 — un login n'échoue pas sur la synchro d'équipes
        print(f"[sokkan] SSO groups sync failed for {email}: {e!r}", file=sys.stderr)
    if not email:
        raise HTTPException(401, "OIDC token has no email")
    # 3.4.3: the display name of the login (`name`, else `preferred_username` when it is not
    # an address) is remembered — Nina named people by the local part of their e-mail
    display = str(claims.get("name") or "").strip()
    if not display or "@" in display:
        display = str(claims.get("preferred_username") or "").strip()
        display = "" if "@" in display else display
    try:
        if display:
            iam.set_name(email, display)
    except Exception as e:  # noqa: BLE001 — a login never fails on this bookkeeping
        print(f"[sokkan] display name of {email} not stored: {e!r}", file=sys.stderr)
    if claims.get("oid") and claims.get("tid"):
        try:  # 3.4 Teams : le compte Entra ID (oid) de la personne = SON compte SOKKAN
            import teams
            if teams.enabled() and claims["tid"] == teams.tenant_id():
                from teams import store as teams_store
                teams_store.link_user(str(claims["oid"]), str(claims["tid"]), email, display)
        except Exception as e:  # noqa: BLE001 — un login n'échoue pas sur le lien Teams
            print(f"[sokkan] Teams link failed for {email}: {e!r}", file=sys.stderr)
    sid = str(claims.get("sid") or "")
    try:  # 3.4 : IdP session → cockpit cookie, for OIDC back-channel / front-channel logout
        if revocation.enabled():
            revocation.record_login(email, sid=sid, sub=str(claims.get("sub") or ""))
    except Exception as e:  # noqa: BLE001 — a login never fails on this bookkeeping
        print(f"[sokkan] OIDC session record failed for {email}: {e!r}", file=sys.stderr)
    resp = RedirectResponse(f"{PUBLIC_URL}{_safe_next(txd.get('n') or '/')}", status_code=302)
    resp.set_cookie(sess.COOKIE, sess.make(email, claims.get("name", ""), sid=sid),
                    max_age=sess.TTL, httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("sokkan_oidc_tx")
    return resp


@app.get("/api/auth/logout")
def auth_oidc_logout():
    resp = RedirectResponse(f"{PUBLIC_URL}/", status_code=302)
    resp.delete_cookie(sess.COOKIE)
    return resp


# NB : /api/magnitude/agent/sync a sa propre auth (header x-magnitude-token),
# pas de cookie — l'agent host n'a pas de session utilisateur.
_AUTH_FREE = ("/api/auth/", "/api/health", "/api/version", "/api/edge/ask", "/api/observability/alert",
              "/api/magnitude/agent/sync", "/api/magnitude/install.sh",
              "/api/magnitude/metrics",  # Prometheus: own bearer token or direct loopback
              "/api/memory/hook",  # jeton x-sokkan-hook-token (hooks des sessions terminal)
              forge_routes.CRED_PATH,  # 3.2 lot 5 : ticket de session + loopback seulement
              "/api/scim/",  # 3.2 lot 6: SCIM, own bearer token (SOKKAN_SCIM_TOKEN)
              "/api/teams/messages")  # 3.4 : JWT Bot Framework vérifié par teams.botauth


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Gate global : toute route /api exige une identité résolue (sauf /api/auth/* + health).
    Indispensable hors CF Access (mode oidc) : sinon les lectures seraient publiques."""
    p = request.url.path
    token = None
    if p.startswith("/api/") and not p.startswith(_AUTH_FREE):
        try:
            user = auth.instance_user(request)
            # 3.2 lot 3 : de quel projet parle la requête, avec quel rôle (projectgate)
            token = projectgate.resolve(request, user)
            # 3.2.2 Captains demo: nothing is written by a visitor (403 before any route)
            ro = demo_captains.guard(request.method, p, user)
            if ro:
                projectgate.reset(token)
                return JSONResponse({"detail": ro}, status_code=403)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        except projectgate.Denied as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
    try:
        return await call_next(request)
    finally:
        projectgate.reset(token)


def _transcripts() -> list[Path]:
    return sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


# 3.2 lot 6 : SCIM 2.0 (Users, Groups) + « Revoke now » (admin)
app.include_router(revocation.router(require))
# 3.4 : OIDC back-channel / front-channel logout (/api/auth/* : no cookie, own checks)
app.include_router(oidc_logout.router())

# banc de recall (CortHeXis → Banc) + carte « Mémoire » de Magnitude (profil, licence)
app.include_router(memeval.router(require, feature_magnitude, _transcripts))


@app.get("/api/health")
def health() -> dict:
    files = list(PROJECT_DIR.glob("*.jsonl"))
    return {"ok": True, "project_dir": str(PROJECT_DIR), "transcripts": len(files)}


def _build_version() -> dict:
    """What THIS process runs: the VERSION file baked into the image (repo root in dev) and the
    commit passed at build time (SOKKAN_COMMIT). Read once — a rollout check compares it with the
    dist it expects, so an old container still answering /api/health is caught."""
    root = Path(__file__).resolve().parent.parent
    ver = ""
    for cand in (root / "VERSION", Path("/app/VERSION")):
        try:
            ver = cand.read_text().strip()
            break
        except OSError:
            continue
    commit = (os.environ.get("SOKKAN_COMMIT") or "").strip()
    if not commit:
        try:
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
    short = commit[:7]
    return {"version": ver or "dev", "commit": short or "unknown",
            "dist": f"{ver or 'dev'}+{short}" if short else (ver or "dev"),
            "image_tag": os.environ.get("SOKKAN_VERSION", "")}


_BUILD = _build_version()


@app.get("/api/version")
def version() -> dict:
    """Auth-free: version, commit and edition of the running process (no secret, no config)."""
    return {**_BUILD, "edition": features.edition()}


@app.get("/api/tags")
def tags() -> list[str]:
    return board.TAGS


def _live_targets() -> set[str]:
    """Ensemble des fenêtres tmux vivantes ('session:window')."""
    return {f"{w['session']}:{w['window']}" for w in _tmux_windows()}


@app.get("/api/sessions")
def sessions() -> list[dict]:
    """Sessions POSSÉDÉES par SOKKAN (créées via spawn), repérées par tag.
    Deux kinds : 'sdk' (chat Agent SDK, défaut S2) et 'tmux' (terminal power-user)."""
    now = time.time()
    live = _live_targets()
    out: list[dict] = []
    _clr = classification.ctx_clearance()
    for s in board.list_sessions():
        if not _in_ctx(s):            # 3.2 : les sessions du projet sélectionné seulement
            continue
        if _clr is not None and (classification.session_level(s["session_id"]) or 0) > _clr:
            continue                  # 3.4 : au-dessus de l'habilitation = n'existe pas
        if s.get("kind") == "sdk":
            csid = s.get("claude_session_id") or ""
            p = PROJECT_DIR / f"{csid}.jsonl" if csid else None
            exists = bool(p and p.exists())
            mtime = p.stat().st_mtime if exists else s["created_at"]
            age = now - mtime
            a = agentchat.peek(s["session_id"])
            st = "working" if (a is not None and a._busy) else "idle"
            out.append({
                **s, "mtime": mtime, "age_s": round(age, 1), "exists": exists,
                # une session SDK est toujours rattachable (resume persisté)
                "alive": True, "live_state": st,
                "active": st == "working" or (exists and age <= ACTIVE_WINDOW_S),
            })
            continue
        p = PROJECT_DIR / f"{s['session_id']}.jsonl"
        exists = p.exists()
        mtime = p.stat().st_mtime if exists else s["created_at"]
        age = now - mtime
        alive = s["window"] in live
        st = panestate.classify(s["window"], alive=alive)["state"]
        out.append({
            **s, "mtime": mtime, "age_s": round(age, 1), "exists": exists,
            "alive": alive, "live_state": st,
            # « active » = claude bosse OU le transcript a bougé récemment
            "active": alive and (st == "working"
                                 or (exists and age <= ACTIVE_WINDOW_S)),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


class SpawnBody(BaseModel):
    tag: str = "session"
    prompt: str = ""
    title: str = ""
    kind: str = "sdk"  # 'sdk' (chat SDK, défaut) | 'tmux' (terminal power-user)
    playbook: str = ""  # id d'un template de session (GET /api/playbooks) — optionnel
    # secrets du coffre (NOMS) pour cette session — pris en compte si
    # SOKKAN_SESSION_SECRETS=named ; None = ceux du playbook, sinon aucun
    secrets: list[str] | None = None
    # 3.2 multi-user : projet de la session (périmètre mémoire). Absent = le projet
    # sélectionné dans le cockpit (en-tête x-sokkan-project), 'default' sinon
    project: str | None = None


def _vault_names() -> list[str]:
    """Vault NAMES a session / an agent of the request's project may use: that project's
    vault only (lot 4); a project without one (feature off, `shared`) gets none."""
    return vault.names(_ctx_project())


def _ctx_project() -> str:
    """Project of the request being served (projectgate), 'default' outside one."""
    ctx = projectgate.current()
    return (ctx or {}).get("project") or projects.DEFAULT_PROJECT


def _in_ctx(row: dict) -> bool:
    """A stored row (session, card, agent…) belongs to the request's project."""
    return (row.get("project") or projects.DEFAULT_PROJECT) == _ctx_project()


def _session_scope(session_id: str | None) -> tuple[str, ...]:
    """Périmètre mémoire d'une session (3.2) : son projet seulement — fail-closed.
    3.4 : avec l'habilitation de la personne qui l'a ouverte."""
    try:
        return classification.session_scope(session_id)
    except Exception:  # noqa: BLE001
        return ()


def _memory_preseed(query: str, top_k: int = 5, max_chars: int = 2400,
                    session_id: str | None = None) -> str:
    """Recherche mémoire DÉTERMINISTE au spawn : le serveur fait le memory_search
    lui-même et pré-injecte le top-k dans le premier message — le rappel ne
    dépend plus de l'obéissance du modèle au rituel. Best-effort : mémoire vide
    ou backend down → chaîne vide (le seed retombe sur le rituel textuel).
    3.2 : limité au projet de la session (jamais une note d'un autre projet)."""
    try:
        hits = mem.search_scoped(query, top_k, _session_scope(session_id))
    except Exception:  # noqa: BLE001 — le spawn ne doit jamais échouer sur la mémoire
        return ""
    if not isinstance(hits, list) or not hits or hits and hits[0].get("empty"):
        return ""
    if session_id and store_backend.enabled():
        try:  # recall_log : le rappel à chaque tour ne réinjecte pas ces notes
            store_backend.log_spawn_recall(session_id, [h for h in hits if h.get("note_name")],
                                           query)
        except Exception:  # noqa: BLE001
            pass
    lines = ["=== Project memory (auto-recalled) ==="]
    for h in hits:
        if not h.get("note_name"):
            continue
        star = "★ " if h.get("priority") else ""
        line = f"- {star}[{h['note_name']}] {h.get('description', '')}".rstrip()
        snip = (h.get("snippet") or "").replace("\n", " ").strip()
        if snip:
            line += f" — {snip}"
        lines.append(line[:400])
        if sum(len(x) + 1 for x in lines) > max_chars:
            break
    return "\n".join(lines) if len(lines) > 1 else ""


def _spawn_sdk(tag: str, prompt: str = "", title: str = "", user: str = "",
               secrets: list[str] | None = None, project: str = "default",
               context: str = "") -> dict:
    """Session SDK : enregistrée dans le store + AgentSession créée ; le seed
    (sujet + mémoire pré-injectée + HITL) part en tâche de fond — les events
    sont bufferisés et rejoués quand le pane se connecte."""
    sid = agentchat.new_sid()
    s = board.add_sdk_session(sid, tag, title=title, prompt=prompt, secrets=secrets,
                              project=project, owner=user if "@" in (user or "") else "")
    session = agentchat.get_or_create(sid, user=user, secrets=secrets)
    day_budget = instance.budgets().get("budget_day_usd", 0.0)
    if day_budget:
        try:  # avertissement (pas un blocage) — le jour est déjà bien entamé ?
            spent = usage_mod.summary(1)["totals"]["today"]["metered"]  # 3.2.3: billed, or API-equivalent on a subscription
            if spent >= day_budget:
                session._emit({"type": "error", "message": (
                    f"Daily budget notice: today's estimated spend is ${spent:.2f}, "
                    f"over the ${day_budget:.2f}/day budget. This session still works — "
                    "consider wrapping up or raising the budget (Setup › Organization).")})
        except Exception:  # noqa: BLE001 — le spawn ne dépend jamais du calcul de coûts
            pass
    bstate, bmsg = budgets.check(project)   # 3.2 lot 4 : budget du projet (jour / mois)
    if bstate != "ok":
        session._emit({"type": "error", "message": bmsg})
    if prompt.strip():
        recall = _memory_preseed(f"{title} {prompt}".strip() if title else prompt,
                                 session_id=sid)
        if context:  # 3.3 Helm : le contexte des cartes mères DESCEND, avant le rappel mémoire
            recall = f"{context}\n\n{recall}".strip()
        _bg(session.handle_user(board.seed_text(prompt, recall)))
    return s


@app.get("/api/playbooks")
def playbooks_list(_u: dict = Depends(current_user)) -> list[dict]:
    """Templates de sessions (spawn UI) : refactor, debug, onboarding mémoire…"""
    return playbooks.catalog()


@app.post("/api/spawn")
async def spawn_session(body: SpawnBody, u: dict = Depends(require("dev"))) -> dict:
    """Crée une session SOKKAN — chat SDK par défaut, fenêtre tmux si kind='tmux'.
    `playbook` applique un template (prompt façonné + tag par défaut) au sujet tapé.
    `project` (3.2) : la personne doit y avoir au moins le rôle dev."""
    body.project = body.project or _ctx_project()
    if body.project != _ctx_project():
        # le rôle de la requête est celui du projet sélectionné : on n'en crée pas ailleurs
        raise HTTPException(400, "the session's project must be the selected project")
    if u.get("project_role") not in ("dev", "maintainer", "admin") and \
            iam.rank(u["role"]) < iam.rank("dev"):
        raise HTTPException(403, f"no developer access to project '{body.project}'")
    if body.kind == "tmux" and body.project != projects.DEFAULT_PROJECT:
        # lot 1 : un terminal lit le dossier mémoire et .mcp.json partagés → projet par défaut
        raise HTTPException(400, "terminal sessions are only available in the default project")
    if body.secrets is not None:
        unknown = [n for n in body.secrets if n not in _vault_names()]
        if unknown:
            raise HTTPException(400, f"secrets not in the vault: {', '.join(unknown)}")
    if body.playbook and body.secrets is None:
        body.secrets = (playbooks.get(body.playbook) or {}).get("secrets")
    if body.playbook:
        rendered = playbooks.render(body.playbook, body.prompt)
        if rendered is None:
            raise HTTPException(400, f"unknown playbook: {body.playbook}")
        pb = playbooks.get(body.playbook)
        # sujet vide sur un playbook qui en attend un = session lancée à l'aveugle
        # (« Bug to investigate: » tout court). Vu en vrai sur une mission blanche :
        # l'agent part explorer au hasard et l'humain doit tout re-prompter.
        if not pb.get("subject_optional") and not body.prompt.strip():
            raise HTTPException(400, f"the \"{pb['label']}\" playbook needs a subject — "
                                     "what should it work on?")
        body.prompt, default_tag = rendered
        if body.tag in ("", "session"):
            body.tag = default_tag
        if not body.title:
            subj = body.prompt.splitlines()[0][:60]
            body.title = pb["label"] if pb.get("subject_optional") else f"{pb['label']}: {subj}"
    if body.kind == "tmux":
        s = board.spawn(body.tag, prompt=body.prompt, title=body.title)
    else:
        extra = {"secrets": body.secrets} if body.secrets is not None else {}
        s = _spawn_sdk(body.tag, prompt=body.prompt, title=body.title, user=u["email"],
                       project=body.project, **extra)
    audit.log(u["email"], "session.spawn", s.get("window") or s["session_id"],
              f"{s['title']} ({body.kind})"
              + (f" · secrets: {', '.join(body.secrets)}" if body.secrets else ""))
    return s


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    # session_id is a file stem; reject path traversal
    if "/" in session_id or ".." in session_id:
        raise HTTPException(400, "invalid session id")
    path = PROJECT_DIR / f"{session_id}.jsonl"
    s = next((x for x in board.list_sessions() if x["session_id"] == session_id), None)
    if s and s.get("kind") == "sdk":
        # le live des panes SDK passe par le WebSocket agent — mais au refresh,
        # l'historique complet se réhydrate depuis le transcript persisté par
        # Claude Code (le ring buffer WS ne garde que les RING_MAX derniers events)
        csid = s.get("claude_session_id") or ""
        tpath = PROJECT_DIR / f"{csid}.jsonl" if csid else None
        if tpath and tpath.exists():
            d = T.parse_file(tpath)
            # 3.1.2 : an agent run's transcript (Crew → History) never shows its secrets
            masked = agents.secrets_for_session(session_id)
            if masked:
                d = agents.redact_obj(d, masked)
            d.update({
                "session_id": session_id, "title": s["title"], "tag": s["tag"],
                "window": "", "active": False, "alive": True,
                "exists": True, "starting": False, "kind": "sdk",
            })
            return d
        return {
            "session_id": session_id, "title": s["title"], "tag": s["tag"],
            "window": "", "git_branch": "", "messages": [], "n_messages": 0,
            "mtime": s["created_at"], "size": 0, "active": False, "alive": True,
            "exists": False, "starting": False, "kind": "sdk",
        }
    alive = bool(s) and s["window"] in _live_targets()
    if not path.exists():
        # session SOKKAN créée mais claude n'a pas encore écrit son transcript
        # (pas de prompt envoyé, ou claude encore en boot) → on lit l'état du pane
        # pour ne PAS rester bloqué en « démarrage » indéfiniment.
        if s:
            booting = panestate.is_booting(s["window"], alive)
            return {
                "session_id": session_id, "title": s["title"], "tag": s["tag"],
                "window": s["window"], "git_branch": "", "messages": [],
                "n_messages": 0, "mtime": s["created_at"], "size": 0,
                "active": alive, "alive": alive, "exists": False,
                "starting": booting,  # True seulement tant que claude n'a pas démarré
            }
        raise HTTPException(404, "session not found")
    d = T.parse_file(path)
    st = panestate.classify(s["window"], alive=alive)["state"] if s else None
    d["active"] = alive and (st == "working"
                             or (time.time() - d["mtime"]) <= ACTIVE_WINDOW_S)
    d["exists"] = True
    d["alive"] = alive
    if s:  # titre/tag/fenêtre depuis le store SOKKAN
        d["title"] = s["title"]
        d["tag"] = s["tag"]
        d["window"] = s["window"]
    return d


def _session_window(session_id: str) -> tuple[str, bool]:
    """Fenêtre tmux d'une session SOKKAN + si elle est vivante."""
    s = next((x for x in board.list_sessions() if x["session_id"] == session_id), None)
    if not s:
        raise HTTPException(404, "session not found")
    return s["window"], s["window"] in _live_targets()


@app.get("/api/sessions/{session_id}/live")
def session_live(session_id: str, _f: None = Depends(feature_tmux)) -> dict:
    """Signe de vie temps-réel depuis le pane tmux : working/awaiting/idle + miroir
    du terminal + choix proposés par claude. Poll rapide côté chat."""
    if "/" in session_id or ".." in session_id:
        raise HTTPException(400, "invalid session id")
    window, alive = _session_window(session_id)
    st = panestate.classify(window, alive=alive)
    return {"session_id": session_id, "window": window, "alive": alive, **st}


# touches autorisées vers le pane (choix de menu, navigation, validation)
_KEY_NAMED = {"Enter", "Escape", "Up", "Down", "Tab", "Space", "BSpace"}


class KeyBody(BaseModel):
    key: str  # "1".."9", "y", "n", ou une touche nommée (Enter/Escape/Up/Down…)


@app.post("/api/sessions/{session_id}/key")
def session_key(session_id: str, body: KeyBody, u: dict = Depends(require("dev")),
                _f: None = Depends(feature_tmux)) -> dict:
    """Envoie UNE touche au pane (répondre à un menu de choix claude depuis le chat)."""
    if "/" in session_id or ".." in session_id:
        raise HTTPException(400, "invalid session id")
    window, alive = _session_window(session_id)
    if not alive:
        raise HTTPException(400, "tmux window is closed")
    k = body.key.strip()
    if k in _KEY_NAMED:
        subprocess.run(["tmux", "send-keys", "-t", window, k], timeout=5)
    elif re.fullmatch(r"[1-9a-zA-Z]", k):
        # littéral (un menu claude se sélectionne au chiffre, sans Enter)
        subprocess.run(["tmux", "send-keys", "-t", window, "-l", k], timeout=5)
    else:
        raise HTTPException(400, f"key not allowed: {k!r}")
    audit.log(u["email"], "session.key", window, k)
    return {"ok": True, "window": window, "key": k}


class SendBody(BaseModel):
    target: str  # tmux "session:window", e.g. "A:Messaging"
    text: str


@app.post("/api/send")
def send(body: SendBody, u: dict = Depends(require("dev")),
         _f: None = Depends(feature_tmux)) -> dict:
    """Type text into a tmux window running Claude Code, then submit (Enter).

    The target must be a currently-live tmux window (validated) — this is how SOKKAN
    lets you intervene in a session from the web. Behind CF Access (admin only).
    """
    valid = {f"{w['session']}:{w['window']}" for w in _tmux_windows()}
    if body.target not in valid:
        raise HTTPException(400, f"unknown tmux target: {body.target}")
    if not body.text.strip():
        raise HTTPException(400, "empty text")
    # -l = literal (no key-name interpretation), then a separate Enter to submit
    subprocess.run(["tmux", "send-keys", "-t", body.target, "-l", body.text], timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", body.target, "Enter"], timeout=5)
    # audit = l'action (un prompt a été envoyé), pas le contenu complet
    audit.log(u["email"], "session.send", body.target, body.text[:120])
    return {"ok": True, "target": body.target}


@app.get("/api/bindings")
def bindings() -> list[dict]:
    """window↔session pour les sessions SOKKAN (toujours connu — on les a créées)."""
    live = _live_targets()
    out = []
    for s in board.list_sessions():
        if not _in_ctx(s):
            continue
        win = s["window"] or ""
        sess, _, wname = win.partition(":")
        out.append({
            "tmux_session": sess, "window": wname, "session_id": s["session_id"],
            "target": win, "tag": s["tag"], "alive": win in live,
            "transcript_exists": (PROJECT_DIR / f"{s['session_id']}.jsonl").exists(),
        })
    return out


@app.delete("/api/sessions/{session_id}")
async def session_close(session_id: str, u: dict = Depends(require("dev"))) -> dict:
    """Supprime une session SOKKAN : ferme le client SDK ou la fenêtre tmux + retire du store."""
    s = next((x for x in board.list_sessions() if x["session_id"] == session_id), None)
    if s and s.get("kind") == "sdk":
        await agentchat.drop(session_id)
        board.close_session(session_id, kill=False)
    else:
        board.close_session(session_id, kill=True)
    audit.log(u["email"], "session.close", session_id)
    return {"ok": True}


class UserBody(BaseModel):
    email: str
    role: str = "dev"
    name: str = ""


@app.get("/api/iam/users")
def iam_users(_u: dict = Depends(require("admin"))) -> list[dict]:
    return iam.list_users()


@app.post("/api/iam/users")
def iam_upsert(body: UserBody, u: dict = Depends(require("admin"))) -> dict:
    try:
        r = iam.upsert_user(body.email, body.role, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "iam.upsert", body.email, f"rôle {body.role}")
    return r


@app.delete("/api/iam/users/{email}")
def iam_delete(email: str, u: dict = Depends(require("admin"))) -> dict:
    try:
        iam.delete_user(email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "iam.delete", email)
    return {"ok": True}


@app.get("/api/audit")
def audit_recent(request: Request, limit: int = 200, q: str = "",
                 u: dict = Depends(current_user)) -> list[dict]:
    """Journal des actions (onglet Journal) : qui a fait quoi, quand.
    3.2 : avec plusieurs projets, le journal complet (titres de sessions, cartes, agents de
    tous les projets) est réservé aux admins de l'instance. Lot 4 (`project_vault_budgets`) :
    un admin (ou maintainer) d'un projet voit le journal DE CE projet (projet sélectionné,
    en-tête x-sokkan-project) — jamais les événements d'instance ni ceux d'autres projets."""
    rank = iam.rank(u.get("role") or "")
    if not projects.multi_project():
        if rank < iam.rank("viewer"):
            raise HTTPException(403, "role 'viewer' required")
        return audit.recent(limit=limit, q=q)
    if rank >= iam.rank("admin"):
        return audit.recent(limit=limit, q=q)
    slug = projectgate.requested_project(request)
    if features.enabled("project_vault_budgets") and \
            projects.prank(projects.effective_role(u, slug)) >= projects.prank("maintainer"):
        return audit.recent(limit=limit, q=q, project=slug)
    raise HTTPException(403, "with several projects the journal is for instance admins "
                             "and the admins of a project (its own journal)")


# --- environnements cloud (connecteur ouvert → control plane NINABOT fermé) ---
def _provision_enabled() -> None:
    if not provision.ENABLED:
        raise HTTPException(404, "environment provisioning is not configured on this instance")


def _provision_call(fn, *args):
    try:
        return fn(*args)
    except provision.ProvisionerError as e:
        raise HTTPException(e.status, e.detail)


class EnvSpawnBody(BaseModel):
    client: str
    tier: str = "starter"
    owner_email: str


@app.get("/api/infra/envs")
def infra_envs(_u: dict = Depends(require("admin")),
               _f: None = Depends(_provision_enabled)) -> list:
    return _provision_call(provision.list_envs)


@app.get("/api/infra/envs/{client}")
def infra_env_detail(client: str, _u: dict = Depends(require("admin")),
                     _f: None = Depends(_provision_enabled)) -> dict:
    return _provision_call(provision.env_detail, client)


@app.post("/api/infra/envs", status_code=202)
def infra_env_spawn(body: EnvSpawnBody, u: dict = Depends(require("admin")),
                    _f: None = Depends(_provision_enabled)) -> dict:
    if body.tier not in provision.TIERS:
        raise HTTPException(400, f"tier must be one of {provision.TIERS}")
    r = _provision_call(provision.spawn, body.client.strip().lower(), body.tier, body.owner_email)
    audit.log(u["email"], "env.spawn", body.client, f"tier {body.tier}")
    return r


@app.delete("/api/infra/envs/{client}")
def infra_env_destroy(client: str, u: dict = Depends(require("owner")),
                      _f: None = Depends(_provision_enabled)) -> dict:
    r = _provision_call(provision.destroy, client)
    audit.log(u["email"], "env.destroy", client)
    return r


@app.get("/api/infra/nodes")
def infra_nodes() -> list[dict]:
    return infra.nodes()


@app.get("/api/infra/targets")
def infra_targets() -> list[dict]:
    return infra.targets()


def _ctx_scope() -> tuple[str, ...]:
    """Memory the request's project may read: itself + shared (3.2), up to the person's
    clearance in each (3.4)."""
    return classification.ctx_scope()


@app.get("/api/memory/stats")
def memory_stats() -> dict:
    """3.4.2: ``store`` says what serves the memory (``mode`` sqlite | postgres) and whether
    project memory / classification are possible — the cockpit warns when the selected
    project cannot have a memory on this instance."""
    store = store_backend.store_info()
    if _ctx_project() != projects.DEFAULT_PROJECT:
        # counts of the selected project only — at the READER's clearance there (3.4.3: a
        # bare slug counted at the default level, so a project of confidential notes said
        # « 0 notes » while /api/memory/notes listed them)
        own = classification.own_entry(_ctx_scope(), _ctx_project())
        notes = memorykb.list_notes(own) if own else []
        return {"notes": len(notes), "chunks": sum(n.get("chunks") or 0 for n in notes),
                "project": _ctx_project(), "store": store}
    return {**memorykb.stats(), "project": _ctx_project(), "store": store}


@app.get("/api/memory/notes")
def memory_notes() -> list[dict]:
    """3.2 : les notes du projet sélectionné + celles de shared (lecture)."""
    return memorykb.list_notes(_ctx_scope())


@app.get("/api/memory/search")
def memory_search(q: str, k: int = 8, deep: bool = False) -> list[dict]:
    """`deep=1` : reranker même en profil standard (asynchrone, ~4-5 s sur CPU).
    3.2 : le projet sélectionné (+ shared) ; projectgate a vérifié l'accès."""
    scope = _ctx_scope()
    if store_backend.enabled():
        out = store_backend.memory_search(q, max(1, min(k, 50)), deep=deep, projects=scope)
        classification.log_access("cockpit", [h for h in out if h.get("note_name")],
                                  actor=(projectgate.current() or {}).get("email"), query=q)
        return out
    return mem.search_scoped(q, k, scope)


def _store_or_503():
    if not store_backend.enabled():
        raise HTTPException(503, "the memory store (3.0) is not enabled on this instance")
    try:
        return store_backend.get_store()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"memory store unavailable: {e}")


@app.get("/api/memory/status")
def memory_status() -> dict:
    """Backend, index (génération, notes), indexeur, profil d'embedding, rappel."""
    out: dict = {"backend": "postgres" if store_backend.enabled() else "sqlite",
                 "store": store_backend.store_info()}
    if not store_backend.enabled():
        out["index"] = memorykb.stats()
        return out
    try:
        from core import embed, recall as core_recall
        out["embed"] = embed.describe()
        out["rerank_policy"] = store_backend.rerank_policy()
        out["recall"] = {"enabled": core_recall.enabled(),
                         **core_recall.RecallConfig.from_env().__dict__}
    except Exception as e:  # noqa: BLE001
        out["embed_error"] = str(e)
    try:
        st = store_backend.get_store()
        out["index"] = st.stats()
        out["generations"] = [g.__dict__ for g in st.list_generations()]
    except Exception as e:  # noqa: BLE001
        out["index_error"] = str(e)
    out["indexer"] = _index_runner.status() if _index_runner else None
    return out


@app.post("/api/memory/index")
def memory_index(u: dict = Depends(require("dev"))) -> dict:
    """Réindexation immédiate (la boucle tourne déjà : watch + périodique)."""
    if not store_backend.enabled() or _index_runner is None:
        raise HTTPException(503, "the memory store (3.0) indexer is not running")
    rep = _index_runner.run_once()
    audit.log(u["email"], "memory.index", "")
    st = _index_runner.status()
    if rep is None:
        raise HTTPException(500, st.get("last_error") or "index failed")
    return st


@app.post("/api/memory/hook")
async def memory_hook(request: Request) -> dict:
    """Hooks UserPromptSubmit / PreToolUse(Task|Agent) des sessions terminal : le rappel
    tourne ici, à chaud (pool Postgres + client d'embedding ouverts). Auth : jeton du
    fichier data/claude-hooks/recall-token (0600), pas de cookie."""
    if not memrecall.check_token(request.headers.get("x-sokkan-hook-token")):
        raise HTTPException(401, "bad hook token")
    if not memrecall.active():
        return {}
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")
    sid = os.environ.get("CORTHEXIS_RECALL_SESSION_ID") or payload.get("session_id") or ""
    scope = _session_scope(sid or None)  # 3.2 : le projet de la session, rien d'autre
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(memrecall.hook_output, payload, sid, scope), timeout=4)
    except Exception as e:  # noqa: BLE001 — un rappel ne casse jamais un tour
        print(f"[sokkan] recall hook failed: {e!r}", file=sys.stderr)
        return {}


@app.get("/api/memory/recall-log")
def memory_recall_log(session: str = "", note: str = "", limit: int = 200) -> dict:
    """Ce que le rappel automatique a injecté : quelle session / quel sous-agent a reçu
    quelles notes, avec quels scores, depuis quelle génération d'index."""
    st = _store_or_503()
    # 3.2 : les injections de notes du projet sélectionné seulement (son projet, pas shared :
    # le journal dit QUELLE session a reçu quoi — des sessions d'autres projets lisent shared)
    return {"entries": st.recall_log(session_id=session or None, note=note or None,
                                     limit=limit,
                                     projects=classification.own_entry(_ctx_scope(),
                                                                       _ctx_project())),
            "summary": st.recall_summary() if not projects.multi_project() else {}}


@app.get("/api/memory/note/{name}")
def memory_note(name: str) -> dict:
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    from core import levels as _lv
    from core import scope as _sc
    # 3.4.3: the same keys whatever the store and the project (null when unknown)
    if store_backend.enabled():
        rec = store_backend.memory_get_record(name, projects=_ctx_scope())
        if rec is None:
            return {"name": name, "body": None, "project": _ctx_project(), "level": None}
        classification.log_access("cockpit", [rec],
                                  actor=(projectgate.current() or {}).get("email"))
        return {"name": name, "body": store_backend.render_note(rec),
                "project": rec.project, "level": _lv.ident(getattr(rec, "level", 2))}
    body = (mem.memory_get(name) if _sc.allows(_ctx_scope(), projects.DEFAULT_PROJECT)
            else None)
    return {"name": name, "body": body, "project": projects.DEFAULT_PROJECT,
            "level": _lv.ident(_lv.DEFAULT) if body is not None else None}


@app.get("/api/memory/migration")
def memory_migration_state(_u: dict = Depends(require("viewer"))) -> dict:
    """Migration 2.x -> 3.0 (CortHeXis tab): steps, normalize plan, progress, date test,
    verification, log, and which index serves searches meanwhile."""
    return memory_migration.status()


class MigrationApproval(BaseModel):
    what: str  # normalize (policy ask) | override (go on in spite of a failed check)


@app.post("/api/memory/migration/approve")
def memory_migration_approve(body: MigrationApproval,
                             u: dict = Depends(require("admin"))) -> dict:
    try:
        doc = memory_migration.approve(body.what, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.log(u["email"], "memory.migration.approve", body.what)
    return {"approved": doc}


# 3.2 lot 5 — GitLab : comptes liés, droits lus avec le jeton de la personne, credential
# helper des sessions (backend/forge/)
def _forge_live_user(sid: str) -> str | None:
    s = agentchat.peek(sid)
    return s.user if s is not None else None


async def _forge_close_sessions(email: str) -> int:
    """Unlink = immediate revocation: close the person's live sessions of forge projects."""
    n = 0
    for sid, s in list(agentchat._registry.items()):
        if (s.user or "").lower() != email:
            continue
        p = projects.get(agentchat.session_project(sid) or "")
        if p and p["access_source"] == "forge":
            await agentchat.drop(sid)
            n += 1
    return n


app.include_router(forge_routes.router(current_user, require, _forge_live_user,
                                       _forge_close_sessions))


# 3.4 classification : niveaux, habilitations, rappel audité (backend/classification_api.py)
import classification_api  # noqa: E402
app.include_router(classification_api.router)
# 3.4 Microsoft Teams : @Nina, approbations en cartes, décisions, calendrier (backend/teams/)
from teams import api as teams_api  # noqa: E402
from teams import graph as teams_graph  # noqa: E402
app.include_router(teams_api.router)
teams_graph.register()        # interface `calendars` : configured() = feature + app

# onglet CortHeXis : graphe, revue, réparations avec approbation (backend/corthexis.py)
app.include_router(corthexis.router)
corthexis.spawn_hook = _spawn_sdk
# 3.0 : après une réparation, l'IndexRunner (store) réindexe ; repli 2.x sinon
corthexis.reindex_hook = lambda: _index_runner.kick() if _index_runner else index_memory.run_index()


@app.post("/api/memory/digest")
def memory_digest(u: dict = Depends(require("dev"))) -> dict:
    """Memory Digest : spawn une session qui synthétise l'état du projet dans la
    note `project-status` — la mémoire se résume elle-même, à la demande."""
    prompt, tag = playbooks.render("digest")
    s = _spawn_sdk(tag, prompt, title="memory digest", user=u["email"], project=_ctx_project())
    audit.log(u["email"], "memory.digest", s["session_id"])
    return s


@app.get("/api/preview/repos")
def preview_repos(_u: dict = Depends(require("dev")),
                  _f: None = Depends(feature_preview)) -> list[dict]:
    return preview.list_repos()


@app.get("/api/preview/diff")
def preview_diff(repo: str, _u: dict = Depends(require("dev")),
                 _f: None = Depends(feature_preview)) -> dict:
    try:
        return preview.diff(repo)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/preview/test/{repo}")
def preview_test(repo: str, u: dict = Depends(require("dev")),
                 _f: None = Depends(feature_preview)) -> dict:
    """Lance la commande de tests du repo (déclarée dans SOKKAN_REPOS) — sur
    clic humain uniquement, jamais automatique. Timeout 10 min."""
    try:
        r = preview.run_tests(repo)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 — timeout etc.
        raise HTTPException(502, f"tests: {e}")
    audit.log(u["email"], "preview.test", repo, f"exit {r['code']}")
    return r


@app.get("/api/preview/envs")
def preview_envs(_u: dict = Depends(require("dev")),
                 _f: None = Depends(feature_preview)) -> list[dict]:
    return previewenv.list_envs()


@app.post("/api/preview/envs/{name}/start")
def preview_env_start(name: str, u: dict = Depends(require("dev")),
                      _f: None = Depends(feature_preview)) -> dict:
    try:
        r = previewenv.start(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit.log(u["email"], "preview.start", name)
    return r


@app.post("/api/preview/envs/{name}/stop")
def preview_env_stop(name: str, u: dict = Depends(require("dev")),
                     _f: None = Depends(feature_preview)) -> dict:
    try:
        r = previewenv.stop(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit.log(u["email"], "preview.stop", name)
    return r


@app.get("/api/preview/trigger")
def preview_trigger_latest(_u: dict = Depends(require("dev")),
                           _f: None = Depends(feature_preview)) -> dict:
    """Dernier aperçu poussé par une session (outil MCP open_preview)."""
    return {"trigger": previewenv.latest_trigger()}


@app.get("/api/preview/shot")
def preview_shot(url: str, w: int = 1440, h: int = 900,
                 _u: dict = Depends(require("dev")),
                 _f: None = Depends(feature_preview)):
    try:
        path = preview.screenshot(url, w, h)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"screenshot failed: {e}")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


def _tmux_windows() -> list[dict]:
    """Live tmux windows (best-effort; empty list if tmux absent)."""
    fmt = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_current_command}\t#{window_activity}"
    try:
        raw = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", fmt],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    out = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        sess, idx, wname, cmd, act = parts[:5]
        out.append({"session": sess, "index": idx, "window": wname,
                    "cmd": cmd, "activity": act})
    return out


@app.get("/api/tmux")
def tmux(_f: None = Depends(feature_tmux)) -> list[dict]:
    return _tmux_windows()


class CardCreate(BaseModel):
    title: str = ""
    description: str = ""  # le "prompt" de la tâche
    tag: str = "backend"
    bucket: str = "Backlog"
    priority: int = 2
    due: str = ""
    # 3.3 Helm (feature `helm`) : hiérarchie et contexte qui descend
    parent_id: int | None = None
    kind: str = "task"
    intent: str = ""
    constraints: str = ""
    decisions: list[str] = []
    classification: str = ""   # 3.4: level of the card (default: project)


class ChecklistItem(BaseModel):
    text: str
    done: bool = False


class CardPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    tag: str | None = None
    bucket: str | None = None
    sort: float | None = None
    priority: int | None = None
    due: str | None = None
    checklist: list[ChecklistItem] | None = None
    archived: int | None = None
    assignee: str | None = None
    # 3.3 Helm : parent_id 0 = remettre à la racine
    parent_id: int | None = None
    kind: str | None = None
    intent: str | None = None
    constraints: str | None = None
    decisions: list[str] | None = None


_HELM_FIELDS = ("parent_id", "kind", "intent", "constraints", "decisions")
_CONTEXT_FIELDS = ("title", "intent", "constraints", "decisions", "kind")


def _helm_fields_allowed(fields: dict) -> None:
    if any(k in fields for k in _HELM_FIELDS) and not features.enabled("helm"):
        raise HTTPException(400, "card hierarchy and context need the Helm feature (SOKKAN_FEATURE_HELM)")


def _helm_note(card_id: int) -> None:
    """The card's context → project memory note `helm-card-<id>` (card:<id>), and its
    parent's (the list of cards under it changed)."""
    if not features.enabled("helm"):
        return
    try:
        c = board.get_card(card_id) or {}
        helm.write_context_note(card_id)
        if c.get("parent_id"):
            helm.write_context_note(c["parent_id"])
    except Exception as e:  # noqa: BLE001 — the note is a copy; the card is the truth
        print(f"[helm] context note of card #{card_id}: {e!r}", file=sys.stderr)


class CardComment(BaseModel):
    body: str


class CardClose(BaseModel):
    resolution: str = ""


class CardReopen(BaseModel):
    bucket: str = "Backlog"
    reason: str = ""


_WEB = {"via": "web"}


@app.get("/api/board")
def board_list(archived: int = 0) -> dict:
    return {"buckets": board.BUCKETS, "cards": board.list_cards(
        include_archived=bool(archived), project=_ctx_project(),
        max_level=classification.ctx_clearance())}


@app.get("/api/board/card/{card_id}")
def board_card_detail(card_id: int) -> dict:
    # 3.4: children above the reader's clearance left out, a classified ancestor untitled
    c = board.card_detail(card_id, max_level=classification.ctx_clearance())
    if not c:
        raise HTTPException(404, "card not found")
    return c


@app.post("/api/board/card")
def board_add(body: CardCreate, u: dict = Depends(require("dev"))) -> dict:
    if not body.title.strip() and not body.description.strip():
        raise HTTPException(400, "title or prompt required")
    extra = {k: v for k, v in body.model_dump().items()
             if k in _HELM_FIELDS and v not in (None, "", [], "task")}
    _helm_fields_allowed(extra)
    level = 2
    if body.classification:
        try:
            level = classification._parse_strict(body.classification)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    try:
        c = board.add_card(body.title, body.description, body.tag, body.bucket,
                           priority=body.priority, due=body.due, user=u["email"], origin=_WEB,
                           project=_ctx_project(), level=level, **extra)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if extra:
        _helm_note(c["id"])
    audit.log(u["email"], "board.card.create", f"card #{c['id']}", c["title"])
    return c


@app.patch("/api/board/card/{card_id}")
def board_patch(card_id: int, body: CardPatch, u: dict = Depends(require("dev"))) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "checklist" in fields:
        fields["checklist"] = [dict(i) for i in body.checklist or []]
    if "assignee" in fields:
        try:
            fields["assignee"] = board.validate_assignee(fields["assignee"], _ctx_project())
        except ValueError as e:
            raise HTTPException(400, str(e))
    _helm_fields_allowed(fields)
    if board.get_card(card_id) is None:
        raise HTTPException(404, "card not found")
    try:
        if "parent_id" in fields:
            board.set_parent(card_id, fields.pop("parent_id") or None, user=u["email"], origin=_WEB)
        c = board.update_card(card_id, user=u["email"], origin=_WEB, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not c:
        raise HTTPException(404, "card not found")
    if any(k in body.model_fields_set for k in _CONTEXT_FIELDS + ("parent_id",)):
        _helm_note(card_id)
    changed = ", ".join(k for k in fields)
    audit.log(u["email"], "board.card.update", f"card #{card_id}", changed)
    return c


@app.post("/api/board/card/{card_id}/comment")
def board_comment(card_id: int, body: CardComment, u: dict = Depends(require("dev"))) -> dict:
    try:
        c = board.add_comment(card_id, body.body, author=u["email"], origin=_WEB)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not c:
        raise HTTPException(404, "card not found")
    audit.log(u["email"], "board.card.comment", f"card #{card_id}", body.body.strip()[:120])
    return c


@app.post("/api/board/card/{card_id}/close")
def board_close(card_id: int, body: CardClose, u: dict = Depends(require("dev"))) -> dict:
    c = board.close_card(card_id, user=u["email"], resolution=body.resolution, origin=_WEB)
    if not c:
        raise HTTPException(404, "card not found")
    audit.log(u["email"], "board.card.close", f"card #{card_id}", body.resolution[:200])
    return c


@app.post("/api/board/card/{card_id}/reopen")
def board_reopen(card_id: int, body: CardReopen, u: dict = Depends(require("dev"))) -> dict:
    try:
        c = board.reopen_card(card_id, user=u["email"], bucket=body.bucket, reason=body.reason,
                              origin=_WEB)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not c:
        raise HTTPException(404, "card not found")
    audit.log(u["email"], "board.card.reopen", f"card #{card_id}", f"→ {body.bucket}")
    return c


@app.delete("/api/board/card/{card_id}")
def board_delete(card_id: int, u: dict = Depends(require("dev"))) -> dict:
    c = board.get_card(card_id)
    board.delete_card(card_id, user=u["email"])
    audit.log(u["email"], "board.card.delete", f"card #{card_id}", (c or {}).get("title", ""))
    return {"ok": True}


@app.post("/api/board/card/{card_id}/spawn")
async def board_spawn(card_id: int, u: dict = Depends(require("dev"))) -> dict:
    """▶ spawn depuis une carte : session SDK pré-seedée avec la description."""
    card = board.get_card(card_id)
    if not card:
        raise HTTPException(404, "card not found")
    context = ""
    if features.enabled("helm") and card.get("parent_id"):
        try:  # 3.3 : intention, contraintes, décisions et liens des cartes mères
            context = helm.spawn_context(card_id)
        except Exception as e:  # noqa: BLE001 — a session always starts
            print(f"[helm] context of card #{card_id}: {e!r}", file=sys.stderr)
    s = _spawn_sdk(card["tag"], prompt=card["description"] or (card["title"] if context else ""),
                   title=card["title"], user=u["email"],
                   project=card.get("project") or projects.DEFAULT_PROJECT, context=context)
    board.update_card(card_id, user=u["email"], origin={**_WEB, "session_id": s["session_id"],
                                                         "session_tag": s.get("tag", "")},
                      session_id=s["session_id"], window="", bucket="Doing")
    audit.log(u["email"], "board.card.spawn", f"card #{card_id}", s["title"])
    return {**s, "card_id": card_id}


@app.get("/api/usage")
def usage_summary(days: int = 30, _u: dict = Depends(require("viewer"))) -> dict:
    """Coûts & tokens agrégés depuis les transcripts (onglet Coûts).
    3.2 : la liste des sessions ne montre que celles du projet sélectionné. Lot 4
    (`project_vault_budgets`) : totaux, série quotidienne et répartition par modèle du
    projet aussi, + son budget (`project_budget`) ; sans la feature, totaux d'instance."""
    p = _ctx_project()
    per_project = features.enabled("project_vault_budgets")
    out = usage_mod.summary(days_back=min(days, 90), **({"project": p} if per_project else {}))
    if per_project:
        out["project_budget"] = budgets.status(p)
        return out
    mine = {}
    for s in board.list_sessions():
        if _in_ctx(s):
            mine[s["session_id"]] = s
            if s.get("claude_session_id"):
                mine[s["claude_session_id"]] = s
    # 3.2.3: a transcript SOKKAN did not start (`external`) is listed only when the
    # operator counts them (SOKKAN_USAGE_EXTERNAL=include) and only in the default project
    keep_external = (out.get("include_external") and not projects.multi_project()
                     and p == projects.DEFAULT_PROJECT)
    out["sessions"] = [x for x in out.get("sessions") or []
                       if x["session_id"] in mine
                       or (keep_external and x.get("source") == "external")]
    return out


class BudgetIn(BaseModel):
    currency: str | None = None   # USD | CHF
    day: float | None = None      # 0 = no daily ceiling
    month: float | None = None    # 0 = no monthly ceiling


@app.get("/api/budgets")
def project_budget(_u: dict = Depends(require("viewer"))) -> dict:
    """Budget of the selected project (3.2 lot 4): ceilings, spend, state."""
    if not features.enabled("project_vault_budgets"):
        raise HTTPException(404, "per-project budgets are off (feature project_vault_budgets)")
    return budgets.status(_ctx_project(), fresh=True)


@app.put("/api/budgets")
def project_budget_set(body: BudgetIn, u: dict = Depends(require("admin"))) -> dict:
    """A project admin (or maintainer) sets their project's ceilings."""
    if not features.enabled("project_vault_budgets"):
        raise HTTPException(404, "per-project budgets are off (feature project_vault_budgets)")
    p = _ctx_project()
    try:
        b = budgets.set_budget(p, day=body.day, month=body.month, currency=body.currency,
                               by=u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "project.budget", p, f"day={b['day']} month={b['month']} {b['currency']}")
    return budgets.status(p, fresh=True)


# --- 3.2 ui features: shared_review, byok_admin, connect_ai (routes in uiroutes.py) ------
import uiroutes  # noqa: E402
import secrets_provider  # noqa: E402
from secrets_provider import routes as secrets_routes  # noqa: E402

app.include_router(uiroutes.router)
# 3.3: Setup › Secrets (provider state, « Test connection »)
app.include_router(secrets_routes.router)


@app.exception_handler(secrets_provider.SecretsError)
async def _secrets_unavailable(_request: Request, exc: secrets_provider.SecretsError):
    """The secrets provider cannot serve (OpenBao sealed/unreachable, keys not migrated):
    503 with the reason — never a value, never a token."""
    return JSONResponse({"detail": f"secrets provider unavailable: {exc}"}, status_code=503)

# --- 3.3 Helm : routes /api/helm/* (+ modèles d'agents) — backend/helm_api.py ----------
helm_api.install(app, current_user, require)


# --- 3.2.2 public demo « Captains » : Setup › Organization read-only, fictional people only
@app.get("/api/demo/organization")
def demo_organization(_u: dict = Depends(require("viewer"))) -> dict:
    if not demo_captains.enabled():
        raise HTTPException(404, "feature disabled on this instance")
    return demo_captains.org_view()
