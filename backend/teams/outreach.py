"""teams.outreach — « Nina asks for help » (3.4.1).

From the cockpit's Nina panel a person says « find me someone available to help with
‹Upgrade Postgres› » / « demande de l'aide sur la carte #16 ». Nina PROPOSES: the people of the
project who could help, with their availability right now (Teams presence + today's calendar,
through Microsoft Graph) and why, the Teams channel mapped to the project, the exact message
(a real @mention when the person is linked to Entra), and two buttons — Send / Cancel.
**Nothing reaches Teams before the click.** Doctrine of every SOKKAN action: Nina proposes,
the human confirms.

* candidates = the members of the current project with the role ``dev`` or above
  (``helm.project_people``: direct grants, SSO teams, instance roles), minus the requester and
  the assignee of the card when it has one;
* availability = ``graph.presence(oid)`` when the person signed in to SOKKAN with Entra ID
  (``user_links``) + ``calendars.events_for(email, today)`` (busy / out of office now, next free
  slot); ranked available > free (calendar only) > unknown > away > busy > out of office. Graph down, no permission,
  no link: « availability unknown » — never an error that blocks the proposal;
* classification: the channel has a level (its audience). A card above it is named « a
  confidential card (#16) » — never its title — and the proposal says so; among several
  channels, the widest audience that may hear the title wins;
* the proposal carries a signed, expiring, single-use token (``teams.signing``, kind
  ``teams.outreach``); ``send`` consumes it as the requester only, posts to the channel
  (``connector.send`` with a ``mention`` entity — Bot Framework format: ``<at>Name</at>`` in
  the text, ``entities: [{type: mention, text, mentioned: {id, name}}]``), and journals
  ``teams.outreach`` (actor, project, channel, recipient, card).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from zoneinfo import ZoneInfo

import teams
from teams import connector, signing, store

KIND = "teams.outreach"
TTL_S = 3600                                    # a proposal is sent within the hour or redone
# `free` = nothing on the calendar right now but no live presence (no Entra link)
STATES = ("available", "free", "unknown", "away", "busy", "offline", "oof")
RANK = {s: i for i, s in enumerate(STATES)}
# Graph `availability` (never `activity`) → state. Anything else, '' or an error = unknown;
# a presence that was read is never promoted to « available » (3.4.3).
_PRESENCE = {"available": "available", "availableidle": "available",
             "busy": "busy", "busyidle": "busy", "donotdisturb": "busy",
             "away": "away", "berightback": "away", "offline": "offline",
             "outofoffice": "oof", "presenceunknown": "unknown"}


def presence_state(availability: str | None) -> str:
    return _PRESENCE.get((availability or "").strip().lower(), "unknown")
_BUSY_SHOW_AS = ("busy", "oof")

T = {
    "fr": {
        "ask": "{to}, peux-tu aider {who} sur {subject} ?",
        "conf_card": "une carte confidentielle (#{id})",
        "card_line": "Carte SOKKAN #{id}",
        "avail": "disponible dans Teams",
        "cal_free": "agenda libre{until}", "until_next": " jusqu'à {t}",
        "busy": "occupé·e dans Teams", "away": "absent·e de Teams", "offline": "hors ligne dans Teams",
        "oof": "hors du bureau",
        "cal_busy": "en réunion jusqu'à {t}", "cal_busy_day": "en réunion le reste de la journée",
        "cal_oof": "hors du bureau aujourd'hui", "unknown": "disponibilité inconnue",
        "no_link": "pas connecté·e à SOKKAN avec Entra ID : pas de présence, mention en clair",
        "graph_down": "présence / agenda indisponibles",
        "next_free": "libre à partir de {t}",
    },
    "en": {
        "ask": "{to}, could you help {who} with {subject}?",
        "conf_card": "a confidential card (#{id})",
        "card_line": "SOKKAN card #{id}",
        "avail": "available in Teams",
        "cal_free": "calendar free{until}", "until_next": " until {t}",
        "busy": "busy in Teams", "away": "away from Teams", "offline": "offline in Teams",
        "oof": "out of office",
        "cal_busy": "in a meeting until {t}", "cal_busy_day": "in meetings for the rest of the day",
        "cal_oof": "out of office today", "unknown": "availability unknown",
        "no_link": "not signed in to SOKKAN with Entra ID: no presence, plain-text mention",
        "graph_down": "presence / calendar unavailable",
        "next_free": "free from {t}",
    },
}


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def _lv():
    from core import levels
    return levels


def _tz() -> _dt.tzinfo:
    try:
        return ZoneInfo(os.environ.get("SOKKAN_TZ") or "Europe/Zurich")
    except Exception:  # noqa: BLE001
        return _dt.timezone.utc


def _hm(iso: str, tz: _dt.tzinfo) -> str:
    try:
        d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.astimezone(tz).strftime("%H:%M")
    except ValueError:
        return iso


def _name(email: str) -> str:
    """How Nina names a person (3.4.3): the IAM name when set, else the display name the
    IdP or Teams gave (`user_links`), else the local part of the address. The `<at>` of a
    mention carries this name."""
    import iam
    n = iam.display_name(email)
    if n:
        return n
    try:
        n = store.display_name_of(email, teams.tenant_id())
    except Exception:  # noqa: BLE001 — no Teams store: the local part
        n = ""
    return n or email.split("@")[0]


# ---- availability ----------------------------------------------------------------------------
def _calendar_now(email: str, now: _dt.datetime, tz: _dt.tzinfo) -> dict | None:
    """{busy, oof, until, next_start} from today's calendar; None when no calendar provider."""
    import calendars
    if calendars.provider() is None:
        return None
    evs = calendars.events_for(email, now.astimezone(tz).date(), tz)
    busy, oof, until, next_start = False, False, None, None
    end_of_busy = now
    for e in sorted(evs, key=lambda e: e.start):
        if (e.show_as or "busy").lower() not in _BUSY_SHOW_AS:
            continue
        try:
            s = _dt.datetime.fromisoformat(e.start.replace("Z", "+00:00"))
            en = _dt.datetime.fromisoformat(e.end.replace("Z", "+00:00"))
        except ValueError:
            continue
        if s <= end_of_busy < en:                      # overlaps now (or the busy chain)
            busy = True
            oof = oof or (e.show_as or "").lower() == "oof"
            end_of_busy = max(end_of_busy, en)
            until = end_of_busy.isoformat()
        elif s > now and next_start is None:
            next_start = s.isoformat()
    return {"busy": busy, "oof": oof, "until": until, "next_start": next_start}


