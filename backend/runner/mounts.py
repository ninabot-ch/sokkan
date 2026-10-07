"""Which volumes a session container mounts, and where.

The api and its session containers share storage: the session works in the api's view of
its workspace (same absolute path, so Preview, the file views and the transcripts keep
working), and its Claude Code transcripts land where the api reads them (History, replay).
A session mounts ONLY:

* its working directory (the default project's workspace, or
  $SOKKAN_DATA_DIR/projects/<slug>/work for another project) — never the whole data volume;
* its transcript directory <CLAUDE_CONFIG_DIR>/projects/<cwd-slug> (+ auto-memory), mounted
  at the same place under the container's own ~/.claude.

`SOKKAN_RUNNER_MOUNTS` maps api-side path prefixes to what backs them, comma separated:
`/data=pvc:sokkan-data` (kubernetes), `/data=volume:sokkan_sokkan-data` or
`/workspace=bind:/srv/sokkan/workspace` (docker). The longest prefix wins.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .base import claude_project_slug

SESSION_HOME = "/home/session"


@dataclass(frozen=True)
class Mount:
    kind: str        # pvc | volume | bind
    source: str      # claim / volume name / host path
    subpath: str     # inside the source ("" = its root)
    target: str      # path in the container
    read_only: bool = False


def parse_mounts(raw: str | None) -> list[tuple[str, str, str]]:
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        prefix, src = item.split("=", 1)
        kind, _, name = src.partition(":")
        if kind not in ("pvc", "volume", "bind") or not name:
            raise ValueError(f"SOKKAN_RUNNER_MOUNTS: bad entry {item!r}")
        out.append((os.path.normpath(prefix), kind, name))
    return sorted(out, key=lambda t: -len(t[0]))


def resolve(path: str, table: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """(kind, source, subpath) backing an api-side path."""
    path = os.path.normpath(path)
    for prefix, kind, name in table:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            sub = os.path.relpath(path, prefix)
            if kind == "bind":
                return kind, os.path.normpath(os.path.join(name, sub)), ""
            return kind, name, "" if sub == "." else sub
    raise ValueError(f"no runner mount covers {path} (SOKKAN_RUNNER_MOUNTS)")


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def plan(cwd: str, table: list[tuple[str, str, str]], make_dirs: bool = True) -> list[Mount]:
    slug = claude_project_slug(os.path.normpath(cwd))
    transcripts = os.path.join(claude_dir(), "projects", slug)
    if make_dirs:  # created by the api (its uid), not by the kubelet / dockerd as root
        for d in (cwd, transcripts):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
    out = []
    for path, target in ((cwd, os.path.normpath(cwd)),
                         (transcripts, f"{SESSION_HOME}/.claude/projects/{slug}")):
        kind, src, sub = resolve(path, table)
        out.append(Mount(kind, src, sub, target))
    return out
