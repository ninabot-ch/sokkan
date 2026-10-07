"""3.2 — feature registry (backend/features.py): one app, features switched one by one,
dependencies declared, an invalid combination turns the faulty feature OFF (never a crash,
never half on), the legacy variables keep working, the edition only changes defaults."""
import subprocess
import sys
from pathlib import Path

import pytest

import features as F

ROOT = Path(__file__).resolve().parent.parent
ALL_VARS = sorted({v.name for f in F.REGISTRY for v in f.vars} | {"SOKKAN_EDITION", "SOKKAN_TIER"})


@pytest.fixture()
def clean_env(monkeypatch):
    """No switch from the host or the CI leaks into these tests."""
    for v in ALL_VARS:
        monkeypatch.delenv(v, raising=False)
    import observability
    monkeypatch.setattr(observability, "ENABLED", False)
    monkeypatch.setenv("SOKKAN_AUTH_MODE", "local")
    return monkeypatch


# ---- registry consistency -------------------------------------------------------------
def test_registry_is_consistent():
    assert F.validate_registry() == []
    ids = [f.id for f in F.REGISTRY]
    assert len(ids) == len(set(ids))
    for f in F.REGISTRY:
        assert f.title and f.description
        assert all(d in F.BY_ID for d in f.requires + f.conflicts)


def test_validation_detects_cycles_unknown_deps_and_one_sided_conflicts(monkeypatch):
    a = F.Feature("a", "A", "a", requires=("b",), vars=(F.Var("SOKKAN_FEATURE_A"),))
    b = F.Feature("b", "B", "b", requires=("a",), conflicts=("c",), vars=(F.Var("SOKKAN_FEATURE_B"),))
    c = F.Feature("c", "C", "c", requires=("ghost",), vars=(F.Var("SOKKAN_FEATURE_C"),))
    monkeypatch.setattr(F, "REGISTRY", (a, b, c))
    monkeypatch.setattr(F, "BY_ID", {"a": a, "b": b, "c": c})
    errs = "\n".join(F.validate_registry())
    assert "cycle" in errs and "unknown feature ghost" in errs and "both sides" in errs
    # a cycle at runtime still never crashes: the feature is off, with the reason
    st = F._resolve_one("a", {"SOKKAN_FEATURE_A": "1", "SOKKAN_FEATURE_B": "1"}, {}, ())
    assert st.enabled is False and "cycle" in st.reason


def test_every_shipped_switch_reaches_the_api_container():
    import yaml
    env = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["api"]["environment"]
    missing = [v for v in F.all_vars() if v not in env]
    assert not missing, f"declare in services.api.environment: {missing}"
    # and every setting a feature reads to decide it is configured (SSO, GitLab, Teams…)
    cfg = sorted({c for f in F.REGISTRY for c in f.config} - set(env))
    assert not cfg, f"declare in services.api.environment: {cfg}"


def test_generated_doc_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-features-doc.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + " — run scripts/gen-features-doc.py"


# ---- no regression: community defaults = the behaviour before the registry ------------
def test_community_defaults_are_the_previous_behaviour(clean_env):
    st = F.resolve()
    on = {k for k, s in st.items() if s.enabled}
    assert on == {"tmux", "preview", "magnitude", "missions_link", "update_check",
                  "named_secrets", "memory_quarantine", "agents"}
    assert F.edition() == "community"
    import agents
    import vault
    assert agents.approval_mode() == "owner" and vault.session_mode() == "named"
    assert not F.problems()


