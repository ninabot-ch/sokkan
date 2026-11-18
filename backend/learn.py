#!/usr/bin/env python3
"""learn.py — 4.0: « Remember for next time », offered when a deliverable is validated.

Audit 17.11.2026: nothing was remembered after two validated works; remembering took a sentence
typed on purpose, then 3 approval cards and 3 reviews (« Read the note », then « Approve ») —
six clicks. Now, once a person validates a deliverable, the panel offers 1 to 3 facts taken
from THIS work, in one action:

* what the person corrected — their own later messages in the work (« it is Thomas Bovet, not
  Théo », « nLPD, not the GDPR », « use vous »), never a sentence the model made up;
* the house template the work followed (a document attached whose name says template,
  charter…), so that « as last time » finds it;
* the structure that was validated (its section titles), when no template was attached.

The person reads each fact as it will be written, may change or untick it, and clicks once.
That reading IS the review of the existing quarantine: each fact is written to the
quarantine with its provenance (session, deliverable, person, `via: atelier.learn`) and
approved by that same person in the same request — the two steps journaled as before, the
note's level = the deliverable's (the highest of what its work read), the person's clearance
checked by `quarantine.approve`, a memory that could not index it refused (409) before
anything is written. Nothing is retained without the click; « Not now » is remembered too.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata

MAX_FACTS = 3
MAX_FACT_CHARS = 400
MIN_MESSAGE = 12

_THANKS = re.compile(r"^\s*(ok|okay|d'accord|merci|parfait|super|top|c'est bon|valide|validé|"
                     r"thanks|thank you|great|perfect|fine|good)\b[\s.!]*$", re.I)
_CORRECTION = re.compile(
    r"\b(pas\b|plutôt|au lieu|remplac|corrig|toujours|jamais|vouvo|tutoi|ton\b|format|modèle|"
    r"n'est pas|il s'agit|s'appelle|nom\b|orthograph|chiffre|montant|total|date|article|loi|"
    r"nlpd|rgpd|not\b|instead|always|never|rather|replace|correct|tone|template|wrong|should)",
    re.I)
_TEMPLATE_DOC = re.compile(r"mod[eè]le|template|gabarit|trame|charte|canevas|style|exemple|"
                           r"vorlage|layout", re.I)

TXT = {
    "fr": {"correction": "Pour « {kind} » : {text}",
           "template": "Pour « {kind} », partir du modèle « {doc} » (document du projet) et en "
                       "reprendre la structure et le ton.",
           "structure": "Format validé pour « {kind} » : {sections}.",
           "source": "Source : travail « {work} », livrable « {title} » validé le {day} par {who}. "
                     "Retenu par {who} le {today}.",
           "why.correction": "Votre correction dans ce travail",
           "why.template": "Le modèle que ce travail a suivi",
           "why.structure": "La structure que vous avez validée"},
    "en": {"correction": "For « {kind} »: {text}",
           "template": "For « {kind} », start from the template « {doc} » (a project document) and "
                       "keep its structure and tone.",
           "structure": "Validated format for « {kind} »: {sections}.",
           "source": "Source: work « {work} », deliverable « {title} » validated on {day} by {who}. "
                     "Remembered by {who} on {today}.",
           "why.correction": "Your correction in this work",
           "why.template": "The template this work followed",
           "why.structure": "The structure you validated"},
}


# ---- what the person said after the first request (simple.db) -------------------------------
def _con():
    import simple
    con = simple._con()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS work_messages (
            sid TEXT NOT NULL, at REAL NOT NULL, by TEXT NOT NULL DEFAULT '', text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learned (
            sid TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL,
            by TEXT NOT NULL, at REAL NOT NULL, notes TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (sid, name, sha256)
        );
        """)
    return con


def note_message(sid: str, by: str, text: str) -> None:
    """A later message of the person in a work (simple_api.work_message)."""
    t = (text or "").strip()
    if not t:
        return
    con = _con()
    con.execute("INSERT INTO work_messages(sid, at, by, text) VALUES(?,?,?,?)",
                (sid, time.time(), (by or "").lower().strip(), t[:4000]))
    con.commit()
    con.close()


def messages(sid: str) -> list[str]:
    con = _con()
    rows = [r[0] for r in con.execute("SELECT text FROM work_messages WHERE sid=? ORDER BY at", (sid,))]
    con.close()
    return rows


