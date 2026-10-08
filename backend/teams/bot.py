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

Nina answers in the language of the message (3.4.2): French when the French trigger matched
(« état », « carte », « note la décision », « lance l'agent », « approbations », « dans
<projet> : ») or the sentence reads as French, English otherwise — replies, refusals and the
cards' labels alike.
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
    lang: str = "en"     # 3.4.2: the language of the message (fr | en) — Nina answers in it


# ---- helpers -------------------------------------------------------------------------------
def _lv():
    from core import levels
    return levels


# ---- language (3.4.2) ------------------------------------------------------------------------
# A French trigger (or the « dans <projet> : » prefix of a 1:1 chat) decides; otherwise a few
# unmistakably French words. English is the default: the cockpit's language.
_FR_TRIGGER = re.compile(
    r"^(?:dans\s+[a-z0-9][a-z0-9-]{0,62}\s*[:,]\s*)?"
    r"(?:état|etat|carte|crée une carte|cree une carte|note la d[ée]cision|décision|"
    r"lance(?:r)?\b|approbations)", re.I)
_FR_WORDS = re.compile(
    r"\b(?:le|la|les|des|une|est|pour|avec|dans|sur|qui|que|quoi|où|quel|quelle|quels|"
    r"quelles|comment|pourquoi|est-ce|nous|vous|peux|peut|faut|merci|bonjour|salut)\b", re.I)


def detect_lang(text: str) -> str:
    """``fr`` when the message is French (a French trigger, or French words), else ``en``."""
    t = (text or "").strip()
    if not t:
        return "en"
    if _FR_TRIGGER.match(t):
        return "fr"
    if re.search(r"[àâçéèêëîïôûùüÿœ]", t.lower()) and _FR_WORDS.search(t):
        return "fr"
    return "fr" if len(_FR_WORDS.findall(t)) >= 2 else "en"


def _t(lang: str, en: str, fr: str) -> str:
    return fr if lang == "fr" else en


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

    lang = detect_lang(text)
    frm = activity.get("from") or {}
    aad = frm.get("aadObjectId") or ""
    tenant = botauth.tenant_of(activity)
    email = store.linked_email(aad, tenant) if aad else None
    if email:   # 3.4.3: the name Teams shows for the person, for Nina's mentions
        store.remember_user_name(aad, tenant, frm.get("name") or "")
    if not email:
        url = teams.public_url() or "SOKKAN"
        raise Stop(_t(lang,
                      f"I act only on behalf of a SOKKAN account. Sign in once to {url} with "
                      "your company account (Microsoft Entra ID), then ask me again.",
                      f"Je n'agis qu'au nom d'un compte SOKKAN. Connectez-vous une fois à {url} "
                      "avec votre compte d'entreprise (Microsoft Entra ID), puis redemandez-moi."))
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
            mine = ", ".join(readable or [_t(lang, "none", "aucun")])
            raise Stop(_t(lang,
                          f"Which project? Start with « in <project>: … » — yours: {mine}",
                          f"Quel projet ? Commencez par « dans <projet> : … » — les vôtres : {mine}"))
    else:
        raise Stop(_t(lang,
                      "This conversation is not linked to a SOKKAN project (an admin maps it in "
                      "Setup › Organization › Teams).",
                      "Cette conversation n'est liée à aucun projet SOKKAN (un admin la relie dans "
                      "Setup › Organization › Teams)."))
    pu = projectgate.project_user(user, project)
    if pu is None:
        raise Stop(_t(lang, f"You have no access to the project « {project} ».",
                      f"Vous n'avez pas accès au projet « {project} »."))
    clr = classification.clearance(user, project)
    clr = _lv().DEFAULT if clr is None else clr
    cap = min(clr, level)
    scope = cap_scope(classification.scope_for(user, project), cap)
    return Ctx(activity, email, aad, user, pu, project, level, cap, scope, lang), text


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
    res = connector.reply(activity, payload)
    try:        # 3.4.3: an approval card answered here is replaced when decided elsewhere
        from teams import proactive
        proactive.track_reply(activity, payload, str((res or {}).get("id") or ""))
    except Exception as e:  # noqa: BLE001 — bookkeeping never fails an answer
        print(f"[teams] reply not tracked: {e!r}", flush=True)
    return None


