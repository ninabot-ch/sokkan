"""Session runners — where the Claude Code CLI of a session or an agent run executes.

`SOKKAN_RUNNER` selects one (feature `kubernetes_runner`, docs/enterprise/KUBERNETES.md):

* `local` (default): a subprocess of the api — the behaviour of every release so far;
* `docker`: one container per session / run on the compose host (runner/docker_runner.py);
* `kubernetes`: one Pod per session / run through the Kubernetes API
  (runner/kubernetes_runner.py, Helm chart deploy/helm/sokkan).

agentchat calls `client_args()` where it used to build the SDK client: for `local` it gets
the options unchanged and no transport (the SDK spawns the CLI as before); for a container
runner it gets the options rewritten for the container (MCP servers through the relay) and a
`RunnerTransport`.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from .base import Handle, Resources, SessionRunner, SessionSpec, session_env  # noqa: F401

RUNNERS = ("local", "docker", "kubernetes")
_runner: SessionRunner | None = None
_pinned = False
_reconciled = False


def selected() -> str:
    """The runner this instance uses (feature `kubernetes_runner` + SOKKAN_RUNNER)."""
    v = (os.environ.get("SOKKAN_RUNNER") or "local").strip().lower()
    if v not in RUNNERS:
        print(f"[sokkan] SOKKAN_RUNNER={v!r} unknown: local runner", file=sys.stderr)
        return "local"
    if v != "local":
        try:
            import features
            if not features.enabled("kubernetes_runner"):
                return "local"
        except Exception:  # noqa: BLE001 — registry unavailable: honour the variable
            pass
    return v


def get_runner() -> SessionRunner:
    global _runner
    if _pinned and _runner is not None:
        return _runner
    name = selected()
    if _runner is None or _runner.name != name:
        if name == "docker":
            from .docker_runner import DockerRunner
            _runner = DockerRunner()
        elif name == "kubernetes":
            from .kubernetes_runner import KubernetesRunner
            _runner = KubernetesRunner()
        else:
            from .local import LocalRunner
            _runner = LocalRunner()
    return _runner


def set_runner(r: SessionRunner | None) -> None:
    """Tests: install a runner (None = back to SOKKAN_RUNNER)."""
    global _runner, _pinned, _reconciled
    _runner, _pinned, _reconciled = r, r is not None, False


def build_argv(options: Any, cli: str = "claude") -> list[str]:
    """The CLI command line the SDK would run for these options (same flags, same
    `--permission-prompt-tool stdio` when can_use_tool is set)."""
    from dataclasses import replace

    from claude_agent_sdk._internal.transport.subprocess_cli import (  # type: ignore
        SubprocessCLITransport)
    if getattr(options, "can_use_tool", None) and not options.permission_prompt_tool_name:
        options = replace(options, permission_prompt_tool_name="stdio")

    async def _empty():  # the SDK writes the prompt itself (streaming mode)
        return
        yield  # noqa: B901

    t = SubprocessCLITransport(prompt=_empty(), options=options)
    t._cli_path = cli
    return t._build_command()


async def _reconcile_once(r: SessionRunner) -> None:
    global _reconciled
    if _reconciled:
        return
    _reconciled = True
    try:
        res = await r.reconcile()
        if res.get("removed") or res.get("kept"):
            print(f"[sokkan] runner {r.name}: kept {len(res.get('kept', []))} live session(s), "
                  f"removed {len(res.get('removed', []))} finished", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[sokkan] runner {r.name}: reconcile failed: {e!r}", file=sys.stderr)


def client_args(sid: str, opts_kwargs: dict, user: str = "", project: str = "default",
                kind: str = "session") -> tuple[Any, Any]:
    """(ClaudeAgentOptions, transport | None) for one session — the runner extension point."""
    from claude_agent_sdk import ClaudeAgentOptions  # type: ignore

    r = get_runner()
    if not r.remote:
        return ClaudeAgentOptions(**opts_kwargs), None

    from . import common, relay
    from .transport import RunnerTransport

    relay_token = common.token_for(sid, "relay")
    servers = opts_kwargs.get("mcp_servers") or {}
    relay.register(relay_token, sid, servers)
    remote_kwargs = dict(opts_kwargs)
    remote_kwargs["mcp_servers"] = {n: {"command": "sokkan-mcp-relay", "args": [n]}
                                    for n in servers}
    remote_kwargs.pop("env", None)  # goes to the container's Secret, filtered
    options = ClaudeAgentOptions(**remote_kwargs)
    spec = SessionSpec(
        sid=sid, argv=build_argv(options, os.environ.get("SOKKAN_SESSION_CLI") or "claude"),
        cwd=str(opts_kwargs.get("cwd") or "/workspace"), project=project or "default",
        user=user, env=session_env(opts_kwargs.get("env")),
        token=common.token_for(sid, "supervisor"), relay_token=relay_token, kind=kind)

    async def _before() -> None:
        await relay.ensure_started()
        await _reconcile_once(r)

    t = RunnerTransport(r, spec, on_close=lambda: relay.unregister(relay_token))
    t.before_connect = _before  # type: ignore[attr-defined]
    return options, t
