#!/usr/bin/env python3
"""sokkan — terminal companion for a SOKKAN cockpit.

Talks to the same HTTP API as the web UI (local-token auth). Zero dependencies.

    pipx install "git+https://github.com/ninabot-ch/sokkan"   # or pip install
    sokkan login http://localhost:3009
    sokkan spawn "add pagination to GET /notes"
    sokkan status

Config: ~/.config/sokkan/cli.json — overridable with SOKKAN_URL / SOKKAN_TOKEN.
"""
from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("SOKKAN_CLI_CONFIG",
                             os.path.expanduser("~/.config/sokkan/cli.json")))
TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


def die(msg: str) -> None:
    print(_c("31", f"✗ {msg}"), file=sys.stderr)
    raise SystemExit(1)


def load_config() -> dict:
    cfg = {}
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if os.environ.get("SOKKAN_URL"):
        cfg["url"] = os.environ["SOKKAN_URL"]
    if os.environ.get("SOKKAN_TOKEN"):
        cfg["token"] = os.environ["SOKKAN_TOKEN"]
    return cfg


class Client:
    def __init__(self, url: str, token: str = ""):
        self.base = url.rstrip("/")
        self.token = token
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self._authed = False

    def _raw(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"}, method=method)
        with self.opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode() or "null")

    def login(self) -> None:
        if self._authed or not self.token:
            return
        try:
            self._raw("POST", "/api/auth/local", {"token": self.token})
        except urllib.error.HTTPError as e:
            if e.code == 400:  # open instance: local login "not applicable"
                pass
            elif e.code == 401:
                die("invalid token — run `sokkan login` again")
            else:
                raise
        self._authed = True

    def call(self, method: str, path: str, body: dict | None = None):
        self.login()
        try:
            return self._raw(method, path, body)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("detail", "")
            except Exception:  # noqa: BLE001
                pass
            die(f"{method} {path} → {e.code} {detail or e.reason}")
        except urllib.error.URLError as e:
            die(f"cannot reach {self.base} ({e.reason}) — is the cockpit up?")


def client() -> Client:
    cfg = load_config()
    if not cfg.get("url"):
        die("not configured — run: sokkan login http://localhost:3009")
    return Client(cfg["url"], cfg.get("token", ""))


def ago(ts: float | None) -> str:
    if not ts:
        return "—"
    s = time.time() - ts
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s / 60)}m"
    if s < 129600:
        return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


# ---- commands ----------------------------------------------------------------

def cmd_login(args) -> None:
    url = (args.url or load_config().get("url") or "").rstrip("/")
    if not url:
        die("usage: sokkan login <url>   (e.g. http://localhost:3009)")
    token = args.token or getpass.getpass("access token (empty for open instances): ")
    c = Client(url, token)
    c.login()
    me = c.call("GET", "/api/me")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"url": url, "token": token}, indent=2), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass
    print(_c("32", f"✔ logged in to {url} as {me.get('email', '?')} ({me.get('role', '?')})"))


def cmd_status(args) -> None:
    c = client()
    inst = c.call("GET", "/api/instance")
    mem = c.call("GET", "/api/memory/stats")
    sess = c.call("GET", "/api/sessions")
    active = [s for s in sess if s.get("active")]
    upd = inst.get("update") or {}
    print(f"{_c('1', inst.get('org_name') or 'SOKKAN')} — {c.base}")
    print(f"  version   {upd.get('local_version', '?')}"
          + (_c("33", f"  (update available: {upd['latest']})") if upd.get("update_available") else ""))
    print(f"  sessions  {len(sess)} total · {len(active)} active")
    print(f"  memory    {mem.get('notes', 0)} notes · {mem.get('chunks', 0)} chunks"
          f" · reindexed {ago(mem.get('last_mtime'))} ago")
    llm = c.call("GET", "/api/llm")
    mode = llm.get("mode", "?")
    model = llm.get("model") or ("anthropic" if mode in ("byok", "env") else "—")
    flag = _c("32", "●") if llm.get("configured") else _c("33", "○ not configured —")
    print(f"  model     {flag} {mode} · {model}")


