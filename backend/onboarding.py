#!/usr/bin/env python3
"""onboarding.py — from « installed » to « the team's first validated deliverable » (4.0).

Two journeys and the administrator's everyday screens, all in the Atelier (simple profile),
none of which asks anybody to open `.env`:

* **First launch (administrator)** — six steps, each one skippable, resumable at the next
  visit: the organisation (name, language), the model (this machine's / SOKKAN Inference /
  the client's own key, with a connection test before anything is saved), the projects
  (with their classification in plain words), the people (personal access code, invitation
  link, or SSO when the instance has one), the first documents, the first work.
* **First sign-in of a person** — their position (the catalogue of positions is another lot:
  this module reads it when it is there, else a starter list), a 60-second tour, a first work
  proposed with an example document of their trade.
* **Administration** — Team (people, roles, projects, deactivate, new code), Usage (per person
  and per project, budget and alert), the validations journal (read through hitl_api).

Store: ``$SOKKAN_DATA_DIR/onboarding.json`` — the setup state, per-person progress, and the
pending invitations (only a SHA-256 of an invitation token is kept, never the token).
Every change is journaled (`audit`). Nothing here is behind a feature switch: it reads the
features it relies on (multi_project, documents, project_vault_budgets) and says when one is
off, in words.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import audit
import auth
import iam
import projects

STEPS = ("org", "model", "projects", "people", "documents", "first_work")
STATES = ("done", "skipped")
LANGS = ("fr", "en")
# the cheapest Anthropic model for the one-token test of a client key (any model the key sees
# would do: the test proves the key, not the model)
TEST_MODEL = "claude-haiku-4-5"
INVITE_TTL_S = 7 * 86400          # an invitation link lives a week, once
_lock = threading.Lock()


# ---- store --------------------------------------------------------------------------------
def store_path() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))) \
        / "onboarding.json"


def _load() -> dict:
    try:
        d = json.loads(store_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def _update(fn) -> dict:
    with _lock:
        d = _load()
        fn(d)
        _save(d)
        return d


def _norm(email: str) -> str:
    return (email or "").lower().strip()


# ---- what the instance already holds (evidence of each step) --------------------------------
def _work_projects() -> list[dict]:
    return [p for p in projects.list_projects() if p["slug"] != projects.SHARED_PROJECT]


def _people_emails() -> set[str]:
    out = {_norm(u.get("email") or "") for u in iam.list_users()}
    try:
        import localaccounts
        out |= {_norm(a["email"]) for a in localaccounts.accounts()}
    except Exception:  # noqa: BLE001 — no local accounts
        pass
    for p in projects.list_projects():
        for g in projects.list_grants(p["slug"]):
            if g.get("principal_kind") == "user":
                out.add(_norm(g.get("principal") or ""))
    out.discard("")
    out.discard("anonyme")
    return out


def _counts() -> dict:
    """How far the instance is, read from what it holds (never from the wizard's own marks)."""
    out = {"projects": 0, "people": 0, "documents": 0, "works": 0, "validated": 0}
    try:
        out["projects"] = len([p for p in _work_projects() if p["slug"] != projects.DEFAULT_PROJECT]) \
            or (1 if projects.get(projects.DEFAULT_PROJECT) else 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        owner = _norm(auth.OWNER_EMAIL)
        out["people"] = len(_people_emails() - {owner})
    except Exception:  # noqa: BLE001
        pass
    try:
        import documents
        if documents.enabled():
            con = documents._con()
            try:
                out["documents"] = con.execute(
                    "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL").fetchone()[0]
            except Exception:  # noqa: BLE001 — older schema without deleted_at
                out["documents"] = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            finally:
                con.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        import board
        out["works"] = len([s for s in board.list_sessions() if (s.get("kind") or "") == "sdk"])
    except Exception:  # noqa: BLE001
        pass
    try:
        out["validated"] = len(audit.recent(limit=1000, q="deliverable.validate"))
    except Exception:  # noqa: BLE001
        pass
    return out


def _first_validation_at() -> float | None:
    try:
        rows = [r for r in audit.recent(limit=1000, q="deliverable.validate")
                if r.get("action") == "deliverable.validate"]
    except Exception:  # noqa: BLE001
        return None
    ts = [float(r.get("ts") or 0) for r in rows if r.get("ts")]
    return min(ts) if ts else None


# ---- the model ---------------------------------------------------------------------------------
def model_state() -> dict:
    """Which engine the instance runs on, in the wizard's three words."""
    import llm
    st = llm.status()
    kind = "none"
    if st.get("engine") == "appliance":
        kind = "local"
    elif st.get("mode") == "byok":
        kind = "key"
    elif st.get("mode") in ("custom", "included"):
        base = (st.get("base_url") or llm.load().get("base_url") or "").lower()
        kind = "sokkan" if "sokkan" in base or st.get("mode") == "included" else "custom"
    elif st.get("configured"):
        kind = "key"
    return {"kind": kind, "configured": bool(st.get("configured")),
            "local_available": bool(llm.appliance_config()),
            "model": st.get("model") or "", "operator_managed": bool(st.get("operator_managed")),
            "sokkan_url": _sokkan_url()}


def _sokkan_url() -> str:
    return (os.environ.get("SOKKAN_INFER_BASE_URL") or "https://infer.sokkan.ch").rstrip("/")


def _result(ok: bool, code: str, lang: str, ms: int | None = None, detail: str = "") -> dict:
    """A test result in words: what happened, what to do. `code` = ok | unreachable | auth |
    quota_exhausted | rate_limited | model | no_local | bad_input | error."""
    import llmerrors
    if ok:
        title = {"fr": "Le modèle répond", "en": "The model answers"}[lang]
        msg = {"fr": "La connexion fonctionne. Vous pouvez continuer.",
               "en": "The connection works. You can go on."}[lang]
    elif code in ("unreachable", "auth", "quota_exhausted", "rate_limited", "overloaded"):
        h = llmerrors.humanize(code, lang)
        title, msg = h["title"], h["message"]
        if code == "auth":
            msg = {"fr": "La clé ou le jeton a été refusé (absent, erroné ou révoqué). Vérifiez-le "
                         "et réessayez : rien n'a été enregistré.",
                   "en": "The key or token was refused (missing, wrong or revoked). Check it and "
                         "try again: nothing was saved."}[lang]
    elif code == "no_local":
        title = {"fr": "Pas de modèle sur cette machine", "en": "No model on this machine"}[lang]
        msg = {"fr": "Cette installation n'a pas de modèle local. Choisissez SOKKAN Inference ou "
                     "votre propre clé.",
               "en": "This installation has no local model. Choose SOKKAN Inference or your own "
                     "key."}[lang]
    elif code == "model":
        title = {"fr": "Modèle inconnu", "en": "Unknown model"}[lang]
        msg = {"fr": "Le service répond mais ne connaît pas ce modèle. Vérifiez son nom.",
               "en": "The service answers but does not know this model. Check its name."}[lang]
    elif code == "bad_input":
        title = {"fr": "Il manque une information", "en": "Something is missing"}[lang]
        msg = detail
        detail = ""
    else:
        title = {"fr": "Le test a échoué", "en": "The test failed"}[lang]
        msg = {"fr": "Le service a répondu de façon inattendue. Réessayez dans un instant.",
               "en": "The service answered unexpectedly. Try again in a moment."}[lang]
    return {"ok": ok, "code": "ok" if ok else code, "title": title, "message": msg,
            "latency_ms": ms, "detail": detail[:200]}


def test_model(kind: str, key: str = "", base_url: str = "", model: str = "",
               lang: str = "fr", client=None) -> dict:
    """One small call, never saved: the local router's /health, or ONE token asked of the
    Anthropic-compatible endpoint (`/v1/messages`, max_tokens 1) — the only test that proves
    the key, the URL and the model together. Never returns the key nor the provider's body."""
    import httpx
    import llm
    import llmerrors
    lang = lang if lang in LANGS else "fr"
    http = client or httpx.Client(timeout=15)
    t0 = time.monotonic()
    ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
    try:
        if kind == "local":
            cfg = llm.appliance_config()
            if not cfg:
                return _result(False, "no_local", lang)
            try:
                r = http.get(cfg["base_url"] + "/health", timeout=5)
                ok = r.status_code == 200 and bool((r.json() or {}).get("ship"))
            except Exception:  # noqa: BLE001
                return _result(False, "unreachable", lang, ms())
            return _result(ok, "ok" if ok else "unreachable", lang, ms())
        if kind == "key" and key.strip().startswith("sk-ant-") and not base_url.strip():
            base, headers = "https://api.anthropic.com", {"x-api-key": key.strip(),
                                                         "anthropic-version": "2023-06-01"}
            model = model.strip() or TEST_MODEL
        else:
            base = (base_url.strip() or (_sokkan_url() if kind == "sokkan" else "")).rstrip("/")
            if not base.startswith(("http://", "https://")):
                return _result(False, "bad_input", lang, detail={
                    "fr": "L'adresse du service doit commencer par https://",
                    "en": "The service address must start with https://"}[lang])
            if not key.strip():
                return _result(False, "bad_input", lang, detail={
                    "fr": "Collez la clé ou le jeton reçu.", "en": "Paste the key or token you received."}[lang])
            headers = {"authorization": f"Bearer {key.strip()}", "x-api-key": key.strip(),
                       "anthropic-version": "2023-06-01"}
            model = model.strip() or (llm.DEFAULT_INCLUDED_MODEL if kind == "sokkan" else "")
            if not model:
                return _result(False, "bad_input", lang, detail={
                    "fr": "Indiquez le nom du modèle.", "en": "Give the model's name."}[lang])
        try:
            r = http.post(base + "/v1/messages", headers=headers, timeout=20, json={
                "model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
        except Exception:  # noqa: BLE001 — DNS, refused, timeout: the same sentence
            return _result(False, "unreachable", lang, ms())
        if 200 <= r.status_code < 300:
            return _result(True, "ok", lang, ms())
        body = r.text[:2000]
        if r.status_code in (400, 404) and "model" in body.lower():
            return _result(False, "model", lang, ms())
        code = llmerrors.classify(r.status_code, body) or "error"
        return _result(False, code, lang, ms(), detail=f"HTTP {r.status_code}")
    finally:
        if client is None:
            http.close()


def save_model(kind: str, key: str = "", base_url: str = "", model: str = "") -> dict:
    """Write the instance's engine (llm.json) — after a passing test, decided by the route."""
    import llm
    if kind == "local":
        llm.CONFIG.unlink(missing_ok=True)       # the machine's model applies again
    elif kind == "key" and key.strip().startswith("sk-ant-") and not base_url.strip():
        llm.save({"mode": "byok", "anthropic_api_key": key.strip()})
    else:
        base = (base_url.strip() or (_sokkan_url() if kind == "sokkan" else "")).rstrip("/")
        llm.save({"mode": "custom", "base_url": base, "auth_token": key.strip(),
                  "model": model.strip() or (llm.DEFAULT_INCLUDED_MODEL if kind == "sokkan" else ""),
                  "small_model": ""})
    return model_state()


# ---- the setup state -----------------------------------------------------------------------
def setup_state(d: dict | None = None) -> dict:
    d = _load() if d is None else d
    s = d.get("setup") or {}
    marks = s.get("steps") or {}
    steps = [{"id": i, "state": (marks.get(i) or {}).get("state") or "todo",
              "at": (marks.get(i) or {}).get("at")} for i in STEPS]
    nxt = next((x["id"] for x in steps if x["state"] == "todo"), None)
    finished = bool(s.get("finished_at")) or nxt is None
    return {"started_at": s.get("started_at"), "finished_at": s.get("finished_at"),
            "finished": finished, "next": nxt, "steps": steps, "lang": s.get("lang") or "",
            "preexisting": bool(s.get("preexisting"))}


def ensure_started(email: str) -> dict:
    """The first time an administrator opens the Atelier: start the clock — or, on an instance
    that already has works (an upgrade), mark the setup as done: nobody is sent back through a
    first launch they never needed."""
    d = _load()
    if (d.get("setup") or {}).get("started_at"):
        return d

    def fn(x: dict) -> None:
        if (x.get("setup") or {}).get("started_at"):
            return
        c = _counts()
        s = x.setdefault("setup", {})
        s["started_at"] = time.time()
        s["started_by"] = email
        if c["works"] > 0:
            s["preexisting"] = True
            s["finished_at"] = s["started_at"]
    return _update(fn)


def mark_step(step: str, state: str, by: str) -> dict:
    if step not in STEPS:
        raise ValueError(f"step: one of {', '.join(STEPS)}")
    if state not in (*STATES, "todo"):
        raise ValueError("state: done | skipped | todo")

    def fn(d: dict) -> None:
        s = d.setdefault("setup", {})
        s.setdefault("started_at", time.time())
        marks = s.setdefault("steps", {})
        if state == "todo":
            marks.pop(step, None)
            s.pop("finished_at", None)
        else:
            marks[step] = {"state": state, "at": time.time(), "by": by}
    return setup_state(_update(fn))


def finish(by: str, reopen: bool = False) -> dict:
    def fn(d: dict) -> None:
        s = d.setdefault("setup", {})
        s.setdefault("started_at", time.time())
        if reopen:
            s.pop("finished_at", None)
            s.pop("preexisting", None)
            s["steps"] = {}
        else:
            s["finished_at"] = time.time()
            s["finished_by"] = by
    return setup_state(_update(fn))


def org_lang() -> str:
    lang = ((_load().get("setup") or {}).get("lang") or "").strip()
    return lang if lang in LANGS else ""


def set_org(name: str, lang: str, by: str) -> dict:
    import instance
    if name.strip():
        instance.set_org_name(name)
    if lang:
        if lang not in LANGS:
            raise ValueError("lang: fr | en")
        _update(lambda d: d.setdefault("setup", {}).__setitem__("lang", lang))
    return {"org_name": instance.info()["org_name"], "lang": org_lang()}


# ---- a person's own progress ----------------------------------------------------------------
def person(email: str) -> dict:
    p = (_load().get("people") or {}).get(_norm(email)) or {}
    return {"role_id": p.get("role_id") or "", "tour_done": bool(p.get("tour_done_at")),
            "welcomed": bool(p.get("welcomed_at")), "first_sid": p.get("first_sid") or ""}


def set_person(email: str, **kw) -> dict:
    def fn(d: dict) -> None:
        p = d.setdefault("people", {}).setdefault(_norm(email), {})
        if kw.get("role_id") is not None:
            p["role_id"] = str(kw["role_id"])[:60]
        if kw.get("tour_done"):
            p["tour_done_at"] = p.get("tour_done_at") or time.time()
        if kw.get("welcomed"):
            p["welcomed_at"] = p.get("welcomed_at") or time.time()
        if kw.get("first_sid"):
            p["first_sid"] = str(kw["first_sid"])[:64]
    _update(fn)
    return person(email)


# ---- positions (the catalogue's hook) -----------------------------------------------------
def roles(lang: str = "fr") -> dict:
    """The positions a person picks from at their first sign-in. The catalogue of positions
    (module `roles_catalog`, another lot) wins when it is installed; its `catalog(lang)`
    returns a list of {id, label, description, ...}. Else the starter list."""
    lang = lang if lang in LANGS else "fr"
    try:
        import roles_catalog  # type: ignore[import-not-found]
        rows = roles_catalog.catalog(lang)
        if rows:
            return {"source": "catalog", "roles": [
                {"id": str(r["id"]), "label": str(r.get("label") or r["id"]),
                 "description": str(r.get("description") or ""), "sample": bool(r.get("sample", True))}
                for r in rows if isinstance(r, dict) and r.get("id")]}
    except Exception:  # noqa: BLE001 — not installed (or broken): the floor
        pass
    import onboarding_samples as S
    return {"source": "starter", "roles": [
        {"id": rid, "label": S.STARTER[rid][lang]["label"],
         "description": S.STARTER[rid][lang]["description"], "sample": True} for rid in S.ORDER]}


def sample_for(role_id: str, lang: str) -> dict:
    import onboarding_samples as S
    lang = lang if lang in LANGS else "fr"
    try:
        import roles_catalog  # type: ignore[import-not-found]
        s = roles_catalog.sample(role_id, lang)
        if s and s.get("ask") and s.get("doc"):
            return {"ask": s["ask"], "name": s["doc"][0], "text": s["doc"][1]}
    except Exception:  # noqa: BLE001
        pass
    r = S.STARTER.get(role_id) or S.STARTER["other"]
    return {"ask": r[lang]["ask"], "name": r[lang]["doc"][0], "text": r[lang]["doc"][1]}


# ---- invitations -------------------------------------------------------------------------------
def _h(token: str) -> str:
    return hashlib.sha256(("sokkan-invite:" + token).encode()).hexdigest()


def create_invite(email: str, by: str) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()

    def fn(d: dict) -> None:
        inv = d.setdefault("invites", {})
        for k in [k for k, v in inv.items() if v.get("email") == _norm(email) and not v.get("used_at")]:
            inv.pop(k)                         # one live link per person: a new one replaces it
        for k in [k for k, v in inv.items() if v.get("expires_at", 0) < now - 30 * 86400]:
            inv.pop(k)
        inv[_h(token)] = {"email": _norm(email), "by": by, "created_at": now,
                          "expires_at": now + INVITE_TTL_S, "used_at": None}
    _update(fn)
    return token


def pending_invites() -> dict[str, dict]:
    now = time.time()
    return {v["email"]: v for v in (_load().get("invites") or {}).values()
            if not v.get("used_at") and v.get("expires_at", 0) > now}


def drop_invites(email: str) -> None:
    _update(lambda d: d.__setitem__("invites", {k: v for k, v in (d.get("invites") or {}).items()
                                                 if v.get("email") != _norm(email)}))


def use_invite(token: str) -> str:
    """The e-mail of a live invitation, consumed. ValueError('expired' | 'unknown')."""
    h = _h((token or "").strip())
    out: dict = {}

    def fn(d: dict) -> None:
        v = (d.get("invites") or {}).get(h)
        if not v or v.get("used_at"):
            out["err"] = "unknown"
            return
        if v.get("expires_at", 0) < time.time():
            out["err"] = "expired"
            return
        v["used_at"] = time.time()
        out["email"] = v["email"]
    _update(fn)
    if out.get("err"):
        raise ValueError(out["err"])
    return out["email"]


# ---- the team --------------------------------------------------------------------------------
def team() -> list[dict]:
    """Everyone the instance knows, once: name, instance role, projects and roles there, how
    they sign in, last sign-in, disabled or not, a pending invitation, their position."""
    import revocation
    try:
        import localaccounts
        codes = {a["email"]: a for a in localaccounts.accounts()}
    except Exception:  # noqa: BLE001
        codes = {}
    users = {_norm(u["email"]): u for u in iam.list_users()}
    grants: dict[str, list[dict]] = {}
    names = {p["slug"]: p["name"] for p in projects.list_projects()}
    for slug in names:
        for g in projects.list_grants(slug):
            if g.get("principal_kind") == "user":
                grants.setdefault(_norm(g["principal"]), []).append(
                    {"slug": slug, "name": names[slug], "role": g.get("role")})
    disabled = {a["email"]: a for a in revocation.disabled_accounts()}
    inv = pending_invites()
    people_d = _load().get("people") or {}
    rl = {r["id"]: r["label"] for r in roles("fr")["roles"]}
    owner = _norm(auth.OWNER_EMAIL)
    out = []
    for e in sorted(set(users) | set(codes) | set(grants) | set(inv) | {owner}):
        if not e or e == "anonyme":
            continue
        u = users.get(e) or {}
        role = u.get("role") or ("owner" if e == owner else "none")
        name = iam.display_name(e) or (codes.get(e) or {}).get("name") or ""
        if name == e:
            name = ""
        rid = (people_d.get(e) or {}).get("role_id") or ""
        out.append({
            "email": e, "name": name, "instance_role": role, "owner": e == owner,
            "admin": iam.rank(role) >= iam.rank("admin"),
            "projects": sorted(grants.get(e, []), key=lambda g: g["name"].lower()),
            "has_code": e in codes, "last_login": (codes.get(e) or {}).get("last_login"),
            "disabled": e in disabled, "invite_pending": e in inv,
            "invite_expires_at": (inv.get(e) or {}).get("expires_at"),
            "position": rl.get(rid, rid),
        })
    out.sort(key=lambda r: (not r["owner"], (r["name"] or r["email"]).lower()))
    return out


def add_person(email: str, name: str, admin: bool, project_slugs: list[str], by: str,
               lang: str = "") -> dict:
    """Create the account and its grants. Local auth: a personal code (returned once) — the
    route turns it into a link when asked. SSO: the grants only, the provider signs them in."""
    email = _norm(email)
    if "@" not in email or " " in email or len(email) > 200:
        raise ValueError("email")
    known = {p["slug"] for p in projects.list_projects()}
    slugs = [s for s in dict.fromkeys(project_slugs or []) if s in known and s != projects.SHARED_PROJECT]
    if not admin and not slugs:
        raise ValueError("projects")
    if email == _norm(auth.OWNER_EMAIL):
        raise ValueError("owner")
    if admin:
        iam.upsert_user(email, "admin", name or email)
    elif name and iam.get_user(email).get("known"):
        iam.set_name(email, name)
    for s in slugs:
        projects.grant(s, "user", email, "dev", created_by=by)
    code = ""
    if auth.MODE == "local":
        import localaccounts
        code = localaccounts.issue(email, by, name=name, create=True)
    if lang in LANGS:
        try:
            import uiprefs
            uiprefs.set_prefs(email, lang=lang, **({"project": slugs[0]} if slugs else {}))
        except Exception:  # noqa: BLE001 — preferences are a convenience
            pass
    return {"email": email, "code": code, "projects": slugs, "admin": admin}


def set_projects(email: str, wanted: dict[str, str], by: str) -> list[dict]:
    """The person's project roles become exactly `wanted` ({slug: role}, '' = none)."""
    email = _norm(email)
    for p in projects.list_projects():
        slug = p["slug"]
        if slug == projects.SHARED_PROJECT:
            continue
        has = next((g for g in projects.list_grants(slug)
                    if g.get("principal_kind") == "user" and _norm(g["principal"]) == email), None)
        want = (wanted or {}).get(slug, "")
        if want and want not in projects.PROJECT_ROLES:
            raise ValueError(f"role: {', '.join(projects.PROJECT_ROLES)}")
        if want and (not has or has.get("role") != want):
            projects.grant(slug, "user", email, want, created_by=by)
        elif not want and has:
            projects.revoke(slug, "user", email)
    return next((r["projects"] for r in team() if r["email"] == email), [])


# ---- usage ----------------------------------------------------------------------------------
def usage_view(days: int = 30) -> dict:
    """Per person and per project over the period (the Costs estimate: API price list, not an
    invoice), with each project's budget and its state."""
    import board
    import budgets
    import instance
    import usage
    days = max(1, min(int(days or 30), 90))
    s = usage._scan(None)
    from datetime import datetime, timedelta
    now = datetime.now(usage.TZ)
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    inc = usage._include_external()
    rows = [r for r in s["rows"] if usage._counted(r, inc) and r["day"] >= start]
    owners: dict[str, str] = {}
    for x in board.list_sessions():
        o = _norm(x.get("owner") or "")
        if o:
            owners[x["session_id"]] = o
            if x.get("claude_session_id"):
                owners[x["claude_session_id"]] = o
    by_person: dict[str, list] = {}
    for r in rows:
        f = s["files"].get(r["path"]) or {}
        sid = f.get("session_id") or ""
        sess = f.get("session") or {}
        who = owners.get(sid) or _norm(sess.get("owner") or "") or "—"
        by_person.setdefault(who, []).append(r)
    names = {t["email"]: t["name"] for t in team()}

    def tot(rs: list) -> dict:
        t = usage._sum(rs)
        return {"cost": round(t["billed"], 4), "api_equiv": round(t["api_equiv"], 4),
                "turns": t["turns"], "out_tokens": t["out_tokens"],
                "today": round(usage._sum([r for r in rs if r["day"] >= today])["billed"], 4)}
    people_rows = [{"email": e, "name": names.get(e, ""), **tot(rs)} for e, rs in by_person.items()]
    people_rows.sort(key=lambda x: (-x["cost"], -x["turns"]))
    proj_rows = []
    pnames = {p["slug"]: p["name"] for p in projects.list_projects()}
    seen = {r["project"] for r in rows} | {p for p in pnames if p != projects.SHARED_PROJECT}
    for slug in sorted(seen):
        rs = [r for r in rows if r["project"] == slug]
        b = budgets.status(slug) if budgets.enabled() else None
        proj_rows.append({"slug": slug, "name": pnames.get(slug, slug), **tot(rs), "budget": b})
    proj_rows.sort(key=lambda x: (-x["cost"], x["name"].lower()))
    inst = instance.budgets()
    day_total = usage._sum([r for r in rows if r["day"] >= today])["billed"]
    alerts = []
    for p in proj_rows:
        b = p.get("budget") or {}
        if b.get("state") in ("warn", "stop"):
            alerts.append({"project": p["slug"], "name": p["name"], "state": b["state"],
                           "pct_day": b.get("pct_day"), "pct_month": b.get("pct_month"),
                           "currency": b.get("currency")})
    if inst.get("budget_day_usd"):
        pct = 100 * day_total / inst["budget_day_usd"]
        if pct >= 80:
            alerts.append({"project": "", "name": "", "state": "stop" if pct >= 100 else "warn",
                           "pct_day": round(pct, 1), "currency": "USD"})
    return {"days": days, "from": start, "to": today, "billing": s["billing"],
            "people": people_rows, "projects": proj_rows,
            "total": tot(rows), "instance_budget": inst, "today_total": round(day_total, 4),
            "project_budgets": budgets.enabled(), "alerts": alerts}


# ---- routes ---------------------------------------------------------------------------------
class StepIn(BaseModel):
    step: str = ""
    state: str = "done"
    finish: bool = False
    reopen: bool = False


class OrgIn(BaseModel):
    org_name: str = ""
    lang: str = ""


class ModelIn(BaseModel):
    kind: str                 # local | sokkan | key | custom
    key: str = ""
    base_url: str = ""
    model: str = ""
    lang: str = "fr"


class ProjectNewIn(BaseModel):
    name: str
    level: str = "team"
    description: str = ""


class PersonIn(BaseModel):
    email: str
    name: str = ""
    admin: bool = False
    projects: list[str] = []
    via: str = "code"         # code | link


class PersonPatch(BaseModel):
    admin: bool | None = None
    projects: dict[str, str] | None = None
    name: str | None = None


class MeIn(BaseModel):
    role_id: str | None = None
    tour_done: bool = False
    welcomed: bool = False
    first_sid: str = ""


class SampleIn(BaseModel):
    role_id: str = ""
    lang: str = "fr"


class InviteIn(BaseModel):
    token: str


class BudgetIn(BaseModel):
    project: str = ""          # '' = the instance's daily ceiling
    day: float | None = None
    month: float | None = None
    currency: str | None = None


def _public_url(request: Request) -> str:
    base = (os.environ.get("SOKKAN_PUBLIC_URL") or "").rstrip("/")
    if base:
        return base
    return f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"


_invite_fails: dict[str, list[float]] = {}


def router(require) -> APIRouter:
    r = APIRouter()

    def admin(request: Request) -> dict:
        """An INSTANCE administrator, whatever project the request names (`require` checks
        the role in the selected project — an admin who is a member of the current one would
        be turned away from the screens that are the instance's)."""
        u = auth.instance_user(request)
        if iam.rank(u.get("role") or "") < iam.rank("admin"):
            raise HTTPException(403, "role 'admin' of the instance required")
        return u

    @r.get("/api/onboarding")
    def state(request: Request, lang: str = "fr", u: dict = Depends(auth.current_user)) -> dict:
        import instance
        email = _norm(u["email"])
        inst_admin = iam.rank(auth.instance_user(request).get("role") or "") >= iam.rank("admin")
        d = ensure_started(email) if inst_admin else _load()
        me = person(email)
        out: dict = {
            "admin": inst_admin,
            "org_name": instance.info()["org_name"], "org_lang": org_lang(),
            "me": {**me, "needs_welcome": not me["welcomed"] and not inst_admin},
            "auth": {"mode": auth.MODE, "codes": auth.MODE == "local",
                     "sso": auth.MODE in ("oidc", "ldaps")},
        }
        if inst_admin:
            st = setup_state(d)
            c = _counts()
            out["setup"] = {**st, "needed": not st["finished"]}
            out["counts"] = c
            out["model"] = model_state()
            out["features"] = {
                "multi_project": _feature("multi_project"),
                "documents": _feature("documents"), "budgets": _feature("project_vault_budgets"),
            }
            first = _first_validation_at()
            started = (d.get("setup") or {}).get("started_at")
            out["ttfv"] = {"started_at": started, "first_validated_at": first,
                           "seconds": round(first - started) if first and started and first >= started else None}
        return out

    @r.put("/api/onboarding/setup")
    def setup_mark(body: StepIn, u: dict = Depends(admin)) -> dict:
        try:
            if body.finish or body.reopen:
                st = finish(u["email"], reopen=body.reopen)
                audit.log(u["email"], "onboarding.reopen" if body.reopen else "onboarding.finish", "setup", project="")
                return st
            st = mark_step(body.step, body.state, u["email"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.log(u["email"], "onboarding.step", body.step, body.state, project="")
        return st

    @r.put("/api/onboarding/org")
    def org(body: OrgIn, u: dict = Depends(admin)) -> dict:
        try:
            out = set_org(body.org_name, body.lang, u["email"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.log(u["email"], "instance.rename", out["org_name"], f"lang={out['lang'] or '-'}", project="")
        if body.lang in LANGS:
            try:
                import uiprefs
                uiprefs.set_prefs(u["email"], lang=body.lang)
            except Exception:  # noqa: BLE001
                pass
        return out

    @r.post("/api/onboarding/model/test")
    def model_test(body: ModelIn, u: dict = Depends(admin)) -> dict:
        res = test_model(body.kind, body.key, body.base_url, body.model, body.lang)
        audit.log(u["email"], "llm.test", body.kind, res["code"], project="")
        return res

    @r.post("/api/onboarding/model")
    def model_save(body: ModelIn, u: dict = Depends(admin)) -> dict:
        if model_state()["operator_managed"]:
            raise HTTPException(403, {"code": "managed", "message": "managed inference"})
        if body.kind not in ("local", "sokkan", "key", "custom"):
            raise HTTPException(400, "kind: local | sokkan | key | custom")
        res = test_model(body.kind, body.key, body.base_url, body.model, body.lang)
        if not res["ok"]:
            return JSONResponse({"saved": False, "test": res}, status_code=200)
        st = save_model(body.kind, body.key, body.base_url, body.model)
        audit.log(u["email"], "llm.config", f"onboarding:{body.kind}",
                  st.get("model") or "", project="")
        return {"saved": True, "test": res, "model": st}

    @r.post("/api/onboarding/projects")
    def project_new(body: ProjectNewIn, u: dict = Depends(admin)) -> dict:
        from core import levels
        name = body.name.strip()
        if len(name) < 2:
            raise HTTPException(400, {"code": "name", "message": "name"})
        if not _feature("multi_project"):
            raise HTTPException(409, {"code": "multi_project_off", "message": "multi_project"})
        lvl = levels.parse(body.level) if body.level in levels.IDS else None
        if lvl is None:
            raise HTTPException(400, {"code": "level", "message": "level"})
        slug = _slug(name)
        base, n = slug, 2
        while projects.get(slug) is not None:
            slug = f"{base[:36]}-{n}"
            n += 1
        p = projects.create(slug, name, access_source="sso_group", created_by=u["email"],
                            description=body.description.strip()[:300])
        projects.grant(slug, "user", u["email"], "admin", created_by=u["email"])
        projects.set_level(slug, lvl)
        audit.log(u["email"], "project.create", slug, f"sso_group · {levels.ident(lvl)}")
        audit.log(u["email"], "project.grant.self", slug, f"user {u['email']} → admin")
        try:
            import app as _app  # the memory indexers learn the project (same as the admin route)
            _app._sync_project_indexers()
        except Exception:  # noqa: BLE001
            pass
        return {"slug": p["slug"], "name": p["name"], "level": levels.ident(lvl)}

    @r.get("/api/onboarding/projects")
    def project_list(u: dict = Depends(admin)) -> dict:
        from core import levels
        out = []
        for p in _work_projects():
            out.append({"slug": p["slug"], "name": p["name"],
                        "level": levels.ident(p.get("level") if p.get("level") is not None else levels.parse("team")),
                        "people": len([g for g in projects.list_grants(p["slug"])
                                       if g.get("principal_kind") == "user"])})
        return {"projects": out, "multi": _feature("multi_project")}

    @r.get("/api/team")
    def team_list(_u: dict = Depends(admin)) -> dict:
        return {"people": team(), "auth": {"mode": auth.MODE, "codes": auth.MODE == "local",
                                           "sso": auth.MODE in ("oidc", "ldaps")},
                "projects": [{"slug": p["slug"], "name": p["name"]} for p in _work_projects()],
                "roles": projects.PROJECT_ROLES}

    @r.post("/api/team")
    def team_add(body: PersonIn, request: Request, u: dict = Depends(admin)) -> dict:
        try:
            res = add_person(body.email, body.name.strip()[:120], body.admin, body.projects,
                             u["email"], lang=org_lang())
        except ValueError as e:
            raise HTTPException(400, {"code": str(e), "message": str(e)})
        audit.log(u["email"], "iam.invite", res["email"],
                  f"{'admin' if res['admin'] else 'member'} · projects {','.join(res['projects']) or '-'}"
                  f" · via {body.via if res['code'] else 'sso'}", project="")
        out = {"email": res["email"], "projects": res["projects"], "admin": res["admin"],
               "url": _public_url(request)}
        if res["code"] and body.via == "link":
            tok = create_invite(res["email"], u["email"])
            out["link"] = f"{_public_url(request)}/?invite={tok}"
            out["expires_at"] = time.time() + INVITE_TTL_S
        elif res["code"]:
            out["code"] = res["code"]
        return out

    @r.patch("/api/team/{email}")
    def team_patch(email: str, body: PersonPatch, u: dict = Depends(admin)) -> dict:
        email = _norm(email)
        if email == _norm(auth.OWNER_EMAIL) and body.admin is False:
            raise HTTPException(400, {"code": "owner", "message": "owner"})
        if body.admin is not None:
            cur = iam.get_user(email)
            if body.admin:
                iam.upsert_user(email, "admin", (body.name or cur.get("name") or email))
            elif cur.get("known") and cur.get("role") != "owner":
                iam.delete_user(email)       # back to project grants only
            audit.log(u["email"], "iam.upsert", email, "admin" if body.admin else "member", project="")
        if body.name:
            iam.set_name(email, body.name.strip()[:120])
        if body.projects is not None:
            try:
                set_projects(email, body.projects, u["email"])
            except ValueError as e:
                raise HTTPException(400, str(e))
            audit.log(u["email"], "project.grant", "*", f"user {email} → "
                      + (", ".join(f"{k}:{v}" for k, v in body.projects.items() if v) or "none"), project="")
        return {"person": next((p for p in team() if p["email"] == email), None)}

    @r.post("/api/team/{email}/code")
    def team_code(email: str, request: Request, via: str = "code", u: dict = Depends(admin)) -> dict:
        email = _norm(email)
        if auth.MODE != "local":
            raise HTTPException(400, {"code": "sso", "message": "sso"})
        if email == _norm(auth.OWNER_EMAIL):
            raise HTTPException(400, {"code": "owner", "message": "owner"})
        import localaccounts
        try:
            code = localaccounts.issue(email, u["email"])
        except ValueError:
            raise HTTPException(404, "unknown account")
        audit.log(u["email"], "iam.access_code.create", email, f"via {via}", project="")
        if via == "link":
            tok = create_invite(email, u["email"])
            return {"email": email, "link": f"{_public_url(request)}/?invite={tok}",
                    "expires_at": time.time() + INVITE_TTL_S}
        return {"email": email, "code": code}

    @r.post("/api/team/{email}/disable")
    async def team_disable(email: str, u: dict = Depends(admin)) -> dict:
        import revocation
        email = _norm(email)
        if email in (_norm(auth.OWNER_EMAIL), _norm(u["email"])):
            raise HTTPException(400, {"code": "self", "message": "self"})
        res = await revocation.revoke(email, u["email"], reason="deactivated in Team")
        drop_invites(email)
        return {"ok": True, "sessions_stopped": res.get("sessions_stopped", 0)}

    @r.post("/api/team/{email}/enable")
    def team_enable(email: str, u: dict = Depends(admin)) -> dict:
        import revocation
        revocation.reinstate(_norm(email), u["email"])
        return {"ok": True}

    @r.get("/api/team/usage")
    def team_usage(days: int = 30, _u: dict = Depends(admin)) -> dict:
        return usage_view(days)

    @r.put("/api/team/budget")
    def team_budget(body: BudgetIn, u: dict = Depends(admin)) -> dict:
        import budgets
        import instance
        if body.project:
            if not budgets.enabled():
                raise HTTPException(409, {"code": "budgets_off", "message": "project_vault_budgets"})
            try:
                b = budgets.set_budget(body.project, day=body.day, month=body.month,
                                       currency=body.currency, by=u["email"])
            except ValueError as e:
                raise HTTPException(400, str(e))
            audit.log(u["email"], "project.budget", body.project,
                      f"day={b['day']} month={b['month']} {b['currency']}", project=body.project)
        else:
            instance.set_budgets(day_usd=body.day if body.day is not None else None)
            audit.log(u["email"], "instance.budgets", f"day={body.day}", project="")
        return usage_view(30)

    @r.get("/api/onboarding/roles")
    def roles_list(lang: str = "fr", _u: dict = Depends(auth.current_user)) -> dict:
        return roles(lang)

    @r.put("/api/onboarding/me")
    def me_set(body: MeIn, u: dict = Depends(auth.current_user)) -> dict:
        out = set_person(u["email"], role_id=body.role_id, tour_done=body.tour_done,
                         welcomed=body.welcomed, first_sid=body.first_sid)
        if body.role_id is not None:
            audit.log(u["email"], "onboarding.position", body.role_id[:60], project="")
        return out

    @r.post("/api/onboarding/sample")
    def sample(body: SampleIn, request: Request, u: dict = Depends(auth.current_user)) -> dict:
        """The example document of the person's position, added to the current project's
        documents (once per person and project), and the first ask that goes with it."""
        import documents
        import projectgate
        s = sample_for(body.role_id or person(u["email"])["role_id"], body.lang)
        if not _feature("documents"):
            return {"ask": s["ask"], "document": None}
        slug = projectgate.requested_project(request)
        pu = projectgate.project_user(auth.instance_user(request), slug)
        if pu is None or iam.rank(pu["role"]) < iam.rank("dev"):
            return {"ask": s["ask"], "document": None}
        mine = [x for x in documents.list_project(slug)
                if x.get("name") == documents.safe_name(s["name"])[0]
                and _norm(x.get("owner") or "") == _norm(u["email"])]
        if mine:
            return {"ask": s["ask"], "document": documents.public(mine[0])}
        tmp = documents.incoming_path(slug)
        tmp.write_text(s["text"], encoding="utf-8")
        try:
            doc = documents.add_file(slug, s["name"], tmp, owner=u["email"])
        except documents.DocumentError as e:
            return {"ask": s["ask"], "document": None, "why": e.code}
        audit.log(u["email"], "document.upload", doc["id"], f"{doc['name']} · onboarding example",
                  project=slug)
        return {"ask": s["ask"], "document": documents.public(doc)}

    @r.post("/api/auth/invite")
    def invite_accept(body: InviteIn, request: Request):
        """An invitation link: signs the person in AND gives them their personal code, once
        (they keep it to come back). Single use; a used or expired link says so."""
        import localaccounts
        import revocation
        import session as sess
        if auth.MODE != "local":
            raise HTTPException(400, {"code": "sso", "message": "sso"})
        ip = (request.headers.get("cf-connecting-ip")
              or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
              or (request.client.host if request.client else "?"))
        now = time.time()
        fails = [t for t in _invite_fails.get(ip, []) if now - t < 60]
        if len(fails) >= 10:
            raise HTTPException(429, "too many attempts")
        try:
            email = use_invite(body.token)
        except ValueError as e:
            _invite_fails[ip] = [*fails, now]
            audit.log("", "auth.invite.refused", str(e), project="")
            raise HTTPException(410 if str(e) == "expired" else 404, {"code": str(e), "message": str(e)})
        if revocation.is_disabled(email):
            raise HTTPException(403, {"code": "disabled", "message": "disabled"})
        code = localaccounts.issue(email, "invite")
        name = localaccounts.display_name(email) or email
        audit.log(email, "auth.login", "invite", project="")
        resp = JSONResponse({"ok": True, "email": email, "name": name, "code": code})
        resp.set_cookie(sess.COOKIE, sess.make(email, name), max_age=sess.TTL, httponly=True,
                        secure=(os.environ.get("SOKKAN_PUBLIC_URL") or "").startswith("https"),
                        samesite="lax")
        return resp

    return r


def _feature(fid: str) -> bool:
    try:
        import features
        return features.enabled(fid)
    except Exception:  # noqa: BLE001
        return False


def _slug(name: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40]
    return s or "projet"
