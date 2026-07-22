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
import os
import re
import secrets
from contextlib import asynccontextmanager
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import jwt

# logique de recherche RAG partagée avec le serveur MCP (une seule source de ranking)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
import index_memory  # noqa: E402
import memory_search_server as mem  # noqa: E402

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import audit
import auth
import board
import cfaccess  # noqa: F401 — utilisé via auth.py (mode cf-access)
import iam
import infra
import oidc
import agentchat
import session as sess
import termproxy
import memorykb
import edge
import notify
import observability
import fleet
import fleetterm
import instance
import llm
import panestate
import preview
import previewenv
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

REINDEX_S = float(os.environ.get("SOKKAN_REINDEX_S", "120"))


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
    threading.Thread(target=_reindex_loop, daemon=True, name="sokkan-reindex").start()
    fleet.start_sync()  # managé : maintient `<name>.fleet` dans /etc/hosts (no-op sinon)
    updatecheck.start()  # 1 GET/jour sur dist/VERSION — opt-out SOKKAN_UPDATE_CHECK=0
    yield


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


def _feature(env_var: str):
    """Server-side feature flag: the route 404s when the feature is disabled.
    /api/features is only a UI hint — enforcement happens here."""
    def dep() -> None:
        if os.environ.get(env_var, "1") == "0":
            raise HTTPException(404, "feature disabled on this instance")
    return dep


feature_preview = _feature("SOKKAN_FEATURE_PREVIEW")
feature_tmux = _feature("SOKKAN_FEATURE_TMUX")

# référence forte sur les tâches fire-and-forget (asyncio ne garde qu'une weakref :
# sans ça, un tour d'agent peut être garbage-collecté en plein vol)
_bg_tasks: set[asyncio.Task] = set()


