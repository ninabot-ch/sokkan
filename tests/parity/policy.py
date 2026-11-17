"""The cockpit's side of a gate, as the bench plays it — the same for every engine.

The bench drives engines through ``backend/harness`` (the contract), not through the whole
api: the cockpit's rules that decide a gate without a person are mirrored here from
``backend/agentchat.py`` (3.6.7 behaviour), and the « person » approves what a person would
approve. Every decision is journaled with who decided and through what, so the journal of
cards can be compared between engines (and against the engine's own journal).

Atelier (simple profile):
* a ``shell`` gate is refused by rule (``policy:simple_profile_no_shell``): the person is
  never asked to approve a command;
* a ``file_write`` to the project's memory from a conversation that was handed documents is
  refused by rule (``policy:documents_memory_write``): a note goes through ``memory_write``
  and its quarantine;
* the later saves of a deliverable the person approved in this turn are approved by rule,
  with the person as decider (``policy:simple_same_document_same_turn``, 4.0 — 3.6.7 refused
  them, ``policy:one_card_per_document``); one card per document and per turn;
* any other ``file_write`` is a card the person approves (``person`` / ``rail``);
* a question is answered with its first choice; other gates are approved.

Cockpit (technical profile, a repository): the developer's « bypass » rule — every gate
approved by rule, journaled as such.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from harness.contract import Decision  # noqa: E402

PERSON = "parity-person"

NO_SHELL = ("No shell in this conversation: the person is not a developer. The folder for the "
            "document already exists — save the document with the Write tool (and change it "
            "with Edit), read files with Read. Do not mention this to the person.")
NO_MEMORY_FILE = ("This conversation was handed documents: it does not write memory files "
                  "directly. Use mcp__sokkan-memory__memory_write — the note waits in quarantine "
                  "until a person approves it.")
# word for word agentchat.py (3.6.7, ``policy:simple_one_approval_per_turn``): plain.POLICY_DENIED
# reads « already approved this document in this turn » as a rule of the space, not a failure;
# the bench's own wording (until 17.11.2026) was shown as « une action a échoué » and told the
# model something the product does not say
ONE_CARD = ("The person already approved this document in this turn: it is saved. Do "
            "not save it again and do not offer another version. Tell them in two or "
            "three sentences what it contains and what remains to confirm (if a part is "
            "still missing, say which one and ask whether to add it); they will ask for "
            "a change if they want one. Do not mention this message.")


def real(path: str) -> str:
    """A path as it is on disk (the kernel adapter hands aliases: symbolic links)."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def inside(path: str, folder: str | Path) -> bool:
    p, d = real(path), real(str(folder))
    return p == d or p.startswith(d.rstrip("/") + "/")


@dataclass
class Card:
    gate_id: str
    kind: str
    tool: str
    target: str            # command or path (real path for files)
    t: float               # seconds since the turn began
    turn: int
    decision: str = ""
    by: str = ""
    via: str = ""
    rule: str = ""
    deliverable: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Policy:
    profile: str                         # atelier | cockpit
    deliverables: Path | None = None
    memory: Path | None = None
    has_documents: bool = False
    journal: list[Card] = field(default_factory=list)
    turn: int = 0
    _turn_t0: float = 0.0
    _saved_this_turn: set = field(default_factory=set)

    def new_turn(self) -> None:
        self.turn += 1
        self._turn_t0 = time.monotonic()
        self._saved_this_turn = set()

    def decide(self, gate) -> Decision:
        target = gate.subject.get("command") or gate.path or ""
        if gate.kind == "file_write" and target:
            target = real(target)
        card = Card(gate_id=gate.gate_id, kind=gate.kind, tool=gate.tool, target=str(target)[:300],
                    t=round(time.monotonic() - self._turn_t0, 1), turn=self.turn)
        d = self._rule(gate, card)
        card.decision, card.by, card.via = d.kind, d.by, d.via
        self.journal.append(card)
        return d

    def _rule(self, gate, card: Card) -> Decision:
        if self.profile == "cockpit":
            card.rule = "rule:bypass"
            if gate.kind == "question":
                return self._answer(gate, by="policy", via="auto_rule")
            return Decision.allow(by="policy", via="auto_rule")
        if gate.kind == "shell":
            card.rule = "policy:simple_profile_no_shell"
            return Decision.deny(NO_SHELL, by="policy", via="auto_rule")
        if gate.kind == "file_write":
            path = card.target
            if self.memory is not None and self.has_documents and inside(path, self.memory):
                card.rule = "policy:documents_memory_write"
                return Decision.deny(NO_MEMORY_FILE, by="policy", via="auto_rule")
            if self.deliverables is not None and inside(path, self.deliverables):
                card.deliverable = True
                if path in self._saved_this_turn:
                    # agentchat.py 4.0: the person's first decision carries the later saves
                    card.rule = "policy:simple_same_document_same_turn"
                    return Decision.allow(by=PERSON, via="auto_rule")
                self._saved_this_turn.add(path)
            card.rule = "person"
            return Decision.allow(by=PERSON, via="rail")
        if gate.kind == "question":
            card.rule = "person"
            return self._answer(gate, by=PERSON, via="rail")
        card.rule = "person"
        return Decision.allow(by=PERSON, via="rail")

    @staticmethod
    def _answer(gate, *, by: str, via: str) -> Decision:
        if "answer" not in gate.options:
            return Decision.allow(by=by, via=via)
        qs = gate.input.get("questions") or []
        answers = {}
        for q in qs:
            opts = q.get("options") or []
            first = opts[0] if opts else "Faites au mieux."
            answers[q.get("question", "")] = first.get("label", first) if isinstance(first, dict) \
                else first
        return Decision.answer(answers or "Faites au mieux.", by=by, via=via)

    # ---- what the journal says -------------------------------------------------------------
    def summary(self) -> dict:
        j = self.journal
        deliv = [c for c in j if c.deliverable and c.decision.startswith("allow")
                 and c.via != "auto_rule"]          # cards the person saw
        per_doc: dict[tuple[int, str], int] = {}
        for c in deliv:
            per_doc[(c.turn, c.target)] = per_doc.get((c.turn, c.target), 0) + 1
        return {
            "cards": len(j),
            "cards_shown_to_person": sum(1 for c in j if c.by == PERSON and c.via != "auto_rule"),
            "cards_by_rule": sum(1 for c in j if c.by == "policy" or c.via == "auto_rule"),
            "deliverable_cards": len(deliv),
            "max_cards_per_document_per_turn": max(per_doc.values(), default=0),
            "second_saves_refused": sum(1 for c in j if c.rule == "policy:one_card_per_document"),
            "same_document_by_rule": sum(
                1 for c in j if c.rule == "policy:simple_same_document_same_turn"),
            "shell_refused": sum(1 for c in j if c.rule == "policy:simple_profile_no_shell"),
            "memory_write_refused": sum(1 for c in j if c.rule == "policy:documents_memory_write"),
            "non_deliverable_writes_approved": sum(
                1 for c in j if c.kind == "file_write" and not c.deliverable
                and c.decision.startswith("allow")),
            "kinds": sorted({c.kind for c in j}),
        }
