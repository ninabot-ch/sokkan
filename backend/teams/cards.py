"""teams.cards — Adaptive Cards (schema 1.5, Universal Actions ``Action.Execute``)."""
from __future__ import annotations

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


def _card(body: list, actions: list | None = None) -> dict:
    c = {"type": "AdaptiveCard", "$schema": SCHEMA, "version": "1.5", "body": body}
    if actions:
        c["actions"] = actions
    return c


def approval(title: str, facts: list[tuple[str, str]], token: str, note: str = "") -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]}]
    if note:
        body.append({"type": "TextBlock", "text": note, "isSubtle": True, "wrap": True,
                     "size": "Small"})
    acts = [{"type": "Action.Execute", "title": t, "verb": verb,
             "data": {"sokkan": "approval", "token": token}, "style": style}
            for t, verb, style in (("Approve", "approve", "positive"),
                                   ("Refuse", "refuse", "destructive"))]
    return _card(body, acts)


def decided(title: str, decision: str, by: str, detail: str = "") -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "wrap": True,
             "text": f"{'✅ Approved' if decision == 'approve' else '⛔ Refused'} by {by}"}]
    if detail:
        body.append({"type": "TextBlock", "text": detail, "wrap": True, "isSubtle": True})
    return _card(body)


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
    return _card(body)
