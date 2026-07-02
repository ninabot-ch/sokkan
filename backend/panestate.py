#!/usr/bin/env python3
"""panestate.py — SOKKAN : état temps-réel d'une session depuis le pane tmux.

Le transcript JSONL n'est écrit par Claude Code qu'APRÈS un tour traité (et pas
du tout tant qu'aucun prompt n'a été envoyé). La vue chat seule est donc soit en
retard, soit bloquée en « démarrage ». On lit ici directement le pane tmux où
claude tourne pour donner un SIGNE DE VIE (working / awaiting / idle) + un miroir
du terminal + les choix proposés par claude (menus de permission), le tout
parsable côté web.

Source de vérité = `tmux capture-pane` (l'écran que voit le terminal).
"""
from __future__ import annotations

import re
import subprocess
import time

# Marqueurs du footer de la BOÎTE DE SAISIE claude (= il attend une entrée).
# Présents uniquement quand l'input box est affichée (donc pas pendant un travail
# ni pendant un menu de permission, qui les remplacent).
_IDLE_FOOTER = re.compile(
    r"auto mode on|accept edits|plan mode|bypass permissions|← for agents|shift\+tab to cycle",
    re.IGNORECASE,
)
# Marqueur canonique de travail en cours.
_WORKING = re.compile(r"esc to interrupt|to interrupt", re.IGNORECASE)
# Une option de menu : « ❯ 1. Yes », « 2) … », avec curseur ❯/> optionnel.
_CHOICE = re.compile(r"^\s*([❯>›]?)\s*([1-9])[.)]\s+(\S.*)$")
# Shells = claude pas encore lancé (fenêtre fraîche).
_SHELLS = {"bash", "zsh", "sh", "fish", "dash"}

# change-detection léger inter-requêtes : target -> (hash_ecran, ts)
_last: dict[str, tuple[int, float]] = {}


def _capture(target: str, lines: int = 0) -> str | None:
    """Texte visible du pane (None si la fenêtre n'existe pas)."""
    cmd = ["tmux", "capture-pane", "-p", "-t", target]
    if lines:
        cmd += ["-S", f"-{lines}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _pane_command(target: str) -> str:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _strip_trailing_blank(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _parse_choices(screen_lines: list[str]) -> tuple[list[dict], str]:
    """Détecte un menu de permission/choix → liste d'options + libellé question.

    Exige ≥2 options numérotées consécutives pour éviter les faux positifs
    (un « 1. » qui traînerait dans une sortie d'outil).
    """
    choices: list[dict] = []
    selected = None
    for ln in screen_lines:
        m = _CHOICE.match(ln)
        if m:
            cursor, num, label = m.group(1), m.group(2), m.group(3).strip()
            # nettoie les artefacts de bordure de la boîte
            label = label.rstrip("│ ").strip()
            choices.append({"key": num, "label": label[:120]})
            if cursor:
                selected = num
        elif choices:
            # la série d'options s'est interrompue → on s'arrête au 1er bloc
            break
    if len(choices) < 2:
        return [], ""
    # la question = dernière ligne non vide AVANT la 1re option
    first_label = choices[0]["label"]
    question = ""
    for ln in screen_lines:
        s = ln.strip(" │").strip()
        if not s:
            continue
        if _CHOICE.match(ln) or first_label[:20] in ln:
            break
        question = s
    return choices, question


def _activity_line(screen_lines: list[str]) -> str:
    """Dernière ligne de spinner claude (« ✻ Crunching… », « esc to interrupt »)."""
    for ln in reversed(screen_lines):
        s = ln.strip()
        if not s:
            continue
        if _WORKING.search(s) or s.startswith(("✻", "✶", "·", "⠋", "●")):
            return s[:160]
        # une ligne ✻ « Crunched for … » reste à l'écran ; on la prend en repli
        if "✻" in s:
            return s[:160]
        break
    return ""


def classify(target: str, alive: bool | None = None) -> dict:
    """État d'une fenêtre tmux 'session:window' où tourne claude.

    Renvoie : state, activity, tail (miroir), choices[], question, changing.
    state ∈ {dead, booting, working, awaiting, idle}.
    """
    if alive is False:
        return {"state": "dead", "activity": "", "tail": "", "choices": [],
                "question": "", "changing": False}

    screen = _capture(target)
    if screen is None:
        return {"state": "dead", "activity": "", "tail": "", "choices": [],
                "question": "", "changing": False}

    lines = _strip_trailing_blank(screen.split("\n"))
    joined = "\n".join(lines)
    cmd = _pane_command(target)

    # change-detection (signe de vie robuste même si le wording du spinner change)
    now = time.time()
    h = hash(joined)
    prev = _last.get(target)
    changing = bool(prev and prev[0] != h and (now - prev[1]) < 8)
    _last[target] = (h, now)

    tail = "\n".join(lines[-30:])

    # 1) claude pas encore lancé (shell brut) → démarrage
    if cmd in _SHELLS or not lines:
        return {"state": "booting", "activity": "", "tail": tail, "choices": [],
                "question": "", "changing": changing}

    # 2) travail en cours (marqueur canonique)
    if _WORKING.search(joined):
        return {"state": "working", "activity": _activity_line(lines), "tail": tail,
                "choices": [], "question": "", "changing": True}

    # 3) boîte de saisie affichée → claude attend une ENTRÉE LIBRE (pas un menu).
    # Testé AVANT les choix : un vrai menu de permission REMPLACE ce footer ; s'il est
    # présent, une liste numérotée à l'écran n'est que du texte (faux positif → B:2).
    if _IDLE_FOOTER.search(joined):
        return {"state": "idle", "activity": "", "tail": tail, "choices": [],
                "question": "", "changing": False}

    # 4) menu de choix (permission / sélection) — uniquement en BAS de l'écran,
    # là où claude affiche réellement ses options.
    choices, question = _parse_choices(lines[-15:])
    if choices:
        return {"state": "awaiting", "activity": question or "choix en attente",
                "tail": tail, "choices": choices, "question": question,
                "changing": changing}

    # 5) repli : si l'écran bouge encore, c'est qu'il travaille ; sinon idle
    return {"state": "working" if changing else "idle",
            "activity": _activity_line(lines) if changing else "",
            "tail": tail, "choices": [], "question": "", "changing": changing}


def is_booting(target: str, alive: bool) -> bool:
    """Vrai uniquement tant que claude n'a pas démarré dans la fenêtre."""
    if not alive:
        return False
    cmd = _pane_command(target)
    return cmd in _SHELLS
