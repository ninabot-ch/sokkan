"""TEST ONLY — runs in a pod that plays the api (its ServiceAccount, labels, data PVC, relay)
on a real cluster: the kubernetes runner creates a session pod through the K8s API, the driver
talks to its supervisor, a new runner instance (= a restarted api) adopts the live pod, then
the session is stopped and must leave nothing behind. Prints one JSON line per step."""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/opt/sokkan")
from runner import base, common, relay  # noqa: E402
from runner.base import SessionSpec  # noqa: E402
from runner.kubernetes_runner import KubernetesRunner  # noqa: E402


def step(name, **kw):
    print(json.dumps({"step": name, **kw}), flush=True)


async def read_until(chan, typ):
    async for m in chan.events():
        if m.get("type") == typ:
            return m
    return None


async def main():
    await relay.ensure_started()
    relay.register(common.token_for("smoke01", "relay"), "smoke01",
                   {"echo": {"command": "python3", "args": ["-c", (
                       "import os,sys\nfor l in sys.stdin: print('relay-as-' + "
                       "os.environ['SOKKAN_SESSION_USER'] + ':' + l.strip(), flush=True)")],
                       "env": {"SOKKAN_SESSION_USER": "alice"}}})
    spec = SessionSpec(sid="smoke01", argv=["claude", "--output-format", "stream-json"],
                       cwd="/data/projects/alpha/work", project="alpha", user="alice",
                       env={"ANTHROPIC_API_KEY": "sk-test"},
                       token=common.token_for("smoke01", "supervisor"),
                       relay_token=common.token_for("smoke01", "relay"))
    r1 = KubernetesRunner(poll_s=0.5)
    t0 = time.monotonic()
    h = await r1.start(spec)
    step("pod_running", pod=h.name, ip=h.host, seconds=round(time.monotonic() - t0, 1))
    chan = await r1.open(h)
    await chan.send({"type": "control_request", "request_id": "req_1",
                     "request": {"subtype": "initialize"}})
    await read_until(chan, "control_response")
    await chan.send({"type": "user", "message": {"role": "user", "content": "hello k8s"}})
    m = await read_until(chan, "assistant")
    step("turn", text=m["message"]["content"][0]["text"], resumed=chan.hello.get("resumed"))
    await chan.close()                                   # the api "dies"
    step("api_gone")
    # wait for the external checks (exec into the session pod) before the restart
    while not os.path.exists("/tmp/continue"):
        await asyncio.sleep(1)
    r2 = KubernetesRunner(poll_s=0.5)                    # a restarted api
    h2 = await r2.start(spec)
    chan2 = await r2.open(h2)
    await chan2.send({"type": "control_request", "request_id": "req_9",
                      "request": {"subtype": "initialize"}})
    init = await read_until(chan2, "control_response")
    await chan2.send({"type": "user", "message": {"role": "user", "content": "after restart"}})
    m2 = await read_until(chan2, "assistant")
    step("reattach", adopted=h2.adopted, same_pod=h2.name == h.name,
         resumed=chan2.hello.get("resumed"),
         cached_init=init["response"]["response"].get("inits") == 1,
         text=m2["message"]["content"][0]["text"], usage=await r2.usage(h2),
         status=await r2.status(h2))
    await chan2.close()
    await r2.stop(h2)
    for _ in range(60):
        if await r2.status(h2) == "gone":
            break
        await asyncio.sleep(1)
    step("stopped", status=await r2.status(h2),
         secret=r2.api.get("secrets", h2.name + "-env") is not None,
         transcript_dir=os.path.isdir("/data/claude/projects/-data-projects-alpha-work"))
    step("done")


asyncio.run(main())
