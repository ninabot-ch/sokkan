"""Git credentials of a session = the PERSON's forge token, never a technical account.

A session of a ``forge`` project gets, in its environment, NO token: only

* git configuration through ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``
  — for each forge host of the project: the credential helper list RESET (so no
  ``store``/``cache`` helper of the user's git config ever receives the token) then
  ``git_credential_helper.py``; ``http.<host>.sslCAInfo`` for a self-hosted GitLab with an
  internal CA;
* ``GIT_TERMINAL_PROMPT=0`` (no prompt: without a linked account, a push fails at once);
* ``SOKKAN_FORGE_TICKET`` — an HMAC ticket that names the session and its person (not a
  token: it only lets the helper ask the API, on the loopback, for the person's current
  token while the person still has access to the project);
* where the helper asks — one of three transports, chosen by the session's runner:

  - ``loopback`` (local runner): ``SOKKAN_FORGE_API_URL``, ``POST /api/forge/git-credential``
    on the api's loopback;
  - ``relay`` (docker / kubernetes runner): the pod cannot reach the api's loopback; the
    helper (``sokkan-git-credential`` in the session image) asks through the authenticated
    MCP relay the runner already set up (``SOKKAN_RELAY_ADDR`` + ``SOKKAN_RELAY_TOKEN``) — the
    api knows which session a relay token belongs to and refuses a ticket of another one;
  - ``socket`` (local runner, Bash inside bubblewrap without network): a per-session Unix
    socket of the api, bound into the sandbox at ``/run/sokkan/forge.sock``; the same socket
    carries the git traffic to the project's forge hosts only (allow-list) through a
    forwarder the wrapper starts inside the sandbox — no general network is opened.

The helper asks at each git operation that needs credentials; git keeps the answer in memory for that one command. Nothing is written to
disk, nothing goes to the model's context. `erase` (git got 401/403 with it) makes the API
re-read the person's access at once.

Residual risk (documented in docs/MULTIUSER.md): a session can run ``git credential fill``
itself and print the token — it is the person's own token, scoped to read/write the
repositories, valid ≤ 2 h; the sandbox (lot 8) and egress filtering reduce it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
from typing import Callable
from urllib.parse import urlparse

import projects
from forge import access, links

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_credential_helper.py")
# the helper as installed in the session image (docker/session.Dockerfile)
POD_HELPER = "sokkan-git-credential"
# inside the bubblewrap sandbox (sandbox.bwrap_argv binds them there)
SANDBOX_SOCKET = "/run/sokkan/forge.sock"
SANDBOX_HELPER = "/run/sokkan/git-credential-helper"
SANDBOX_FORWARD_PORT = 47391     # the in-sandbox forwarder (private network namespace)
TRANSPORTS = ("loopback", "relay", "socket")

# email of the person of a LIVE session (None = none); set by forge.routes.router
LIVE_USER: Callable[[str], str | None] | None = None


def api_url() -> str:
    return os.environ.get("SOKKAN_FORGE_API_URL") or \
        f"http://127.0.0.1:{os.environ.get('SOKKAN_API_PORT', '8097')}"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).rstrip(b"=").decode()


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def ticket(sid: str, email: str) -> str:
    msg = f"{sid}|{email.lower().strip()}"
    sig = hmac.new(links.mac_key(), msg.encode(), hashlib.sha256).hexdigest()
    return f"v1.{_b64(sid)}.{_b64(email.lower().strip())}.{sig}"


def verify(t: str) -> tuple[str, str] | None:
    try:
        v, sid_b, email_b, sig = (t or "").split(".")
        if v != "v1":
            return None
        sid, email = _unb64(sid_b), _unb64(email_b)
    except (ValueError, UnicodeDecodeError):
        return None
    msg = f"{sid}|{email}".encode()
    for k in links.mac_keys():  # a forge data-key rotation in progress: old tickets still verify
        if hmac.compare_digest(hmac.new(k, msg, hashlib.sha256).hexdigest(), sig):
            return sid, email
    return None


def _hosts(project: str) -> list[str]:
    return sorted({links.norm_url(r.base_url) for r in access.repos(project)})


def session_env(sid: str, email: str, project: str | None,
                base_env: dict | None = None, transport: str = "loopback",
                network: bool = False) -> dict[str, str]:
    """Environment additions of a session (nothing when the project is not a forge
    project or the feature is off). ``base_env`` = the environment it is merged into (an
    existing GIT_CONFIG_COUNT is continued, not overwritten). ``transport`` = how the helper
    reaches the api (see the module doc); ``network`` (socket only) = the sandbox shares the
    host network, the git traffic needs no forwarder."""
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r}")
    p = projects.get(project) if project else None
    if not email or p is None or p["access_source"] != "forge" or not access.enabled():
        return {}
    base_env = os.environ if base_env is None else base_env
    try:
        n = int(base_env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    if transport == "relay":
        helper = f"!{os.environ.get('SOKKAN_FORGE_POD_HELPER') or POD_HELPER}"
    elif transport == "socket":
        helper = f"!{_sandbox_python()} {SANDBOX_HELPER}"
    else:
        py = os.environ.get("SOKKAN_PYTHON", sys.executable)
        helper = f"!'{py}' '{HELPER}'"
    env: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for host in _hosts(project):
        pairs.append((f"credential.{host}.helper", ""))           # reset: no store/cache
        pairs.append((f"credential.{host}.helper", helper))
        if links.ca_bundle() and host == links.norm_url(links.gitlab_url()):
            pairs.append((f"http.{host}/.sslCAInfo", links.ca_bundle()))
        if transport == "socket" and not network:
            # no network in the sandbox: git reaches THIS forge through the forwarder
            pairs.append((f"http.{host}/.proxy", f"http://127.0.0.1:{SANDBOX_FORWARD_PORT}"))
    for i, (k, v) in enumerate(pairs, start=n):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    env["GIT_CONFIG_COUNT"] = str(n + len(pairs))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["SOKKAN_FORGE_TICKET"] = ticket(sid, email)
    if transport == "loopback":
        env["SOKKAN_FORGE_API_URL"] = api_url()
    elif transport == "socket":
        env["SOKKAN_FORGE_SOCKET"] = SANDBOX_SOCKET
        if not network:
            env["SOKKAN_FORGE_FORWARD"] = str(SANDBOX_FORWARD_PORT)
    return env


def _sandbox_python() -> str:
    """A python3 under /usr (the sandbox sees /usr, not the api's venv)."""
    for p in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(p):
            return p
    return "python3"


def credential(t: str, protocol: str, host: str, path: str = "",
               live_user=None) -> tuple[dict, str]:
    """→ (git credential attributes or {}, reason). ``live_user(sid)`` returns the email
    of the person who owns the LIVE session (None = no live session)."""
    who = verify(t)
    if who is None:
        return {}, "invalid ticket"
    sid, email = who
    if live_user is not None and (live_user(sid) or "").lower().strip() != email:
        return {}, "no live session of this person"
    import board
    project = board.get_session_project(sid)
    p = projects.get(project) if project else None
    if p is None or p["access_source"] != "forge" or not access.enabled():
        return {}, "not a forge project"
    role = projects.effective_role({"email": email, "role": "none"}, project)
    if role is None:
        return {}, "no access to the project"
    want = links.norm_url(f"{protocol}://{host}")
    repo = next((r for r in access.repos(project) if _origin(r.base_url) == want), None)
    if repo is None:
        return {}, "host is not a forge of the project"
    tok = links.access_token(email, repo.provider, repo.base_url)
    if not tok:
        return {}, f"no {repo.provider} account linked (Setup › My account › Linked accounts)"
    user, password = links.provider_for(repo.provider, repo.base_url).git_credentials(tok)
    return {"username": user, "password": password}, "ok"


def answer(body: dict, *, live_user=None, bound_sid: str | None = None) -> dict:
    """One helper request (``{ticket, action, protocol, host, path}``) → its JSON answer.
    Shared by the loopback route, the runner relay and the sandbox socket. ``bound_sid`` =
    the session the CHANNEL belongs to (relay token / per-session socket, fixed by the
    api): a ticket of any other session is refused (a replayed or stolen ticket)."""
    from forge import ForgeError, ForgeUnauthorized
    t = str(body.get("ticket") or "")
    live_user = live_user or LIVE_USER
    if bound_sid is not None:
        who = verify(t)
        if who is None:
            return {"reason": "invalid ticket"}
        if not hmac.compare_digest(who[0], bound_sid):
            return {"reason": "ticket of another session"}
    if body.get("action") == "erase":
        try:
            erase(t)
        except ForgeError:
            pass
        return {"ok": True}
    try:
        cred, reason = credential(t, str(body.get("protocol") or ""),
                                  str(body.get("host") or ""), str(body.get("path") or ""),
                                  live_user=live_user)
    except ForgeUnauthorized:
        cred, reason = {}, "forge refused the token"
    except ForgeError as e:
        cred, reason = {}, f"forge unavailable ({type(e).__name__})"
    return cred or {"reason": reason}


def erase(t: str) -> None:
    """git was refused with the token: re-read the person's access now."""
    who = verify(t)
    if who is None:
        return
    sid, email = who
    import board
    project = board.get_session_project(sid)
    if project:
        access.resolve(email, project, force=True)


def _origin(url: str) -> str:
    """scheme://host[:port] of a forge base URL (a GitLab under a path prefix included)."""
    u = urlparse(links.norm_url(url))
    return f"{u.scheme}://{u.netloc}"


# ---- per-session Unix socket (Bash inside bubblewrap) ---------------------------------
_socks: dict[str, tuple[asyncio.base_events.Server, str]] = {}
_sock_dir: str | None = None


def _socket_dir() -> str:
    """A private (0700) short directory: AF_UNIX paths are limited to 107 bytes."""
    global _sock_dir
    if _sock_dir is None or not os.path.isdir(_sock_dir):
        base = os.environ.get("SOKKAN_FORGE_SOCKET_DIR") or None
        if base:
            os.makedirs(base, mode=0o700, exist_ok=True)
        _sock_dir = tempfile.mkdtemp(prefix="sokkan-forge-", dir=base)
        os.chmod(_sock_dir, 0o700)
    return _sock_dir


def socket_path(sid: str) -> str | None:
    e = _socks.get(sid)
    return e[1] if e else None


def allowed_origins(sid: str) -> set[tuple[str, int]]:
    """(host, port) of the forge hosts of a session's project — the only places the
    sandbox socket tunnels to."""
    import board
    project = board.get_session_project(sid)
    out: set[tuple[str, int]] = set()
    for r in access.repos(project) if project else []:
        u = urlparse(_origin(r.base_url))
        if u.hostname:
            out.add((u.hostname.lower(), u.port or (443 if u.scheme == "https" else 80)))
    return out


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def _tunnel(sid: str, first: bytes, reader, writer) -> None:
    """An HTTP proxy request from the in-sandbox forwarder: CONNECT host:port (https) or an
    absolute-form request (http) — to a forge host of the session's project only."""
    try:
        method, target, _ = first.decode("latin-1").split(" ", 2)
    except ValueError:
        writer.close()
        return
    headers = [first]
    while True:                       # the rest of the request head
        line = await asyncio.wait_for(reader.readline(), 15)
        headers.append(line)
        if line in (b"\r\n", b"\n", b""):
            break
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        host, port_i = host.strip("[]").lower(), int(port) if port.isdigit() else 443
    else:
        u = urlparse(target)
        host, port_i = (u.hostname or "").lower(), u.port or 80
        if u.scheme != "http":
            host = ""
    allowed = await asyncio.to_thread(allowed_origins, sid)
    if (host, port_i) not in allowed:
        print(f"[sokkan] forge socket {sid[:8]}: refused tunnel to {host}:{port_i}",
              file=sys.stderr)
        writer.write(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    try:
        ur, uw = await asyncio.open_connection(host, port_i)
    except OSError:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    if method.upper() == "CONNECT":
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
    else:
        uw.write(b"".join(headers))
        await uw.drain()
    await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))


