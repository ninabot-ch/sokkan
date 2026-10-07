#!/usr/bin/env python3
"""sokkan-egress-proxy — the only way out of a session container (stdlib only).

Session containers have no direct network access (docker: internal network; kubernetes:
NetworkPolicy). Their CLI reaches the model endpoint, and the forges the operator allows,
through this HTTPS CONNECT proxy (HTTPS_PROXY in the session env). Only CONNECT to an
allowed host:port is relayed; everything else gets 403. Allowed hosts:
SOKKAN_EGRESS_ALLOW = comma list of `host`, `*.domain` or `host:port` (port 443 when
omitted). Each decision is logged on stderr (host, port, allowed or not).
"""
from __future__ import annotations

import asyncio
import os
import sys


def parse_allow(raw: str) -> list[tuple[str, int]]:
    out = []
    for item in (raw or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        host, _, port = item.rpartition(":") if item.count(":") == 1 else (item, "", "")
        out.append((host or item, int(port) if port.isdigit() else 443))
    return out


def allowed(rules: list[tuple[str, int]], host: str, port: int) -> bool:
    host = host.lower().rstrip(".")
    for pat, p in rules:
        if p != port:
            continue
        if pat == host or (pat.startswith("*.") and host.endswith(pat[1:])):
            return True
    return False


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


def make_handler(rules: list[tuple[str, int]]):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        except Exception:  # noqa: BLE001
            writer.close()
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(line) < 2 or line[0].upper() != "CONNECT":
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port_s = line[1].rpartition(":")
        port = int(port_s) if port_s.isdigit() else 443
        ok = allowed(rules, host, port)
        print(f"[sokkan-egress] CONNECT {host}:{port} {'allowed' if ok else 'DENIED'}",
              file=sys.stderr, flush=True)
        if not ok:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except Exception:  # noqa: BLE001
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
    return handle


async def amain() -> None:
    rules = parse_allow(os.environ.get("SOKKAN_EGRESS_ALLOW", "api.anthropic.com"))
    port = int(os.environ.get("SOKKAN_EGRESS_PORT") or 3128)
    server = await asyncio.start_server(make_handler(rules), "0.0.0.0", port)
    print(f"[sokkan-egress] :{port} allow={rules}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(amain())
