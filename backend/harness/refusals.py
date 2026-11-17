"""What the cockpit is told when an ENGINE turns away a call to a tool the run does not have.

The cockpit removes tools from a run (``RunSpec.deny_tools``, ``RunSpec.shell=False``): the
Atelier's « no shell », the kernel's own ``memory_note``, an agent's allowed list. Both engines
then refuse such a call themselves, in their own words (the CLI « No such tool available: Bash »,
the kernel « Refusé : règle deny terminal() »). For the cockpit it is the same refusal as the
one its own rule would have given (agentchat `gate`): the simple view tells it apart from a real
failure by these words (plain.py ``POLICY_DENIED``, 3.6.7 — a call a rule of the space turns
away is not shown). The engine's own words follow, for the technical detail."""
from __future__ import annotations

NO_SHELL = "No shell in this conversation (removed from this run)."
# 3.6.8 — what the agent of an Atelier work (a run without a shell) is told when it calls a shell
# anyway, by the cockpit's gate or by an engine's own refusal: it names only tools such a work has
# (save with Write / Edit, read with Read) and never a shell tool; plain.POLICY_DENIED recognises
# its first words, so the Atelier does not show it as a failure
ATELIER_NO_SHELL = (
    "No shell in this conversation: the person is not a developer. The folder for the document "
    "already exists — save the document with the Write tool (and change it with Edit), read "
    "files with Read. Do not mention this to the person.")
NO_MEMORY = "This conversation does not write memory files directly (removed from this run)."
MEMORY_TOOLS = {"memory_note", "memory"}
# 4.0 (kernel host-3c) — a file tool pointed outside the run's folder: the kernel refuses it
# (`reason: outside_root`) before anything runs. A rule of the space: plain.POLICY_DENIED
# recognises these first words, the agent goes on.
OUTSIDE_ROOT = "Outside this conversation's folder (a rule of the space): not read nor written."
# a card the person (or a rule of the cockpit) refused, when the decision carried no words
DECLINED = "The user declined this action."


def removed(name: str, classification: str, engine_said: str) -> str:
    """The cockpit's sentence for a call to a tool removed from the run, then the engine's."""
    if classification == "shell":
        head = NO_SHELL
    elif name in MEMORY_TOOLS:
        head = NO_MEMORY
    else:
        head = f"{name} is not in this agent's allowed tools (removed from this run)."
    return f"{head} {engine_said}".strip()


def outside_root(engine_said: str) -> str:
    """The cockpit's sentence for a file tool the engine refused outside the run's folder."""
    return f"{OUTSIDE_ROOT} {engine_said}".strip()


def declined(cockpit_said: str, engine_said: str) -> str:
    """The result of a refused card, as the cockpit decided it: its own words first (a rule of
    the space says it in POLICY_DENIED words, the person's « No » in PERSON_DENIED words), the
    person's refusal when the decision had none, then the engine's."""
    head = (cockpit_said or "").strip()
    try:
        from plain import PERSON_DENIED, POLICY_DENIED
        known = any(m in head for m in (*PERSON_DENIED, *POLICY_DENIED))
    except ImportError:                 # the harness without the cockpit (tests of the contract)
        known = bool(head)
    if not known:
        head = f"{DECLINED} {head}".strip()
    return f"{head} {engine_said}".strip()
