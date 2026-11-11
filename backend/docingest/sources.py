"""sources.py — where documents come from. Read-only, allow-listed, incremental.

A source LISTS files and lets the extractor OPEN them for reading; it has no other verb.

* `folder`  — a local directory or a mount (CIFS/NFS mounted read-only by the host).
* `rclone`  — Microsoft 365 (SharePoint / OneDrive), Google Drive or any rclone remote,
              mirrored one way (remote -> local mirror) by `rclone sync`, then read like a
              folder. SOKKAN never holds a write permission on the tenant: the application
              registered there is read-only (docs/enterprise/DOC-INGEST.md § 4).
* anything else — `register("kind", Class)` (interface `Source`).

Allow-list: a file is read only if its path RELATIVE to the source root matches an `allow`
entry, matches no `deny` entry, and its real path (symlinks resolved) is still inside the
root. Everything else is never opened — not hashed, not stat-ed beyond the listing.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from . import config as cfgmod
from .config import ConfigError, SourceConfig


@dataclass(frozen=True)
class FileRef:
    source: str
    rel: str            # path relative to the source root, '/' separated — the identity
    path: Path          # where to read it (local file or mirror)
    size: int
    mtime: float


class SourceError(RuntimeError):
    pass


class Source(Protocol):
    """Interface of a source kind. `validate` is called when the configuration is loaded."""
    cfg: SourceConfig

    @staticmethod
    def validate(cfg: SourceConfig) -> None: ...
    def prepare(self) -> dict: ...                 # e.g. mirror sync; returns a small report
    def iter_files(self) -> Iterator[FileRef]: ...
    def describe(self) -> str: ...


# ---- allow-list ----------------------------------------------------------------------------
def _match(rel: str, pattern: str) -> bool:
    """`Cours` or `Cours/**` = everything under Cours; otherwise a glob on the whole path
    ('*' does not cross a '/', '**' does)."""
    pattern = pattern.strip("/")
    if pattern.endswith("/**"):
        base = pattern[:-3]
        if not _has_glob(base):
            return rel == base or rel.startswith(base + "/")
    if not _has_glob(pattern):
        return rel == pattern or rel.startswith(pattern + "/")
    rx = _glob_rx(pattern)
    return rx.match(rel) is not None


def _has_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def _glob_rx(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def allowed(cfg: SourceConfig, rel: str) -> bool:
    """THE allow-list decision. `rel` is relative, normalised, never climbing."""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False
    if not any(_match(rel, a) for a in cfg.allow):
        return False
    return not any(_match(rel, d) for d in cfg.deny)


def _walk_roots(cfg: SourceConfig) -> list[str]:
    """Directories worth walking: the literal prefix of each allow entry (so that a sibling
    directory outside the allow-list is never even listed)."""
    out = []
    for a in cfg.allow:
        parts = []
        for seg in a.split("/"):
            if _has_glob(seg):
                break
            parts.append(seg)
        out.append("/".join(parts))
    out = sorted(set(out))
    return [r for r in out if not any(r != o and (o == "" or r.startswith(o + "/")) for o in out)]


class _TreeSource:
    """Shared listing of a local tree (the folder itself, or an rclone mirror)."""
    cfg: SourceConfig

    def tree_root(self) -> Path:
        raise NotImplementedError

    def iter_files(self) -> Iterator[FileRef]:
        root = self.tree_root()
        try:
            real_root = root.resolve(strict=True)
        except OSError as e:
            raise SourceError(f"{self.cfg.id}: root not readable: {e}") from None
        seen: set[str] = set()
        for start in _walk_roots(self.cfg):
            top = real_root / start if start else real_root
            if top.is_file():
                candidates = [(str(top.parent), [], [top.name])]
            else:
                candidates = os.walk(top, followlinks=False)
            for dirpath, dirnames, filenames in candidates:
                dirnames.sort()
                for fn in sorted(filenames):
                    p = Path(dirpath) / fn
                    try:
                        rel = p.relative_to(real_root).as_posix()
                    except ValueError:
                        continue
                    if rel in seen or not allowed(self.cfg, rel):
                        continue
                    seen.add(rel)
                    try:
                        # a symlink may point anywhere: the REAL file must be inside the root
                        rp = p.resolve(strict=True)
                        rp.relative_to(real_root)
                        st = rp.stat()
                    except (OSError, ValueError):
                        continue
                    if not rp.is_file():
                        continue
                    yield FileRef(self.cfg.id, rel, rp, st.st_size, st.st_mtime)


# ---- folder ----------------------------------------------------------------------------------
def is_readonly_mount(path: Path) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


class FolderSource(_TreeSource):
    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg

    @staticmethod
    def validate(cfg: SourceConfig) -> None:
        if not cfg.root or not os.path.isabs(cfg.root):
            raise ConfigError(f"{cfg.id}: `root` must be an absolute directory")
        if os.path.realpath(cfg.root) == "/":
            raise ConfigError(f"{cfg.id}: `root` cannot be /")

    def tree_root(self) -> Path:
        return Path(self.cfg.root)

    def prepare(self) -> dict:
        root = self.tree_root()
        if not root.is_dir():
            raise SourceError(f"{self.cfg.id}: {root} is not a directory (mount missing?)")
        ro = is_readonly_mount(root)
        if self.cfg.require_readonly and not ro:
            raise SourceError(
                f"{self.cfg.id}: {root} is not mounted read-only and the source says "
                "require_readonly — mount it `ro` (docs/enterprise/DOC-INGEST.md § 3)")
        return {"readonly_mount": ro}

    def describe(self) -> str:
        return f"folder {self.cfg.root}"


# ---- rclone ----------------------------------------------------------------------------------
# flags an operator may add per source (`options.flags`): read-side tuning only. Anything that
# could turn the sync around or write to the remote is not in this list.
RCLONE_SAFE_FLAGS = (
    "--drive-shared-with-me", "--drive-team-drive", "--drive-root-folder-id",
    "--drive-export-formats", "--drive-skip-shortcuts", "--drive-acknowledge-abuse",
    "--onedrive-drive-id", "--onedrive-drive-type", "--tpslimit", "--transfers", "--checkers",
    "--bwlimit", "--max-age", "--min-age", "--fast-list", "--drive-pacer-min-sleep",
)
# never passed, whatever the configuration says
RCLONE_VERBS = ("sync", "lsjson", "version", "listremotes")


def rclone_bin() -> str:
    return os.environ.get("SOKKAN_DOC_INGEST_RCLONE") or shutil.which("rclone") or "rclone"


def rclone_config() -> str:
    return (os.environ.get("SOKKAN_DOC_INGEST_RCLONE_CONFIG")
            or str(cfgmod.home() / "rclone.conf"))


def mirror_root(cfg: SourceConfig) -> Path:
    return cfgmod.home() / "mirror" / cfg.id


def _extra_flags(cfg: SourceConfig) -> list[str]:
    out = []
    for f in cfg.options.get("flags") or []:
        name = str(f).split("=", 1)[0]
        if name not in RCLONE_SAFE_FLAGS:
            raise ConfigError(f"{cfg.id}: rclone flag {name!r} is not allowed here "
                              f"(allowed: {', '.join(RCLONE_SAFE_FLAGS)})")
        out.append(str(f))
    return out


def rclone_sync_command(cfg: SourceConfig, allow_entry: str, limits=None) -> list[str]:
    """`rclone sync <remote>:<allow_entry> <mirror>/<allow_entry>` — ONE direction, built here
    and nowhere else: the source is always the remote, the destination always inside this
    source's mirror directory. Raises on anything else."""
    if not cfgmod.REMOTE_RE.match(cfg.remote or ""):
        raise ConfigError(f"{cfg.id}: invalid rclone remote name {cfg.remote!r}")
    entry = allow_entry.strip("/")
    if _has_glob(entry) or not entry or ".." in entry.split("/"):
        raise ConfigError(f"{cfg.id}: an rclone `allow` entry is a remote directory, without "
                          f"wildcard: {allow_entry!r}")
    if entry not in cfg.allow:
        raise ConfigError(f"{cfg.id}: {entry!r} is not in the allow-list")
    mirror = mirror_root(cfg).resolve()
    dest = (mirror / entry).resolve()
    if mirror != dest and mirror not in dest.parents:
        raise ConfigError(f"{cfg.id}: destination {dest} escapes the mirror")
    lim = limits or cfgmod.limits_for(cfg)
    cmd = [rclone_bin(), "sync", f"{cfg.remote}:{entry}", str(dest),
           "--config", rclone_config(),
           "--max-size", f"{int(lim.max_file_mb * 1024)}k",
           "--create-empty-src-dirs=false", "--stats-one-line", "--stats", "0",
           "--use-json-log", "--log-level", "NOTICE"]
    for e in cfg.extensions:
        cmd += ["--include", f"*{e}"]
    cmd += _extra_flags(cfg)
    assert cmd[1] in RCLONE_VERBS and cmd[2].startswith(cfg.remote + ":") and cmd[3] == str(dest)
    return cmd


