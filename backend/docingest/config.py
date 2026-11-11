"""config.py — what the operator declares: the sources (allow-list) and the limits.

The sources live in ONE file on the host, `$SOKKAN_DATA_DIR/doc-ingest/sources.json`
(SOKKAN_DOC_INGEST_CONFIG). It is deliberately not editable from the cockpit: adding a path
the agent may read is an act of the machine's operator, not of a logged-in user.

    {"sources": [
      {"id": "nas-formation", "kind": "folder", "root": "/mnt/nas/formation",
       "allow": ["Cours/**", "PV/**"], "deny": ["**/Brouillons/**"],
       "classification": "project", "project": "default",
       "require_readonly": true, "trust_after": 3, "schedule": "30 1 * * *"},
      {"id": "sharepoint-qualite", "kind": "rclone", "remote": "m365",
       "allow": ["Qualite/Procedures", "Direction/PV"], "classification": "confidential"}
    ]}
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# what the extractors read; anything else is listed as `skipped: extension` (never opened)
DEFAULT_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt", ".csv", ".html",
                      ".htm", ".rtf", ".odt")
DEFAULT_DENY = ("**/~$*", "**/.*", "**/.*/**", "**/Thumbs.db", "**/*.tmp")


class ConfigError(ValueError):
    pass


def data_dir() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def home() -> Path:
    return Path(os.environ.get("SOKKAN_DOC_INGEST_DIR") or data_dir() / "doc-ingest")


def config_path() -> Path:
    return Path(os.environ.get("SOKKAN_DOC_INGEST_CONFIG") or home() / "sources.json")


def _num(name: str, default, cast=int):
    raw = (os.environ.get(name) or "").strip()
    try:
        return cast(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    """Per pass and per document. Environment defaults; a source may lower or raise them."""
    max_file_mb: float = 25.0             # a bigger file is listed, never opened
    max_doc_chars: int = 240_000          # extracted text given to the model (~70k tokens)
    extract_timeout_s: int = 180
    extract_mem_mb: int = 4096            # resident memory of the extraction child: killed past it
    max_tokens_per_pass: int = 0          # 0 = no ceiling; prompt + completion tokens
    max_docs_per_pass: int = 0
    max_minutes_per_pass: int = 0
    concurrency: int = 4                  # simultaneous model streams
    extract_workers: int = 2              # extraction child processes (shared by the streams)

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            max_file_mb=_num("SOKKAN_DOC_INGEST_MAX_FILE_MB", 25.0, float),
            max_doc_chars=_num("SOKKAN_DOC_INGEST_MAX_DOC_CHARS", 240_000),
            extract_timeout_s=_num("SOKKAN_DOC_INGEST_EXTRACT_TIMEOUT_S", 180),
            extract_mem_mb=_num("SOKKAN_DOC_INGEST_EXTRACT_MEM_MB", 4096),
            max_tokens_per_pass=_num("SOKKAN_DOC_INGEST_MAX_TOKENS_PER_PASS", 0),
            max_docs_per_pass=_num("SOKKAN_DOC_INGEST_MAX_DOCS_PER_PASS", 0),
            max_minutes_per_pass=_num("SOKKAN_DOC_INGEST_MAX_MINUTES_PER_PASS", 0),
            concurrency=max(1, min(_num("SOKKAN_DOC_INGEST_CONCURRENCY", 4), 32)),
        )


@dataclass(frozen=True)
class SourceConfig:
    id: str
    kind: str
    allow: tuple[str, ...]
    deny: tuple[str, ...] = DEFAULT_DENY
    root: str = ""                        # folder: absolute directory
    remote: str = ""                      # rclone: remote name of the rclone config
    classification: int = 2               # floor of every note derived from this source
    project: str = "default"
    enabled: bool = True
    require_readonly: bool = False        # folder: refuse a mount that is not read-only
    trust_after: int = 0                  # 0 = every batch waits for a human
    trust_max_level: int = 2              # above: never auto-approved, whatever the trust
    schedule: str = ""                    # cron of the incremental pass ('' = manual only)
    timezone: str = ""
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    limits: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)   # kind-specific (rclone flags allow-list)

    def public(self) -> dict:
        from core import levels
        return {"id": self.id, "kind": self.kind, "allow": list(self.allow),
                "deny": list(self.deny), "root": self.root, "remote": self.remote,
                "classification": levels.ident(self.classification), "project": self.project,
                "enabled": self.enabled, "require_readonly": self.require_readonly,
                "trust_after": self.trust_after,
                "trust_max_level": levels.ident(self.trust_max_level),
                "schedule": self.schedule, "extensions": list(self.extensions)}


def _globs(raw, what: str, sid: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ConfigError(f"{sid}: `{what}` must be a list of path patterns")
    out = []
    for g in raw:
        g = g.strip().replace("\\", "/")
        if g.startswith("/") or ".." in g.split("/"):
            # an allow entry is RELATIVE to the source root and cannot climb out of it
            raise ConfigError(f"{sid}: `{what}` entry {g!r} must be relative, without '..'")
        out.append(g.strip("/"))
    return tuple(out)


def parse_source(raw: dict) -> SourceConfig:
    from core import levels
    if not isinstance(raw, dict):
        raise ConfigError("a source must be an object")
    sid = str(raw.get("id") or "").strip()
    if not ID_RE.match(sid):
        raise ConfigError(f"source id {sid!r}: lowercase kebab-case, 2-40 characters")
    kind = str(raw.get("kind") or "").strip()
    allow = _globs(raw.get("allow"), "allow", sid)
    if not allow:
        # no allow-list = nothing is read: an empty list is a configuration error, never
        # « everything »
        raise ConfigError(f"{sid}: `allow` is empty — list the paths this source may read")
    deny = DEFAULT_DENY + _globs(raw.get("deny") or [], "deny", sid)
    lvl = levels.parse(raw.get("classification"))
    project = str(raw.get("project") or "default").strip()
    if not PROJECT_RE.match(project):
        raise ConfigError(f"{sid}: invalid project {project!r}")
    tml = levels.parse(raw.get("trust_max_level"))
    ext = raw.get("extensions")
    if ext is not None and (not isinstance(ext, list) or not all(
            isinstance(e, str) and re.match(r"^\.[a-z0-9]{1,8}$", e) for e in ext)):
        raise ConfigError(f"{sid}: `extensions` must look like [\".pdf\", \".docx\"]")
    schedule = str(raw.get("schedule") or "").strip()
    if schedule:
        import cronexpr
        try:
            cronexpr.parse(schedule)
        except cronexpr.CronError as e:
            raise ConfigError(f"{sid}: schedule: {e}") from None
    try:
        trust_after = max(0, int(raw.get("trust_after") or 0))
    except (TypeError, ValueError):
        raise ConfigError(f"{sid}: trust_after must be a number") from None
    cfg = SourceConfig(
        id=sid, kind=kind, allow=allow, deny=deny,
        root=str(raw.get("root") or "").strip(), remote=str(raw.get("remote") or "").strip(),
        classification=levels.DEFAULT if lvl is None else lvl, project=project,
        enabled=bool(raw.get("enabled", True)),
        require_readonly=bool(raw.get("require_readonly", False)),
        trust_after=trust_after, trust_max_level=levels.DEFAULT if tml is None else tml,
        schedule=schedule, timezone=str(raw.get("timezone") or "").strip(),
        extensions=tuple(ext) if ext else DEFAULT_EXTENSIONS,
        limits=dict(raw.get("limits") or {}), options=dict(raw.get("options") or {}))
    from . import sources
    sources.validate(cfg)          # kind known + kind-specific rules (root absolute, remote…)
    return cfg


def load(path: Path | None = None) -> list[SourceConfig]:
    """Every declared source. A missing file = no source (the feature is on, nothing is read).
    An invalid file raises: a broken allow-list never degrades into « read everything »."""
    p = path or config_path()
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"{p}: not valid JSON ({e})") from None
    raw = doc.get("sources") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        raise ConfigError(f"{p}: expected {{\"sources\": [...]}}")
    out, seen = [], set()
    for r in raw:
        s = parse_source(r)
        if s.id in seen:
            raise ConfigError(f"duplicate source id {s.id}")
        seen.add(s.id)
        out.append(s)
    return out


def get(source_id: str) -> SourceConfig | None:
    return next((s for s in load() if s.id == source_id), None)


def limits_for(src: SourceConfig) -> Limits:
    base = Limits.from_env()
    over = {k: v for k, v in (src.limits or {}).items() if k in Limits.__dataclass_fields__}
    try:
        return Limits(**{**base.__dict__, **{k: type(getattr(base, k))(v)
                                             for k, v in over.items()}})
    except (TypeError, ValueError):
        raise ConfigError(f"{src.id}: invalid `limits`") from None
