#!/usr/bin/env python3
"""Runs INSIDE the api container (it has the `websockets` package): follow one SDK session on
the api's own port and print its events as JSON lines until the turn ends.

    python3 ws_session.py <sid> <cookie value> [--send "text"] [--timeout 120]

Ends at the first `result` event that follows the (optional) message sent, or at an `error`
event carrying a `code` (a readable inference error), or at the timeout (exit 3)."""
import asyncio
import json
import sys

import websockets


async def main() -> int:
    sid, cookie = sys.argv[1], sys.argv[2]
    send = sys.argv[sys.argv.index("--send") + 1] if "--send" in sys.argv else None
    timeout = float(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 120.0
    url = f"ws://127.0.0.1:8097/api/agent/ws/{sid}"
    hdr = {"Cookie": f"sokkan_session={cookie}"}
    try:
        ws = await websockets.connect(url, additional_headers=hdr, max_size=None)
    except TypeError:                      # websockets < 14
        ws = await websockets.connect(url, extra_headers=hdr, max_size=None)
    try:
        async with asyncio.timeout(timeout):
            if send:
                # let the replay of the seed turn drain first, then speak
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), 1.5)
                except asyncio.TimeoutError:
                    pass
                await ws.send(json.dumps({"type": "user", "text": send}))
            async for raw in ws:
                ev = json.loads(raw)
                print(json.dumps(ev, ensure_ascii=False), flush=True)
                if ev.get("type") == "permission":      # the test never needs a tool: deny
                    await ws.send(json.dumps({"type": "permission", "id": ev.get("id"), "decision": "deny"}))
                if ev.get("type") == "error" and ev.get("code"):
                    return 0
                if ev.get("type") == "result":
                    # an inference error is announced right after the result: give it a beat
                    try:
                        nxt = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
                        print(json.dumps(nxt, ensure_ascii=False), flush=True)
                    except asyncio.TimeoutError:
                        pass
                    return 0
    except TimeoutError:
        return 3
    finally:
        await ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
