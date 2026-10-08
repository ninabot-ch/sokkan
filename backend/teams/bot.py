"""teams.bot — what @Nina does with a (verified) Teams activity.

Every action runs AS the Teams user who wrote or clicked: their SOKKAN account (linked at
their Entra ID sign-in), their role in the channel's project, their clearance capped by the
channel's level (what Nina says in a channel is read by everyone in it).

Commands (French or English), after the @mention:
  status | état                         project status card (board, latest runs, approvals)
  card: <title> | carte : <titre>        create a card in the project (dev+)
  decision: <text> | note la décision : <texte>
                                        capture a decision into the project memory
  run <agent> | lance l'agent <agent>   propose a run → approval card
  approvals | approbations              pending agent approvals as cards
  anything else                         a question to Nina, answered as the asker
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
import time
from dataclasses import dataclass

import teams
from teams import botauth, cards, connector, signing, store


@dataclass
class Ctx:
    activity: dict
    email: str
    aad: str
    user: dict           # instance identity (iam)
    pu: dict             # the person inside the project (projectgate.project_user)
    project: str
    channel_level: int   # audience of the conversation
    cap: int             # what may be shown here: min(clearance, channel level)
    scope: tuple


# ---- helpers -------------------------------------------------------------------------------
def _lv():
    from core import levels
    return levels


def clean_text(activity: dict) -> str:
    t = activity.get("text") or ""
    t = re.sub(r"<at>.*?</at>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(html.unescape(t).split())


def channel_key(activity: dict) -> str:
    ch = ((activity.get("channelData") or {}).get("channel") or {}).get("id")
    return ch or (activity.get("conversation") or {}).get("id", "")


def is_personal(activity: dict) -> bool:
    return (activity.get("conversation") or {}).get("conversationType") == "personal"


def thread_link(activity: dict) -> str:
    """Deep link to the message (Teams 'Copy link' format)."""
    cd = activity.get("channelData") or {}
    conv = (activity.get("conversation") or {}).get("id", "")
    ch = (cd.get("channel") or {}).get("id") or conv.split(";")[0]
    parent = conv.split(";messageid=")[1] if ";messageid=" in conv else activity.get("id", "")
    q = f"tenantId={botauth.tenant_of(activity)}"
    if (cd.get("team") or {}).get("aadGroupId"):
        q += f"&groupId={cd['team']['aadGroupId']}"
    q += f"&parentMessageId={parent}"
    return f"https://teams.microsoft.com/l/message/{ch}/{activity.get('id', '')}?{q}"


def cap_scope(scope: tuple, cap: int) -> tuple:
    from core import scope as S
    caps = S.caps(scope) or {}
    return S.normalize([S.entry(p, min(c, cap)) for p, c in caps.items()])


class Stop(Exception):
    """A reply that ends the handling (no access, not linked…)."""


def _identify(activity: dict, text: str) -> tuple[Ctx, str]:
    import classification
    import projectgate
    import projects

    frm = activity.get("from") or {}
    aad = frm.get("aadObjectId") or ""
    tenant = botauth.tenant_of(activity)
    email = store.linked_email(aad, tenant) if aad else None
    if not email:
        url = teams.public_url() or "SOKKAN"
        raise Stop(f"I act only on behalf of a SOKKAN account. Sign in once to {url} with "
                   "your company account (Microsoft Entra ID), then ask me again.")
    user = classification.user_for(email)
    key = channel_key(activity)
    m = store.channel(key)
    if m is not None:
        project, level = m["project"], int(m["level"])
    elif is_personal(activity):
        level = _lv().MAX                       # a 1:1 chat: only the person reads it
        mm = re.match(r"^(?:in|dans)\s+([a-z0-9][a-z0-9-]{0,62})\s*[:,]\s*(.*)$", text, re.I)
        readable = projects.readable_projects(user)
        if mm:
            project, text = mm.group(1).lower(), mm.group(2)
        elif len(readable) == 1:
            project = readable[0]
        else:
            raise Stop("Which project? Start with « in <project>: … » — yours: "
                       + ", ".join(readable or ["none"]))
    else:
        raise Stop("This conversation is not linked to a SOKKAN project (an admin maps it in "
                   "Setup › Organization › Teams).")
    pu = projectgate.project_user(user, project)
    if pu is None:
        raise Stop(f"You have no access to the project « {project} ».")
    clr = classification.clearance(user, project)
    clr = _lv().DEFAULT if clr is None else clr
    cap = min(clr, level)
    scope = cap_scope(classification.scope_for(user, project), cap)
    return Ctx(activity, email, aad, user, pu, project, level, cap, scope), text


# ---- entry point --------------------------------------------------------------------------
def handle(activity: dict) -> dict | None:
    """Handle a VERIFIED activity. Returns the body of the HTTP answer (invoke) or None."""
    kind = activity.get("type")
    if kind == "invoke" and activity.get("name") == "adaptiveCard/action":
        return _on_action(activity, ((activity.get("value") or {}).get("action") or {}))
    if kind == "message" and isinstance(activity.get("value"), dict) and \
            activity["value"].get("sokkan") == "approval":       # Action.Submit fallback
        out = _on_action(activity, {"verb": activity["value"].get("verb"),
                                    "data": activity["value"]})
        connector.reply(activity, connector.text(_invoke_text(out)))
        return None
    if kind != "message":
        return None
    text = clean_text(activity)
    try:
        ctx, text = _identify(activity, text)
        payload = route(ctx, text)
    except Stop as s:
        payload = connector.text(str(s))
    connector.reply(activity, payload)
    return None


def _invoke_text(out: dict) -> str:
    v = out.get("value")
    return v if isinstance(v, str) else "Done."


def route(ctx: Ctx, text: str) -> dict:
    low = text.lower()
    if re.match(r"^(status|état|etat)\b", low):
        return status(ctx)
    m = re.match(r"^(?:card|carte|create a card|crée une carte|cree une carte)\s*:?\s*(.+)$",
                 text, re.I | re.S)
    if m:
        return create_card(ctx, m.group(1).strip())
    m = re.match(r"^(?:note (?:la|the) d[ée]cision|d[ée]cision|decision)\s*:?\s*(.+)$",
                 text, re.I | re.S)
    if m:
        return decision(ctx, m.group(1).strip())
    m = re.match(r"^(?:run|launch|lance(?:r)?(?: l'agent| l’agent)?)\s+([A-Za-z0-9_.-]+)", text, re.I)
    if m:
        return propose_run(ctx, m.group(1))
    if re.match(r"^(approvals|approbations)\b", low):
        return approvals(ctx)
    return ask_nina(ctx, text)


# ---- commands -------------------------------------------------------------------------------
def status(ctx: Ctx) -> dict:
    import agents
    import board
    import classification
    b = board.list_cards(project=ctx.project, max_level=ctx.cap)
    counts = {k: len(v) for k, v in b.items()}
    runs = []
    try:
        for a in agents.list_agents(ctx.pu):
            if (a.get("project") or "default") != ctx.project:
                continue
            for r in agents.list_runs(ctx.pu, a["id"], 1):
                r = classification.redact_run(r, ctx.cap)
                runs.append(f"{a['name']} · run #{r['id']} · {r['status']}"
                            + (f" · classified {r['classified']}" if r.get("classified") else ""))
        pending = agents.pending_approvals(ctx.pu)
        n = len([a for a in pending["agents"] if (a.get("project") or "default") == ctx.project])
    except Exception:  # noqa: BLE001 — Crew off: status without it
        n = 0
    _audit(ctx, "teams.status", ctx.project)
    return connector.card(cards.status(ctx.project, counts, runs[:5], n,
                                       _lv().label(ctx.cap)), f"Status of {ctx.project}")


def _need(ctx: Ctx, role: str) -> None:
    import projects
    if projects.prank(ctx.pu.get("project_role")) < projects.prank(role):
        raise Stop(f"That needs the role {role} in « {ctx.project} ».")


def inherited_level(ctx: Ctx) -> int:
    """Level of what is written from this conversation: the highest of the channel's level
    and the project default (an unmapped 1:1 chat: the project default)."""
    if is_personal(ctx.activity) and store.channel(channel_key(ctx.activity)) is None:
        return _lv().DEFAULT
    return max(ctx.channel_level, _lv().DEFAULT)


def create_card(ctx: Ctx, title: str) -> dict:
    import board
    _need(ctx, "dev")
    level = inherited_level(ctx)
    c = board.add_card(title[:200], "", project=ctx.project, level=level, user=ctx.email,
                       origin={"via": "teams"})
    _audit(ctx, "board.card.create", f"card #{c['id']}", f"from Teams: {title[:80]}")
    return connector.text(f"Card #{c['id']} created in « {ctx.project} » ({_lv().label(level)}).")


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "-".join(words)[:40].strip("-") or "decision"


def decision(ctx: Ctx, text: str) -> dict:
    """« @Nina note la décision : … » → a note in the project memory: author, date, link to
    the thread, level = the highest of the channel's and the project default."""
    import store_backend
    _need(ctx, "dev")
    level = inherited_level(ctx)
    now = _dt.datetime.now(_dt.timezone.utc)
    name = f"decision-{now:%Y%m%d}-{_slug(text)}"[:64].rstrip("-")
    link = thread_link(ctx.activity)
    desc = " ".join(text.split())[:180]
    fm = ["---", f"name: {name}", f"description: {json.dumps('Decision: ' + desc, ensure_ascii=False)}"]
    if level != _lv().DEFAULT:
        fm.append(f"classification: {_lv().ident(level)}")
    fm += ["metadata:", "  type: decision", f"  author: {json.dumps(ctx.email)}",
           f"  decided_at: {now:%Y-%m-%dT%H:%M:%SZ}", f"  source: {json.dumps(link)}", "---", ""]
    body = (f"{text.strip()}\n\nDecided in Microsoft Teams by {ctx.email} on "
            f"{now:%Y-%m-%d %H:%M} UTC — [thread]({link}).\n")
    d = store_backend.memory_dir_for(ctx.project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name.replace('-', '_')}.md"
    n = 2
    while path.exists():
        path = d / f"{name.replace('-', '_')}_{n}.md"
        n += 1
    if n > 2:
        name = f"{name}-{n - 1}"
        fm[1] = f"name: {name}"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(fm) + body, encoding="utf-8")
    tmp.replace(path)
    if level > _lv().DEFAULT and store_backend.enabled():
        try:
            store_backend.get_store().set_level(name, level, project=ctx.project, by=ctx.email,
                                                reason="decision captured in Teams")
        except Exception as e:  # noqa: BLE001
            print(f"[teams] level floor of {name} not recorded: {e!r}")
    _audit(ctx, "teams.decision", f"{ctx.project}/{name}", desc[:120])
    return connector.text(f"Decision noted in the memory of « {ctx.project} » as **{name}** "
                          f"({_lv().label(level)}).")


