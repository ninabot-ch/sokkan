#!/usr/bin/env python3
"""Runs INSIDE the api container of the kernel-compose battery (tests/e2e_kernel_compose/run.sh):
one Atelier work on the kernel engine, through the api a browser uses.

    python3 atelier.py start [--timeout 420]    sign in, start a work, approve every card,
                                                stop when a deliverable is saved; one JSON line
    python3 atelier.py close <sid>              DELETE /api/sessions/<sid>

The sign-in code is the instance's SOKKAN_LOCAL_TOKEN (the api's own environment)."""
import asyncio
import json
import os
import sys
import time

import httpx
import websockets

BASE = "http://127.0.0.1:8097"
ASK = ("Rédige un court mail de relance (cinq lignes au plus) pour un client qui n'a pas répondu "
       "à notre devis du mois dernier, et enregistre-le comme document.")


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/api/auth/local", json={"token": os.environ["SOKKAN_LOCAL_TOKEN"]})
    r.raise_for_status()
    return c


async def follow(sid: str, cookie: str, timeout: float, c: httpx.Client) -> dict:
    out = {"sid": sid, "cards": 0, "errors": [], "events": {}, "deliverables": []}
    url = f"ws://127.0.0.1:8097/api/agent/ws/{sid}"
    hdr = {"Cookie": f"sokkan_session={cookie}"}
    try:
        ws = await websockets.connect(url, additional_headers=hdr, max_size=None)
    except TypeError:                      # websockets < 14
        ws = await websockets.connect(url, extra_headers=hdr, max_size=None)
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 5))
            except asyncio.TimeoutError:
                ev = {}
            t = ev.get("type")
            if t:
                out["events"][t] = out["events"].get(t, 0) + 1
            if t == "permission":
                out["cards"] += 1
                await ws.send(json.dumps({"type": "permission", "id": ev.get("id"), "decision": "allow"}))
            if t == "error":
                out["errors"].append({"code": ev.get("code"), "message": str(ev.get("message"))[:300]})
                if ev.get("code") in ("sandbox_unavailable", "engine_unavailable"):
                    break
            d = c.get(f"/api/simple/sessions/{sid}/deliverables").json().get("deliverables") or []
            if d and (t in ("result", None) or out["cards"]):
                out["deliverables"] = [x.get("name") for x in d]
                if t in ("result", None):
                    break
    finally:
        await ws.close()
    out["s"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    cmd = sys.argv[1]
    c = client()
    if cmd == "close":
        r = c.delete(f"/api/sessions/{sys.argv[2]}")
        print(json.dumps({"close": r.status_code}))
        return 0 if r.status_code == 200 else 1
    timeout = float(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 420.0
    r = c.post("/api/simple/start", json={"ask": ASK, "lang": "fr"}, timeout=120)
    if r.status_code != 200:
        print(json.dumps({"start": r.status_code, "body": r.text[:300]}))
        return 1
    sid = r.json()["session_id"]
    out = asyncio.run(follow(sid, c.cookies.get("sokkan_session") or "", timeout, c))
    eng = c.get("/api/engines/session", params={"session_id": sid})
    out["engine"] = eng.json() if eng.status_code == 200 else eng.status_code
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["deliverables"] else 1


if __name__ == "__main__":
    sys.exit(main())
