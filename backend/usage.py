#!/usr/bin/env python3
"""usage.py — SOKKAN: tokens and costs from the Claude Code transcripts (Operate › Costs).

Source of truth = the JSONL transcripts of Claude Code: each assistant message carries
`message.usage` (input / output / cache read / cache write) and `message.model`.

3.2.3 — reconciliation (the 3.2.2 tab showed $567 for a day on an instance that runs on
a Claude subscription). What changed, and why:

* **One API message = one count.** The CLI writes one transcript line PER CONTENT BLOCK
  (thinking, text, tool_use…) and every line repeats the usage of the whole message; the
  lines are deduplicated by `message.id` (max of each field, like agentcost.Meter).
* **Prices from a versioned table** (`pricing.py`, `model_prices.json`), per model and per
  token kind (input / cache write 1.25× or 2× / cache read / output). An unknown model is
  counted in tokens, never priced at a guessed tariff.
* **Source.** The workspace folder of the instance can hold transcripts SOKKAN did not
  start (the operator's own `claude` CLI in the same directory). A transcript is
  `sokkan` when it is one of the instance's sessions (board session id or Claude session
  id, agent runs included) or lives in a per-project workspace; anything else is
  `external`, reported apart and left out of the totals and budgets
  (`SOKKAN_USAGE_EXTERNAL=include` to count it).
* **Billing basis**, per model, from how the instance reaches it (`billing()`):
    api           Claude with an API key — real cost at the public price;
    subscription  Claude through a Pro/Max login (CLI login, setup-token) — nothing billed
                  per token; the API-equivalent cost is shown apart, labelled as such;
    gateway       SOKKAN Inference tiers (`sokkan-*`) — billed by the gateway: tokens ×
                  tier price, and the gateway's own ledger when it answers;
    local         Magnitude / a model served on your hardware — 0, tokens only;
    custom        another endpoint — price from agentcost's table, else unpriced.
  `SOKKAN_BILLING_BASIS=api|subscription` forces the Claude basis. The basis applies to
  the whole history (a transcript does not say how it was authenticated).
* **Budgets** (per project, daily notice) use the metered spend of SOKKAN sessions:
  billed cost, and on a subscription the API-equivalent (a usage brake — nothing is
  billed per token there).

Cache sqlite per file (mtime + size): the transcripts are re-parsed only when they change.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pricing

# same resolution as app.py: CLAUDE_CONFIG_DIR + slug of the workspace
_claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
_cwd_slug = (os.environ.get("SOKKAN_AGENT_CWD")
             or ("/workspace" if os.path.isdir("/workspace") else os.getcwd())).replace("/", "-")
PROJECT_DIR = Path(
    os.environ.get("SOKKAN_PROJECT_DIR", os.path.join(_claude_dir, "projects", _cwd_slug))
)
DB = Path(os.environ.get("SOKKAN_USAGE_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "usage.db")))
TZ = ZoneInfo(os.environ.get("SOKKAN_TZ") or "Europe/Zurich")

SCHEMA = 3   # 3.2.3: per (file, day, model) rows, deduplicated messages, cost split by kind
BASES = ("api", "subscription", "gateway", "local", "custom")
BASIS_LABEL = {
    "api": "Claude API (API key)",
    "subscription": "Claude subscription (login)",
    "gateway": "SOKKAN Inference",
    "local": "Local engines (Magnitude)",
    "custom": "Other endpoint",
}
BASIS_METHOD = {
    "api": "tokens × the public Claude API price of each model — what the API key is billed",
    "subscription": "included in the Claude subscription — nothing billed per token; the "
                    "API-equivalent cost (same tokens at the public API price) is shown apart",
    "gateway": "tokens × the SOKKAN Inference tier price; the gateway's own ledger is shown "
               "when it answers",
    "local": "served on your hardware — no cost per token, tokens only",
    "custom": "tokens × the price in the model price table (SOKKAN_MODEL_PRICES); unpriced "
              "models are counted in tokens only",
}


def _data_dir() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    r = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if not r or r["value"] != str(SCHEMA):
        # the 3.2.2 cache counted every content block and guessed prices: re-parse all
        con.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS days;"
                          " DROP TABLE IF EXISTS rows;")
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA),))
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
            session_id TEXT, first_prompt TEXT, models TEXT,
            turns INTEGER, in_tokens INTEGER, out_tokens INTEGER,
            cache_read INTEGER, cache_write INTEGER,
            cost REAL,                 -- API-equivalent cost of the priced messages (USD)
            unpriced_turns INTEGER,
            first_ts REAL, last_ts REAL,
            project TEXT NOT NULL DEFAULT 'default',
            entrypoint TEXT
        );
        CREATE TABLE IF NOT EXISTS rows (
            path TEXT, day TEXT, model TEXT, turns INTEGER,
            in_tokens INTEGER, out_tokens INTEGER, cache_read INTEGER,
            cw_5m INTEGER, cw_1h INTEGER,
            cost_in REAL, cost_out REAL, cost_cr REAL, cost_cw REAL,
            priced INTEGER,
            PRIMARY KEY (path, day, model)
        );
        CREATE INDEX IF NOT EXISTS ix_rows_day ON rows(day);
        """
    )
    con.commit()
    return con