def _invoke_text(out: dict) -> str:
    v = out.get("value")
    return v if isinstance(v, str) else "Done."


def route(ctx: Ctx, text: str) -> dict:
    low = text.lower()
    m = re.match(r"^(status|état|etat)\b", low)
    if m:
        ctx.lang = "en" if m.group(1) == "status" else "fr"
        return status(ctx)
    m = re.match(r"^(card|carte|create a card|crée une carte|cree une carte)\s*:?\s*(.+)$",
                 text, re.I | re.S)
    if m:
        ctx.lang = "fr" if m.group(1).lower().startswith(("carte", "crée", "cree")) else "en"
        return create_card(ctx, m.group(2).strip())
    m = re.match(r"^(note (?:la|the) d[ée]cision|d[ée]cision|decision)\s*:?\s*(.+)$",
                 text, re.I | re.S)
    if m:
        head = m.group(1).lower()
        ctx.lang = "fr" if head.startswith("note la") or "é" in head else \
            ("en" if head.startswith(("note the", "decision")) else ctx.lang)
        return decision(ctx, m.group(2).strip())
    m = re.match(r"^(run|launch|lance(?:r)?(?: l'agent| l’agent)?)\s+([A-Za-z0-9_.-]+)", text, re.I)
    if m:
        ctx.lang = "fr" if m.group(1).lower().startswith("lance") else "en"
        return propose_run(ctx, m.group(2))
    m = re.match(r"^(approvals|approbations)\b", low)
    if m:
        ctx.lang = "fr" if m.group(1) == "approbations" else "en"
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
                                       _lv().label(ctx.cap), lang=ctx.lang),
                          _t(ctx.lang, f"Status of {ctx.project}", f"État de {ctx.project}"))


def _need(ctx: Ctx, role: str) -> None:
    import projects
    if projects.prank(ctx.pu.get("project_role")) < projects.prank(role):
        raise Stop(_t(ctx.lang, f"That needs the role {role} in « {ctx.project} ».",
                      f"Il faut le rôle {role} dans « {ctx.project} »."))


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
    return connector.text(_t(
        ctx.lang, f"Card #{c['id']} created in « {ctx.project} » ({_lv().label(level)}).",
        f"Carte #{c['id']} créée dans « {ctx.project} » ({_lv().label(level)})."))


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "-".join(words)[:40].strip("-") or "decision"


def decision(ctx: Ctx, text: str) -> dict:
    """« @Nina note la décision : … » → a note in the project memory: author, date, link to
    the thread, level = the highest of the channel's and the project default."""
    import store_backend
    _need(ctx, "dev")
    level = inherited_level(ctx)
    try:    # 3.4.2: the 2.x index has no project and no level — never a phantom note
        store_backend.require_store(ctx.project, level)
    except store_backend.StoreRequired as e:
        _audit(ctx, "teams.decision.refused", ctx.project,
               f"{e.code}: project={ctx.project} level={_lv().ident(level)} "
               f"store={store_backend.store_info()['mode']}")
        raise Stop(store_backend.store_required_message(ctx.lang, e.migrating))
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
    return connector.text(_t(
        ctx.lang,
        f"Decision noted in the memory of « {ctx.project} » as **{name}** ({_lv().label(level)}).",
        f"Décision notée dans la mémoire de « {ctx.project} » sous **{name}** "
        f"({_lv().label(level)})."))


def _viewer_ids(ctx: Ctx) -> list[str]:
    """3.4.3 — `refresh.userIds` of a card answered to a person: Teams refreshes THEIR copy
    (verb `refresh`) when they look at it, so a decision taken by someone else shows."""
    fid = str(((ctx.activity.get("from") or {}).get("id")) or "")
    return [fid] if fid else []


