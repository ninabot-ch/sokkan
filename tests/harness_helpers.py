"""Helpers of the tests that play a session through the session contract.

The cockpit's session (`agentchat.AgentSession`) no longer knows the engine's SDK: what a
test used to hand it directly (an SDK message, a permission request, a scripted client)
now enters where it enters in production — through the engine's adapter
(`harness.legacy`), which turns it into contract events.
"""
from __future__ import annotations


def ask(sess, tool: str, inp: dict | None = None):
    """A permission request of the engine for one tool call, decided by the cockpit:
    the coroutine returns the `contract.Decision` (as the adapter's permission callback
    receives it)."""
    from harness import legacy
    return sess.gate(legacy.tool_gate(tool, inp if inp is not None else {}))


def scripted(sess, client):
    """Run the session on a scripted SDK client (`query` / `receive_response`): the real
    adapter, a fake CLI."""
    from harness import legacy
    sess.attach(legacy.LegacyHarness(client=client))
    return sess


def feed(sess, msg) -> None:
    """One SDK message, as the CLI would send it: adapter → contract events → session."""
    from harness import legacy
    if sess.harness is None:
        sess.attach(legacy.LegacyHarness(client=object()))
    sess.harness.drain()
    for ev in sess.harness.translate(msg):
        sess._consume(ev)


def drain(sess) -> None:
    """Let the session consume what its engine has emitted so far (no turn running)."""
    for ev in sess.harness.drain():
        sess._consume(ev)
