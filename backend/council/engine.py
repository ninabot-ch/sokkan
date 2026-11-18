"""engine.py — what makes a role speak.

A council never calls a model itself: it hands a `Call` to an `Engine` and gets a `Reply`.
MVP: `models.ModelEngine` (direct model calls with the project's documents in the context, no
tools). Later an engine can run a SOKKAN kernel session per role behind the same interface.
`FakeEngine` is the deterministic engine of the tests (and of a demo without any model).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol


class EngineError(RuntimeError):
    """The call failed in a way the council reports (`code` is shown to the person)."""

    def __init__(self, message: str, code: str = "engine"):
        super().__init__(message)
        self.code = code


@dataclass
class Call:
    council_id: str
    role: str                      # role id
    phase: str                     # round1 | round2 | disagreements | synthesis
    system: str
    user: str
    tier: str                      # ship | deep | claude (decided by the service, never here)
    project: str = ""
    level: int = 4                 # classification of the project (fail closed)
    claude_model: str = ""
    max_tokens: int = 700
    json_mode: bool = False
    pii: object = None             # pii.PseudoMap of the council (Claude route only)
    who: list = field(default_factory=list)


@dataclass
class Reply:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0
    model: str = ""
    route: str = ""
    cost_chf: float = 0.0


class Engine(Protocol):
    def complete(self, call: Call) -> Reply: ...


def words(s: str) -> int:
    return len(s.split())


class FakeEngine:
    """Deterministic engine: no network, no model. `script` maps (role, phase) or role or phase
    to a text or a callable(call) → text; `synthesis` is the JSON text of the DAF's synthesis
    (or a list of texts, one per request — to test an invalid first answer)."""

    def __init__(self, script: dict | None = None, synthesis=None, disagreements=None,
                 tokens: tuple[int, int] = (1000, 120), fail: Callable | None = None):
        self.script = script or {}
        self._synth = synthesis
        self._dis = disagreements
        self.tokens = tokens
        self.fail = fail
        self.calls: list[Call] = []

    def _next(self, src, default):
        if isinstance(src, list):
            return src.pop(0) if len(src) > 1 else src[0]
        return src if src is not None else default

    def complete(self, call: Call) -> Reply:
        self.calls.append(call)
        if self.fail:
            self.fail(call)
        key = (call.role, call.phase)
        if call.phase == "synthesis":
            text = self._next(self._synth, json.dumps(default_synthesis()))
        elif call.phase == "disagreements":
            text = self._next(self._dis, json.dumps({"disagreements": [
                {"topic": "Delai", "roles": ["code", "achats"], "question": "Peut-on tenir 8 semaines ?"},
                {"topic": "Prix", "roles": ["marcom", "daf"], "question": "Quel prix public ?"}]}))
        else:
            v = self.script.get(key) or self.script.get(call.role) or self.script.get(call.phase)
            text = v(call) if callable(v) else (v or f"[{call.role}/{call.phase}] avis (estime).")
        return Reply(text=text, tokens_in=self.tokens[0], tokens_out=self.tokens[1], ms=5,
                     model=f"fake-{call.tier}", route=call.tier, cost_chf=0.0)


def default_synthesis() -> dict:
    return {
        "perimetre": {"resume": "Prototype fonctionnel et plan de lancement.",
                      "inclus": ["prototype", "notice"], "exclus": ["production en serie"]},
        "budget": {"lignes": [{"poste": "Composants", "montant_chf": 400.0, "source": "devis-composants.md"},
                              {"poste": "Conformite", "montant_chf": 1200.0, "source": "estimé"}],
                   "total_chf": 1600.0},
        "risques": [{"risque": "Delai fournisseur", "gravite": "moyenne", "responsable": "achats"}],
        "kanban": [{"titre": "Specifier le prototype", "responsable": "conception",
                    "critere_de_fin": "Specification relue et signee par le CEO"},
                   {"titre": "Plan de test", "responsable": "qa", "critere_de_fin": "10 cas de test ecrits"}],
        "agents_a_recruter": [{"nom": "agent-notice", "profil": "Redacteur technique",
                               "perimetre": "Rediger la notice a partir de la specification",
                               "outils": ["Read", "Write"], "palier": "ship"}],
        "roles_humains": [{"profil": "Ingenieur electronique", "pourcentage_poste": 20,
                           "perimetre": "Relire le schema"}],
        "calendrier": [{"jalon": "Prototype", "echeance": "semaine 6", "responsable": "conception"}],
    }
