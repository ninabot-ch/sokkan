"""A fake GitLab for the lot 5 tests — no real GitLab is ever called.

Serves, on 127.0.0.1:<random port>:
* OAuth 2: ``/oauth/token`` (authorization_code with PKCE S256 check, refresh_token with
  ROTATION like GitLab: a refresh token works once), ``/oauth/revoke``;
* API v4: ``/api/v4/user``, ``/api/v4/projects/:id/members/all/:user_id`` (level from
  ``members``), ``/api/v4/projects/:id/protected_branches``;
* git smart HTTP through the real ``git http-backend`` for bare repos under ``repos_dir``:
  Basic auth whose password must be a live access token; read needs level ≥ 10, push ≥ 30;
  a pre-receive hook refuses a push to the protected ``main`` below Maintainer (40) and
  records the push options (``-o merge_request.create`` …) in ``push_options.log``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

PRE_RECEIVE = """#!/bin/sh
i=0
while [ "$i" -lt "${GIT_PUSH_OPTION_COUNT:-0}" ]; do
  eval "echo \\"\\$GIT_PUSH_OPTION_$i\\"" >> "$GIT_DIR/push_options.log"
  i=$((i+1))
done
while read old new ref; do
  if [ "$ref" = "refs/heads/main" ] && [ "${FAKE_LEVEL:-0}" -lt 40 ]; then
    echo "GitLab: You are not allowed to push code to protected branches on this project." >&2
    exit 1
  fi