def propose_run(ctx: Ctx, agent_ref: str) -> dict:
    import agents
    _need(ctx, "dev")
    a = agents.resolve(agent_ref, ctx.project)   # 3.2 lot 4: names unique per project
    if a is None or (a.get("project") or "default") != ctx.project or not agents.can_read(ctx.pu, a):
        raise Stop(_t(ctx.lang, f"No agent « {agent_ref} » in « {ctx.project} ».",
                      f"Aucun agent « {agent_ref} » dans « {ctx.project} »."))
    if a["status"] != "active":
        raise Stop(_t(ctx.lang,
                      f"The agent {a['name']} is {a['status']}: only an approved agent runs.",
                      f"L'agent {a['name']} est {a['status']} : seul un agent approuvé "
                      "s'exécute."))
    fr = ctx.lang == "fr"
    spec = {"title": f"Lancer l'agent {a['name']} ?" if fr else f"Run the agent {a['name']}?",
            "facts": [(_t(ctx.lang, "Project", "Projet"), ctx.project),
                      (_t(ctx.lang, "Requested by", "Demandé par"), ctx.email),
                      (_t(ctx.lang, "Purpose", "Objet"), a["purpose"][:200])],
            "note": _t(ctx.lang,
                       "Another person approves when four-eyes approval is on. The run acts as "
                       "its owner, within the owner's clearance.",
                       "Une autre personne approuve quand la double validation est active. Le "
                       "run agit comme son propriétaire, dans son habilitation."),
            "open_url": _open(f"/?plane=build&tab=crew&agent={a['id']}"), "lang": ctx.lang}
    tok = signing.issue("agent.run", str(a["id"]), ctx.project, ctx.email, card=spec)
    _audit(ctx, "teams.approval.request", a["name"], "run")
    return connector.card(cards.approval(spec["title"], spec["facts"], tok, spec["note"],
                                         spec["open_url"], user_ids=_viewer_ids(ctx),
                                         lang=ctx.lang),
                          _t(ctx.lang, f"Approve a run of {a['name']}",
                             f"Approuver un run de {a['name']}"))


def _open(path: str) -> str:
    base = teams.public_url()
    return f"{base}{path}" if base.startswith("https://") else ""


_FIELD = {"budget_usd": ("Budget", "Budget"), "max_minutes": ("Duration", "Durée"),
          "model": ("Model", "Modèle"), "tools": ("Tools", "Outils"), "mcp": ("MCP", "MCP"),
          "trigger": ("Trigger", "Déclencheur"), "schedule": ("Schedule", "Horaire"),
          "purpose": ("Purpose", "Objet"), "deliverable": ("Deliverable", "Livrable"),
          "secrets": ("Secrets", "Secrets"), "auto_approve": ("Auto-approve", "Auto-approbation"),
          "name": ("Name", "Nom"), "prompt": ("Prompt", "Prompt")}


def _fmt_value(key: str, v, lang: str = "en") -> str:
    if v is None or v == "" or v == []:
        return "—"
    if key == "budget_usd":
        return f"{float(v):.2f} USD"
    if key == "max_minutes":
        return f"{int(v)} min"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)[:120]


def change_facts(a: dict, lang: str = "en") -> list[tuple[str, str]]:
    """3.4.3 — what an « Apply the change » approval changes, field by field: before → after
    (the card showed the project, the owner and the trigger, never the change itself)."""
    pc = a.get("pending_change") or {}
    out = []
    for k, new in pc.items():
        old = a.get(k)
        if old == new:
            continue
        label = _FIELD.get(k, (k, k))[1 if lang == "fr" else 0]
        out.append((label, f"{_fmt_value(k, old, lang)} → {_fmt_value(k, new, lang)}"))
    return out


