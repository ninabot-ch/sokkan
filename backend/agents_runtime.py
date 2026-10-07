#!/usr/bin/env python3
"""agents_runtime.py — SOKKAN 3.1 "Crew up": the scheduler and the run executor.

One asyncio loop inside the API process (spec: docs/AGENTS.md § Scheduler):
- due cron / one-shot occurrences become runs (UNIQUE(agent_id, scheduled_for) →
  never twice, even with two loops), missed occurrences while the API was down get
  ONE catch-up run if recent enough;
- queued runs (scheduler, UI, MCP from another process, alert events) are started
  through an atomic claim, at most one live run per agent and
  SOKKAN_AGENTS_MAX_CONCURRENT overall;
- a run = an ordinary SDK session (agentchat.AgentSession) with the agent's policy
  (tools, auto-approvals, vault secrets by name, budget), the memory recall block
  and the agent's mission as first message;
- at the end: status, cost/tokens, deliverable (secret values redacted) filed to
  the agent's outputs, notifications, audit.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import agentchat
import agents
import audit
import board
import cronexpr
import notify
import observability
import playbooks
import quarantine
import vault

TICK_S = float(os.environ.get("SOKKAN_AGENTS_TICK_S", "15"))
MAX_CONCURRENT = max(1, int(os.environ.get("SOKKAN_AGENTS_MAX_CONCURRENT", "2")))
MISFIRE_S = float(os.environ.get("SOKKAN_AGENTS_MISFIRE_S", str(6 * 3600)))
DATA_DIR = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))
_DELIVERY_RE = re.compile(r"^\s*\**DELIVERY\**\s*:\s*\**\s*(done|incomplete)\b[\s\-—:–]*(.*)$",
                          re.IGNORECASE | re.MULTILINE)


def enabled() -> bool:
    return os.environ.get("SOKKAN_FEATURE_AGENTS", "1") != "0"


INCIDENT_STATUSES = ("failed", "timeout", "budget")


def incidents_enabled() -> bool:
    """SOKKAN_AGENTS_INCIDENTS=1 : a run ending failed / timeout / budget opens (or
    joins) the agent's incident in Operate. Off by default."""
    return (os.environ.get("SOKKAN_AGENTS_INCIDENTS") or "0").strip().lower() in (
        "1", "true", "yes", "on")


def demo_mode() -> bool:
    """Public demo only (SOKKAN_DEMO_CREW=1 + demo banner): simulated runs, no model."""
    import demo_crew
    return enabled() and demo_crew.enabled()


