#!/usr/bin/env python3
"""notify.py — SOKKAN : notifications sortantes (Telegram / webhook générique).

Deux usages :
- HITL push : une session attend une approbation depuis trop longtemps (tu as
  lancé et tu es parti) → on te pingue, avec un lien pour venir cliquer.
- Alertes prod : la stack d'observabilité (Grafana alerting) POST vers le
  cockpit → transformé en notification + (voir observability) session de diag.

Config persistée par instance sous $SOKKAN_DATA_DIR/notify.json (chmod 0600,
même convention que llm.py). Aucune dépendance : httpx est déjà là. Best-effort
— une notif qui échoue n'interrompt jamais le flux produit.
"""
from __future__ import annotations

import json
import os
import threading

import httpx

DATA_DIR = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
CONFIG = os.path.join(DATA_DIR, "notify.json")
PUBLIC_URL = (os.environ.get("SOKKAN_PUBLIC_URL", "http://localhost:3009")).rstrip("/")
# délai avant de pinguer sur une permission non résolue (tu réponds vite = pas
# de spam ; tu es parti = ping)
HITL_DELAY_S = float(os.environ.get("SOKKAN_NOTIFY_HITL_DELAY_S", "25"))

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> dict:
    """Enregistre la config (channels telegram/webhook). Merge partiel."""
    with _lock:
        cur = _load()
        cur.update({k: v for k, v in cfg.items() if v is not None})
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cur, f, indent=2)
        os.chmod(CONFIG, 0o600)
    return status()


def status() -> dict:
    """Vue non-sensible pour l'UI : quels canaux sont configurés (sans secrets)."""
    c = _load()
    tg = c.get("telegram") or {}
    wh = c.get("webhook") or {}
    return {
        "telegram": bool(tg.get("bot_token") and tg.get("chat_id")),
        "webhook": bool(wh.get("url")),
        "hitl_enabled": c.get("hitl_enabled", True),
    }


def enabled() -> bool:
    s = status()
    return s["telegram"] or s["webhook"]


def hitl_enabled() -> bool:
    return enabled() and _load().get("hitl_enabled", True)


def session_link(sid: str) -> str:
    return f"{PUBLIC_URL}/?session={sid}"


def send(title: str, body: str = "", link: str = "", kind: str = "info") -> dict:
    """Envoie sur tous les canaux configurés (best-effort). Retourne le détail
    par canal (utile pour le bouton « test » de l'UI)."""
    cfg = _load()
    out: dict[str, str] = {}
    text = title if not body else f"{title}\n{body}"
    if link:
        text += f"\n{link}"

    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                json={"chat_id": tg["chat_id"], "text": text,
                      "disable_web_page_preview": True},
                timeout=10)
            out["telegram"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001
            out["telegram"] = f"error: {e}"

    wh = cfg.get("webhook") or {}
    if wh.get("url"):
        try:
            r = httpx.post(wh["url"], json={"title": title, "body": body,
                                            "link": link, "kind": kind}, timeout=10)
            out["webhook"] = "ok" if r.status_code < 300 else f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001
            out["webhook"] = f"error: {e}"
    return out