def _socket_handler(sid: str):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.readline(), 15)
        except Exception:  # noqa: BLE001
            writer.close()
            return
        if first.lstrip().startswith(b"{"):        # a credential request (one JSON line)
            try:
                body = json.loads(first)
                ans = await asyncio.to_thread(answer, body, bound_sid=sid)
            except Exception as e:  # noqa: BLE001
                ans = {"reason": f"bad request ({type(e).__name__})"}
            writer.write((json.dumps(ans) + "\n").encode())
            try:
                await writer.drain()
            finally:
                writer.close()
            return
        await _tunnel(sid, first, reader, writer)
    return handle


async def open_socket(sid: str) -> str:
    """Start (once) the session's Unix socket; → its path on the host. Whoever can connect
    to it IS this session (the api binds it into this session's sandbox only)."""
    if sid in _socks:
        return _socks[sid][1]
    path = os.path.join(_socket_dir(), f"{hashlib.sha256(sid.encode()).hexdigest()[:16]}.sock")
    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(_socket_handler(sid), path=path)
    os.chmod(path, 0o600)
    _socks[sid] = (server, path)
    return path


async def close_socket(sid: str) -> None:
    e = _socks.pop(sid, None)
    if e is None:
        return
    e[0].close()
    try:
        os.unlink(e[1])
    except OSError:
        pass
