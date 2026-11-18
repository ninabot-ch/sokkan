"""connectors.ingest — doc_ingest source kind ``webdav``: a WebDAV share (Nextcloud, ownCloud,
a NAS) mirrored ONE WAY into the source's mirror, then read by the memory agent exactly like a
folder (allow-list, deny-list, classification of the source, quarantine).

    {"id": "nextcloud-qualite", "kind": "webdav", "allow": ["Qualite", "Comites/2026"],
     "classification": "project", "project": "default",
     "options": {"url": "https://cloud.example.ch/remote.php/dav/files/ingest",
                 "user": "ingest", "password_secret": "INGEST_WEBDAV_PASSWORD"}}

The password is a vault secret NAME of the source's project (never in sources.json). The
account should be a read-only account of the share: the mirror never writes back, and this
code only issues PROPFIND and GET. SharePoint / OneDrive keep their rclone source (§ 4 of
DOC-INGEST.md); SMB keeps the ``folder`` kind on a read-only mount.
"""
from __future__ import annotations

import os
from pathlib import Path

from docingest import config as cfgmod
from docingest.config import ConfigError
from docingest.sources import SourceError, _has_glob, _TreeSource, mirror_root

from . import ConnectorError


class WebDAVSource(_TreeSource):
    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def validate(cfg) -> None:
        o = cfg.options or {}
        if not str(o.get("url") or "").startswith(("https://", "http://")):
            raise ConfigError(f"{cfg.id}: options.url must be the WebDAV address")
        if not o.get("user") or not o.get("password_secret"):
            raise ConfigError(f"{cfg.id}: options.user and options.password_secret (a vault NAME)")
        for a in cfg.allow:
            if _has_glob(a):
                raise ConfigError(f"{cfg.id}: webdav `allow` entries are remote directories: {a!r}")

    def tree_root(self) -> Path:
        return mirror_root(self.cfg)

    def _client(self):
        import vault

        from .webdav import WebDAV
        o = self.cfg.options
        pw = vault.session_env([o["password_secret"]], project=self.cfg.project).get(o["password_secret"])
        if not pw:
            raise SourceError(f"{self.cfg.id}: vault secret {o['password_secret']} is empty")
        return WebDAV({"providers": {"webdav": {"url": o["url"]}}},
                      {"email": o["user"], "login": o["user"]}, pw)

    def prepare(self) -> dict:
        lim = cfgmod.limits_for(self.cfg)
        root = mirror_root(self.cfg)
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        try:
            dav = self._client()
        except ConnectorError as e:
            raise SourceError(f"{self.cfg.id}: {e.message}") from None
        report = {"synced": [], "errors": [], "fetched": 0}
        exts = tuple(self.cfg.extensions)
        for entry in self.cfg.allow:
            keep: set[Path] = set()
            todo = ["/" + entry.strip("/")]
            try:
                while todo:
                    for it in dav.list(todo.pop()):
                        if it["is_dir"]:
                            todo.append(it["path"])
                            continue
                        if not it["name"].lower().endswith(exts) or it["size"] > lim.max_file_mb * 1024 * 1024:
                            continue
                        dest = (root / it["path"].lstrip("/")).resolve()
                        if root.resolve() not in dest.parents:
                            continue
                        keep.add(dest)
                        if dest.exists() and dest.stat().st_size == it["size"]:
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        tmp.write_bytes(dav.read(it["path"]))
                        os.replace(tmp, dest)
                        report["fetched"] += 1
            except ConnectorError as e:
                report["errors"].append({"path": entry, "error": e.message[:300]})
                continue
            base = (root / entry.strip("/")).resolve()
            if base.exists():
                for p in base.rglob("*"):
                    if p.is_file() and p.resolve() not in keep:
                        p.unlink()           # one way: gone on the share = gone in the mirror
            report["synced"].append(entry)
        if report["errors"] and not report["synced"]:
            raise SourceError(f"{self.cfg.id}: webdav sync failed: {report['errors'][0]['error']}")
        return report

    def describe(self) -> str:
        return f"webdav {self.cfg.options.get('url')} -> {mirror_root(self.cfg)}"


def register() -> None:
    from docingest import sources
    sources.register("webdav", WebDAVSource)