def propose_run(ctx: Ctx, agent_ref: str) -> dict:
    import agents
    _need(ctx, "dev")
    a = agents.resolve(agent_ref, ctx.project)   # 3.2 lot 4: names unique per project
    if a is None or (a.get("project") or "default") != ctx.project or not agents.can_read(ctx.pu, a):
        raise Stop(f"No agent « {agent_ref} » in « {ctx.project} ».")
    if a["status"] != "active":
        raise Stop(f"The agent {a['name']} is {a['status']}: only an approved agent runs.")
    spec = {"title": f"Run the agent {a['name']}?",
            "facts": [("Project", ctx.project), ("Requested by", ctx.email),
                      ("Purpose", a["purpose"][:200])],
            "note": "Another person approves when four-eyes approval is on. The run acts as its "
                    "owner, within the owner's clearance.",
            "open_url": _open(f"/?plane=build&tab=crew&agent={a['id']}")}
    tok = signing.issue("agent.run", str(a["id"]), ctx.project, ctx.email, card=spec)
    _audit(ctx, "teams.approval.request", a["name"], "run")
    return connector.card(cards.approval(spec["title"], spec["facts"], tok, spec["note"],
                                         spec["open_url"]), f"Approve a run of {a['name']}")


def _open(path: str) -> str:
    base = teams.public_url()
    return f"{base}{path}" if base.startswith("https://") else ""