# ---- the offer ------------------------------------------------------------------------------
def kind_of(title: str, playbook_label: str = "") -> str:
    """« Procès-verbal du comité de direction du 10.12.2026 » → « Procès-verbal du comité de
    direction »: what the work IS, without its date or number (the rule must fit the next one)."""
    if playbook_label:
        return playbook_label
    t = re.sub(r"\s+(du|de|le|of|on|for|pour|au)\s+\d[\d./\- ]*\w*$", "", title or "", flags=re.I)
    t = re.sub(r"[\s—–-]*\b\d{1,2}[./]\d{1,2}([./]\d{2,4})?\b.*$", "", t)
    t = re.sub(r"\s+(n°|no\.?|#)\s*\d+.*$", "", t, flags=re.I)
    t = re.sub(r"\s+(v|version)\s*\d+$", "", t, flags=re.I)
    return t.strip(" ,;:—–-") or (title or "").strip()


_GENERIC_SECTION = re.compile(r"(?i)points? (à|a) (confirmer|v[ée]rifier)|points? to (confirm|check)|sources?$")


def _sections(text: str) -> list[str]:
    """The section titles that make THIS kind of document (« Points to confirm » is in all)."""
    out = [re.sub(r"[*_`]", "", m.group(1)).strip()[:50]
           for m in re.finditer(r"^##\s+(.+)$", text or "", re.M)]
    return [x for x in out if not _GENERIC_SECTION.search(x)][:8]


def _clip(s: str, n: int = MAX_FACT_CHARS) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_SAMPLE_DOC = re.compile(r"^\s*(exemple|example|beispiel|esempio)\s*[-–—:_]|\((exemple|example|beispiel)\b", re.I)


def is_sample(doc_name: str) -> bool:
    """4.0.1 — an EXAMPLE document (the first work's « Exemple - compte rendu de réunion.md »,
    the public demo's fictional files) is never offered as the template to follow: « retain »
    proposed to model the team's minutes on a demo example."""
    if _SAMPLE_DOC.search(doc_name or ""):
        return True
    try:
        import onboarding_samples
        names = {v["doc"][0] for lang in onboarding_samples.SAMPLES.values() for v in lang.values()
                 if isinstance(v, dict) and v.get("doc")}
        if doc_name in names:
            return True
    except Exception:  # noqa: BLE001 — the samples module is optional
        pass
    try:
        import demo_atelier
        return bool(demo_atelier.enabled())
    except Exception:  # noqa: BLE001
        return False


def candidates(project: str, sid: str, name: str, lang: str = "fr") -> list[dict]:
    """The facts offered for this validated deliverable (0 to 3)."""
    import documents
    import plain
    import playbooks
    import simple
    T = TXT.get(lang, TXT["fr"])
    w = simple.get_work(sid) or {}
    text = simple.read_deliverable(project, sid, name)
    title = plain.title_from_markdown(text[:4000]) or plain.human_title(name)
    label = ""
    if w.get("playbook"):
        try:
            label = (playbooks.get(w["playbook"]) or {}).get("i18n", {}).get(lang, {}).get("label", "")
        except Exception:  # noqa: BLE001
            label = ""
    kind = kind_of(title, label)
    out: list[dict] = []
    # 1. the person's corrections, in their words (newest first: the last word counts)
    for m in reversed(messages(sid)):
        s = " ".join(m.split())
        if len(s) < MIN_MESSAGE or _THANKS.match(s) or not _CORRECTION.search(s):
            continue
        out.append({"kind": "correction", "why": T["why.correction"],
                    "text": _clip(T["correction"].format(kind=kind, text=s))})
        if len(out) >= 2:
            break
    # 2. the template the work followed
    try:
        docs = documents.of_session(sid)
    except Exception:  # noqa: BLE001
        docs = []
    tpl = next((d for d in docs if _TEMPLATE_DOC.search(d.get("name") or "")
                and not is_sample(d.get("name") or "")), None)
    if tpl is not None:
        out.append({"kind": "template", "why": T["why.template"],
                    "text": _clip(T["template"].format(kind=kind, doc=tpl["name"]))})
    # 3. the validated structure, when no template says it already
    secs = _sections(text)
    if tpl is None and len(secs) >= 2:
        out.append({"kind": "structure", "why": T["why.structure"],
                    "text": _clip(T["structure"].format(kind=kind, sections=" · ".join(secs)))})
    for i, f in enumerate(out):
        f["id"] = f"f{i + 1}"
    return out[:MAX_FACTS]


def state(sid: str, name: str, sha: str) -> dict | None:
    con = _con()
    r = con.execute("SELECT state, by, at, notes FROM learned WHERE sid=? AND name=? AND sha256=?",
                    (sid, name, sha)).fetchone()
    con.close()
    return dict(r) if r else None