def availability(email: str, lang: str = "en", now: _dt.datetime | None = None) -> dict:
    """{state, reason, next_free, oid} — never raises: what is unknown is said so."""
    from teams import graph
    t = T[lang]
    now = now or _dt.datetime.now(_dt.timezone.utc)
    tz = _tz()
    oid = store.aad_of(email, teams.tenant_id()) or ""
    pres = ""
    cal = None
    errors = False
    if oid:
        try:
            pres = graph.presence(oid)
        except Exception as e:  # noqa: BLE001 — unknown, not an error
            errors = True
            print(f"[teams] presence of {email} unavailable: {type(e).__name__}")
    try:
        cal = _calendar_now(email, now, tz)
    except Exception as e:  # noqa: BLE001
        errors = True
        print(f"[teams] calendar of {email} unavailable: {type(e).__name__}")
    state = presence_state(pres)
    reasons: list[str] = []
    if state != "unknown":
        reasons.append(t[state if state != "available" else "avail"])
    if cal:
        if cal["oof"]:
            state = "oof"
            reasons.append(t["cal_oof"])
        elif cal["busy"]:
            if state == "unknown":          # live presence wins over the calendar when known
                state = "busy"
            reasons.append(t["cal_busy"].format(t=_hm(cal["until"], tz)) if cal["until"]
                           else t["cal_busy_day"])
        elif state == "unknown" and not errors:
            state = "free"
            reasons.append(t["cal_free"].format(
                until=t["until_next"].format(t=_hm(cal["next_start"], tz)) if cal["next_start"] else ""))
    if state == "unknown":
        reasons.append(t["graph_down"] if errors else t["unknown"])
    if not oid:
        reasons.append(t["no_link"])
    next_free = None
    if cal and cal["busy"] and cal["until"]:
        next_free = cal["until"]
        reasons.append(t["next_free"].format(t=_hm(cal["until"], tz)))
    return {"state": state, "reason": " · ".join(reasons), "next_free": next_free, "oid": oid}