def approvals(ctx: Ctx) -> dict:
    import agents
    pend = agents.pending_approvals(ctx.pu)
    mine = [a for a in pend["agents"] if (a.get("project") or "default") == ctx.project]
    if not mine:
        return connector.text("Nothing waits for an approval in « {} ».".format(ctx.project))
    atts = []
    for a in mine[:5]:
        spec = {"title": f"Activate the agent {a['name']}?" if not a.get("pending_change")
                else f"Apply the change to the agent {a['name']}?",
                "facts": [("Project", ctx.project), ("Owner", a.get("owner") or ""),
                          ("Trigger", json.dumps(a.get("trigger"))
                           if not isinstance(a.get("trigger"), str) else a["trigger"])],
                "open_url": _open(f"/?plane=build&tab=crew&agent={a['id']}")}
        tok = signing.issue("agent.activate", str(a["id"]), ctx.project, a.get("owner") or "",
                            card=spec)
        atts.append(cards.approval(spec["title"], spec["facts"], tok, "", spec["open_url"]))
    return {"type": "message", "summary": "Approvals",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                             "content": c} for c in atts]}


def ask_nina(ctx: Ctx, text: str) -> dict:
    import assistant
    if not text:
        return connector.text("Ask me about « {} » — or: status, card: …, decision: …, "
                              "run <agent>, approvals.".format(ctx.project))
    try:
        out = assistant.chat(ctx.email, text, scope=ctx.scope, channel="teams")
    except ValueError as e:
        return connector.text(f"Nina: {e}")
    tail = ""
    if _lv().parse(out.get("level")) not in (None, _lv().DEFAULT):
        tail = f"\n\n_classification: {_lv().label(_lv().parse(out['level']))}_"
    return connector.text(out["reply"] + tail)


