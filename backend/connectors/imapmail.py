"""connectors.imapmail — any mailbox through IMAP (read, drafts) and SMTP (send, on a card).

The person connects with their own login (an app password where the provider has them); the
instance settings say where the servers are (`providers.imap`: imap_host, imap_port,
smtp_host, smtp_port, security ``ssl`` | ``starttls`` | ``none``). ``none`` is refused unless
the host is loopback/private and ``SOKKAN_CONNECTORS_ALLOW_PLAIN=1`` (tests, a relay on the
machine) — a password never crosses a network in clear.

Threads: IMAP has no conversation id; a thread is the message and every message sharing its
root (References / In-Reply-To / Message-ID), found by header search in the folder and in
Sent. Ids are ``<folder>:<uid>`` (UIDs, stable within UIDVALIDITY).
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import ipaddress
import os
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime

from . import ConnectorError

DRAFTS = ("Drafts", "INBOX.Drafts", "Brouillons", "[Gmail]/Drafts")
SENT = ("Sent", "INBOX.Sent", "Sent Items", "Éléments envoyés", "[Gmail]/Sent Mail")


def _private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    return ip.is_loopback or ip.is_private


def _cfg(settings: dict) -> dict:
    c = dict((settings.get("providers") or {}).get("imap") or {})
    if not c.get("imap_host"):
        raise ConnectorError("IMAP n'est pas configuré (Réglages › Connecteurs).", 409, "not_configured")
    sec = c.get("security") or "ssl"
    if sec == "none":
        if not (os.environ.get("SOKKAN_CONNECTORS_ALLOW_PLAIN") == "1" and _private(c["imap_host"])):
            raise ConnectorError("IMAP sans chiffrement refusé.", 409, "plain")
    c["security"] = sec
    return c


def _text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_content()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                from .graph import _strip_html
                return _strip_html(part.get_content())
        return ""
    if msg.get_content_type() == "text/html":
        from .graph import _strip_html
        return _strip_html(msg.get_content())
    try:
        return msg.get_content()
    except (KeyError, LookupError):
        return ""


def _addrs(v) -> list[str]:
    return [a for _, a in getaddresses([str(x) for x in ([v] if v else [])]) if a]


class Mail:
    def __init__(self, settings: dict, acc: dict, password: str):
        self.cfg, self.acc, self.password = _cfg(settings), acc, password
        self.login = acc.get("login") or acc["email"]

    # -- connection
    def _imap(self):
        c = self.cfg
        port = int(c.get("imap_port") or (993 if c["security"] == "ssl" else 143))
        try:
            if c["security"] == "ssl":
                m = imaplib.IMAP4_SSL(c["imap_host"], port, ssl_context=ssl.create_default_context(),
                                      timeout=20)
            else:
                m = imaplib.IMAP4(c["imap_host"], port, timeout=20)
                if c["security"] == "starttls":
                    m.starttls(ssl_context=ssl.create_default_context())
            m.login(self.login, self.password)
        except imaplib.IMAP4.error:
            raise ConnectorError("Le serveur IMAP refuse ces identifiants.", 403, "denied") from None
        except OSError as e:
            raise ConnectorError(f"Serveur IMAP injoignable ({type(e).__name__}).", 502, "upstream") from None
        return m

    @staticmethod
    def _q(name: str) -> str:
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _folder(self, m, wanted: tuple[str, ...]) -> str:
        typ, data = m.list()
        names = []
        for raw in data or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            mm = re.search(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not mm:
                continue
            name = mm.group("name").strip().strip('"')
            flags = mm.group("flags")
            if ("\\Drafts" in flags and wanted is DRAFTS) or ("\\Sent" in flags and wanted is SENT):
                return name
            names.append(name)
        for w in wanted:
            if w in names:
                return w
        if wanted is DRAFTS:
            m.create(self._q("Drafts"))
            return "Drafts"
        return ""

    def _fetch(self, m, folder: str, uids: list[bytes], body: bool) -> list[dict]:
        out = []
        for uid in uids:
            typ, data = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[])" if body else
                              "(FLAGS BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.400>)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            parts = [x for x in data if isinstance(x, tuple)]
            if body:
                raw = parts[0][1]
            else:
                # servers answer the two sections in either order: put the header first
                head = [p[1] for p in parts if b"HEADER" in p[0]]
                text = [p[1] for p in parts if b"TEXT" in p[0] and b"HEADER" not in p[0]]
                raw = b"".join(head) + b"\r\n" + b"".join(text)
            flags = b" ".join(p[0] for p in parts).decode(errors="replace")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            try:
                date = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else ""
            except (TypeError, ValueError):
                date = ""
            refs = (msg.get("References") or "").split() + [msg.get("In-Reply-To") or ""]
            root = next((r for r in refs if r), msg.get("Message-ID") or "")
            d = {"id": f"{folder}:{uid.decode()}", "thread_id": f"{folder}:{uid.decode()}",
                 "message_id": msg.get("Message-ID") or "", "root": root,
                 "subject": str(msg.get("Subject") or ""), "from": (_addrs(msg.get("From")) or [""])[0],
                 "to": _addrs(msg.get("To")), "cc": _addrs(msg.get("Cc")), "date": date,
                 "is_draft": "\\Draft" in flags, "has_attachments": any(
                     p.get_filename() for p in msg.walk()) if msg.is_multipart() else False}
            text = _text(msg) if body else ""
            if body:
                d["body"] = text
                d["attachments"] = [{"name": p.get_filename(), "size": len(p.get_payload(decode=True) or b"")}
                                    for p in msg.walk() if p.get_filename()]
            else:
                if msg.is_multipart():
                    d["snippet"] = ""
                else:
                    try:
                        d["snippet"] = re.sub(r"\s+", " ", msg.get_content() or "")[:300]
                    except (KeyError, LookupError):
                        d["snippet"] = ""
            out.append(d)
        return out

    @staticmethod
    def _split(mid: str) -> tuple[str, bytes]:
        if ":" not in mid:
            raise ConnectorError("identifiant de message invalide", 400, "id")
        folder, uid = mid.rsplit(":", 1)
        if not uid.isdigit():
            raise ConnectorError("identifiant de message invalide", 400, "id")
        return folder, uid.encode()

    # -- reads
    def list(self, folder: str = "INBOX", mailbox: str = "", query: str = "", limit: int = 20) -> list[dict]:
        if mailbox and mailbox.lower() != self.login.lower():
            raise ConnectorError("IMAP : une boîte partagée se connecte comme un compte à part.", 400,
                                 "shared_imap")
        folder = "INBOX" if (folder or "inbox").lower() == "inbox" else folder
        m = self._imap()
        try:
            if m.select(self._q(folder), readonly=True)[0] != "OK":
                raise ConnectorError(f"Dossier introuvable : {folder}", 404, "not_found")
            crit = ["ALL"]
            if query:
                q = query.replace('"', "")
                crit = ["OR", "SUBJECT", f'"{q}"', "OR", "FROM", f'"{q}"', "BODY", f'"{q}"']
            typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", *crit) if query else m.uid("SEARCH", *crit)
            uids = (data[0] or b"").split()[-max(1, min(limit, 50)):]
            msgs = self._fetch(m, folder, list(reversed(uids)), body=False)
        finally:
            m.logout()
        return msgs

    def _get(self, m, mid: str) -> dict:
        folder, uid = self._split(mid)
        if m.select(self._q(folder), readonly=True)[0] != "OK":
            raise ConnectorError("Dossier introuvable.", 404, "not_found")
        got = self._fetch(m, folder, [uid], body=True)
        if not got:
            raise ConnectorError("Message introuvable.", 404, "not_found")
        return got[0]

    def thread(self, thread_id: str, mailbox: str = "") -> list[dict]:
        m = self._imap()
        try:
            first = self._get(m, thread_id)
            root = first.get("root") or first.get("message_id")
            found = {first["id"]: first}
            if root:
                for folder in ("INBOX", self._folder(m, SENT)):
                    if not folder or m.select(self._q(folder), readonly=True)[0] != "OK":
                        continue
                    r = root.replace('"', "")
                    typ, data = m.uid("SEARCH", "OR", "OR", "HEADER", "Message-ID", f'"{r}"',
                                      "HEADER", "References", f'"{r}"', "HEADER", "In-Reply-To", f'"{r}"')
                    for d in self._fetch(m, folder, (data[0] or b"").split()[-30:], body=True):
                        found.setdefault(d["id"], d)
        finally:
            m.logout()
        return sorted(found.values(), key=lambda d: d["date"])

    # -- drafts (in the person's own Drafts folder)
    def _append_draft(self, msg: EmailMessage) -> dict:
        m = self._imap()
        try:
            folder = self._folder(m, DRAFTS)
            typ, data = m.append(self._q(folder), "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()),
                                 msg.as_bytes())
            if typ != "OK":
                raise ConnectorError("Le serveur IMAP refuse le brouillon.", 502, "upstream")
            uid = ""
            mm = re.search(rb"APPENDUID \d+ (\d+)", b" ".join(x for x in data if isinstance(x, bytes)))
            if mm:
                uid = mm.group(1).decode()
            else:
                m.select(self._q(folder), readonly=True)
                typ, d2 = m.uid("SEARCH", "HEADER", "Message-ID", f'"{msg["Message-ID"]}"')
                uid = ((d2[0] or b"").split() or [b""])[-1].decode()
        finally:
            m.logout()
        return {"draft_id": f"{folder}:{uid}", "subject": str(msg["Subject"]),
                "to": _addrs(msg["To"]), "cc": _addrs(msg["Cc"]), "mailbox": ""}

    def draft_reply(self, message_id: str, body: str, mailbox: str = "", reply_all: bool = False) -> dict:
        m = self._imap()
        try:
            orig = self._get(m, message_id)
        finally:
            m.logout()
        msg = EmailMessage()
        msg["From"] = self.login
        msg["To"] = orig["from"]
        if reply_all:
            cc = [a for a in orig["to"] + orig["cc"] if a.lower() != self.login.lower()]
            if cc:
                msg["Cc"] = ", ".join(cc)
        subj = orig["subject"]
        msg["Subject"] = subj if subj.lower().startswith(("re:", "aw:", "tr:")) else f"Re: {subj}"
        if orig.get("message_id"):
            msg["In-Reply-To"] = orig["message_id"]
            msg["References"] = " ".join(x for x in (orig.get("root"), orig["message_id"]) if x and x != "")
        msg["Message-ID"] = make_msgid(domain=self.login.split("@")[-1] or None)
        msg.set_content(body)
        return self._append_draft(msg)

    def draft_new(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  mailbox: str = "") -> dict:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.login, ", ".join(to), subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Message-ID"] = make_msgid(domain=self.login.split("@")[-1] or None)
        msg.set_content(body)
        return self._append_draft(msg)

    def _raw(self, m, mid: str) -> bytes:
        folder, uid = self._split(mid)
        if m.select(self._q(folder))[0] != "OK":
            raise ConnectorError("Brouillon introuvable.", 404, "not_found")
        typ, data = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise ConnectorError("Brouillon introuvable.", 404, "not_found")
        if b"\\Draft" not in data[0][0]:
            raise ConnectorError("Ce message n'est pas un brouillon.", 409, "not_draft")
        return data[0][1]

    def get_draft(self, draft_id: str, mailbox: str = "") -> dict:
        m = self._imap()
        try:
            self._raw(m, draft_id)
            return self._get(m, draft_id)
        finally:
            m.logout()

    def send_draft(self, draft_id: str, mailbox: str = "") -> dict:
        """SMTP submission of the draft AS IT IS NOW in the mailbox (what the person may have
        edited in their own mail client), then moved to Sent."""
        m = self._imap()
        try:
            raw = self._raw(m, draft_id)
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            rcpts = _addrs(msg.get("To")) + _addrs(msg.get("Cc")) + _addrs(msg.get("Bcc"))
            if not rcpts:
                raise ConnectorError("Le brouillon n'a pas de destinataire.", 409, "no_recipient")
            del msg["Bcc"]
            self._smtp(msg, rcpts)
            folder, uid = self._split(draft_id)
            sent = self._folder(m, SENT)
            if sent:
                m.append(self._q(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            m.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            m.expunge()
        finally:
            m.logout()
        return {"sent": True, "recipients": rcpts}

    def _smtp(self, msg, rcpts: list[str]) -> None:
        c = self.cfg
        host = c.get("smtp_host") or c["imap_host"]
        sec = c.get("smtp_security") or c["security"]
        port = int(c.get("smtp_port") or (465 if sec == "ssl" else 587))
        try:
            if sec == "ssl":
                s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
            else:
                s = smtplib.SMTP(host, port, timeout=30)
                if sec == "starttls":
                    s.starttls(context=ssl.create_default_context())
            with s:
                if c.get("smtp_auth", True):
                    s.login(self.login, self.password)
                s.send_message(msg, from_addr=self.login, to_addrs=rcpts)
        except smtplib.SMTPAuthenticationError:
            raise ConnectorError("Le serveur SMTP refuse ces identifiants.", 403, "denied") from None
        except (smtplib.SMTPException, OSError) as e:
            raise ConnectorError(f"Envoi SMTP impossible ({type(e).__name__}).", 502, "upstream") from None

    def check(self) -> None:
        self._imap().logout()
