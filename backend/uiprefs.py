#!/usr/bin/env python3
"""uiprefs.py — how ONE person sees the cockpit (feature `simple_profile`).

* ``profile``: ``simple`` (task-first home, plain-language thread) or ``technical`` (every
  screen as before). A person without a choice gets ``SOKKAN_PROFILE_DEFAULT`` (technical
  when unset): turning the feature on changes nothing for anybody until someone chooses, or
  the instance says its default.
* ``lang``: ``auto`` (the browser's), ``fr`` or ``en`` — the simple profile's screens only.
* ``theme``: ``light`` (default), ``dark`` or ``auto`` (the system's) — idem.
* ``project``: the project the person last worked in (3.6.7), so that signing in on a
  browser that another person used last does not land them in THAT person's project.
* ``roles``: the person's jobs in SOKKAN Atelier (4.0, `backend/roles.py`) — a list of job
  ids, ``None`` until chosen (the Atelier then offers the choice once); an empty list = the
  person said « later ». Set by the person or, as a default, by an instance admin.

A UI preference, not a right: what a person may do is still decided by roles and features.
The one thing it gates is the approval mode, which a person on the simple profile cannot
change (`may_change_mode`). ``SOKKAN_PROFILE_LOCKED=1``: only an instance admin changes
somebody's profile (a pilot's volunteers stay on the simple one).

Store: ``$SOKKAN_DATA_DIR/uiprefs.json`` ({email: {profile, lang, theme, at}}).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

PROFILES = ("simple", "technical")
LANGS = ("auto", "fr", "en")
THEMES = ("light", "dark", "auto")
_lock = threading.Lock()
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


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
        "project": rec.get("project") if isinstance(rec.get("project"), str)
        and _SLUG.fullmatch(rec["project"]) else "",
        "roles": [r for r in rec["roles"] if isinstance(r, str) and _SLUG.fullmatch(r)]
        if isinstance(rec.get("roles"), list) else None,
        "available": on,
        "locked": locked(),
    }


def set_prefs(email: str, *, profile: str | None = None, lang: str | None = None,
              theme: str | None = None, project: str | None = None,
              roles: list[str] | None = None) -> dict:
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"profile: one of {', '.join(PROFILES)}")
    if lang is not None and lang not in LANGS:
        raise ValueError(f"lang: one of {', '.join(LANGS)}")
    if theme is not None and theme not in THEMES:
        raise ValueError(f"theme: one of {', '.join(THEMES)}")
    if project is not None and not _SLUG.fullmatch(project):
        raise ValueError("project: a project slug")
    if roles is not None and (not isinstance(roles, list) or len(roles) > 12
                              or not all(isinstance(r, str) and _SLUG.fullmatch(r) for r in roles)):
        raise ValueError("roles: a list of job ids")
    with _lock:
        d = _load()
        rec = dict(d.get(_key(email)) or {})
        for k, v in (("profile", profile), ("lang", lang), ("theme", theme), ("project", project),
                     ("roles", list(dict.fromkeys(roles)) if roles is not None else None)):
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