def approvals(ctx: Ctx) -> dict:
    import agents
    pend = agents.pending_approvals(ctx.pu)
    mine = [a for a in pend["agents"] if (a.get("project") or "default") == ctx.project]
    if not mine:
        return connector.text(_t(ctx.lang,
                                 f"Nothing waits for an approval in « {ctx.project} ».",
                                 f"Rien n'attend d'approbation dans « {ctx.project} »."))
    atts = []
    fr = ctx.lang == "fr"
    for a in mine[:5]:
        if a.get("pending_change"):
            title = (f"Appliquer la modification de l'agent {a['name']} ?" if fr
                     else f"Apply the change to the agent {a['name']}?")
        else:
            title = f"Activer l'agent {a['name']} ?" if fr else f"Activate the agent {a['name']}?"
        facts = [(_t(ctx.lang, "Project", "Projet"), ctx.project),
                 (_t(ctx.lang, "Owner", "Propriétaire"), a.get("owner") or ""),
                 (_t(ctx.lang, "Trigger", "Déclencheur"), json.dumps(a.get("trigger"))
                  if not isinstance(a.get("trigger"), str) else a["trigger"])]
        if a.get("pending_change"):
            facts += change_facts(a, ctx.lang) or [(_t(ctx.lang, "Change", "Modification"),
                                                     _t(ctx.lang, "no difference", "aucune différence"))]
        spec = {"title": title, "facts": facts,
                "open_url": _open(f"/?plane=build&tab=crew&agent={a['id']}"), "lang": ctx.lang}
        tok = signing.issue("agent.activate", str(a["id"]), ctx.project, a.get("owner") or "",
                            card=spec)
        atts.append(cards.approval(spec["title"], spec["facts"], tok, "", spec["open_url"],
                                   user_ids=_viewer_ids(ctx), lang=ctx.lang))
    return {"type": "message", "summary": _t(ctx.lang, "Approvals", "Approbations"),
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                             "content": c} for c in atts]}


def ask_nina(ctx: Ctx, text: str) -> dict:
    import assistant
    if not text:
        return connector.text(_t(
            ctx.lang,
            f"Ask me about « {ctx.project} » — or: status, card: …, decision: …, run <agent>, "
            "approvals.",
            f"Posez-moi une question sur « {ctx.project} » — ou : état, carte : …, "
            "note la décision : …, lance l'agent <agent>, approbations."))
    try:
        out = assistant.chat(ctx.email, text, scope=ctx.scope, channel="teams")
    except ValueError as e:
        return connector.text(f"Nina : {e}" if ctx.lang == "fr" else f"Nina: {e}")
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
    lang = spec.get("lang") or "en"
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
        return _invoke(_t(lang, f"Refused for you: {e}", f"Refusé pour vous : {e}"))
    import audit
    audit.log(email, f"teams.approval.{verb}", f"{row['kind']} {row['ref']}",
              f"requested by {row['requested_by']}", project=row.get("project") or None)
    try:                                    # the card of the other channels follows
        from teams import proactive
        proactive.poke()
    except Exception:  # noqa: BLE001
        pass
    return _invoke(card=cards.decided(title, verb, email, detail, lang=lang))


def _decide_agent(pu: dict, row: dict, verb: str, email: str, spec: dict) -> tuple[str, str]:
    import agents
    import features
    lang = spec.get("lang") or "en"
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
            detail = _t(lang, f"run #{r['id']} queued", f"run #{r['id']} en file")
        else:
            detail = _t(lang, "no run", "pas de run")
    elif row["kind"] == "agent.activate":
        if verb == "approve":
            agents.approve(pu, aid)
            detail = _t(lang, "active", "actif")
        else:
            agents.reject(pu, aid)
            detail = _t(lang, "sent back to draft", "renvoyé en brouillon")
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
    lang = spec.get("lang") or "en"
    title = spec.get("title") or _t(lang, "Approval", "Approbation")
    if row.get("used_at"):
        how = row.get("decision") or "closed"
        detail = "" if how in ("approve", "refuse") else _t(lang, "decided in SOKKAN",
                                                             "décidé dans SOKKAN")
        return _invoke(card=cards.decided(title, how if how in ("approve", "refuse") else "closed",
                                          row.get("used_by") if how in ("approve", "refuse") else "",
                                          detail, lang=lang))
    if p.get("e", 0) < time.time():
        return _invoke(card=cards.decided(title, "closed", "",
                                          _t(lang, "this approval has expired",
                                             "cette approbation a expiré"), lang=lang))
    return _invoke(card=cards.approval(title, [tuple(f) for f in spec.get("facts") or []], token,
                                       spec.get("note", ""), spec.get("open_url", ""), lang=lang))


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
