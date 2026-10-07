#!/usr/bin/env python3
"""board_mcp.py — SOKKAN : serveur MCP stdio pour que les sessions Claude Code
interagissent avec le studio elles-mêmes : créer/déplacer des cartes du board
(mêmes données que le web, sqlite board.db) et pousser leur WIP en aperçu
(l'onglet Preview s'ouvre sur ce que la session vient de modifier).

Enregistré dans .mcp.json (serveur `sokkan-board`). La session appelante est
identifiée via $TMUX_PANE (le serveur MCP hérite de l'env de claude, lancé
dans une fenêtre tmux SOKKAN) → attribution dans la timeline et l'audit.

3.2 — the board is fully drivable from a session: get_card, search_cards (reads,
auto-approved), update_card, close_card / reopen_card, archive_card,
comment_card, link_card (writes: the session's normal permission gate, i.e. the
same policy as create_card / move_card; inside an agent run, only if the agent
was granted this server, and auto-approved only by its own `auto_approve`).
Every write is signed by the identity the API put in this process's env
(SOKKAN_SESSION_ID / SOKKAN_SESSION_USER, SOKKAN_AGENT_NAME in a run) — no tool
takes an author argument, so the model cannot choose who it is.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit  # noqa: E402
import board  # noqa: E402
import iam  # noqa: E402
import previewenv  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sokkan-board")


def _session_ctx() -> dict:
    """Session SOKKAN appelante. Session SDK (chat du cockpit, carte spawnée, run
    d'agent) : l'API pose SOKKAN_SESSION_ID dans l'env du serveur MCP
    (agentchat.mcp_servers_for) — prioritaire, car le process peut hériter du
    TMUX_PANE de l'API et désigner une fenêtre qui n'est pas la sienne. Session
    terminal : résolue via la fenêtre tmux du process."""
    sid = (os.environ.get("SOKKAN_SESSION_ID") or "").strip()
    if sid:
        for s in board.list_sessions():
            if s["session_id"] == sid:
                return {"session_id": sid, "tag": s["tag"] or "", "window": s["window"] or ""}
        return {"session_id": sid, "tag": "", "window": ""}
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
    who = ctx.get("tag") or ctx.get("window") or ctx.get("session_id", "")[:8]
    return f"session:{who or 'inconnue'}"


def _origin() -> tuple[str, dict]:
    """(who, origin) of a write. who = the agent in a Crew run, else the human
    driving the session (set by the API), else the session itself (terminal).
    origin = the SOKKAN session it comes from, for the card's history."""
    ctx = _session_ctx()
    human = (os.environ.get("SOKKAN_SESSION_USER") or "").strip().lower()
    agent = (os.environ.get("SOKKAN_AGENT_NAME") or "").strip()
    if os.environ.get("SOKKAN_AGENT_RUN") == "1" and agent:
        run_id = (os.environ.get("SOKKAN_AGENT_RUN_ID") or "").strip()
        who, via = f"agent:{agent}", f"agent-run #{run_id}" if run_id else "agent-run"
    elif "@" in human:
        who, via = human, "mcp"
    else:
        who, via = _actor(ctx), "mcp"
    return who, {"session_id": ctx.get("session_id", ""),
                 "session_tag": ctx.get("tag") or ctx.get("window", ""), "via": via}


def _write_denied() -> dict | None:
    """Defence in depth: a session driven by a known viewer does not write the
    board (the API's board routes need dev). System sessions (…@sokkan) and
    terminal sessions (no identity) keep the historical behaviour."""
    human = (os.environ.get("SOKKAN_SESSION_USER") or "").strip().lower()
    if "@" not in human or human.endswith("@sokkan"):
        return None
    u = iam.get_user(human)
    if u.get("known") and iam.rank(u["role"]) < iam.rank("dev"):
        return {"error": f"read-only: {human} is a {u['role']} on this SOKKAN, the board "
                         "needs dev or above"}
    return None


def _audit(who: str, origin: dict, action: str, card_id: int, detail: str = "") -> None:
    sess = origin.get("session_tag") or (origin.get("session_id") or "")[:8]
    audit.log(who, action, f"card #{card_id}",
              (detail + (f" \u00b7 session {sess}" if sess else "")).strip(" \u00b7"))


def _missing(card_id: int) -> dict:
    return {"error": f"card {card_id} not found (see search_cards / list_board)"}


def _project() -> str:
    """3.2 lot 3 — the board of THIS session's project. The API sets SOKKAN_SESSION_PROJECT
    in the server's environment; without it (a server started outside SOKKAN) = the
    default project while the instance has one, else no board at all (fail-closed)."""
    p = os.environ.get("SOKKAN_SESSION_PROJECT")
    if p is not None:
        return p.strip()
    import projects
    return "" if projects.multi_project() else projects.DEFAULT_PROJECT


def _foreign(card_id: int) -> bool:
    """A card of another project (or none at all): "not found" for this session."""
    c = board.get_card(card_id)
    return c is None or (c.get("project") or "default") != _project()


@mcp.tool()
def create_card(title: str, tag: str = "backend", description: str = "",
                bucket: str = "Backlog", priority: int = 2, parent_id: int | None = None) -> dict:
    """Crée une carte sur le board SOKKAN (apparaît dans l'onglet Board).

    Utiliser pour transformer une stratégie / un plan en tâches actionnables.
    Plus tard, depuis le web, « ▶ spawn » sur la carte ouvre une session pré-seedée.

    Args:
        title: titre court de la tâche.
        tag: domaine parmi la liste (backend, frontend, infra, seo, llm, …). Voir list_tags().
        description: le détail / prompt de la tâche (servira de seed au spawn).
        bucket: colonne (Backlog par défaut ; Doing/Review/Done possibles).
        priority: 0=urgente, 1=haute, 2=normale (défaut), 3=basse.
        parent_id: (Helm, 3.3) la carte sous laquelle ranger celle-ci (sous-tâche d'une carte,
            carte d'un projet) ; son intention, ses contraintes et ses décisions s'appliquent.
    """
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    if not _project():
        return {"error": "this session has no project: no board"}
    if parent_id is not None:
        if not _helm_on():
            return {"error": "card hierarchy needs the Helm feature (SOKKAN_FEATURE_HELM)"}
        if _foreign(parent_id):
            return _missing(parent_id)
    try:
        card = board.add_card(title=title, description=description, tag=tag,
                              bucket=bucket, priority=priority, user=who, origin=origin,
                              project=_project(), parent_id=parent_id)
    except ValueError as e:
        return {"error": str(e)}
    _audit(who, origin, "board.card.create", card["id"], title)
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
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    if bucket not in board.BUCKETS:
        return {"error": f"unknown bucket: {bucket} (valid: {board.BUCKETS})"}
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    card = board.update_card(card_id, user=who, origin=origin, bucket=bucket)
    if not card:
        return {"error": f"carte {card_id} introuvable"}
    _audit(who, origin, "board.card.move", card_id, f"→ {bucket}")
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
    return board.list_cards(project=_project())


@mcp.tool()
def get_card(card_id: int) -> dict:
    """Full detail of one board card: fields (title, description, tag, bucket,
    priority, due, assignee, checklist, archived, closed_at/closed_by), its
    comments, its history (who did what, when, from which session) and its links
    to sessions / agents / agent runs / incidents (with their current status).

    Args:
        card_id: id of the card (see search_cards() or list_board()).
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    d = board.card_detail(card_id)
    return d if d else _missing(card_id)


@mcp.tool()
def search_cards(query: str = "", tag: str = "", bucket: str = "", assignee: str = "",
                 include_archived: bool = False, limit: int = 30) -> dict:
    """Find board cards. All filters combine (AND); an empty filter is ignored.

    Args:
        query: words to find in the title, the description or the comments.
        tag: exact tag (see list_tags()).
        bucket: column, one of Backlog/Doing/Review/Done.
        assignee: an IAM user email, `agent:<name>`, or `none` for unassigned cards.
        include_archived: also return archived cards.
        limit: max results (default 30, max 200).
    """
    if bucket and bucket not in board.BUCKETS:
        return {"error": f"unknown bucket: {bucket} (valid: {board.BUCKETS})"}
    rows = board.search_cards(query, tag=tag, bucket=bucket, assignee=assignee,
                              include_archived=include_archived, limit=limit,
                              project=_project())
    keep = ("id", "title", "tag", "bucket", "priority", "due", "assignee", "archived",
            "closed_at", "session_id", "updated_at")
    cards = [{**{k: c.get(k) for k in keep},
              "excerpt": (c.get("description") or "")[:200]} for c in rows]
    return {"count": len(cards), "cards": cards}


_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _helm_on() -> bool:
    try:
        import features
        return features.enabled("helm")
    except Exception:  # noqa: BLE001
        return False


@mcp.tool()
def get_card_tree(card_id: int) -> dict:
    """(Helm) A card with everything above and below it: the breadcrumb of its parent
    cards with their intent / constraints / decisions (the context this work must
    respect), its sub-cards by column, and its computed progress (done / in progress /
    waiting / blocked, from the sub-cards and their live sessions, runs, incidents).

    Args:
        card_id: id of the card.
    """
    if not _helm_on():
        return {"error": "Helm is off on this instance (SOKKAN_FEATURE_HELM)"}
    if _foreign(card_id):
        return _missing(card_id)
    import helm
    d = helm.detail(card_id) or {}
    keep = ("id", "title", "kind", "bucket", "assignee", "intent", "constraints", "decisions",
            "parent_id", "due")
    return {"card": {k: d.get(k) for k in keep},
            "parents": [{k: a.get(k) for k in keep} for a in board.ancestors(card_id)],
            "progress": d.get("rollup"),
            "children": {b: [{"id": c["id"], "title": c["title"], "assignee": c.get("assignee"),
                              "state": (c.get("rollup") or {}).get("state")} for c in cs]
                         for b, cs in (d.get("kanban") or {}).get("cards", {}).items()},
            "context": helm.context_block(card_id)}


@mcp.tool()
def morning_brief(person: str = "", team: str = "") -> dict:
    """(Helm) The morning brief of this project, gathered by SOKKAN (read-only): cards that
    moved since the last working day, blocked cards, approvals waiting, Operate incidents,
    agents in error, recent decisions, today's agenda when a calendar is configured, and a
    markdown draft (`markdown`). Report only what it returns.

    Args:
        person: the email of the person the brief is for (their cards), or "".
        team: or a team id (`sso:<group>`) for its members' cards; both empty = the project.
    """
    if not _helm_on():
        return {"error": "Helm is off on this instance (SOKKAN_FEATURE_HELM)"}
    if not _project():
        return {"error": "this session has no project: no board"}
    import helm
    return helm.morning_brief(_project(), person=person.strip(), team=team.strip())


@mcp.tool()
def update_card(card_id: int, title: str | None = None, description: str | None = None,
                tag: str | None = None, priority: int | None = None, due: str | None = None,
                assignee: str | None = None, parent_id: int | None = None) -> dict:
    """Edit a board card. Only the arguments you pass change; the rest is kept.
    To change the column use move_card, to finish a card use close_card.

    Args:
        card_id: id of the card.
        title: new short title.
        description: new description (markdown; it seeds a session spawned from the card).
        tag: one of list_tags().
        priority: 0=urgent, 1=high, 2=normal, 3=low.
        due: due date YYYY-MM-DD, or "" to clear it.
        assignee: an IAM user email, `agent:<name>` of an existing agent, or "" to unassign.
        parent_id: (Helm) move the card under another card of the board, or 0 for the top
            level. A card cannot go under itself or under one of its own sub-cards.
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    fields: dict = {}
    new_parent: int | None = None
    if parent_id is not None:
        if not _helm_on():
            return {"error": "card hierarchy needs the Helm feature (SOKKAN_FEATURE_HELM)"}
        if parent_id and _foreign(parent_id):
            return _missing(parent_id)
        new_parent = int(parent_id)
    if title is not None:
        if not title.strip():
            return {"error": "title cannot be empty"}
        fields["title"] = title.strip()[:200]
    if description is not None:
        fields["description"] = description.strip()
    if tag is not None:
        if tag not in board.TAGS:
            return {"error": f"unknown tag: {tag} (valid: {board.TAGS})"}
        fields["tag"] = tag
    if priority is not None:
        if priority not in board.PRIORITIES:
            return {"error": "priority must be 0 (urgent), 1 (high), 2 (normal) or 3 (low)"}
        fields["priority"] = int(priority)
    if due is not None:
        if due and not _DUE_RE.match(due.strip()):
            return {"error": "due must be YYYY-MM-DD, or \"\" to clear it"}
        fields["due"] = due.strip()
    if assignee is not None:
        try:
            fields["assignee"] = board.validate_assignee(assignee, _project())
        except ValueError as e:
            return {"error": str(e)}
    if not fields and new_parent is None:
        return {"error": "nothing to update: pass at least one field"}
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    if new_parent is not None:
        try:
            board.set_parent(card_id, new_parent or None, user=who, origin=origin)
        except ValueError as e:
            return {"error": str(e)}
        fields["parent_id"] = new_parent
    card = board.update_card(card_id, user=who, origin=origin, **fields)
    if not card:
        return _missing(card_id)
    _audit(who, origin, "board.card.update", card_id, ", ".join(fields))
    return card


@mcp.tool()
def close_card(card_id: int, resolution: str = "") -> dict:
    """Close a card: it goes to Done and is marked finished (closed_at/closed_by).
    Closing is NOT deleting — the card, its comments and its history stay, and
    reopen_card brings it back. Close only work that is really finished and
    validated; otherwise move it to Review (move_card) for a human to validate.

    Args:
        card_id: id of the card.
        resolution: one line on how it ended (recorded in the card's history).
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    card = board.close_card(card_id, user=who, resolution=resolution, origin=origin)
    if not card:
        return _missing(card_id)
    _audit(who, origin, "board.card.close", card_id, resolution[:200])
    return card


@mcp.tool()
def reopen_card(card_id: int, bucket: str = "Backlog", reason: str = "") -> dict:
    """Reopen a closed (or archived) card into a column other than Done.

    Args:
        card_id: id of the card.
        bucket: column to reopen into: Backlog (default), Doing or Review.
        reason: why it is reopened (recorded in the card's history).
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    try:
        card = board.reopen_card(card_id, user=who, bucket=bucket, reason=reason, origin=origin)
    except ValueError as e:
        return {"error": str(e)}
    if not card:
        return _missing(card_id)
    _audit(who, origin, "board.card.reopen", card_id, f"→ {bucket}")
    return card


@mcp.tool()
def archive_card(card_id: int, reason: str = "") -> dict:
    """Archive a card: it leaves the board but is kept (get_card, search_cards with
    include_archived) and can be restored with reopen_card. Use it for cards that
    are obsolete or duplicates — not for finished work (close_card).

    Args:
        card_id: id of the card.
        reason: why it is archived (recorded in the card's history).
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    card = board.archive_card(card_id, user=who, reason=reason, origin=origin)
    if not card:
        return _missing(card_id)
    _audit(who, origin, "board.card.archive", card_id, reason[:200])
    return card


@mcp.tool()
def comment_card(card_id: int, body: str) -> dict:
    """Add a comment to a card (progress note, question, result, link to a PR…).
    The comment is signed by this session and the person driving it (or the
    agent, in an agent run); you cannot choose the author.

    Args:
        card_id: id of the card.
        body: the comment, markdown.
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    if not (body or "").strip():
        return {"error": "empty comment"}
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    c = board.add_comment(card_id, body, author=who, origin=origin)
    if not c:
        return _missing(card_id)
    _audit(who, origin, "board.card.comment", card_id, body.strip().splitlines()[0][:120])
    return c


@mcp.tool()
def link_card(card_id: int, kind: str, ref: str, remove: bool = False) -> dict:
    """Link a card to an object that exists in SOKKAN, or remove that link.

    Args:
        card_id: id of the card.
        kind: session | agent | run | incident | mr.
        ref: session id; agent id or name; agent run id; incident id; merge request URL.
             Linking to your own session: kind="session", ref="self".
        remove: true to remove the link instead.
    """
    if _foreign(card_id):  # 3.2: a card of another project does not exist here
        return _missing(card_id)
    if kind == "session" and ref.strip().lower() == "self":
        ref = _session_ctx().get("session_id", "")
        if not ref:
            return {"error": "this session is not a SOKKAN session: pass its id"}
    denied = _write_denied()
    if denied:
        return denied
    who, origin = _origin()
    try:
        out = board.link_card(card_id, kind, ref, user=who, remove=remove, origin=origin)
    except ValueError as e:
        return {"error": str(e)}
    if out is None:
        return _missing(card_id)
    _audit(who, origin, "board.card.unlink" if remove else "board.card.link", card_id,
           f"{kind} {ref}")
    return out


if __name__ == "__main__":
    mcp.run()
