"""hitl_api.py — export of the journal of human validations (backend/hitl_journal.py).

* ``GET /api/hitl/journal``  JSON (default) or ``?format=csv``; filters ``project``,
  ``actor`` (who decided, or whose session it was), ``session``, ``since`` / ``until``
  (ISO date or epoch seconds), ``decision``.

Readable by the instance's admins (every project) and by a project's admins for their own
project — the same people who read the action journal and the audited recall. Not behind a
feature flag: the journal is always on.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

import audit
import auth
import hitl_journal
import iam
import projects

router = APIRouter()


@router.get("/api/hitl/journal")
def journal(project: str = "", actor: str = "", session: str = "", since: str = "",
            until: str = "", decision: str = "", limit: int = 5000, format: str = "json",
            u: dict = Depends(auth.current_user)):
    try:
        t0, t1 = hitl_journal.parse_when(since), hitl_journal.parse_when(until)
    except ValueError:
        raise HTTPException(400, "since / until: an ISO date (2027-01-12) or epoch seconds")
    if iam.rank(u.get("instance_role") or u["role"]) >= iam.rank("admin"):
        scope = None                                   # instance admin: every project
    else:
        base = {**u, "role": u.get("instance_role") or u["role"]}
        mine = [p["slug"] for p in projects.list_projects()
                if projects.effective_role(base, p["slug"]) == "admin"]
        if not mine or (project and project not in mine):
            raise HTTPException(403, "the validations journal is for the admins of the "
                                     "instance, or of the project asked for")
        scope = mine
    rows = hitl_journal.query(project=project or None, projects=scope, actor=actor,
                              session_id=session, since=t0, until=t1, decision=decision,
                              limit=limit)
    audit.log(u["email"], "hitl.journal.read", project or "*",
              f"{len(rows)} rows" + (" (csv)" if format == "csv" else ""), project=project or "")
    if format == "csv":
        return PlainTextResponse(
            hitl_journal.to_csv(rows), media_type="text/csv",
            headers={"content-disposition": 'attachment; filename="validations.csv"'})
    return {"entries": [hitl_journal.public(r) for r in rows], "count": len(rows)}
