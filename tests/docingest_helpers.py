"""Shared pieces of the doc_ingest tests: an isolated data directory, a scripted model."""
from __future__ import annotations

import json
import re
import threading


class FakeLLM:
    """Stands for the local model. `script(document_text, user_prompt) -> dict` builds the
    answer; by default a tiny deterministic reader:
      a line `DECISION: …`  -> a decision (quote = the line);  a text containing `MENU` or
      shorter than 30 chars -> abstain;  otherwise a reference note."""

    def __init__(self, script=None, tokens=(1000, 200)):
        self.script = script or default_script
        self.calls: list[dict] = []
        self.tokens = tokens
        self._lock = threading.Lock()

    def complete(self, system, user, schema, max_tokens=4096, name="doc_notes"):
        doc = document_of(user)
        with self._lock:
            self.calls.append({"system": system, "user": user, "doc": doc})
        out = self.script(doc, user)
        if isinstance(out, Exception):
            raise out
        return out, {"tokens_in": self.tokens[0], "tokens_out": self.tokens[1]}

    def close(self):
        pass


def document_of(user: str) -> str:
    m = re.search(r"<<<DOCUMENT (\w+)\n(.*)\nDOCUMENT \1>>>", user, re.S)
    return m.group(2) if m else ""


def offered(user: str) -> list[str]:
    return re.findall(r"^- name: ([a-z0-9._-]+) \|", user, re.M)


def answer(**kw) -> dict:
    base = {"verdict": "note", "reason": "", "language": "fr", "document_date": "",
            "classification": "project",
            "reference": {"slug": "", "title": "", "description": "", "body": "", "tags": [],
                          "links": []},
            "decisions": [], "duplicate_of": "", "conflicts": [], "injection_suspected": False}
    ref = kw.pop("reference", None)
    base.update(kw)
    if ref:
        base["reference"] = {**base["reference"], **ref}
    return base


def default_script(doc: str, user: str) -> dict:
    if "MENU" in doc or len(doc.strip()) < 30:
        return answer(verdict="abstain", reason="nothing durable")
    title = doc.strip().splitlines()[0].lstrip("# ").strip()[:80]
    decisions = [{"slug": "", "statement": ln.split(":", 1)[1].strip(), "date": "",
                  "decided_by": "", "quote": ln.strip(), "tags": []}
                 for ln in doc.splitlines() if ln.startswith("DECISION:")]
    return answer(reference={"slug": title, "title": title,
                             "description": f"Document : {title}",
                             "body": " ".join(doc.split())[:600] + " — résumé de test du "
                                     "document, suffisamment long pour être une note.",
                             "tags": ["test", "formation"]},
                  decisions=decisions)


def write_sources(home, sources: list[dict]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "sources.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