LEGACY = [  # (env, feature, expected) — the semantics each variable had before
    ({"SOKKAN_FEATURE_TMUX": "0"}, "tmux", False),
    ({"SOKKAN_FEATURE_TMUX": "false"}, "tmux", True),   # historical: only "0" turned it off
    ({"SOKKAN_FEATURE_PREVIEW": "0"}, "preview", False),
    ({"SOKKAN_FEATURE_MAGNITUDE": "0"}, "magnitude", False),
    ({"SOKKAN_FEATURE_ASSISTANT": "1"}, "assistant", True),
    ({"SOKKAN_FEATURE_MISSIONS_LINK": "0"}, "missions_link", False),
    ({"SOKKAN_UPDATE_CHECK": "no"}, "update_check", False),
    ({"SOKKAN_PREVIEW_ALLOW_PRIVATE": "1"}, "preview_private_targets", True),
    ({"SOKKAN_PREVIEW_ALLOW_PRIVATE": "true"}, "preview_private_targets", False),  # was == "1"
    ({"SOKKAN_FEATURE_AGENTS": "0"}, "agents", False),
    ({"SOKKAN_AGENTS_USE_CLI_LOGIN": "1"}, "agents_cli_login", True),
    ({"SOKKAN_AGENTS_APPROVAL": "four_eyes"}, "four_eyes", True),
    ({"SOKKAN_AGENTS_APPROVAL": "admin"}, "admin_approval", True),
    ({"SOKKAN_AGENTS_INCIDENTS": "1"}, "agent_incidents", True),   # forced without Operate
    ({"SOKKAN_CREW_VIEWER_READONLY": "yes"}, "crew_viewer_readonly", True),
    ({"SOKKAN_DEMO_BANNER": "1"}, "demo_banner", True),
    ({"SOKKAN_DEMO_BANNER": "1", "SOKKAN_DEMO_CREW": "1"}, "demo_crew", True),
    ({"SOKKAN_SESSION_SECRETS": "all"}, "named_secrets", False),
    ({"SOKKAN_SESSION_SECRETS": "bogus"}, "named_secrets", True),  # unknown = most restrictive
]


@pytest.mark.parametrize("env,fid,expected", LEGACY)
def test_legacy_variables_keep_their_meaning(clean_env, env, fid, expected):
    for k, v in env.items():
        clean_env.setenv(k, v)
    assert F.enabled(fid) is expected


def test_legacy_modules_follow_the_registry(clean_env):
    import agents
    import agents_runtime
    import demo_crew
    import vault
    clean_env.setenv("SOKKAN_AGENTS_APPROVAL", "admin")
    assert agents.approval_mode() == "admin"
    clean_env.setenv("SOKKAN_AGENTS_APPROVAL", "garbage")
    assert agents.approval_mode() == "owner"
    clean_env.setenv("SOKKAN_SESSION_SECRETS", "all")
    assert vault.session_mode() == "all"
    clean_env.setenv("SOKKAN_DEMO_CREW", "1")
    assert demo_crew.requested() and not demo_crew.enabled()   # banner missing
    clean_env.setenv("SOKKAN_DEMO_BANNER", "1")
    assert demo_crew.enabled() and agents_runtime.demo_mode()


def test_canonical_name_wins_over_the_legacy_one(clean_env):
    clean_env.setenv("SOKKAN_AGENTS_APPROVAL", "four_eyes")
    clean_env.setenv("SOKKAN_FEATURE_FOUR_EYES", "0")
    assert F.enabled("four_eyes") is False
    st = F.resolve()["four_eyes"]
    assert st.var == "SOKKAN_FEATURE_FOUR_EYES" and not st.legacy
    clean_env.setenv("SOKKAN_FEATURE_FOUR_EYES", "")      # empty = unset → legacy decides
    st = F.resolve()["four_eyes"]
    assert st.enabled and st.legacy and "legacy name" in st.reason


# ---- editions -------------------------------------------------------------------------
def test_enterprise_edition_defaults(clean_env):
    clean_env.setenv("SOKKAN_EDITION", "enterprise")
    st = F.resolve()
    assert st["four_eyes"].enabled and st["multi_project"].enabled
    assert not st["missions_link"].enabled and not st["admin_approval"].enabled
    assert st["named_secrets"].enabled and st["agents"].enabled
    import agents
    assert agents.approval_mode() == "four_eyes"
    clean_env.setenv("SOKKAN_AGENTS_APPROVAL", "owner")   # an explicit choice beats the edition
    assert agents.approval_mode() == "owner"
    clean_env.setenv("SOKKAN_EDITION", "platinum")        # unknown edition = community
    assert F.edition() == "community" and not F.enabled("multi_project")


def test_managed_instance_forces_named_secrets(clean_env):
    clean_env.setenv("SOKKAN_SESSION_SECRETS", "all")
    clean_env.setenv("SOKKAN_FEATURE_NAMED_SECRETS", "0")
    clean_env.setenv("SOKKAN_TIER", "starter")
    st = F.resolve()["named_secrets"]
    assert st.enabled and st.source == "forced"


