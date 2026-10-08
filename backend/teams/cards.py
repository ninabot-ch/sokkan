"""teams.cards — Adaptive Cards 1.5 for Teams (Universal Actions).

Checked against the Adaptive Cards 1.5 schema (vendored in
``tests/fixtures/teams/adaptive-card-1.5.schema.json``) and Microsoft's Universal Action
Model guidance:

* ``version: "1.5"`` with ``Action.Execute`` (``verb`` + ``data``): the click reaches the bot
  as an ``adaptiveCard/action`` invoke, whose answer REPLACES the card for the clicker;
* every ``Action.Execute`` sits in an ``ActionSet`` (older Teams clients ignore ``fallback``
  outside one) and carries an ``Action.Submit`` fallback whose ``data`` repeats the verb —
  the bot handles both (the Submit path cannot return a card: it replies with a message);
* ``refresh`` (``Action.Execute``, verb ``refresh``) lets a viewer's client fetch the
  current state of an approval (decided, expired…). ``userIds`` is left out unless the
  caller knows the MRIs (max 60): Teams then refreshes automatically for chats and channels
  of 60 members or fewer, and offers « Refresh card » otherwise;
* ``fallbackText`` for a client below 1.5, ``msteams.width = Full`` (a Teams host property,
  outside the open schema — the only key the schema test strips).
"""
from __future__ import annotations

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.5"
MAX_REFRESH_USERS = 60


def _card(body: list, fallback_text: str = "", refresh: dict | None = None) -> dict:
    c: dict = {"type": "AdaptiveCard", "$schema": SCHEMA, "version": VERSION, "body": body,
               "msteams": {"width": "Full"}}
    if fallback_text:
        c["fallbackText"] = fallback_text
    if refresh:
        c["refresh"] = refresh
    return c


def _execute(title: str, verb: str, data: dict, style: str = "default") -> dict:
    return {"type": "Action.Execute", "title": title, "verb": verb, "data": data,
            "style": style,
            # older clients: the same action as a Submit (the verb travels in data)
            "fallback": {"type": "Action.Submit", "title": title, "style": style,
                         "data": {**data, "verb": verb}}}


def actions_of(card: dict) -> list[dict]:
    """Every action of a card (ActionSets of the body, then the card's own)."""
    out = []
    for el in card.get("body") or []:
        if el.get("type") == "ActionSet":
            out += el.get("actions") or []
    return out + list(card.get("actions") or [])


def approval(title: str, facts: list[tuple[str, str]], token: str, note: str = "",
             open_url: str = "", user_ids: list[str] | None = None) -> dict:
    """An approval: Approve / Refuse (signed single-use token), refreshable."""
    data = {"sokkan": "approval", "token": token}
    body: list = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
                   "style": "heading"},
                  {"type": "FactSet", "facts": [{"title": k, "value": v or "—"} for k, v in facts]}]
    if note:
        body.append({"type": "TextBlock", "text": note, "isSubtle": True, "wrap": True,
                     "size": "Small"})
    acts = [_execute("Approve", "approve", data, "positive"),
            _execute("Refuse", "refuse", data, "destructive")]
    if open_url:
        acts.append({"type": "Action.OpenUrl", "title": "Open in SOKKAN", "url": open_url})
    body.append({"type": "ActionSet", "actions": acts})
    refresh: dict = {"action": {"type": "Action.Execute", "verb": "refresh", "data": data}}
    if user_ids:
        refresh["userIds"] = list(user_ids)[:MAX_REFRESH_USERS]
    return _card(body, f"{title} — open SOKKAN to decide.", refresh)


def decided(title: str, decision: str, by: str, detail: str = "") -> dict:
    """The card after the decision (replaces the approval for everyone)."""
    label = {"approve": "✅ Approved", "refuse": "⛔ Refused"}.get(decision, "ℹ️ Closed")
    head = f"{label} by {by}" if by else label
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
             "style": "heading"},
            {"type": "TextBlock", "wrap": True, "text": head}]
    if detail:
        body.append({"type": "TextBlock", "text": detail, "wrap": True, "isSubtle": True})
    return _card(body, f"{title} — {head}.")


def notice(title: str, text: str, open_url: str = "") -> dict:
    """A card without decision (e.g. an approval above the channel's level)."""
    body: list = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
                   "style": "heading"},
                  {"type": "TextBlock", "text": text, "wrap": True}]
    if open_url:
        body.append({"type": "ActionSet", "actions": [
            {"type": "Action.OpenUrl", "title": "Open in SOKKAN", "url": open_url}]})
    return _card(body, f"{title} — {text}")


def status(project: str, buckets: dict[str, int], runs: list[str], pending: int,
           level_label: str) -> dict:
    facts = [{"title": b, "value": str(n)} for b, n in buckets.items()]
    body = [{"type": "TextBlock", "text": f"Project {project}", "weight": "Bolder"},
            {"type": "FactSet", "facts": facts}]
    if runs:
        body.append({"type": "TextBlock", "text": "Latest agent runs", "weight": "Bolder",
                     "spacing": "Medium"})
        body += [{"type": "TextBlock", "text": r, "wrap": True, "size": "Small"} for r in runs]
    body.append({"type": "TextBlock", "isSubtle": True, "size": "Small", "wrap": True,
                 "text": f"{pending} approval(s) waiting · shown up to « {level_label} »"})
    return _card(body, f"Project {project}: {pending} approval(s) waiting.")