def transcript_dirs() -> list[tuple[Path, str]]:
    """(directory of Claude Code transcripts, project) — the instance's workspace for
    `default`, and since 3.2 lot 3 one workspace per other project
    ($SOKKAN_DATA_DIR/projects/<slug>/work → ~/.claude/projects/<its slug>)."""
    out = [(PROJECT_DIR, "default")]
    root = _data_dir() / "projects"
    try:
        work = sorted(p for p in root.glob("*/work") if p.is_dir())
    except OSError:
        work = []
    for w in work:
        slug = w.parent.name
        if slug in ("default", "_no-project"):
            continue
        out.append((Path(_claude_dir) / "projects" / str(w).replace("/", "-"), slug))
    return out


# --------------------------------------------------------------------------- parsing

def _first_prompt(d: dict) -> str:
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str) and c.strip():
        for ln in c.strip().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("<") and not ln.startswith("Caveat:"):
                return ln[:80]
    return ""


def parse_transcript(path: Path) -> dict:
    """One transcript → its API messages, deduplicated by `message.id` (the CLI writes
    one line per content block, each with the usage of the whole message — 3.2.2 summed
    them all). Returns {"messages": [{model, ts, usage}], "first_prompt", "entrypoint"}."""
    by_id: dict[str, dict] = {}
    loose: list[dict] = []
    first_prompt, entrypoint = "", ""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "user" and not first_prompt:
                first_prompt = _first_prompt(d)
            if not entrypoint and d.get("entrypoint"):
                entrypoint = str(d["entrypoint"])
            if t != "assistant":
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            model = msg.get("model") or ""
            if not u or model == "<synthetic>":   # API error placeholder, not a billed turn
                continue
            try:
                ts = datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = path.stat().st_mtime
            mid = msg.get("id") or d.get("requestId")
            if not mid:
                loose.append({"model": model, "ts": ts, "usage": u})
                continue
            prev = by_id.get(mid)
            if prev is None:
                by_id[mid] = {"model": model, "ts": ts, "usage": dict(u)}
                continue
            # same message, another content block: keep the largest figure of each field
            pu = prev["usage"]
            for k, v in u.items():
                if isinstance(v, (int, float)) and isinstance(pu.get(k), (int, float)):
                    pu[k] = max(pu[k], v)
                elif k not in pu:
                    pu[k] = v
            cc, pcc = u.get("cache_creation") or {}, pu.get("cache_creation") or {}
            if isinstance(cc, dict) and isinstance(pcc, dict) and cc:
                pu["cache_creation"] = {k: max(int(cc.get(k) or 0), int(pcc.get(k) or 0))
                                        for k in set(cc) | set(pcc)}
            prev["ts"] = min(prev["ts"], ts)
    return {"messages": [*by_id.values(), *loose], "first_prompt": first_prompt,
            "entrypoint": entrypoint}


