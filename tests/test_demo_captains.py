"""3.2.2 — « Captains » on the public read-only demo (feature `demo_captains`).

* the seed: fictional people only, 2-3 projects, Helm hierarchy with progress, one
  reframe suggestion, a « Shared with me » share to the visitor; idempotent; demo_crew's
  agents untouched;
* the visitor (a viewer) reads Helm, the board, the project selector, Setup › Engines and
  Organization — and writes NOTHING: every write answers 403 « read-only demo » and no
  store changes;
* no key, no secret, no real member in any answer the visitor gets.
"""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_ui_features import _FakeHttp


def _cards(project=None, include_archived=True):
    import board
    return [c for col in board.list_cards(include_archived=include_archived, project=project).values()
            for c in col]

SPEC = json.loads((Path(__file__).parent / "fixtures" / "demo_captains.json").read_text())
VISITOR = "demo@sokkan.ch"
SECRET = "sk-ant-api03-DEMO-MUST-NEVER-LEAK-9f8e7d6cQRST"
VAULT_VALUE = "pg-password-MUST-NEVER-LEAK-0042"
REAL_MEMBER = "ops.real@acme-corp.ch"
INTERNAL_URL = "https://llm.internal.acme-corp.ch/anthropic"


def _bow_embed(texts):
    # no embedding engine in tests: a deterministic bag of words (the drift detector runs)
    vocab: dict[str, int] = {}
    out = []
    for t in texts:
        v = [0.0] * 512
        for w in t.lower().split():
            v[vocab.setdefault(w, len(vocab)) % 512] += 1.0
        out.append(v)
    return out


