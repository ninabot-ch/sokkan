"""No Claude Code CLI a run can start is older than MIN_CLAUDE_CLI — offline, from the pins.

Which CLI runs (backend/harness/legacy.py, backend/agentchat.py):
* api image, local runner, a Claude work: the npm CLI of docker/api.Dockerfile
  (`claudetier.pinned_cli()` → `cli_path`);
* api image, local runner, any other run, or a venv install: the CLI bundled with
  claude-agent-sdk (the SDK looks for its own binary first);
* docker / kubernetes runner: `claude` in the session image = the SDK's bundled CLI, copied by
  docker/session.Dockerfile (`build_argv` sets argv[0], `cli_path` is not used there).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The Anthropic API refuses Claude Opus 5.5 to an older CLI: « 400 Claude Code 2.1.259 does not
# support this model; version 2.1.280 or newer is required » (seen with claude-agent-sdk 0.2.152,
# which bundles 2.1.259). Raise it when the API asks for a newer CLI.
MIN_CLAUDE_CLI = "2.1.280"


def _v(s: str) -> tuple[int, ...]:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", s.strip())
    assert m, f"not a version: {s!r}"
    return tuple(int(x) for x in m.groups())


def _arg(dockerfile: str, name: str) -> str:
    text = (ROOT / "docker" / dockerfile).read_text()
    m = re.search(rf"^ARG {name}=(\d+\.\d+\.\d+)$", text, re.M)
    assert m, f"docker/{dockerfile}: ARG {name}=<x.y.z>"
    return m.group(1)


def _sdk_pin() -> str:
    req = (ROOT / "backend" / "requirements.txt").read_text()
    m = re.search(r"^claude-agent-sdk==(\d+\.\d+\.\d+)$", req, re.M)
    assert m, "backend/requirements.txt: claude-agent-sdk==<x.y.z>"
    return m.group(1)


def test_version_compare_is_numeric():
    assert _v("2.1.280") > _v("2.1.259") and _v("2.1.1000") > _v("2.1.280")
    assert _v("2.1.326 (Claude Code)") == (2, 1, 326)


def test_api_image_cli_is_recent_enough():
    v = _arg("api.Dockerfile", "CLAUDE_CODE_VERSION")
    assert _v(v) >= _v(MIN_CLAUDE_CLI), f"api.Dockerfile pins Claude Code {v} < {MIN_CLAUDE_CLI}"


def test_session_image_cli_is_recent_enough_and_matches_the_api():
    v = _arg("session.Dockerfile", "CLAUDE_CLI_VERSION")
    assert _v(v) >= _v(MIN_CLAUDE_CLI), f"session.Dockerfile CLI {v} < {MIN_CLAUDE_CLI}"
    assert v == _arg("api.Dockerfile", "CLAUDE_CODE_VERSION"), (
        "a run must start the same CLI on every runner: session.Dockerfile CLAUDE_CLI_VERSION "
        "(the SDK's bundled CLI) and api.Dockerfile CLAUDE_CODE_VERSION differ")
    text = (ROOT / "docker" / "session.Dockerfile").read_text()
    assert '/claude --version | grep -F "${CLAUDE_CLI_VERSION} "' in text, (
        "the session image build must check the bundled CLI it copies")


def test_installed_sdk_bundles_a_recent_enough_cli():
    sdk = pytest.importorskip("claude_agent_sdk")
    try:
        from claude_agent_sdk._cli_version import __cli_version__ as bundled
    except ImportError:  # pragma: no cover — an SDK that does not say which CLI it bundles
        pytest.fail("claude_agent_sdk does not expose its bundled CLI version")
    installed = getattr(sdk, "__version__", "")
    assert _v(bundled) >= _v(MIN_CLAUDE_CLI), (
        f"claude-agent-sdk {installed} bundles Claude Code {bundled} < {MIN_CLAUDE_CLI}")
    if installed == _sdk_pin():
        assert bundled == _arg("session.Dockerfile", "CLAUDE_CLI_VERSION"), (
            f"claude-agent-sdk {installed} bundles {bundled}: update CLAUDE_CLI_VERSION in "
            "docker/session.Dockerfile (and CLAUDE_CODE_VERSION in docker/api.Dockerfile)")


# ---- what the newer CLI changed that a run must not see ------------------------------------
def test_a_run_keeps_its_mcp_tools_loaded(monkeypatch):
    """Claude Code 2.1.326 defers the MCP tools behind ToolSearch; 2.1.259 offered them. A run
    keeps them offered (tests/test_board_e2e_cli.py sees them in the request), unless the
    operator chose otherwise."""
    pytest.importorskip("claude_agent_sdk")
    from harness import contract, legacy
    from runner import base

    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    kw = legacy.LegacyHarness()._options(contract.RunSpec(run_id="s", cwd="/w"))
    assert kw["env"] == {"ENABLE_TOOL_SEARCH": "false"}
    kw = legacy.LegacyHarness()._options(contract.RunSpec(run_id="s", cwd="/w", env={"A": "1"}))
    assert kw["env"]["ENABLE_TOOL_SEARCH"] == "false" and kw["env"]["A"] == "1"
    # it reaches a session container too (docker / kubernetes runner)
    assert base.session_env(kw["env"], api_env={})["ENABLE_TOOL_SEARCH"] == "false"

    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    kw = legacy.LegacyHarness()._options(contract.RunSpec(run_id="s", cwd="/w"))
    assert "ENABLE_TOOL_SEARCH" not in kw["env"]  # the CLI inherits the operator's value
    assert base.session_env(kw["env"])["ENABLE_TOOL_SEARCH"] == "true"
