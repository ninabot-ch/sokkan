#!/usr/bin/env python3
"""claudetier.py — « Write with Claude » on an appliance (feature `claude_tier`).

On an on-prem box every session runs on the model of the machine (backend/shiprouter.py).
Some work is worth a stronger model and carries nothing of the organisation — the text of a
course, a public page. For that work a person may ask, EXPLICITLY, at the moment they send
it, for Claude (Anthropic API, with the organisation's own key). Nothing else ever goes
there: not a keyword, not the size of a prompt, not a failure of the local model.

Who decides what, and where:

* **The gesture** — a work started with « Write with Claude » (simple profile) is stored with
  ``tier = claude`` and the chosen model (`simple.works`). Its session gets three headers on
  every inference call (`session_env`): ``x-sokkan-tier: claude``, ``x-sokkan-project`` and
  the model in ``ANTHROPIC_MODEL`` (so the CLI shapes its requests for that model).
* **The rule** — by the CLASSIFICATION OF THE PROJECT, not by the task: Claude is allowed in
  a project classified at most ``SOKKAN_CLAUDE_MAX_LEVEL`` (default ``team``: public and
  team/internal projects). ``project`` — the default level of every project nobody
  classified — stays on the box, so turning the feature on opens nothing by itself.
  ``confidential`` and ``restricted`` are refused whatever the variable says (`HARD_MAX`).
* **The instance decides** (`gate`) before the work starts: no key, project too high, model
  outside the list → the choice is greyed out with the reason, and a start is refused (409).
* **The router checks again** at every request: it asks the api
  (``GET /api/internal/claude-grant``, shared secret ``SOKKAN_ROUTER_SECRET``, never through
  the reverse proxy) for the decision AND the key of that project. A project reclassified in
  the middle of a work is refused at its next request, with a sentence a person can read —
  never a silent fall back to another model.

Why the router gets the key from the api, and not from .env or from the key store:

* the key is the one the administrator typed in the cockpit (`modelkeys`, encrypted with the
  vault key); copying it into .env would make two places to rotate and one in clear;
* reading the store from the router would hand it the data volume AND the vault key — every
  secret of the instance — for one value;
* the api already holds both and already decides the classification: one internal call, the
  key travels on the compose network only, is kept in the router's memory for a few seconds,
  and is never written, logged or returned to a session.

What a person sees: the badge of each answer names the model THE API ANSWERED WITH (the
``model`` of the response, recorded in the transcript), never the one that was asked for.
"""
from __future__ import annotations

import hmac
import os
import re

# the models a person may choose (SOKKAN_CLAUDE_MODELS overrides, comma-separated)
DEFAULT_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_LEVEL = 1          # team: public (0) and team/internal (1) projects
HARD_MAX = 2                   # never above `project`: confidential and restricted are refused
DESTINATION = {"fr": "Anthropic, États-Unis — hors de Suisse et d'Europe",
               "en": "Anthropic, United States — outside Switzerland and Europe"}
HEADER_TIER, HEADER_PROJECT = "x-sokkan-tier", "x-sokkan-project"
GRANT_PATH = "/api/internal/claude-grant"
CODES = ("off", "no_key", "level", "model", "router")

_MODEL_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$")


# ---- configuration ---------------------------------------------------------------------
def enabled() -> bool:
    import features
    return features.enabled("claude_tier")


def appliance() -> bool:
    """The instance runs its sessions through the local router (the only place the tier is
    honoured): an appliance engine, from SOKKAN_SHIP_URL."""
    try:
        import llm
        return llm.load().get("engine") == "appliance"
    except Exception:  # noqa: BLE001
        return False


SECRET_FILE = "/run/sokkan-router/secret"   # written by scripts/api-entrypoint.sh (3.6.6)


