"""Conformance C1–C8 (backend/harness/conformance), played against every engine.

* `reference` — the in-memory engine of the conformance package (gates born on the
  engine's side, no question, no plan): always played.
* `legacy` — the adapter of the engine SOKKAN drives today, for real: the Claude Code CLI
  bundled with the SDK, in front of a scripted model (tests/mock_anthropic.py). No credit is
  spent; skipped when the SDK ships without its CLI. Opt out: SOKKAN_TEST_E2E_AGENTS=0.

A new adapter adds its rig here; the scenarios do not change.
"""
import asyncio
import os
import sys
import uuid
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract  # noqa: E402
from harness.conformance import driver, reference, scenarios  # noqa: E402
from mock_anthropic import MockAnthropic, all_text  # noqa: E402


def _bundled_cli() -> bool:
    try:
        import claude_agent_sdk
    except ImportError:
        return False
    d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
    return os.path.exists(os.path.join(d, "claude"))


# ---- reference engine ----------------------------------------------------------------------

class ReferenceRig(driver.Rig):
    name = "reference"

    def __init__(self, tmp: Path, **engine) -> None:
        self.workspace, self.outside = tmp / "work", tmp / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        self.model = reference.ReferenceModel()
        self.engine = engine

    def capabilities(self):
        return reference.ReferenceHarness(**self.engine).capabilities()

    def harness(self):
        return reference.ReferenceHarness(self.model, **self.engine)

    def spec(self, *, context=None, resume=None, confined=False):
        return contract.RunSpec(
            run_id="run-" + uuid.uuid4().hex[:8], cwd=str(self.workspace), project="p-7f3a",
            user="u-91c2", context=context, resume=resume,
            confinement=contract.Confinement(project="p-7f3a") if confined else None)

    def script(self, *turns):
        self.model.script.extend(turns)

    def model_saw(self, text):
        return any(text in r for r in self.model.requests)

    def last_request_saw(self, text):
        return bool(self.model.requests) and text in self.model.requests[-1]


# ---- the CLI adapter, real CLI, scripted model ---------------------------------------------

class _ScriptedModel(MockAnthropic):
    """Plays the steps of the conformance scenarios as tool calls of the CLI, turn by turn
    (a new turn = a request whose last message is a prompt, not a tool result)."""

    def __init__(self, workspace: Path):
        super().__init__()
        self.workspace = workspace
        self.turns: deque = deque()
        self.current: list = []

    def answer(self, req, mid):
        msgs = req.get("messages") or []
        if not req.get("tools") or not msgs:
            return {"type": "text", "text": "ok"}, "end_turn"
        # the last USER message (the CLI may append messages of its own after it)
        last = [m for m in msgs if m.get("role") == "user"][-1].get("content")
        is_result = isinstance(last, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in last)
        if not is_result:
            self.current = list(self.turns.popleft()) if self.turns else []
        while self.current:
            step = self.current.pop(0)
            if step.kind == "say":
                self.current = []
                return {"type": "text", "text": step.text}, "end_turn"
            name, inp = self._tool(step)
            return {"type": "tool_use", "id": f"toolu_{mid:04d}", "name": name,
                    "input": inp}, "tool_use"
        return {"type": "text", "text": "Done."}, "end_turn"

    def _tool(self, step):
        if step.kind == "shell":
            return "Bash", {"command": step.text, "description": "conformance"}
        if step.kind == "write":
            path = step.path if os.path.isabs(step.path) else str(self.workspace / step.path)
            return "Write", {"file_path": path, "content": step.content}
        if step.kind == "ask":
            return "AskUserQuestion", {"questions": [{
                "question": step.text, "header": "Choice", "multiSelect": False,
                "options": [{"label": o, "description": o} for o in step.options]}]}
        raise AssertionError(step)


