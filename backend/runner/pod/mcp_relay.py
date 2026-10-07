#!/usr/bin/env python3
"""sokkan-mcp-relay <server> — stdio MCP server inside a session container (stdlib only).

The SOKKAN MCP servers (memory, board, agents, observability) need the api's data and
code; they keep running in the api. In a session container the CLI starts this relay
instead: it opens a TCP connection to the api's relay (SOKKAN_RELAY_ADDR = host:port),
says which server it wants with the session's relay token, then pipes stdio both ways.
The api starts the real server with the identity it set for this session (user, project,
agent run) — the container cannot choose another one.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sokkan-mcp-relay <server>", file=sys.stderr)
        return 2
    host, _, port = (os.environ.get("SOKKAN_RELAY_ADDR") or "").rpartition(":")
    if not host or not port.isdigit():
        print("sokkan-mcp-relay: SOKKAN_RELAY_ADDR is not host:port", file=sys.stderr)
        return 2
    sock = socket.create_connection((host, int(port)), timeout=30)
    sock.settimeout(None)
    hdr = {"token": os.environ.get("SOKKAN_RELAY_TOKEN", ""), "server": argv[1]}
    sock.sendall((json.dumps(hdr) + "\n").encode())

    def up() -> None:  # CLI → api
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=up, daemon=True).start()
    out = sys.stdout.buffer
    while True:  # api → CLI
        try:
            chunk = sock.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        out.write(chunk)
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