# ---- prompt -------------------------------------------------------------------
def build_prompt(a: dict, run: dict, recall: str = "", now: float | None = None) -> str:
    tz = a.get("timezone") or cronexpr.DEFAULT_TZ
    when = datetime.fromtimestamp(now or time.time(), ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
    mission = a["purpose"]
    if a.get("playbook"):
        r = playbooks.render(a["playbook"], a["purpose"])
        if r:
            mission = r[0]
    tools = ", ".join(a.get("tools") or []) or "none"
    if a.get("auto_approve"):
        gate = ("These run without asking: " + ", ".join(a["auto_approve"]) + ". Any other "
                "mutating call waits for a human approval (the owner is pinged).")
    else:
        gate = ("Every mutating call (writes, shell commands that change state) waits for a "
                "human approval — the owner is pinged, the run waits. Prefer read-only "
                "investigation and put the changes you recommend in your deliverable.")
    lines = [
        f'You are the SOKKAN agent "{a["name"]}", running unattended — run #{run["id"]}, '
        f'trigger: {run["trigger"]}, {when}. Owner: {a["owner"]}.',
        "", "## Mission", mission.strip(),
        "", "## Expected deliverable", a["deliverable"].strip(),
        "", "## Done when",
        (a.get("done_criteria") or "the deliverable above is complete and accurate.").strip(),
        "", "## What you have",
        f"- Tools: {tools}. Tools outside this list are refused.",
        f"- {gate}",
    ]
    if a.get("secrets"):
        lines.append("- Secrets, as environment variables: "
                     + ", ".join(f"${s}" for s in a["secrets"])
                     + ". Use them in commands ($NAME) — never print, echo, log or write "
                       "their values anywhere, including your deliverable.")
    else:
        lines.append("- No secrets are available to this agent.")
    limits = [f"{a.get('max_minutes') or 30} minutes"]
    if a.get("budget_usd"):
        limits.insert(0, f"${a['budget_usd']:.2f}")
    lines.append(f"- Limits for this run: {' and '.join(limits)}. Stay well inside them.")
    ctx = run.get("context") or {}
    if ctx:
        lines += ["", "## Event that triggered this run", str(ctx)[:2000]]
    if recall:
        lines += ["", recall,
                  "The notes above were auto-recalled from project memory for this mission; "
                  "use mcp__sokkan-memory__memory_get / memory_search for more."]
    else:
        lines += ["", "Start with mcp__sokkan-memory__memory_search on this mission to load "
                      "the project context."]
    lines += [
        "", "## How to finish",
        "Your final message IS the deliverable (markdown); it is filed to: "
        + ", ".join(a.get("outputs") or ["card"]) + ". Nobody is chatting with you: do not "
        "ask questions, decide and note open points in the deliverable. End with one last line:",
        "DELIVERY: done",
        "or",
        "DELIVERY: incomplete — <why>",
    ]
    return "\n".join(lines)


def parse_delivery(text: str) -> tuple[str | None, str]:
    """→ ('done'|'incomplete'|None, reason) from the LAST DELIVERY: line."""
    m = None
    for m in _DELIVERY_RE.finditer(text or ""):
        pass
    if not m:
        return None, ""
    return m.group(1).lower(), m.group(2).strip()


def _cron_latest_due(a: dict, now: float) -> float | None:
    """Most recent occurrence ≤ now, starting from the stored next_run_at."""
    try:
        cron = cronexpr.parse(a["schedule"])
    except cronexpr.CronError:
        return None
    tz = a.get("timezone") or cronexpr.DEFAULT_TZ
    occ = a["next_run_at"]
    for _ in range(100000):
        nxt = cron.next_after(occ, tz)
        if nxt > now:
            return occ
        occ = nxt
    return occ


class Runtime:
    def __init__(self, recall: Callable[[str, str], str] | None = None):
        self.runner_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.recall = recall
        self.tasks: dict[int, asyncio.Task] = {}
        self.sessions: dict[int, agentchat.AgentSession] = {}
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        self._cancelled: set[int] = set()
        self._stopping = False

    # ---- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._wake = asyncio.Event()
        self.recover()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
        for t in list(self.tasks.values()):
            t.cancel()

    def poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def recover(self) -> None:
        """Runs left `running` by a previous process cannot be resumed mid-turn:
        mark them interrupted (+ notification); queued ones will be picked up."""
        for r in agents.runs_with_status("running"):
            if r["runner"] == self.runner_id:
                continue
            agents.update_run(r["id"], status="interrupted", waiting_approval=0,
                              error="the SOKKAN API restarted during this run")
            a = agents.get(r["agent_id"])
            if a:
                audit.log(f"agent:{a['name']}", "agent.run.interrupted", f"run #{r['id']}", "")
                self._notify(a, r, "interrupted", "The API restarted during this run.")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 — the scheduler never dies
                print(f"[agents] tick failed: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                assert self._wake is not None
                await asyncio.wait_for(self._wake.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass
            if self._wake is not None:
                self._wake.clear()

    # ---- one tick -------------------------------------------------------------
    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.schedule_due(now)
        self.start_queued()
        await self.watchdog(time.time())  # started_at is wall-clock: never a fake `now`

    def schedule_due(self, now: float) -> None:
        for a in agents.due_agents(now):
            if a["trigger"] == "cron":
                occ = _cron_latest_due(a, now)
            else:
                occ = a["next_run_at"]
            if occ is None:
                agents.set_next_run(a["id"], None)
                continue
            if now - occ > MISFIRE_S:
                agents.enqueue_run(a["id"], "schedule", "scheduler", scheduled_for=occ,
                                   status="skipped", error="missed while the API was down")
            elif agents.active_run(a["id"]):
                agents.enqueue_run(a["id"], "schedule", "scheduler", scheduled_for=occ,
                                   status="skipped", error="previous run still going")
            else:
                agents.enqueue_run(a["id"], "schedule", "scheduler", scheduled_for=occ)
            nxt = None if a["trigger"] == "once" else agents.next_fire(a, now)
            agents.set_next_run(a["id"], nxt, last_run_at=now)

    def start_queued(self) -> None:
        live_agents = {r["agent_id"] for r in agents.runs_with_status("running")}
        for r in agents.runs_with_status("queued"):
            if len(self.tasks) >= MAX_CONCURRENT:
                return
            if r["agent_id"] in live_agents:
                continue  # one live run per agent; it waits its turn
            a = agents.get(r["agent_id"])
            if not a or a["status"] not in ("active",):
                agents.update_run(r["id"], status="cancelled",
                                  error=f"agent is {a['status'] if a else 'gone'}")
                continue
            if not agents.claim_run(r["id"], self.runner_id):
                continue  # someone else took it
            live_agents.add(r["agent_id"])
            run = agents.get_run(r["id"])
            t = asyncio.create_task(self._execute(a, run))
            self.tasks[r["id"]] = t
            t.add_done_callback(lambda _t, rid=r["id"]: self._done(rid, _t))

    def _done(self, rid: int, task: asyncio.Task) -> None:
        """A run task ended. If it died before recording an end state (cancelled
        before its first step, unexpected exception), never leave it `running`."""
        self.tasks.pop(rid, None)
        r = agents.get_run(rid)
        if r and r["status"] == "running":
            why = ("cancelled before it started" if task.cancelled()
                   else f"runner error: {task.exception()!r}"[:500])
            agents.update_run(rid, status="cancelled" if rid in self._cancelled else "failed",
                              error=why, waiting_approval=0)

    async def watchdog(self, now: float) -> None:
        for r in agents.runs_with_status("running"):
            if r["runner"] != self.runner_id or r["id"] not in self.tasks:
                continue
            a = agents.get(r["agent_id"]) or {}
            limit = float(a.get("max_minutes") or 30) * 60
            if r["started_at"] and now - r["started_at"] > limit:
                self.tasks[r["id"]].cancel()  # _execute records the timeout

    # ---- events ----------------------------------------------------------------
    def fire_event(self, kind: str, name: str = "", context: dict | None = None) -> list[int]:
        """An Operate alert (or any future event) → runs of the matching agents."""
        out = []
        for a in agents.event_agents(kind):
            pat = a["event"].split(":", 1)[1] if ":" in a["event"] else "*"
            if not fnmatch.fnmatch(name or "", pat):
                continue
            if agents.active_run(a["id"]):
                agents.enqueue_run(a["id"], "event", f"event:{kind}", context=context,
                                   status="skipped", error="previous run still going")
                continue
            r = agents.enqueue_run(a["id"], "event", f"event:{kind}", context=context)
            if r:
                out.append(r["id"])
        self.poke()
        return out

    def cancel(self, run_id: int) -> bool:
        t = self.tasks.get(run_id)
        if t:
            self._cancelled.add(run_id)
            t.cancel()
            return True
        r = agents.get_run(run_id)
        if r and r["status"] == "queued":
            agents.update_run(run_id, status="cancelled", error="cancelled before start")
            return True
        return False

    # ---- a run -----------------------------------------------------------------
    async def _execute(self, a: dict, run: dict) -> None:
        rid = run["id"]
        actor = f"agent:{a['name']}"
        secret_vals = vault.session_env(a.get("secrets") or [])
        missing = [s for s in a.get("secrets") or [] if s not in secret_vals]
        if missing:
            agents.update_run(rid, status="failed",
                              error=f"secrets missing from the vault: {', '.join(missing)}")
            if not self._incident(a, agents.get_run(rid)):
                self._notify(a, agents.get_run(rid), "failed",
                             f"Secrets missing from the vault: {', '.join(missing)}")
            return
        sid = agentchat.new_sid()
        board.add_sdk_session(sid, "agent", title=f"agent {a['name']} · run #{rid}",
                              prompt=a["purpose"][:300])

        def on_wait(waiting: bool) -> None:
            agents.update_run(rid, waiting_approval=int(waiting))

        policy = {
            "agent": a["name"], "run": rid,
            "tools": a.get("tools") or [],
            "auto_approve": a.get("auto_approve") or [],
            "secrets": a.get("secrets") or [],
            "budget_usd": a.get("budget_usd") or 0,
            "mcp": list(dict.fromkeys([*(a.get("mcp") or ["sokkan-memory"]), "sokkan-agents"])),
            "on_wait": on_wait,
        }
        sess = agentchat.get_or_create(sid, user=a["owner"], model=a.get("model") or None,
                                       policy=policy)
        self.sessions[rid] = sess
        agents.update_run(rid, session_id=sid)
        audit.log(actor, "agent.run.start", f"run #{rid}", f"{run['trigger']} · session {sid}")
        recall = ""
        if self.recall:
            try:
                recall = self.recall(f"{a['name']} {a['purpose']}"[:1000], sid) or ""
            except Exception:  # noqa: BLE001 — memory down never blocks a run
                recall = ""
        prompt = build_prompt(a, run, recall)
        status, error = "succeeded", ""
        timeout = float(a.get("max_minutes") or 30) * 60
        try:
            await asyncio.wait_for(sess.handle_user(prompt), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()  # we handle it: let the cleanup below await normally
            if self._stopping:
                status, error = "interrupted", "the SOKKAN API stopped during this run"
            elif rid in self._cancelled:
                status, error = "cancelled", "cancelled by a human"
            else:
                status = "timeout"
                error = f"over the {a.get('max_minutes') or 30} min limit — interrupted"
            try:
                await asyncio.wait_for(sess.interrupt(), 10)
            except Exception:  # noqa: BLE001
                pass
        res = sess.last_result or {}
        text = res.get("text") or ""
        if status == "succeeded":
            errs = [e.get("message", "") for e in sess.events if e.get("type") == "error"]
            subtype = res.get("subtype") or ""
            if "budget" in subtype:
                status, error = "budget", f"run budget ${a.get('budget_usd', 0):.2f} reached"
            elif not res:
                status, error = "failed", (errs[-1] if errs else "the session produced no result")
            elif res.get("is_error"):
                status, error = "failed", text[:500] or subtype or "model error"
            else:
                verdict, why = parse_delivery(text)
                if verdict == "incomplete":
                    status, error = "incomplete", why or "the agent reported an incomplete delivery"
        deliverable = agents.redact(text, secret_vals)
        error = agents.redact(error, secret_vals)
        outputs = {}
        if deliverable.strip() and status in ("succeeded", "incomplete"):
            outputs = self._file(a, run, sid, deliverable, status)
        agents.update_run(rid, status=status, error=error, deliverable=deliverable[:60000],
                          outputs=outputs, waiting_approval=0,
                          cost_usd=round(sess.cost_usd, 6), tokens_in=sess.tokens_in,
                          tokens_out=sess.tokens_out, num_turns=sess.num_turns)
        agents.set_next_run(a["id"], (agents.get(a["id"]) or {}).get("next_run_at"),
                            last_run_at=time.time())
        audit.log(actor, "agent.run.end", f"run #{rid}",
                  f"{status} · ${sess.cost_usd:.4f} · {error[:200]}")
        final = agents.get_run(rid)
        if not self._incident(a, final):
            self._notify(a, final, status, error or self._summary(deliverable))
        self.sessions.pop(rid, None)
        # free the CLI subprocess; the transcript stays, the session reopens on demand
        try:
            await agentchat.drop(sid)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _summary(text: str) -> str:
        body = _DELIVERY_RE.sub("", text or "").strip()
        return body[:500] + ("…" if len(body) > 500 else "")

    @staticmethod
    def _summary_full(text: str) -> str:
        return _DELIVERY_RE.sub("", text or "").strip()[:20000]

    def _file(self, a: dict, run: dict, sid: str, deliverable: str, status: str) -> dict:
        out: dict = {}
        stamp = datetime.now(ZoneInfo(a.get("timezone") or cronexpr.DEFAULT_TZ)).strftime(
            "%Y-%m-%d %H:%M")
        link = notify.session_link(sid)
        for kind in a.get("outputs") or []:
            try:
                if kind == "card":
                    c = board.add_card(
                        title=f"[agent {a['name']}] run #{run['id']} — {stamp}",
                        description=f"{deliverable[:8000]}\n\n---\nRun #{run['id']} ({status}) · "
                                    f"session {link}",
                        tag="devops", bucket="Review", priority=2,
                        user=f"agent:{a['name']}")
                    out["card"] = c["id"]
                elif kind == "memory":
                    # QUARANTAINE : la note n'est rappelée qu'après relecture humaine
                    name = f"agent-{a['name']}-latest"[:64]
                    first = self._summary(deliverable).splitlines()[0][:160] if deliverable else ""
                    r = quarantine.write(
                        name,
                        f"Latest deliverable of the agent {a['name']} "
                        f"({stamp}, run #{run['id']}): {first}",
                        self._summary_full(deliverable),
                        {"agent": a["name"], "run": run["id"], "session": sid,
                         "via": "output"})
                    if r.get("ok"):
                        out["memory"] = name
                        out["memory_quarantined"] = True
                    else:
                        out["memory_error"] = r.get("error", "")
                elif kind == "file":
                    d = DATA_DIR / "agents" / a["name"]
                    d.mkdir(parents=True, exist_ok=True)
                    p = d / f"run-{run['id']:05d}.md"
                    p.write_text(deliverable, encoding="utf-8")
                    out["file"] = str(p)
                elif kind == "notify":
                    out["notify"] = True  # sent by _notify at the end (once)
            except Exception as e:  # noqa: BLE001 — filing one output never loses the others
                out[f"{kind}_error"] = f"{type(e).__name__}: {e}"
        return out

    def _incident(self, a: dict, run: dict | None) -> bool:
        """SOKKAN_AGENTS_INCIDENTS=1 : failed / timeout / budget → the agent's incident in
        Operate (one open incident per agent, later failures join it); a succeeded run
        resolves it. True when a NEW incident was opened and notified through the
        Operate channel — the run's own failure ping is then not sent twice."""
        if run is None or not incidents_enabled():
            return False
        try:
            if run["status"] == "succeeded":
                for iid in observability.resolve_agent_incidents(a["id"]):
                    audit.log(f"agent:{a['name']}", "incident.resolved", f"incident #{iid}",
                              f"run #{run['id']} succeeded")
                return False
            if run["status"] not in INCIDENT_STATUSES:
                return False
            iid, created = observability.agent_incident(
                a["id"], a["name"], run["id"], run["status"], run.get("error") or "",
                run.get("session_id") or "")
            agents.update_run(run["id"], outputs={**(run.get("outputs") or {}), "incident": iid})
            audit.log(f"agent:{a['name']}", "incident.open" if created else "incident.update",
                      f"incident #{iid}", f"run #{run['id']} {run['status']}")
        except Exception as e:  # noqa: BLE001 — an incident store problem never fails a run
            print(f"[agents] incident failed: {type(e).__name__}: {e}", file=sys.stderr)
            return False
        if not created:
            return False
        try:
            notify.send(f"SOKKAN — 🚨 agent {a['name']}: {run['status']} (run #{run['id']})",
                        (run.get("error") or "")[:900],
                        f"{notify.PUBLIC_URL}/?tab=operate&incident={iid}", "alert")
        except Exception:  # noqa: BLE001
            pass
        return True

    def _notify(self, a: dict, run: dict | None, status: str, body: str) -> None:
        if run is None:
            return
        want = set(a.get("notify_on") or [])
        key = {"failed": "failure", "interrupted": "failure", "incomplete": "failure",
               "timeout": "timeout", "budget": "budget", "succeeded": "success"}.get(status)
        explicit = "notify" in (a.get("outputs") or []) and status in ("succeeded", "incomplete")
        if not explicit and (key is None or key not in want):
            return
        icon = {"succeeded": "✅", "incomplete": "🟡", "budget": "💸", "timeout": "⏱️"}.get(
            status, "❌")
        link = notify.session_link(run["session_id"]) if run.get("session_id") else notify.PUBLIC_URL
        try:
            notify.send(f"SOKKAN — {icon} agent {a['name']}: {status} (run #{run['id']})",
                        body[:900], link, "agent")
        except Exception:  # noqa: BLE001 — a failed notification never fails the run
            pass


_runtime: Runtime | None = None


def start(recall=None) -> Runtime | None:
    global _runtime
    if not enabled():
        return None
    import demo_crew
    if demo_crew.requested() and not demo_crew.enabled():
        print("[agents] SOKKAN_DEMO_CREW ignored: only for the public demo "
              "(SOKKAN_DEMO_BANNER=1) — the real scheduler runs", file=sys.stderr)
    _runtime = demo_crew.DemoRuntime(recall) if demo_crew.enabled() else Runtime(recall=recall)
    _runtime.start()
    return _runtime


def get_runtime() -> Runtime | None:
    return _runtime


def poke() -> None:
    if _runtime:
        _runtime.poke()
