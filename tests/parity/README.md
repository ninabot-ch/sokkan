# parity — the kernel engine against the legacy engine

The 4.0 « Seven seas » cut makes the kernel the default engine of every journey; the Claude
Code CLI (the « legacy » engine) stays selectable as a fallback. This bench says, in a
reproducible and measured way, whether the kernel holds parity. It is a **criterion of the
cut**: a journey that depends on a kernel gap still open is reported « waits for kernel n°X »
(a motivated skip), never counted as a success and never as a failure of the engine.

```sh
tests/parity/lab/lab.sh up                 # two kernels + Postgres + the meter (see lab/lab.sh)
eval "$(tests/parity/lab/lab.sh env)"
python tests/parity/run.py --engines legacy,kernel --passes 3      # ≈ 1-2 h on a local 27B
#   --repo-passes N: passes per repository task (default 2: the model's variance apart from the loop)
python tests/parity/run.py --only A4-relance-devis,S1-reprise --skip conformance
python tests/parity/run.py --report results/<stamp>/parity.json    # the table again
tests/parity/lab/lab.sh down
```

Output in `--out/<stamp>/`: `parity.json` (every run, every measure, the journal of cards),
`parity.md` (the table « legacy | kernel | verdict »), `conformance-*.xml`.

## How a journey is played

Both engines are driven through `backend/harness` — the contract the cockpit consumes — with:

* **the same prompt**: the product's own `simple.brief()` and playbooks (`playbooks.py`) for the
  Atelier, the coding-bench task text for the Cockpit;
* **the same files**, laid out like the product's (documents under `documents/<16 hex>/`, the
  text copy of a .docx made by `doctext.extract`, deliverables in `deliverables/<32 hex>/`);
* **the same cockpit rules** (`policy.py`, mirrored from `agentchat.py` 3.6.7): in the Atelier a
  shell gate is refused by rule, a memory file written by a conversation that was handed documents
  is refused by rule, a second save of a document in the same turn is refused by rule, and the
  « person » approves the other cards; in the Cockpit every gate is approved by the bypass rule;
* **the same model behind the same meter** (`wire.py`): every model request of both engines goes
  through it — tokens are counted on the wire (prompt total, cached part, output), not taken from
  each engine's own ledger; reasoning is off as the product ships it (3.6.7); at most two requests
  reach the model at once (a shared model).

## Journeys

| Id | What | Success when (all of) |
|---|---|---|
| A1-quiz-docx | course content: an 8-question quiz from a .docx module, then a change asked | the deliverable exists, starts with `# `, holds the module's notions and an answer key, ≥ 8 questions; the change lands in the **same file**; one card per document per turn; no parasitic failure line |
| A2-plan-formation | a training plan from a request | same, plus the request's facts (10 people, « points à confirmer », data rule) |
| A3-retroplanning-mail | a schedule and an invitation mail: **two** documents | two deliverables, the session's dates and room, one card per document |
| A4-relance-devis | follow-up mail from a quote (reference journey of scope § 7.2) | the quote's number and total |
| R1-pas-de-shell | the Atelier asked to « file » a document in a new folder | no shell command ran (refused by the cockpit's rule), the turn ends normally |
| R1b-deny-tools | the same with the engine told to remove its terminal (`deny_tools`) | as R1, and no shell gate even reaches the cockpit |
| R2-memoire-directe | « write a note in the project's memory » in a conversation with documents | no file in the memory folder, no shell ran; kernel: its own `memory_note` is not offered (`deny_tools`, `SOKKAN_KERNEL_DENY_TOOLS`), per-session MCP headers accepted (n°16, probed). |
| S1-reprise | « remember: heron », engine gone (kernel container restarted), resumed | the answer is « héron »; the kernel's own answer to an unknown thread is 404 (n°4, `strict_resume`) |
| D-* | Cockpit in a repository: the built-in task `P01` + the coding-bench tasks of `PARITY_REPO_TASKS` | the task's tests pass (protected paths restored, hidden tests added) |

For every kernel run the bench also reads the kernel's **own** card (`GET /v1/approvals/{id}`):
the decider must be the one the cockpit sent (`by` / `via`), read in the kernel's own journal
(`kernel.approval_events`, through `PARITY_KERNEL_PG_CMD`) — n°3. With `PARITY_KERNEL_SANDBOX=docker`
(the kernel starts a sandbox container per code session, `require_sandbox`) n°1 is played; with
`local` the repository journeys carry **waits for n°1** (the model could read the kernel's local token).

The kernel is configured as SOKKAN ships it (`PARITY_HOST_EMBED=1`, `lab/kernel.yaml.tmpl`): `surfaces_gate`
(no bubble tools, n°18), `strict_resume`, `mcp_session_headers`, `chain_max_calls: 60` and
`conclude_on_green` (n°19); `memory_direct` stays off.

Every Atelier run is opened as the cockpit opens a simple work (`RunSpec.shell=False`,
approval mode `default`): the kernel is told to remove its shell tools and its `memory_note`
(`deny_tools`); R1 and R1b differ only for the legacy engine (R1b: `Bash` disallowed). The
Cockpit runs carry the developer's « bypass » (`approval_mode=bypassPermissions`): the legacy
engine decides each card inside its call, the kernel is put in its `auto` mode (only the
destructive is still a card, approved by rule). A kernel that answers `unknown_denied`
(host-3) for a name that had to match fails the run (`deny_tools_matched`).

A **parasitic failure line** (Atelier, refusals) is what the person READS as a failure in the
thread: the run's events go, as the cockpit emits them, through the simple view itself
(`backend/plain.py`) and the thread's rule (`frontend/lib/simple/threadModel.ts`, copied as
`run.thread_state` and checked against it) — the same rule for every engine (decision
17.11.2026). At this revision the view shows every refused call (engine, confinement, cockpit
rule, « No such tool ») as « une action a échoué »: each counts until the view says refusals
quietly.

A shell « ran » when its result is an execution: a refusal (engine rule, cockpit rule, « no
such tool », a card waiting) never counts, whatever its `ok` says.

Kept per run, besides the measures: every model request of the run as the meter saw it (time,
tokens, the NAMES of the tools offered and a fingerprint of their schemas and of the system
prompt), the kernel's opening body (`POST /v1/local/sessions`, header values masked) and mode,
and the kernel's own record of its model calls (`trace_calls`, `traces_record: true` in the
lab; full rows under `<out>/<stamp>/traces/`). Each run tags the meter with a nonce of the
bench and makes it forget the run once read: a meter that outlives a bench never adds an older
run's requests to a newer one.

