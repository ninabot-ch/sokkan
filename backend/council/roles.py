"""roles.py — the nine roles of the council, one YAML file each (backend/council/roles/*.yaml).

Same shape as the job kits of `backend/roles/` (id, order, `i18n.fr` / `i18n.en`) with the fields
a council role needs: `tier` (default model tier), `avatar` (fixed initials + icon, never a
generated face), `i18n.<lang>.system_prompt` and `limits` (the 150-words / sourced-figures rule).
A self-hoster may add or replace a role with SOKKAN_COUNCIL_ROLES_DIR (same id = replaces it) —
but the nine ids of ORDER must stay: the council always sits with its nine.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

BUILTIN = Path(__file__).with_name("roles")
LANGS = ("fr", "en")
ORDER = ("daf", "code", "ops", "conception", "achats", "qa", "marcom", "juridique", "coach")
TIERS = ("ship", "deep")
_FIELDS = ("label", "short", "description", "system_prompt", "limits")
_cache: dict = {}


class RoleError(ValueError):
    pass


def _check(r: dict, where: str) -> dict:
    for k in ("id", "order", "tier", "avatar", "i18n"):
        if k not in r:
            raise RoleError(f"{where}: missing {k}")
    if r["tier"] not in TIERS:
        raise RoleError(f"{where}: tier must be one of {TIERS}")
    for lang in LANGS:
        t = (r["i18n"] or {}).get(lang) or {}
        for f in _FIELDS:
            if not str(t.get(f) or "").strip():
                raise RoleError(f"{where}: i18n.{lang}.{f} is empty")
    if not (r["avatar"].get("initials") and r["avatar"].get("icon")):
        raise RoleError(f"{where}: avatar needs initials and icon")
    return r


def load(force: bool = False) -> dict[str, dict]:
    stamp = []
    dirs = [BUILTIN]
    extra = (os.environ.get("SOKKAN_COUNCIL_ROLES_DIR") or "").strip()
    if extra:
        dirs.append(Path(extra))
    for d in dirs:
        if d.is_dir():
            stamp += [(str(p), p.stat().st_mtime_ns) for p in sorted(d.glob("*.yaml"))]
    if not force and _cache.get("stamp") == stamp:
        return _cache["roles"]
    out: dict[str, dict] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yaml")):
            r = yaml.safe_load(p.read_text(encoding="utf-8"))
            out[r["id"]] = _check(r, p.name)
    missing = [i for i in ORDER if i not in out]
    if missing:
        raise RoleError("council roles missing: " + ", ".join(missing))
    _cache.update(stamp=stamp, roles=out)
    return out


def ids() -> list[str]:
    """The roles in the order they speak: the nine of ORDER (DAF → coach)."""
    return list(ORDER)


def get(rid: str) -> dict:
    return load()[rid]


def text(rid: str, lang: str) -> dict:
    return get(rid)["i18n"][lang if lang in LANGS else "en"]


def public(lang: str) -> list[dict]:
    """What the room shows: name, short name, description, avatar — for each role, in order."""
    out = []
    for rid in ids():
        r, t = get(rid), text(rid, lang)
        out.append({"id": rid, "label": t["label"], "short": t["short"], "description": t["description"],
                    "avatar": r["avatar"], "leads": bool(r.get("leads")), "tier": r["tier"]})
    return out