def _mark(sid: str, name: str, sha: str, st: str, by: str, notes: list[str]) -> None:
    con = _con()
    con.execute("INSERT OR REPLACE INTO learned(sid, name, sha256, state, by, at, notes) "
                "VALUES(?,?,?,?,?,?,?)", (sid, name, sha, st, (by or "").lower(), time.time(),
                                         ",".join(notes)))
    con.commit()
    con.close()


def _current(project: str, sid: str, name: str) -> tuple[dict, str]:
    """(the deliverable as listed, its sha) — it must be validated in its current content."""
    import simple
    d = next((x for x in simple.list_deliverables(project, sid) if x["name"] == name), None)
    if d is None:
        raise FileNotFoundError(name)
    p = simple.deliverable_path(project, sid, name)
    return d, simple._sha(p)


def offer(project: str, sid: str, name: str, lang: str = "fr") -> dict:
    d, sha = _current(project, sid, name)
    if not d["validated"]:
        return {"state": "not_validated", "facts": []}
    st = state(sid, name, sha)
    if st is not None:
        return {"state": st["state"], "facts": [], "notes": [n for n in st["notes"].split(",") if n],
                "at": st["at"]}
    facts = candidates(project, sid, name, lang)
    return {"state": "offer" if facts else "none", "facts": facts}


def dismiss(project: str, sid: str, name: str, by: str) -> dict:
    _d, sha = _current(project, sid, name)
    _mark(sid, name, sha, "dismissed", by, [])
    return {"state": "dismissed"}


def _slug(s: str, n: int = 40) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:n].strip("-") or "fait"


class LearnError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code = status, code


def retain(project: str, sid: str, name: str, facts: list[dict], by: str, *,
           clearance: int | None, lang: str = "fr") -> dict:
    """Write the chosen facts (as the person read or changed them) and approve them, in one
    request. → {state, notes}."""
    import househead
    import plain
    import quarantine
    import simple
    import store_backend
    d, sha = _current(project, sid, name)
    if not d["validated"]:
        raise LearnError(409, "not_validated")
    chosen = [_clip(str(f.get("text") or "")) for f in (facts or [])]
    chosen = [c for c in chosen if len(c) >= 8][:MAX_FACTS]
    if not chosen:
        raise LearnError(400, "empty")
    level = househead._level_of(sid)
    if clearance is not None and level > clearance:
        raise LearnError(403, "level")
    try:
        store_backend.require_store(project, level)
    except store_backend.StoreRequired as e:
        raise LearnError(409, e.code, str(e))
    T = TXT.get(lang, TXT["fr"])
    text = simple.read_deliverable(project, sid, name)
    title = plain.title_from_markdown(text[:4000]) or plain.human_title(name)
    work = (simple.get_work(sid) or {}).get("ask") or ""
    try:
        import board
        work = next((x.get("title") for x in board.list_sessions() if x["session_id"] == sid),
                    None) or work
    except Exception:  # noqa: BLE001
        pass
    try:
        import iam
        who = iam.display_name(by) or by
    except Exception:  # noqa: BLE001
        who = by
    day = time.strftime("%d.%m.%Y", time.localtime(d.get("validated_at") or time.time()))
    today = time.strftime("%d.%m.%Y")
    source = T["source"].format(work=_clip(work, 80), title=title, day=day, who=who, today=today)
    names = []
    for f in chosen:
        h = hashlib.sha256(f.encode()).hexdigest()[:6]
        note = f"retenu-{_slug(kind_of(title), 40)}-{h}"
        out = quarantine.write(note, f, f"{f}\n\n{source}\n", {
            "agent": "", "run": "", "session": sid, "user": (by or "").lower(),
            "via": "atelier.learn", "deliverable": name, "sha256": sha},
            project=project, level=level)
        if not out.get("ok"):
            raise LearnError(400, "invalid", out.get("error") or "")
        try:
            quarantine.approve(note, by, project, clearance)
        except KeyError:
            raise LearnError(403, "level")
        if level > 2 and store_backend.enabled():
            try:
                store_backend.get_store().set_level(note, level, project=project, by=by,
                                                    reason="remembered from a validated deliverable")
            except Exception as e:  # noqa: BLE001
                print(f"[sokkan] level floor of {note} not recorded: {e!r}")
        names.append(note)
    _mark(sid, name, sha, "retained", by, names)
    return {"state": "retained", "notes": names}