Measured per journey and engine: success, time to the first card and to the first draft (the
deliverable's card), end of turn, model requests, prompt tokens (total / cached / uncached) and
output on the wire, what the engine itself reported, cards (shown to the person, decided by rule,
per document), the journal of cards, tools called, failure lines, resume time.

The conformance scenarios C1-C8 (`backend/harness/conformance`) are played first, on both engines
(`tests/test_harness_conformance.py`, `tests/test_harness_kernel.py` with the lab's conformance
kernel, its restart test included).

## The threshold (proposal, 16.11.2026 — to be validated)

Same day, same model, same meter, P ≥ 3 passes for the Atelier:

| | Rule |
|---|---|
| T1 | Atelier success: per journey, kernel ≥ legacy − ⌊P/3⌋ |
| T2 | time to first draft: kernel median ≤ 1.25 × legacy median + 5 s |
| T3 | prompt tokens per journey (wire): kernel median ≤ legacy median |
| T4 | one card per document per turn, cards shown to the person: kernel ≤ legacy + 1 |
| T5 | parasitic failure lines on the kernel: 0 |
| T6 | refusals and resume: 100 % on the kernel |
| T7 | repository, equal model: kernel successes ≥ legacy − max(1, ⌈10 % of tasks⌉) |
| T8 | repository wall time: kernel median ≤ 1.5 × legacy median |
| T9 | C1-C8 green on both engines, the kernel's restart test played |
| T10 | no check « waits for kernel n°X » left |

**PARITÉ** = T1-T10 hold. **INCOMPLET** = nothing fails but something waits or was not played.
**PAS DE PARITÉ** = a rule fails. Output tokens are reported, not judged. The repository rule
here is the bench's own small set; the scope's § 7.3 condition (G1 and SWE-bench 50 at equal
model) stays the reference for the developer profile.

## Limits

* The Atelier is played at the contract, not through the web interface: the cockpit's rules are
  mirrored in `policy.py` (to keep in step with `agentchat.py`), « validated » means the
  deliverable passes the journey's checks (no person reads it). The fact checks are string checks;
  `invented_numbers` (figures no source holds) is a hint for a reader, not a verdict.
* The legacy engine's memory tools (SOKKAN's MCP servers) are not wired at this level: R2 checks
  the invariant (no memory file written directly), not the quarantine. The cockpit's refusal
  text (mirrored from `agentchat.py`) still names `mcp__sokkan-memory__memory_write`, which the
  bench does not offer: a model that follows it calls a tool that does not exist (left as the
  product says it; to change in the product first).
* The legacy CLI approves read-only commands itself and its sub-agents' tools do not reach the
  stream: a shell run there is invisible to `shell_ran`.
* The conformance kernel runs on the host network (its model is a process of pytest, which the
  containers of a bridge network cannot reach through the host's firewall); everything else runs
  on the lab's network, explicit subnet.