# ---- candidates and channel ------------------------------------------------------------------
def candidates(project: str, requester: str, exclude: tuple[str, ...] = ()) -> list[dict]:
    import helm
    skip = {requester.lower(), *(e.lower() for e in exclude if e)}
    out = []
    for p in helm.project_people(project):
        if p["email"].lower() in skip:
            continue
        out.append({"email": p["email"], "name": _name(p["email"])})
    return out


def pick_channel(project: str, level: int | None) -> dict | None:
    """The channel of the project with the widest audience that may hear the subject
    (level ≥ the card's); none can → the widest one, and the subject is redacted."""
    chans = [c for c in store.channels() if c["project"] == project]
    if not chans:
        return None
    ok = [c for c in chans if level is None or int(c["level"]) >= level]
    return min(ok or chans, key=lambda c: (int(c["level"]), c["name"] or c["channel_id"]))


def resolve_card(subject: str | None, card_id: int | None, project: str,
                 clearance: int | None) -> tuple[dict | None, list[dict]]:
    """(the card, the ambiguous matches) within the requester's clearance."""
    import board
    if card_id is not None:
        c = board.get_card(card_id)
        if c and not c.get("archived") and (c.get("project") or "default") == project \
                and board.visible(c, clearance):
            return c, []
        return None, []
    if not subject:
        return None, []
    hits = board.search_cards(subject, project=project, max_level=clearance, limit=6)
    if not hits:
        return None, []
    exact = [c for c in hits if c["title"].strip().lower() == subject.strip().lower()]
    if exact:
        return exact[0], []
    if len(hits) == 1:
        return hits[0], []
    return None, hits[:5]


# ---- the proposal ----------------------------------------------------------------------------
def _message(lang: str, to: dict, who: str, subject_text: str, card_line: str) -> str:
    at = f"<at>{to['name']}</at>" if to.get("mention") else to["name"]
    msg = T[lang]["ask"].format(to=at, who=who, subject=subject_text)
    return f"{msg}\n\n{card_line}" if card_line else msg


def propose(user_email: str, project: str, clearance: int | None, subject: str | None,
            card_id: int | None, lang: str = "en") -> dict:
    """The structured proposal (no token when it cannot be sent: no channel, no candidate)."""
    lv = _lv()
    lang = lang if lang in T else "en"
    card, ambiguous = resolve_card(subject, card_id, project, clearance)
    if ambiguous:
        return {"kind": "outreach", "project": project, "lang": lang, "subject": subject,
                "ambiguous": [{"id": c["id"], "title": c["title"]} for c in ambiguous]}
    level = None
    if card is not None:
        level = int(card["level"]) if card.get("level") is not None else lv.DEFAULT
    chan = pick_channel(project, level)
    redacted = bool(chan and level is not None and level > int(chan["level"]))
    if card is not None:
        shown = (T[lang]["conf_card"].format(id=card["id"]) if redacted
                 else (f"« {card['title']} »" if lang == "fr" else f"“{card['title']}”"))
        card_out = {"id": card["id"], "title": None if redacted else card["title"],
                    "level": level, "assignee": card.get("assignee") or ""}
        url = _open(f"/?plane=build&tab=board&card={card['id']}")
        card_line = "" if redacted else T[lang]["card_line"].format(id=card["id"]) + (
            f" · {url}" if url else "")
    else:
        shown = f"« {subject} »" if lang == "fr" else f"“{subject}”"
        card_out, card_line = None, ""
    exclude = (card_out["assignee"],) if card_out and card_out["assignee"] and \
        not card_out["assignee"].startswith("agent:") else ()
    who = _name(user_email)
    people = []
    for p in candidates(project, user_email, exclude):
        av = availability(p["email"], lang)
        to = {"email": p["email"], "name": p["name"], "mention": bool(av["oid"]), "oid": av["oid"],
              "state": av["state"], "reason": av["reason"], "next_free": av["next_free"]}
        to["text"] = _message(lang, to, who, shown, card_line)
        people.append(to)
    people.sort(key=lambda p: (RANK[p["state"]], p["name"].lower()))
    out = {"kind": "outreach", "project": project, "lang": lang, "subject": subject,
           "card": card_out, "redacted": redacted,
           "channel": None if chan is None else {
               "id": chan["channel_id"], "name": chan["name"] or chan["channel_id"],
               "level": int(chan["level"]), "level_label": lv.label(int(chan["level"]))},
           "requester": {"email": user_email, "name": who},
           "candidates": [{k: v for k, v in p.items() if k != "oid"} for p in people],
           "to": people[0]["email"] if people else None, "token": None}
    if chan is not None and people:
        out["token"] = signing.issue(KIND, chan["channel_id"], project, user_email, ttl=TTL_S,
                                     card={"proposal": {**out, "people": people}})
    return out


