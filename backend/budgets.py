#!/usr/bin/env python3
"""budgets.py — SOKKAN 3.2 lot 4: a cost ceiling per project, per day and per month.

On top of the per-session budget (instance setting, hard stop) and the per-run budget of an
agent, a project admin sets a daily and / or monthly ceiling for their project, in USD or
CHF. The spend is the estimate of the Costs tab (`usage.project_spend`: transcripts of the
project's sessions and agent runs, API price list — not an invoice).

* ≥ 80 % of a ceiling: a warning, once per session and period;
* ≥ 100 %: hard stop (HITL) — a session of the project refuses new turns, a new session
  is told so, an agent run of the project does not start (status `budget`). A project
  admin raises the ceiling (Costs → Project budget) or waits for the next day / month.

Only with the feature `project_vault_budgets` (registry). Off: no project ceiling is
enforced (the instance budgets still are). Stored in projects.db (`project_budgets`).
CHF ceilings compare with the USD estimate through SOKKAN_FX_USD_PER_CHF (agentcost).
"""
from __future__ import annotations

import threading
import time

import projects

CURRENCIES = ("USD", "CHF")
WARN_AT = 0.8
_CACHE_S = 30.0
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_budgets (
    project TEXT PRIMARY KEY REFERENCES projects(slug) ON DELETE CASCADE,
    currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD', 'CHF')),
    day REAL NOT NULL DEFAULT 0,
    month REAL NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""


def enabled() -> bool:
    import features
    return features.enabled("project_vault_budgets")


def _con():
    con = projects._con()
    con.executescript(_SCHEMA)
    return con


def get(project: str) -> dict:
    """The ceilings of a project (0 = none)."""
    con = _con()
    try:
        r = con.execute("SELECT * FROM project_budgets WHERE project=?", (project,)).fetchone()
    finally:
        con.close()
    if not r:
        return {"project": project, "currency": "USD", "day": 0.0, "month": 0.0,
                "updated_by": "", "updated_at": None}
    return dict(r)


def set_budget(project: str, *, day: float | None = None, month: float | None = None,
               currency: str | None = None, by: str = "") -> dict:
    if not projects.get(project):
        raise ValueError(f"unknown project {project!r}")
    cur = get(project)
    currency = (currency or cur["currency"]).upper()
    if currency not in CURRENCIES:
        raise ValueError("currency must be USD or CHF")
    vals = {}
    for k, v in (("day", day), ("month", month)):
        v = cur[k] if v is None else float(v)
        if not (0 <= v <= 1_000_000):
            raise ValueError(f"{k} ceiling must be between 0 and 1000000")
        vals[k] = v
    con = _con()
    try:
        con.execute("INSERT INTO project_budgets(project, currency, day, month, updated_by, "
                    "updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
                    "currency=excluded.currency, day=excluded.day, month=excluded.month, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (project, currency, vals["day"], vals["month"], by, time.time()))
        con.commit()
    finally:
        con.close()
    with _lock:
        _cache.pop(project, None)
    return get(project)


def _fx(currency: str) -> float:
    """USD per unit of the ceiling's currency."""
    if currency == "CHF":
        import agentcost
        return agentcost.fx_usd_per_chf()
    return 1.0


def spend_usd(project: str, fresh: bool = False) -> dict:
    """{"day": usd, "month": usd}, cached a few seconds (checked at every turn)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(project)
        if hit and not fresh and now - hit[0] < _CACHE_S:
            return hit[1]
    import usage
    try:
        v = usage.project_spend(project)
    except Exception:  # noqa: BLE001 — cost estimate unavailable: never block on it
        v = {"day": 0.0, "month": 0.0}
    with _lock:
        _cache[project] = (now, v)
    return v


def status(project: str, fresh: bool = False) -> dict:
    """Ceilings, spend (in the ceiling's currency) and state: ok | warn | stop | off."""
    b = get(project)
    out = {**b, "enabled": enabled(), "spent_day": 0.0, "spent_month": 0.0,
           "state": "off", "message": ""}
    if not out["enabled"]:
        return out
    s = spend_usd(project, fresh=fresh)
    fx = _fx(b["currency"])
    out["spent_day"] = round(s["day"] / fx, 4)
    out["spent_month"] = round(s["month"] / fx, 4)
    state, msg = "ok", ""
    for period, label in (("day", "daily"), ("month", "monthly")):
        cap = float(b[period] or 0)
        if not cap:
            continue
        spent = out[f"spent_{period}"]
        out[f"pct_{period}"] = round(100 * spent / cap, 1)
        if spent >= cap:
            state = "stop"
            msg = (f"Project '{project}' {label} budget reached ({spent:.2f} ≥ {cap:.2f} "
                   f"{b['currency']}). A project admin raises it in Costs → Project budget.")
            break
        if spent >= WARN_AT * cap and state == "ok":
            state = "warn"
            msg = (f"Project '{project}' has used {spent:.2f} of its {cap:.2f} {b['currency']} "
                   f"{label} budget (≥80%). New turns stop at the limit.")
    out["state"], out["message"] = state, msg
    return out


def check(project: str | None) -> tuple[str, str]:
    """(state, message) for an enforcement point. Never raises: a broken estimate = ok."""
    if not project or not enabled():
        return "ok", ""
    try:
        st = status(project)
    except Exception:  # noqa: BLE001
        return "ok", ""
    return (st["state"] if st["state"] in ("warn", "stop") else "ok"), st["message"]


def list_all() -> list[dict]:
    """Every project with a ceiling (admin overview)."""
    con = _con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM project_budgets ORDER BY project")]
    finally:
        con.close()
