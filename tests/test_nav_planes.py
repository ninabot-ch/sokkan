"""SOKKAN 3.2.2 — navigation by planes.

* the front's pure navigation module (frontend/lib/planes.ts): deep-link table (every 3.2.1
  `?tab=` still opens its place), visibility by role/feature, landing by role — run with
  node (type stripping), see tests/nav/planes_test.ts;
* `/api/me/nav`: the last plane, kept per user;
* Setup › Engines = « Connect your AI » + « Model keys » on one page: a key posed through
  Engines IS the key the Model keys route lists (one store), and the other way round.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_ui_features import SECRET_KEY, _FakeHttp
from test_ui_features import world as _world_fixture

world = _world_fixture

ROOT = Path(__file__).resolve().parent.parent


def test_front_navigation_module():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--test",
                        str(ROOT / "tests/nav/planes_test.ts")],
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "# fail 0" in r.stdout


def test_last_plane_is_kept_per_user(world):
    bob = world["as"]("bob@x")
    assert bob.get("/api/me/nav").json() == {"last_plane": None}
    assert bob.put("/api/me/nav", json={"last_plane": "operate"}).status_code == 200
    assert world["as"]("bob@x").get("/api/me/nav").json() == {"last_plane": "operate"}
    # someone else is not affected
    assert world["as"]("admin@x").get("/api/me/nav").json() == {"last_plane": None}
    assert world["as"]("bob@x").put("/api/me/nav", json={"last_plane": "nowhere"}).status_code == 400
    assert json.loads((world["tmp"] / "navprefs.json").read_text())["bob@x"]["last_plane"] == "operate"


def test_engines_and_model_keys_are_one_key(world, monkeypatch):
    import llm
    import modelkeys
    monkeypatch.setenv("SOKKAN_CONNECT_AI_MODE", "governed")
    http = _FakeHttp(200)
    monkeypatch.setattr(modelkeys, "_http", http)
    monkeypatch.setenv("SOKKAN_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("SOKKAN_GATEWAY_ADMIN_TOKEN", "gw-admin")
    monkeypatch.setenv("SOKKAN_GATEWAY_CLIENT", "acme")

    def adm():
        return world["as"]("admin@x")
    assert adm().put("/api/connect-ai/policy", json={
        "allowed": ["claude", "sokkan_router"], "zones": {"claude": "EU", "sokkan_router": "CH"},
        "allowed_zones": [], "tiers": [], "per_project": False}).status_code == 200
    # 1. posed through Setup › Engines (the Claude card)
    r = adm().put("/api/connect-ai/engines/claude", json={"auth": "key", "key": SECRET_KEY})
    assert r.status_code == 200, r.text
    assert SECRET_KEY not in r.text
    card = next(e for e in r.json()["engines"] if e["id"] == "claude")
    assert [(k["provider"], k["masked"], k["set_by"]) for k in card["keys"]] == [("anthropic", "…WXYZ", "admin@x")]
    # … is the very record the Model keys route lists
    mk = adm().get("/api/admin/model-keys").json()["keys"]
    assert [(k["provider"], k["masked"], k["set_by"], k["set_at"]) for k in mk] == \
        [("anthropic", card["keys"][0]["masked"], "admin@x", card["keys"][0]["set_at"])]
    assert ("PUT", "https://gw.example/admin/tenant/acme/byok") in [(c[0], c[1]) for c in http.calls]
    # the card's Test = the Model keys test (provider called once, key never echoed)
    t = adm().post("/api/connect-ai/engines/claude/test")
    assert t.status_code == 200 and t.json()["ok"] is True and SECRET_KEY not in t.text
    assert adm().get("/api/admin/model-keys").json()["keys"][0]["test_ok"] is True
    # a non-admin never sees the instance key on the cards
    v = world["as"]("bob@x").get("/api/connect-ai").json()
    assert all(not e["keys"] for e in v["engines"]) and "WXYZ" not in json.dumps(v)

    # 2. replaced through the old Model keys route → the Engines card shows the new one
    new_key = "sk-ant-api03-ANOTHER-ONE-9999QRST"
    assert adm().put("/api/admin/model-keys/anthropic", json={"key": new_key, "use_for_sessions": True}).status_code == 200
    card = next(e for e in adm().get("/api/connect-ai").json()["engines"] if e["id"] == "claude")
    assert card["keys"][0]["masked"] == "…QRST" and card["connected"] is True
    assert llm.session_env("x@y")["ANTHROPIC_API_KEY"] == new_key

    # 3. removed through Engines → gone for Model keys, engine disconnected, sessions lose it
    d = adm().delete("/api/connect-ai/engines/claude/key")
    assert d.status_code == 200, d.text
    assert adm().get("/api/admin/model-keys").json()["keys"] == []
    card = next(e for e in d.json()["view"]["engines"] if e["id"] == "claude")
    assert card["connected"] is False and card["keys"] == []
    assert "ANTHROPIC_API_KEY" not in llm.session_env("x@y")
    assert ("DELETE", "https://gw.example/admin/tenant/acme/byok/anthropic") in [(c[0], c[1]) for c in http.calls]
    assert adm().delete("/api/connect-ai/engines/claude/key").status_code == 404
    # admin only, unknown engine, wrong provider
    assert world["as"]("bob@x").post("/api/connect-ai/engines/claude/test").status_code == 403
    assert adm().post("/api/connect-ai/engines/nope/test").status_code == 404
    assert adm().post("/api/connect-ai/engines/claude/test?provider=openai").status_code == 400