def _open(path: str) -> str:
    base = teams.public_url()
    return f"{base}{path}" if base.startswith("https://") else ""


# ---- the send --------------------------------------------------------------------------------
def post_message(service_url: str, conversation_id: str, text: str,
                 mention: dict | None = None) -> dict:
    """A text message in a conversation, with ONE real @mention when ``mention`` =
    {id: <Entra object id | 29:… | UPN>, name}. The ``<at>Name</at>`` in ``text`` must be the
    entity's ``text`` verbatim (Bot Framework contract)."""
    payload: dict = {"text": text}
    if mention and mention.get("id"):
        tag = f"<at>{mention['name']}</at>"
        if tag not in text:
            raise ValueError("the mention tag is not in the text")
        payload["entities"] = [{"type": "mention", "text": tag,
                                "mentioned": {"id": mention["id"], "name": mention["name"]}}]
    return connector.send(service_url, conversation_id, payload)


def thread_link(channel_id: str, activity_id: str) -> str:
    q = f"tenantId={teams.tenant_id()}"
    team = store.remembered(channel_id).get("team_id")
    if team:
        q += f"&groupId={team}"
    return f"https://teams.microsoft.com/l/message/{channel_id}/{activity_id}?{q}&parentMessageId={activity_id}"


def send(user: dict, token: str, to: str) -> dict:
    """Consume the proposal's token AS the requester and post. {ok, link, text, to}."""
    from teams import proactive
    email = (user.get("email") or "").lower().strip()
    try:
        p = signing.peek(token)
    except signing.Invalid as e:
        raise Refused(400, f"this proposal is no longer valid: {e}")
    if p.get("k") != KIND:
        raise Refused(400, "not an outreach proposal")
    if (user.get("project") or p["p"]) != p["p"]:
        raise Refused(404, "not found")
    try:
        row = signing.consume(token, "", email, "send")
    except signing.Invalid as e:
        raise Refused(409, f"this proposal was already used or expired: {e}")
    if (row.get("requested_by") or "").lower() != email:
        signing.release(row["nonce"])
        raise Refused(403, "only the person who asked Nina can send this message")
    spec = {}
    try:
        spec = (json.loads(row.get("card") or "{}") or {}).get("proposal") or {}
    except ValueError:
        pass
    person = next((x for x in spec.get("people") or [] if x["email"].lower() == to.lower()), None)
    if person is None:
        signing.release(row["nonce"])
        raise Refused(400, "choose a recipient among the people Nina listed")
    channel_id = row["ref"]
    chan = store.channel(channel_id)
    if chan is None or chan["project"] != row["project"]:
        raise Refused(409, "this channel is no longer mapped to the project")
    surl, conv = proactive._reach(channel_id)
    mention = {"id": person["oid"], "name": person["name"]} if person.get("mention") and person.get("oid") else None
    try:
        res = post_message(surl, conv, person["text"], mention)
    except (connector.Error, ValueError) as e:
        signing.release(row["nonce"])
        raise Refused(502, f"Teams did not take the message: {e}")
    import audit
    card = (spec.get("card") or {}).get("id")
    audit.log(email, KIND, channel_id[:120],
              f"to {person['email']}" + (f" · card #{card}" if card else "")
              + (f" · {spec.get('subject', '')[:80]}" if spec.get("subject") and not spec.get("redacted") else ""),
              project=row["project"])
    aid = str(res.get("id") or "")
    return {"ok": True, "to": person["email"], "text": person["text"],
            "link": thread_link(channel_id, aid) if aid else "", "activity_id": aid}


