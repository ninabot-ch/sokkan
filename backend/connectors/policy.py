"""connectors.policy — what a connector may hand to a session's model, and from where.

Two rules, decided in this file only:

1. **Classification × zone.** The content a connector reads (a mail, an event, a document)
   enters the session of a PROJECT and goes to the model of that session. A project classified
   ``confidential`` or ``restricted`` (rank ≥ 3) gets content only when that model runs in
   Switzerland or on the machine (zone ``CH`` or ``local``). Anything else — a zone the
   instance does not know included — gets a refusal a person can read, never the content.
   ``SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE`` lowers the bar further (e.g. ``team``: only
   public and team projects reach a model outside Switzerland); it can never raise it.

2. **Where the data may come from.** A shared mailbox, a SharePoint site, a network folder
   are reachable from a project only when the administrator listed them for that project
   (`scopes`). A person's own mailbox and calendar are theirs in every project.
"""
from __future__ import annotations

import os

from . import ConnectorError

SAFE_ZONES = ("CH", "local")
CONFIDENTIAL = 3
HARD_MAX_OFFSHORE = CONFIDENTIAL - 1   # project (2) at most outside Switzerland


def project_level(project: str) -> int:
    import claudetier
    return claudetier.project_level(project or "default")


def max_level_offshore() -> int:
    from core import levels
    raw = os.environ.get("SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE")
    lv = levels.parse(raw) if raw and raw.strip() else None
    return min(HARD_MAX_OFFSHORE if lv is None else lv, HARD_MAX_OFFSHORE)


def session_zone(session_id: str = "", project: str = "") -> str:
    """Where the model of this session runs: CH | EU | US | local | '' (unknown).

    ``SOKKAN_CONNECTORS_MODEL_ZONE`` (the operator's declaration) wins; then a « Write with
    Claude » work (Anthropic, United States); then the engine of the session's project or the
    instance default (Connect your AI zones; an appliance = local)."""
    forced = (os.environ.get("SOKKAN_CONNECTORS_MODEL_ZONE") or "").strip()
    if forced:
        return forced if forced in ("CH", "EU", "US", "local") else ""
    try:
        import claudetier
        if session_id and claudetier.work_tier(session_id)[0] == "claude":
            return "US"
    except Exception:  # noqa: BLE001
        pass
    eid = ""
    try:
        import connectai
        eid = connectai.project_engine(project) or ""
    except Exception:  # noqa: BLE001
        eid = ""
    if not eid:
        try:
            import llm
            eid = (llm.load() or {}).get("engine") or ""
        except Exception:  # noqa: BLE001
            eid = ""
    if eid == "appliance":
        return "local"
    try:
        import connectai
        if eid in connectai.BY_ID:
            z = connectai.zone_of(eid)
            if z:
                return z
            if eid == "claude":
                return "US"
    except Exception:  # noqa: BLE001
        pass
    return ""


def content_allowed(project: str, zone: str) -> tuple[bool, str]:
    """May the content of a connector reach the model of a session of ``project`` running in
    ``zone``? → (allowed, reason a person can read)."""
    lvl = project_level(project)
    if zone in SAFE_ZONES:
        return True, ""
    if lvl <= max_level_offshore():
        return True, ""
    import claudetier
    where = {"EU": "dans l'Union européenne", "US": "aux États-Unis"}.get(
        zone, "dans une zone que l'instance ne connaît pas")
    return False, (f"Ce projet est classé « {claudetier.level_label(lvl)} » : le contenu de vos "
                   f"mails, agendas et documents ne part pas vers un modèle {where}. Il faut un "
                   "modèle en Suisse ou sur la machine (Réglages › Moteurs).")


def check_content(project: str, zone: str) -> None:
    ok, why = content_allowed(project, zone)
    if not ok:
        raise ConnectorError(why, 403, "classification")


# ---- what a project may reach -------------------------------------------------------------
def scopes(settings: dict, project: str) -> dict:
    """{shared_mailboxes, sites, folders} the admin opened for ``project`` (and for every
    project with the key ``*``)."""
    s = (settings.get("scopes") or {})
    out = {"shared_mailboxes": [], "sites": [], "folders": []}
    for key in ("*", project or "default"):
        for k in out:
            out[k] += [str(x) for x in (s.get(key) or {}).get(k) or []]
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def check_mailbox(settings: dict, project: str, person_login: str, mailbox: str) -> None:
    """A session reads the person's own mailbox, or a shared mailbox listed for the project."""
    m = (mailbox or "").strip().lower()
    if not m or m == (person_login or "").lower():
        return
    allowed = [x.lower() for x in scopes(settings, project)["shared_mailboxes"]]
    if m not in allowed:
        raise ConnectorError(
            f"La boîte « {mailbox} » n'est pas ouverte à ce projet. Un administrateur peut "
            "l'ajouter dans Réglages › Connecteurs.", 403, "mailbox_scope")


def check_path(settings: dict, project: str, provider: str, path: str) -> None:
    """A shared folder / site path is reachable when it is under one the admin listed for the
    project (no list = the whole drive of the person for graph/webdav, never for folder)."""
    p = "/" + (path or "").strip("/")
    if ".." in p.split("/"):
        raise ConnectorError("chemin invalide", 400, "path")
    key = "sites" if provider == "graph" else "folders"
    roots = scopes(settings, project)[key]
    if not roots:
        if provider == "folder":
            raise ConnectorError("Aucun dossier partagé n'est ouvert à ce projet.", 403, "path_scope")
        return
    for r in roots:
        r = "/" + r.strip("/")
        if r == "/" or p == r or p.startswith(r + "/"):
            return
    raise ConnectorError(f"« {path} » est hors des dossiers ouverts à ce projet.", 403, "path_scope")
