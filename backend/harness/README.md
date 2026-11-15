# The session layer (`backend/harness`)

SOKKAN drives AI sessions: a person talks, a model works with tools, and nothing that
matters happens without a person's approval. Until 3.6 the cockpit was welded to ONE
engine — the Claude Code CLI piloted through its SDK. This package is the seam: a
**contract** between the cockpit and whatever runs the session, and the current engine
rewritten as the **first adapter** of that contract.

```
backend/harness/
  contract.py     events of a run, Gate, Decision, Capabilities, RunSpec — plain dataclasses
  base.py         Harness(ABC): start · send · decide · interrupt · stop · capabilities · set_mode
  registry.py     declared engines, detection, selection by configuration
  legacy.py       adapter 1: the Claude Code CLI through the Claude Agent SDK
  conformance/    scenarios C1–C8 against the interface, a reference engine, the driver
```

Nothing changes for a user in this step. The cockpit still shows the same pane, the same
approval cards, the same costs: `agentchat.py` consumes contract events and **translates
them into the WebSocket flow the front already reads**. Moving the front itself onto the
contract's events is a later step, and so is every new engine.

## The contract in one page

An adapter **emits** events, the cockpit **consumes** them, and nothing else crosses the
package: no SDK type, no CLI message, no HTTP payload of an engine.

| Event | Says |
|---|---|
| `run.started` | the engine is reached; which adapter, which model, resumed from what |
| `run.info` | what the engine learnt later: its own session reference, the model that really answers (`model`), the model it was asked to use (`requested_model`, an alias before any answer) |
| `turn.started` / `turn.ended` | one prompt, from start to result (text, error flag, what the engine says it cost, how long it took by the engine's clock) |
| `assistant.text` / `assistant.think` | the answer and, apart, the reasoning (whole blocks or `delta` fragments) |
| `tool.called` / `tool.result` | a tool call, classified (`shell`, `file_write`, `file_read`, `net`, `mcp`, `subagent`, `other`), and its outcome |
| `gate.requested` / `gate.resolved` | ask before acting, and what was really applied |
| `context.injected` | memory was put in front of the model: by which path, which notes, how much |
| `usage` | tokens of one model call (`scope="message"`) or the engine's own total for the turn (`scope="turn"`) |
| `run.ended` | the run is over; can it be resumed, and from what |
| `error` | `recoverable` (a tool refused by the confinement: the run goes on) or not (the turn is lost) |

Three rules hold for every engine:

1. **One verb for human control: ask before acting.** A `gate.requested` carries a kind
   (`tool`, `shell`, `file_write`, `net`, `mcp`, `question`, `plan`, `budget`), a subject
   and the decisions the engine accepts for it. The cockpit answers with a `Decision`
   (`allow_once`, `allow_session`, `deny`, `answer`, `edit`) through `Harness.decide`. Who
   decides — a rule, a person in the rail, a person in a chat card — is the cockpit's
   business; `by` / `via` travel with the decision and come back in `gate.resolved`.
2. **The cockpit produces the memory, the adapter delivers it.** `RunSpec.context` is a
   provider: the adapter calls it before a turn (and before a sub-agent), puts what it
   returns in front of the model by the best path its engine has, and says so with
   `context.injected`. The scope — project, clearance of the person — was applied by the
   cockpit. An adapter never widens it and never recalls on its own.
3. **The engine counts, the cockpit prices.** `usage` carries tokens, never money that the
   cockpit would trust (`turn.ended.cost_usd` is what the engine *claims*; budgets and
   Operate › Costs use the cockpit's price table).

### Where a gate is born

`Capabilities.gate_authority` says it, and it changes what `decide` means:

* `"core"` — the engine is started at its strictest and asks for everything it was not
  told to run without asking (`RunSpec.auto_allow`). The cockpit is the only policy. This
  is the current engine: the CLI never runs in a bypass mode; « accept edits » and
  « bypass » are rules of the cockpit, applied gate by gate.
* `"engine"` — the engine has its own policy. It raises a gate only when that policy says
  so, refuses some actions without asking, and may refuse what the cockpit allowed. The
  cockpit publishes and decides those gates, and **can only restrict**:
  * a `deny` always holds;
  * an `allow` is a request — the outcome is the `decision` of `gate.resolved`
    (`via="engine"` when the engine overruled);
  * a gate can be settled on the engine's side (its delay ran out, another client
    decided): `gate.resolved` arrives while a card is still open, and the cockpit closes
    the card (journal: nobody here decided);
  * the cockpit never sends a decision the gate does not offer (`Gate.options`): an edited
    input is not sent to an engine that takes none — it is turned into a refusal.

  What such an engine refuses without asking is invisible as a gate; it shows as a failed
  `tool.result`. The cockpit's « never allow » rules (an agent's tool list, the simple
  profile's « no shell ») must therefore ALSO be given to the engine when the run starts
  (`RunSpec.deny_tools`, `auto_allow`) — a rule that only exists as a reaction to a gate
  does nothing when no gate comes.

## Writing an adapter

Subclass `Harness`, declare it in `registry.py`, give it a rig in
`tests/test_harness_conformance.py`.

| Method | Contract |
|---|---|
| `capabilities()` | Honest, and available before `start`. What is absent is greyed out by the cockpit, never emulated. |
| `start(spec)` | Returns the run's single event iterator. Reaching the engine happens on the first iteration: `run.started`, or an exception (nothing was started). |
| `send(prompt)` | Starts a turn, returns its `turn_id`. Events come through the stream until `turn.ended` with that id. One turn at a time. |
| `decide(gate_id, decision)` | Applies a decision; `False` when the gate is unknown or already settled. Callable from another task than the one iterating. |
| `interrupt()` | Stops the turn, keeps the run. |
| `set_mode(mode)` | Only for what must change the ENGINE (`plan`). Raises `Unsupported` otherwise. |
| `stop()` | Ends the run, denies what still waits, emits `run.ended`; idempotent. |

What an adapter must get right (each line is a conformance scenario or a unit test):

* The stream survives its consumer. A cancelled iteration loses nothing; the next one
  continues. A gate blocks only the action it guards — the stream keeps flowing while a
  person thinks.
* Identifiers are stable within a run; `gate_id` may be the engine's own approval id (the
  cockpit treats it as opaque, and uses it as the id of the card).