class LegacyRig(driver.Rig):
    name = "legacy"
    PROJECT = "radio"

    def __init__(self, tmp: Path, mock: _ScriptedModel) -> None:
        self.workspace = tmp / "projects" / self.PROJECT / "work"
        self.outside = tmp / "projects" / "tv" / "work"
        self.mock = mock

    def capabilities(self):
        from harness import legacy
        return legacy.CAPABILITIES

    def harness(self):
        from harness import legacy
        return legacy.LegacyHarness()

    def spec(self, *, context=None, resume=None, confined=False):
        return contract.RunSpec(
            run_id="run-" + uuid.uuid4().hex[:8], cwd=str(self.workspace),
            project=self.PROJECT, user="alice@x", model="claude-sonnet-4-5", context=context,
            resume=resume,
            confinement=contract.Confinement(project=self.PROJECT) if confined else None,
            options={"legacy": {"setting_sources": []}})

    def script(self, *turns):
        self.mock.turns.extend(turns)

    def model_saw(self, text):
        return any(text in all_text(r) for r in self.mock.requests)

    def last_request_saw(self, text):
        reqs = self.mock.main_requests()
        return bool(reqs) and text in all_text(reqs[-1])


@pytest.fixture()
def legacy_rig(tmp_path, monkeypatch):
    if os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0":
        pytest.skip("SOKKAN_TEST_E2E_AGENTS=0")
    if not _bundled_cli():
        pytest.skip("claude-agent-sdk without bundled CLI")
    import audit
    import board
    import projects
    import runner
    import sandbox

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "1")
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", "/nonexistent/bwrap")    # hooks-only everywhere
    monkeypatch.delenv("SOKKAN_RUNNER", raising=False)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    projects.create("radio", "Radio", created_by="admin@x")
    projects.create("tv", "TV", created_by="admin@x")
    for p in ("radio", "tv"):
        (tmp_path / "projects" / p / "work").mkdir(parents=True, exist_ok=True)
    for k, v in {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                 "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "DISABLE_AUTOUPDATER": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    runner.set_runner(None)
    sandbox.reset()
    with _ScriptedModel(tmp_path / "projects" / "radio" / "work") as mock:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        yield LegacyRig(tmp_path, mock)
    sandbox.reset()


def _play(scenario, rig):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(scenario(rig), 240))
    finally:
        loop.close()


IDS = [name for name, _ in scenarios.ALL]


@pytest.mark.parametrize("scenario", [s for _, s in scenarios.ALL], ids=IDS)
def test_reference_engine(scenario, tmp_path):
    _play(scenario, ReferenceRig(tmp_path))


def test_reference_engine_with_question_and_plan(tmp_path):
    """C7 the other way round: the same engine built WITH the two capabilities."""
    rig = ReferenceRig(tmp_path, question=True, plan=True)
    assert rig.capabilities().question and rig.capabilities().plan
    _play(scenarios.c7_capabilities_are_honest, rig)


@pytest.mark.parametrize("scenario", [s for _, s in scenarios.ALL], ids=IDS)
def test_legacy_engine(scenario, legacy_rig):
    _play(scenario, legacy_rig)


def test_the_scenarios_catch_an_engine_that_does_not_conform(tmp_path):
    """A conformance suite that cannot fail proves nothing: three engines that cheat."""

    class RunsAnyway(reference.ReferenceHarness):       # ignores a refusal
        async def _gate(self, turn, kind, subject, options):
            await super()._gate(turn, kind, subject, options)
            return contract.Decision.allow()

    class Leaks(reference.ReferenceHarness):            # writes outside its space
        def _inside(self, path):
            return True

    class Silent(reference.ReferenceHarness):           # injects without saying so
        async def _turn(self, turn, prompt):
            put = self._mail.put
            self._mail.put = lambda ev: None if isinstance(
                ev, contract.ContextInjected) else put(ev)
            await super()._turn(turn, prompt)

    for cheat, scenario in ((RunsAnyway, scenarios.c1_shell_gate_denied),
                            (Leaks, scenarios.c3_write_outside_refused),
                            (Silent, scenarios.c2_context_injected)):
        d = tmp_path / cheat.__name__
        d.mkdir()
        rig = ReferenceRig(d)
        rig.harness = lambda rig=rig, cheat=cheat: cheat(rig.model)
        with pytest.raises(AssertionError):
            _play(scenario, rig)