def _bg(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {**user, "source": auth.MODE}


@app.get("/api/auth/info")
def auth_info() -> dict:
    return auth.auth_info()


@app.get("/api/instance")
def instance_info(_u: dict = Depends(current_user)) -> dict:
    return {**instance.info(), "update": updatecheck.state()}


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
    """État de la stack obs (Prom/Grafana/Loki) + fil d'incidents."""
    return {**observability.status(), "incidents": observability.incidents(30)}


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
    for a in alerts:
        labels = a.get("labels", {}) if isinstance(a, dict) else {}
        ann = a.get("annotations", {}) if isinstance(a, dict) else {}
        title = labels.get("alertname") or ann.get("summary") or "Production alert"
        severity = labels.get("severity", "warning")
        summary = ann.get("description") or ann.get("summary") or a.get("valueString", "")
        if a.get("status") == "resolved":
            continue  # on ne spawn que sur firing
        rid = observability.record_incident(title, summary, severity)
        prompt = (
            f"A production alert just fired: **{title}** (severity: {severity}).\n"
            f"{summary}\n"
            f"Labels: {labels}\n\n"
            "You are the on-call engineer. First search the project memory for anything "
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
    return {"ok": True, "spawned": spawned}


@app.get("/api/edge/ask")
def edge_ask(domain: str = ""):
    """Gate d'émission de certificat du caddy edge (on_demand_tls `ask`) :
    200 si le hostname est une route custom enregistrée, 404 sinon. Sans auth
    (appelé par caddy) — ne divulgue rien : booléen sur un hostname public."""
    if not edge.allowed(domain):
        raise HTTPException(404, "unknown host")
    return {"ok": True}


class OrgName(BaseModel):
    org_name: str


@app.post("/api/instance")
def instance_set(body: OrgName, u: dict = Depends(require("admin"))) -> dict:
    r = instance.set_org_name(body.org_name)
    audit.log(u["email"], "instance.rename", body.org_name)
    return r


@app.get("/api/llm")
def llm_status(_u: dict = Depends(current_user)) -> dict:
    """Config LLM de l'instance (mode + modèle) — jamais la clé."""
    return llm.status()


class LlmConfig(BaseModel):
    mode: str  # 'byok'
    anthropic_api_key: str = ""
    claude_oauth_token: str = ""  # abonnement Claude Pro/Max (`claude setup-token`)


@app.get("/api/llm/usage")
def llm_usage(_u: dict = Depends(current_user)):
    """Usage/quota du jour (mode inférence incluse) ; null en BYOK."""
    return llm.usage()


@app.post("/api/llm")
def llm_set(body: LlmConfig, u: dict = Depends(require("admin"))) -> dict:
    """Règle la clé LLM de l'instance (admin). La clé/le token reste sur CETTE VM,
    jamais transmis à NINABOT. Une instance en « inférence incluse » est opérée
    par NINABOT → on n'autorise pas de la basculer en BYOK depuis le cockpit."""
    if llm.status().get("operator_managed"):
        raise HTTPException(403, "this instance uses managed inference (operated by NINABOT)")
    if body.mode != "byok":
        raise HTTPException(400, "mode must be 'byok'")
    if body.anthropic_api_key.strip():
        llm.save({"mode": "byok", "anthropic_api_key": body.anthropic_api_key.strip()})
    elif body.claude_oauth_token.strip():
        llm.save({"mode": "byok", "claude_oauth_token": body.claude_oauth_token.strip()})
    else:
        raise HTTPException(400, "anthropic_api_key or claude_oauth_token required")
    audit.log(u["email"], "llm.config", f"byok:{llm.status().get('byok_kind')}")
    return llm.status()


@app.get("/api/features")
def features() -> dict:
    """Onglets/capacités actifs sur cette instance — le front masque le reste."""
    return {
        # l'onglet Infra existe dès qu'il a quelque chose à montrer : topologie
        # (Prometheus) et/ou flotte managée (SOKKAN_FLEET_*, VMs clients).
        "infra": infra.ENABLED or fleet.ENABLED,
        "infra_topo": infra.ENABLED,
        "fleet": fleet.ENABLED,
        # onglet Operate : dès qu'une stack d'observabilité est branchée
        "observe": observability.ENABLED,
        "preview": os.environ.get("SOKKAN_FEATURE_PREVIEW", "1") != "0",
        "tmux": os.environ.get("SOKKAN_FEATURE_TMUX", "1") != "0",
    }


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
    if os.environ.get("SOKKAN_FEATURE_TMUX", "1") == "0" or not _origin_ok(websocket):
        await websocket.close(code=4403)
        return
    await termproxy.ws(websocket, "ws")


@app.api_route("/term", methods=["GET"])
@app.api_route("/term/{path:path}", methods=["GET", "POST"])
async def term_http(request: Request, path: str = "", _f: None = Depends(feature_tmux)):
    return await termproxy.http(request, path)


# --- Chantier B : chat interactif piloté par le Claude Agent SDK -------------
def _ws_user(websocket: WebSocket) -> dict | None:
    """Identité d'une connexion WS (auth.current_user est duck-typé : WS a
    .headers et .cookies comme Request). None si non authentifié / rôle insuffisant."""
    try:
        user = auth.current_user(websocket)  # type: ignore[arg-type]
    except HTTPException:
        return None
    if iam.rank(user["role"]) < iam.rank("dev"):
        return None
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
    await websocket.accept()
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
    try:
        while True:
            msg = await websocket.receive_json()
            t = msg.get("type")
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


# --- flow OIDC (login → Authentik → callback → session cookie) ---
PUBLIC_URL = os.environ.get("SOKKAN_PUBLIC_URL", "http://localhost:3009").rstrip("/")
_REDIRECT = f"{PUBLIC_URL}/api/auth/callback"


@app.get("/api/auth/login")
def auth_oidc_login():
    if not oidc.ENABLED:
        raise HTTPException(501, "OIDC not configured")
    verifier, challenge = oidc.new_pkce()
    state = secrets.token_urlsafe(16)
    url = oidc.authorize_url(_REDIRECT, state, challenge)
    tx = jwt.encode({"s": state, "v": verifier, "exp": int(time.time()) + 600},
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
    if not email:
        raise HTTPException(401, "OIDC token has no email")
    resp = RedirectResponse(f"{PUBLIC_URL}/", status_code=302)
    resp.set_cookie(sess.COOKIE, sess.make(email, claims.get("name", "")),
                    max_age=sess.TTL, httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("sokkan_oidc_tx")
    return resp


@app.get("/api/auth/logout")
def auth_oidc_logout():
    resp = RedirectResponse(f"{PUBLIC_URL}/", status_code=302)
    resp.delete_cookie(sess.COOKIE)
    return resp


_AUTH_FREE = ("/api/auth/", "/api/health", "/api/edge/ask", "/api/observability/alert")


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Gate global : toute route /api exige une identité résolue (sauf /api/auth/* + health).
    Indispensable hors CF Access (mode oidc) : sinon les lectures seraient publiques."""
    p = request.url.path
    if p.startswith("/api/") and not p.startswith(_AUTH_FREE):
        try:
            auth.current_user(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


def _transcripts() -> list[Path]:
    return sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


@app.get("/api/health")
def health() -> dict:
    files = list(PROJECT_DIR.glob("*.jsonl"))
    return {"ok": True, "project_dir": str(PROJECT_DIR), "transcripts": len(files)}


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
    for s in board.list_sessions():
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


def _spawn_sdk(tag: str, prompt: str = "", title: str = "", user: str = "") -> dict:
    """Session SDK : enregistrée dans le store + AgentSession créée ; le seed
    (sujet + consigne memory_search + HITL) part en tâche de fond — les events
    sont bufferisés et rejoués quand le pane se connecte."""
    sid = agentchat.new_sid()
    s = board.add_sdk_session(sid, tag, title=title, prompt=prompt)
    session = agentchat.get_or_create(sid, user=user)
    if prompt.strip():
        _bg(session.handle_user(board.seed_text(prompt)))
    return s


@app.post("/api/spawn")
async def spawn_session(body: SpawnBody, u: dict = Depends(require("dev"))) -> dict:
    """Crée une session SOKKAN — chat SDK par défaut, fenêtre tmux si kind='tmux'."""
    if body.kind == "tmux":
        s = board.spawn(body.tag, prompt=body.prompt, title=body.title)
    else:
        s = _spawn_sdk(body.tag, prompt=body.prompt, title=body.title, user=u["email"])
    audit.log(u["email"], "session.spawn", s.get("window") or s["session_id"],
              f"{s['title']} ({body.kind})")
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
def audit_recent(limit: int = 200, q: str = "", _u: dict = Depends(require("dev"))) -> list[dict]:
    """Journal des actions (onglet Journal) : qui a fait quoi, quand."""
    return audit.recent(limit=limit, q=q)


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


@app.get("/api/memory/stats")
def memory_stats() -> dict:
    return memorykb.stats()


@app.get("/api/memory/notes")
def memory_notes() -> list[dict]:
    return memorykb.list_notes()


@app.get("/api/memory/search")
def memory_search(q: str, k: int = 8) -> list[dict]:
    return mem.memory_search(q, k)


@app.get("/api/memory/note/{name}")
def memory_note(name: str) -> dict:
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    return {"name": name, "body": mem.memory_get(name)}


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


@app.get("/api/board")
def board_list(archived: int = 0) -> dict:
    return {"buckets": board.BUCKETS, "cards": board.list_cards(include_archived=bool(archived))}


@app.get("/api/board/card/{card_id}")
def board_card_detail(card_id: int) -> dict:
    c = board.get_card(card_id)
    if not c:
        raise HTTPException(404, "card not found")
    return {**c, "events": board.card_events(card_id)}


@app.post("/api/board/card")
def board_add(body: CardCreate, u: dict = Depends(require("dev"))) -> dict:
    if not body.title.strip() and not body.description.strip():
        raise HTTPException(400, "title or prompt required")
    c = board.add_card(body.title, body.description, body.tag, body.bucket,
                       priority=body.priority, due=body.due, user=u["email"])
    audit.log(u["email"], "board.card.create", f"carte #{c['id']}", c["title"])
    return c


@app.patch("/api/board/card/{card_id}")
def board_patch(card_id: int, body: CardPatch, u: dict = Depends(require("dev"))) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "checklist" in fields:
        fields["checklist"] = [dict(i) for i in body.checklist or []]
    c = board.update_card(card_id, user=u["email"], **fields)
    if not c:
        raise HTTPException(404, "card not found")
    changed = ", ".join(k for k in fields)
    audit.log(u["email"], "board.card.update", f"carte #{card_id}", changed)
    return c


@app.delete("/api/board/card/{card_id}")
def board_delete(card_id: int, u: dict = Depends(require("dev"))) -> dict:
    c = board.get_card(card_id)
    board.delete_card(card_id, user=u["email"])
    audit.log(u["email"], "board.card.delete", f"carte #{card_id}", (c or {}).get("title", ""))
    return {"ok": True}


@app.post("/api/board/card/{card_id}/spawn")
async def board_spawn(card_id: int, u: dict = Depends(require("dev"))) -> dict:
    """▶ spawn depuis une carte : session SDK pré-seedée avec la description."""
    card = board.get_card(card_id)
    if not card:
        raise HTTPException(404, "card not found")
    s = _spawn_sdk(card["tag"], prompt=card["description"], title=card["title"], user=u["email"])
    board.update_card(card_id, user=u["email"], session_id=s["session_id"],
                      window="", bucket="Doing")
    audit.log(u["email"], "board.card.spawn", f"carte #{card_id}", s["title"])
    return {**s, "card_id": card_id}


@app.get("/api/usage")
def usage_summary(days: int = 30, _u: dict = Depends(require("dev"))) -> dict:
    """Coûts & tokens agrégés depuis les transcripts (onglet Coûts)."""
    return usage_mod.summary(days_back=min(days, 90))