class RcloneSource(_TreeSource):
    def __init__(self, cfg: SourceConfig):
        self.cfg = cfg

    @staticmethod
    def validate(cfg: SourceConfig) -> None:
        if not cfgmod.REMOTE_RE.match(cfg.remote or ""):
            raise ConfigError(f"{cfg.id}: `remote` must name a remote of the rclone config")
        for a in cfg.allow:
            if _has_glob(a):
                raise ConfigError(f"{cfg.id}: rclone `allow` entries are remote directories "
                                  f"(no wildcard): {a!r}")
        _extra_flags(cfg)

    def tree_root(self) -> Path:
        return mirror_root(self.cfg)

    def prepare(self) -> dict:
        """Incremental one-way sync of every allow-listed remote directory into the mirror
        (rclone compares size + modification time and only fetches what changed)."""
        lim = cfgmod.limits_for(self.cfg)
        mirror_root(self.cfg).mkdir(parents=True, exist_ok=True)
        os.chmod(mirror_root(self.cfg), 0o700)
        report = {"synced": [], "errors": []}
        for entry in self.cfg.allow:
            cmd = rclone_sync_command(self.cfg, entry, lim)
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=int(self.cfg.options.get("sync_timeout_s") or 6 * 3600))
            except FileNotFoundError:
                raise SourceError("rclone is not installed (SOKKAN_DOC_INGEST_RCLONE)") from None
            except subprocess.TimeoutExpired:
                report["errors"].append({"path": entry, "error": "sync timed out"})
                continue
            if p.returncode != 0:
                # never the config, never a token: rclone's own last line only
                tail = (p.stderr or "").strip().splitlines()[-1:] or ["rclone failed"]
                report["errors"].append({"path": entry, "error": tail[0][:300],
                                         "code": p.returncode})
            else:
                report["synced"].append(entry)
        if report["errors"] and not report["synced"]:
            raise SourceError(f"{self.cfg.id}: rclone sync failed: {report['errors'][0]['error']}")
        return report

    def describe(self) -> str:
        return f"rclone {self.cfg.remote}: -> {mirror_root(self.cfg)}"


# ---- registry --------------------------------------------------------------------------------
_KINDS: dict[str, type] = {"folder": FolderSource, "rclone": RcloneSource}


def register(kind: str, cls: type) -> None:
    """Add a source kind (a connector). `cls(cfg)` must implement `Source`; its listing is
    still filtered by `allowed()` in the runner — a connector cannot widen the allow-list."""
    if not re.match(r"^[a-z][a-z0-9_-]{1,30}$", kind):
        raise ValueError(f"invalid source kind {kind!r}")
    _KINDS[kind] = cls


def kinds() -> list[str]:
    return sorted(_KINDS)


def validate(cfg: SourceConfig) -> None:
    if cfg.kind not in _KINDS:
        raise ConfigError(f"{cfg.id}: unknown source kind {cfg.kind!r} ({', '.join(kinds())})")
    _KINDS[cfg.kind].validate(cfg)


def open_source(cfg: SourceConfig) -> Source:
    validate(cfg)
    return _KINDS[cfg.kind](cfg)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:      # the only way this package opens a source file: rb
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