def _aggregate(parsed: dict) -> tuple[dict, dict]:
    """→ (file totals, {(day, model): row})."""
    tot = {"turns": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "unpriced": 0,
           "first_ts": None, "last_ts": None, "models": set()}
    rows: dict[tuple[str, str], dict] = {}
    for m in parsed["messages"]:
        u, model, ts = m["usage"], m["model"], m["ts"]
        i = int(u.get("input_tokens") or 0)
        o = int(u.get("output_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        det = u.get("cache_creation") or {}
        c1h = int(det.get("ephemeral_1h_input_tokens") or 0) if isinstance(det, dict) else 0
        c5m = int(det.get("ephemeral_5m_input_tokens") or 0) if isinstance(det, dict) else 0
        if c1h + c5m == 0:
            c5m = cw
        c = pricing.cost(model, u)
        day = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
        r = rows.setdefault((day, model), {"turns": 0, "in_tokens": 0, "out_tokens": 0,
                                           "cache_read": 0, "cw_5m": 0, "cw_1h": 0,
                                           "cost_in": 0.0, "cost_out": 0.0, "cost_cr": 0.0,
                                           "cost_cw": 0.0, "priced": 1 if c else 0})
        r["turns"] += 1
        r["in_tokens"] += i
        r["out_tokens"] += o
        r["cache_read"] += cr
        r["cw_5m"] += c5m
        r["cw_1h"] += c1h
        if c:
            r["cost_in"] += c["input"]
            r["cost_out"] += c["output"]
            r["cost_cr"] += c["cache_read"]
            r["cost_cw"] += c["cache_write"]
            tot["cost"] += c["total"]
        else:
            tot["unpriced"] += 1
        tot["turns"] += 1
        tot["in"] += i
        tot["out"] += o
        tot["cr"] += cr
        tot["cw"] += c5m + c1h
        tot["models"].add(model)
        tot["first_ts"] = ts if tot["first_ts"] is None else min(tot["first_ts"], ts)
        tot["last_ts"] = ts if tot["last_ts"] is None else max(tot["last_ts"], ts)
    return tot, rows


def refresh() -> None:
    """Updates the cache for new / changed transcripts (incremental)."""
    con = _con()
    known = {r["path"]: (r["mtime"], r["size"]) for r in con.execute("SELECT path, mtime, size FROM files")}
    live = set()
    for d, proj in transcript_dirs():
        try:
            # sub-agents (Task tool) write their own transcript under the parent session:
            # <session>/subagents/agent-*.jsonl — their tokens belong to that session
            files = [(p, p.stem) for p in d.glob("*.jsonl")]
            files += [(p, p.parent.parent.name) for p in d.glob("*/subagents/*.jsonl")]
        except OSError:
            files = []
        for p, session_id in files:
            try:
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            live.add(key)
            if known.get(key) == (st.st_mtime, st.st_size):
                continue
            parsed = parse_transcript(p)
            tot, rows = _aggregate(parsed)
            con.execute(
                "INSERT OR REPLACE INTO files(path, mtime, size, session_id, first_prompt,"
                " models, turns, in_tokens, out_tokens, cache_read, cache_write, cost,"
                " unpriced_turns, first_ts, last_ts, project, entrypoint)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, st.st_mtime, st.st_size, session_id, parsed["first_prompt"],
                 ",".join(sorted(tot["models"])), tot["turns"], tot["in"], tot["out"],
                 tot["cr"], tot["cw"], tot["cost"], tot["unpriced"], tot["first_ts"],
                 tot["last_ts"], proj, parsed["entrypoint"]),
            )
            con.execute("DELETE FROM rows WHERE path=?", (key,))
            con.executemany(
                "INSERT INTO rows(path, day, model, turns, in_tokens, out_tokens, cache_read,"
                " cw_5m, cw_1h, cost_in, cost_out, cost_cr, cost_cw, priced)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(key, day, model, r["turns"], r["in_tokens"], r["out_tokens"], r["cache_read"],
                  r["cw_5m"], r["cw_1h"], r["cost_in"], r["cost_out"], r["cost_cr"],
                  r["cost_cw"], r["priced"]) for (day, model), r in rows.items()],
            )
    for gone in set(known) - live:
        con.execute("DELETE FROM files WHERE path=?", (gone,))
        con.execute("DELETE FROM rows WHERE path=?", (gone,))
    con.commit()
    con.close()


# --------------------------------------------------------------------------- billing basis

def _magnitude_state() -> dict:
    try:
        import magnitude
        return {"nodes": magnitude.load()["nodes"], "catalog": [m["id"] for m in magnitude.CATALOG],
                "shim_url_of": magnitude.shim_url_of}
    except Exception:  # noqa: BLE001 — Magnitude absent / state unreadable
        return {"nodes": {}, "catalog": [], "shim_url_of": None}


def _local_model_ids() -> set[str]:
    """Models served on the instance's own hardware: the Magnitude catalogue, what the
    nodes serve, and the engines they found running."""
    st = _magnitude_state()
    ids = set(st["catalog"])
    for node in st["nodes"].values():
        if (node.get("serving") or {}).get("model"):
            ids.add(node["serving"]["model"])
        for e in node.get("engines") or []:
            if e.get("model"):
                ids.add(e["model"])
    return ids


