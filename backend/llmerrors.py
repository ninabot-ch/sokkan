#!/usr/bin/env python3
"""llmerrors.py — what a person reads when the model does not answer.

A session whose inference endpoint answers 402 (credit / quota exhausted), 429 (rate limit) or
is unreachable used to show the raw line of the CLI (`API Error: 402 {"type":"error",…}`), or
nothing but a spinner. This module turns the status (or the raw line) into a short message in
the person's language: what happened, whether their work is lost (it is not), what to do and
who can do it. One place, used by the local Ship → Deep router (backend/shiprouter.py), by the
session stream (agentchat) and by tests/e2e_appliance.

`SOKKAN_ERROR_LANG` = en (default) | fr."""
from __future__ import annotations

import json
import os
import re

# code → {lang: (title, message)} ; the message always says what to do
_MSG = {
    "quota_exhausted": {
        "en": ("Inference credit used up",
               "The inference credit of this instance is used up (the model service answered 402). "
               "Nothing is lost: your session and its history are kept. Ask the administrator of "
               "this instance to top up or raise the plan, then send your message again."),
        "fr": ("Crédit d'inférence épuisé",
               "Le crédit d'inférence de cette instance est épuisé (le service de modèle a répondu 402). "
               "Rien n'est perdu : votre session et son historique sont conservés. Demandez à "
               "l'administrateur de l'instance de recharger le crédit ou de relever le forfait, "
               "puis renvoyez votre message."),
    },
    "rate_limited": {
        "en": ("Too many requests for now",
               "The model service is limiting requests right now (429): today's quota is reached, or "
               "too many requests arrived at once. Nothing is lost. Wait a minute and send your "
               "message again; if it keeps happening, the administrator can see the quota in "
               "Setup › Engines."),
        "fr": ("Trop de requêtes pour l'instant",
               "Le service de modèle limite les requêtes en ce moment (429) : le quota du jour est "
               "atteint, ou trop de requêtes sont arrivées en même temps. Rien n'est perdu. Patientez "
               "une minute puis renvoyez votre message ; si cela persiste, l'administrateur voit le "
               "quota dans Setup › Engines."),
    },
    "unreachable": {
        "en": ("The model cannot be reached",
               "The model service does not answer (network down, or the local model is still "
               "starting). Nothing is lost. Try again in a minute; if it lasts, tell the "
               "administrator: the model of this machine may need a restart."),
        "fr": ("Le modèle est injoignable",
               "Le service de modèle ne répond pas (réseau coupé, ou modèle local encore en cours de "
               "démarrage). Rien n'est perdu. Réessayez dans une minute ; si cela dure, prévenez "
               "l'administrateur : le modèle de cette machine a peut-être besoin d'être relancé."),
    },
    "auth": {
        "en": ("The model service refused this instance",
               "The model service refused the credentials of this instance (401/403): the key or "
               "token is missing, wrong or revoked. Only the administrator can fix it "
               "(Setup › Engines)."),
        "fr": ("Le service de modèle a refusé cette instance",
               "Le service de modèle a refusé les identifiants de cette instance (401/403) : la clé "
               "ou le jeton est absent, erroné ou révoqué. Seul l'administrateur peut corriger "
               "(Setup › Engines)."),
    },
    "overloaded": {
        "en": ("The model is busy",
               "The model service is overloaded or restarting (5xx). Nothing is lost. Try again in "
               "a moment."),
        "fr": ("Le modèle est occupé",
               "Le service de modèle est surchargé ou redémarre (5xx). Rien n'est perdu. Réessayez "
               "dans un instant."),
    },
    "too_long": {
        "en": ("This conversation is too long for the model",
               "The request is larger than what the model can read at once. Start a new session "
               "(the memory keeps what matters), or attach fewer / shorter documents."),
        "fr": ("Cette conversation est trop longue pour le modèle",
               "La demande dépasse ce que le modèle peut lire en une fois. Ouvrez une nouvelle "
               "session (la mémoire garde l'essentiel), ou joignez moins de documents, ou plus courts."),
    },
}
CODES = tuple(_MSG)
# the Anthropic error `type` each code is sent as (what the CLI and SDKs understand)
ANTHROPIC_TYPE = {"quota_exhausted": "billing_error", "rate_limited": "rate_limit_error",
                  "unreachable": "api_error", "auth": "authentication_error",
                  "overloaded": "overloaded_error", "too_long": "invalid_request_error"}
HTTP_STATUS = {"quota_exhausted": 402, "rate_limited": 429, "unreachable": 503, "auth": 401,
               "overloaded": 529, "too_long": 400}


def lang(explicit: str | None = None) -> str:
    v = (explicit or os.environ.get("SOKKAN_ERROR_LANG") or "en").strip().lower()[:2]
    return v if v in ("en", "fr") else "en"


def classify(status: int | None, text: str = "") -> str | None:
    """HTTP status (None = no HTTP answer at all) + raw body → code, None = not one of ours."""
    t = (text or "").lower()
    if status is None:
        return "unreachable"
    if status == 402 or "billing" in t or "insufficient" in t and "credit" in t:
        return "quota_exhausted"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status in (400, 413) and re.search(r"context|too long|too large|maximum.*tokens|prompt is too", t):
        return "too_long"
    if status in (502, 504):          # a proxy in front that cannot reach the model
        return "unreachable"
    if 500 <= status < 600:
        return "overloaded"
    return None


def humanize(code: str, language: str | None = None) -> dict:
    title, message = _MSG[code][lang(language)]
    return {"code": code, "title": title, "message": message}


_RAW = re.compile(r"API Error:?\s*(\d{3})?", re.I)
_CONN = re.compile(r"connection error|econnrefused|enotfound|etimedout|fetch failed|"
                   r"connect(ion)? (refused|timed out|reset)|unable to connect|network", re.I)


def from_text(text: str, language: str | None = None) -> dict | None:
    """The CLI's own error line (`API Error: 429 {…}`, `API Error: Connection error.`) → the
    readable message; None when the text is not an inference error. A message that is already
    one of ours (the router wrote it) is recognised by its marker and returned as is."""
    if not text:
        return None
    m = re.search(r"\[sokkan:([a-z_]+)\]", text)
    if m and m.group(1) in _MSG:
        return humanize(m.group(1), language)
    r = _RAW.search(text)
    if not r:
        return None
    status = int(r.group(1)) if r.group(1) else None
    if status is None and not _CONN.search(text):
        return None
    code = classify(status, text)
    return humanize(code, language) if code else None


def anthropic_body(code: str, language: str | None = None, detail: str = "") -> dict:
    """The error as an Anthropic-format body (what the CLI prints after `API Error: <status>`).
    The `[sokkan:<code>]` marker lets `from_text` find the code again on the other side."""
    h = humanize(code, language)
    msg = f"{h['title']} — {h['message']} [sokkan:{code}]"
    if detail:
        msg += f" ({detail[:200]})"
    return {"type": "error", "error": {"type": ANTHROPIC_TYPE[code], "message": msg}}


def dumps(code: str, language: str | None = None, detail: str = "") -> bytes:
    return json.dumps(anthropic_body(code, language, detail), ensure_ascii=False).encode()
