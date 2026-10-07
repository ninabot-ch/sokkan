# The working method

How a team works with SOKKAN Enterprise, from a manager's goal to production. Every gate has
a human; every decision ends up in the project memory. Steps marked **○** are planned
(3.2 later lots, 3.3 "Helm"); **◐** in progress on the 3.2 branch; the rest is shipped.

<p align="center"><img src="method.svg" width="100%" alt="Working method: 1 project card, 2 engineer kanban, 3 spawn a session, 4 agents, 5 preview for validation, 6 human approval, 7 merge request, 8 operate, 9 decisions to CortHeXis, progress rolls up to the manager"/></p>

## The nine steps

| # | Step | Who | What happens | Status |
|---|---|---|---|---|
| 1 | **Project card** | manager | The manager writes the goal on a card at project level; Nina asks the questions that make it actionable (scope, done criteria, constraints). | ○ 3.3 Helm |
| 2 | **Engineer kanban** | Nina + manager | The card splits into engineer cards on the project board; the parent's context flows down to every child card. Nina proposes the split; the manager accepts or reframes. | ○ 3.3 Helm |
| 3 | **▶ Spawn a session** | engineer | A card becomes a session. The server runs the memory search itself and injects the project's notes in the first message; the session proposes a plan and **waits for the go**. From the cockpit, VS Code or the CLI. | ● |
| 4 | **Agents do the routine** | Crew | Recurring or one-shot work (nightly audit, log triage, report, triage on an alert) runs as agents: one card each, allowed tools, secrets by name, budget per run. A session may only *propose* an agent; a human approves (four-eyes on request). | ● |
| 5 | **Preview = validation** | reviewer | The change runs in a preview; the reviewer looks at the result, not the diff. Sharing a session or a preview read-only or read-write with a reviewer is the validation step of 3.2. | ● preview · ○ sharing (3.2) |
| 6 | **Human approval** | reviewer | Every mutating tool call waits for a click (HITL); agent creation and changes need an approver, distinct from the proposer and the owner in `four_eyes` mode. | ● |
| 7 | **Merge request** | engineer | The session pushes a branch and opens the merge request **with the person's own forge token**: the forge refuses what the person may not do. | ○ 3.2 lot 5 |
| 8 | **Operate** | ops team | After the merge: an alert becomes an incident with a diagnosis session already started (with the project memory), runbooks are replayed as supervised sessions. Agent failures open incidents too. | ● |
| 9 | **Decisions → CortHeXis** | everyone | The durable facts and decisions (one fact per note) go to the project memory; notes written by agents wait in quarantine for a human review. The next session starts knowing them. | ● |
| ↺ | **Progress rolls up** | — | Card states, runs and incidents roll up to the parent card and the manager's direction view; morning brief (calendar + kanban). | ○ 3.3 Helm |

## Roles and gates

| Gate | Decided by | Enforced by |
|---|---|---|
| Who can see / work on a project | IdP group or forge access level | project gate (server side) ◐ |
| A tool call that changes something | the person driving the session | approval widget; `allowed_tools` of an agent |
| An agent exists or changes | owner / admin / a second person (`SOKKAN_AGENTS_APPROVAL`) | agents API (403 with the reason) |
| A note from an agent enters the memory | maintainer+ of the project | quarantine outside the indexed directory |
| Code reaches a protected branch | the forge's own rules | the forge (push with the person's token) ○ |
| Spend | budgets per session / day / run (per project ○) | hard stop at the limit |

## What the manager sees today and in 3.3

* **Today (3.1 / 3.2):** the project board, the Crew deck (state of every agent at a glance),
  Costs, the Journal.
* **3.3 Helm:** hierarchical cards (manager → engineer), automatic progress roll-up, a
  direction view across projects, Nina suggesting reframes, the morning brief.