@pytest.fixture()
def demo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import agents
    import app as a
    import audit
    import auth
    import board
    import helm
    import iam
    import llm
    import modelkeys
    import observability
    import sharing
    import usage
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_OWNER_EMAIL", VISITOR)
    for k in ("SOKKAN_DEMO_BANNER", "SOKKAN_DEMO_CAPTAINS", "SOKKAN_FEATURE_MULTI_PROJECT",
              "SOKKAN_FEATURE_HELM", "SOKKAN_FEATURE_ASSISTANT", "SOKKAN_FEATURE_SHARED_REVIEW",
              "SOKKAN_FEATURE_CONNECT_AI", "SOKKAN_FEATURE_BYOK_ADMIN", "SOKKAN_FEATURE_AGENTS",
              "SOKKAN_CREW_VIEWER_READONLY"):
        monkeypatch.setenv(k, "1")
    monkeypatch.setenv("SOKKAN_CONNECT_AI_MODE", "governed")
    for v in ("SOKKAN_GATEWAY_URL", "SOKKAN_GATEWAY_ADMIN_TOKEN", "SOKKAN_GATEWAY_CLIENT",
              "SOKKAN_INFER_BASE_URL", "SOKKAN_INFER_TOKEN", "ANTHROPIC_API_KEY",
              "CLAUDE_CODE_OAUTH_TOKEN", "SOKKAN_EDITION", "SOKKAN_ROUTER_WELCOME_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "viewer")
    iam.upsert_user(VISITOR, "viewer")
    iam.upsert_user("admin@x", "admin")
    iam.upsert_user(REAL_MEMBER, "dev", "Real Ops Person")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    helm.init(force=True)
    monkeypatch.setattr(helm, "_EMB_CACHE", {})
    monkeypatch.setattr(helm, "_embed", _bow_embed)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(observability, "DB", str(tmp_path / "incidents.db"))
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "PROJECT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(sharing, "DB", tmp_path / "shares.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    monkeypatch.setattr(modelkeys, "_http", _FakeHttp(200))
    board.add_sdk_session("d" * 32, "backend", title="ops: deployment hardening review", project="default")

    who = {"email": VISITOR}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    # real things on the instance the visitor must never see: a model key (through
    # Engines, with an internal base URL), a vault secret, a real member
    adm = as_("admin@x")
    r = adm.put("/api/connect-ai/policy", json={"allowed": ["claude", "sokkan_router"],
                                                "zones": {"claude": "EU", "sokkan_router": "CH"}})
    assert r.status_code == 200, r.text
    r = adm.put("/api/connect-ai/engines/claude", json={"auth": "key", "key": SECRET,
                                                         "base_url": INTERNAL_URL})
    assert r.status_code == 200, r.text
    vault.set_secret("PROD_DB_PASSWORD", VAULT_VALUE)
    import demo_captains
    rep = demo_captains.seed(copy.deepcopy(SPEC))
    return {"as": as_, "tmp": tmp_path, "report": rep, "dc": demo_captains}


# ---------------------------------------------------------------- seed

def test_seed_writes_the_fictional_organization(demo):
    import board
    import helm
    import projects
    import sharing
    rep = demo["report"]
    assert [p["slug"] for p in rep["projects"]] == ["player", "newsroom", "archives"]
    v = {"email": VISITOR, "role": "viewer"}
    for slug in ("player", "newsroom", "archives"):
        assert projects.effective_role(v, slug) == "viewer"
    # hierarchy with a computed progress that is neither 0 nor 1 on the main project
    player = next(p for p in rep["projects"] if p["slug"] == "player")
    assert 0 < player["progress"][0] < 1 and player["cards"] == 13
    deep = [c for c in _cards("player") if len(board.ancestors(c["id"])) == 2]
    assert len(deep) == 9
    # the reframe suggestion: the Kafka card contradicts the recorded decision
    sugg = helm.list_suggestions(["player"])
    assert any(s["kind"] == "contradiction" and "kafka" in json.dumps(s["evidence"]).lower() for s in sugg)
    # « Shared with me »: a read share of a default-project session, to the visitor
    inbox = sharing.inbox(v)
    assert len(inbox) == 1 and inbox[0]["access"] == "read" and inbox[0]["created_by"].endswith("@example.com")


def test_seed_is_idempotent_and_keeps_the_demo_crew(demo):
    import agents
    import projects
    import sharing
    aid = agents.create({"email": "admin@x", "role": "admin"}, {
        "name": "nightly-cve-audit", "purpose": "Audit the dependencies.",
        "deliverable": "A table.", "trigger": "manual", "tools": ["Read"]})["id"]
    before = len(_cards())
    rep2 = demo["dc"].seed(copy.deepcopy(SPEC))
    assert rep2["removed_cards"] == before - len(_cards("default"))
    assert len(_cards()) == before
    assert agents.get(aid) is not None
    assert len(sharing.inbox({"email": VISITOR, "role": "viewer"})) == 1
    assert len(projects.list_grants("player")) == 4          # 3 teams + the visitor
    assert sum(1 for t in projects.list_teams() if t["id"].startswith("local:demo-")) == 4


def test_seed_refuses_real_people_and_admins(demo):
    bad = copy.deepcopy(SPEC)
    bad["people"][0]["email"] = "someone@acme-corp.ch"
    with pytest.raises(demo["dc"].SeedError):
        demo["dc"].check(bad)
    bad = copy.deepcopy(SPEC)
    bad["people"][0]["role"] = "admin"
    with pytest.raises(demo["dc"].SeedError):
        demo["dc"].check(bad)
    bad = copy.deepcopy(SPEC)
    bad["projects"] = bad["projects"][:1]
    with pytest.raises(demo["dc"].SeedError):
        demo["dc"].check(bad)


# ---------------------------------------------------------------- what the visitor sees

def test_visitor_reads_helm_board_projects_and_setup(demo):
    c = demo["as"](VISITOR)
    f = c.get("/api/features").json()
    assert f["demo_captains"] is True and f["helm"] is True
    acc = c.get("/api/helm/access").json()
    assert acc["steers"] == [] and set(acc["reads"]) >= {"player", "newsroom", "archives"}
    assert acc["read_only"] is True
    deck = c.get("/api/helm/deck").json()
    assert len(deck["items"]) == 3
    card = deck["items"][0]["card"]["id"]
    assert c.get(f"/api/helm/cards/{card}").status_code == 200
    assert c.get("/api/helm/filters").json()["teams"]
    mine = c.get("/api/projects").json()
    slugs = {p["slug"] for p in (mine["projects"] if isinstance(mine, dict) else mine)}
    assert {"player", "newsroom", "archives"} <= slugs
    assert c.get("/api/board", headers={"x-sokkan-project": "player"}).status_code == 200
    org = c.get("/api/demo/organization").json()
    assert org["read_only"] and len(org["members"]) == 6 and len(org["teams"]) == 4
    assert {p["slug"] for p in org["projects"]} >= {"player", "newsroom", "archives"}
    eng = c.get("/api/connect-ai").json()
    claude = next(e for e in eng["engines"] if e["id"] == "claude")
    assert claude["connected"] and claude["connection"]["masked"] is None
    assert claude["connection"]["by"] == "" and claude["connection"]["base_url"] == ""
    assert claude["keys"] == [] and eng["can_admin"] is False


def _fingerprint(tmp: Path) -> dict:
    """Content of every store but the audit log and Nina's history."""
    out = {}
    for p in sorted(tmp.rglob("*")):
        if not p.is_file() or p.name.startswith(("audit", "assistant")) or p.suffix in (".db-wal", ".db-shm"):
            continue
        if p.suffix == ".db":
            con = sqlite3.connect(p)
            rows = []
            for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                rows.append((t, sorted(map(repr, con.execute(f'SELECT * FROM "{t}"').fetchall()))))
            con.close()
            out[str(p)] = hashlib.sha256(repr(rows).encode()).hexdigest()
        else:
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_visitor_writes_nothing(demo):
    import helm
    c = demo["as"](VISITOR)
    deck = c.get("/api/helm/deck").json()
    card = deck["items"][0]["card"]["id"]
    sid = helm.list_suggestions(["player"])[0]["id"]
    share = c.get("/api/shares/inbox").json()[0]["id"]
    before = _fingerprint(demo["tmp"])
    writes = [
        ("post", f"/api/helm/suggestions/{sid}/approve", None),
        ("post", f"/api/helm/suggestions/{sid}/ignore", None),
        ("post", f"/api/helm/cards/{card}/baseline", None),
        ("post", f"/api/helm/cards/{card}/suggestions/refresh", None),
        ("post", "/api/helm/projects", {"title": "x", "project": "player"}),
        ("delete", "/api/helm/calendar", None),
        ("post", "/api/board/card", {"title": "hack", "description": ""}),
        ("patch", f"/api/board/card/{card}", {"bucket": "Done"}),
        ("post", f"/api/board/card/{card}/comment", {"body": "hi"}),
        ("put", "/api/me/nav", {"last_plane": "setup"}),
        ("put", "/api/connect-ai/engines/claude", {"auth": "key", "key": "sk-x"}),
        ("delete", "/api/connect-ai/engines/claude/key", None),
        ("put", "/api/connect-ai/policy", {"allowed": []}),
        ("post", "/api/iam/users", {"email": "x@example.com", "role": "admin"}),
        ("post", "/api/admin/projects", {"slug": "evil", "name": "e"}),
        ("post", "/api/vault", {"name": "X", "value": "y"}),
        ("delete", f"/api/shares/{share}", None),
        ("post", "/api/shares", {"kind": "session", "target": "d" * 32, "principal_kind": "user",
                                 "principal": "lea.martin@example.com", "access": "read"}),
        ("post", "/api/spawn", {"tag": "backend", "prompt": "x"}),
        ("post", "/api/instance", {"org_name": "pwned"}),
    ]
    for method, url, body in writes:
        for project in (None, "player"):
            c = demo["as"](VISITOR, project)
            r = getattr(c, method)(url, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, url, project, r.status_code, r.text)
            assert r.json()["detail"] == "read-only demo", (url, r.text)
    assert _fingerprint(demo["tmp"]) == before
    # the operator (instance admin) is not a visitor: the guard lets them through
    assert demo["as"]("admin@x").put("/api/me/nav", json={"last_plane": "setup"}).status_code == 200


def test_no_key_secret_or_real_member_in_the_demo_answers(demo):
    import helm
    c = demo["as"](VISITOR)
    card = c.get("/api/helm/deck").json()["items"][0]["card"]["id"]
    share = c.get("/api/shares/inbox").json()[0]["id"]
    urls = ["/api/features", "/api/me", "/api/projects", "/api/connect-ai", "/api/demo/organization",
            "/api/helm/access", "/api/helm/deck", "/api/helm/filters", f"/api/helm/cards/{card}",
            f"/api/helm/cards/{card}/activity", f"/api/helm/cards/{card}/costs",
            "/api/shares/inbox", f"/api/shares/{share}", "/api/board", "/api/agents",
            "/api/agents/meta", "/api/connect-ai/crew-engines", "/api/instance", "/api/llm",
            "/api/admin/model-keys", "/api/vault", "/api/iam/users", "/api/admin/projects"]
    seen = []
    for project in (None, "player", "newsroom"):
        c = demo["as"](VISITOR, project)
        for u in urls:
            r = c.get(u)
            seen.append((u, r.status_code))
            body = r.text
            for needle in (SECRET, SECRET[-4:] + '"', "…" + SECRET[-4:], VAULT_VALUE, REAL_MEMBER,
                           "Real Ops Person", INTERNAL_URL, "admin@x"):
                assert needle not in body, (u, project, needle)
    ok = {u for u, s in seen if s == 200}
    assert {"/api/connect-ai", "/api/demo/organization", "/api/helm/deck", "/api/shares/inbox"} <= ok
    # admin-only routes stay closed to the visitor
    assert ("/api/admin/model-keys", 403) in seen and ("/api/vault", 403) in seen
    _ = helm


def test_feature_off_keeps_everything_as_before(demo, monkeypatch):
    monkeypatch.setenv("SOKKAN_DEMO_CAPTAINS", "0")
    c = demo["as"](VISITOR)
    assert c.get("/api/features").json()["demo_captains"] is False
    assert c.get("/api/demo/organization").status_code == 404
    assert c.get("/api/helm/deck").json()["items"] == []            # a viewer does not steer
    assert c.get("/api/helm/access").json()["read_only"] is False
    # without the banner the feature is off too (requires demo_banner)
    monkeypatch.setenv("SOKKAN_DEMO_CAPTAINS", "1")
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "0")
    assert c.get("/api/features").json()["demo_captains"] is False
    assert c.put("/api/me/nav", json={"last_plane": "setup"}).status_code == 200