* `RunSpec.env` goes to the run's TOOLS, never to the model. `RunSpec.mcp_servers` are
  stdio servers the engine launches inside whatever sandbox it has; each entry's `env`
  carries the caller's identity for that server.
* **Identity.** An engine that treats what it is handed as a secret, logs it, or sends it
  off the host must declare `Capabilities.identity = "opaque"` and be given opaque handles
  for `run_id`, `user` and `project` — never a project name or an e-mail address. The
  cockpit keeps the mapping. (Not built yet: see « Not done in this step ».)
* **Usage.** One `usage` event per model call, `scope="message"`. A repeated `message_id`
  supersedes (the largest of each field) — never an addition. An engine that first reports
  a call before its output is known settles it later with `revision=True`. An engine that
  knows the exact total of a turn says it with one `usage` of `scope="turn"` before
  `turn.ended`. The cockpit turns all of it into ONE record per turn (see « Costs » below).
  Declare `Capabilities.usage = "events"` when the events are the only source.
* **Spend caps.** Declare `Capabilities.budget` (`usd`, `tokens`) when the engine enforces
  `RunSpec.budget_usd` / `budget_tokens` itself. A model without a price cannot be capped
  in money: cap it in tokens. A refusal for budget is a `gate` of kind `budget` when a
  person can lift it, an `error` otherwise.
* Errors of the engine become `error` events (`code`, `message`), not exceptions through
  the stream — except at `start`, where failing to reach the engine raises.

### Conformance

