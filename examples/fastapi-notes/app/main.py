"""fastapi-notes — tiny team-notes API used as a SOKKAN example workspace.

Run it the way the memory note `run-and-port` says:

    uvicorn app.main:app --port 8734 --reload
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import storage

app = FastAPI(title="fastapi-notes")

AUTH_TOKEN = os.environ.get("NOTES_TOKEN", "dev-token")
GIT_REV = os.environ.get("GIT_REV", "dev")


class NoteIn(BaseModel):
    title: str
    body: str = ""


@app.exception_handler(HTTPException)
async def error_envelope(_request: Request, exc: HTTPException) -> JSONResponse:
    # convention: every error body is {"error": {"code", "message"}}
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": exc.status_code, "message": exc.detail}})


def require_token(x_notes_token: str | None) -> None:
    if x_notes_token != AUTH_TOKEN:
        raise HTTPException(401, "missing or invalid X-Notes-Token")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "rev": GIT_REV}


@app.get("/notes")
def list_notes() -> list[dict]:
    return storage.list_notes()


@app.get("/notes/{note_id}")
def get_note(note_id: int) -> dict:
    note = storage.get_note(note_id)
    if not note:
        raise HTTPException(404, f"note {note_id} not found")
    return note


@app.post("/notes", status_code=201)
def create_note(body: NoteIn, x_notes_token: str | None = Header(default=None)) -> dict:
    require_token(x_notes_token)
    if not body.title.strip():
        raise HTTPException(422, "title must not be empty")
    return storage.add_note(body.title.strip(), body.body)
