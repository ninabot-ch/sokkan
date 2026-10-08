"""GET /api/version — auth-free, says what the RUNNING process is (3.2.3 rollout check)."""
from pathlib import Path


def test_version_is_auth_free_and_matches_the_tree(monkeypatch):
    from fastapi.testclient import TestClient

    import app as a

    r = TestClient(a.app).get("/api/version")
    assert r.status_code == 200
    j = r.json()
    want = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert j["version"] == want
    assert j["edition"] in ("community", "enterprise")
    assert set(j) == {"version", "commit", "dist", "image_tag", "edition"}
    if j["commit"] != "unknown":
        assert j["dist"] == f"{want}+{j['commit']}"


def test_version_payload_has_no_secret_or_config(monkeypatch):
    from fastapi.testclient import TestClient

    import app as a

    body = TestClient(a.app).get("/api/version").text.lower()
    for leak in ("token", "secret", "key", "password", "http"):
        assert leak not in body