def _is_local_url(url: str) -> bool:
    st = _magnitude_state()
    shims = {st["shim_url_of"](n) for n in st["nodes"].values()} if st["shim_url_of"] else set()
    u = (url or "").rstrip("/")
    return bool(u) and (u in shims or u.endswith(":8790"))


def billing() -> dict:
    """How the instance reaches its models → {claude, mode, why, endpoint, model}."""
    import llm
    forced = (os.environ.get("SOKKAN_BILLING_BASIS") or "").strip().lower()
    c = llm.load() or {}
    mode = c.get("mode") or "env"
    if forced in ("api", "subscription"):
        claude, why = forced, f"SOKKAN_BILLING_BASIS={forced}"
    elif mode == "byok" and c.get("anthropic_api_key"):
        claude, why = "api", "Claude with the API key set in Setup › Engines"
    elif mode == "byok" and c.get("claude_oauth_token"):
        claude, why = "subscription", "Claude with a subscription token (setup-token)"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        claude, why = "api", "Claude with ANTHROPIC_API_KEY"
    elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        claude, why = "subscription", "Claude with CLAUDE_CODE_OAUTH_TOKEN (subscription)"
    else:
        claude, why = "subscription", ("Claude through the CLI login of the host (Pro/Max "
                                       "subscription) — no API key configured")
    endpoint = None
    if mode == "included":
        endpoint = "gateway"
    elif mode == "custom":
        endpoint = "local" if _is_local_url(c.get("base_url", "")) else "custom"
    return {"claude": claude, "mode": mode, "why": why, "endpoint": endpoint,
            "model": c.get("model") or None}


def basis_for(model: str, b: dict, local_ids: set[str]) -> str:
    m = (model or "").lower()
    if m.startswith("sokkan-"):
        return "gateway"
    if m.startswith("claude") or pricing.canonical(m):
        return b["claude"]
    if model in local_ids:
        return "local"
    if b.get("endpoint") in ("local", "gateway") and model == b.get("model"):
        return b["endpoint"]
    return "custom"


def _other_price(model: str, basis: str) -> dict | None:
    """Price of a non-Claude model (gateway tier / custom table), USD per M tokens."""
    try:
        import agentcost
        p = agentcost._price_for(model, "gateway" if basis == "gateway" else "custom")
        fx = agentcost.fx_usd_per_chf()
    except Exception:  # noqa: BLE001
        return None
    if not p:
        return None
    k = fx if p["currency"] == "CHF" else 1.0
    return {"input": p["input"] * k, "output": p["output"] * k,
            "cache_read": p["cache_read"] * k, "cache_write": p["cache_write"] * k,
            "currency": p["currency"], "source": p["source"]}


def _include_external() -> bool:
    return (os.environ.get("SOKKAN_USAGE_EXTERNAL") or "").strip().lower() in ("include", "1", "on", "true")


# --------------------------------------------------------------------------- queries

def _known_sessions() -> dict:
    import board
    known = {}
    for s in board.list_sessions():
        if s.get("claude_session_id"):
            known[s["claude_session_id"]] = s
        known[s["session_id"]] = s
    return known


def _files(con, known: dict) -> dict[str, dict]:
    """path → {session_id, proj, source, session, …} for every cached transcript."""
    out = {}
    for r in con.execute("SELECT * FROM files"):
        s = known.get(r["session_id"])
        if s:
            src, proj = "sokkan", s.get("project") or "default"
        elif (r["project"] or "default") != "default":
            src, proj = "sokkan", r["project"]   # per-project workspaces are SOKKAN's own
        else:
            src, proj = "external", "default"
        out[r["path"]] = {**dict(r), "source": src, "proj": proj, "session": s}
    return out


def _price_row(r: dict, basis: str, other: dict) -> dict:
    """API-equivalent and billed cost of one (file, day, model) row."""
    api_eq = (r["cost_in"] + r["cost_out"] + r["cost_cr"] + r["cost_cw"]) if r["priced"] else 0.0
    if basis == "api":
        billed, priced = api_eq, bool(r["priced"])
    elif basis == "subscription":
        billed, priced = 0.0, bool(r["priced"])
    elif basis == "local":
        billed, priced = 0.0, True
    else:   # gateway / custom
        p = other.get(r["model"])
        if p:
            billed = (r["in_tokens"] * p["input"] + r["out_tokens"] * p["output"]
                      + r["cache_read"] * p["cache_read"]
                      + (r["cw_5m"] + r["cw_1h"]) * p["cache_write"]) / 1e6
            priced = True
        else:
            billed, priced = 0.0, False
    metered = api_eq if basis == "subscription" else billed
    return {"api_equiv": api_eq, "billed": billed, "metered": metered, "priced_basis": priced}