# ---- approvals (Adaptive Card actions) ------------------------------------------------------
def _invoke(text: str = "", card: dict | None = None) -> dict:
    if card is not None:
        return {"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive",
                "value": card}
    return {"statusCode": 200, "type": "application/vnd.microsoft.activity.message",
            "value": text}


def _on_action(activity: dict, action: dict) -> dict:
    import agents
    import classification
    import projectgate

    verb = action.get("verb")
    data = action.get("data") or {}
    if data.get("sokkan") == "approval" and verb == "refresh":
        return _refresh(data.get("token") or "")
    if data.get("sokkan") != "approval" or verb not in ("approve", "refuse"):
        return _invoke("Unknown action.")
    frm = activity.get("from") or {}
    aad = frm.get("aadObjectId") or ""
    email = store.linked_email(aad, botauth.tenant_of(activity)) if aad else None
    if not email:
        return _invoke("Sign in once to SOKKAN with your company account first.")
    try:
        p = signing.peek(data.get("token") or "")
    except signing.Invalid as e:
        return _invoke(f"Refused: {e}.")
    pu = projectgate.project_user(classification.user_for(email), p["p"])
    if pu is None:
        return _invoke("You have no access to this project.")
    try:
        row = signing.consume(data["token"], aad, email, verb)
    except signing.Invalid as e:
        return _invoke(f"Refused: {e}.")
    spec = _spec(row)
    try:
        if row["kind"] == "run.tool":
            title, detail = _decide_tool(pu, row, verb, email)
        else:
            title, detail = _decide_agent(pu, row, verb, email, spec)
    except (agents.AgentError, agents.Forbidden, agents.NotFound) as e:
        signing.release(row["nonce"])       # the token stays usable by someone entitled
        # 3.4.1: a refusal is a decision of the gate (four-eyes, role, vanished object) —
        # it was invisible in the journal during the live validation of 08.10
        import audit
        audit.log(email, "teams.approval.refused", f"{row['kind']} {row['ref']}",
                  f"{verb}: {e}", project=row.get("project") or None)
        return _invoke(f"Refused for you: {e}")
    import audit
    audit.log(email, f"teams.approval.{verb}", f"{row['kind']} {row['ref']}",
              f"requested by {row['requested_by']}", project=row.get("project") or None)
    try:                                    # the card of the other channels follows
        from teams import proactive
        proactive.poke()
    except Exception:  # noqa: BLE001
        pass
    return _invoke(card=cards.decided(title, verb, email, detail))


