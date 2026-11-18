"""A simulated Microsoft Graph + Microsoft identity platform, on a real socket (no Microsoft
account, nothing leaves the machine). Enough of the API for backend/connectors/graph.py:
OAuth authorization code + PKCE + refresh, /me, mail (folders, conversations, replies,
drafts, send, shared mailboxes), calendar (calendarView, events, getSchedule), files
(OneDrive / a SharePoint site: children, search, content). Every request is recorded."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

USER = "alice@contoso.test"


class FakeGraph:
    def __init__(self):
        self.codes: dict[str, dict] = {}          # code → {challenge, redirect_uri, user}
        self.refresh: dict[str, str] = {}         # refresh → user
        self.access: dict[str, str] = {}          # access → user
        self.token_ttl = 3600
        self.seen: list[dict] = []
        self.sent: list[dict] = []
        self.boxes = {USER: self._seed_box(USER), "info@contoso.test": self._seed_box("info@contoso.test")}
        self.events = {USER: [
            {"id": "ev1", "subject": "Revue budget", "start": {"dateTime": "2030-01-07T09:00:00.0000000"},
             "end": {"dateTime": "2030-01-07T10:00:00.0000000"}, "location": {"displayName": "Salle 2"},
             "organizer": {"emailAddress": {"address": USER}}, "attendees": [
                 {"emailAddress": {"address": "bob@contoso.test"}}], "showAs": "busy",
             "body": {"contentType": "html", "content": "<p>Ordre du jour : <b>budget</b></p>"},
             "attachments": [{"name": "budget.xlsx", "size": 1200}]}]}
        self.files = {"/me/drive": {"Rapports/q4.txt": b"Chiffre Q4 : 1,2 MCHF", "notes.md": b"# Notes"},
                      "/sites/site-fin/drive": {"Shared/charte.txt": b"Charte financiere"}}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    @staticmethod
    def _seed_box(addr):
        return [
            {"id": f"{addr}-m1", "conversationId": "c1", "subject": "Demande de devis",
             "from": {"emailAddress": {"address": "client@fabrikam.test"}},
             "toRecipients": [{"emailAddress": {"address": addr}}], "ccRecipients": [],
             "receivedDateTime": "2030-01-06T08:00:00Z", "bodyPreview": "Pourriez-vous…",
             "body": {"contentType": "html", "content": "<p>Pourriez-vous nous envoyer un devis ?</p>"},
             "isDraft": False, "hasAttachments": False, "parentFolderId": "inbox"},
            {"id": f"{addr}-m2", "conversationId": "c2", "subject": "Facture",
             "from": {"emailAddress": {"address": "compta@fabrikam.test"}},
             "toRecipients": [{"emailAddress": {"address": addr}}], "ccRecipients": [],
             "receivedDateTime": "2030-01-05T08:00:00Z", "bodyPreview": "Ci-joint",
             "body": {"contentType": "text", "content": "Ci-joint la facture."},
             "isDraft": False, "hasAttachments": True, "parentFolderId": "inbox"}]

    # -- the browser part of the flow, played by the test
    def consent(self, authorize_url: str, user: str = USER) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                            "user": user, "scope": q["scope"]}
        return q["state"], code

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj=None, raw: bytes | None = None):
                body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
                self.send_response(status)
                self.send_header("content-type", "application/octet-stream" if raw is not None else "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("content-length") or 0))

            def _user(self):
                auth = self.headers.get("authorization", "")
                return fake.access.get(auth.removeprefix("Bearer "))

            def handle_one(self, method):
                u = urlparse(self.path)
                path, q = unquote(u.path), {k: v[0] for k, v in parse_qs(u.query).items()}
                raw = self._body() if method in ("POST", "PUT", "PATCH") else b""
                fake.seen.append({"method": method, "path": path, "query": q,
                                  "auth": self.headers.get("authorization", "")})
                if path.endswith("/oauth2/v2.0/token"):
                    return self.token({k: v[0] for k, v in parse_qs(raw.decode()).items()})
                user = self._user()
                if not user:
                    return self._send(401, {"error": {"code": "InvalidAuthenticationToken"}})
                try:
                    return fake.route(self, method, path, q, raw, user)
                except KeyError:
                    return self._send(404, {"error": {"code": "ItemNotFound"}})

            def token(self, f):
                if f.get("client_secret") != "graph-secret-for-tests":
                    return self._send(401, {"error": "invalid_client"})
                if f["grant_type"] == "authorization_code":
                    c = fake.codes.pop(f.get("code", ""), None)
                    if not c or c["redirect_uri"] != f.get("redirect_uri"):
                        return self._send(400, {"error": "invalid_grant"})
                    ch = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
                    if ch != c["challenge"]:
                        return self._send(400, {"error": "invalid_grant", "error_description": "PKCE"})
                    user = c["user"]
                elif f["grant_type"] == "refresh_token":
                    user = fake.refresh.get(f.get("refresh_token", ""))
                    if not user:
                        return self._send(400, {"error": "invalid_grant"})
                else:
                    return self._send(400, {"error": "unsupported_grant_type"})
                at, rt = "at-" + secrets.token_hex(8), "rt-" + secrets.token_hex(8)
                fake.access[at], fake.refresh[rt] = user, user
                return self._send(200, {"access_token": at, "refresh_token": rt, "expires_in": fake.token_ttl,
                                        "scope": f.get("scope", "")})

            def do_GET(self):
                self.handle_one("GET")

            def do_POST(self):
                self.handle_one("POST")

            def do_PUT(self):
                self.handle_one("PUT")

            def do_PATCH(self):
                self.handle_one("PATCH")

        return H

    def _box(self, path, user):
        m = re.match(r"^/v1\.0/(me|users/([^/]+))(/.*)$", path)
        if not m:
            raise KeyError(path)
        addr = user if m.group(1) == "me" else m.group(2)
        if addr != user and addr != "info@contoso.test":
            raise PermissionError
        return addr, m.group(3)

    def route(self, h, method, path, q, raw, user):
        if path == "/v1.0/me":
            return h._send(200, {"mail": user, "userPrincipalName": user})
        if path.startswith("/v1.0/sites/"):
            return self.files_route(h, method, path, q, raw)
        try:
            addr, rest = self._box(path, user)
        except PermissionError:
            return h._send(403, {"error": {"code": "ErrorAccessDenied"}})
        box = self.boxes.setdefault(addr, [])
        m = re.match(r"^/mailFolders/([^/]+)/messages$", rest)
        if m and method == "GET":
            f = m.group(1).lower()
            msgs = [x for x in box if x.get("parentFolderId") == f]
            if "$search" in q:
                s = q["$search"].strip('"').lower()
                msgs = [x for x in msgs if s in (x["subject"] + x["body"]["content"]).lower()]
            return h._send(200, {"value": msgs[: int(q.get("$top", 50))]})
        if rest == "/messages" and method == "GET":
            cid = re.search(r"conversationId eq '([^']+)'", q.get("$filter", "")).group(1)
            return h._send(200, {"value": [x for x in box if x["conversationId"] == cid]})
        if rest == "/messages" and method == "POST":
            j = json.loads(raw)
            d = {"id": "draft-" + secrets.token_hex(4), "conversationId": "c-new", "isDraft": True,
                 "parentFolderId": "drafts", "receivedDateTime": "2030-01-06T10:00:00Z", "bodyPreview": "",
                 "from": {"emailAddress": {"address": addr}}, "hasAttachments": False, **j}
            box.append(d)
            return h._send(201, d)
        m = re.match(r"^/messages/([^/]+)(/(createReply|createReplyAll|send))?$", rest)
        if m:
            mid, verb = m.group(1), m.group(3)
            msg = next(x for x in box if x["id"] == mid) if any(x["id"] == mid for x in box) else None
            if msg is None:
                raise KeyError(mid)
            if verb in ("createReply", "createReplyAll") and method == "POST":
                d = {"id": "draft-" + secrets.token_hex(4), "conversationId": msg["conversationId"],
                     "subject": "RE: " + msg["subject"], "isDraft": True, "parentFolderId": "drafts",
                     "toRecipients": [msg["from"]], "ccRecipients": msg["ccRecipients"] if verb == "createReplyAll" else [],
                     "from": {"emailAddress": {"address": addr}}, "receivedDateTime": "2030-01-06T10:00:00Z",
                     "body": {"contentType": "html", "content": ""}, "bodyPreview": "", "hasAttachments": False}
                box.append(d)
                return h._send(201, d)
            if verb == "send" and method == "POST":
                if not msg["isDraft"]:
                    return h._send(400, {"error": {"code": "ErrorInvalidRequest"}})
                msg.update(isDraft=False, parentFolderId="sentitems")
                self.sent.append({"box": addr, "id": mid, "to": msg.get("toRecipients"), "body": msg["body"]})
                return h._send(202)
            if method == "PATCH":
                msg.update(json.loads(raw))
                return h._send(200, msg)
            if method == "GET":
                return h._send(200, msg)
        if rest == "/calendarView":
            evs = [e for e in self.events.get(addr, []) if e["start"]["dateTime"][:19] < q["endDateTime"][:19]
                   and e["end"]["dateTime"][:19] > q["startDateTime"][:19]]
            return h._send(200, {"value": evs})
        m = re.match(r"^/events/([^/]+)$", rest)
        if m:
            return h._send(200, next(e for e in self.events.get(addr, []) if e["id"] == m.group(1)))
        if rest == "/events" and method == "POST":
            j = json.loads(raw)
            e = {"id": "ev-" + secrets.token_hex(4), "webLink": "https://outlook.example/ev", **j}
            self.events.setdefault(addr, []).append(e)
            return h._send(201, e)
        if rest == "/calendar/getSchedule":
            j = json.loads(raw)
            return h._send(200, {"value": [{"scheduleId": s, "scheduleItems": [
                {"status": "busy", "start": {"dateTime": "2030-01-07T13:00:00"},
                 "end": {"dateTime": "2030-01-07T15:00:00"}}]} for s in j["schedules"] if s.endswith("contoso.test")]})
        return self.files_route(h, method, path, q, raw)

    def files_route(self, h, method, path, q, raw):
        m = re.match(r"^/v1\.0(/me/drive|/sites/[^/]+/drive)/root(?::/(.*?):)?/(children|content|search\(q='(.*)'\))$", path)
        if not m:
            raise KeyError(path)
        drive, rel, what = m.group(1), (m.group(2) or ""), m.group(3)
        files = self.files.setdefault(drive, {})
        if what == "children":
            pre = rel.strip("/") + "/" if rel else ""
            seen, out = set(), []
            for f in sorted(files):
                if f.startswith(pre):
                    head = f[len(pre):].split("/", 1)
                    if head[0] in seen:
                        continue
                    seen.add(head[0])
                    out.append({"name": head[0], "id": head[0], **({"folder": {}} if len(head) > 1 else {"size": len(files[f])})})
            if rel and not out:
                raise KeyError(rel)
            return h._send(200, {"value": out})
        if what == "content" and method == "GET":
            return h._send(200, raw=files[rel])
        if what == "content" and method == "PUT":
            files[rel] = raw
            return h._send(201, {"id": rel, "name": rel.rsplit("/", 1)[-1], "webUrl": "https://sp.example/" + rel})
        s = (m.group(4) or "").lower()
        out = [{"name": f.rsplit("/", 1)[-1], "id": f, "size": len(v),
                "parentReference": {"path": f"/drive/root:/{f.rsplit('/', 1)[0]}" if "/" in f else "/drive/root:"}}
               for f, v in files.items() if s in f.lower()]
        return h._send(200, {"value": out})
