#!/usr/bin/env python3
"""assistant.py — Nina, l'agente DevOps embarquée (S1).

Chat d'assistance produit dans le cockpit : persona DevOps qui connaît SOKKAN
par cœur (corpus `assistant_kb/*.md`, injecté ENTIER dans le prompt système —
à cette taille le RAG serait de l'overhead ; on indexera quand la KB grossira).

Frontière (spec docs/sokkan/spec-agente-devops.md, décision Nick 23-07) :
enforcement STRUCTUREL — ce module n'a aucun accès au workspace, aux fichiers,
à l'env des sessions ni au control plane. Ce qu'il ne reçoit pas, il ne peut
pas le divulguer.

S2 (2026-09-10) ajoute le DOSSIER CLIENT et la MÉMOIRE PROJET au prompt, via
des accesseurs curés en LECTURE SEULE (fleet.view / llm.status+usage /
usage.summary / memory_search). Deux garde-fous structurels :
- `_dossier()` recopie une **allowlist** de champs, jamais le dict brut : l'URI
  de connexion d'une DBaaS ne peut pas fuir même si le portail l'envoie ;
- la mémoire est **pré-récupérée** et injectée (pas d'outil à appeler), même
  doctrine que le recall déterministe au spawn de la 2.0 — la garantie est
  mécanique, elle ne dépend pas du bon vouloir du modèle.

LLM : SOKKAN_ASSISTANT_LLM_{URL,TOKEN,MODEL} si posés (service NINABOT, seedé
au provisioning cloud — le wallet du client n'est jamais débité), sinon
fallback sur la config LLM de l'instance (llm.py) pour le dev/test.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

import llm

KB_DIR = Path(__file__).parent / "assistant_kb"
DB = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))) / "assistant.db"
DAILY_LIMIT = int(os.environ.get("SOKKAN_ASSISTANT_DAILY_LIMIT", "50"))
MAX_TOKENS = 800
HISTORY_TURNS = 12  # messages (user+assistant confondus) rejoués au modèle
KB_CAP = 40_000     # garde-fou : au-delà on tronque (et il sera temps d'indexer)

PERSONA = """Tu es Nina, l'ingénieure DevOps embarquée dans le cockpit SOKKAN Cloud.
Tu connais le produit par cœur (la base de connaissance ci-dessous) et tu aides
l'utilisateur à réussir : importer son projet, semer sa mémoire, piloter ses
sessions, gérer sa flotte et ses coûts.

Tu disposes plus bas de trois blocs : la base de connaissance produit, le
DOSSIER CLIENT (l'état réel de cette instance : plan, flotte, conso, crédits,
catalogue et prix) et des EXTRAITS DE MÉMOIRE PROJET pertinents pour la
question posée. Appuie-toi dessus : cite les vrais chiffres, les vrais noms de
machines, le vrai solde. Si le dossier ne contient pas l'information, dis-le et
indique l'écran — n'invente jamais un chiffre.

Règles absolues :
- Tu ne lis, ne cites et ne devines JAMAIS un secret : credentials, URI de
  connexion, variables d'environnement, clés. Si on te le demande, indique
  l'écran du cockpit où l'information se gère (ex. l'URI PostgreSQL se révèle
  dans Ma flotte, réservé admin).
- Tu n'as pas accès au code du client ni à ses machines — tu ne prétends
  jamais le contraire.
- Tu n'exécutes aucune action : tu expliques, tu guides, tu montres où cliquer.
- Ce que tu ne sais pas, tu ne l'inventes pas : tu proposes d'écrire à
  hello@sokkan.ch (le fondateur répond).
- Tu réponds dans la langue de l'utilisateur (FR/EN), ton direct et technique,
  chaleureux sans blabla. Réponses courtes d'abord, détail si on te le demande.
