"""SOKKAN 3.2 — hardening review, lot B: B1 (secrets by name forced on managed instances,
warning when a cockpit runs in `all`) and B2 (agent incidents on by default with Operate)."""


def test_b1_named_secrets_forced_on_managed_instances_and_warning(monkeypatch):
    import vault
    monkeypatch.setenv("SOKKAN_SESSION_SECRETS", "all")
    monkeypatch.delenv("SOKKAN_TIER", raising=False)
    assert vault.session_mode() == "all" and "whole vault" in vault.mode_warning()
    monkeypatch.setenv("SOKKAN_TIER", "starter")              # SOKKAN Cloud managed
    assert vault.session_mode() == "named" and vault.mode_warning() is None


def test_b2_agent_incidents_on_by_default_when_operate_is_on(monkeypatch):
    import agents_runtime
    import observability
    monkeypatch.delenv("SOKKAN_AGENTS_INCIDENTS", raising=False)
    monkeypatch.setattr(observability, "ENABLED", False)
    assert agents_runtime.incidents_enabled() is False
    monkeypatch.setattr(observability, "ENABLED", True)
    assert agents_runtime.incidents_enabled() is True
    monkeypatch.setenv("SOKKAN_AGENTS_INCIDENTS", "0")
    assert agents_runtime.incidents_enabled() is False
    monkeypatch.setenv("SOKKAN_AGENTS_INCIDENTS", "")
    assert agents_runtime.incidents_enabled() is True
