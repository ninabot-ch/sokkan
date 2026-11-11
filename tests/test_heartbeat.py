"""Supervision heartbeat: off by default, exact documented payload, no client identity."""
import json
import re
import socket
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import appliance
import audit
import heartbeat

DOC = Path(__file__).resolve().parent.parent / "docs" / "appliance" / "HEARTBEAT.md"


@pytest.fixture(autouse=True)
def hb_env(tmp_path, monkeypatch):
    monkeypatch.setattr(appliance, "DIR", tmp_path / "appliance")
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    for k in ("SOKKAN_HEARTBEAT_URL", "SOKKAN_HEARTBEAT", "SOKKAN_HEARTBEAT_TOKEN",
              "SOKKAN_HEARTBEAT_INTERVAL_S", "SOKKAN_INSTANCE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(heartbeat, "_db_up", lambda: None)  # no network probe in tests
    heartbeat._state.update(last_sent_ts=None, last_ok=None, last_status=None, last_error="")


def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used while the heartbeat is disabled")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def test_disabled_by_default(monkeypatch):
    _no_network(monkeypatch)
    assert heartbeat.enabled() is False
    assert heartbeat.send_once()["ok"] is False
    assert heartbeat.state()["enabled"] is False
    heartbeat.start()  # starts nothing


def test_kill_switch_wins(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setenv("SOKKAN_HEARTBEAT_URL", "http://127.0.0.1:1/x")
    assert heartbeat.enabled() is True
    monkeypatch.setenv("SOKKAN_HEARTBEAT", "0")
    assert heartbeat.enabled() is False
    assert heartbeat.send_once()["error"] == "disabled"


def test_interval_min(monkeypatch):
    assert heartbeat.interval_s() == 900
    monkeypatch.setenv("SOKKAN_HEARTBEAT_INTERVAL_S", "5")
    assert heartbeat.interval_s() == 60
    monkeypatch.setenv("SOKKAN_HEARTBEAT_INTERVAL_S", "120")
    assert heartbeat.interval_s() == 120


def _doc_fields():
    return re.findall(r"^\| `([a-z_]+)` \|", DOC.read_text(), re.M)


def test_payload_keys_match_fields_and_doc():
    p = heartbeat.payload()
    assert set(p) == set(heartbeat.FIELDS)
    assert len(p) == len(heartbeat.FIELDS)
    assert set(_doc_fields()) == set(heartbeat.FIELDS)
    assert p["schema"] == 1 and set(p["health"]) == {"api", "db", "inference"}
    assert p["health"]["api"] is True and p["support_tunnel_open"] is False
    json.dumps(p)


def test_instance_id_persisted_and_override(monkeypatch):
    a = heartbeat.payload()["instance"]
    assert a and heartbeat.payload()["instance"] == a
    monkeypatch.setenv("SOKKAN_INSTANCE_ID", "client-01")
    assert heartbeat.payload()["instance"] == "client-01"


def test_no_identity_in_payload(monkeypatch):
    import platform
    monkeypatch.setenv("SOKKAN_OWNER_EMAIL", "owner@secret-client.example")
    monkeypatch.setenv("SOKKAN_COMMIT", "abc1234")
    blob = json.dumps(heartbeat.payload()) + json.dumps(heartbeat.state())
    assert "owner@secret-client.example" not in blob and "secret-client" not in blob
    host = socket.gethostname()
    if len(host) > 3:
        assert host not in blob
    assert platform.node() not in blob or len(platform.node()) <= 3


def test_probes_never_raise(monkeypatch):
    monkeypatch.setattr(heartbeat, "_disk", lambda: 1 / 0)
    monkeypatch.setattr(heartbeat, "_version", lambda: 1 / 0)
    monkeypatch.setattr(appliance, "backup_status", lambda: 1 / 0)
    heartbeat.inference_probe = lambda: 1 / 0
    try:
        p = heartbeat.payload()
    finally:
        heartbeat.inference_probe = None
    assert p["disk_free_pct"] is None and p["backup_age_s"] is None and p["health"]["inference"] is None


def test_review_score_none_without_corthexis(monkeypatch):
    monkeypatch.delitem(sys.modules, "corthexis", raising=False)
    assert heartbeat.payload()["memory_review_score"] is None


def test_review_score_from_report(monkeypatch):
    class Fake:
        _state = {"report": {"score": 87}}
    monkeypatch.setitem(sys.modules, "corthexis", Fake)
    assert heartbeat.payload()["memory_review_score"] == 87


def test_inference_probe_hook():
    heartbeat.inference_probe = lambda: True
    try:
        assert heartbeat.payload()["health"]["inference"] is True
    finally:
        heartbeat.inference_probe = None


class _H(BaseHTTPRequestHandler):
    got: list = []
    code = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _H.got.append((self.headers.get("Authorization"), self.headers.get("User-Agent"),
                       json.loads(self.rfile.read(n))))
        self.send_response(_H.code)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    _H.got, _H.code = [], 200
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/hb"
    srv.shutdown()


def test_send_to_local_server(server, monkeypatch):
    monkeypatch.setenv("SOKKAN_HEARTBEAT_URL", server)
    monkeypatch.setenv("SOKKAN_HEARTBEAT_TOKEN", "tok123")
    r = heartbeat.send_once()
    assert r == {"ok": True, "status": 200, "error": ""}
    auth, ua, body = _H.got[0]
    assert auth == "Bearer tok123" and ua.startswith("sokkan-heartbeat/")
    assert set(body) == set(heartbeat.FIELDS)
    st = heartbeat.state()
    assert st["last_ok"] is True and st["last_status"] == 200 and st["url_host"] == "127.0.0.1"
    assert "tok123" not in json.dumps(st)


def test_failures_are_silent(server, monkeypatch):
    monkeypatch.setenv("SOKKAN_HEARTBEAT_URL", server)
    _H.code = 500
    r = heartbeat.send_once()
    assert r["ok"] is False and r["status"] == 500
    monkeypatch.setenv("SOKKAN_HEARTBEAT_URL", "http://127.0.0.1:1/none")
    r = heartbeat.send_once()
    assert r["ok"] is False and r["status"] is None and r["error"]
    assert heartbeat.state()["last_ok"] is False


def test_route_admin_preview(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    asked = []

    def require(role):
        asked.append(role)
        return lambda: {"email": "a@b.ch"}

    app = FastAPI()
    app.include_router(heartbeat.router(require))
    j = TestClient(app).get("/api/appliance/heartbeat").json()
    assert asked == ["admin"] and set(j) == {"state", "payload"}
    assert set(j["payload"]) == set(heartbeat.FIELDS)
