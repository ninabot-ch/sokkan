#!/usr/bin/env python3
"""agents_mcp.py — SOKKAN 3.1 "Crew up": MCP server `sokkan-agents`.

Lets a session create and drive agents (spec: docs/AGENTS.md). Embedded in every
SDK session by agentchat (like sokkan-board / sokkan-memory). Rules:
- reads (list/get) are auto-approved in the session;
- writes first pass the session's normal permission gate, then:
  create_agent → a PENDING proposal a human approves in the Crew tab;
  update_agent on an approved agent → a pending change, same approval;
- inside an agent run (SOKKAN_AGENT_RUN=1) the server is read-only: an agent
  does not breed agents.
The caller is identified by SOKKAN_SESSION_ID / SOKKAN_SESSION_USER, set by the
API in this process's environment — the model cannot choose them.
Shares agents.db with the API (SQLite, WAL); queued runs are picked up by the
API's scheduler within one tick.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents  # noqa: E402
import audit  # noqa: E402
import iam  # noqa: E402
import vault  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sokkan-agents")


def _who() -> tuple[dict, str]:
    """(IAM user behind this session, actor label for created_by/audit)."""
    email = (os.environ.get("SOKKAN_SESSION_USER") or "").strip().lower()
    if not email or "@" not in email or email.endswith("@sokkan"):
        # terminal session (no API-set identity) or a system session (alert@sokkan):
        # act for the instance owner — writes still wait for a human approval
        email = os.environ.get("SOKKAN_OWNER_EMAIL", "owner@localhost").lower()
    sid = os.environ.get("SOKKAN_SESSION_ID", "")
    return iam.get_user(email), (f"session:{sid}" if sid else f"session-of:{email}")


def _in_run() -> bool:
    return os.environ.get("SOKKAN_AGENT_RUN") == "1"


def _err(e: Exception) -> dict:
    return {"ok": False, "error": str(e)}


def _ro() -> dict:
    return {"ok": False, "error": "read-only inside an agent run: agents cannot create, change "
                                  "or start agents. Put the suggestion in your deliverable."}


def _view(a: dict) -> dict:
    v = agents.public(a)
    v["crew_status_hint"] = {
        "pending": "waiting for a human approval in the Crew tab — it does not run yet",
        "draft": "draft — not scheduled",
        "active": "active — runs on its trigger",
        "paused": "paused — not scheduled",
        "archived": "archived",
    }.get(a["status"], "")
    return v


@mcp.tool()
def create_agent(name: str, purpose: str, deliverable: str, done_criteria: str = "",
                 model: str = "", trigger: str = "manual", schedule: str = "",
                 timezone: str = "Europe/Zurich", once_at: float | None = None,
                 event: str = "", tools: list[str] | None = None,
                 auto_approve: list[str] | None = None, mcp_servers: list[str] | None = None,
                 secrets: list[str] | None = None, budget_usd: float = 0,
                 max_minutes: int = 30, outputs: list[str] | None = None,
                 notify_on: list[str] | None = None, playbook: str = "") -> dict:
    """Propose a new SOKKAN agent (a recurring or one-shot unattended job).

    Before calling: unless the user already gave everything, interview them ONE
    question at a time (purpose, deliverable + done criteria, trigger, model tier,
    tools/MCP, vault secret names, budget, what may run without approval, where the
    deliverable goes), recap the card and get their OK. The `new-agent` playbook
    describes the interview.

    The agent is created as a PENDING proposal: it never runs until a human
    approves it in the cockpit's Crew tab. Tell the user so.

    Args:
        name: kebab-case slug, e.g. "nightly-cve-audit".
        purpose: the mission — what the agent does and on what (repo, service, logs…).
        deliverable: what each run must hand back (report format, sections…).
        done_criteria: how the agent knows the run is finished.
        model: "" (instance default), "haiku", "sonnet", "opus" or a model id.
        trigger: "manual", "once" (with once_at, UTC epoch), "cron" (with schedule)
            or "event" (with event = "alert" or "alert:<alertname glob>").
        schedule: 5-field cron in `timezone` wall-clock time, e.g. "0 2 * * *"
            (02:00 every night), "0 8 * * mon" (Mondays 08:00).
        timezone: IANA zone of the schedule (default Europe/Zurich).
        tools: tools the agent may use (default Read, Glob, Grep, WebFetch,
            WebSearch, Bash); anything else is refused during runs.
        auto_approve: subset of tools run WITHOUT asking a human, in Claude Code
            rule syntax, e.g. ["Bash(npm audit:*)"]. Default: every mutating call
            waits for a human.
        mcp_servers: extra SOKKAN MCP servers: sokkan-board, sokkan-observability
            (sokkan-memory is always on).
        secrets: vault secret NAMES (e.g. ["GITHUB_TOKEN"]) exposed as env vars
            during runs. NEVER pass a secret value; an admin stores values in
            Profile → Secrets.
        budget_usd: hard cost cap per run (0 = none beyond the instance budget).
        max_minutes: wall-clock cap per run (default 30).
        outputs: where the deliverable goes: any of card (board card in Review,
            default), memory (note agent-<name>-latest), file, notify.
        notify_on: any of failure, timeout, budget, approval, success.
        playbook: optional playbook id used as the mission template.
    """
    if _in_run():
        return _ro()
    user, actor = _who()
    fields = dict(name=name, purpose=purpose, deliverable=deliverable,
                  done_criteria=done_criteria, model=model, trigger=trigger,
                  schedule=schedule, timezone=timezone, once_at=once_at, event=event,
                  tools=tools, auto_approve=auto_approve, mcp=mcp_servers, secrets=secrets,
                  budget_usd=budget_usd, max_minutes=max_minutes, outputs=outputs,
                  notify_on=notify_on, playbook=playbook)
    fields = {k: v for k, v in fields.items() if v is not None}
    try:
        a = agents.create(user, fields, created_by=actor, proposal=True,
                          known_secrets=vault.names())
    except agents.AgentError as e:
        return _err(e)
    audit.log(actor, "agent.propose", a["name"], f"owner {a['owner']} · {a['trigger']}")
    return {"ok": True, "agent": _view(a),
            "next_step": "A human must approve it in the Crew tab before it runs."}


@mcp.tool()
def update_agent(agent: str, changes: dict) -> dict:
    """Change an agent (by id or name). `changes` = fields to set (same names as
    create_agent; use "mcp" for mcp_servers). On an approved agent the change is
    stored as PENDING and applied only once a human approves it in Crew — the
    approved version keeps running meanwhile."""
    if _in_run():
        return _ro()
    user, actor = _who()
    a = agents.resolve(agent)
    if not a:
        return _err(agents.NotFound("agent not found"))
    if "mcp_servers" in changes:
        changes = {**changes, "mcp": changes.pop("mcp_servers")}
    try:
        out = agents.update(user, a["id"], changes, from_session=True,
                            known_secrets=vault.names())
    except agents.AgentError as e:
        return _err(e)
    audit.log(actor, "agent.update.propose", out["name"], ", ".join(sorted(changes)))
    return {"ok": True, "agent": _view(out),
            "next_step": ("Change pending: a human approves it in the Crew tab."
                          if out.get("pending_change") or out["status"] == "pending" else "")}


@mcp.tool()
def list_agents(include_archived: bool = False) -> list[dict]:
    """The agents you can see (yours; all of them for an admin), with run stats."""
    user, _ = _who()
    return [_view(a) for a in agents.list_agents(user, include_archived=include_archived)]


@mcp.tool()
def get_agent(agent: str) -> dict:
    """One agent by id or name — full definition, status, next run, last run."""
    user, _ = _who()
    a = agents.resolve(agent)
    try:
        agents._need(user, a, write=False)
    except agents.AgentError as e:
        return _err(e)
    full = next((x for x in agents.list_agents(user, include_archived=True)
                 if x["id"] == a["id"]), a)
    return _view(full)


@mcp.tool()
def run_agent_now(agent: str) -> dict:
    """Queue a run of an ACTIVE (approved) agent now. The cockpit's scheduler
    starts it within ~15 s; follow it with get_run."""
    if _in_run():
        return _ro()
    user, actor = _who()
    a = agents.resolve(agent)
    try:
        r = agents.request_run(user, (a or {}).get("id", -1) if a else -1, trigger="session",
                               requested_by=actor)
    except agents.AgentError as e:
        return _err(e)
    audit.log(actor, "agent.run.request", a["name"], f"run #{r['id']}")
    return {"ok": True, "run": r}


def _status(agent: str, status: str, verb: str) -> dict:
    if _in_run():
        return _ro()
    user, actor = _who()
    a = agents.resolve(agent)
    try:
        out = agents.set_status(user, (a or {}).get("id", -1), status, from_session=True)
    except agents.AgentError as e:
        return _err(e)
    audit.log(actor, f"agent.{verb}", out["name"], "")
    return {"ok": True, "agent": _view(out)}


@mcp.tool()
def pause_agent(agent: str) -> dict:
    """Pause an active agent (no more scheduled runs until resumed)."""
    return _status(agent, "paused", "pause")


@mcp.tool()
def resume_agent(agent: str) -> dict:
    """Resume a paused agent that a human approved before."""
    return _status(agent, "active", "resume")


@mcp.tool()
def archive_agent(agent: str) -> dict:
    """Archive an agent for good (its run history stays readable)."""
    return _status(agent, "archived", "archive")


@mcp.tool()
def list_runs(agent: str, limit: int = 20) -> list[dict] | dict:
    """Recent runs of an agent: status, cost, tokens, session, when."""
    user, _ = _who()
    a = agents.resolve(agent)
    try:
        runs = agents.list_runs(user, (a or {}).get("id", -1), limit=limit)
    except agents.AgentError as e:
        return _err(e)
    for r in runs:
        r["deliverable"] = (r["deliverable"] or "")[:300]
    return runs


@mcp.tool()
def get_run(run_id: int) -> dict:
    """One run in full: status, error, cost, tokens, deliverable, where it was filed."""
    user, _ = _who()
    try:
        return agents.get_run_for(user, int(run_id))
    except agents.AgentError as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()