# ---- what Nina says in the panel -------------------------------------------------------------
_SAY = {
    "fr": {
        "intro": "Voici qui pourrait aider sur {subject} dans « {project} ». Je propose de demander à "
                 "**{to}** dans le canal Teams « {channel} » — rien ne part sans votre clic :",
        "no_channel": "Aucun canal Teams n'est relié au projet « {project} » : un admin le fait dans "
                      "Setup › Organization › Teams. En attendant, voici les disponibilités :",
        "nobody": "Personne d'autre que vous n'a un rôle développeur ou plus dans « {project} » — je "
                  "n'ai personne à solliciter. Un admin ajoute des membres dans Setup › Organization › "
                  "Projects & teams.",
        "redacted": "La carte #{id} est au-dessus du niveau du canal « {channel} » : je ne cite pas son "
                    "titre dans Teams (« une carte confidentielle »).",
        "which": "Plusieurs cartes correspondent à « {subject} » : {list} — laquelle ? "
                 "(par ex. « demande de l'aide sur la carte #{first} »)",
        "what": "Sur quel sujet, ou quelle carte ? Par exemple : « trouve-moi quelqu'un de disponible "
                "pour aider sur ‹Upgrade Postgres› » ou « demande de l'aide sur la carte #16 ».",
    },
    "en": {
        "intro": "Here is who could help with {subject} in « {project} ». I suggest asking **{to}** in "
                 "the Teams channel « {channel} » — nothing goes out without your click:",
        "no_channel": "No Teams channel is mapped to the project « {project} »: an admin does it in "
                      "Setup › Organization › Teams. Meanwhile, here is who is available:",
        "nobody": "Nobody but you has a developer role or above in « {project} » — I have no one to "
                  "ask. An admin adds members in Setup › Organization › Projects & teams.",
        "redacted": "Card #{id} is above the level of the channel « {channel} »: I will not name it in "
                    "Teams (« a confidential card »).",
        "which": "Several cards match “{subject}”: {list} — which one? "
                 "(e.g. “ask for help on card #{first}”)",
        "what": "On what, or which card? For example: “find me someone available to help with "
                "‹Upgrade Postgres›” or “ask for help on card #16”.",
    },
}


def reply(proposal: dict) -> str:
    """Nina's answer: a sentence + the ```sokkan-outreach``` block the cockpit renders."""
    lang = proposal.get("lang", "en")
    s = _SAY[lang]
    if proposal.get("ambiguous"):
        lst = ", ".join(f"#{c['id']} « {c['title']} »" for c in proposal["ambiguous"])
        return s["which"].format(subject=proposal.get("subject") or "", list=lst,
                                 first=proposal["ambiguous"][0]["id"])
    if not proposal.get("subject") and not proposal.get("card"):
        return s["what"]
    project = proposal["project"]
    if not proposal["candidates"]:
        return s["nobody"].format(project=project)
    card = proposal.get("card")
    subject = (("« " if lang == "fr" else "“") + (card["title"] if card and card["title"] else proposal["subject"] or "")
               + (" »" if lang == "fr" else "”")) if not proposal.get("redacted") else \
        T[lang]["conf_card"].format(id=card["id"])
    lines = []
    if proposal["channel"] is None:
        lines.append(s["no_channel"].format(project=project))
    else:
        to = next(c for c in proposal["candidates"] if c["email"] == proposal["to"])
        lines.append(s["intro"].format(subject=subject, project=project, to=to["name"],
                                       channel=proposal["channel"]["name"]))
        if proposal.get("redacted"):
            lines.append(s["redacted"].format(id=card["id"], channel=proposal["channel"]["name"]))
    block = {k: v for k, v in proposal.items() if k != "kind"}
    return "\n\n".join(lines) + "\n\n```sokkan-outreach\n" + json.dumps(block, ensure_ascii=False) + "\n```"


# ---- intent (FR / EN), deterministic — no model in the loop -------------------------------------
# paired quotes only: an apostrophe inside « quelqu'un » or « m'aider » is not a quote
_QUOTED = re.compile(r"«\s*(.+?)\s*»|“\s*(.+?)\s*”|‹\s*(.+?)\s*›|\"\s*(.+?)\s*\"|(?<!\w)'([^']+?)'(?!\w)")
_CARD = re.compile(r"(?:\b(?:card|carte|ticket)\s*#?\s*|#)(\d{1,7})\b", re.I)
_FIND_EN = re.compile(r"\b(?:find|get|look\s+for|looking\s+for|need)\s+(?:me\s+|us\s+)?"
                      r"(?:someone|somebody|anyone|a\s+(?:person|colleague|teammate))\b|"
                      r"\bwho(?:'s|\s+is)\s+(?:available|free|around)\b|"
                      r"\b(?:someone|somebody|anyone)\s+(?:available|free)\b", re.I)
