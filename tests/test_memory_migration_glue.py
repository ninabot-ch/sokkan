"""memory/memory_migration.py — SOKKAN glue of the 2.x -> 3.0 migration (paths, config
translation, API view, approvals)."""
import json

import pytest


@pytest.fixture()
def mm(monkeypatch, tmp_path):
    import memory_migration as m
    for v in ("CORTHEXIS_MEMORY_PROFILE", "SOKKAN_MEMORY_PROFILE", "ML_SERVICE_URL",
              "CORTHEXIS_ML_SERVICE_URL", "SOKKAN_EMBED_MODEL", "CORTHEXIS_LEGACY_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "auto")
    monkeypatch.setenv("CORTHEXIS_MIGRATION_DIR", str(tmp_path / "mig"))
    monkeypatch.setattr(m, "LEGACY_DB", tmp_path / "memory.db")
    monkeypatch.setattr(m, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(m, "start", lambda: None)
    return m


def test_config_translation(mm, monkeypatch):
    assert mm.config_translation()["profile"] == "leger"
    monkeypatch.setenv("ML_SERVICE_URL", "http://ml:8001")
    t = mm.config_translation()
    assert t["profile"] == "remote" and "ML_SERVICE_URL" in t["reason"]
    monkeypatch.setenv("SOKKAN_MEMORY_PROFILE", "standard")
    assert mm.config_translation()["profile"] == "standard"


def test_status_before_and_after(mm, tmp_path):
    st = mm.status()
    assert st["status"] == "pending" and st["serving"] == "memory.db (2.x)"
    assert st["steps_order"][0] == "archive" and st["plan"] is None
    d = tmp_path / "mig"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"status": "done", "steps": {}}))
    (d / "normalize-plan.txt").write_text("normalize plan (dry run)\n")
    st = mm.status()
    assert st["status"] == "done" and st["serving"] == "store"
    assert st["plan"].startswith("normalize plan")


def test_status_off_in_2x_mode(mm, monkeypatch):
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "sqlite")
    assert mm.status()["status"] == "off" and not mm.active()


def test_approve_writes_its_own_file(mm, tmp_path):
    doc = mm.approve("normalize", "owner@example.org")
    assert doc["normalize"]["by"] == "owner@example.org"
    assert json.loads((tmp_path / "mig" / "approved.json").read_text())["normalize"]
    assert "override" in mm.approve("switch", "owner@example.org")
    with pytest.raises(ValueError):
        mm.approve("everything", "x")