def _decide_agent(pu: dict, row: dict, verb: str, email: str, spec: dict) -> tuple[str, str]:
    import agents
    import features
    aid = int(row["ref"])
    a = agents.get(aid) or {}
    title = spec.get("title") or f"Agent {a.get('name', aid)}"
    if row["kind"] == "agent.run":
        if verb == "approve":
            if features.enabled("four_eyes") and email == row["requested_by"]:
                raise agents.Forbidden("four-eyes approval: another person approves")
            r = agents.request_run(pu, aid, "manual",
                                   requested_by=f"{row['requested_by']} via Teams, "
                                                f"approved by {email}")
            detail = f"run #{r['id']} queued"
        else:
            detail = "no run"
    elif row["kind"] == "agent.activate":
        if verb == "approve":
            agents.approve(pu, aid)
            detail = "active"
        else:
            agents.reject(pu, aid)
            detail = "sent back to draft"
    else:
        raise agents.AgentError("unknown approval kind")
    return title, detail


def _spec(row: dict) -> dict:
    try:
        return json.loads(row.get("card") or "{}") or {}
    except ValueError:
        return {}


def _refresh(token: str) -> dict:
    """``refresh`` of an approval card: the current state for whoever looks at it (no
    identity needed — the card was already shown in this conversation; nothing is decided)."""
    try:
        p = signing.peek(token, allow_expired=True)
    except signing.Invalid as e:
        return _invoke(f"Refused: {e}.")
    row = signing.row(p["n"]) or {}
    spec = _spec(row)
    title = spec.get("title") or "Approval"
    if row.get("used_at"):
        how = row.get("decision") or "closed"
        detail = "" if how in ("approve", "refuse") else "decided in SOKKAN"
        return _invoke(card=cards.decided(title, how if how in ("approve", "refuse") else "closed",
                                          row.get("used_by") if how in ("approve", "refuse") else "",
                                          detail))
    if p.get("e", 0) < time.time():
        return _invoke(card=cards.decided(title, "closed", "", "this approval has expired"))
    return _invoke(card=cards.approval(title, [tuple(f) for f in spec.get("facts") or []], token,
                                       spec.get("note", ""), spec.get("open_url", "")))


def _resolve_permission(sess, pid: str, decision: dict) -> None:
    """Resolve a tool approval of a live session from this (worker) thread: the future
    belongs to the API's event loop."""
    fut = (getattr(sess, "_perms", None) or {}).get(pid)
    get_loop = getattr(fut, "get_loop", None)
    loop = get_loop() if get_loop else None
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(sess.resolve_permission, pid, decision)
    else:
        sess.resolve_permission(pid, decision)


def _decide_tool(pu: dict, row: dict, verb: str, email: str) -> tuple[str, str]:
    """A tool call of an agent run, approved or denied from Teams — by the agent's owner
    (dev+) or an admin of the project, like in the cockpit."""
    import agentchat
    import agents
    import sharing
    run_id, pid = row["ref"].split(":", 1)
    run = agents.get_run(int(run_id))
    if run is None:
        raise agents.NotFound("run not found")
    a = agents.get(run["agent_id"]) or {}
    if not a or (a.get("project") or "default") != row["project"] or not agents.can_manage(pu, a):
        raise agents.Forbidden("only the agent's owner or an admin of the project decides "
                               "its tool calls")
    pend = {x["id"]: x for x in sharing.pending_approvals(run["session_id"])}
    sess = agentchat.peek(run["session_id"])
    if pid not in pend or sess is None:
        raise agents.AgentError("this tool call no longer waits (decided, or the run ended)")
    _resolve_permission(sess, pid, {
        "decision": "allow" if verb == "approve" else "deny",
        "message": None if verb == "approve" else f"Denied by {email} (Microsoft Teams)"})
    title = _spec(row).get("title") or f"Agent {a.get('name')} · {pend[pid]['tool']}"
    return title, ("allowed — the run continues" if verb == "approve" else "denied")


def _audit(ctx: Ctx, action: str, resource: str, detail: str = "") -> None:
    import audit
    audit.log(ctx.email, action, resource, detail, project=ctx.project)