_HELP_EN = re.compile(r"\b(?:ask|request|get|need)\s+(?:for\s+)?(?:some\s+)?help\b|\bhelp\s+(?:me\s+)?(?:on|with)\b", re.I)
_FIND_FR = re.compile(r"\b(?:trouve|trouver|trouvez|cherche|chercher|cherchez|il\s+me\s+faut)[- ]?"
                      r"(?:moi\s+|nous\s+)?quelqu.un\b|"
                      r"\bqui\s+(?:est|serait)\s+(?:dispo|disponible|libre)\b|"
                      r"\bquelqu.un\s+(?:de\s+|qui\s+est\s+)?(?:dispo|disponible|libre)\b", re.I)
_HELP_FR = re.compile(r"\bdemand(?:e|er|ez|ons)\s+(?:de\s+l.|l.|un\s+coup\s+de\s+main|)\s*aide\b|"
                      r"\b(?:un\s+coup\s+de\s+main|de\s+l.aide)\b|\baide(?:r|z)?[- ]moi\b", re.I)
_QUESTION = re.compile(r"^(?:how|what|why|where|when|which|can\s+i|do\s+i|comment|pourquoi|où|quand|"
                       r"quel(?:le|s|les)?|est-ce|c'est\s+quoi)\b", re.I)
_SUBJECT = re.compile(r"\b(?:on|with|about|for|regarding|sur|avec|pour|concernant|à\s+propos\s+de)\s+(.+)$",
                      re.I | re.S)


def intent(message: str) -> dict | None:
    """{subject, card_id, lang} when the message asks Nina to find help; None otherwise."""
    m = " ".join((message or "").split())
    if not m or len(m) > 400:
        return None
    if _QUESTION.match(m):
        return None                       # « how do I get help on X? » asks Nina, not a colleague
    fr = bool(_FIND_FR.search(m) or _HELP_FR.search(m))
    en = bool(_FIND_EN.search(m) or _HELP_EN.search(m))
    if not (fr or en):
        return None
    lang = "fr" if (fr and not en) or (fr and re.search(r"\b(quelqu|dispo|aide|carte|sur)\b", m, re.I)) else "en"
    card = _CARD.search(m)
    card_id = int(card.group(1)) if card else None
    subject = None
    q = _QUOTED.search(m)
    if q:
        subject = next(g for g in q.groups() if g).strip()
    elif card is None:
        s = _SUBJECT.search(m)
        if s:
            subject = s.group(1).strip()
            # « pour m'aider sur X » / « to help me with X »: the subject is after the verb
            hm = re.match(r"^(?:m'|t'|me\s+|nous\s+|to\s+)?(?:aider|help)(?:\s+(?:me|us|moi|nous))?\b(.*)$",
                          subject, re.I | re.S)
            if hm:
                s2 = _SUBJECT.search(hm.group(1))
                subject = s2.group(1).strip() if s2 else ""
            subject = subject.strip("?.!:;,").strip()
            subject = re.sub(r"^(?:the|la|le|les|l')\s+(?:card|carte)\s*:?\s*", "", subject, flags=re.I).strip()
            subject = re.sub(r"^(?:the|a|an|this|that|le|la|les|l'|un|une|des|du|ce|cette|ces)\s+", "",
                             subject, flags=re.I).strip()
            if re.match(r"^(?:someone|somebody|quelqu|me\b|moi\b|us\b|nous\b|help\b|aide\b|this\b|that\b|it\b|ça\b|cela\b)", subject, re.I):
                subject = None
    subject = (subject[:160] if subject else "") or None
    strong = bool(_FIND_FR.search(m) or _FIND_EN.search(m))
    if not strong and subject is None and card_id is None:
        return None                       # « how do I get help? » is a question, not a request
    return {"subject": subject or None, "card_id": card_id, "lang": lang}
