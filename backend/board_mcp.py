#!/usr/bin/env python3
"""board_mcp.py — SOKKAN : serveur MCP stdio pour que les sessions Claude Code
interagissent avec le studio elles-mêmes : créer/déplacer des cartes du board
(mêmes données que le web, sqlite board.db) et pousser leur WIP en aperçu
(l'onglet Preview s'ouvre sur ce que la session vient de modifier).

Enregistré dans .mcp.json (serveur `sokkan-board`). La session appelante est
identifiée via $TMUX_PANE (le serveur MCP hérite de l'env de claude, lancé
dans une fenêtre tmux SOKKAN) → attribution dans la timeline et l'audit.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit  # noqa: E402
import board  # noqa: E402
import previewenv  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sokkan-board")


def _session_ctx() -> dict:
    """Session SOKKAN appelante, résolue via la fenêtre tmux du process."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return {}
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_name}"],
            capture_output=True, text=True, timeout=3,
        )
        win = r.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return {}
    if not win:
        return {}
    for s in board.list_sessions():
        if s["window"] == win:
            return {"session_id": s["session_id"], "tag": s["tag"], "window": win}
    return {"window": win}


def _actor(ctx: dict) -> str:
    return f"session:{ctx.get('tag') or ctx.get('window') or 'inconnue'}"


@mcp.tool()
def create_card(title: str, tag: str = "backend", description: str = "",
                bucket: str = "Backlog", priority: int = 2) -> dict:
    """Crée une carte sur le board SOKKAN (apparaît dans l'onglet Board).

    Utiliser pour transformer une stratégie / un plan en tâches actionnables.
    Plus tard, depuis le web, « ▶ spawn » sur la carte ouvre une session pré-seedée.

    Args:
        title: titre court de la tâche.
        tag: domaine parmi la liste (backend, frontend, infra, seo, llm, …). Voir list_tags().
        description: le détail / prompt de la tâche (servira de seed au spawn).
        bucket: colonne (Backlog par défaut ; Doing/Review/Done possibles).
        priority: 0=urgente, 1=haute, 2=normale (défaut), 3=basse.
    """
    ctx = _session_ctx()
    card = board.add_card(title=title, description=description, tag=tag,
                          bucket=bucket, priority=priority, user=_actor(ctx))
    audit.log(_actor(ctx), "board.card.create", f"carte #{card['id']}", title)
    return card


@mcp.tool()
def move_card(card_id: int, bucket: str) -> dict:
    """Déplace une carte du board vers une autre colonne.

    Typiquement : passer SA carte en Review quand le travail est prêt à être
    validé (rien ne passe en Done sans validation humaine).

    Args:
        card_id: id de la carte (cf. list_board()).
        bucket: colonne cible parmi Backlog/Doing/Review/Done.
    """
    if bucket not in board.BUCKETS:
        return {"error": f"unknown bucket: {bucket} (valid: {board.BUCKETS})"}
    ctx = _session_ctx()
    card = board.update_card(card_id, user=_actor(ctx), bucket=bucket)
    if not card:
        return {"error": f"carte {card_id} introuvable"}
    audit.log(_actor(ctx), "board.card.move", f"carte #{card_id}", f"→ {bucket}")
    return card


@mcp.tool()
def open_preview(env: str, path: str = "/") -> dict:
    """Pousse le WIP de cette session dans l'onglet Preview de SOKKAN.

    Démarre (si besoin) le dev-server de l'environnement et signale à l'UI
    quoi afficher — appeler quand une modification visuelle est prête à être
    montrée, AVANT commit/push. L'humain la voit dans l'onglet Preview.

    Args:
        env: nom de l'environnement de preview (ex. « ninjob-frontend »).
             Les envs disponibles sont dans la config preview-envs.json.
        path: chemin à afficher (ex. « /dashboard »).
    """
    ctx = _session_ctx()
    try:
        t = previewenv.trigger(env, path=path, session_id=ctx.get("session_id", ""),
                               tag=ctx.get("tag", ""), window=ctx.get("window", ""),
                               user=_actor(ctx))
    except ValueError as e:
        envs = [x["name"] for x in previewenv.list_envs()]
        return {"error": str(e), "envs_disponibles": envs}
    audit.log(_actor(ctx), "preview.trigger", env, path)
    return {**t, "note": "aperçu signalé — visible dans l'onglet Preview de SOKKAN"}


@mcp.tool()
def list_tags() -> list[str]:
    """Liste les tags valides pour les cartes/sessions."""
    return board.TAGS


@mcp.tool()
def list_board() -> dict:
    """Retourne les cartes du board groupées par colonne (Backlog/Doing/Review/Done)."""
    return board.list_cards()


if __name__ == "__main__":
    mcp.run()
