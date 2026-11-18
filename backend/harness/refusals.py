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
# 4.0 (audit coworker, 18.11) — the Atelier has no task board, no agent team, no monitoring: those
# are the Cockpit's (a developer's). A call to one in an Atelier work is refused with what to do
# instead; never « I have created a task » for something that does not exist for this person.
ATELIER_NO_COCKPIT = (
    "Not available in this conversation: the person works in SOKKAN Atelier, which has no task "
    "board, no team of automatic agents and no monitoring. Do not create, plan or schedule "
    "anything there and never say you did. If they want a work repeated automatically (every "
    "Friday…), say in one sentence that the person who administers SOKKAN sets that up; deliver "
    "this time's document now. Do not mention this message.")
# the cockpit's MCP servers an Atelier work never has (agentchat.ATELIER_MCP_OFF)
ATELIER_MCP_OFF = ("sokkan-board", "sokkan-agents", "sokkan-observability")
# 4.0.1 — the engines' own tools an Atelier work never has, beyond the shell: the internet
# (search, page fetch), sub-agents, scheduled or repeated tasks, workflows and worktrees, and the
# kernel's tools that change its own setup (skills, packs, tool servers). They were still in the
# work's catalogue in 4.0.0 (seen at the launch shoot, 18.11): the model was offered tools the
# person's space refuses. Out of the catalogue like the shell (3.6.8): the legacy adapter puts
# them in `disallowed_tools` and refuses one called anyway by a PreToolUse hook; the kernel
# adapter sends them in `deny_tools`; the cockpit's gate refuses them as a last line. Names per
# engine (the CLI's; the kernel's bare names). A name the engine does not have is harmless.
# No setting brings the internet back into the Atelier: nothing in it relies on it (no kit, no
# connector), and a confidential project's content must never leave the machine.
ATELIER_OFF_LEGACY = (
    "WebFetch", "WebSearch",                                 # the internet
    "Agent", "Task", "ListAgents", "SendMessage",            # sub-agents (Task: older CLIs)
    "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "RemoteTrigger",  # scheduling
    "Workflow", "EnterWorktree", "ExitWorktree")
ATELIER_OFF_KERNEL = (
    "web_search", "web_fetch",                               # the internet
    "delegate", "delegate_status", "delegate_cancel",        # sub-agents
    "agent_draft", "session_send",                           # an agent (and its schedule), another thread
    "skill_install", "skill_remove", "pack_install", "pack_remove",   # the kernel's own setup
    "mcp_server_add", "mcp_server_remove", "mcp_server_read_tools")
ATELIER_OFF = frozenset(ATELIER_OFF_LEGACY) | frozenset(ATELIER_OFF_KERNEL)
# what the agent of an Atelier work is told when it calls one anyway (the hook, the cockpit's
# gate, an engine's own refusal): plain.POLICY_DENIED recognises its first words
ATELIER_NO_TOOL = (
    "Not available in this conversation: the person works in SOKKAN Atelier, where a work reads "
    "the project's documents and saves its document — no internet search or page reading, no "
    "sub-agents, no scheduled or repeated tasks, no workflows. Work from the documents you have; "
    "what they do not say goes under « Points to confirm ». If the person wants something "
    "repeated automatically, say in one sentence that the person who administers SOKKAN sets "
    "that up. Do not mention this message.")
NO_MEMORY = "This conversation does not write memory files directly (removed from this run)."
MEMORY_TOOLS = {"memory_note", "memory"}
# 4.0 (kernel host-3c) — a file tool pointed outside the run's folder: the kernel refuses it
# (`reason: outside_root`) before anything runs. A rule of the space: plain.POLICY_DENIED
# recognises these first words, the agent goes on.
OUTSIDE_ROOT = "Outside this conversation's folder (a rule of the space): not read nor written."
# a card the person (or a rule of the cockpit) refused, when the decision carried no words
DECLINED = "The user declined this action."


def removed(name: str, classification: str, engine_said: str, atelier: bool = False) -> str:
    """The cockpit's sentence for a call to a tool removed from the run, then the engine's.
    ``atelier``: the run is an Atelier work (a tool of :data:`ATELIER_OFF` is said as such)."""
    if atelier and name in ATELIER_OFF:
        head = ATELIER_NO_TOOL
    elif classification == "shell":
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
