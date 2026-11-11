#!/usr/bin/env python3
"""Guard of the simple profile's design tokens (frontend/app/simple.css).

    python3 scripts/check-simple-tokens.py        # exit 1 with the list of what is wrong

* a colour, a pixel font size, a shadow or a duration is written only in the token blocks
  (`html[data-sk-theme…] { --sk-… }`); every rule after them uses `var(--sk-*)`;
* the simple profile's components (frontend/components/simple, frontend/lib/simple) carry
  `sk-*` classes only — no Tailwind utility, no literal colour, no pixel font size;
* floor: no font token under 12 px, body 14 px, targets 40 px;
* contrast (WCAG 2.x AA, 4.5:1) of every text level on every surface, of the text on the main
  action, of brass and sea on the page — in the light AND the dark theme;
* French and English dictionaries hold the same keys, none empty.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "frontend" / "app" / "simple.css"
DIRS = (ROOT / "frontend" / "components" / "simple", ROOT / "frontend" / "lib" / "simple")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
COLOR_FN = re.compile(r"\b(?:rgba?|hsla?)\(")


def _rgb(v: str):
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)), 1.0
    m = re.match(r"rgba?\(([^)]+)\)", v)
    if m:
        p = [x.strip() for x in m.group(1).split(",")]
        return tuple(int(float(x)) for x in p[:3]), float(p[3]) if len(p) > 3 else 1.0
    raise ValueError(v)


def _lum(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    (f, _), (b, _) = _rgb(fg), _rgb(bg)
    hi, lo = sorted((_lum(f), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def tokens(css: str, selector: str) -> dict[str, str]:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, flags=re.S)
    if not m:
        return {}
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: v.strip() for k, v in re.findall(r"(--sk-[\w-]+)\s*:\s*([^;]+);", body)}


def main() -> int:
    errs: list[str] = []
    css = CSS.read_text(encoding="utf-8")
    base = tokens(css, "html[data-sk-theme]")
    light = tokens(css, 'html[data-sk-theme="light"]')
    dark = tokens(css, 'html[data-sk-theme="dark"]')
    if not (base and light and dark):
        return print("token blocks not found in simple.css") or 1
    if set(light) != set(dark):
        errs.append(f"light and dark do not define the same tokens: {sorted(set(light) ^ set(dark))}")

    # 1. literals only inside the token blocks
    start = css.index("/* ================================== base")
    rules = re.sub(r"/\*.*?\*/", "", css[start:], flags=re.S)
    for n, line in enumerate(rules.splitlines(), 1):
        if HEX.search(line) or COLOR_FN.search(line):
            errs.append(f"simple.css (rules) literal colour: {line.strip()[:90]}")
        if re.search(r"font(?:-size)?\s*:[^;]*\b\d+(?:\.\d+)?px", line):
            errs.append(f"simple.css (rules) pixel font size: {line.strip()[:90]}")
        if re.search(r"(?:transition|animation)[^;]*\b\d+(?:\.\d+)?m?s\b", line) and "var(--sk-dur" not in line \
                and "sk-spin" not in line and "sk-pulse" not in line:
            errs.append(f"simple.css (rules) literal duration: {line.strip()[:90]}")
        if "cubic-bezier" in line:
            errs.append(f"simple.css (rules) a second motion curve: {line.strip()[:90]}")

    # 2. floors
    for k, v in base.items():
        if k.startswith("--sk-fs-") and float(v.rstrip("px")) < 12:
            errs.append(f"{k}: {v} is under the 12 px floor")
    if base.get("--sk-fs-md") != "14px":
        errs.append("--sk-fs-md (body) must be 14px")
    if float(base.get("--sk-target", "0").rstrip("px")) < 40:
        errs.append("--sk-target must be at least 40px")

    # 3. contrast, both themes
    for name, t in (("light", light), ("dark", dark)):
        pairs = [(fg, bg) for fg in ("--sk-text-strong", "--sk-text", "--sk-text-soft", "--sk-text-muted")
                 for bg in ("--sk-bg", "--sk-drawer", "--sk-surface", "--sk-raised", "--sk-sunken")]
        pairs += [("--sk-on-brass", "--sk-brass-fill"), ("--sk-on-brass", "--sk-brass-fill-hover")]
        pairs += [(fg, bg) for fg in ("--sk-brass", "--sk-sea", "--sk-ok", "--sk-warn", "--sk-danger")
                  for bg in ("--sk-bg", "--sk-surface", "--sk-drawer")]
        for fg, bg in pairs:
            try:
                c = contrast(t[fg], t[bg])
            except (KeyError, ValueError) as e:
                errs.append(f"{name}: {fg} on {bg}: unreadable token ({e})")
                continue
            if c < 4.5:
                errs.append(f"{name}: {fg} on {bg} = {c:.2f}:1 (< 4.5)")

    # 4. components: sk-* classes only, no literal colour / font size
    for d in DIRS:
        for f in sorted(d.rglob("*.ts*")):
            src = f.read_text(encoding="utf-8")
            rel = f.relative_to(ROOT)
            if f.name == "dict.ts":
                continue
            for m in re.finditer(r'className=(?:"([^"]*)"|\{`([^`]*)`\})', src):
                for cls in re.sub(r"\$\{[^}]*\}", " ", m.group(1) or m.group(2) or "").split():
                    if not re.fullmatch(r"(sk(-[\w-]+)?|is-[\w-]*|[gk])", cls):
                        errs.append(f"{rel}: class « {cls} » is not a simple-profile class")
            code = re.sub(r"//.*", "", src)
            if HEX.search(re.sub(r"&#\d+;", "", code)) or COLOR_FN.search(code):
                errs.append(f"{rel}: literal colour")
            if re.search(r"fontSize|font-size|\b\d+px\b", re.sub(r'd="[^"]*"', "", code)):
                errs.append(f"{rel}: pixel size written in a component")

    # 5. dictionaries
    dict_src = (ROOT / "frontend" / "lib" / "simple" / "dict.ts").read_text(encoding="utf-8")
    fr_part, en_part = dict_src.split("export const en", 1)
    keys = lambda s: re.findall(r'^\s*"([\w.]+)":\s*"((?:[^"\\]|\\.)*)"', s, flags=re.M)  # noqa: E731
    fr, en = dict(keys(fr_part)), dict(keys(en_part))
    if set(fr) != set(en):
        errs.append(f"dictionaries differ: {sorted(set(fr) ^ set(en))[:8]}")
    errs += [f"empty translation: {lang}.{k}" for lang, d in (("fr", fr), ("en", en)) for k, v in d.items() if not v.strip()]
    used = set()
    for d in DIRS:
        for f in d.rglob("*.tsx"):
            used |= set(re.findall(r'\bt\(\s*"([\w.]+)"', f.read_text(encoding="utf-8")))
    errs += [f"key used but not in the dictionary: {k}" for k in sorted(used - set(fr))]

    for e in errs:
        print("✗", e)
    if not errs:
        print(f"simple-profile tokens OK — {len(light)} theme tokens × 2, {len(fr)} sentences × 2 languages")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