def router_secret() -> str:
    """The secret shared with the router: SOKKAN_ROUTER_SECRET when set, else the file the
    api's entrypoint writes in the volume the two share — so that an upgrade by an older
    upgrade.sh (which never wrote the variable) still connects them."""
    v = (os.environ.get("SOKKAN_ROUTER_SECRET") or "").strip()
    if v:
        return v
    try:
        with open(SECRET_FILE, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def models() -> list[str]:
    raw = os.environ.get("SOKKAN_CLAUDE_MODELS")
    out = [m.strip() for m in (raw.split(",") if raw and raw.strip() else DEFAULT_MODELS) if m.strip()]
    return list(dict.fromkeys(m for m in out if m.startswith("claude-")))


def max_level() -> int:
    from core import levels
    raw = os.environ.get("SOKKAN_CLAUDE_MAX_LEVEL")
    lv = levels.parse(raw) if raw and raw.strip() else None
    return min(DEFAULT_MAX_LEVEL if lv is None else lv, HARD_MAX)


def label(model: str | None) -> str:
    """« Claude Opus 5.5 » for claude-opus-5-5; the id itself for anything else."""
    m = _MODEL_RE.match((model or "").strip())
    if not m:
        return (model or "").strip()
    fam, major, minor = m.group(1), m.group(2), m.group(3)
    return f"Claude {fam.capitalize()} {major}{'.' + minor if minor else ''}"


def describe_model(model: str | None, lang: str = "fr") -> dict | None:
    """The badge of an answer, from the model the API answered with. None = nothing to say
    (no model, or the CLI's own synthetic line for an error)."""
    m = (model or "").strip()
    if not m or m.startswith("<"):
        return None
    if m.startswith("claude-"):
        return {"model": m, "label": label(m), "where": "anthropic",
                "place": "Anthropic"}
    ship = (os.environ.get("SOKKAN_SHIP_MODEL") or "").strip()
    alias = (os.environ.get("SOKKAN_SHIP_ALIAS") or "sokkan-ship").strip()
    if m in (ship, alias) or (appliance() and not ship):
        return {"model": m, "label": "Modèle local" if lang == "fr" else "Local model",
                "where": "local", "place": "chez vous" if lang == "fr" else "on your machine"}
    return {"model": m, "label": m, "where": "other", "place": ""}


# ---- the key and the administrator's default ------------------------------------------
def _rec() -> dict:
    import modelkeys
    return modelkeys.record("instance", "anthropic") or {}


def key_present() -> bool:
    return bool(_rec().get("ct"))


def available_models() -> list[str]:
    """The list, filtered on what the key sees (GET /v1/models at its last test) when known."""
    seen = _rec().get("seen_models") or []
    ms = models()
    if seen:
        filtered = [m for m in ms if m in seen]
        return filtered or ms
    return ms


def default_model() -> str:
    av = available_models()
    for cand in (_rec().get("default_model"), os.environ.get("SOKKAN_CLAUDE_MODEL"), DEFAULT_MODEL):
        if cand and cand in av:
            return cand
    return av[0] if av else ""


def set_default_model(model: str) -> str:
    import modelkeys
    if model not in models():
        raise ValueError("model: one of " + ", ".join(models()))
    if not modelkeys.record("instance", "anthropic"):
        raise ValueError("no Anthropic key yet")
    modelkeys._update("instance", "anthropic", default_model=model)
    return model


# ---- the project's classification -----------------------------------------------------
def project_level(project: str) -> int:
    import projects
    p = projects.get(project) or {}
    try:
        return int(p.get("level") if p.get("level") is not None else 2)
    except (TypeError, ValueError):
        return 4            # unreadable = the highest: fail closed


# ---- the decision ---------------------------------------------------------------------
_FR_LEVELS = ("Public", "Équipe (interne)", "Projet", "Confidentiel", "Restreint")


def level_label(rank: int, lang: str = "fr") -> str:
    """The customer's own grid when it has one (CORTHEXIS/SOKKAN_CLASSIFICATION_LABELS), else
    the level in the person's language."""
    from core import levels
    lab = levels.label(rank)
    if lang == "fr" and lab == levels.IDS[levels.clamp(rank)].capitalize():
        return _FR_LEVELS[levels.clamp(rank)]
    return lab


def gate(project: str, model: str = "", lang: str = "fr") -> dict:
    """May a work of ``project`` be written with Claude (``model``, or the default)?
    → {allowed, code, reason, …}. Decided here, checked again by the router."""
    from core import levels
    lvl, cap = project_level(project), max_level()
    av = available_models()
    import projects
    out = {"allowed": False, "code": "", "reason": "", "project": project,
           "project_name": (projects.get(project) or {}).get("name") or project,
           "level": levels.ident(lvl), "level_label": level_label(lvl, lang),
           "max_level": levels.ident(cap), "max_label": level_label(cap, lang),
           "models": [{"id": m, "label": label(m)} for m in av],
           "default_model": default_model(), "destination": DESTINATION.get(lang, DESTINATION["en"])}
    fr = lang == "fr"
    if not enabled() or not appliance():
        out.update(code="off", reason="Claude n'est pas proposé sur cette instance." if fr
                   else "Claude is not offered on this instance.")
    elif not router_secret():
        # said to people: never a variable name (the operator reads the code `router`)
        out.update(code="router", reason="Claude n'est pas encore activé sur cette machine — demandez à l'administrateur."
                   if fr else "Claude is not activated on this machine yet — ask the administrator.")
    elif not key_present():
        out.update(code="no_key", reason="Aucune clé Anthropic n'est saisie : l'administrateur la saisit dans Réglages."
                   if fr else "No Anthropic key: the administrator enters it in Settings.")
    elif lvl > cap:
        out.update(code="level", reason=(
            f"Ce projet est classé « {level_label(lvl)} » : ses travaux restent sur le modèle de la "
            f"machine. Claude n'est possible que sur un projet classé « {level_label(cap)} » ou moins."
            if fr else
            f"This project is classified « {levels.label(lvl)} »: its work stays on the model of this "
            f"machine. Claude is possible on a project classified « {levels.label(cap)} » or lower."))
    elif model and model not in av:
        out.update(code="model", reason=f"Modèle non proposé : {model}." if fr
                   else f"Model not offered: {model}.")
    else:
        out["allowed"] = True
    return out


def grant(project: str, model: str, secret: str, forwarded: bool) -> tuple[int, dict]:
    """What the router asks before sending ONE request to Anthropic. 404 for anything that
    is not the router (no secret configured, a wrong one, a request through the proxy) —
    the route does not exist for them."""
    want = router_secret()
    if not want or forwarded or not secret or not hmac.compare_digest(secret, want):
        return 404, {"detail": "not found"}
    g = gate(project, model)
    if not g["allowed"]:
        return 200, {"allowed": False, "code": g["code"], "reason": g["reason"]}
    import modelkeys
    key = modelkeys.get_plain("instance:anthropic") or ""
    if not key:
        return 200, {"allowed": False, "code": "no_key", "reason": g["reason"] or "no key"}
    return 200, {"allowed": True, "code": "", "key": key,
                 "models": [m["id"] for m in g["models"]], "default_model": g["default_model"]}


# ---- a session of a Claude work ---------------------------------------------------------
def work_tier(sid: str) -> tuple[str, str]:
    """(tier, model) chosen when the work started — ('', '') for an ordinary one."""
    try:
        import simple
        w = simple.get_work(sid) or {}
    except Exception:  # noqa: BLE001
        return "", ""
    return (w.get("tier") or ""), (w.get("claude_model") or "")


def session_env(sid: str, project: str, env: dict) -> dict:
    """The env overrides of a Claude work's session ({} for any other session). Always the
    headers, even when the project was reclassified since: the router refuses readably,
    nothing falls back to another model behind the person's back."""
    tier, model = work_tier(sid)
    if tier != "claude":
        return {}
    model = model or default_model() or DEFAULT_MODEL
    lines = [x for x in (env.get("ANTHROPIC_CUSTOM_HEADERS") or "").split("\n")
             if x.strip() and not x.lower().startswith((HEADER_TIER, HEADER_PROJECT))]
    lines += [f"{HEADER_TIER}: claude", f"{HEADER_PROJECT}: {project}"]
    return {"ANTHROPIC_MODEL": model, "ANTHROPIC_CUSTOM_HEADERS": "\n".join(lines)}


def pinned_cli() -> str:
    """The Claude Code CLI installed in the image (pinned), '' when there is none."""
    import shutil
    return shutil.which("claude") or ""


def cap_scope(sid: str, scope):
    """The memory recall of a Claude work reads at most the level Claude may receive: a note
    classified higher in the same project never reaches the prompt sent to Anthropic."""
    if scope is None or work_tier(sid)[0] != "claude":
        return scope
    from core import scope as sc
    caps = sc.caps(sc.normalize(scope)) or {}
    cap = max_level()
    return sc.normalize([*scope, *(sc.entry(p, cap) for p in caps)])


def doc_allowed(doc: dict) -> bool:
    try:
        return int(doc.get("level") if doc.get("level") is not None else 2) <= max_level()
    except (TypeError, ValueError):
        return False
