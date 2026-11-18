"""connectors.demo — a fake mailbox, calendar and shared folder (provider ``demo``).

For demonstrations, trainings and tests: the same tools and the same cards as the real
providers, on invented data (no real person, no real company). The state lives in
``$SOKKAN_DATA_DIR/connectors-demo/<person>.json`` so the session's MCP process and the API
see the same drafts; « sending » a mail moves it to Sent and nothing leaves the machine.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import ConnectorError, data_dir

_lock = threading.Lock()


def _today(h: int, m: int = 0, days: int = 0) -> str:
    d = datetime.now(timezone.utc).replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(login: str) -> dict:
    t = "conv-offre-2027"
    return {
        "mail": [
            {"id": "m1", "thread_id": t, "folder": "inbox", "subject": "Offre de formation 2027",
             "from": "claire.dumont@exemple-client.ch", "to": [login], "cc": [],
             "date": _today(7, 42), "is_draft": False, "has_attachments": True,
             "body": "Bonjour,\n\nPourriez-vous nous envoyer d'ici vendredi une offre pour trois "
                     "jours de formation Excel avancé, douze participants, en mars ?\n\n"
                     "Merci d'avance,\nClaire Dumont"},
            {"id": "m2", "thread_id": "conv-salle", "folder": "inbox", "subject": "Salle B indisponible jeudi",
             "from": "accueil@exemple-interne.ch", "to": [login], "cc": [], "date": _today(8, 5),
             "is_draft": False, "has_attachments": False,
             "body": "La salle B est en travaux jeudi. La salle C est libre."},
            {"id": "m3", "thread_id": "conv-facture", "folder": "inbox", "subject": "Facture 2026-114",
             "from": "compta@exemple-fournisseur.ch", "to": [login], "cc": [], "date": _today(9, 30, -1),
             "is_draft": False, "has_attachments": True,
             "body": "Veuillez trouver ci-joint la facture 2026-114, échéance à 30 jours."},
        ],
        "events": [
            {"id": "e1", "subject": "Point équipe", "start": _today(8, 30), "end": _today(9, 0),
             "location": "Salle A", "organizer": login, "attendees": [login, "marc@exemple-interne.ch"],
             "body": "Ordre du jour : planning de mars.", "attachments": []},
            {"id": "e2", "subject": "Appel offre 2027 — Claire Dumont", "start": _today(14, 0),
             "end": _today(14, 45), "location": "Visio", "organizer": "claire.dumont@exemple-client.ch",
             "attendees": [login, "claire.dumont@exemple-client.ch"],
             "body": "Discuter du programme et du budget.",
             "attachments": [{"name": "programme-2026.pdf", "size": 48211}], "thread_id": t},
        ],
        "files": {
            "/Offres/modele-offre.md": "# Offre de formation\n\nClient : …\nDurée : …\nPrix par jour : 1 800 CHF\n",
            "/Offres/2026/offre-excel-intermediaire.md": "# Offre Excel intermédiaire\n\n2 jours, 10 participants, 3 600 CHF.\n",
            "/Procédures/note-de-frais.md": "Les notes de frais sont remises avant le 5 du mois.\n",
        },
        "sent": [],
    }


class _State:
    def __init__(self, email: str, login: str):
        safe = re.sub(r"[^a-z0-9@._-]", "_", email.lower())
        self.path = os.path.join(data_dir(), "connectors-demo", f"{safe}.json")
        self.login = login or email

    def load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return _seed(self.login)

    def save(self, d: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)


def _summary(m: dict) -> dict:
    return {k: m[k] for k in ("id", "thread_id", "subject", "from", "to", "cc", "date", "is_draft",
                              "has_attachments")} | {"snippet": m["body"][:300]}


class Mail:
    def __init__(self, email: str, login: str = ""):
        self.st = _State(email, login)

    def list(self, folder: str = "inbox", mailbox: str = "", query: str = "", limit: int = 20) -> list[dict]:
        d = self.st.load()
        f = (folder or "inbox").lower()
        q = (query or "").lower()
        out = [m for m in d["mail"] if m["folder"] == f and (not q or q in (m["subject"] + m["body"] + m["from"]).lower())]
        return [_summary(m) for m in sorted(out, key=lambda m: m["date"], reverse=True)[:limit]]

    def thread(self, thread_id: str, mailbox: str = "") -> list[dict]:
        d = self.st.load()
        out = [dict(m) for m in d["mail"] if m["thread_id"] == thread_id or m["id"] == thread_id]
        if not out:
            raise ConnectorError("Fil introuvable.", 404, "not_found")
        return sorted(out, key=lambda m: m["date"])

    def draft_reply(self, message_id: str, body: str, mailbox: str = "", reply_all: bool = False) -> dict:
        with _lock:
            d = self.st.load()
            orig = next((m for m in d["mail"] if m["id"] == message_id), None)
            if not orig:
                raise ConnectorError("Message introuvable.", 404, "not_found")
            subj = orig["subject"] if orig["subject"].lower().startswith("re:") else f"Re: {orig['subject']}"
            dr = {"id": "d-" + uuid.uuid4().hex[:10], "thread_id": orig["thread_id"], "folder": "drafts",
                  "subject": subj, "from": self.st.login, "to": [orig["from"]],
                  "cc": [a for a in orig["cc"]] if reply_all else [],
                  "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "is_draft": True, "has_attachments": False, "body": body}
            d["mail"].append(dr)
            self.st.save(d)
        return {"draft_id": dr["id"], "subject": subj, "to": dr["to"], "cc": dr["cc"], "mailbox": ""}

    def draft_new(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  mailbox: str = "") -> dict:
        with _lock:
            d = self.st.load()
            dr = {"id": "d-" + uuid.uuid4().hex[:10], "thread_id": "conv-" + uuid.uuid4().hex[:8],
                  "folder": "drafts", "subject": subject, "from": self.st.login, "to": to, "cc": cc or [],
                  "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "is_draft": True,
                  "has_attachments": False, "body": body}
            d["mail"].append(dr)
            self.st.save(d)
        return {"draft_id": dr["id"], "subject": subject, "to": to, "cc": cc or [], "mailbox": ""}

    def get_draft(self, draft_id: str, mailbox: str = "") -> dict:
        m = next((m for m in self.st.load()["mail"] if m["id"] == draft_id), None)
        if not m:
            raise ConnectorError("Brouillon introuvable.", 404, "not_found")
        if not m["is_draft"]:
            raise ConnectorError("Ce message n'est pas un brouillon.", 409, "not_draft")
        return dict(m)

    def send_draft(self, draft_id: str, mailbox: str = "") -> dict:
        with _lock:
            d = self.st.load()
            m = next((m for m in d["mail"] if m["id"] == draft_id and m["is_draft"]), None)
            if not m:
                raise ConnectorError("Brouillon introuvable.", 404, "not_found")
            m.update(folder="sent", is_draft=False, date=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            d["sent"].append({"id": m["id"], "to": m["to"], "at": time.time()})
            self.st.save(d)
        return {"sent": True, "recipients": m["to"] + m["cc"], "demo": True}


class Calendar:
    def __init__(self, email: str, login: str = ""):
        self.st = _State(email, login)

    def events(self, start: str, end: str) -> list[dict]:
        out = [e for e in self.st.load()["events"] if e["start"] < end and e["end"] > start]
        return [{k: e.get(k) for k in ("id", "subject", "start", "end", "location", "organizer", "attendees")}
                | {"online": e.get("location") == "Visio", "show_as": "busy"}
                for e in sorted(out, key=lambda e: e["start"])]

    def event(self, event_id: str) -> dict:
        e = next((e for e in self.st.load()["events"] if e["id"] == event_id), None)
        if not e:
            raise ConnectorError("Événement introuvable.", 404, "not_found")
        return dict(e)

    def busy_of(self, attendees, start, end):
        return {a: [(_today(10), _today(11, 30))] for a in attendees}

    def create(self, subject: str, start: str, end: str, attendees: list[str], location: str = "",
               body: str = "", online: bool = False) -> dict:
        with _lock:
            d = self.st.load()
            e = {"id": "e-" + uuid.uuid4().hex[:10], "subject": subject, "start": start, "end": end,
                 "location": location or ("Visio" if online else ""), "organizer": self.st.login,
                 "attendees": attendees, "body": body, "attachments": []}
            d["events"].append(e)
            self.st.save(d)
        return {"id": e["id"], "demo": True}


class Files:
    def __init__(self, email: str, login: str = ""):
        self.st = _State(email, login)

    def _all(self) -> dict:
        return self.st.load()["files"]

    def list(self, path: str) -> list[dict]:
        p = "/" + (path or "/").strip("/")
        p = "" if p == "/" else p
        seen, out = set(), []
        for f in sorted(self._all()):
            if not f.startswith(p + "/"):
                continue
            rest = f[len(p) + 1:]
            head = rest.split("/", 1)[0]
            if head in seen:
                continue
            seen.add(head)
            is_dir = "/" in rest
            out.append({"path": f"{p}/{head}", "name": head, "is_dir": is_dir,
                        "size": 0 if is_dir else len(self._all()[f].encode()), "modified": ""})
        return out

    def search(self, query: str, path: str = "/") -> list[dict]:
        q = query.lower()
        return [{"path": f, "name": f.rsplit("/", 1)[-1], "is_dir": False, "size": len(v.encode()),
                 "modified": ""} for f, v in self._all().items() if q in f.lower() or q in v.lower()]

    def read(self, path: str) -> bytes:
        p = "/" + (path or "").strip("/")
        if p not in self._all():
            raise ConnectorError(f"Introuvable : {path}", 404, "not_found")
        return self._all()[p].encode()

    def write(self, path: str, data: bytes) -> dict:
        with _lock:
            d = self.st.load()
            p = "/" + path.strip("/")
            target, n = p, 1
            while target in d["files"]:
                stem, dot, ext = p.rpartition(".")
                target = f"{stem} ({n}).{ext}" if dot else f"{p} ({n})"
                n += 1
            d["files"][target] = data.decode("utf-8", "replace")
            self.st.save(d)
        return {"path": target, "demo": True}