def cmd_sessions(args) -> None:
    for s in client().call("GET", "/api/sessions"):
        dot = _c("32", "●") if s.get("active") else _c("90", "○")
        title = s.get("title") or (s.get("prompt") or "")[:60]
        print(f"{dot} {s['session_id'][:8]}  {s.get('tag', ''):<12} {ago(s.get('mtime')):>4}  {title}")


def cmd_spawn(args) -> None:
    c = client()
    s = c.call("POST", "/api/spawn",
               {"tag": args.tag, "prompt": args.task, "title": args.title or "", "kind": "sdk"})
    print(_c("32", f"✔ session {s['session_id']} spawned") + f" — memory recall first, then it waits for your go\n  {c.base}  (Sessions tab)")


def cmd_board(args) -> None:
    d = client().call("GET", "/api/board")
    cards = d.get("cards", {})  # {bucket: [card, …]}
    for bucket in d.get("buckets", []):
        rows = cards.get(bucket, [])
        if not rows:
            continue
        print(_c("1", f"{bucket} ({len(rows)})"))
        for x in rows:
            sid = f" · {x['session_id'][:8]}" if x.get("session_id") else ""
            print(f"  #{x['id']:<4} {x['title'][:70]}{_c('90', sid)}")


def cmd_card(args) -> None:
    c = client()
    card = c.call("POST", "/api/board/card",
                  {"title": args.title, "description": args.description or args.title,
                   "tag": args.tag, "bucket": "Backlog"})
    print(_c("32", f"✔ card #{card['id']} created") + f" — {card['title']}")
    if args.spawn:
        s = c.call("POST", f"/api/board/card/{card['id']}/spawn")
        print(_c("32", f"✔ session {s['session_id']} spawned from card #{card['id']}"))


def cmd_mem(args) -> None:
    res = client().call("GET", "/api/memory/search?"
                        + urllib.parse.urlencode({"q": args.query, "k": args.k}))
    for r in res:
        if r.get("empty") or r.get("error") or r.get("info"):
            print(r.get("info") or r.get("error"))
            return
        star = _c("33", "★ ") if r.get("priority") else ""
        score = r.get("score") or 0.0
        print(f"{_c('36', f'{score:.2f}')} {star}{_c('1', r['note_name'])}")
        print(f"      {r.get('snippet', '')[:160]}")


def cmd_note(args) -> None:
    d = client().call("GET", f"/api/memory/note/{urllib.parse.quote(args.name)}")
    print(d.get("body", ""))


def cmd_digest(args) -> None:
    c = client()
    s = c.call("POST", "/api/memory/digest")
    print(_c("32", f"✔ digest session {s['session_id']} spawned") + " — it will condense the memory into `project-status`")


def cmd_health(args) -> None:
    d = client().call("GET", "/api/health")
    print(_c("32", "✔ healthy") if d.get("ok") else _c("31", f"✗ {d}"))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sokkan", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="save cockpit URL + access token")
    s.add_argument("url", nargs="?")
    s.add_argument("--token", help="access token (prompted if omitted)")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("status", help="instance, sessions, memory, model at a glance")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("sessions", help="list sessions")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("spawn", help="spawn a session (memory recall first, HITL)")
    s.add_argument("task")
    s.add_argument("-t", "--tag", default="backend")
    s.add_argument("--title", default="")
    s.set_defaults(fn=cmd_spawn)

    s = sub.add_parser("board", help="show the kanban")
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("card", help="create a card (add --spawn to run it now)")
    s.add_argument("title")
    s.add_argument("-d", "--description", default="")
    s.add_argument("-t", "--tag", default="backend")
    s.add_argument("--spawn", action="store_true")
    s.set_defaults(fn=cmd_card)

    s = sub.add_parser("mem", help="semantic search over the project memory")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=8)
    s.set_defaults(fn=cmd_mem)

    s = sub.add_parser("note", help="print a memory note")
    s.add_argument("name")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("digest", help="spawn the memory-digest session")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("health", help="ping /api/health")
    s.set_defaults(fn=cmd_health)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
