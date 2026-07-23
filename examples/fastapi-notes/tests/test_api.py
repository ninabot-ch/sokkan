"""Integration tests — real storage against a temp DB, no mocks
(the convention documented in memory note `testing-conventions`)."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_DB", str(tmp_path / "notes.db"))
    from app import main, storage
    importlib.reload(storage)
    return TestClient(main.app)


def test_healthz_convention(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "rev" in body


def test_error_envelope(client):
    r = client.get("/notes/999")
    assert r.status_code == 404
    assert set(r.json()["error"]) == {"code", "message"}


def test_create_requires_token(client):
    r = client.post("/notes", json={"title": "x"})
    assert r.status_code == 401


def test_create_and_get(client):
    r = client.post("/notes", json={"title": "hello", "body": "world"},
                    headers={"X-Notes-Token": "dev-token"})
    assert r.status_code == 201
    note = r.json()
    assert client.get(f"/notes/{note['id']}").json()["title"] == "hello"
