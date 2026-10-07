"""Run INSIDE the api pod (k3d-test.sh): one real cockpit session through agentchat with the
kubernetes runner. Phase `start`: a session pod is created, one turn, then this process dies
WITHOUT closing the session (= an api restart). Phase `resume`: a new process reattaches to
the same live pod, runs a turn, then closes the session (pod + Secret deleted)."""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app/backend")
import agentchat  # noqa: E402

SID = os.environ.get("SMOKE_SID", "k3dsmoke0001")


async def turn(text: str) -> dict:
    s = agentchat.get_or_create(SID, user="smoke@example.com")
    await s.handle_user(text)
    out = [e.get("text") for e in s.events if e.get("type") == "text"]
    t = getattr(s.client, "_transport", None)
    h = getattr(t, "handle", None)
    return {"events": [e.get("type") for e in s.events][-12:], "texts": [x for x in out if x],
            "errors": [e.get("message") for e in s.events if e.get("type") == "error"],
            "pod": getattr(h, "name", None), "adopted": getattr(h, "adopted", None),
            "resumed": getattr(getattr(t, "chan", None), "hello", {}).get("resumed")}


def check(r: dict, adopted: bool) -> None:
    print(json.dumps(r), flush=True)
    bad = [t for t in r["texts"] if "API Error" in t] or r["errors"]
    if bad or not r["texts"] or r["adopted"] is not adopted:
        print(f"SMOKE FAILED: {bad or r}", flush=True)
        os._exit(1)


async def main(phase: str) -> None:
    if phase == "start":
        check(await turn("hello from k3d"), adopted=False)
        os._exit(0)          # no drop: the pod must survive, like an api restart
    check(await turn("second turn after the api restart"), adopted=True)
    await agentchat.drop(SID)
    print(json.dumps({"dropped": True}), flush=True)


asyncio.run(main(sys.argv[1]))
