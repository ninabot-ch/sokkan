#!/usr/bin/env python3
"""claudepii.py — the « PII sandwich » around « Write with Claude », on the api's side.

Decision of 18.11.2026 (4.0): Claude is offered on demand for the work of projects that are NOT
confidential. No job is refused Claude any more (the `409 claude_role` of the job kits is gone);
instead, the personal data a work would send is found, the person is told, and asked:

    « Anonymiser et envoyer à Claude »   the values are replaced on the machine by stable
                                         pseudonyms ([PERSONNE_1], [IBAN_1] …), Claude writes
                                         from those, the values are put back in what it answers
                                         and in the files the agent writes;
    « Rester sur le modèle local »       the work runs on the machine's model, nothing leaves.

Nothing is sent to Claude before that choice: `/api/simple/start` (and a follow-up message of a
Claude work) answers `409 claude_pii` with the categories and counts — never the values — and
creates nothing. The choice is journaled (`hitl_journal.pii_decision`: who, when, what).

A confidential or restricted project never reaches this point: `claudetier.gate` refuses it
first, whatever is said here (rule D9, enforced by the server).

Where the sandwich is made: in the router (backend/shiprouter.py), on EVERY request of a Claude
work, through `POST /api/internal/claude-pii` (shared router secret, never through the proxy).
The api holds the work's mapping and the organisation's directory; the router sends the request,
gets it back pseudonymised with the pseudonym → value table for the answer, and puts the values
back in the stream the CLI reads. So what the agent reads on its own during the work (a file, a
note, a tool's result) is pseudonymised too, not only what the person typed — and a work whose
first message carried nothing personal is still covered if a document read later does.
"""
from __future__ import annotations

import threading

import pii

MAX_DOC_CHARS = 400_000          # what is scanned of one attached document for the card
_lock = threading.Lock()

TEXT = {
    "fr": {
        "found": "Votre demande ou vos pièces jointes contiennent des données personnelles.",
        "ask": "Elles peuvent être anonymisées sur la machine avant l'envoi à Claude, ou le travail peut rester sur le modèle local.",
    },
    "en": {
        "found": "Your request or your attachments contain personal data.",
        "ask": "It can be anonymised on the machine before it goes to Claude, or the work can stay on the local model.",
    },
}


# ---- who the organisation knows ------------------------------------------------------------
def names(extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The names the organisation profile knows (directory and signatories), plus ``extra``."""
    out: list[str] = []
    try:
        import orgprofile
        p = orgprofile.get(reveal=False)
        out += [x.get("name") or "" for x in (p.get("people") or [])]
        out += [x.get("name") or "" for x in (p.get("signatories") or [])]
    except Exception:  # noqa: BLE001 — no profile: the patterns still run
        pass
    out += list(extra)
    seen, keep = set(), []
    for n in out:
        n = " ".join((n or "").split())
        if len(n) >= 3 and "@" not in n and n.casefold() not in seen:
            seen.add(n.casefold())
            keep.append(n)
    return keep


def doc_text(doc: dict) -> str:
    """What the agent would read of an attached document (its text copy)."""
    try:
        import documents
        p = documents.text_path_of(doc)
        if p is None:
            return ""
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_DOC_CHARS)
    except Exception:  # noqa: BLE001
        return ""


# ---- the card ------------------------------------------------------------------------------
def scan(texts: list[str], who: list[str]) -> list[pii.Finding]:
    found: list[pii.Finding] = []
    for t in texts:
        found += pii.detect(t or "", who)
    return found


def card(found: list[pii.Finding], lang: str, step: str = "start") -> dict:
    """The body of `409 claude_pii`: what was found (categories, counts, masked hints) and the
    two choices — the browser shows it as it is. On a follow-up of a Claude work the second
    choice is « do not send » (the work itself already runs with Claude)."""
    lang = "fr" if lang == "fr" else "en"
    s = pii.summary(found, lang)
    return {"code": "claude_pii", "message": TEXT[lang]["found"] + " " + TEXT[lang]["ask"],
            "pii": {**s, "step": step,
                    "choices": ["anonymize", "local"] if step == "start" else ["anonymize", "cancel"]}}


# ---- the mapping of a work -----------------------------------------------------------------
def load(sid: str) -> pii.PseudoMap:
    import simple
    return pii.PseudoMap(simple.get_pii_map(sid))


def seed(sid: str, texts: list[str], who: list[str]) -> dict[str, int]:
    """Give the pseudonyms in the order the person wrote things (« [PERSONNE_1] » is the first
    name of the request), before the first request leaves."""
    import simple
    with _lock:
        m = load(sid)
        found: list[pii.Finding] = []
        for t in texts:
            found += m.anonymize(t or "", who)[1]
        simple.save_pii_map(sid, m.to_dict())
    return pii.counts(found)


def anonymize_request(sid: str, project: str, payload: dict) -> tuple[int, dict]:
    """The router's call: (status, body). 404 when ``sid`` is not a Claude work of ``project``
    (the router then refuses: nothing leaves without its sandwich)."""
    import simple
    if project.startswith("prj-") or sid.startswith("run-"):
        # an opaque engine was handed the handles of the project and of the run, never their names
        from harness import identity
        r = identity.resolve(project)
        project = r[1] if r and r[0] == "project" else project
        r = identity.resolve(sid)
        sid = r[1] if r and r[0] == "run" else sid
    w = simple.get_work(sid or "") or {}
    if not sid or w.get("tier") != "claude" or w.get("project") != project:
        return 404, {"detail": "not found"}
    lang = w.get("lang") or "fr"
    who = names()
    with _lock:
        m = load(sid)
        before = len(m)
        out, found = pii.anonymize_payload(payload, m, who, lang)
        if len(m) != before:
            simple.save_pii_map(sid, m.to_dict())
        restore = dict(m.values)
    return 200, {"payload": out, "restore": restore, "found": pii.counts(found)}
