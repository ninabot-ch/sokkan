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
* ``SOKKAN_FORGE_API_URL`` — where the helper asks (loopback).

The helper asks ``POST /api/forge/git-credential`` at each git operation that needs
credentials; git keeps the answer in memory for that one command. Nothing is written to
disk, nothing goes to the model's context. `erase` (git got 401/403 with it) makes the API
re-read the person's access at once.

Residual risk (documented in docs/MULTIUSER.md): a session can run ``git credential fill``
itself and print the token — it is the person's own token, scoped to read/write the
repositories, valid ≤ 2 h; the sandbox (lot 8) and egress filtering reduce it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from urllib.parse import urlparse

import projects
from forge import access, links

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_credential_helper.py")


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
    good = hmac.new(links.mac_key(), f"{sid}|{email}".encode(), hashlib.sha256).hexdigest()
    return (sid, email) if hmac.compare_digest(good, sig) else None


def _hosts(project: str) -> list[str]:
    return sorted({links.norm_url(r.base_url) for r in access.repos(project)})


def session_env(sid: str, email: str, project: str | None,
                base_env: dict | None = None) -> dict[str, str]:
    """Environment additions of a session (nothing when the project is not a forge
    project or the feature is off). ``base_env`` = the environment it is merged into (an
    existing GIT_CONFIG_COUNT is continued, not overwritten)."""
    p = projects.get(project) if project else None
    if not email or p is None or p["access_source"] != "forge" or not access.enabled():
        return {}
    base_env = os.environ if base_env is None else base_env
    try:
        n = int(base_env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    py = os.environ.get("SOKKAN_PYTHON", sys.executable)
    env: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for host in _hosts(project):
        pairs.append((f"credential.{host}.helper", ""))           # reset: no store/cache
        pairs.append((f"credential.{host}.helper", f"!'{py}' '{HELPER}'"))
        if links.ca_bundle() and host == links.norm_url(links.gitlab_url()):
            pairs.append((f"http.{host}/.sslCAInfo", links.ca_bundle()))
    for i, (k, v) in enumerate(pairs, start=n):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    env["GIT_CONFIG_COUNT"] = str(n + len(pairs))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["SOKKAN_FORGE_TICKET"] = ticket(sid, email)
    env["SOKKAN_FORGE_API_URL"] = api_url()
    return env


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
        return {}, f"no {repo.provider} account linked (Profile → Linked accounts)"
    user, password = links.provider_for(repo.provider, repo.base_url).git_credentials(tok)
    return {"username": user, "password": password}, "ok"


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