`conformance/scenarios.py` holds C1–C8; they only use `Harness`, contract events and a
`Rig` (how to build the harness, how to script the model behind it, what the model was
shown). The suite is parametrised by engine: `tests/test_harness_conformance.py` has one
rig per engine and plays every scenario against each.

`conformance/reference.py` is the **fake engine**: about 300 lines, no SDK, no model, no
transcript — an in-memory engine written from this README and the contract alone. It
passes C1–C8, and the cockpit runs a session on it (gates, memory, usage records, the
journal) with nothing that knows it is not the CLI. That is the proof the contract is
enough for a second engine. It is deliberately unlike the CLI where the contract lets
engines differ: its gates are born on its side, it takes no edited input, it has no
question and no plan unless built with them, its memory path is a preamble, it confines
its own tools, it reports usage only as events and gives no turn total nor duration. It
is not declared in the registry: an instance cannot select it. A test also proves the
scenarios can fail (three cheating variants of it are caught).

```
pytest tests/test_harness_contract.py tests/test_harness_conformance.py tests/test_usage_events.py
```

The CLI adapter is played for real (the CLI bundled with the SDK, in front of a scripted
model: no credit is spent); those cases skip when the SDK ships without its CLI.

| # | Scenario |
|---|---|
| C1 | a shell command that deletes a file → one `shell` gate; denied → the file is still there |
| C2 | the cockpit has a note for the prompt → `context.injected` names it, the model saw it |
| C3 | a write outside the run's space → refused, a recoverable `error`, the run goes on |
| C4 | the run is stopped then resumed → history intact, or `resumable=False` said honestly |
| C5 | three turns → `usage` for each, with a model and tokens, never double counted |
| C6 | nothing runs while the cockpit holds its decision; who decided is echoed; a decision is applied once |
| C7 | a capability declared absent is absent (no gate of that kind); one declared present works; the cockpit greys it out (below) |
| C8 | the engine injects exactly what the cockpit's provider returned — never more |

**Greyed, not emulated (C7, cockpit side).** When the engine of a session lacks plan or
question, the session sends the pane one `{"type": "capabilities", "plan", "question"}`
event at start; the pane drops `plan` from its mode cycle and says why, and
`AgentSession.set_mode("plan")` refuses without touching the engine. An engine with every
capability (today's) adds nothing to the flow.

C6 proves what a second-approver rule needs from an engine. The rule itself (four-eyes on
a session's gate) does not exist in the cockpit yet; today four-eyes covers the approval of
an agent, which does not depend on the engine.

## Costs: one record per turn, two sources

The cockpit keeps what an engine reports in a ledger of its own, `usage_events.db`, next to
the transcript cache (`backend/usage.py`):

| Table | One row per | Written | Read by |
|---|---|---|---|
| `usage_events` | **turn** — the normalised record: `engine`, `model` (+ `models`), `in_tokens`, `out_tokens`, `cache_read`, `cache_write`, `cost_usd` (what the pane showed, `cost_source` = `meter` or `engine`), `list_cost_usd` (price table, call by call; NULL when unpriced), `duration_ms`, `project`, `session_id`, `engine_session`, `basis`, `calls`, `reported`, `is_error`, `stop_reason` | at `turn.ended` (`usage.record_turn`) | the meter of an agent run (settles the turn on it), the session's token counters, the study journal (and so the study export); `usage.turns(sid \| project=)` |
| `usage_calls` | model call (`usage` of scope `message`), deduplicated by `message_id` | as each call is reported (`usage.record_call`) | `usage.session_totals(sid, source="events")`; pricing of a turn |

The tokens of a turn are the engine's own total when it gives one (`scope="turn"`, the CLI
does: it is the figure the meter has settled on since 3.6.4), else the sum of its calls. A
turn the engine ended without a result is recorded (`reported = 0`) and counts nothing.
Every write is best-effort: a ledger that cannot be written never breaks a turn.

Operate › Costs and the budgets still read the CLI's transcripts, unchanged.
`usage.session_totals(sid, source="transcripts" | "events")` gives one session by either
source, priced by the same code; tests prove they agree on a same run, including a real
run of the CLI.

Two things were measured while proving it, and both matter to whoever reads events:

* The CLI reports a call when its first block is complete: its OUTPUT tokens are not known
  yet (1 announced for 5 in the transcript, on the scripted model). The adapter settles the
  calls of a turn against the exact totals of the turn's result (`revision=True`).
* A call the CLI makes AFTER a turn's result (it does when a background sub-agent
  reports) is read with the next turn, or never: it is in the transcript, not in the
  events. For this engine the transcripts stay the source of truth
  (`Capabilities.usage == "transcripts"`).

