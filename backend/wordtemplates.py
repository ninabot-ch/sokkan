#!/usr/bin/env python3
"""wordtemplates.py — the organisation's own Word templates (4.0 « native formats »).

An administrator of the instance drops a .dotx or .docx (logo, styles, page header and
footer): every Word file the Atelier produces is then written ON it (`outputs.to_docx`), and
the PDF takes its logo and footer. One template by default, and optionally one per kind of
document (« lettre », « offre », « pv »…): a deliverable names its kind in its front matter
(``template: offre``); a mail merge uses « lettre » when there is one.

Store: ``$SOKKAN_DATA_DIR/word-templates/<kind>.docx`` + ``index.json`` (original name, who,
when, SHA-256). The file is checked like a dropped document (a real Word package, no macro,
bounded size) and must open in python-docx.

Routes (instance admins; the list is readable by any signed-in person, without the files):

    GET    /api/admin/word-templates                 the templates
    PUT    /api/admin/word-templates/{kind}?name=…   body = the file (raw, like a document drop)
    GET    /api/admin/word-templates/{kind}/content  download it back
    DELETE /api/admin/word-templates/{kind}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

MAX_BYTES = 10 * 1024 * 1024
KIND = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def root() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))) / "word-templates"


def _index() -> dict:
    try:
        return json.loads((root() / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(ix: dict) -> None:
    root().mkdir(parents=True, exist_ok=True)
    tmp = root() / f".index.{uuid.uuid4().hex}.json"
    tmp.write_text(json.dumps(ix, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, root() / "index.json")


def norm_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    k = {"": "default", "défaut": "default", "defaut": "default", "letter": "lettre",
         "letters": "lettre", "lettres": "lettre"}.get(k, k)
    if not KIND.fullmatch(k):
        raise ValueError("a template name is 1-40 characters: a-z, 0-9, -")
    return k


def listing() -> list[dict]:
    ix = _index()
    out = []
    for k in sorted(ix, key=lambda x: (x != "default", x)):
        if (root() / f"{k}.docx").is_file():
            out.append({"kind": k, **{f: ix[k].get(f) for f in ("name", "by", "at", "sha256", "size")}})
    return out


def kinds() -> list[str]:
    return [t["kind"] for t in listing()]


def path_for(kind: str) -> Path | None:
    """The template for a kind of document: its own, else the default one, else None."""
    for k in ((norm_kind(kind) if kind and KIND.fullmatch((kind or "").strip().lower()) else ""), "default"):
        if k and (root() / f"{k}.docx").is_file():
            return root() / f"{k}.docx"
    return None


def store(kind: str, tmp: Path, raw_name: str, by: str) -> dict:
    """Check and keep an uploaded template (at ``tmp``, removed on refusal)."""
    import documents
    try:
        k = norm_kind(kind)
        name = (raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()[:120] or f"{k}.docx"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ("docx", "dotx"):
            raise documents.DocumentError(415, "type", "a Word template is a .dotx or .docx file")
        if tmp.stat().st_size > MAX_BYTES:
            raise documents.DocumentError(413, "too_large", "template larger than 10 MB")
        documents.check_content(tmp, "docx")
        import io
        import outputs
        try:
            from docx import Document  # noqa: F401
            doc, _ = outputs._open_template(tmp)
            doc.save(io.BytesIO())
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001 — not a Word file python-docx can write on
            raise documents.DocumentError(415, "disguised", f"this Word file cannot be used as a template ({e.__class__.__name__})")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    root().mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(tmp.read_bytes()).hexdigest()
    size = tmp.stat().st_size
    os.replace(tmp, root() / f"{k}.docx")
    ix = _index()
    ix[k] = {"name": name, "by": (by or "").lower(), "at": time.time(), "sha256": h, "size": size}
    _save_index(ix)
    return {"kind": k, **ix[k]}


def remove(kind: str) -> bool:
    k = norm_kind(kind)
    p = root() / f"{k}.docx"
    ix = _index()
    found = p.is_file() or k in ix
    p.unlink(missing_ok=True)
    ix.pop(k, None)
    _save_index(ix)
    return found


# ---- routes ----------------------------------------------------------------------------------
def router():
    import audit
    import auth
    import documents
    import iam

    r = APIRouter()

    def _admin(u: dict = Depends(auth.current_user)) -> dict:
        if iam.rank(u.get("instance_role") or u["role"]) < iam.rank("admin"):
            raise HTTPException(403, "role 'admin' required")
        return u

    @r.get("/api/admin/word-templates")
    def list_templates(_u: dict = Depends(auth.current_user)) -> dict:
        return {"templates": listing(), "max_bytes": MAX_BYTES}

    @r.put("/api/admin/word-templates/{kind}")
    async def put_template(kind: str, request: Request, name: str = "", u: dict = Depends(_admin)) -> dict:
        try:
            k = norm_kind(kind)
        except ValueError as e:
            raise HTTPException(400, {"code": "kind", "message": str(e)})
        root().mkdir(parents=True, exist_ok=True)
        tmp = root() / f".incoming-{uuid.uuid4().hex}.part"
        total = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise documents.DocumentError(413, "too_large", "template larger than 10 MB")
                    f.write(chunk)
            if not total:
                raise documents.DocumentError(400, "empty", "empty file")
            out = store(k, tmp, name, u["email"])
        except documents.DocumentError as e:
            tmp.unlink(missing_ok=True)
            audit.log(u["email"], "word_template.refused", k, f"{name}: {e.detail}", project="")
            raise HTTPException(e.status, {"code": e.code, "message": e.detail})
        audit.log(u["email"], "word_template.set", k, f"{out['name']} · sha256 {out['sha256'][:16]}", project="")
        return {"template": out, "templates": listing()}

    @r.get("/api/admin/word-templates/{kind}/content")
    def get_template(kind: str, _u: dict = Depends(_admin)):
        try:
            k = norm_kind(kind)
        except ValueError:
            raise HTTPException(404, "not found")
        p = root() / f"{k}.docx"
        if not p.is_file():
            raise HTTPException(404, "not found")
        name = (_index().get(k) or {}).get("name") or f"{k}.docx"
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        return FileResponse(p, media_type=documents.MIME["docx"], headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @r.delete("/api/admin/word-templates/{kind}")
    def delete_template(kind: str, u: dict = Depends(_admin)) -> dict:
        try:
            k = norm_kind(kind)
        except ValueError:
            raise HTTPException(404, "not found")
        if not remove(k):
            raise HTTPException(404, "not found")
        audit.log(u["email"], "word_template.remove", k, "", project="")
        return {"ok": True, "templates": listing()}

    return r
