"""/api/spawn — garde-fous côté API (pas de modèle, pas de session réelle).

Le playbook est rendu AVANT tout spawn : une requête refusée ici ne crée rien.
"""
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp()
os.environ.update(
    SOKKAN_DATA_DIR=_TMP, CLAUDE_CONFIG_DIR=f"{_TMP}/claude",
    SOKKAN_MEMORY_DIR=f"{_TMP}/memory", SOKKAN_AGENT_CWD=_TMP,
    SOKKAN_LOCAL_TOKEN="", SOKKAN_OWNER_EMAIL="owner@localhost",
    SOKKAN_UPDATE_CHECK="0",
)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """App réelle, état sur disque jetable. Les bases sont posées explicitement :
    l'ordre des modules de test ne doit pas décider de ce que voit celui-ci."""
    from fastapi.testclient import TestClient

    import app as a
    import board
    import iam

    d = tmp_path_factory.mktemp("spawn-api")
    saved = {"iam": iam.DB, "board": board.DB}
    iam.DB, board.DB = d / "iam.db", d / "board.db"
    iam.init(force=True)
    board.init(force=True)
    try:
        # sans context manager : pas de startup (ni thread de réindexation)
        yield TestClient(a.app)
    finally:
        iam.DB, board.DB = saved["iam"], saved["board"]


def test_playbook_without_subject_is_refused_and_spawns_nothing(client):
    """« Bug to investigate: » tout court = session lancée à l'aveugle (vu sur
    une mission blanche : l'humain a dû tout re-prompter à la main)."""
    r = client.post("/api/spawn", json={"playbook": "debug", "prompt": ""})
    assert r.status_code == 400
    assert "needs a subject" in r.json()["detail"]
    assert "Debug" in r.json()["detail"]  # le label, pas l'id
    r = client.post("/api/spawn", json={"playbook": "refactor", "prompt": "   "})
    assert r.status_code == 400


def test_unknown_playbook_still_400(client):
    r = client.post("/api/spawn", json={"playbook": "nope", "prompt": "x"})
    assert r.status_code == 400 and "unknown playbook" in r.json()["detail"]


def test_subject_optional_playbooks_are_not_blocked(client, monkeypatch):
    """`digest` / `onboard-memory` n'ont pas de sujet par construction — le
    garde-fou ne doit pas les fermer. On coupe le spawn juste après le rendu."""
    import app as a

    seen = {}

    def _fake_spawn(tag, prompt="", title="", user=""):
        seen.update(tag=tag, prompt=prompt, title=title)
        return {"session_id": "sid", "title": title, "tag": tag}

    monkeypatch.setattr(a, "_spawn_sdk", _fake_spawn)
    r = client.post("/api/spawn", json={"playbook": "digest", "prompt": ""})
    assert r.status_code == 200
    assert "project-status" in seen["prompt"]
