"""Appliance support tunnel: api side (backend/appliance.py) and host script (support-tunnel.sh)."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import appliance
import audit

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "appliance" / "support-tunnel.sh"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    d = tmp_path / "appliance"
    d.mkdir()
    monkeypatch.setattr(appliance, "DIR", d)
    monkeypatch.setattr(appliance, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    appliance._seen_ids.clear()
    monkeypatch.delenv("SOKKAN_SUPPORT_MAX_HOURS", raising=False)
    return d


# ----------------------------------------------------------------------------- python side
def test_state_missing_and_corrupt(env):
    st = appliance.support_state()
    assert st["open"] is False and st["host_agent"] is False and st["remaining_s"] == 0
    (env / "support-state.json").write_text("{not json")
    st = appliance.support_state()
    assert st["open"] is False and st["host_agent"] is False


def test_state_open_remaining(env):
    dl = int(time.time()) + 600
    (env / "support-state.json").write_text(json.dumps({"open": True, "deadline": dl, "by": "a@b.ch"}))
    st = appliance.support_state()
    assert st["open"] is True and st["host_agent"] is True and 590 <= st["remaining_s"] <= 600


def test_request_validation(env, monkeypatch):
    for bad in (0, -1, 9, "abc", float("nan")):
        with pytest.raises(ValueError):
            appliance.request_support("open", bad, "a@b.ch")
    with pytest.raises(ValueError):
        appliance.request_support("destroy", 1, "a@b.ch")
    monkeypatch.setenv("SOKKAN_SUPPORT_MAX_HOURS", "2")
    with pytest.raises(ValueError):
        appliance.request_support("open", 3, "a@b.ch")
    assert not (env / "support-request.json").exists()
    req = appliance.request_support("open", 1.5, "a@b.ch")
    on_disk = json.loads((env / "support-request.json").read_text())
    assert on_disk == req and req["action"] == "open" and req["hours"] == 1.5 and len(req["id"]) >= 16
    rows = audit.recent(10)
    assert rows[0]["user"] == "a@b.ch" and rows[0]["action"] == "support.tunnel.request"


def _write_events(env, evs, tail=""):
    with open(env / "support-events.jsonl", "a") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")
        f.write(tail)


def test_ingest_idempotent_and_truncated(env):
    ev1 = {"id": "1-1-1", "ts": 1, "event": "opened", "by": "me@x.ch", "hours": 2,
           "deadline": 1700000000, "reason": "why"}
    _write_events(env, [ev1], tail='{"id": "2-2-2", "ts": 2, "even')
    assert appliance.ingest_events() == 1
    assert appliance.ingest_events() == 0
    rows = [r for r in audit.recent(10) if r["action"].startswith("support.tunnel.")]
    assert len(rows) == 1 and rows[0]["user"] == "me@x.ch" and rows[0]["action"] == "support.tunnel.opened"
    assert "hours=2" in rows[0]["detail"] and "deadline=2023-11-14T22:13:20Z" in rows[0]["detail"]
    assert "reason=why" in rows[0]["detail"]
    # the line gets completed: it is now ingested
    with open(env / "support-events.jsonl", "w") as f:
        f.write(json.dumps(ev1) + "\n")
        f.write(json.dumps({"id": "2-2-2", "ts": 2, "event": "expired", "by": "", "hours": None,
                            "deadline": None, "reason": ""}) + "\n")
    assert appliance.ingest_events() == 1
    last = audit.recent(1)[0]
    assert last["action"] == "support.tunnel.expired" and last["user"] == "system"


def test_ingest_offset_lost_no_duplicate(env):
    _write_events(env, [{"id": "a-1-1", "event": "closed", "by": "u@x.ch"}])
    assert appliance.ingest_events() == 1
    for p in env.glob("support-events.offset"):
        p.unlink()
    assert appliance.ingest_events() == 0


def test_ingest_never_raises(env):
    (env / "support-events.jsonl").write_bytes(b"\xff\xfe\n[1]\n{}\nnull\n")
    assert appliance.ingest_events() == 0


def test_ingest_readonly_dir_offset_fallback(env, tmp_path, monkeypatch):
    _write_events(env, [{"id": "r-1-1", "event": "closed", "by": "u@x.ch"}])
    monkeypatch.setattr(appliance.os, "access", lambda *a, **k: False)
    assert appliance.ingest_events() == 1
    assert (tmp_path / "appliance-events.offset").exists()
    assert not (env / "support-events.offset").exists()


def test_backup_status(env):
    b = appliance.backup_status()
    assert b["age_s"] is None
    (env / "backup-status.json").write_text(json.dumps(
        {"last_success_ts": time.time() - 100, "last_attempt_ts": time.time() - 100,
         "last_error": "", "snapshot": "s1", "size_bytes": 5}))
    b = appliance.backup_status()
    assert 99 <= b["age_s"] <= 105 and b["snapshot"] == "s1"
    (env / "backup-status.json").write_text("garbage")
    assert appliance.backup_status()["age_s"] is None


@pytest.fixture()
def client(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    role = {"v": "admin"}

    def require(min_role):
        def dep():
            if min_role == "admin" and role["v"] != "admin":
                from fastapi import HTTPException
                raise HTTPException(403, "admin only")
            return {"email": "boss@x.ch", "role": role["v"]}
        return dep

    app = FastAPI()
    app.include_router(appliance.router(require))
    c = TestClient(app)
    c.role = role
    return c


def test_route_status(client, env):
    client.role["v"] = "viewer"
    _write_events(env, [{"id": "s-1-1", "event": "opened", "by": "u@x.ch", "hours": 1}])
    r = client.get("/api/appliance/status")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"support", "backup", "heartbeat"} and j["support"]["open"] is False
    assert any(x["action"] == "support.tunnel.opened" for x in audit.recent(10))


def test_route_support(client, env):
    r = client.post("/api/appliance/support", json={"action": "open", "hours": 1})
    assert r.status_code == 409 and "not installed" in r.json()["detail"]
    assert not (env / "support-request.json").exists()
    (env / "support-state.json").write_text(json.dumps({"open": False}))
    r = client.post("/api/appliance/support", json={"action": "open", "hours": 99})
    assert r.status_code == 400 and "at most" in r.json()["detail"]
    r = client.post("/api/appliance/support", json={"action": "open", "hours": 2})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["request"]["by"] == "boss@x.ch"
    assert json.loads((env / "support-request.json").read_text())["hours"] == 2
    client.role["v"] = "viewer"
    assert client.post("/api/appliance/support", json={"action": "close"}).status_code == 403


# ----------------------------------------------------------------------------- host script
@pytest.fixture()
def sh(env, tmp_path):
    flag = tmp_path / "tunnel-up"
    e = dict(os.environ)
    e.update(SOKKAN_APPLIANCE_DIR=str(env), SOKKAN_TUNNEL_UP_CMD=f"touch {flag}",
             SOKKAN_TUNNEL_DOWN_CMD=f"rm -f {flag}", SOKKAN_TUNNEL_PROBE_CMD=f"test -f {flag}")
    e.pop("SOKKAN_SUPPORT_NOW", None)
    e.pop("SOKKAN_SUPPORT_MAX_HOURS", None)
    interp = shutil.which("dash") or "sh"

    def run(*args, **over):
        ee = {**e, **over}
        return subprocess.run([interp, str(SCRIPT), *args], env=ee, capture_output=True, text=True)

    run.flag = flag
    run.env = ee_dir = env
    run.events = lambda: [json.loads(x) for x in (ee_dir / "support-events.jsonl").read_text().splitlines()]
    run.state = lambda: json.loads((ee_dir / "support-state.json").read_text())
    return run


def test_open_close(sh):
    p = sh("open", "2", "--by", "me@x.ch", "--reason", "ticket 4")
    assert p.returncode == 0, p.stderr
    assert sh.flag.exists()
    st = sh.state()
    assert st["open"] is True and st["by"] == "me@x.ch" and st["deadline"] - st["opened_at"] == 7200
    assert oct((sh.env / "support-state.json").stat().st_mode & 0o777) == "0o644"
    ev = sh.events()
    assert ev[0]["event"] == "opened" and ev[0]["hours"] == 2 and ev[0]["reason"] == "ticket 4"
    s = json.loads(sh("status").stdout)
    assert s["open"] is True and 7190 < s["remaining_s"] <= 7200
    assert sh("close", "--by", "me@x.ch").returncode == 0
    assert not sh.flag.exists() and sh.state()["open"] is False
    assert sh("close").returncode == 0  # idempotent
    assert [e["event"] for e in sh.events()] == ["opened", "closed"]
    assert len({e["id"] for e in sh.events()}) == 2


def test_open_decimals_and_bounds(sh):
    assert sh("open", "0.5").returncode == 0
    assert sh.state()["deadline"] - sh.state()["opened_at"] == 1800
    sh("close")
    for bad in ("99", "0", "-1", "abc", "8.01"):
        p = sh("open", bad)
        assert p.returncode == 2, bad
        assert "hours" in p.stderr
    assert sh("open", "9", SOKKAN_SUPPORT_MAX_HOURS="10").returncode == 0
    assert sh("open", "3", "--by", "bad\nname").returncode == 2


def test_expire(sh):
    now = int(time.time())
    sh("open", "1", "--by", "me@x.ch", SOKKAN_SUPPORT_NOW=str(now))
    assert sh("expire", SOKKAN_SUPPORT_NOW=str(now + 3599)).returncode == 0
    assert sh.flag.exists() and sh.state()["open"] is True
    assert sh("expire", SOKKAN_SUPPORT_NOW=str(now + 3600)).returncode == 0
    assert not sh.flag.exists() and sh.state()["open"] is False
    last = sh.events()[-1]
    assert last["event"] == "expired" and last["by"] == "system:deadline"


def test_forced_closed(sh):
    sh("close")
    sh.flag.touch()  # tunnel up behind the state's back
    assert sh("expire").returncode == 0
    assert not sh.flag.exists()
    assert sh.events()[-1]["event"] == "forced_closed"
    sh("expire")
    assert [e["event"] for e in sh.events()].count("forced_closed") == 1


def test_apply_once(sh):
    def req(rid, action="open", hours=1, ts=None):
        (sh.env / "support-request.json").write_text(json.dumps(
            {"id": rid, "action": action, "hours": hours, "by": "boss@x.ch", "ts": ts or time.time()}))

    req("r1")
    assert sh("apply").returncode == 0 and sh.state()["open"] is True
    assert sh.state()["last_request_id"] == "r1" and sh.state()["by"] == "boss@x.ch"
    sh("close")
    assert sh("apply").returncode == 0  # replay of r1 is ignored
    assert sh.state()["open"] is False
    req("r2", ts=time.time() - 3600)  # too old
    sh("apply")
    assert sh.state()["open"] is False
    req("r3", hours=99)
    assert sh("apply").returncode == 2 and sh.state()["open"] is False
    req("r4")
    sh("apply")
    req("r5", action="close")
    sh("apply")
    assert sh.state()["open"] is False and not sh.flag.exists()


def test_up_failure(sh):
    p = sh("open", "1", SOKKAN_TUNNEL_UP_CMD="false")
    assert p.returncode == 1
    assert sh.state()["open"] is False
    assert sh.events()[-1]["event"] == "open_failed"


def test_json_escaping(sh):
    assert sh("open", "1", "--by", 'a"b\\c', "--reason", 'say "hi" \\o/').returncode == 0
    sh("close", "--by", 'x"y\\')
    for ev in sh.events():
        assert isinstance(ev, dict)
    assert sh.events()[0]["by"] == 'a"b\\c' and sh.events()[0]["reason"] == 'say "hi" \\o/'
    assert sh.events()[1]["by"] == 'x"y\\'
    json.loads(sh("status").stdout)
    json.loads((sh.env / "support-state.json").read_text())


def test_end_to_end_audit(sh):
    now = int(time.time())
    sh("open", "1", "--by", "me@x.ch", SOKKAN_SUPPORT_NOW=str(now))
    sh("expire", SOKKAN_SUPPORT_NOW=str(now + 4000))
    assert appliance.ingest_events() == 2
    rows = {r["action"]: r for r in audit.recent(20)}
    assert rows["support.tunnel.opened"]["user"] == "me@x.ch"
    assert rows["support.tunnel.expired"]["user"] == "system:deadline"
    assert rows["support.tunnel.opened"]["resource"] == "tailscale"


def test_script_is_posix():
    for interp in ("sh", "dash"):
        if shutil.which(interp):
            assert subprocess.run([interp, "-n", str(SCRIPT)]).returncode == 0