# ---- invalid combinations: OFF with the reason, never a crash, never half on -----------
def test_missing_dependency_turns_the_feature_off_with_a_clear_reason(clean_env):
    clean_env.setenv("SOKKAN_FEATURE_DEMO_CREW", "1")      # without the demo banner
    clean_env.setenv("SOKKAN_FEATURE_AGENTS", "0")
    clean_env.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    st = F.resolve()
    assert not st["demo_crew"].enabled and st["demo_crew"].problem
    assert "requires `agents`" in st["demo_crew"].reason
    assert not st["crew_viewer_readonly"].enabled and st["crew_viewer_readonly"].problem
    import agents
    import agents_runtime
    assert agents.viewer_readonly() is False and agents_runtime.demo_mode() is False
    logged: list[str] = []
    bad = F.startup_report(logged.append)
    assert {s.id for s in bad} == {"demo_crew", "crew_viewer_readonly"}
    assert any("`demo_crew` is OFF: requires `agents`" in m for m in logged)


def test_a_default_on_feature_with_a_dependency_off_is_not_a_problem(clean_env):
    st = F.resolve()["sso_teams"]                        # on by default, no SSO configured
    assert not st.enabled and not st.problem and "requires `sso`" in st.reason


def test_conflict_and_planned(clean_env):
    # 3.2.0 ships every roadmap entry; a synthetic planned feature keeps the rule tested
    ghost = F.Feature("ghost_planned", "Ghost", "a roadmap entry", status="planned",
                      kind="planned", target="9.9", vars=(F.Var("SOKKAN_FEATURE_GHOST_PLANNED"),))
    clean_env.setattr(F, "REGISTRY", (*F.REGISTRY, ghost))
    clean_env.setattr(F, "BY_ID", {**F.BY_ID, ghost.id: ghost})
    clean_env.setenv("SOKKAN_FEATURE_FOUR_EYES", "1")
    clean_env.setenv("SOKKAN_FEATURE_ADMIN_APPROVAL", "1")
    clean_env.setenv("SOKKAN_FEATURE_GHOST_PLANNED", "1")
    st = F.resolve()
    assert st["four_eyes"].enabled and not st["admin_approval"].enabled
    assert "conflicts with `four_eyes`" in st["admin_approval"].reason
    assert not st["ghost_planned"].enabled and st["ghost_planned"].problem and \
        "planned" in st["ghost_planned"].reason
    assert {s.id for s in F.problems()} == {"admin_approval", "ghost_planned"}


def test_unknown_feature_id_is_a_programming_error():
    with pytest.raises(KeyError):
        F.enabled("nope")


# ---- API ------------------------------------------------------------------------------
@pytest.fixture()
def client(clean_env, tmp_path):
    from fastapi.testclient import TestClient

    import app as a
    import auth
    import iam
    clean_env.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    iam.upsert_user("admin@x", "admin")
    clean_env.setattr(auth, "resolve_email", lambda request: "admin@x")
    return TestClient(a.app)


def test_api_features_serves_state_and_reason(client, clean_env):
    clean_env.setenv("SOKKAN_FEATURE_DEMO_CREW", "1")
    d = client.get("/api/features").json()
    assert d["tmux"] is True and d["agents"] is True        # flat keys kept for the UI
    reg = d["registry"]
    assert reg["edition"] == "community" and reg["problems"] == ["demo_crew"]
    item = {i["id"]: i for i in reg["items"]}["demo_crew"]
    assert item["enabled"] is False and "demo_banner" in item["reason"]
    assert item["requires"] == ["agents", "demo_banner"]


def test_routes_follow_the_registry(client, clean_env):
    clean_env.setenv("SOKKAN_FEATURE_AGENTS", "0")
    assert client.get("/api/agents").status_code == 404
    clean_env.setenv("SOKKAN_FEATURE_AGENTS", "1")
    assert client.get("/api/agents").status_code == 200
    # multi_project off (community): no new project — existing ones are untouched
    r = client.post("/api/admin/projects", json={"slug": "radio", "name": "Radio"})
    assert r.status_code == 409 and "multi_project" in r.json()["detail"]
    clean_env.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    assert client.post("/api/admin/projects", json={"slug": "radio", "name": "Radio"}).status_code == 200