def _scan(project: str | None) -> dict:
    refresh()
    con = _con()
    try:
        known = _known_sessions()
        files = _files(con, known)
        rows = [dict(r) for r in con.execute("SELECT * FROM rows")]
    finally:
        con.close()
    b = billing()
    local_ids = _local_model_ids()
    models = {r["model"] for r in rows}
    bases = {m: basis_for(m, b, local_ids) for m in models}
    other = {m: _other_price(m, bases[m]) for m in models if bases[m] in ("gateway", "custom")}
    out = []
    for r in rows:
        f = files.get(r["path"])
        if f is None:
            continue
        if project is not None and f["proj"] != project:
            continue
        basis = bases[r["model"]]
        out.append({**r, **_price_row(r, basis, other), "basis": basis,
                    "source": f["source"], "project": f["proj"]})
    return {"rows": out, "files": files, "billing": b, "bases": bases, "other": other}


def _counted(r: dict, include_external: bool) -> bool:
    return r["source"] == "sokkan" or include_external


def project_spend(project: str) -> dict:
    """Metered spend of a project, USD: today and this month (SOKKAN_TZ) — for the
    per-project budgets (lot 4). SOKKAN sessions only (unless SOKKAN_USAGE_EXTERNAL)."""
    now = datetime.now(TZ)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-01")
    inc = _include_external()
    rows = [r for r in _scan(project)["rows"] if _counted(r, inc)]
    return {"day": sum(r["metered"] for r in rows if r["day"] >= day),
            "month": sum(r["metered"] for r in rows if r["day"] >= month)}


def _sum(rows: list[dict]) -> dict:
    t = {"turns": 0, "in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0,
         "cost_in": 0.0, "cost_out": 0.0, "cost_cr": 0.0, "cost_cw": 0.0,
         "api_equiv": 0.0, "billed": 0.0, "metered": 0.0}
    for r in rows:
        t["turns"] += r["turns"]
        t["in_tokens"] += r["in_tokens"]
        t["out_tokens"] += r["out_tokens"]
        t["cache_read"] += r["cache_read"]
        t["cache_write"] += r["cw_5m"] + r["cw_1h"]
        if r["priced"]:
            for k in ("cost_in", "cost_out", "cost_cr", "cost_cw"):
                t[k] += r[k]
        t["api_equiv"] += r["api_equiv"]
        t["billed"] += r["billed"]
        t["metered"] += r["metered"]
    return t


def _gateway_ledger() -> dict | None:
    try:
        import llm
        u = llm.usage()
    except Exception:  # noqa: BLE001
        return None
    if not u:
        return None
    keep = ("client", "day", "used_today", "used_month", "spent_today_centimes",
            "spent_month_centimes", "balance_centimes")
    return {**{k: u.get(k) for k in keep if k in u}, "currency": "CHF"}


def _r(v: float) -> float:
    return round(v, 6)


