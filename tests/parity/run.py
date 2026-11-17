"""The parity bench: the kernel engine against the legacy engine (Claude Code CLI), same
journeys, same model, same meter. Criterion of the 4.0 cut (docs: tests/parity/README.md).

    python tests/parity/run.py --engines legacy,kernel --passes 3 --out <dir>
    python tests/parity/run.py --only A4-relance-devis,S1-reprise --engines kernel
    python tests/parity/run.py --report <dir>/parity.json      # table only, from a result file

Settings (environment, nothing about a host is written in the repository):

    PARITY_UPSTREAM       the model, OpenAI- and Anthropic-compatible (a vLLM: /v1/chat/completions
                          and /v1/messages), e.g. http://127.0.0.1:8004/v1
    PARITY_MODEL          its model name (sent by both engines; default: what /v1/models lists first)
    PARITY_WIRE_URL       a meter already running (the lab's `meter` service, published on
                          127.0.0.1): the runner tags and reads it by HTTP. Unset = an in-process
                          meter on PARITY_WIRE_BIND:PARITY_WIRE_PORT (default 127.0.0.1:18790)
    PARITY_THINKING       1 = leave the model's reasoning on (default 0, as the product ships)
    PARITY_MAX_CONCURRENT model requests at once, default 2 (a shared model)
    PARITY_WORKDIR        where runs happen; the kernel sees it at the SAME path (default
                          /srv/sokkan-parity/ws)
    PARITY_KERNEL_URL     the kernel's API, PARITY_KERNEL_TOKEN_FILE its local token
    PARITY_KERNEL_RESTART_CMD  how to restart the kernel (resume journey), e.g. `docker restart …`
    PARITY_KERNEL_SANDBOX the kernel's tool backend as configured (local | docker): `local` =
                          gap n°1 open
    PARITY_LEGACY_MODEL   the model name the CLI is started with (default claude-sonnet-4-5; the
                          meter replaces it by PARITY_MODEL on the wire)
    PARITY_REPO_TASKS     a folder of repository tasks (coding-bench format) next to the built-in one
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

import journeys as J  # noqa: E402
import policy as P  # noqa: E402
import wire as W  # noqa: E402
from harness import contract  # noqa: E402
from harness.contract import GateRequested, RunEnded, TurnEnded, Unsupported  # noqa: E402

ENGINES = ("legacy", "kernel")
GAP = "waits"          # status of a check that depends on an open kernel gap


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ============================================================================================
# engines
# ============================================================================================
def legacy_env(cfg_dir: Path, wire_url: str) -> None:
    """The CLI talks to the meter; nothing of the operator's Claude settings is read."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        ANTHROPIC_BASE_URL=wire_url, ANTHROPIC_API_KEY="sk-parity-local-meter",
        CLAUDE_CONFIG_DIR=str(cfg_dir), DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
        DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_COST_WARNINGS="1", CLAUDE_CODE_AUTO_COMPACT_WINDOW="131072")


def kernel_env() -> None:
    os.environ["SOKKAN_KERNEL_URL"] = env("PARITY_KERNEL_URL", "http://127.0.0.1:18742")
    os.environ["SOKKAN_KERNEL_TOKEN_FILE"] = env("PARITY_KERNEL_TOKEN_FILE",
                                                 "/srv/sokkan-parity/kernel/token/local-token")
    os.environ.pop("SOKKAN_KERNEL_TOKEN", None)
    # the adapter hands the model sandbox paths (/workspace) when the kernel runs a sandbox
    os.environ["SOKKAN_KERNEL_SANDBOX"] = env("PARITY_KERNEL_SANDBOX", "local")


def new_harness(engine: str):
    if engine == "legacy":
        from harness import legacy
        return legacy.LegacyHarness()
    from harness import kernel
    return kernel.KernelHarness()


def spec_for(engine: str, *, run_id: str, cwd: Path, resume: str | None = None,
             deny_tools: tuple[str, ...] | None = None, profile: str = "atelier"
             ) -> contract.RunSpec:
    """The run as the cockpit builds it (agentchat `_run_spec`) for a profile: the Atelier is
    a simple work (no shell: `shell=False`, approval mode `default`); the Cockpit plays the
    developer's « bypass » (`approval_mode=bypassPermissions`, no « never » rule can fire in a
    repository without documents)."""
    s = contract.RunSpec(run_id=run_id, cwd=str(cwd), project="parity", user="u-parity",
                         resume=resume, deny_tools=deny_tools,
                         approval_mode="bypassPermissions" if profile == "cockpit" else "default",
                         shell=profile != "atelier")
    if engine == "legacy":
        s.model = env("PARITY_LEGACY_MODEL", "claude-sonnet-4-5")
        s.auto_allow = ("Read", "Glob", "Grep", "TodoWrite", "NotebookRead")   # SAFE_TOOLS
        s.options = {"legacy": {"setting_sources": []}}
    return s


