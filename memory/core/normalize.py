"""Bring a memory corpus back to ONE naming convention, and keep it there.

Invariant (core.notes)::

    file = <name with "-" replaced by "_">.md        name = kebab-case

Repairs, all lossless:

1. **merge**: a file without frontmatter whose normalised name is the one of an existing
   note is *appended* to that note, then removed (``cat >> wrong_name.md`` used to
   create a ghost note, and the update was invisible in the real one);
2. **YAML**: a frontmatter YAML rejects (almost always an unquoted ``description:``
   containing ": ") is rewritten, description as a ``>-`` block; text written
   above the frontmatter is moved below it;
3. **name / file**: a ``name:`` that is not kebab-case is derived from the file; a
   file that does not follow its name is renamed; ``[[old]]`` links and mentions of
   ``old_file.md`` are rewritten in the whole corpus;
4. **type**: a note without ``metadata.type`` gets ``feedback`` when its name starts
   with "feedback", ``project`` otherwise;
5. **links**: a ``[[link]]`` that targets nothing is re-attached when it designates a
   note unambiguously (old file name, case or separator variant).

A file touched less than ``grace`` seconds ago is never renamed nor merged: another
session may be writing it.

Everything is computed in memory first, so ``--dry-run`` shows the exact plan
(renames, merges, rewrites, re-linked notes) that applying would carry out::

    python -m core.normalize <memory_dir> --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import env, env_int
from .dates import content_hash
from .notes import (INDEX_FILENAME, LINK_RE, SLUG_RE, filename_for, parse_note, slugify)

DEFAULT_GRACE = 300


@dataclass
class Action:
    kind: str          # merge | rewrite | rename | relink | heal | manual | skipped-fresh
    path: str          # file concerned (name at the time of the action)
    target: str = ""   # merge target / new file name
    detail: str = ""

    def render(self) -> str:
        label = {
            "merge": f"merge     {self.path} -> {self.target}",
            "rewrite": f"rewrite   {self.path}: {self.detail}",
            "rename": f"rename    {self.path} -> {self.target}",
            "relink": f"relink    {self.path}: {self.detail}",
            "heal": f"heal      {self.path}: {self.detail}",
            "manual": f"MANUAL    {self.path}: {self.detail}",
            "skipped-fresh": f"later     {self.path}: {self.detail}",
        }
        return label.get(self.kind, f"{self.kind} {self.path} {self.detail}")


@dataclass
class Plan:
    memory_dir: str
    dry_run: bool
    actions: list[Action] = field(default_factory=list)
    # new body hash -> body hash before this pass, for every note whose body was only
    # rewritten mechanically (links, file mentions). core.dates uses it so that such
    # notes keep their date even when the rewrite changed a link *target*.
    hash_aliases: dict[str, str] = field(default_factory=dict)
    renamed: dict[str, str] = field(default_factory=dict)       # old file -> new file
    merged: dict[str, str] = field(default_factory=dict)        # orphan file -> target file
    applied: bool = False
    conflicts: list[str] = field(default_factory=list)          # files changed under us

    @property
    def changes(self) -> list[Action]:
        return [a for a in self.actions if a.kind not in ("manual", "skipped-fresh")]

    def summary(self) -> dict:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out

    def render(self) -> str:
        head = (f"normalize {'plan (dry run)' if self.dry_run else 'applied'} — "
                f"{self.memory_dir}")
        if not self.actions:
            return head + "\nnothing to do: the corpus follows the convention."
        order = ["merge", "rename", "rewrite", "relink", "heal", "manual", "skipped-fresh"]
        lines = [head]
        counts = self.summary()
        lines.append(", ".join(f"{counts[k]} {k}" for k in order if k in counts))
        for kind in order:
            for a in self.actions:
                if a.kind == kind:
                    lines.append("  " + a.render())
        for c in self.conflicts:
            lines.append(f"  CONFLICT  {c}: changed on disk during the pass, left untouched")
        return "\n".join(lines)


def dump_frontmatter(fm: dict) -> str:
    desc = " ".join(str(fm.get("description") or "").split())
    out = [f"name: {fm['name']}", "description: >-", f"  {desc}"]
    rest = {k: v for k, v in fm.items() if k not in ("name", "description")}
    if rest:
        out.append(yaml.safe_dump(rest, allow_unicode=True, sort_keys=False,
                                  width=10_000).rstrip())
    return "---\n" + "\n".join(out) + "\n---\n"


def _note_files(mem_dir: Path, index_filename: str) -> list[Path]:
    return sorted(p for p in mem_dir.glob("*.md") if p.name != index_filename)


def run(
    mem_dir: Path | str,
    *,
    dry_run: bool = False,
    grace: int = DEFAULT_GRACE,
    now: float | None = None,
    index_filename: str = INDEX_FILENAME,
) -> Plan:
    mem_dir = Path(mem_dir)
    now = time.time() if now is None else now
    plan = Plan(memory_dir=str(mem_dir), dry_run=dry_run)

    # ---- load: the whole pass works on this in-memory copy
    files = _note_files(mem_dir, index_filename)
    original: dict[str, str] = {}
    mtimes: dict[str, float] = {}
    for p in files:
        original[p.name] = p.read_text(encoding="utf-8", errors="replace")
        mtimes[p.name] = p.stat().st_mtime
    text = dict(original)              # current virtual content, keyed by current file name
    origin = {n: n for n in text}      # current name -> original name
    fresh = {n for n, mt in mtimes.items() if now - mt < grace}
    merged_into: set[str] = set()      # bodies that received real content

    def is_fresh(name: str) -> bool:
        return origin.get(name, name) in fresh

    # ---- 1. merge files without frontmatter into their namesake
    parsed = {n: parse_note(t, n) for n, t in text.items()}
    by_slug: dict[str, str] = {}
    for n, note in parsed.items():
        if note.fm is not None:
            by_slug.setdefault(slugify(n[:-3]), n)
            by_slug.setdefault(slugify(str(note.fm.get("name") or "")), n)
    for n, note in list(parsed.items()):
        if note.fm is not None:
            continue
        target = by_slug.get(slugify(n[:-3]))
        if not target or target == n:
            plan.actions.append(Action("manual", n, detail="no frontmatter and no namesake note"))
            continue
        if is_fresh(n) or is_fresh(target):
            plan.actions.append(Action("skipped-fresh", n, target,
                                       f"merge into {target} once nobody writes it"))
            continue
        plan.actions.append(Action("merge", n, target))
        plan.merged[n] = target
        text[target] = text[target].rstrip("\n") + "\n\n" + text[n].strip() + "\n"
        merged_into.add(target)
        del text[n]
        parsed.pop(n)
        parsed[target] = parse_note(text[target], target)

    cache: dict[tuple[str, str], object] = {(n, text[n]): parse_note(text[n], n) for n in text}

    # ---- 2/3/4. frontmatter, name, file, type
    renames_name: dict[str, str] = {}
    renames_file: dict[str, str] = {}
    taken = set(text)
    claimed: dict[str, str] = {}
    for n in sorted(parsed):
        note = parsed[n]
        fm = note.fm
        if fm is None:
            continue
        what: list[str] = []
        if not note.yaml_ok:
            what.append("YAML rewritten")
        if note.above:
            what.append("text above the frontmatter moved below")
        old_name = str(fm.get("name") or "").strip()
        new_name = old_name if (SLUG_RE.match(old_name or "-") and "_" not in old_name) \
            else slugify(n[:-3])
        if new_name != old_name:
            fm["name"] = new_name
            if old_name:
                renames_name[old_name] = new_name
            what.append(f"name '{old_name}' -> {new_name}")
        meta = fm.get("metadata")
        if not isinstance(meta, dict):
            meta = fm["metadata"] = {}
        if not meta.get("type") or meta.get("type") in ("unknown", "memory"):
            meta["type"] = "feedback" if new_name.startswith("feedback") else "project"
            what.append(f"type {meta['type']} added")
        if not str(fm.get("description") or "").strip():
            plan.actions.append(Action("manual", n, detail="empty description"))
        if what:
            plan.actions.append(Action("rewrite", n, detail=", ".join(what)))
            body = note.raw_body.lstrip("\n")
            if note.above:
                body = note.above + "\n\n" + body
            text[n] = dump_frontmatter(fm) + "\n" + body
        want = filename_for(new_name)
        if want in claimed and claimed[want] != n:
            plan.actions.append(Action("manual", n, detail=(
                f"name '{new_name}' already used by {claimed[want]}: duplicate note")))
            continue
        claimed[want] = n
        if want == n:
            continue
        if is_fresh(n):
            plan.actions.append(Action("skipped-fresh", n, want, f"rename to {want} later"))
            continue
        if want in taken:
            plan.actions.append(Action("manual", n, detail=f"should be {want}, already taken"))
            continue
        plan.actions.append(Action("rename", n, want))
        renames_file[n] = want
        taken.discard(n)
        taken.add(want)

    for old, new in renames_file.items():
        text[new] = text.pop(old)
        origin[new] = origin.pop(old)
        plan.renamed[origin[new]] = new

    # ---- links and file mentions across the corpus
    link_res = [(re.compile(r"\[\[\s*" + re.escape(o) + r"\s*((?:[|#][^\]]*)?)\]\]"), nw)
                for o, nw in renames_name.items()]
    file_res = [(re.compile(r"(?<![\w./-])" + re.escape(o) + r"(?![\w-])"), nw)
                for o, nw in renames_file.items()]
    if link_res or file_res:
        for n in sorted(text):
            t = text[n]
            t2 = t
            for rx, nw in link_res:
                t2 = rx.sub(lambda m, nw=nw: f"[[{nw}{m.group(1) or ''}]]", t2)
            for rx, nw in file_res:
                t2 = rx.sub(nw, t2)
            if t2 != t:
                text[n] = t2
                plan.actions.append(Action("relink", n, detail="links / file mentions rewritten"))

    # ---- 5. links that target nothing: re-attach the unambiguous ones

    def parsed_now(n: str, t: str):
        key = (n, t)
        if key not in cache:
            cache[key] = parse_note(t, n)
        return cache[key]

    names: dict[str, str] = {}
    for n, t in text.items():
        note = parsed_now(n, t)
        nm = note.name if note.fm is not None else slugify(n[:-3])
        names[nm] = nm
        names.setdefault(slugify(n[:-3]), nm)
        names.setdefault(slugify(nm), nm)
    for old, new in renames_name.items():
        names.setdefault(slugify(old), new)
    for old, new in renames_file.items():
        names.setdefault(slugify(old[:-3]), names.get(slugify(new[:-3]), slugify(new[:-3])))
    for n in sorted(text):
        fixed: list[str] = []

        def fix(m: re.Match, fixed=fixed) -> str:
            target = m.group(1).strip()
            if target in names:
                return m.group(0)
            hit = names.get(slugify(target.removesuffix(".md")))
            if not hit:
                return m.group(0)
            fixed.append(f"[[{target}]] -> [[{hit}]]")
            return f"[[{hit}{m.group(2)}]]"

        t2 = LINK_RE.sub(fix, text[n])
        if t2 != text[n]:
            text[n] = t2
            plan.actions.append(Action("heal", n, detail=", ".join(fixed)))

    # ---- hash aliases for bodies rewritten mechanically
    for n, t in text.items():
        src = origin.get(n, n)
        if src in merged_into or n in merged_into or src not in original or t == original[src]:
            continue
        before = content_hash(parsed_now(src, original[src]).body)
        after = content_hash(parsed_now(n, t).body)
        if before != after:
            plan.hash_aliases[after] = before

    if dry_run:
        return plan
    _apply(mem_dir, plan, original, mtimes, text, origin)
    return plan


def _apply(mem_dir: Path, plan: Plan, original: dict[str, str], mtimes: dict[str, float],
           text: dict[str, str], origin: dict[str, str]) -> None:
    def unchanged_on_disk(src: str) -> bool:
        p = mem_dir / src
        try:
            return p.stat().st_mtime == mtimes[src] and \
                p.read_text(encoding="utf-8", errors="replace") == original[src]
        except FileNotFoundError:
            return False

    touched = {origin.get(cur, cur) for cur, t in text.items()
               if cur != origin.get(cur, cur) or t != original.get(origin.get(cur, cur))}
    touched |= set(plan.merged) | set(plan.merged.values())
    blocked = {src for src in sorted(touched) if not unchanged_on_disk(src)}
    for src in sorted(blocked):
        plan.conflicts.append(src)
    blocked_targets = {plan.merged[o] for o in plan.merged if o in blocked}
    moves: list[tuple[str, str]] = []
    # 1. content, written in place; 2. merged orphans removed; 3. renames. In that
    # order: a note is often renamed onto the very file name of its orphan.
    for cur, t in text.items():
        src = origin.get(cur, cur)
        if src in blocked or src in blocked_targets:
            continue
        if t != original.get(src):
            (mem_dir / src).write_text(t, encoding="utf-8")
        if cur != src:
            moves.append((src, cur))
    for orphan, target in plan.merged.items():
        if orphan in blocked or target in blocked:
            continue
        (mem_dir / orphan).unlink(missing_ok=True)
    for src, cur in moves:
        if (mem_dir / cur).exists():
            plan.conflicts.append(f"{src} -> {cur}")
            continue
        os.replace(mem_dir / src, mem_dir / cur)
    plan.applied = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("memory_dir", nargs="?", default=env("MEMORY_DIR"))
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--grace", type=int,
                    default=env_int("NORMALIZE_GRACE", DEFAULT_GRACE))
    args = ap.parse_args(argv)
    if not args.memory_dir or not Path(args.memory_dir).is_dir():
        print("memory dir not found (argument or CORTHEXIS_MEMORY_DIR)", file=sys.stderr)
        return 1
    plan = run(args.memory_dir, dry_run=args.dry_run, grace=args.grace)
    print(plan.render())
    return 2 if plan.conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
