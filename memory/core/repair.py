"""One-click repairs of the memory, as proposals: compute, show the diff, apply on approval.

Nothing here writes until ``apply()``: a proposal is the complete list of file changes
(content before → after) with a unified diff per file, so the person who approves sees
exactly what will happen. ``apply()`` refuses (``Conflict``) if any file changed since
the proposal was computed — a sister session may have written it in the meantime —
and keeps a copy of every file it overwrites or removes in ``backup_dir``.

Four repairs, the ones three agents did by hand on 02.10.2026:

* ``relink``  — a broken ``[[old]]`` points to the note that replaced it, in every note
  that cites it (label and section kept);
* ``merge``   — note B is folded into note A (A's header kept, B's body appended under a
  heading), B is removed, links to B point to A;
* ``rename``  — a note gets back to the convention (kebab-case name, file
  ``<name with _>.md``), links are rewritten;
* ``close``   — a dormant project is closed: a dated line at the top of its body says so,
  its date is refreshed.

Links are only rewritten outside code spans and blocks (``[[x]]`` in code is an example).
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .notes import FM_RE, INDEX_FILENAME, filename_for, parse_note, slugify

CODE_SPLIT = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.S)
LINK_FULL = re.compile(r"\[\[([^\]|#]+)([^\]]*)\]\]")
CLOSE_STAMP = "> **Closed on {date}** — {reason}"


class RepairError(ValueError):
    pass


class Conflict(RepairError):
    """A file changed between the proposal and its approval."""


@dataclass
class FileChange:
    path: str                      # file name inside the memory folder
    before: str | None             # None = created
    after: str | None              # None = removed
    before_hash: str | None = None
    diff: str = ""

    def __post_init__(self):
        if self.before is not None and self.before_hash is None:
            self.before_hash = _h(self.before)
        if not self.diff:
            self.diff = "".join(difflib.unified_diff(
                (self.before or "").splitlines(keepends=True),
                (self.after or "").splitlines(keepends=True),
                fromfile=f"a/{self.path}" if self.before is not None else "/dev/null",
                tofile=f"b/{self.path}" if self.after is not None else "/dev/null"))


@dataclass
class Proposal:
    kind: str
    title: str
    summary: str
    changes: list[FileChange] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            h = hashlib.sha1(self.kind.encode())
            for c in self.changes:
                h.update(f"{c.path}\0{c.before_hash}\0{_h(c.after or '')}".encode())
            self.id = h.hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["files"] = len(self.changes)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        changes = [FileChange(**{k: c[k] for k in ("path", "before", "after", "before_hash",
                                                     "diff")}) for c in d.get("changes", [])]
        return cls(d["kind"], d["title"], d["summary"], changes, d.get("params") or {},
                   d.get("id", ""))


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _files(mem_dir: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(Path(mem_dir).glob("*.md")) if p.name != INDEX_FILENAME}


def _file_of(mem_dir: Path, name: str, files: dict[str, str] | None = None) -> str:
    """File name of note ``name`` (frontmatter name, else file stem)."""
    files = files if files is not None else _files(mem_dir)
    want = slugify(name)
    for fname, text in files.items():
        n = parse_note(text, fname)
        if n.name == name or slugify(n.name) == want:
            return fname
    raise RepairError(f"note not found: {name}")


def rewrite_links(text: str, mapping: dict[str, str]) -> str:
    """``[[old…]]`` → ``[[new…]]`` for every slug in ``mapping``, outside code."""
    keys = {slugify(k.removesuffix(".md")): v for k, v in mapping.items()}

    def fix(m: re.Match) -> str:
        target = keys.get(slugify(m.group(1).strip().removesuffix(".md")))
        return f"[[{target}{m.group(2)}]]" if target else m.group(0)

    parts = CODE_SPLIT.split(text)
    return "".join(p if i % 2 else LINK_FULL.sub(fix, p) for i, p in enumerate(parts))


def set_frontmatter_field(text: str, key: str, value: str, *, meta: bool = False) -> str:
    """Set ``key: value`` (top level, or under ``metadata:``) without reformatting the rest
    of the header. A file without header gets one."""
    m = FM_RE.search(text)
    line = f"{'  ' if meta else ''}{key}: {value}"
    if not m:
        block = f"metadata:\n{line}" if meta else line
        return f"---\n{block}\n---\n\n{text.lstrip()}"
    fm = m.group(1)
    if meta:
        rx = re.compile(rf"^(\s+){re.escape(key)}:.*$", re.M)
        meta_m = re.search(r"^metadata:\s*$", fm, re.M)
        if meta_m:
            rest = fm[meta_m.end():]
            # the metadata block = following indented lines
            blk = re.match(r"(?:\n[ \t]+[^\n]*)*", rest).group(0)
            if rx.search(blk):
                blk2 = rx.sub(lambda mm: f"{mm.group(1)}{key}: {value}", blk, count=1)
            else:
                blk2 = blk + "\n" + line
            fm2 = fm[:meta_m.end()] + blk2 + rest[len(blk):]
        else:
            fm2 = fm + f"\nmetadata:\n{line}"
    else:
        rx = re.compile(rf"^{re.escape(key)}:.*$", re.M)
        fm2 = rx.sub(line, fm, count=1) if rx.search(fm) else f"{line}\n{fm}"
    return text[:m.start(1)] + fm2 + text[m.end(1):]


def _today(now: datetime.datetime | None) -> str:
    return (now or datetime.datetime.now(datetime.timezone.utc)).date().isoformat()


# ---------------------------------------------------------------------------- the four repairs

def propose_relink(mem_dir: Path, target: str, new_target: str,
                   sources: list[str] | None = None) -> Proposal:
    files = _files(mem_dir)
    _file_of(mem_dir, new_target, files)     # the new target must exist
    changes = []
    for fname, text in files.items():
        n = parse_note(text, fname)
        if sources and n.name not in sources:
            continue
        new = rewrite_links(text, {target: new_target})
        if new != text:
            changes.append(FileChange(fname, text, new))
    if not changes:
        raise RepairError(f"no note links to [[{target}]] any more")
    return Proposal("relink", f"Point [[{target}]] to [[{new_target}]]",
                    f"{len(changes)} note(s) cite [[{target}]], which does not exist; the link "
                    f"will lead to “{new_target}”.", changes,
                    {"target": target, "new_target": new_target})


def propose_merge(mem_dir: Path, keep: str, drop: str,
                  now: datetime.datetime | None = None) -> Proposal:
    if slugify(keep) == slugify(drop):
        raise RepairError("cannot merge a note into itself")
    files = _files(mem_dir)
    fk, fd = _file_of(mem_dir, keep, files), _file_of(mem_dir, drop, files)
    tk, td = files[fk], files[fd]
    nk, nd = parse_note(tk, fk), parse_note(td, fd)
    dropped_body = rewrite_links(nd.body, {nd.name: nk.name}).strip()
    merged_section = f"\n\n## Merged from {nd.name}\n\n"
    if nd.description and nd.description != nk.description:
        merged_section += f"_{nd.description}_\n\n"
    merged_section += dropped_body + "\n"
    new_keep = tk.rstrip("\n") + merged_section
    new_keep = set_frontmatter_field(new_keep, "modified", _today(now), meta=True)
    new_keep = rewrite_links(new_keep, {nd.name: nk.name})
    changes = [FileChange(fk, tk, new_keep), FileChange(fd, td, None)]
    for fname, text in files.items():
        if fname in (fk, fd):
            continue
        new = rewrite_links(text, {nd.name: nk.name, fd[:-3]: nk.name})
        if new != text:
            changes.append(FileChange(fname, text, new))
    return Proposal("merge", f"Merge “{nd.name}” into “{nk.name}”",
                    f"The text of “{nd.name}” is appended to “{nk.name}”, then “{nd.name}” is "
                    f"removed; {len(changes) - 2} other note(s) are re-linked.", changes,
                    {"keep": nk.name, "drop": nd.name})


def propose_rename(mem_dir: Path, name: str, new_name: str | None = None) -> Proposal:
    files = _files(mem_dir)
    fname = _file_of(mem_dir, name, files)
    text = files[fname]
    n = parse_note(text, fname)
    new_name = slugify(new_name or n.name)
    if not new_name:
        raise RepairError("empty name")
    new_file = filename_for(new_name)
    if new_file != fname and new_file in files:
        raise RepairError(f"{new_file} already exists: merge the two notes instead")
    new_text = set_frontmatter_field(text, "name", new_name)
    new_text = rewrite_links(new_text, {n.name: new_name, fname[:-3]: new_name})
    changes = []
    if new_file == fname:
        if new_text == text:
            raise RepairError(f"{name} already follows the convention")
        changes.append(FileChange(fname, text, new_text))
    else:
        changes += [FileChange(fname, text, None), FileChange(new_file, None, new_text)]
    for other, t in files.items():
        if other == fname:
            continue
        nt = rewrite_links(t, {n.name: new_name, fname[:-3]: new_name})
        if nt != t:
            changes.append(FileChange(other, t, nt))
    return Proposal("rename", f"Rename “{n.name}” → “{new_name}”",
                    f"File {fname} becomes {new_file}; the links of "
                    f"{len([c for c in changes if c.before and c.after and c.path != fname])} "
                    "other note(s) follow.", changes, {"name": n.name, "new_name": new_name})


def propose_close(mem_dir: Path, name: str, reason: str = "",
                  now: datetime.datetime | None = None) -> Proposal:
    files = _files(mem_dir)
    fname = _file_of(mem_dir, name, files)
    text = files[fname]
    reason = " ".join((reason or "dormant project closed from the memory review; the open items "
                       "below are no longer followed.").split())
    stamp = CLOSE_STAMP.format(date=_today(now), reason=reason)
    m = FM_RE.search(text)
    if m:
        new = text[:m.end()] + "\n" + stamp + "\n\n" + text[m.end():].lstrip("\n")
    else:
        new = stamp + "\n\n" + text
    new = set_frontmatter_field(new, "modified", _today(now), meta=True)
    n = parse_note(text, fname)
    return Proposal("close", f"Close the project “{n.name}”",
                    "A dated line at the top of the note says the project is closed; its date "
                    "is refreshed so it no longer reads as pending.",
                    [FileChange(fname, text, new)], {"name": n.name, "reason": reason})


# ---------------------------------------------------------------------------- apply

def apply(mem_dir: Path, proposal: Proposal, backup_dir: Path | None = None) -> list[str]:
    """Write the proposal. All-or-nothing check first: any file whose content is not the
    one the proposal was computed on raises ``Conflict`` and nothing is written."""
    mem_dir = Path(mem_dir)
    for c in proposal.changes:
        p = mem_dir / c.path
        if Path(c.path).name != c.path or not c.path.endswith(".md"):
            raise RepairError(f"refusing path {c.path!r}")
        if c.before is None:
            if p.exists():
                raise Conflict(f"{c.path} appeared since the proposal")
        else:
            if not p.exists():
                raise Conflict(f"{c.path} disappeared since the proposal")
            if _h(p.read_text(encoding="utf-8", errors="replace")) != c.before_hash:
                raise Conflict(f"{c.path} changed since the proposal")
    if backup_dir is not None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = Path(backup_dir) / f"{stamp}-{proposal.kind}-{proposal.id}"
        dest.mkdir(parents=True, exist_ok=True)
        for c in proposal.changes:
            if c.before is not None:
                shutil.copy2(mem_dir / c.path, dest / c.path)
    written = []
    # creations and modifications first, removals last: a crash leaves a duplicate, never
    # a lost note
    for c in sorted(proposal.changes, key=lambda c: c.after is None):
        p = mem_dir / c.path
        if c.after is None:
            p.unlink()
        else:
            tmp = p.with_suffix(".md.tmp")
            tmp.write_text(c.after, encoding="utf-8")
            tmp.replace(p)
        written.append(c.path)
    return written
