"""work_export_api.py — route of the `work_export` feature (backend/work_export.py).

* ``GET /api/admin/work-export``   the archive (zip) of what the caller may read: notes,
                                   agents, skills, instructions, connectors without their
                                   secrets, deliverables, README and manifest.

For the instance's admins only (403 otherwise); 404 while the feature is off. Every export
is written to the action journal: who, when, how many files of each kind, the SHA-256 of
the archive (also sent as the ``X-Sokkan-Export-Sha256`` response header). A refused export
(a vault value reached the archive, or the vault cannot be read) is journaled too.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

import audit
import auth
import features
import iam
import work_export

router = APIRouter()
PATH = "/api/admin/work-export"


def _on() -> None:
    if not features.enabled("work_export"):
        raise HTTPException(404, "feature disabled on this instance")


def _admin(u: dict = Depends(auth.current_user)) -> dict:
    if iam.rank(u.get("instance_role") or u["role"]) < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required")
    return u


@router.get(PATH)
def export(_f=Depends(_on), u: dict = Depends(_admin)):
    try:
        out = work_export.build(u)
    except work_export.ExportUnavailable as e:
        audit.log(u["email"], "work.export.refused", "", str(e), project="")
        raise HTTPException(503, str(e)) from e
    except work_export.ExportLeak as e:
        audit.log(u["email"], "work.export.refused", "", str(e), project="")
        raise HTTPException(500, str(e)) from e
    m = out["manifest"]
    counts = ", ".join(f"{n} {k}" for k, n in m["counts"].items() if n) or "nothing"
    audit.log(u["email"], "work.export", ",".join(p["slug"] for p in m["projects"]) or "-",
              f"{m['files_total']} files ({counts}), {m['bytes_total']} bytes, "
              f"sha256 {out['sha256']}", project="")
    return Response(out["zip"], media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{work_export.filename(m)}"',
        "X-Sokkan-Export-Sha256": out["sha256"],
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
