"""Egress filter of the kernel engine's sandboxes (docker-compose.kernel.yml, service
`kernel-egress-filter`). Standard library only: it runs in the kernel's `sandboxd` image.

A sandbox sits on an INTERNAL network where the only thing it reaches is this filter (the socket
proxy sets HTTP(S)_PROXY to it in every sandbox). The filter decides WHICH names may be reached,
then hands the request to the kernel's own egress proxy (`kernel-egress`), which resolves the
name and refuses every private, loopback, link-local or docker address — the filter never
connects anywhere but there.

    SOKKAN_KERNEL_SANDBOX_NET   none        every request refused (the default: an Atelier
                                            session needs no network; the kernel also starts
                                            its sandboxes with no network at all)
                                registries  only the names of SOKKAN_KERNEL_EGRESS_ALLOW
                                            (package registries by default), ports 80 and 443
                                open        any name (still public addresses only, upstream)
    SOKKAN_KERNEL_EGRESS_ALLOW  comma list of names; `pypi.org` allows pypi.org and its
                                sub-domains. An IP literal is never allowed in `registries`.
    SOKKAN_KERNEL_EGRESS_UPSTREAM  http://kernel-egress:3128

Accepted forms: `CONNECT host:port` (https) and an absolute-URI request (`GET http://host/…`,
plain http). Anything else: 400. Refused: 403, with the reason in the body.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import sys
from urllib.parse import urlsplit

log = logging.getLogger("egress-filter")

DEFAULT_ALLOW = (
    "pypi.org,files.pythonhosted.org,"             # pip
    "registry.npmjs.org,registry.yarnpkg.com,"     # npm, yarn
    "proxy.golang.org,sum.golang.org,"             # go modules
    "index.crates.io,static.crates.io,"            # cargo
    "repo.maven.apache.org,repo1.maven.org,"       # maven
    "rubygems.org,"                                # gem
    "deb.debian.org,security.debian.org"           # apt in a debian-based sandbox
)
PORTS = {80, 443}
MAX_HEAD = 64 * 1024


def allow_list(raw: str | None) -> tuple[str, ...]:
    raw = DEFAULT_ALLOW if raw is None or not raw.strip() else raw
    return tuple(sorted({h.strip().lower().strip(".") for h in raw.split(",") if h.strip()}))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def decide(mode: str, allow: tuple[str, ...], host: str, port: int) -> str | None:
    """None = allowed, else the reason of the refusal."""
    host = (host or "").lower().strip().rstrip(".")
    if not host:
        return "no destination"
    if mode == "open":
        return None
    if mode != "registries":
        return "this instance gives its sandboxes no network (SOKKAN_KERNEL_SANDBOX_NET=none)"
    if _is_ip(host):
        return f"{host}: an address, not a name — only package registries are reachable"
    if port not in PORTS:
        return f"{host}:{port}: only ports 80 and 443 are reachable"
    if any(host == a or host.endswith("." + a) for a in allow):
        return None
    return f"{host}: not a package registry of this instance (SOKKAN_KERNEL_EGRESS_ALLOW)"


def target(request_line: str) -> tuple[str, str, int]:
    """(method, host, port) of a proxy request line; ValueError if it is not one."""
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ValueError("not an HTTP/1.x request line")
    method, uri = parts[0].upper(), parts[1]
    if method == "CONNECT":
        host, _, port = uri.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("CONNECT host:port expected")
        return method, host.strip("[]"), int(port)
    u = urlsplit(uri)
    if u.scheme != "http" or not u.hostname:
        raise ValueError("absolute http:// URI expected")
    return method, u.hostname, u.port or 80


def one_request(head: bytes) -> tuple[bytes, int]:
    """The head of a plain-http request with `Connection: close` (any connection header of the
    client dropped), and the length of its body. A chunked body: ValueError."""
    lines = head.decode("latin-1").split("\r\n")
    out, length = [lines[0]], 0
    for ln in lines[1:]:
        if not ln:
            continue
        name, _, value = ln.partition(":")
        n = name.strip().lower()
        if n == "transfer-encoding":
            raise ValueError("chunked request bodies are not accepted")
        if n == "content-length":
            if not value.strip().isdigit():
                raise ValueError("bad Content-Length")
            length = int(value.strip())
        if n in ("connection", "proxy-connection", "keep-alive"):
            continue
        out.append(ln)
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1"), length


class Filter:
    def __init__(self, mode: str, allow: tuple[str, ...], upstream: str):
        self.mode, self.allow = mode, allow
        u = urlsplit(upstream)
        if not u.hostname or not u.port:
            raise SystemExit(f"egress filter: upstream {upstream!r} must be http://host:port")
        self.up_host, self.up_port = u.hostname, u.port

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            try:
                method, host, port = target(line)
            except ValueError as e:
                return await _answer(writer, 400, str(e))
            why = decide(self.mode, self.allow, host, port)
            if why:
                log.warning("refused %s %s:%s — %s", method, host, port, why)
                return await _answer(writer, 403, why)
            log.info("%s %s:%s", method, host, port)
            if method == "CONNECT":
                ur, uw = await asyncio.open_connection(self.up_host, self.up_port)
                uw.write(head)      # same request, to the kernel's egress (it resolves and checks)
                await uw.drain()
                await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
                return
            # plain http: ONE request per connection (a kept-alive connection could carry a
            # second request to another name, never checked) — its body by length only
            try:
                head, length = one_request(head)
            except ValueError as e:
                return await _answer(writer, 400, str(e))
            body = await reader.readexactly(length) if length else b""
            ur, uw = await asyncio.open_connection(self.up_host, self.up_port)
            uw.write(head + body)
            await uw.drain()
            await _pipe(ur, writer)
            uw.close()
        except (ConnectionError, OSError) as e:
            await _answer(writer, 502, f"egress unreachable: {type(e).__name__}")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


async def _answer(writer, status: int, msg: str) -> None:
    body = f"sokkan egress filter: {msg}\n".encode()
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(status, "Error")
    try:
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, RuntimeError):
            pass


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mode = (os.environ.get("SOKKAN_KERNEL_SANDBOX_NET") or "none").strip().lower()
    if mode not in ("none", "registries", "open"):
        raise SystemExit(f"egress filter: SOKKAN_KERNEL_SANDBOX_NET={mode!r} (none | registries | open)")
    allow = allow_list(os.environ.get("SOKKAN_KERNEL_EGRESS_ALLOW"))
    f = Filter(mode, allow, os.environ.get("SOKKAN_KERNEL_EGRESS_UPSTREAM") or "http://kernel-egress:3128")
    server = await asyncio.start_server(f.handle, "0.0.0.0", int(os.environ.get("LISTEN_PORT", "3128")),
                                        limit=MAX_HEAD)
    log.info("mode %s%s → %s:%s", mode, f" ({len(allow)} names)" if mode == "registries" else "",
             f.up_host, f.up_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
