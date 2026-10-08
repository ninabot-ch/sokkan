"""A tiny in-process Kubernetes API for Secrets (the kubernetes secrets provider's contract
test): GET/POST/PUT/PATCH(merge)/list with labelSelector, resourceVersion conflicts (409),
bearer token checked. Not a cluster: the RBAC denial path is a token the server refuses."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_PATH = re.compile(r"^/api/v1/namespaces/([^/]+)/secrets(?:/([^/]+))?$")


class FakeK8s:
    def __init__(self, token: str = "good-token", bind: str = "127.0.0.1"):
        self.token = token
        self.reviews: list[str] = []
        self.sa_tokens: dict[str, tuple[str, str]] = {}
        self.secrets: dict[tuple[str, str], dict] = {}
        self.rv = 0
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _body(self):
                n = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _handle(self, method):
                if self.headers.get("authorization") != f"Bearer {fake.token}":
                    return self._send(403, {"message": "forbidden"})
                u = urlsplit(self.path)
                if method == "POST" and u.path == "/apis/authentication.k8s.io/v1/tokenreviews":
                    b = self._body()
                    jwt = (b.get("spec") or {}).get("token", "")
                    fake.reviews.append(jwt)
                    who = fake.sa_tokens.get(jwt)
                    st = {"authenticated": False}
                    if who:
                        st = {"authenticated": True, "user": {
                            "username": f"system:serviceaccount:{who[0]}:{who[1]}",
                            "uid": "uid-" + who[1],
                            "groups": ["system:serviceaccounts", f"system:serviceaccounts:{who[0]}"]}}
                    return self._send(201, {"apiVersion": "authentication.k8s.io/v1",
                                            "kind": "TokenReview", "status": st})
                m = _PATH.match(u.path)
                if not m:
                    return self._send(404, {"message": "not found"})
                ns, name = m.group(1), m.group(2)
                with fake.lock:
                    if method == "GET" and name is None:
                        sel = parse_qs(u.query).get("labelSelector", [""])[0]
                        want = dict(x.split("=", 1) for x in sel.split(",") if x)
                        items = [s for (n2, _), s in fake.secrets.items() if n2 == ns and all(
                            (s["metadata"].get("labels") or {}).get(k) == v for k, v in want.items())]
                        return self._send(200, {"items": items})
                    key = (ns, name)
                    if method == "GET":
                        s = fake.secrets.get(key)
                        return self._send(200, s) if s else self._send(404, {"message": "nf"})
                    if method == "POST":
                        b = self._body()
                        key = (ns, b["metadata"]["name"])
                        if key in fake.secrets:
                            return self._send(409, {"message": "exists"})
                        fake.rv += 1
                        b["metadata"]["resourceVersion"] = str(fake.rv)
                        fake.secrets[key] = b
                        return self._send(201, b)
                    s = fake.secrets.get(key)
                    if s is None:
                        return self._send(404, {"message": "nf"})
                    b = self._body()
                    if method == "PUT":
                        if b["metadata"].get("resourceVersion") != s["metadata"]["resourceVersion"]:
                            return self._send(409, {"message": "conflict"})
                        s = b
                    elif method == "PATCH":
                        data = dict(s.get("data") or {})
                        for k, v in (b.get("data") or {}).items():
                            if v is None:
                                data.pop(k, None)
                            else:
                                data[k] = v
                        s = {**s, "data": data}
                    fake.rv += 1
                    s["metadata"]["resourceVersion"] = str(fake.rv)
                    fake.secrets[key] = s
                    return self._send(200, s)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_PATCH(self):
                self._handle("PATCH")

        self.srv = ThreadingHTTPServer((bind, 0), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def sa_jwt(self, namespace: str, name: str) -> str:
        """A ServiceAccount token as the kubelet mounts it (claims only: the fake TokenReview,
        like the real one, is what vouches for it)."""
        import base64
        import os as _os

        def b(d):
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        claims = {"iss": "kubernetes/serviceaccount",
                  "kubernetes.io/serviceaccount/namespace": namespace,
                  "kubernetes.io/serviceaccount/service-account.name": name,
                  "kubernetes.io/serviceaccount/service-account.uid": "uid-" + name,
                  "kubernetes.io/serviceaccount/secret.name": name + "-token",
                  "sub": f"system:serviceaccount:{namespace}:{name}"}
        sig = base64.urlsafe_b64encode(_os.urandom(64)).rstrip(b"=").decode()
        jwt = f"{b({'alg': 'RS256', 'typ': 'JWT', 'kid': 'k'})}.{b(claims)}.{sig}"
        self.sa_tokens[jwt] = (namespace, name)
        return jwt

    def close(self):
        self.srv.shutdown()
