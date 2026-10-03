#!/usr/bin/env python3
"""HTTP side of the self-hosted upgrade bench: what a user (browser) sees on :3009.

Runs on the "machine" (the Docker host of the test), stdlib only, any SOKKAN version.

    client.py snapshot <token> <out.json>      health, sessions, board, search, CortHeXis
    client.py watch <token> <seconds> <query>  one JSON line per second (search through the
                                               web, re-login after a restart)
"""
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3009"
QUERY = "who delivers our flour and when?"


class Client:
    def __init__(self, token: str):
        self.token = token
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def login(self) -> None:
        req = urllib.request.Request(BASE + "/api/auth/local",
                                     data=json.dumps({"token": self.token}).encode(),
                                     headers={"content-type": "application/json"})
        self.op.open(req, timeout=10).read()

    def get(self, path: str, timeout: float = 20, raw: bool = False):
        for attempt in (0, 1):
            try:
                with self.op.open(BASE + path, timeout=timeout) as r:
                    body = r.read()
                    return (r.status, body.decode("utf-8", "replace") if raw else
                            json.loads(body or b"null"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:
                    self.login()
                    continue
                return e.code, None
        return 0, None


def _q(s: str) -> str:
    return urllib.request.quote(s)


def snapshot(c: Client) -> dict:
    out: dict = {"t": time.time()}
    try:
        c.login()
    except Exception as e:  # noqa: BLE001
        out["login_error"] = repr(e)
    st, h = c.get("/api/health")
    out["health"] = {"status": st, "body": h}
    st, ss = c.get("/api/sessions")
    out["sessions"] = {"status": st, "ids": sorted(s.get("session_id") for s in ss or [])}
    st, b = c.get("/api/board")
    cards = (b or {}).get("cards") if isinstance(b, dict) else None
    if isinstance(cards, dict):           # {bucket: [card…]}
        cards = [x for v in cards.values() for x in (v or [])]
    out["board"] = {"status": st, "titles": sorted(str(x.get("title")) for x in cards or []
                                                   if isinstance(x, dict))}
    st, r = c.get("/api/memory/search?q=" + _q(QUERY) + "&k=3")
    out["search"] = {"status": st, "top": [x.get("note_name") for x in r or []]}
    st, g = c.get("/api/corthexis/graph", timeout=60)
    out["corthexis_graph"] = {"status": st,
                              "nodes": len((g or {}).get("nodes") or []) if st == 200 else None}
    st, m = c.get("/api/memory/migration")
    out["migration"] = {"status": st, "state": (m or {}).get("status") if st == 200 else None,
                        "serving": (m or {}).get("serving") if st == 200 else None}
    # the web app: page served, and does its bundle carry the CortHeXis tab?
    st, html = c.get("/", raw=True)
    out["web"] = {"status": st, "corthexis_tab": False}
    if st == 200 and html:
        for src in sorted(set(re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', html))):
            s2, js = c.get(src, raw=True)
            if s2 == 200 and js and "CortHeXis" in js:
                out["web"]["corthexis_tab"] = True
                break
    return out


def watch(c: Client, seconds: float, q: str) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        t0 = time.monotonic()
        row = {"t": round(time.time(), 1)}
        try:
            st, r = c.get("/api/memory/search?q=" + _q(q) + "&k=3", timeout=10)
            row.update(status=st, top=(r[0].get("note_name") if r else None)
                       if isinstance(r, list) else None,
                       error=(r[0].get("error") if r and isinstance(r, list) else None))
        except Exception as e:  # noqa: BLE001 — connection refused while containers restart
            row.update(status=0, top=None, error=type(e).__name__)
        row["ms"] = round(1000 * (time.monotonic() - t0))
        print(json.dumps(row), flush=True)
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))


if __name__ == "__main__":
    cmd, token = sys.argv[1], sys.argv[2]
    c = Client(token)
    if cmd == "snapshot":
        snap = snapshot(c)
        with open(sys.argv[3], "w") as fh:
            json.dump(snap, fh, indent=1)
        print(json.dumps(snap)[:600])
    elif cmd == "watch":
        watch(c, float(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else QUERY)
