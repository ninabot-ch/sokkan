"""4.1.1 — memory ceilings on the appliance. Every long-running service the appliance starts has
its own memory ceiling (a leak dies in its own cgroup instead of a box-wide OOM), Postgres is the
kernel's LAST pick, the model the first one but no longer at 900. Off the appliance nothing
changes: the ceilings default to 0 (= none) on the services every deployment runs.

Measured on test-ionos1 (20.11.2026, scale 1/8 under a 4 GiB slice): a leak in the api is
OOM-killed alone (oom_memcg = its own scope), Ship / Postgres / web keep running; an unbounded
process fills the slice → the kernel kills the model first, Postgres answers throughout."""
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
SHIP = yaml.safe_load((ROOT / "deploy/appliance/ship/docker-compose.yml").read_text())["services"]["ship"]
ENV_EX = (ROOT / "deploy/appliance/env.appliance.example").read_text()
SHIP_ENV = (ROOT / "deploy/appliance/ship/ship.env.example").read_text()
UPGRADE = (ROOT / "scripts/upgrade.sh").read_text()

VAR = re.compile(r"^\$\{([A-Z0-9_]+):-([^}]*)\}$")
# services the appliance runs (COMPOSE_PROFILES=appliance,rerank), one-shot fetch included
APPLIANCE = ("api", "db", "web", "router", "tls", "corthexis-embed", "corthexis-rerank",
             "corthexis-embed-fetch")
EVERYWHERE = ("api", "db", "web", "corthexis-embed-fetch")   # also on cloud VMs / workstations


def _example(name: str) -> str | None:
    m = re.search(rf"^{name}=(.*)$", ENV_EX, re.M)
    return m.group(1) if m else None


def test_every_appliance_service_has_a_ceiling_set_by_the_appliance_env():
    for svc in APPLIANCE:
        raw = str(COMPOSE[svc].get("mem_limit", ""))
        m = VAR.match(raw)
        assert m, f"{svc}: mem_limit must be an env-driven value, got {raw!r}"
        val = _example(m.group(1))
        assert val and val != "0", f"{svc}: env.appliance.example must set {m.group(1)}"


def test_services_of_every_deployment_keep_no_ceiling_by_default():
    for svc in EVERYWHERE:
        m = VAR.match(str(COMPOSE[svc]["mem_limit"]))
        assert m.group(2) == "0", f"{svc}: default must stay 0 (no ceiling) off the appliance"


def test_swap_cannot_bypass_the_ceiling_of_long_running_services():
    for svc in ("api", "db", "web", "router", "tls"):
        assert COMPOSE[svc]["memswap_limit"] == COMPOSE[svc]["mem_limit"], svc


def test_oom_order_model_first_postgres_last():
    db = VAR.match(str(COMPOSE["db"]["oom_score_adj"]))
    assert db and db.group(2) == "0"                       # off the appliance: unchanged
    assert int(_example(db.group(1))) < 0                  # appliance: Postgres last
    ship = VAR.match(str(SHIP["oom_score_adj"]))
    assert ship and int(ship.group(2)) == 300              # was 900: first, not "always it"
    assert re.search(r"^SHIP_OOM_SCORE_ADJ=300$", SHIP_ENV, re.M)


def test_upgrade_writes_missing_ceilings_once_and_keeps_the_operators(tmp_path):
    set_env = re.search(r"^  set_env\(\) \{.*?^  \}\n", UPGRADE, re.M | re.S).group(0)
    block = re.search(r"^  # 4\.1\.1: memory ceilings.*?^  fi\n", UPGRADE, re.M | re.S).group(0)
    script = tmp_path / "t.sh"
    script.write_text("set -e\n" + set_env + block)
    env = tmp_path / ".env"
    env.write_text("COMPOSE_PROFILES=appliance,rerank\nSOKKAN_API_MEM=12g\n")
    for _ in range(2):                                     # idempotent, and fine under set -e
        subprocess.run(["sh", str(script)], cwd=tmp_path, check=True, capture_output=True)
    text = env.read_text()
    assert "SOKKAN_API_MEM=12g" in text and text.count("SOKKAN_API_MEM=") == 1
    for svc in APPLIANCE:
        name = VAR.match(str(COMPOSE[svc]["mem_limit"])).group(1)
        assert text.count(f"{name}=") == 1, name
    assert "SOKKAN_DB_OOM_SCORE_ADJ=-500" in text
    env.write_text("COMPOSE_PROFILES=edge\n")              # not an appliance: untouched
    subprocess.run(["sh", str(script)], cwd=tmp_path, check=True, capture_output=True)
    assert env.read_text() == "COMPOSE_PROFILES=edge\n"
