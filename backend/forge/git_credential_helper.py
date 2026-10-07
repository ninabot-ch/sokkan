#!/usr/bin/env python3
"""git credential helper of a SOKKAN session (lot 5) — standard library only.

git runs it as ``<helper> get|store|erase`` with the request on stdin (protocol, host,
path…). ``get`` asks the SOKKAN API, with the session's ticket (SOKKAN_FORGE_TICKET), for
the PERSON's current token, and prints it to git — on stdout, which git reads; it never
reaches a terminal, a log, the environment or a file. ``erase`` (git was refused) asks the
API to re-read the person's access. ``store`` is a no-op: nothing is kept.

How it reaches the API (forge.gitcred, first match):

* ``SOKKAN_FORGE_SOCKET`` — the session's Unix socket (Bash inside bubblewrap, no network);
* ``SOKKAN_RELAY_ADDR`` + ``SOKKAN_RELAY_TOKEN`` — the runner's authenticated relay (session
  container / pod: the api's loopback is out of reach);
* ``SOKKAN_FORGE_API_URL`` — HTTP on the api's loopback (local runner).

``<helper> --forward <port> <socket>`` (bubblewrap without network only): listens on
127.0.0.1:<port> INSIDE the sandbox's private network and pipes each connection to the
session socket, which tunnels to the project's forge hosts only. It returns once
listening and keeps serving in the background until the sandbox ends.
"""
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request


def _ask_line(sock: socket.socket, msg: dict) -> dict:
    sock.sendall((json.dumps(msg) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.decode() or "{}")


def ask(req: dict) -> dict:
    path = os.environ.get("SOKKAN_FORGE_SOCKET", "")
    if path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(20)
            s.connect(path)
            return _ask_line(s, req)
    relay, token = os.environ.get("SOKKAN_RELAY_ADDR", ""), os.environ.get("SOKKAN_RELAY_TOKEN", "")
    if relay and token:
        host, _, port = relay.rpartition(":")
        with socket.create_connection((host, int(port)), timeout=20) as s:
            return _ask_line(s, {"token": token, "op": "git-credential", "request": req})
    api = os.environ.get("SOKKAN_FORGE_API_URL", "http://127.0.0.1:8097").rstrip("/")
    r = urllib.request.Request(api + "/api/forge/git-credential", data=json.dumps(req).encode(),
                               method="POST", headers={"content-type": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _serve(srv: socket.socket, path: str) -> None:
    while True:
        c, _ = srv.accept()
        u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            u.connect(path)
        except OSError:
            c.close()
            continue
        threading.Thread(target=_pipe, args=(c, u), daemon=True).start()
        threading.Thread(target=_pipe, args=(u, c), daemon=True).start()


def forward(port: int, path: str) -> int:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError:
        return 0          # already forwarding in this sandbox
    srv.listen(16)
    if os.fork():         # parent: listening, the command may start
        return 0
    os.setsid()
    fd = os.open(os.devnull, os.O_RDWR)
    for i in (0, 1, 2):   # never hold the command's output pipes open
        os.dup2(fd, i)
    _serve(srv, path)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "--forward":
        return forward(int(argv[2]), argv[3])
    action = argv[1] if len(argv) > 1 else ""
    attrs: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            attrs[k] = v
    if action not in ("get", "erase"):
        return 0
    ticket = os.environ.get("SOKKAN_FORGE_TICKET", "")
    if not ticket:
        print("sokkan: no forge ticket in this session — it cannot push", file=sys.stderr)
        return 0
    req = {"ticket": ticket, "action": action, "protocol": attrs.get("protocol", ""),
           "host": attrs.get("host", ""), "path": attrs.get("path", "")}
    try:
        ans = ask(req)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"sokkan: credential request failed ({type(e).__name__})", file=sys.stderr)
        return 0
    if action == "get":
        if ans.get("username") and ans.get("password"):
            sys.stdout.write(f"username={ans['username']}\npassword={ans['password']}\n")
        elif ans.get("reason"):
            print(f"sokkan: no credentials for {attrs.get('host', '?')}: {ans['reason']}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