done
exit 0
"""


class FakeGitLab:
    def __init__(self, repos_dir: Path, client_id: str = "cid", client_secret: str = "csecret",
                 host: str = "127.0.0.1"):
        self.repos_dir = Path(repos_dir)
        self.client_id, self.client_secret = client_id, client_secret
        self.users: dict[str, dict] = {}          # uid → {username, state}
        self.members: dict[tuple[str, str], int] = {}   # (project path, uid) → level
        self.protected: dict[str, list[str]] = {}
        self.codes: dict[str, tuple[str, str]] = {}     # code → (uid, challenge)
        self.access: dict[str, tuple[str, float]] = {}  # token → (uid, expires)
        self.refresh: dict[str, str] = {}               # refresh token → uid
        self.revoked: list[str] = []
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, headers)
        self.ttl = 7200
        self.down = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, obj=None, headers=None, raw: bytes | None = None):
                body = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                for k, v in (headers or {"content-type": "application/json"}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                n = int(self.headers.get("content-length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                fake._handle(self, "GET")

            def do_POST(self):
                fake._handle(self, "POST")

        self.server = ThreadingHTTPServer((host, 0), H)
        self.url = f"http://{host}:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ---- lifecycle / setup --------------------------------------------------------------
    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def add_user(self, uid: str, username: str):
        self.users[uid] = {"username": username, "state": "active"}

    def set_level(self, path: str, uid: str, level: int | None):
        if level is None:
            self.members.pop((path, uid), None)
        else:
            self.members[(path, uid)] = level

    def add_repo(self, path: str):
        bare = self.repos_dir / f"{path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True)
        subprocess.run(["git", "-C", str(bare), "config", "receive.advertisePushOptions", "true"],
                       check=True)
        hook = bare / "hooks" / "pre-receive"
        hook.write_text(PRE_RECEIVE)
        hook.chmod(0o755)
        self.protected[path] = ["main"]
        return bare

    def consent(self, uid: str, code_challenge: str) -> str:
        """The person clicks "Authorize" on GitLab: a code bound to the PKCE challenge."""
        code = secrets.token_urlsafe(16)
        self.codes[code] = (uid, code_challenge)
        return code

    def tokens_of(self, uid: str) -> list[str]:
        return [t for t, (u, _e) in self.access.items() if u == uid]

    def expire_tokens(self, uid: str):
        for t, (u, _e) in list(self.access.items()):
            if u == uid:
                self.access[t] = (u, time.time() - 1)

    # ---- request handling ---------------------------------------------------------------
    def _issue(self, uid: str) -> dict:
        at, rt = "glat-" + secrets.token_hex(16), "glrt-" + secrets.token_hex(16)
        self.access[at] = (uid, time.time() + self.ttl)
        self.refresh[rt] = uid
        return {"access_token": at, "token_type": "Bearer", "expires_in": self.ttl,
                "refresh_token": rt, "created_at": int(time.time()),
                "scope": "read_user read_api read_repository write_repository"}

    def _uid_of(self, token: str) -> str | None:
        v = self.access.get(token)
        if not v or v[1] < time.time() or token in self.revoked:
            return None
        uid = v[0]
        return uid if self.users.get(uid, {}).get("state") == "active" else None

    def _handle(self, h, method: str):
        u = urlparse(h.path)
        self.requests.append((method, u.path, dict(h.headers)))
        if self.down:
            return h._send(503, {"message": "down"})
        if u.path.startswith("/oauth/"):
            return self._oauth(h, u.path, parse_qs(h._body().decode()))
        if u.path.startswith("/api/v4/"):
            tok = (h.headers.get("authorization") or "").removeprefix("Bearer ")
            uid = self._uid_of(tok)
            if uid is None:
                return h._send(401, {"message": "401 Unauthorized"})
            return self._api(h, u.path[len("/api/v4"):], uid)
        return self._git(h, method, u)

    def _oauth(self, h, path, form):
        f = {k: v[0] for k, v in form.items()}
        if f.get("client_id") != self.client_id or f.get("client_secret") != self.client_secret:
            return h._send(401, {"error": "invalid_client"})
        if path == "/oauth/revoke":
            self.revoked.append(f.get("token", ""))
            return h._send(200, {})
        if f.get("grant_type") == "authorization_code":
            got = self.codes.pop(f.get("code", ""), None)
            if not got:
                return h._send(400, {"error": "invalid_grant"})
            uid, challenge = got
            v = f.get("code_verifier", "")
            calc = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
            if calc != challenge:
                return h._send(400, {"error": "invalid_grant", "error_description": "PKCE"})
            return h._send(200, self._issue(uid))
        if f.get("grant_type") == "refresh_token":
            uid = self.refresh.pop(f.get("refresh_token", ""), None)   # rotation: single use
            if uid is None or self.users.get(uid, {}).get("state") != "active":
                return h._send(400, {"error": "invalid_grant"})
            return h._send(200, self._issue(uid))
        return h._send(400, {"error": "unsupported_grant_type"})

    def _api(self, h, path, uid):
        if path == "/user":
            return h._send(200, {"id": int(uid), "username": self.users[uid]["username"],
                                 "name": self.users[uid]["username"].title(), "state": "active"})
        parts = path.split("/")
        # /projects/<id>/members/all/<uid>  |  /projects/<id>/protected_branches
        if len(parts) >= 4 and parts[1] == "projects":
            proj = unquote(parts[2])
            if parts[3:5] == ["members", "all"] and len(parts) == 6:
                lv = self.members.get((proj, parts[5]))
                if lv is None:
                    return h._send(404, {"message": "404 Not found"})
                return h._send(200, {"id": int(parts[5]), "access_level": lv, "state": "active"})
            if parts[3] == "protected_branches":
                if (proj, uid) not in self.members:
                    return h._send(404, {"message": "404 Project Not Found"})
                return h._send(200, [{"name": b} for b in self.protected.get(proj, [])])
        return h._send(404, {"message": "404 Not found"})

    def _git(self, h, method, u):
        auth = h.headers.get("authorization") or ""
        uid = None
        if auth.startswith("Basic "):
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            uid = self._uid_of(pw)
        if uid is None:
            return h._send(401, {}, headers={"WWW-Authenticate": 'Basic realm="GitLab"',
                                             "content-type": "text/plain"})
        repo = u.path.split(".git/")[0].lstrip("/")
        level = self.members.get((repo, uid), 0)
        push = "git-receive-pack" in (u.path + "?" + u.query)
        if level < (30 if push else 10):
            return h._send(403, {}, headers={"content-type": "text/plain"},
                           raw=b"You are not allowed to push code to this project.\n")
        env = {**os.environ, "GIT_PROJECT_ROOT": str(self.repos_dir), "GIT_HTTP_EXPORT_ALL": "1",
               "PATH_INFO": u.path, "QUERY_STRING": u.query, "REQUEST_METHOD": method,
               "CONTENT_TYPE": h.headers.get("content-type") or "",
               "REMOTE_USER": self.users[uid]["username"], "REMOTE_ADDR": "127.0.0.1",
               "GIT_PROTOCOL": h.headers.get("git-protocol") or "",
               "FAKE_LEVEL": str(level)}
        body = h._body() if method == "POST" else b""
        if h.headers.get("content-encoding") == "gzip":
            import gzip
            body = gzip.decompress(body)
        env["CONTENT_LENGTH"] = str(len(body))
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, {}
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            elif k:
                headers[k] = v.strip()
        return h._send(status, raw=payload, headers=headers)
