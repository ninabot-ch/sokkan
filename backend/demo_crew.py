#!/usr/bin/env python3
"""demo_crew.py — the Crew of the PUBLIC READ-ONLY DEMO (3.1.1). Never anywhere else.

Two things, both inert unless the instance is the demo:

1. `seed <file.json>` — writes a fictional crew (agents + a plausible run history)
   into agents.db. Idempotent: an agent is matched by name and rewritten, its demo
   runs (runner `demo-seed` / `demo-sim`) are replaced; anything else is left alone.

2. `DemoRuntime` — replaces the scheduler when SOKKAN_DEMO_CREW=1 AND the demo
   banner is on (SOKKAN_DEMO_BANNER=1). It NEVER starts an SDK session and never
   calls a model: a due cron occurrence, or an agent seeded with `loop`, gets a
   *simulated* run that replays a recorded script (`context.simulated`), shown as
   such in the Live tab. So the deck lives (a card breathes, armed agents fire at
   their hour) at zero inference cost.

Spec: docs/AGENTS.md § Public demo.
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

import agents
import cronexpr

SEED_RUNNER = "demo-seed"
SIM_RUNNER = "demo-sim"
KEEP_RUNS = 40  # simulated history kept per agent (older demo runs are pruned)
DEFAULT_LIVE_S = 150


def requested() -> bool:
    """Asked for in the environment (whatever its dependencies say)."""
    import features
    return bool(features.requested("demo_crew"))


def enabled() -> bool:
    """Demo crew = asked for (SOKKAN_DEMO_CREW=1) AND on the demo instance (banner on).
    One without the other is a misconfiguration: the real scheduler runs."""
    import features  # registry: `demo_crew` requires `agents` and `demo_banner`
    return features.enabled("demo_crew")


# ---- per-agent demo config (live script, loop) ---------------------------------
_CFG_SCHEMA = """CREATE TABLE IF NOT EXISTS demo_crew (
    agent_id INTEGER PRIMARY KEY, config TEXT NOT NULL DEFAULT '{}')"""


def _con() -> sqlite3.Connection:
    con = agents._con()
    con.execute(_CFG_SCHEMA)
    return con


def config(agent_id: int) -> dict:
    con = _con()
    try:
        r = con.execute("SELECT config FROM demo_crew WHERE agent_id=?", (agent_id,)).fetchone()
    finally:
        con.close()
    try:
        return json.loads(r["config"]) if r else {}
    except ValueError:
        return {}


# ---- seed -----------------------------------------------------------------------
class SeedError(ValueError):
    pass


_RUN_FIELDS = ("status", "trigger", "cost_usd", "tokens_in", "tokens_out", "num_turns",
               "deliverable", "error", "requested_by")


def _past_occurrences(a: dict, n: int, now: float) -> list[float]:
    """The n most recent cron occurrences ≤ now (oldest first)."""
    cron = cronexpr.parse(a["schedule"])
    tz = a.get("timezone") or cronexpr.DEFAULT_TZ
    out: list[float] = []
    t = now - 70 * 86400
    while True:
        t = cron.next_after(t, tz)
        if t > now:
            break
        out.append(t)
    return out[-n:]


def check(spec: dict) -> list[dict]:
    """Validate a seed file. Every agent goes through the same validation as the API,
    and a copy-paste between agents (same purpose / deliverable / done criteria) is
    refused — the demo shows these texts to every visitor."""
    items = spec.get("agents")
    if not isinstance(items, list) or not items:
        raise SeedError("seed: 'agents' must be a non-empty list")
    seen: dict[str, dict[str, str]] = {"purpose": {}, "deliverable": {}, "done_criteria": {}}
    out = []
    for it in items:
        fields = {k: v for k, v in it.items() if k in agents._EDITABLE}
        try:
            v = agents.validate(fields, partial=False)
        except agents.AgentError as e:
            raise SeedError(f"{it.get('name')}: {e}") from None
        if not v.get("done_criteria"):
            raise SeedError(f"{v['name']}: done_criteria is required in a demo seed")
        for k in seen:
            key = " ".join(v[k].lower().split())
            if key in seen[k]:
                raise SeedError(f"{v['name']}: same {k} as {seen[k][key]} — copy-paste?")
            seen[k][key] = v["name"]
        status = it.get("status", "active")
        if status not in agents.STATUSES:
            raise SeedError(f"{v['name']}: status {status!r}")
        merged = {**{"auto_approve": [], "schedule": "", "event": "", "once_at": None}, **v}
        try:
            agents._check_trigger(merged)
        except agents.AgentError as e:
            raise SeedError(f"{v['name']}: {e}") from None
        for r in it.get("runs") or []:
            if r.get("status") not in agents.RUN_FINAL:
                raise SeedError(f"{v['name']}: a seeded run must be finished, not {r.get('status')!r}")
            if "occurrence" in r and v["trigger"] != "cron":
                raise SeedError(f"{v['name']}: 'occurrence' needs a cron agent")
        out.append({"fields": v, "item": it, "status": status})
    return out


def seed(spec: dict, now: float | None = None) -> list[dict]:
    """Write the crew. Returns [{name, id, deck}]. Idempotent (see module doc)."""
    now = time.time() if now is None else now
    plan = check(spec)
    report = []
    for p in plan:
        v, it, status = p["fields"], p["item"], p["status"]
        row = {
            "timezone": cronexpr.DEFAULT_TZ, "auto_approve": [], "secrets": [], "budget_usd": 0.0,
            "max_minutes": 30, "model": "", "playbook": "", "schedule": "", "event": "",
            "once_at": None, "notify_on": ["failure", "timeout", "budget", "approval"], **v,
            "owner": it.get("owner") or "demo@sokkan.ch",
            "status": status, "pending_change": None,
            "created_by": it.get("created_by") or f"user:{it.get('owner') or 'demo@sokkan.ch'}",
            "proposed_by": it.get("owner") or "demo@sokkan.ch", "pending_change_by": "",
            "approved_by": "", "approved_at": None, "last_run_at": None,
            "created_at": now - float(it.get("created_days_ago", 30)) * 86400, "updated_at": now,
        }
        if status in ("active", "paused"):
            row["approved_by"] = it.get("approved_by") or row["owner"]
            row["approved_at"] = row["created_at"] + 600
        row["next_run_at"] = agents.next_fire(row, now)
        cols = [c for c in row if c != "id"]
        vals = [json.dumps(row[c]) if c in agents._JSON_FIELDS else row[c] for c in cols]
        con = _con()
        try:
            ex = con.execute("SELECT id FROM agents WHERE name=?", (v["name"],)).fetchone()
            if ex:
                aid = ex["id"]
                con.execute(f"UPDATE agents SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                            (*vals, aid))
                con.execute("DELETE FROM runs WHERE agent_id=? AND runner IN (?,?)",
                            (aid, SEED_RUNNER, SIM_RUNNER))
            else:
                aid = con.execute(f"INSERT INTO agents({', '.join(cols)}) VALUES"
                                  f"({', '.join('?' * len(cols))})", vals).lastrowid
            demo_cfg = {"loop": bool(it.get("loop")), "live": it.get("live") or {}}
            con.execute("INSERT OR REPLACE INTO demo_crew(agent_id, config) VALUES(?,?)",
                        (aid, json.dumps(demo_cfg)))
            runs = it.get("runs") or []
            occ = []
            n_occ = sum(1 for r in runs if "occurrence" in r)
            if n_occ:
                occ = _past_occurrences(row, max(r.get("occurrence", 0) for r in runs) + 1, now)
            last = None
            for r in runs:
                if "occurrence" in r:
                    k = int(r["occurrence"])  # 0 = the latest past occurrence, 1 = the one before…
                    if k >= len(occ):
                        continue
                    start = occ[-1 - k] + float(r.get("delay_s", 4))
                    sched = occ[-1 - k]
                else:
                    start = now - float(r.get("ago_h", 1)) * 3600
                    sched = None
                end = start + float(r.get("duration_s", 120))
                d = {k: r.get(k) for k in _RUN_FIELDS if k in r}
                d.setdefault("trigger", "schedule" if sched else "manual")
                d.setdefault("requested_by", "scheduler" if sched else row["owner"])
                outputs = {}
                if d.get("deliverable") and d["status"] in ("succeeded", "incomplete"):
                    outputs = _seed_outputs(row, d["deliverable"])
                con.execute(
                    "INSERT INTO runs(agent_id, trigger, scheduled_for, status, runner, cost_usd,"
                    " tokens_in, tokens_out, num_turns, deliverable, outputs, error, context,"
                    " requested_by, created_at, started_at, ended_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, d["trigger"], sched, d["status"], SEED_RUNNER, d.get("cost_usd", 0),
                     d.get("tokens_in", 0), d.get("tokens_out", 0), d.get("num_turns", 0),
                     d.get("deliverable", ""), json.dumps(outputs), d.get("error", ""),
                     json.dumps(r.get("context") or {}), d["requested_by"], start, start, end))
                last = max(last or 0, end)
            if last:
                con.execute("UPDATE agents SET last_run_at=? WHERE id=?", (last, aid))
        finally:
            con.close()
        full = next((x for x in agents.list_agents({"email": "", "role": "owner"},
                                                   include_archived=True) if x["id"] == aid), {})
        report.append({"name": v["name"], "id": aid, "deck": full.get("deck")})
    return report


def _seed_outputs(a: dict, deliverable: str) -> dict:
    # Honest outputs only: no board card or memory note is invented for a demo run.
    out: dict = {}
    if "notify" in (a.get("outputs") or []):
        out["notify"] = True
    return out


# ---- the simulated runtime ---------------------------------------------------------
class DemoRuntime:
    """Same surface as agents_runtime.Runtime; never touches the SDK or a model."""

    def __init__(self, recall=None):
        self.runner_id = SIM_RUNNER
        self.tasks: dict = {}
        self.sessions: dict = {}
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        self._rng = random.Random()

    def start(self) -> None:
        print("[agents] DEMO CREW: simulated runs only — no session, no model call",
              file=sys.stderr)
        self._wake = asyncio.Event()
        self.recover()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()

    def poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def recover(self) -> None:
        # a REAL run left running by a previous process is interrupted, as usual;
        # simulated runs simply carry on (they are a function of the clock)
        for r in agents.runs_with_status("running", "queued"):
            if r["runner"] != SIM_RUNNER:
                agents.update_run(r["id"], status="interrupted", waiting_approval=0,
                                  error="demo instance: agents do not really run here")

    async def _loop(self) -> None:
        from agents_runtime import TICK_S
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 — the loop never dies
                print(f"[agents demo] tick failed: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                assert self._wake is not None
                await asyncio.wait_for(self._wake.wait(), min(TICK_S, 5))
            except asyncio.TimeoutError:
                pass
            if self._wake is not None:
                self._wake.clear()

    async def tick(self, now: float | None = None) -> None:
        self.step(time.time() if now is None else now)

    def step(self, now: float) -> None:
        """One pass: finish simulated runs whose time is up, fire due occurrences as
        simulated runs, keep `loop` agents running, prune the history."""
        for r in agents.runs_with_status("running", "queued"):
            if r["runner"] != SIM_RUNNER:
                # nothing real runs on the demo (a run queued by hand is refused)
                agents.update_run(r["id"], status="cancelled",
                                  error="demo instance: agents do not really run here")
                continue
            dur = float((r.get("context") or {}).get("duration_s") or DEFAULT_LIVE_S)
            if now - (r["started_at"] or now) >= dur:
                self._finish(r, now)
        for a in agents.due_agents(now):
            if not agents.active_run(a["id"]):
                self._start(a, "schedule", now, scheduled_for=a["next_run_at"])
            agents.set_next_run(a["id"], agents.next_fire(a, now), last_run_at=now)
        con = _con()
        try:
            loops = [r["agent_id"] for r in con.execute("SELECT agent_id, config FROM demo_crew")
                     if json.loads(r["config"] or "{}").get("loop")]
        finally:
            con.close()
        for aid in loops:
            a = agents.get(aid)
            if a and a["status"] == "active" and not agents.active_run(aid):
                self._start(a, "schedule", now)  # a schedule-driven agent, replayed back to back
        self._prune()

    def _template(self, a: dict) -> dict:
        """What a simulated run of `a` replays: the seeded live script, else the
        latest succeeded run (deliverable, cost) with generic steps."""
        live = dict(config(a["id"]).get("live") or {})
        if not live.get("deliverable"):
            con = _con()
            try:
                r = con.execute("SELECT * FROM runs WHERE agent_id=? AND status='succeeded'"
                                " ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
            finally:
                con.close()
            if r:
                live.setdefault("deliverable", r["deliverable"])
                live.setdefault("cost_usd", r["cost_usd"])
                live.setdefault("tokens_in", r["tokens_in"])
                live.setdefault("tokens_out", r["tokens_out"])
                live.setdefault("num_turns", r["num_turns"])
                if r["started_at"] and r["ended_at"]:
                    live.setdefault("duration_s", min(240.0, r["ended_at"] - r["started_at"]))
        live.setdefault("duration_s", DEFAULT_LIVE_S)
        live.setdefault("steps", [
            {"at": 2, "kind": "recall", "text": f"Recalled the project notes for {a['name']}."},
            {"at": 10, "kind": "text", "text": "Working on the mission — " + a["purpose"][:160]},
            {"at": max(20.0, float(live["duration_s"]) - 20), "kind": "text",
             "text": "Writing the deliverable."},
        ])
        return live

    def _start(self, a: dict, trigger: str, now: float, scheduled_for: float | None = None) -> None:
        t = self._template(a)
        ctx = {"simulated": True, "duration_s": float(t["duration_s"]), "steps": t["steps"]}
        r = agents.enqueue_run(a["id"], trigger, "scheduler" if trigger == "schedule" else a["owner"],
                               scheduled_for=scheduled_for, context=ctx)
        if r is None:
            return
        agents.update_run(r["id"], status="running", runner=SIM_RUNNER, started_at=now)

    def _finish(self, r: dict, now: float) -> None:
        a = agents.get(r["agent_id"]) or {}
        t = self._template(a) if a else {}
        jitter = 1 + self._rng.uniform(-0.12, 0.12)
        agents.update_run(
            r["id"], status="succeeded", ended_at=(r["started_at"] or now)
            + float((r.get("context") or {}).get("duration_s") or DEFAULT_LIVE_S),
            deliverable=t.get("deliverable") or "Done.\nDELIVERY: done",
            outputs=_seed_outputs(a, "") if a else {},
            cost_usd=round(float(t.get("cost_usd") or 0) * jitter, 4),
            tokens_in=int(float(t.get("tokens_in") or 0) * jitter),
            tokens_out=int(float(t.get("tokens_out") or 0) * jitter),
            num_turns=int(t.get("num_turns") or 0))
        if a:
            agents.set_next_run(a["id"], a.get("next_run_at"), last_run_at=now)

    def _prune(self) -> None:
        con = _con()
        try:
            con.execute(
                "DELETE FROM runs WHERE runner IN (?,?) AND status NOT IN ('running','queued')"
                " AND id NOT IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER"
                " (PARTITION BY agent_id ORDER BY id DESC) AS n FROM runs) WHERE n <= ?)",
                (SEED_RUNNER, SIM_RUNNER, KEEP_RUNS))
        finally:
            con.close()

    def fire_event(self, kind: str, name: str = "", context: dict | None = None) -> list[int]:
        return []  # alerts never start a run on the demo

    def cancel(self, run_id: int) -> bool:
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("seed", "check"):
        print("usage: demo_crew.py check|seed <crew.json> [--force]", file=sys.stderr)
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if argv[0] == "check":
        for p in check(spec):
            print(f"ok  {p['fields']['name']} ({p['status']})")
        return 0
    if not requested() and "--force" not in argv:
        print("refused: SOKKAN_DEMO_CREW is not set — this seeds a FICTIONAL crew, meant for "
              "the public demo only (--force to seed anyway)", file=sys.stderr)
        return 1
    for r in seed(spec):
        print(f"{r['id']:>4}  {r['deck']:<8} {r['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