"""


# ---- base ----------------------------------------------------------------
def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT NOT NULL, ts REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_email, ts)")
    return con


def history(user_email: str, limit: int = 50) -> list[dict]:
    con = _con()
    rows = con.execute(
        "SELECT role, content, ts FROM messages WHERE user_email=? ORDER BY ts DESC LIMIT ?",
        (user_email, limit)).fetchall()
    con.close()
    return [{"role": r, "content": c, "ts": t} for r, c, t in reversed(rows)]


def _today_count(con: sqlite3.Connection, user_email: str) -> int:
    midnight = time.time() - (time.time() % 86400)
    return con.execute(
        "SELECT COUNT(*) FROM messages WHERE user_email=? AND role='user' AND ts>=?",
        (user_email, midnight)).fetchone()[0]


# ---- KB (corpus produit, cache par mtime) ---------------------------------
_kb_cache: tuple[float, str] | None = None


def _kb() -> str:
    global _kb_cache
    if not KB_DIR.is_dir():
        return ""
    mtime = max((p.stat().st_mtime for p in KB_DIR.glob("*.md")), default=0.0)
    if _kb_cache and _kb_cache[0] == mtime:
        return _kb_cache[1]
    parts = [p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(KB_DIR.glob("*.md"))]
    text = "\n\n---\n\n".join(parts)[:KB_CAP]
    _kb_cache = (mtime, text)
    return text


# ---- LLM -----------------------------------------------------------------
def _llm_config() -> dict | None:
    """Endpoint Anthropic-compatible : assistant dédié (NINABOT) sinon config
    instance. Retourne {url, token, model} ou None si rien de configurable."""
    url = os.environ.get("SOKKAN_ASSISTANT_LLM_URL", "")
    tok = os.environ.get("SOKKAN_ASSISTANT_LLM_TOKEN", "")
    if url and tok:
        # `api` : "anthropic" (défaut — la passerelle managée parle Messages) ou
        # "openai" pour un endpoint /chat/completions (Ollama, vLLM, LiteLLM…),
        # ce qui permet de faire tourner Nina sur du silicium maison.
        return {"url": url.rstrip("/"), "token": tok,
                "api": os.environ.get("SOKKAN_ASSISTANT_LLM_API", "anthropic").lower(),
                "model": os.environ.get("SOKKAN_ASSISTANT_LLM_MODEL", "qwen3-coder-plus")}
    c = llm.load()
    if c.get("mode") == "included" and c.get("base_url") and c.get("auth_token"):
        return {"url": c["base_url"].rstrip("/"), "token": c["auth_token"],
                "api": "anthropic", "model": c.get("model") or "qwen3-coder-plus"}
    if c.get("mode") == "byok" and c.get("anthropic_api_key"):
        return {"url": "https://api.anthropic.com", "token": c["anthropic_api_key"],
                "api": "anthropic", "model": "claude-haiku-4-5-20251001"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"url": "https://api.anthropic.com", "token": os.environ["ANTHROPIC_API_KEY"],
                "api": "anthropic", "model": "claude-haiku-4-5-20251001"}
    return None


def _fallback_config() -> dict | None:
    """Endpoint de repli (`SOKKAN_ASSISTANT_LLM_FALLBACK_*`). Sert à mettre Nina
    sur du silicium maison SANS la rendre indisponible quand il ne tourne pas :
    primaire = XPU local, repli = passerelle managée."""
    url = os.environ.get("SOKKAN_ASSISTANT_LLM_FALLBACK_URL", "")
    tok = os.environ.get("SOKKAN_ASSISTANT_LLM_FALLBACK_TOKEN", "")
    if not (url and tok):
        return None
    return {"url": url.rstrip("/"), "token": tok,
            "api": os.environ.get("SOKKAN_ASSISTANT_LLM_FALLBACK_API", "anthropic").lower(),
            "model": os.environ.get("SOKKAN_ASSISTANT_LLM_FALLBACK_MODEL", "sokkan-ship")}


# Un primaire injoignable ne doit pas coûter son timeout à CHAQUE message :
# on le met au coin pendant PRIMARY_RETRY_S avant de retenter.
PRIMARY_RETRY_S = 120
_primary_down_until = 0.0


def _ask(cfg: dict, system: str, msgs: list[dict], user_email: str) -> str:
    """Un aller-retour modèle. Deux dialectes, une seule sortie texte."""
    hdr_user = {"x-sokkan-user": f"assistant:{user_email}"}
    if cfg.get("api") == "openai":
        r = httpx.post(
            f"{cfg['url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['token']}", **hdr_user},
            json={"model": cfg["model"], "max_tokens": MAX_TOKENS,
                  "messages": [{"role": "system", "content": system}, *msgs]},
            timeout=120,  # un modèle local sur GPU maison est plus lent qu'une API
        )
        r.raise_for_status()
        m = (r.json().get("choices") or [{}])[0].get("message") or {}
        # les modèles à raisonnement rendent content=None et tout mettent dans
        # reasoning_content (piège déjà vu sur la passerelle d'inférence)
        return (m.get("content") or m.get("reasoning_content") or "").strip()
    r = httpx.post(
        f"{cfg['url']}/v1/messages",
        headers={"x-api-key": cfg["token"], "anthropic-version": "2023-06-01", **hdr_user},
        json={"model": cfg["model"], "max_tokens": MAX_TOKENS,
              "system": system, "messages": msgs},
        timeout=60,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", [])
                   if b.get("type") == "text").strip()


def configured() -> bool:
    return (_llm_config() or _fallback_config()) is not None and bool(_kb())


def _ask_with_fallback(cfg: dict, fb: dict | None, system: str, msgs: list[dict],
                       user_email: str) -> tuple[str, str]:
    """Primaire d'abord, repli si le primaire ne répond pas. Pensé pour mettre
    Nina sur du silicium maison (vLLM-XPU) sans la rendre indisponible quand
    celui-ci ne tourne pas : « quand dispo » est une bascule, pas un pari.
    Retourne (réponse, backend qui a répondu)."""
    global _primary_down_until
    if fb and time.time() < _primary_down_until:
        return _ask(fb, system, msgs, user_email), "fallback"
    try:
        out = _ask(cfg, system, msgs, user_email)
        _primary_down_until = 0.0
        return out, "primary"
    except (httpx.HTTPError, ValueError) as e:
        if not fb:
            raise
        _primary_down_until = time.time() + PRIMARY_RETRY_S
        print(f"[assistant] primaire KO ({e}) → repli pour {PRIMARY_RETRY_S}s")
        return _ask(fb, system, msgs, user_email), "fallback"


# ---- dossier client (S2) — accesseurs curés, LECTURE SEULE ---------------
_DOSSIER_TTL = 60.0
_dossier_cache: tuple[float, str] = (0.0, "")


def _fleet_lines() -> list[str]:
    """Ressources + catalogue, par ALLOWLIST de champs. L'URI d'une DBaaS et
    tout autre secret que le portail enverrait ne sont jamais recopiés."""
    import fleet
    if not fleet.ENABLED:
        return []
    v = fleet.view()
    out = [f"plan courant : {v.get('plan') or '—'}"]
    res = v.get("resources") or []
    if res:
        out.append("ressources de la flotte :")
        for r in res:
            if r.get("status") == "destroyed":
                continue
            bits = [f"  - {r.get('name') or r.get('sku')} ({r.get('sku')}) — {r.get('status')}"]
            if r.get("fleet_host"):
                bits.append(f", joignable en `{r['fleet_host']}`")
            if r.get("private_ip"):
                bits.append(f" ({r['private_ip']})")
            out.append("".join(bits))
    else:
        out.append("ressources de la flotte : aucune pour l'instant")
    cat = [c for c in (v.get("catalog") or []) if c.get("price_chf")]
    if cat:
        out.append("catalogue commandable (prix mensuel CHF) :")
        out += [f"  - {c['sku']} · {c['label']} · {c['price_chf']} CHF" for c in cat]
    routes = v.get("routes") or []
    if routes:
        out.append("services exposés sur le web : "
                   + ", ".join(str(r.get("hostname")) for r in routes))
    return out


def _inference_lines() -> list[str]:
    out = []
    st = llm.status()
    tier = st.get("model") or "—"
    out.append(f"inférence : mode {st.get('mode')}, modèle/tier {tier}")
    u = llm.usage()
    if u:
        bal = u.get("balance_centimes")
        if bal is not None:
            out.append(f"solde de crédits : {bal / 100:.2f} CHF")
        if u.get("spent_month_centimes") is not None:
            out.append(f"dépensé — aujourd'hui {u.get('spent_today_centimes', 0) / 100:.2f} CHF, "
                       f"ce mois {u['spent_month_centimes'] / 100:.2f} CHF")
        if u.get("used_today") is not None:
            out.append(f"tokens d'inférence — aujourd'hui {u['used_today']}, "
                       f"ce mois {u.get('used_month', '?')}")
        for tier in (u.get("coding_tiers_chf_per_mtok") or []):
            out.append(f"  tarif {tier.get('id')} : {tier.get('chf_per_mtok_in')} CHF/Mtok en entrée, "
                       f"{tier.get('chf_per_mtok_out')} en sortie")
    return out


def _sessions_lines() -> list[str]:
    import usage
    tot = (usage.summary(days_back=30) or {}).get("totals") or {}
    def _fmt(k: str, label: str) -> str | None:
        d = tot.get(k)
        if not d:
            return None
        return (f"  - {label} : {d.get('turns', 0)} tours, "
                f"{d.get('out_tokens', 0)} tokens produits, ~{d.get('cost', 0):.2f} USD estimés")
    lines = [x for x in (_fmt("today", "aujourd'hui"), _fmt("7d", "7 derniers jours"),
                         _fmt("30d", "30 derniers jours")) if x]
    return ["consommation des sessions d'agent :", *lines] if lines else []


def _dossier() -> str:
    """État réel de l'instance, en texte compact. Chaque source est isolée :
    une brique indisponible (portail down, pas de flotte) en retire une ligne,
    elle ne casse pas la conversation."""
    global _dossier_cache
    now = time.time()
    if _dossier_cache[0] > now - _DOSSIER_TTL:
        return _dossier_cache[1]
    lines: list[str] = [
        f"version de l'instance : {os.environ.get('SOKKAN_VERSION', 'dev')}",
        f"offre : {os.environ.get('SOKKAN_TIER') or 'self-hosted'}",
    ]
    for source in (_fleet_lines, _inference_lines, _sessions_lines):
        try:
            lines += source()
        except Exception as e:  # noqa: BLE001 — un accesseur muet vaut mieux qu'un chat mort
            print(f"[assistant] dossier: {source.__name__} indisponible ({e})")
    text = "\n".join(lines)
    _dossier_cache = (now, text)
    return text


# Mots-outils français : présents dans presque toute phrase FR, quasi absents
# d'une phrase EN. Assez pour trancher une question de chat ; volontairement
# pas une lib de détection de langue pour trois lignes de prompt.
_FR_HINTS = re.compile(
    r"(?i)\b(je|tu|il|elle|nous|vous|le|la|les|un|une|des|du|de|mon|ma|mes|ton|ta|tes|"
    r"est|sont|c.est|qu.est|quel|quelle|comment|pourquoi|combien|où|avec|dans|pour|sur|"
    r"pas|plus|pourrais|peux|dois|pourquoi|ça|pourrait)\b")


def _language_directive(message: str) -> str:
    """Dire explicitement au modèle dans quelle langue répondre.

    La persona le demande déjà (« la langue de l'utilisateur »), mais noyée dans
    ~3 100 tokens de contexte, les modèles ouverts locaux l'ignorent une fois sur
    deux — mesuré le 11.09 sur gpt-oss-20b ET qwen3-next-80b, qui répondaient en
    français à une question anglaise. Même doctrine que le reste de S2 : ce qui
    peut être décidé en Python ne se délègue pas au bon vouloir du modèle.
    Message trop court ou ambigu → pas de directive, on laisse la persona faire.
    """
    words = re.findall(r"[A-Za-zÀ-ÿ']+", message)
    if len(words) < 3:
        return ""
    fr = len(_FR_HINTS.findall(message))
    if fr >= 2:
        return "\n\nRÉPONDS EN FRANÇAIS."
    if fr == 0:
        return "\n\nREPLY IN ENGLISH."
    return ""


def _memory_context(query: str, top_k: int = 4) -> str:
    """Extraits de la mémoire projet pertinents pour la question. Pré-récupérés
    (pas d'outil à appeler) — même doctrine que le recall au spawn."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
        import memory_search_server as mem
        hits = mem.memory_search(query, top_k) or []
    except Exception as e:  # noqa: BLE001
        print(f"[assistant] mémoire indisponible ({e})")
        return ""
    out = []
    for h in hits:
        name = h.get("note_name")
        if not name:  # {"info": …} quand la mémoire est vide, {"error": …} si l'index est KO
            continue
        excerpt = (h.get("snippet") or "").strip().replace("\n", " ")
        out.append(f"[[{name}]] — {h.get('description') or ''}\n{excerpt[:600]}")
    return "\n\n".join(out)


def chat(user_email: str, message: str) -> dict:
    """Un tour de chat. Retourne {reply} ou lève ValueError (limite/config)."""
    message = (message or "").strip()
    if not message:
        raise ValueError("message vide")
    if len(message) > 4000:
        raise ValueError("message trop long (4000 caractères max)")
    cfg, fb = _llm_config(), _fallback_config()
    if not cfg and fb:  # primaire absent : le repli devient le chemin normal
        cfg, fb = fb, None
    if not cfg:
        raise ValueError("assistant non configuré sur cette instance")

    con = _con()
    if _today_count(con, user_email) >= DAILY_LIMIT:
        con.close()
        raise ValueError("limite quotidienne atteinte — réessayez demain, "
                         "ou écrivez à hello@sokkan.ch")

    past = history(user_email, HISTORY_TURNS)
    msgs = [{"role": m["role"], "content": m["content"]} for m in past]
    msgs.append({"role": "user", "content": message})
    system = f"{PERSONA}\n\n=== BASE DE CONNAISSANCE PRODUIT ===\n\n{_kb()}"
    dossier = _dossier()
    if dossier:
        system += f"\n\n=== DOSSIER CLIENT (état réel, lecture seule) ===\n\n{dossier}"
    notes = _memory_context(message)
    if notes:
        system += ("\n\n=== EXTRAITS DE MÉMOIRE PROJET (pertinents pour la question) ==="
                   f"\n\n{notes}")
    # en dernier : ce qui est en fin de prompt système pèse le plus
    system += _language_directive(message)

    reply, via = _ask_with_fallback(cfg, fb, system, msgs, user_email)
    reply = reply or "(réponse vide)"

    now = time.time()
    con.execute("INSERT INTO messages(user_email, role, content, ts) VALUES(?,?,?,?)",
                (user_email, "user", message, now))
    con.execute("INSERT INTO messages(user_email, role, content, ts) VALUES(?,?,?,?)",
                (user_email, "assistant", reply, now + 0.001))
    con.commit()
    con.close()
    return {"reply": reply, "via": via}
