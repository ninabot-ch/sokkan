"""connectors.webdav — shared documents on a WebDAV server (Nextcloud, ownCloud, a NAS) and
on a network folder (SMB / NFS share mounted on the host: provider ``folder``).

Reads are open (list, search by name, read); a write is an action, executed on a card.

``folder``: the operator mounts the share (``mount -t cifs … -o ro`` for reading; a SECOND
mount, read-write, for the deliverables if they want any — `providers.folder.write_root`).
SOKKAN never mounts anything itself and never follows a path out of the root (symlinks
resolved and checked). The rights are those of the mount: an operator who mounts read-only
gets a read-only connector, whatever a card says.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree as ET

from . import ConnectorError, http

MAX_READ = 25 * 1024 * 1024


def _clean(path: str) -> str:
    parts = [p for p in (path or "/").replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise ConnectorError("chemin invalide", 400, "path")
    return "/" + "/".join(parts)


class WebDAV:
    def __init__(self, settings: dict, acc: dict, password: str):
        base = ((settings.get("providers") or {}).get("webdav") or {}).get("url") or ""
        if not base:
            raise ConnectorError("WebDAV n'est pas configuré (Réglages › Connecteurs).", 409, "not_configured")
        self.login = acc.get("login") or acc["email"]
        url = base.replace("{login}", quote(self.login))
        if urlparse(url).scheme != "https":
            from .caldav import _plain_ok
            if not _plain_ok(url):
                raise ConnectorError("WebDAV : https obligatoire.", 400, "url")
        self.base = url.rstrip("/")
        self.base_path = urlparse(self.base).path.rstrip("/")
        self.auth = (self.login, password)

    def _url(self, path: str) -> str:
        return self.base + quote(_clean(path))

    def _req(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None):
        with http(60.0) as h:
            r = h.request(method, self._url(path), content=body, auth=self.auth, headers=headers or {})
        if r.status_code in (401, 403):
            raise ConnectorError("Le serveur de documents refuse l'accès.", 403, "denied")
        if r.status_code == 404:
            raise ConnectorError(f"Introuvable : {path}", 404, "not_found")
        if r.status_code >= 300:
            raise ConnectorError(f"Le serveur de documents a répondu {r.status_code}.", 502, "upstream")
        return r

    def _propfind(self, path: str, depth: str) -> list[dict]:
        r = self._req("PROPFIND", path, body=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                      b'<d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>',
                      headers={"depth": depth, "content-type": "application/xml"})
        out = []
        for resp in ET.fromstring(r.content).findall("{DAV:}response"):
            href = unquote(urlparse(resp.findtext("{DAV:}href", "") or "").path)
            rel = href[len(self.base_path):] if href.startswith(self.base_path) else href
            rel = _clean(rel)
            is_dir = resp.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
            size = resp.findtext(".//{DAV:}getcontentlength") or "0"
            out.append({"path": rel, "name": rel.rsplit("/", 1)[-1], "is_dir": is_dir,
                        "size": int(size) if size.isdigit() else 0,
                        "modified": resp.findtext(".//{DAV:}getlastmodified") or ""})
        return out

    def list(self, path: str) -> list[dict]:
        me = _clean(path)
        return [x for x in self._propfind(me, "1") if x["path"] != me]

    def search(self, query: str, path: str = "/", depth: int = 3) -> list[dict]:
        q, out, todo = query.lower(), [], [(_clean(path), 0)]
        while todo and len(out) < 50:
            cur, d = todo.pop(0)
            for it in self.list(cur):
                if q in it["name"].lower():
                    out.append(it)
                if it["is_dir"] and d + 1 < depth:
                    todo.append((it["path"], d + 1))
        return out

    def read(self, path: str) -> bytes:
        r = self._req("GET", path)
        if len(r.content) > MAX_READ:
            raise ConnectorError("Fichier trop volumineux.", 413, "too_large")
        return r.content

    def write(self, path: str, data: bytes) -> dict:
        p = _clean(path)
        # never overwrite: a deliverable that exists gets a numbered name
        name, n = p, 1
        while True:
            with http() as h:
                head = h.request("HEAD", self._url(name), auth=self.auth)
            if head.status_code == 404:
                break
            stem, dot, ext = p.rpartition(".")
            name = f"{stem} ({n}).{ext}" if dot else f"{p} ({n})"
            n += 1
            if n > 50:
                raise ConnectorError("Trop de versions de ce fichier.", 409, "exists")
        self._req("PUT", name, body=data, headers={"content-type": "application/octet-stream"})
        return {"path": name}

    def check(self) -> None:
        self._propfind("/", "0")


class Folder:
    """A share mounted on the host (`providers.folder.root`, `write_root` optional)."""

    def __init__(self, settings: dict):
        cfg = (settings.get("providers") or {}).get("folder") or {}
        if not cfg.get("root"):
            raise ConnectorError("Aucun dossier réseau n'est configuré.", 409, "not_configured")
        self.root = Path(cfg["root"]).resolve()
        self.write_root = Path(cfg["write_root"]).resolve() if cfg.get("write_root") else None

    @staticmethod
    def _inside(root: Path, path: str) -> Path:
        p = (root / _clean(path).lstrip("/")).resolve()
        if p != root and root not in p.parents:
            raise ConnectorError("chemin hors du dossier partagé", 400, "path")
        return p

    def _item(self, p: Path) -> dict:
        st = p.stat()
        rel = "/" + str(p.relative_to(self.root)).replace(os.sep, "/").lstrip(".")
        return {"path": _clean(rel), "name": p.name, "is_dir": p.is_dir(),
                "size": 0 if p.is_dir() else st.st_size, "modified": st.st_mtime}

    def list(self, path: str) -> list[dict]:
        d = self._inside(self.root, path)
        if not d.is_dir():
            raise ConnectorError(f"Introuvable : {path}", 404, "not_found")
        out = []
        for c in sorted(d.iterdir()):
            try:
                self._inside(self.root, str(c.relative_to(self.root)))
            except (ConnectorError, ValueError):
                continue                    # a symlink out of the share: not shown
            out.append(self._item(c))
        return out[:500]

    def search(self, query: str, path: str = "/", depth: int = 4) -> list[dict]:
        base, q, out = self._inside(self.root, path), query.lower(), []
        for dirpath, dirs, files in os.walk(base):
            if Path(dirpath).relative_to(base).parts.__len__() >= depth:
                dirs[:] = []
            for n in dirs + files:
                if q in n.lower():
                    p = Path(dirpath) / n
                    try:
                        self._inside(self.root, str(p.relative_to(self.root)))
                    except (ConnectorError, ValueError):
                        continue
                    out.append(self._item(p))
                    if len(out) >= 50:
                        return out
        return out

    def read(self, path: str) -> bytes:
        p = self._inside(self.root, path)
        if not p.is_file():
            raise ConnectorError(f"Introuvable : {path}", 404, "not_found")
        if p.stat().st_size > MAX_READ:
            raise ConnectorError("Fichier trop volumineux.", 413, "too_large")
        return p.read_bytes()

    def write(self, path: str, data: bytes) -> dict:
        if self.write_root is None:
            raise ConnectorError("Le dossier réseau est en lecture seule (aucun dossier d'écriture configuré).",
                                 409, "read_only")
        p = self._inside(self.write_root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        target, n = p, 1
        while target.exists():
            target = p.with_name(f"{p.stem} ({n}){p.suffix}")
            n += 1
        with open(target, "xb") as f:
            f.write(data)
        return {"path": "/" + str(target.relative_to(self.write_root)).replace(os.sep, "/")}
