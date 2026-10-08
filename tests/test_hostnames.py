"""3.2.3 — every IP the cockpit shows gets a name: SOKKAN_HOSTS > SOKKAN_INFRA_NODES >
/etc/hosts > Tailscale > reverse DNS, shown as « name (ip) »."""
import json
import subprocess

import pytest


@pytest.fixture()
def hn(tmp_path, monkeypatch):
    import hostnames

    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n127.0.1.1 gmk1\n"
                     "100.124.110.23  rog1   # ninabot infra\n"
                     "10.0.0.9 build-box build-box.lan\n")
    monkeypatch.setenv("SOKKAN_HOSTS_FILE", str(hosts))
    monkeypatch.delenv("SOKKAN_HOSTS", raising=False)
    monkeypatch.delenv("SOKKAN_INFRA_NODES", raising=False)
    ts = {"Self": {"DNSName": "gmk1.tail9.ts.net.", "HostName": "gmk1", "TailscaleIPs": ["100.89.156.12"]},
          "Peer": {"a": {"DNSName": "ninjob-dr1.tail9.ts.net.", "HostName": "ninjob-dr1",
                         "TailscaleIPs": ["100.96.55.99", "fd7a::1"]},
                   "b": {"DNSName": "iphone.tail9.ts.net.", "HostName": "localhost",
                         "TailscaleIPs": ["100.81.0.1"]}}}
    monkeypatch.setattr(hostnames.shutil, "which", lambda exe: "/usr/bin/tailscale" if exe == "tailscale" else None)
    monkeypatch.setattr(hostnames.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(ts), ""))
    rdns = {"192.0.2.7": ("web7.example.net", [], ["192.0.2.7"])}

    def fake_rdns(ip):
        if ip in rdns:
            return rdns[ip]
        raise OSError("NXDOMAIN")
    monkeypatch.setattr(hostnames.socket, "gethostbyaddr", fake_rdns)
    hostnames.clear_caches()
    yield hostnames
    hostnames.clear_caches()


def test_resolution_order(hn, monkeypatch):
    assert hn.resolve("100.124.110.23") == "rog1"                 # /etc/hosts
    assert hn.resolve("100.96.55.99:9100") == "ninjob-dr1"        # Tailscale MagicDNS, first label
    assert hn.resolve("100.81.0.1") == "iphone"                   # DNSName beats HostName "localhost"
    assert hn.resolve("192.0.2.7") == "web7.example.net"          # reverse DNS
    assert hn.resolve("203.0.113.1") is None                      # nobody knows it
    assert hn.resolve("loki:3100") is None                        # already a name
    assert hn.resolve("localhost:9090") == "localhost"
    monkeypatch.setenv("SOKKAN_INFRA_NODES", json.dumps({"100.124.110.23": {"name": "rog1-gpu", "role": "x"}}))
    assert hn.resolve("100.124.110.23") == "rog1-gpu"             # topology map beats /etc/hosts
    monkeypatch.setenv("SOKKAN_HOSTS", json.dumps({"100.124.110.23": "ROG1", "203.0.113.1": {"name": "edge"}}))
    assert hn.resolve("100.124.110.23") == "ROG1"                 # SOKKAN_HOSTS wins
    assert hn.resolve("203.0.113.1") == "edge"
    monkeypatch.setenv("SOKKAN_HOSTS", "{not json")
    assert hn.resolve("100.124.110.23") == "rog1-gpu"             # broken JSON = ignored, no crash


def test_labels_and_text(hn):
    assert hn.label("100.124.110.23") == "rog1 (100.124.110.23)"
    assert hn.label("203.0.113.1") == "203.0.113.1"
    d = hn.describe("100.96.55.99:9100")
    assert d == {"host": "100.96.55.99", "port": "9100", "name": "ninjob-dr1",
                 "label": "ninjob-dr1 (100.96.55.99:9100)"}
    txt = "disk full on 100.124.110.23:9100, 203.0.113.1 fine"
    out = hn.annotate_text(txt)
    assert out == "disk full on rog1 (100.124.110.23):9100, 203.0.113.1 fine"
    assert hn.annotate_text(out) == out                           # idempotent


def test_infra_targets_and_nodes_carry_names(hn, monkeypatch):
    import infra

    up = [{"metric": {"job": "node", "instance": "100.96.55.99:9100"}, "value": [0, "1"]},
          {"metric": {"job": "cadvisor", "instance": "cadvisor:8080"}, "value": [0, "1"]}]
    monkeypatch.setattr(infra, "ENABLED", True)
    monkeypatch.setattr(infra, "NODES", {})
    monkeypatch.setattr(infra, "_q", lambda expr: up if expr == "up" else up[:1] if expr == 'up{job="node"}' else [])
    t = {x["instance"]: x for x in infra.targets()}
    assert t["100.96.55.99:9100"]["name"] == "ninjob-dr1"
    assert t["100.96.55.99:9100"]["label"] == "ninjob-dr1 (100.96.55.99:9100)"
    assert t["cadvisor:8080"]["name"] is None and t["cadvisor:8080"]["label"] == "cadvisor:8080"
    (n,) = infra.nodes()
    assert n["ip"] == "100.96.55.99" and n["name"] == "ninjob-dr1"
    assert n["label"] == "ninjob-dr1 (100.96.55.99)"


def test_prometheus_results_get_instance_name(hn, monkeypatch):
    import observability

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"resultType": "vector", "result": [
                {"metric": {"instance": "100.124.110.23:9100"}, "value": [0, "1"]},
                {"metric": {"instance": "203.0.113.1:9100"}, "value": [0, "1"]}]}}
    monkeypatch.setattr(observability, "PROM", "http://prom")
    monkeypatch.setattr(observability.httpx, "get", lambda *a, **k: R())
    res = observability.query_metrics("up")["result"]
    assert res[0]["metric"]["instance_name"] == "rog1"
    assert "instance_name" not in res[1]["metric"]
