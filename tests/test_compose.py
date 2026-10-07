"""docker-compose.yml without `include:` (any Compose v2 from 2.12), its standalone copy
docker/embed/compose.yml, the GPU overrides, and what scripts/memory-setup.sh writes."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EMBED_SERVICES = ("corthexis-embed-fetch", "corthexis-embed", "corthexis-rerank")


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text())


def _norm_paths(svc: dict, base: str) -> dict:
    """Volume sources relative to the sokkan folder, whatever file they are written in."""
    out = json.loads(json.dumps(svc))
    vols = []
    for v in out.get("volumes", []):
        src, _, rest = v.partition(":")
        if src.startswith("."):
            src = os.path.normpath(os.path.join(base, src))
        vols.append(f"{src}:{rest}")
    out["volumes"] = vols
    return out


def test_main_compose_has_no_include_and_carries_the_memory_services():
    main = _load(ROOT / "docker-compose.yml")
    assert "include" not in main
    for name in EMBED_SERVICES:
        assert name in main["services"], name
    assert "corthexis-models" in main["volumes"]
    # nested defaults with text around them need Compose 2.20 (`${A:-x${B}y}`)
    raw = (ROOT / "docker-compose.yml").read_text()
    assert not re.search(r"\$\{[A-Z_]+:-[^}$]*\$\{[^}]*\}[^}]+\}", raw)
    assert not re.search(r"\$\{[A-Z_]+:-[^}$]+\$\{", raw)


def test_standalone_embed_compose_is_in_sync_with_the_main_one():
    main = _load(ROOT / "docker-compose.yml")["services"]
    alone = _load(ROOT / "docker" / "embed" / "compose.yml")["services"]
    for name in EMBED_SERVICES:
        assert _norm_paths(main[name], ".") == _norm_paths(alone[name], "docker/embed"), name


@pytest.mark.parametrize("accel", ["sycl", "cuda"])
def test_gpu_overrides_extend_the_main_compose(accel):
    ov = _load(ROOT / "docker" / "embed" / f"compose.{accel}.yml")["services"]
    gpu = ov["corthexis-embed-gpu"]
    # `extends: file` is resolved from the project folder (the sokkan folder)
    assert gpu["extends"] == {"file": "docker-compose.yml", "service": "corthexis-embed"}
    assert "corthexis-rerank" in ov
    assert not (ROOT / "docker" / "embed" / "compose.cpu.yml").exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI absent")
def test_compose_config_resolves_with_the_gpu_override(tmp_path):
    r = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if r.returncode:
        pytest.skip("no docker compose plugin")
    env = {**os.environ, "COMPOSE_PROFILES": "rerank"}
    r = subprocess.run(["docker", "compose", "--env-file", str(ROOT / ".env.example"),
                        "-f", "docker-compose.yml", "-f", "docker/embed/compose.sycl.yml",
                        "config", "--format", "json"], cwd=ROOT, capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]
    assert "intel" in svc["corthexis-embed-gpu"]["image"]
    assert "intel" in svc["corthexis-rerank"]["image"]
    assert any(str(v.get("source", "")).endswith("docker/embed/run.sh")
               for v in svc["corthexis-embed-gpu"]["volumes"])


# ---------------------------------------------------------------- memory-setup.sh

def _setup_dir(tmp_path: Path, rec: str, accel: str, env_text: str) -> tuple[Path, dict]:
    d = tmp_path / "sokkan"
    (d / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "memory-setup.sh", d / "scripts" / "memory-setup.sh")
    (d / ".env").write_text(env_text)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-m" ]; then echo \'{json.dumps({"recommended": rec, "accel": accel, "reason": "test"})}\'; exit 0; fi\n'
        f'exec {sys.executable} "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    for tool in ("sh", "sed", "grep", "tr", "paste", "mv", "cat", "dirname", "tail"):
        p = shutil.which(tool)
        if p:
            (bin_ / tool).symlink_to(p)
    return d, {"PATH": str(bin_), "HOME": str(tmp_path)}


def _env(d: Path) -> dict:
    return dict(line.split("=", 1) for line in (d / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def _run(d: Path, env: dict, *args: str) -> str:
    r = subprocess.run(["sh", "scripts/memory-setup.sh", "--non-interactive", *args], cwd=d,
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout


def test_memory_setup_gpu_writes_the_override_then_removes_it(tmp_path):
    d, env = _setup_dir(tmp_path, "gpu", "sycl", "SOKKAN_VERSION=3.0.0\n")
    _run(d, env)
    e = _env(d)
    assert e["SOKKAN_MEMORY_PROFILE"] == "gpu" and e["SOKKAN_EMBED_ACCEL"] == "sycl"
    assert e["COMPOSE_FILE"] == "docker-compose.yml:docker/embed/compose.sycl.yml"
    assert e["COMPOSE_PATH_SEPARATOR"] == ":" and e["COMPOSE_PROFILES"] == "rerank"
    _run(d, env)                                       # idempotent
    assert _env(d)["COMPOSE_FILE"] == "docker-compose.yml:docker/embed/compose.sycl.yml"
    _run(d, env, "--profile", "leger")                 # back to CPU: override removed
    e = _env(d)
    assert "COMPOSE_FILE" not in e and "COMPOSE_PATH_SEPARATOR" not in e
    assert "COMPOSE_PROFILES" not in e and e["SOKKAN_VERSION"] == "3.0.0"


def test_memory_setup_keeps_a_compose_file_of_the_user(tmp_path):
    d, env = _setup_dir(tmp_path, "gpu", "cuda",
                        "COMPOSE_FILE=docker-compose.yml:my-override.yml\n")
    _run(d, env)
    assert _env(d)["COMPOSE_FILE"] == \
        "docker-compose.yml:my-override.yml:docker/embed/compose.cuda.yml"
    _run(d, env, "--profile", "leger")
    assert _env(d)["COMPOSE_FILE"] == "docker-compose.yml:my-override.yml"


def test_memory_setup_cpu_writes_no_compose_file(tmp_path):
    d, env = _setup_dir(tmp_path, "leger", "cpu", "")
    _run(d, env)
    e = _env(d)
    assert e["SOKKAN_MEMORY_PROFILE"] == "leger" and "COMPOSE_FILE" not in e


# ---------------------------------------------------------------- rollback.sh

def test_rollback_replaces_the_code_and_keeps_env_workspace_and_user_files(tmp_path):
    import tarfile
    d = tmp_path / "sokkan"
    (d / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "rollback.sh", d / "scripts" / "rollback.sh")
    (d / ".env").write_text("SOKKAN_LOCAL_TOKEN=t\nSOKKAN_VERSION=3.0.0\n"
                            "COMPOSE_FILE=docker-compose.yml:docker/embed/compose.sycl.yml\n")
    (d / "docker-compose.yml").write_text("services: {}  # 3.0\n")
    (d / "frontend" / "components").mkdir(parents=True)
    (d / "frontend" / "components" / "CortHeXisGraph.tsx").write_text("3.0 only")
    (d / "memory" / "core").mkdir(parents=True)
    (d / "memory" / "core" / "store.py").write_text("3.0 only")
    (d / "workspace").mkdir()
    (d / "workspace" / "project.txt").write_text("mine")
    (d / "my-notes.txt").write_text("mine too")
    old = tmp_path / "old"
    for f, t in {"docker-compose.yml": "services: {}  # 2.3\n",
                 "frontend/app/page.tsx": "2.3", "memory/index_memory.py": "2.3",
                 "scripts/doctor.sh": "2.3", "VERSION": "2.3.0\n"}.items():
        (old / "sokkan" / f).parent.mkdir(parents=True, exist_ok=True)
        (old / "sokkan" / f).write_text(t)
    tgz = tmp_path / "sokkan-2.3.0.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(old / "sokkan", arcname="sokkan")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/docker.log\n')
    (bin_ / "docker").chmod(0o755)
    r = subprocess.run(["sh", "scripts/rollback.sh", str(tgz)], cwd=d, capture_output=True,
                       text=True, env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (d / "frontend" / "components").exists()        # the 3.0 leftovers are gone
    assert not (d / "memory" / "core").exists()
    assert (d / "frontend" / "app" / "page.tsx").read_text() == "2.3"
    assert "# 2.3" in (d / "docker-compose.yml").read_text()
    assert (d / "workspace" / "project.txt").read_text() == "mine"
    assert (d / "my-notes.txt").exists()
    env = (d / ".env").read_text()
    assert "SOKKAN_LOCAL_TOKEN=t" in env and "SOKKAN_VERSION=2.3.0" in env
    assert "COMPOSE_FILE" not in env
    assert "compose up -d --build --remove-orphans" in (tmp_path / "docker.log").read_text()
    assert not list(d.glob(".sokkan-rollback.*"))


# ---- 3.1.x : every operator-facing SOKKAN_* variable reaches the api container -----
# (the 2.0.1 trap: a variable in .env but not in `environment:` never enters the
# container). Per-session variables set by the API for its MCP subprocesses must
# NOT be there — SOKKAN_AGENT_RUN=1 at the API level would make it read-only.
AGENT_VARS_31X = (
    "SOKKAN_FEATURE_AGENTS", "SOKKAN_AGENTS_MAX_CONCURRENT", "SOKKAN_AGENTS_MISFIRE_S",
    "SOKKAN_AGENTS_TICK_S", "SOKKAN_AGENTS_APPROVAL", "SOKKAN_SESSION_SECRETS",
    "SOKKAN_MEMORY_QUARANTINE_DIR", "SOKKAN_AGENTS_INCIDENTS", "SOKKAN_CREW_VIEWER_READONLY",
    "SOKKAN_DEMO_CREW",
    # 3.1.2
    "SOKKAN_AGENTS_MAX_TOKENS_PER_RUN", "SOKKAN_MODEL_PRICES", "SOKKAN_FX_USD_PER_CHF",
)
PER_SESSION_VARS = ("SOKKAN_SESSION_ID", "SOKKAN_SESSION_USER", "SOKKAN_AGENT_RUN",
                    "SOKKAN_AGENT_NAME", "SOKKAN_AGENT_RUN_ID")


def test_agent_variables_are_passed_to_the_api_container():
    env = _load(ROOT / "docker-compose.yml")["services"]["api"]["environment"]
    missing = [v for v in AGENT_VARS_31X if v not in env]
    assert not missing, f"declare in services.api.environment: {missing}"
    leaked = [v for v in PER_SESSION_VARS if v in env]
    assert not leaked, f"per-session variables must not be set on the API: {leaked}"
    spec = (ROOT / "docs" / "AGENTS.md").read_text()
    undocumented = [v for v in AGENT_VARS_31X if v not in spec]
    assert not undocumented, f"document in docs/AGENTS.md: {undocumented}"


def test_agent_variables_read_from_the_environment_are_all_declared():
    """Any SOKKAN_AGENTS_* / SOKKAN_CREW_* the backend reads is in the api block."""
    env = _load(ROOT / "docker-compose.yml")["services"]["api"]["environment"]
    read = set()
    for f in (ROOT / "backend").glob("*.py"):
        read |= set(re.findall(r"\"(SOKKAN_(?:AGENTS|CREW)_[A-Z0-9_]+)\"", f.read_text()))
    read.discard("SOKKAN_AGENTS_DB")  # test/path override, defaults under SOKKAN_DATA_DIR
    assert read and not sorted(read - set(env))
