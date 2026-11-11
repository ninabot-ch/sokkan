#!/usr/bin/env python3
"""uiprefs.py — how ONE person sees the cockpit (feature `simple_profile`).

* ``profile``: ``simple`` (task-first home, plain-language thread) or ``technical`` (every
  screen as before). A person without a choice gets ``SOKKAN_PROFILE_DEFAULT`` (technical
  when unset): turning the feature on changes nothing for anybody until someone chooses, or
  the instance says its default.
* ``lang``: ``auto`` (the browser's), ``fr`` or ``en`` — the simple profile's screens only.
* ``theme``: ``light`` (default), ``dark`` or ``auto`` (the system's) — idem.

A UI preference, not a right: what a person may do is still decided by roles and features.
The one thing it gates is the approval mode, which a person on the simple profile cannot
change (`may_change_mode`). ``SOKKAN_PROFILE_LOCKED=1``: only an instance admin changes
somebody's profile (a pilot's volunteers stay on the simple one).

Store: ``$SOKKAN_DATA_DIR/uiprefs.json`` ({email: {profile, lang, theme, at}}).
"""
from __future__ import annotations

import json
import os
import threading
import time

PROFILES = ("simple", "technical")
LANGS = ("auto", "fr", "en")
THEMES = ("light", "dark", "auto")
_lock = threading.Lock()


def store_path() -> str:
    return os.environ.get("SOKKAN_UIPREFS_STORE") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "uiprefs.json")


def feature_on() -> bool:
    import features
    return features.enabled("simple_profile")


def default_profile() -> str:
    v = (os.environ.get("SOKKAN_PROFILE_DEFAULT") or "").strip().lower()
    return v if v in PROFILES else "technical"


def locked() -> bool:
    return (os.environ.get("SOKKAN_PROFILE_LOCKED") or "").strip().lower() in ("1", "true", "yes", "on")


def _load() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(email: str) -> str:
    return (email or "").lower().strip()


def get(email: str) -> dict:
    """The person's preferences, defaults filled in. With the feature off the profile is
    always ``technical`` (a stored choice is kept for the day it comes back on)."""
    rec = _load().get(_key(email)) or {}
    on = feature_on()
    profile = rec.get("profile") if rec.get("profile") in PROFILES else default_profile()
    return {
        "profile": profile if on else "technical",
        "chosen": rec.get("profile") in PROFILES,
        "lang": rec.get("lang") if rec.get("lang") in LANGS else "auto",
        "theme": rec.get("theme") if rec.get("theme") in THEMES else "light",
        "available": on,
        "locked": locked(),
    }


def set_prefs(email: str, *, profile: str | None = None, lang: str | None = None,
              theme: str | None = None) -> dict:
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"profile: one of {', '.join(PROFILES)}")
    if lang is not None and lang not in LANGS:
        raise ValueError(f"lang: one of {', '.join(LANGS)}")
    if theme is not None and theme not in THEMES:
        raise ValueError(f"theme: one of {', '.join(THEMES)}")
    with _lock:
        d = _load()
        rec = dict(d.get(_key(email)) or {})
        for k, v in (("profile", profile), ("lang", lang), ("theme", theme)):
            if v is not None:
                rec[k] = v
        rec["at"] = time.time()
        d[_key(email)] = rec
        p = store_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, p)
    return get(email)


def is_simple(email: str) -> bool:
    return get(email)["profile"] == "simple"


def may_change_mode(email: str) -> bool:
    """The approval mode (confirmations / auto-edits / automode / plan) of a session: never
    from the simple profile — every action that changes something waits for the person."""
    return not is_simple(email)