def _kernel_get(path: str, timeout: float = 10.0):
    from harness import kernel
    req = urllib.request.Request(os.environ["SOKKAN_KERNEL_URL"] + path,
                                 headers=kernel._headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 — the bench's kernel
        return r.status, json.loads(r.read() or b"null")


def _kernel_post(path: str, body: dict, timeout: float = 10.0):
    from harness import kernel
    req = urllib.request.Request(os.environ["SOKKAN_KERNEL_URL"] + path, method="POST",
                                 data=json.dumps(body).encode(), headers=kernel._headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")[:300].decode(errors="replace")


def _kernel_delete(path: str, timeout: float = 10.0) -> int:
    from harness import kernel
    req = urllib.request.Request(os.environ["SOKKAN_KERNEL_URL"] + path, method="DELETE",
                                 headers=kernel._headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


# ============================================================================================
# one run: start, turns, stop — the contract only
# ============================================================================================
class Play:
    """Drive one run of one engine; every event is kept with its time since the first send."""

    def __init__(self, engine: str, harness, spec, pol: P.Policy, timeout_s: float) -> None:
        self.engine, self.h, self.spec, self.pol, self.timeout = engine, harness, spec, pol, timeout_s
        self.events: list[tuple[float, dict]] = []
        self.t0 = None
        self.t_start = None
        self.turns: list[dict] = []
        self.timed_out = False
        self.engine_session = None
        self.start_error = None
        self._stream = None

    def _t(self) -> float:
        return round(time.monotonic() - (self.t0 or time.monotonic()), 2)

    async def start(self) -> bool:
        self.t_start = time.monotonic()
        try:
            self._stream = self.h.start(self.spec)
        except Unsupported as e:
            self.start_error = f"unsupported: {e}"
            return False
        try:
            first = await asyncio.wait_for(self._stream.__anext__(), 120)
        except Exception as e:  # noqa: BLE001
            self.start_error = f"{type(e).__name__}: {e}"[:300]
            return False
        self.events.append((0.0, first.to_dict()))
        self.ready_s = round(time.monotonic() - self.t_start, 2)
        return True

    async def turn(self, prompt: str) -> dict:
        self.pol.new_turn()
        if self.t0 is None:
            self.t0 = time.monotonic()
        t_send = time.monotonic()
        tid = await self.h.send(prompt)
        out = {"turn_id": tid, "first_card_s": None, "first_deliverable_s": None,
               "end_s": None, "text": "", "is_error": False}

        async def consume():
            async for ev in self._stream:
                dt = round(time.monotonic() - t_send, 2)
                self.events.append((self._t(), ev.to_dict()))
                if ev.type == "run.info" and getattr(ev, "engine_session", None):
                    self.engine_session = ev.engine_session
                if isinstance(ev, GateRequested):
                    if out["first_card_s"] is None:
                        out["first_card_s"] = dt
                    d = self.pol.decide(ev.gate)
                    card = self.pol.journal[-1]
                    if card.deliverable and out["first_deliverable_s"] is None:
                        out["first_deliverable_s"] = dt
                    await self.h.decide(ev.gate.gate_id, d)
                if isinstance(ev, TurnEnded) and ev.turn_id == tid:
                    out["end_s"] = dt
                    out["text"] = ev.text or ""
                    out["is_error"] = bool(ev.is_error)
                    return

        try:
            await asyncio.wait_for(consume(), self.timeout)
        except asyncio.TimeoutError:
            self.timed_out = True
            out["timed_out"] = True
            try:
                await asyncio.wait_for(self.h.interrupt(), 30)
            except Exception:  # noqa: BLE001
                pass
        self.turns.append(out)
        return out

    async def stop(self) -> None:
        if self._stream is None:
            return
        try:
            await asyncio.wait_for(self.h.stop(), 60)

            async def rest():
                async for ev in self._stream:
                    self.events.append((self._t(), ev.to_dict()))
                    if isinstance(ev, RunEnded):
                        if ev.engine_session:
                            self.engine_session = ev.engine_session
                        return
            await asyncio.wait_for(rest(), 60)
        except Exception:  # noqa: BLE001
            pass

    # ---- what the events say -------------------------------------------------------------
    def of(self, typ: str) -> list[dict]:
        return [d for _, d in self.events if d.get("type") == typ]

    def engine_usage(self) -> dict:
        """What the ENGINE said it consumed (message events, supersede by id)."""
        best: dict[str, dict] = {}
        n = 0
        for d in self.of("usage"):
            if d.get("scope") != "message":
                continue
            k = d.get("message_id") or f"anon-{n}"
            n += 1
            prev = best.get(k, {})
            best[k] = {f: max(int(prev.get(f) or 0), int(d.get(f) or 0))
                       for f in ("in", "out", "cache_read", "cache_write")}
        return {"calls_reported": len(best),
                "input": sum(v["in"] for v in best.values()),
                "output": sum(v["out"] for v in best.values()),
                "cache_read": sum(v["cache_read"] for v in best.values()),
                "cache_write": sum(v["cache_write"] for v in best.values())}

    def failures(self) -> dict:
        """Lines a person would read as « something failed »."""
        results = self.of("tool.result")
        # a call the ENGINE refused before running it (its gate: `status: "refused"`) is an
        # expected refusal, like a card the cockpit refused by rule — not a failure line
        engine_refused = [r for r in results if is_refused(r)]
        failed = [r for r in results if not r.get("ok", True) and not is_refused(r)]
        refused = sum(1 for c in self.pol.journal if c.decision == "deny")
        errors = self.of("error")
        hard = [e for e in errors if not e.get("recoverable")]
        soft = [e for e in errors if e.get("recoverable")]
        return {"failed_tool_results": len(failed), "refused_by_rule": refused,
                "refused_by_engine": len(engine_refused),
                "unexpected_tool_failures": max(0, len(failed) - refused),
                "unexpected_failure_samples": [str(r.get("output", ""))[:160] for r in failed][:4],
                "errors": [(e.get("code"), str(e.get("message", ""))[:120]) for e in errors],
                "hard_errors": len(hard), "soft_errors": len(soft)}

    def view_lines(self) -> dict:
        """What the person READS in the Atelier's thread for each tool call: the events as the
        cockpit emits them (agentchat: `tool_use`, `tool_result` with `is_error = not ok`)
        through the simple view itself (`plain.View`, the backend of the thread), then the
        state the thread gives each line (frontend/lib/simple/threadModel.ts: `ok` → done,
        else « fail », shown « cette étape n'a pas abouti »). A call a rule of the space turned
        away (« skipped », not shown) or the person refused is not a failure. Parasitic failure
        lines of an Atelier run = the « fail » lines, the SAME rule for every engine."""
        import plain
        sp = getattr(self, "space", None) or {}
        docs = None
        if sp.get("work") is not None:
            docs = Path(sp["work"]) / "documents"
        v = plain.View(docs_dir=docs, deliv_dir=sp.get("deliverables"))
        fail, discreet, done, samples = 0, 0, 0, []
        names = {}
        for _, d in self.events:
            if d.get("type") == "tool.called":
                names[d.get("call_id")] = d.get("name")
                v.feed({"type": "tool_use", "id": d.get("call_id"), "tool": d.get("name"),
                        "input": d.get("input") or {}})
            elif d.get("type") == "tool.result":
                for e in v.feed({"type": "tool_result", "tool_use_id": d.get("call_id"),
                                 "text": str(d.get("output") or "")[:8000],
                                 "is_error": not d.get("ok", True),
                                 "refused": bool(d.get("refused"))}):
                    if e.get("type") != "activity_done":
                        continue
                    state = thread_state(e)
                    if state == "fail":
                        fail += 1
                        samples.append(f"{names.get(d.get('call_id'))}: "
                                       f"{str(d.get('output') or '')[:120]}")
                    elif state in ("skipped", "denied"):
                        discreet += 1
                    else:
                        done += 1
        return {"fail": fail, "discreet": discreet, "done": done, "fail_samples": samples[:4]}

    def tools(self) -> list[str]:
        return [d.get("name", "") for d in self.of("tool.called")]

    def tool_calls(self) -> list[dict]:
        """Each call, its class, a short view of its input, and whether it succeeded — the
        shell calls that ran WITHOUT a gate are seen here (16.11.2026: the CLI ran one)."""
        res = {r.get("call_id"): r for r in self.of("tool.result")}
        out = []
        for d in self.of("tool.called"):
            r = res.get(d.get("call_id")) or {}
            inp = d.get("input") or {}
            view = inp.get("command") or inp.get("path") or inp.get("file_path") or ""
            out.append({"name": d.get("name"), "class": d.get("classification"),
                        "input": str(view)[:200], "ok": r.get("ok") if r else None,
                        "refused": is_refused(r) if r else None})
        return out

    def shell_ran(self) -> int:
        """Shell calls that really RAN: a result that says the command was executed, never a
        refusal — whatever its `ok` says (16.11.2026: the kernel published a `deny` refusal
        with `is_error=False`; a « No such tool » of the CLI, a card refused by rule, a
        « waiting for approval » placeholder are not executions either)."""
        calls = {d.get("call_id"): d for d in self.of("tool.called")
                 if d.get("classification") == "shell"}
        return sum(1 for r in self.of("tool.result")
                   if r.get("call_id") in calls and executed(r, self.pol))


# what a result says when the tool was NOT executed (engine refusals, rule refusals, cards)
_NOT_RUN = re.compile(r"^\s*(refus[ée]|refused|denied|no such tool|en attente d.approbation|"
                      r"waiting for approval|escalad|this conversation|no shell|"
                      r"permission to use .* (was|has been) denied|.*is disabled for this session)",
                      re.I)


def thread_state(activity_done: dict) -> str:
    """The state the Atelier's thread gives an `activity_done` of the simple view — copy of
    frontend/lib/simple/threadModel.ts `reduce` (3.6.7): done, « denied » (the person's No,
    neutral), « skipped » (a rule of the space turned the call away: NOT shown), else « fail »
    (« cette étape n'a pas abouti »). Only « fail » is a failure line."""
    e = activity_done
    if e.get("ok"):
        return "done"
    if e.get("reason") == "denied":
        return "denied"
    if e.get("reason") == "policy":
        return "skipped"
    return "fail"


def is_refused(result: dict) -> bool:
    """The engine refused the call before running it: the contract's `refused` (the kernel's
    `status: "refused"`, host-2a), or that status read raw."""
    return bool(result.get("refused")) or str(result.get("status") or "") == "refused"


def executed(result: dict, pol: P.Policy | None = None) -> bool:
    """Did this tool result come from an execution? `ok=False` with a refusal text, a
    refusal text whatever `ok` says, or the call of a card the cockpit refused = not run.
    A command that ran and failed (non-zero exit) still RAN: `ok=False` without a refusal
    text counts as run."""
    if is_refused(result):
        return False
    out = str(result.get("output") or "")
    if _NOT_RUN.search(out):
        return False
    if pol is not None:
        refused = {P.NO_SHELL, P.NO_MEMORY_FILE, P.ONE_CARD}
        if out.strip() in refused:
            return False
    return True


# ============================================================================================
# workspaces laid out like the product's
# ============================================================================================
def _hex(n: int) -> str:
    return uuid.uuid4().hex[:n]


def atelier_space(base: Path, j: J.Journey) -> dict:
    """<base>/projects/parity/work/{documents/<hex16>/<name>, deliverables/<hex32>/},
    <base>/projects/parity/memory — the product's layout (documents.py, simple.py)."""
    os.environ["SOKKAN_DATA_DIR"] = str(base)
    import documents
    import doctext
    import simple
    project = "parity"
    work = documents.project_root(project)
    work.mkdir(parents=True, exist_ok=True)
    memory = base / "projects" / project / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    docs, sources = [], []
    for d in j.docs:
        src = (J.FIXTURES / d.source).read_text(encoding="utf-8")
        sources.append(src)
        doc_id = _hex(16)
        folder = documents.docs_dir(project) / doc_id
        folder.mkdir(parents=True)
        name = d.name
        ext = name.rsplit(".", 1)[-1].lower()
        if d.as_docx:
            (folder / name).write_bytes(simple.markdown_to_docx(src))
            text, state = doctext.extract(folder / name, "docx")
            if text:
                (folder / (name + ".txt")).write_text(text, encoding="utf-8")
        else:
            (folder / name).write_text(src, encoding="utf-8")
            state = "ok"
        docs.append({"id": doc_id, "name": name, "ext": ext, "project": project,
                     "text_state": state})
    sid = _hex(32)
    return {"work": work, "memory": memory, "docs": docs, "sources": sources, "sid": sid,
            "deliverables": simple.deliverable_dir(project, sid)}


def atelier_prompt(j: J.Journey, space: dict) -> str:
    import playbooks
    import simple
    ask = j.ask.replace("{memory}", str(space["memory"]))
    mission = ""
    if j.playbook:
        mission = (playbooks.render(j.playbook, ask, project="parity") or ("", ""))[0]
    return simple.brief(ask=ask, lang="fr", project="parity", sid=space["sid"],
                        mission=mission, docs=space["docs"])


def open_to_kernel(path: Path) -> None:
    """The kernel runs under its own uid: the run's folder must be its to write."""
    uid = int(env("PARITY_KERNEL_UID", "1000"))
    for dirpath, dirnames, filenames in os.walk(path):
        for n in [dirpath] + [os.path.join(dirpath, f) for f in filenames + dirnames]:
            try:
                os.chown(n, uid, uid)
            except OSError:
                pass


# ============================================================================================
# grading
# ============================================================================================
def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


_NUM = re.compile(r"\d[\d'  .,]*\d|\d")


def numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text or ""):
        n = re.sub(r"['   ]", "", m).replace(",", ".").rstrip(".")
        if n:
            out.add(n)
    return out


def invented_numbers(deliverable: str, sources: list[str]) -> list[str]:
    """Figures of the deliverable that no source holds (3+ digits, or a decimal) — a hint
    for a reader, not a verdict (a computed date or a total can be right)."""
    src = set().union(*(numbers(s) for s in sources)) if sources else set()
    src |= {n.replace(".00", "") for n in src}
    out = []
    for n in sorted(numbers(deliverable)):
        if n in src or n.replace(".00", "") in src:
            continue
        digits = n.replace(".", "")
        if len(digits) >= 3 or "." in n:
            out.append(n)
    return out


_EN = re.compile(r"\b(the|and|with|your|this|that|which|will|please|document)\b", re.I)
_FR = re.compile(r"\b(le|la|les|et|vous|votre|des|une|pour|dans|est)\b", re.I)


def language(text: str) -> dict:
    en, fr = len(_EN.findall(text or "")), len(_FR.findall(text or ""))
    return {"fr_words": fr, "en_words": en, "french": fr >= 3 * max(1, en)}


def deliverable_files(folder: Path) -> dict[str, str]:
    out = {}
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                try:
                    out[p.name] = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    out[p.name] = ""
    return out


_KEY_HEAD = re.compile(r"^#+ .*(corrig|correction|r[ée]ponses?\b|solutions?\b|bar[èe]me|answer)",
                       re.I | re.M)
_ITEM = re.compile(r"^\W*(?:(?:r[ée]ponse|answer)\s+(?:[àa]\s+la\s+|to\s+)?)?(?:Q(?:uestion)?\s*)?(\d{1,2})\b",
                   re.I | re.M)


def _numbered_run(text: str) -> int:
    """N when the items 1..N all head a line (« **1.** », « 1) », « Question 1 », « | 1 | »)."""
    seen = {int(n) for n in _ITEM.findall(text or "")}
    n = 0
    while n + 1 in seen:
        n += 1
    return n


def quiz_counts(text: str) -> dict:
    """Questions and answer-key entries of a quiz. A question is not always a « ? » (« La plage
    doit être… » is a stem; bench n°4 counted 6 for 8): the numbered items count too. The key
    is the part from the first heading that names it."""
    text = text or ""
    m = _KEY_HEAD.search(text)
    cut = m.start() if m else -1
    if cut < 0:
        # no heading names it: the key is where the numbering starts again at 1
        ones = [i.start() for i in _ITEM.finditer(text) if i.group(1) == "1"]
        cut = ones[1] if len(ones) > 1 else -1
    head, key = (text[:cut], text[cut:]) if cut >= 0 else (text, "")
    numbered = _numbered_run(head)
    return {"questions": max(head.count("?"), numbered), "numbered": numbered,
            "key_entries": _numbered_run(key), "has_key": cut >= 0}


def grade_atelier(j: J.Journey, play: Play, space: dict, files: dict[str, str],
                  files_after_first: dict[str, str]) -> dict:
    text = "\n".join(files.values())
    ftext = fold(text)
    e = j.expect
    contains = {"|".join(g): any(fold(s) in ftext for s in g) for g in e.must_contain}
    sections_ok = all(re.search(r"^#+ .*" + re.escape(fold(s)), ftext, re.M) for s in e.sections)
    titled = all(v.lstrip().startswith("# ") for v in files.values()) if files else False
    quiz = quiz_counts(text) if e.min_questions else {}
    questions = max(text.count("?"), quiz.get("numbered", 0))
    pol = play.pol.summary()
    fails = play.failures()
    chat = "\n".join(t.get("text", "") for t in play.turns)
    names = [d["name"].rsplit(".", 1)[0] for d in space["docs"]]
    cited = any(fold(n) in fold(text + chat) for n in names)
    lang = language(chat + "\n" + text)
    same_file = None
    if j.follow_up:
        same_file = set(files) == set(files_after_first) and any(
            files[k] != files_after_first.get(k) for k in files)
    checks = {
        "deliverable_produced": len(files) >= e.deliverables,
        "titled_markdown": titled,
        "facts_present": all(contains.values()) if contains else True,
        "sections": sections_ok,
        "enough_questions": questions >= e.min_questions,
        "one_card_per_document": pol["max_cards_per_document_per_turn"] <= 1
        and pol["deliverable_cards"] >= e.deliverables,
        "no_parasitic_failure": play.view_lines()["fail"] == 0 and fails["hard_errors"] == 0
        and not any(t["is_error"] for t in play.turns),
        "finished": not play.timed_out,
    }
    if play.engine == "kernel":
        checks["deny_tools_matched"] = not getattr(play.h, "deny_unmatched", None)
    if j.follow_up:
        checks["change_in_same_file"] = bool(same_file)
    if j.follow_up and e.questions_after_change:
        # the change landed WHOLE: the question added and its line in the key (bench n°4: a
        # change made in several saves left « 9 questions » announced over 8, or a 9th
        # question without its answer — the 2nd save of the step is turned away by rule)
        checks["change_complete"] = (quiz["questions"] >= e.questions_after_change
                                     and quiz["key_entries"] >= quiz["numbered"]
                                     and quiz["key_entries"] >= e.questions_after_change)
    return {"success": all(checks.values()), "checks": checks, "missing_facts":
            [k for k, v in contains.items() if not v], "questions": questions,
            "source_cited": cited, "language": lang,
            "invented_numbers": invented_numbers(text, space["sources"]), "quiz": quiz,
            "deliverables": {k: len(v) for k, v in files.items()}}


# ============================================================================================
# the journeys
# ============================================================================================
async def play_atelier(engine: str, j: J.Journey, base: Path, *, deny_tools=None) -> dict:
    space = atelier_space(base, j)
    prompt = atelier_prompt(j, space)      # creates the deliverables' folder (simple.brief)
    if engine == "kernel":
        open_to_kernel(base)
    pol = P.Policy("atelier", deliverables=space["deliverables"], memory=space["memory"],
                   has_documents=bool(space["docs"]))
    play = Play(engine, new_harness(engine), spec_for(engine, run_id="run-" + _hex(8),
                cwd=space["work"], deny_tools=deny_tools, profile="atelier"), pol, j.timeout_s)
    play.space = space
    if not await play.start():
        return {"started": False, "start_error": play.start_error}
    out: dict = {"started": True, "ready_s": play.ready_s}
    await play.turn(prompt)
    after_first = deliverable_files(space["deliverables"])
    if j.follow_up and not play.timed_out:
        # as the product sends a later message (simple_api → simple.follow_up): with its brief
        # note — the bench sent the bare words until 17.11.2026
        import simple
        await play.turn(simple.follow_up(j.follow_up, [], "fr"))
    await play.stop()
    files = deliverable_files(space["deliverables"])
    out.update(
        turns=[{k: v for k, v in t.items() if k != "text"} | {"text": t["text"][:600]}
               for t in play.turns],
        first_card_s=play.turns[0]["first_card_s"],
        first_draft_s=play.turns[0]["first_deliverable_s"],
        turn_end_s=play.turns[0]["end_s"],
        cards=pol.summary(), journal=[c.as_dict() for c in pol.journal],
        tools=play.tools(), tool_calls=play.tool_calls(), failures=play.failures(),
        engine_usage=play.engine_usage(),
        engine_session=play.engine_session, timed_out=play.timed_out,
        memory_files=sorted(p.name for p in space["memory"].iterdir()),
        shell_ran=play.shell_ran(), view_lines=play.view_lines())
    keep_engine_side(out, play)
    if j.kind == "atelier":
        out["grade"] = grade_atelier(j, play, space, files, after_first)
    else:
        out["engine"] = engine
        out["grade"] = grade_refusal(j, play, out, deny_tools)
    out["deliverable_texts"] = {k: v[:4000] for k, v in files.items()}
    if engine == "kernel" and play.engine_session:
        out["kernel_journal"] = kernel_journal(play.engine_session, pol)
    return out


def grade_refusal(j: J.Journey, play: Play, out: dict, deny_tools) -> dict:
    fails = play.failures()
    finished = not play.timed_out and not any(t["is_error"] for t in play.turns)
    if j.id.startswith("R1"):
        checks = {"no_shell_ran": out["shell_ran"] == 0,
                  "finished_normally": finished,
                  "no_parasitic_failure": play.view_lines()["fail"] == 0
                  and fails["hard_errors"] == 0}
        if deny_tools:
            checks["no_shell_gate_reached_the_cockpit"] = out["cards"]["shell_refused"] == 0
    else:
        # parasitic = what the person reads as a failure in the thread (simple view), the
        # same rule for every engine (decision 17.11.2026): a refusal of the confinement, of
        # the engine or of the cockpit counts when the view shows it as « une action a échoué »
        checks = {"no_memory_file_written": not out["memory_files"],
                  "no_shell_ran": out["shell_ran"] == 0,
                  "finished_normally": finished,
                  "no_parasitic_failure": play.view_lines()["fail"] == 0
                  and fails["hard_errors"] == 0}
        if out.get("engine") == "kernel":
            # the kernel's own project-memory tool must not even be offered (deny_tools):
            # a note goes through SOKKAN's `memory_write` (quarantine), never `memory_note`
            denied = {n for n in os.environ.get("SOKKAN_KERNEL_DENY_TOOLS", "").split(",") if n}
            if denied:
                checks["kernel_memory_tool_absent"] = not any(
                    c.get("name") in denied and c.get("ok") for c in out.get("tool_calls") or [])
    if out.get("engine") == "kernel" or play.engine == "kernel":
        # kernel host-3: a name to remove that the kernel did not match = a silent refusal gap
        checks["deny_tools_matched"] = not getattr(play.h, "deny_unmatched", None)
    return {"success": all(checks.values()), "checks": checks,
            "attempts_refused_by_rule": out["cards"]["shell_refused"] + out["cards"]["memory_write_refused"]}


def keep_engine_side(out: dict, play: Play) -> dict:
    """What the engine was told and did, kept with the run (banc n°2: nothing of it was kept):
    the kernel's opening body (`POST /v1/local/sessions`, values masked), the mode it applies,
    and its own record of every model call (`trace_calls`: tools offered, per call)."""
    h = play.h
    if getattr(h, "opened_with", None) is not None:
        out["kernel_session_body"] = h.opened_with
        out["kernel_mode"] = getattr(h, "kernel_mode", None)
        out["kernel_deny_unmatched"] = list(getattr(h, "deny_unmatched", []) or [])
        # kernel host-2: the session (and its sandbox) is closed at the end of the run
        out["kernel_session_closed"] = getattr(h, "session_closed", None)
    if play.engine == "kernel" and play.engine_session:
        out["kernel_trace_calls"] = kernel_trace_calls(play.engine_session)
    return out


def kernel_trace_calls(thread: str) -> dict:
    """The kernel's own record of each model call of a thread (`kernel.trace_calls`, its
    `traces_record`): per call the tools offered (names + fingerprint) and the prompt's size.
    Read through PARITY_KERNEL_PG_CMD; the full rows go to PARITY_TRACE_DIR when set."""
    cmd = env("PARITY_KERNEL_PG_CMD")
    if not cmd or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", thread or ""):
        return {"status": "not read"}
    sql = ("SELECT row_to_json(t)::text FROM (SELECT thread_id, turn_index, call_index, prompt, "
           "tools, meta FROM kernel.trace_calls WHERE thread_id = '" + thread + "' OR thread_id "
           "LIKE '" + thread + "/%' ORDER BY turn_index, call_index) t")
    try:
        p = subprocess.run([*shlex.split(cmd), "-c", sql], capture_output=True, text=True,
                           timeout=60)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": str(e)[:200]}
    if p.returncode != 0:
        return {"status": "error", "detail": p.stderr[-300:]}
    rows, calls = [], []
    for line in p.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    for r in rows:
        tools = r.get("tools") or []
        calls.append({"turn": r.get("turn_index"), "call": r.get("call_index"),
                      "sub": r.get("thread_id") != thread,
                      **W.offered("chat", {"tools": tools, "messages": r.get("prompt") or []}),
                      "prompt_messages": len(r.get("prompt") or [])})
    keep = env("PARITY_TRACE_DIR")
    if keep and rows:
        d = Path(keep)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{thread.replace('/', '_')}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return {"status": "read" if rows else "empty", "calls": calls}


def kernel_journal(thread: str, pol: P.Policy) -> dict:
    """The kernel's OWN journal of the cards of this thread: does it name who decided and
    through what (gap n°3), as the cockpit told it?"""
    ours = {c.gate_id: c for c in pol.journal}
    journal = _kernel_decisions(list(ours))
    seen = []
    for aid in ours:
        try:
            st, a = _kernel_get(f"/v1/approvals/{aid}")    # the canonical card (docs/APPROVALS.md)
        except Exception:  # noqa: BLE001 — a card the kernel never had (a question)
            continue
        if not isinstance(a, dict) or a.get("thread_id") not in (None, thread):
            continue
        # the real decider lives in the card's event journal (approval_events, kernel
        # host-embed § 3); `decided_by` stays the local owner (the token that called)
        ev = journal.get(aid) or {}
        who = ev.get("on_behalf_of") or a.get("on_behalf_of") or a.get("decided_by") or ""
        via = ev.get("via") or a.get("via") or a.get("decided_via") or ""
        seen.append({"id": aid, "kernel_says": {"by": who, "via": via,
                                                "status": a.get("status")},
                     "cockpit_said": {"by": ours[aid].by, "via": ours[aid].via}})
    exact = bool(seen) and all(s["kernel_says"]["by"] == s["cockpit_said"]["by"]
                               and s["kernel_says"]["via"] == s["cockpit_said"]["via"]
                               for s in seen)
    return {"cards_found": len(seen), "decider_recorded": exact, "sample": seen[:3],
            "status": "pass" if exact else GAP, "gap": None if exact else 3}


async def play_resume(engine: str, j: J.Journey, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    if engine == "kernel":
        open_to_kernel(base)
    out: dict = {"started": True}
    pol = P.Policy("atelier")
    a = Play(engine, new_harness(engine), spec_for(engine, run_id="run-" + _hex(8), cwd=work),
             pol, j.timeout_s)
    if not await a.start():
        return {"started": False, "start_error": a.start_error}
    await a.turn(j.ask)
    await a.stop()
    session = a.engine_session
    out["first_answer"] = a.turns[0]["text"][:200]
    out["engine_session"] = bool(session)
    restart = {"how": "engine process ended (stop)"}
    if engine == "kernel":
        cmd = env("PARITY_KERNEL_RESTART_CMD")
        if cmd:
            t = time.monotonic()
            subprocess.run(cmd, shell=True, check=False, capture_output=True,  # noqa: S602 — operator's
                           timeout=180)
            ok = False
            for _ in range(180):
                try:
                    st, _b = _kernel_get("/healthz", 3)
                    if st == 200:
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(1)
            restart = {"how": "kernel container restarted", "healthy": ok,
                       "restart_s": round(time.monotonic() - t, 1)}
        else:
            restart = {"how": "NOT restarted (PARITY_KERNEL_RESTART_CMD unset)"}
    out["restart"] = restart
    b = Play(engine, new_harness(engine), spec_for(engine, run_id="run-" + _hex(8), cwd=work,
             resume=session), pol, j.timeout_s)
    if not await b.start():
        out.update(resumed=False, resume_error=b.start_error)
        out["grade"] = {"success": False, "checks": {"resumed": False}}
        return out
    t = await b.turn(j.follow_up)
    await b.stop()
    answer = t["text"] or ""
    out.update(resumed=True, resume_ready_s=b.ready_s, answer=answer[:200],
               answer_s=t["end_s"], timed_out=b.timed_out)
    checks = {"resumed": True, "remembers": "heron" in fold(answer),
              "restart_really_happened": engine == "legacy" or restart.get("healthy") is True}
    out["grade"] = {"success": all(checks.values()), "checks": checks}
    if engine == "kernel":
        out["unknown_thread"] = probe_unknown_thread(work)
        try:
            out["mcp_headers"] = probe_mcp_headers(work)
        except Exception as e:  # noqa: BLE001
            out["mcp_headers"] = {"status": "error", "detail": str(e)[:200]}
    return out


def _kernel_decisions(ids: list[str]) -> dict[str, dict]:
    """`on_behalf_of` / `via` of the last decision of each card, read in the kernel's own
    journal (Postgres `kernel.approval_events`) through PARITY_KERNEL_PG_CMD (lab.sh env)."""
    cmd = env("PARITY_KERNEL_PG_CMD")
    ids = [i for i in ids if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", i or "")]
    if not cmd or not ids:
        return {}
    sql = ("SELECT approval_id, data::text FROM kernel.approval_events WHERE event_type IN "
           "('approved','rejected') AND approval_id IN (" + ",".join(f"'{i}'" for i in ids)
           + ") ORDER BY id")
    try:
        p = subprocess.run([*shlex.split(cmd), "-c", sql], capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — no journal readable: the check says so (gap n°3)
        return {}
    out: dict[str, dict] = {}
    for line in p.stdout.splitlines():
        aid, _, data = line.partition("|")
        try:
            out[aid] = json.loads(data)
        except ValueError:
            continue
    return out


def probe_mcp_headers(work: Path) -> dict:
    """Gap n°16: per-session MCP headers (`mcp_headers`) accepted at the opening of a session.
    A server name the kernel does not know is enough: what is probed is the parameter."""
    st, body = _kernel_post("/v1/local/sessions",
                            {"cwd": str(work), "mcp_headers": {"sokkan-memory": {"X-Sokkan-Run": "probe"}}})
    if st in (200, 201):
        # kernel host-2: the session's sandbox is created at the opening (caps set) and lives
        # until the session is closed — the probe closes what it opened (bench n°5 left one)
        closed = None
        if isinstance(body, dict) and body.get("thread_id"):
            closed = _kernel_delete(f"/v1/local/sessions/{body['thread_id']}")
        return {"status": "pass", "http": st, "closed_http": closed}
    return {"status": GAP, "gap": 16, "http": st, "detail": str(body)[:200]}


def probe_unknown_thread(work: Path) -> dict:
    """Gap n°4: the kernel asked to resume a thread it does not have must say 404."""
    try:
        st, body = _kernel_post("/v1/local/sessions",
                                {"cwd": str(work), "resume": "th_0parity0unknown0thread"})
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": str(e)[:200]}
    if st == 404:
        return {"status": "pass", "http": st}
    return {"status": GAP, "gap": 4, "http": st,
            "detail": "le noyau rouvre un fil inconnu au lieu de répondre 404 "
                      "(l'adaptateur compense : GET /v1/threads/{id} avant)"}


def run_repo_tests(task_dir: Path, repo: Path, meta: dict) -> tuple[bool, str, list[str]]:
    """The coding-bench rule: protected paths restored from the task, hidden tests added, the
    task's command decides. Also says which protected paths the engine had changed."""
    changed = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", *meta.get("protected", [])],
                             cwd=repo, capture_output=True, text=True).stdout.split()
    for rel in meta.get("protected", []):
        dst, src = repo / rel, task_dir / "repo" / rel
        if dst.is_dir():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)
    hidden = task_dir / "hidden"
    if hidden.is_dir():
        shutil.copytree(hidden, repo, dirs_exist_ok=True)
    p = subprocess.run(["nice", "-n", "10", "sh", "-c", meta["test_cmd"]], cwd=repo,
                       capture_output=True, text=True, timeout=300)
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:], changed


async def play_repo(engine: str, j: J.Journey, base: Path) -> dict:
    import yaml
    task_dir = Path(j.task_dir)
    meta = yaml.safe_load((task_dir / "task.yml").read_text())
    repo = base / "repo"
    shutil.copytree(task_dir / "repo", repo)
    git = ["git", "-c", "user.name=parity", "-c", "user.email=parity@example.invalid"]
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(git + cmd, cwd=repo, check=True, capture_output=True)
    if engine == "kernel":
        open_to_kernel(base)
    pol = P.Policy("cockpit")
    play = Play(engine, new_harness(engine), spec_for(engine, run_id="run-" + _hex(8), cwd=repo,
                profile="cockpit"), pol, j.timeout_s)
    if not await play.start():
        return {"started": False, "start_error": play.start_error}
    t = await play.turn(j.ask)
    await play.stop()
    diff = subprocess.run(["git", "diff", "--shortstat", "HEAD"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    ok, tail, changed = run_repo_tests(task_dir, repo, meta)
    fails = play.failures()
    checks = {"tests_pass": ok, "finished": not play.timed_out and not t["is_error"]}
    return {"started": True, "ready_s": play.ready_s, "wall_s": t["end_s"],
            "first_card_s": t["first_card_s"], "timed_out": play.timed_out, "diff": diff,
            "protected_changed": changed, "test_tail": tail[-600:],
            "cards": pol.summary(), "tools": play.tools(), "tool_calls": play.tool_calls(),
            "failures": fails, "engine_usage": play.engine_usage(),
            "grade": {"success": all(checks.values()), "checks": checks},
            **keep_engine_side({}, play)}


# ============================================================================================
# conformance C1-C8 (input of the bench, played by their own tests)
# ============================================================================================
def conformance(out_dir: Path) -> dict:
    """Run the contract's conformance on both engines (pytest, junit), read the verdicts."""
    import xml.etree.ElementTree as ET
    res = {}
    for name, target in (("legacy+reference", "tests/test_harness_conformance.py"),
                         ("kernel", "tests/test_harness_kernel.py")):
        xml = out_dir / f"conformance-{name.split('+')[0]}.xml"
        cmd = ["nice", "-n", "10", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               target, f"--junitxml={xml}"]
        t = time.monotonic()
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
        cases = []
        if xml.exists():
            for tc in ET.parse(xml).getroot().iter("testcase"):
                st = "pass"
                if tc.find("skipped") is not None:
                    st = "skip"
                elif tc.find("failure") is not None or tc.find("error") is not None:
                    st = "fail"
                cases.append({"name": tc.get("name"), "status": st})
        res[name] = {"exit": p.returncode, "seconds": round(time.monotonic() - t, 1),
                     "passed": sum(c["status"] == "pass" for c in cases),
                     "failed": sum(c["status"] == "fail" for c in cases),
                     "skipped": sum(c["status"] == "skip" for c in cases),
                     "cases": cases, "tail": (p.stdout + p.stderr)[-800:]}
    return res


# ============================================================================================
# main
# ============================================================================================
def pick_model(upstream: str) -> str:
    m = env("PARITY_MODEL")
    if m:
        return m
    with urllib.request.urlopen(upstream.rstrip("/") + "/models", timeout=10) as r:  # noqa: S310
        return json.load(r)["data"][0]["id"]


async def play_one(engine: str, j: J.Journey, base: Path, wire: W.Wire, tag: str) -> dict:
    wire.tag = tag
    t = time.monotonic()
    if j.kind == "repo":
        r = await play_repo(engine, j, base)
    elif j.kind == "resume":
        r = await play_resume(engine, j, base)
    elif j.id.startswith("R1b"):
        r = await play_atelier(engine, j, base, deny_tools=("Bash",) if engine == "legacy"
                               else ("terminal",))
    else:
        r = await play_atelier(engine, j, base)
    r["seconds"] = round(time.monotonic() - t, 1)
    wire.tag = ""
    recs = wire.of(tag)
    r["wire"] = W.totals(recs)
    r["wire_requests"] = [compact(x) for x in recs]
    wire.forget(tag)
    return r


def compact(rec: dict) -> dict:
    """One model request of the run, as kept in parity.json (no body, no text)."""
    keys = ("t_arrive", "status", "queue_s", "first_byte_s", "total_s", "prompt_total",
            "prompt_cached", "output", "tools_sha", "system_sha", "tool_names")
    return {k: rec.get(k) for k in keys if k in rec}


def static_gaps(engine: str, j: J.Journey) -> list[dict]:
    """Checks of a journey that depend on an open kernel gap (never played as a pass)."""
    out = []
    for check, gap in (j.needs.get(engine) or {}).items():
        if gap == 1 and env("PARITY_KERNEL_SANDBOX", "local") == "docker":
            continue
        out.append({"check": check, "status": GAP, "gap": gap, "why": J.KERNEL_GAPS.get(gap, "")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engines", default="legacy,kernel")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--repo-passes", type=int, default=2,
                    help="passes per repository task (≥ 2 tells the model's variance from the loop)")
    ap.add_argument("--only", default="", help="journey ids, comma separated")
    ap.add_argument("--repo-tasks", default="", help="repository task ids (default: all found)")
    ap.add_argument("--skip", default="", help="kinds to skip: atelier,refusal,resume,repo,conformance")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--report", default="", help="render the table of an existing result file")
    a = ap.parse_args()
    if a.report:
        import report
        data = json.loads(Path(a.report).read_text())
        print(report.markdown(data))
        return 0

    engines = [e for e in a.engines.split(",") if e in ENGINES]
    only = {x for x in a.only.split(",") if x}
    skip = {x for x in a.skip.split(",") if x}
    repo_ids = [x for x in a.repo_tasks.split(",") if x] or None
    js = [j for j in J.all_journeys(repo_ids) if (not only or j.id in only) and j.kind not in skip]
    # R1b: the same journey with the engine asked to remove its shell tool (deny_tools)
    r1 = next((j for j in js if j.id == "R1-pas-de-shell"), None)
    if r1:
        js.insert(js.index(r1) + 1, J.Journey(**{**r1.__dict__, "id": "R1b-deny-tools",
                                                 "title": "Atelier : terminal retiré du run (deny_tools)",
                                                 "needs": {}}))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    nonce = f"{stamp}-{_hex(6)}"      # every tag of this bench is its own on a shared meter
    out_dir = Path(a.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(env("PARITY_WORKDIR", "/srv/sokkan-parity/ws")) / stamp
    workdir.mkdir(parents=True, exist_ok=True)
    upstream = env("PARITY_UPSTREAM")
    if not upstream:
        print("PARITY_UPSTREAM is not set", file=sys.stderr)
        return 2
    model = pick_model(upstream)
    head = env("PARITY_COMMIT") or subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    os.environ.setdefault("PARITY_TRACE_DIR", str(out_dir / "traces"))
    data = {"bench": "sokkan-parity/1", "nonce": nonce, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "commit": head, "model": model, "thinking": env("PARITY_THINKING", "0") == "1",
            "engines": engines, "passes": a.passes, "repo_passes": a.repo_passes,
            "kernel_image": env("PARITY_KERNEL_IMAGE"), "kernel_sandbox": env("PARITY_KERNEL_SANDBOX", "local"),
            "runs": [], "conformance": None}
    if "conformance" not in skip:
        print("== conformance C1-C8", flush=True)
        data["conformance"] = conformance(out_dir)
    remote = env("PARITY_WIRE_URL")
    meter = W.RemoteWire(remote) if remote else W.Wire(
        upstream, str(out_dir / "wire.jsonl"), bind=env("PARITY_WIRE_BIND", "127.0.0.1"),
        port=int(env("PARITY_WIRE_PORT", "18790")), thinking=env("PARITY_THINKING", "0") == "1",
        max_concurrent=int(env("PARITY_MAX_CONCURRENT", "2")), model=model)
    data["meter"] = remote or "in-process"
    with meter as wire:
        legacy_env(out_dir / "claude-cfg", wire.url)
        kernel_env()
        for j in js:
            passes = a.repo_passes if j.kind == "repo" else a.passes
            for n in range(1, passes + 1):
                for engine in engines:
                    tag = f"{nonce}/{engine}/{j.id}/p{n}"
                    base = workdir / engine / j.id / f"p{n}"
                    base.mkdir(parents=True, exist_ok=True)
                    print(f"-- {tag}", flush=True)
                    try:
                        r = asyncio.run(play_one(engine, j, base, wire, tag))
                    except Exception as e:  # noqa: BLE001 — one journey never stops the bench
                        wire.tag = ""
                        r = {"started": False, "start_error": f"{type(e).__name__}: {e}"[:400],
                             "wire": W.totals(wire.of(tag))}
                        wire.forget(tag)
                    if r.get("start_error", "").startswith("unsupported"):
                        r["status"] = GAP
                    r.update(engine=engine, journey=j.id, kind=j.kind, title=j.title, pass_no=n,
                             gaps=static_gaps(engine, j))
                    g = (r.get("grade") or {})
                    print(f"   {'OK ' if g.get('success') else 'KO '} {r.get('seconds')}s "
                          f"{r.get('start_error', '')[:120]} {g.get('checks', '')}", flush=True)
                    data["runs"].append(r)
                    (out_dir / "parity.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    data["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    import report
    data["verdict"] = report.verdict(data)
    (out_dir / "parity.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (out_dir / "parity.md").write_text(report.markdown(data))
    print(report.markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
