"""Session runners — where the Claude Code CLI of a session or an agent run executes.

`SOKKAN_RUNNER` selects one (feature `kubernetes_runner`, docs/enterprise/KUBERNETES.md):

* `local` (default): a subprocess of the api — the behaviour of every release so far;
* `docker`: one container per session / run on the compose host (runner/docker_runner.py);
* `kubernetes`: one Pod per session / run through the Kubernetes API
  (runner/kubernetes_runner.py, Helm chart deploy/helm/sokkan).

The engine's adapter asks where its CLI runs (`harness.legacy.client_args`): for `local` it
keeps its options unchanged and no transport (the SDK spawns the CLI as before); for a
container runner it gets the options rewritten for the container (MCP servers through the
relay) and a `RunnerTransport` — the SDK's own transport, carried over the runner's channel.
Both live in the adapter: nothing here imports the engine's SDK.
"""
from __future__ import annotations

import os
import sys

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