def summary(days_back: int = 30, project: str | None = None) -> dict:
    """Operate › Costs. `project` (3.2 lot 4): THAT project only; None = the instance.

    totals[*].cost = BILLED (what is actually charged per token), .api_equiv = the same
    tokens at the public API price, .metered = what budgets count. Every figure is for
    SOKKAN sessions; external transcripts are in `sources.external` (and counted only with
    SOKKAN_USAGE_EXTERNAL=include)."""
    s = _scan(project)
    inc = _include_external()
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
    week = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    month30 = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    counted = [r for r in s["rows"] if _counted(r, inc)]
    period = [r for r in counted if r["day"] >= start]

    def tot(rows):
        t = _sum(rows)
        return {"cost": _r(t["billed"]), "api_equiv": _r(t["api_equiv"]),
                "metered": _r(t["metered"]), "out_tokens": t["out_tokens"],
                "turns": t["turns"]}

    totals = {"today": tot([r for r in counted if r["day"] >= today]),
              "7d": tot([r for r in counted if r["day"] >= week]),
              "30d": tot([r for r in counted if r["day"] >= month30]),
              "period": tot(period), "all": tot(counted)}

    days: dict[str, list] = {}
    for r in period:
        days.setdefault(r["day"], []).append(r)
    day_rows = []
    for d in sorted(days):
        t = _sum(days[d])
        day_rows.append({"day": d, "turns": t["turns"], "in_tokens": t["in_tokens"],
                         "out_tokens": t["out_tokens"], "cost": _r(t["billed"]),
                         "api_equiv": _r(t["api_equiv"])})

    by_basis = []
    for basis in BASES:
        rs = [r for r in period if r["basis"] == basis]
        if not rs:
            continue
        t = _sum(rs)
        by_basis.append({"basis": basis, "label": BASIS_LABEL[basis], "method": BASIS_METHOD[basis],
                         "billed": _r(t["billed"]), "api_equiv": _r(t["api_equiv"]),
                         "turns": t["turns"], "in_tokens": t["in_tokens"],
                         "out_tokens": t["out_tokens"], "cache_read": t["cache_read"],
                         "cache_write": t["cache_write"],
                         "unpriced_turns": sum(r["turns"] for r in rs if not r["priced_basis"])})

    by_model = []
    for model in sorted({r["model"] for r in period}):
        rs = [r for r in period if r["model"] == model]
        t = _sum(rs)
        p = pricing.price(model)
        by_model.append({"model": model, "priced_as": p["model"] if p else None,
                         "basis": s["bases"][model],
                         "priced": all(r["priced_basis"] for r in rs),
                         **{k: (_r(v) if isinstance(v, float) else v) for k, v in t.items()},
                         "price": p or s["other"].get(model)})
    by_model.sort(key=lambda m: (-m["api_equiv"], -m["out_tokens"]))

    by_project = []
    for proj in sorted({r["project"] for r in period}):
        t = _sum([r for r in period if r["project"] == proj])
        by_project.append({"project": proj, "billed": _r(t["billed"]),
                           "api_equiv": _r(t["api_equiv"]), "turns": t["turns"],
                           "out_tokens": t["out_tokens"]})
    by_project.sort(key=lambda p: (-p["api_equiv"], p["project"]))

    sources = {}
    for src in ("sokkan", "external"):
        rs = [r for r in s["rows"] if r["source"] == src and r["day"] >= start]
        t = _sum(rs)
        sources[src] = {"transcripts": len({r["path"] for r in rs}), "turns": t["turns"],
                        "out_tokens": t["out_tokens"], "billed": _r(t["billed"]),
                        "api_equiv": _r(t["api_equiv"]),
                        "counted": src == "sokkan" or inc}

    # one line per session: its own transcript + the transcripts of its sub-agents
    per_sid: dict[str, list] = {}
    for r in period:
        per_sid.setdefault(s["files"][r["path"]]["session_id"], []).append(r)
    sessions = []
    for sid, rs in per_sid.items():
        fs = [s["files"][p] for p in {r["path"] for r in rs}]
        main = next((f for f in fs if Path(f["path"]).stem == sid), fs[0])
        t = _sum(rs)
        sess = main["session"] or {}
        sessions.append({
            "session_id": sid, "source": main["source"],
            "title": sess.get("title") or main["first_prompt"] or sid[:8],
            "tag": sess.get("tag", ""), "project": main["proj"],
            "models": ",".join(sorted({r["model"] for r in rs})),
            "bases": sorted({r["basis"] for r in rs}),
            "subagents": sum(1 for f in fs if "/subagents/" in f["path"]),
            "turns": t["turns"], "in_tokens": t["in_tokens"], "out_tokens": t["out_tokens"],
            "cache_read": t["cache_read"], "cost": _r(t["billed"]),
            "api_equiv": _r(t["api_equiv"]), "last_ts": max(f["last_ts"] or 0 for f in fs),
        })
    sessions.sort(key=lambda x: (-x["api_equiv"], -x["out_tokens"]))

    b = s["billing"]
    pv = pricing.version()
    return {
        "period": {"days": days_back, "from": start, "to": today},
        "pricing": pv,
        "billing": {**b, "claude_label": BASIS_LABEL[b["claude"]],
                    "claude_method": BASIS_METHOD[b["claude"]]},
        "totals": totals, "days": day_rows, "by_basis": by_basis, "by_model": by_model,
        "by_project": by_project, "sources": sources, "sessions": sessions[:25],
        "include_external": inc,
        "gateway": _gateway_ledger() if any(x["basis"] == "gateway" for x in by_basis) else None,
        "unpriced_models": [m["model"] for m in by_model if not m["priced"]],
        "project": project,
        "note": (f"{BASIS_LABEL[b['claude']]}: {BASIS_METHOD[b['claude']]}. "
                 f"Prices: table of {pv['version']}. SOKKAN sessions only"
                 + (" + external transcripts (SOKKAN_USAGE_EXTERNAL)" if inc else "") + "."),
    }