The operational data of SOKKAN (board, journal, study, usage) is SQLite; Postgres holds the
memory store only. The ledger has no Postgres twin: its schema is versioned with
`PRAGMA user_version` and migrated in place (the first draft of this lot kept the calls in
`usage_events`; they move to `usage_calls`).

## What is still tied to the current engine

The SDK is imported by `legacy.py` only (a test walks the backend to prove it). What
remains outside is vocabulary and files of that engine, not its types:

| Where | What | Why it stays for now |
|---|---|---|
| `agentchat.py` | tool names (`SAFE_TOOLS`, an agent's `tools` / `auto_approve` rules, the titles of cards), mode names (`acceptEdits`, `bypassPermissions`) | they are stored in agents and sent by the front; the rules that only need a KIND already read `gate.kind` / `gate.path` |
| `agentchat.py`, `transcript.py`, `app.py`, `study_export.py` | replay of a session's history from the CLI's transcript; `board.claude_session_id` | history belongs to the engine; a `history()` on the interface comes with the second engine |
| `usage.py` | the transcripts as the source of Costs | see above |
| `sandbox.py` | the confinement policy answers in the CLI's hook format and names its tools | the next engine confines its tools itself (`Capabilities.sandbox = "engine"`) |
| `backend/runner/` | session containers and pods that run the CLI | they are this engine's execution substrate; the SDK transport that used to live there is now in `legacy.py` |
| `memrecall.py` (`cli_settings`), `memory/recall_hook.py` | command hooks of TERMINAL sessions | terminal sessions are not driven through the contract |
| `llm.py`, `connectai.py` | the model routing handed to a session as environment variables of the CLI | becomes a typed part of `RunSpec` when an engine needs something else |
| `agentcost.py` | the meter reads usage in the Messages API's field names | internal shape; fed from `usage` events and settled on the turn's record |

## The guard

`tests/test_harness_contract.py::test_the_sdk_is_imported_by_the_adapter_only` walks
`backend/`, `memory/`, `scripts/`, `cli/` and `magnitude/` and fails when an engine SDK
(`claude_agent_sdk`, its former name `claude_code_sdk`, or the `anthropic` client) is
imported anywhere but `backend/harness/legacy.py` — by `import`, `from … import`,
`importlib.import_module("…")` or `__import__("…")`. The appliance router
(`shiprouter.py`) speaks HTTP to the models it routes to; it imports no SDK.

## Selecting the engine

`SOKKAN_ENGINE_RUNTIME` (compose; `engine.runtime` in the Helm chart) names the engine of
new sessions and runs. Empty, or a name that is not declared, = the first declared engine,
`legacy`. One engine is declared in this release.

## Not done in this step

* The front still receives the historical flow (translated in `agentchat._consume`); only
  the `capabilities` event is new, and only an engine that lacks something sends it.
* No second engine is declared. Setup › Engines does not show the session engine yet
  (`registry.describe()` is ready for it): with one engine there is nothing to choose.
* Opaque identity handles (`Capabilities.identity = "opaque"`) are specified, not minted.
* Costs still read transcripts; switching an engine to the `events` source (and never
  counting a session twice) is part of bringing that engine in.
* Events are not journalled one by one by the cockpit; the journal of validations and the
  audit log are written where they were.
* No `history()` on the interface: the replay of a session reads the CLI's transcript.
