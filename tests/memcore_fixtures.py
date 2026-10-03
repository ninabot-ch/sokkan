"""Fictional memory corpus for the memory/core tests (no real notes)."""
from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime.datetime = T0):
        self.t = t

    def __call__(self) -> datetime.datetime:
        return self.t

    def advance(self, **kw) -> datetime.datetime:
        self.t = self.t + datetime.timedelta(**kw)
        return self.t


class FakeEmbedder:
    def __init__(self, ident: str = "fake:tiny@8", dim: int = 8):
        self.ident, self.dim, self.calls = ident, dim, 0

    def identity(self) -> str:
        return self.ident

    def embed_docs(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            d = hashlib.sha256(t.encode()).digest()
            out.append([b / 255 for b in d[: self.dim]])
        return out


def fm(name, description, *, type_="project", extra_meta="", top=""):
    meta = f"metadata:\n  type: {type_}\n{extra_meta}" if type_ else ""
    return f"{top}---\nname: {name}\ndescription: {description}\n{meta}---\n\n"


def write(d: Path, filename: str, text: str, when: datetime.datetime | None = None) -> Path:
    p = d / filename
    p.write_text(text, encoding="utf-8")
    ts = (when or T0 - datetime.timedelta(days=30)).timestamp()
    os.utime(p, (ts, ts))
    return p


def messy_corpus(d: Path) -> dict[str, Path]:
    """Every defect the normalizer repairs, in a corpus about a fictional café."""
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    # the real note uses dashes in its file name; a session appended to the
    # "convention" file name instead and created an orphan without frontmatter
    files["roaster"] = write(d, "coffee-roaster-setup.md", fm(
        "coffee-roaster-setup", "Roaster profile for the house blend") +
        "Medium roast, first crack at 9 minutes.\n")
    files["roaster_orphan"] = write(d, "coffee_roaster_setup.md",
                                    "UPDATE: the drum now preheats to 200 C.\n")
    # unquoted description with ": " breaks YAML; a dated note
    files["backup"] = write(d, "till_backup.md", fm(
        "till-backup", "Till backup: nightly at 02:00",
        extra_meta="  modified: '2026-02-10T08:00:00+00:00'\n") +
        "The till exports sales every night. See [[Garden Watering Plan]].\n")
    # text pasted above the frontmatter
    files["menu"] = write(d, "menu_board.md", fm(
        "menu-board", "Chalk menu board conventions",
        top="MAJ: oat milk is now free.\n\n") + "Prices in CHF, one line per drink.\n")
    # name not kebab: derived from the file
    files["garden"] = write(d, "garden.md", fm(
        "Garden Watering Plan", "Terrace plants watering rota") +
        "Water the olive trees on Mondays. Tide data in tides.md.\n")
    # kebab name, file does not follow it -> renamed, mentions rewritten
    files["tides"] = write(d, "tides.md", fm(
        "tide-tables", "Harbour tide tables for deliveries") +
        "Deliveries by boat only at high tide.\n")
    # links to heal (case/separator variants, old file name)
    files["harbor"] = write(d, "harbor_log.md", fm(
        "harbor-log", "Harbour delivery log") +
        "Check [[Tide_Tables]] and [[coffee_roaster_setup]] before ordering.\n")
    # missing type, a feedback note
    files["rule"] = write(d, "feedback_no_espresso_after_six.md", fm(
        "feedback-no-espresso-after-six", "Never serve espresso after 18:00", type_=None) +
        "Rule from the owner.\n")
    # an orphan nobody claims
    files["lonely"] = write(d, "random_thoughts.md", "Just some text.\n")
    return files


def snapshot(d: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.md"))}
