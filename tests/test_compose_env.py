"""Every environment variable the backend reads is either passed to the api container by
docker-compose.yml, or classified below WITH A REASON (set by the image, set per session by
the api itself, a test/path override, another runtime). A variable that is only in `.env`
never enters the container — the 2.0.1 trap, found again on ~25 variables in November 2026
(SOKKAN_OBS_ALERT_TOKEN, SOKKAN_ORG_NAME, SOKKAN_GRAFANA_USER, SOKKAN_CHROMIUM…).

Adding `os.environ.get("SOKKAN_NEW")` to the backend makes this file fail until the variable
is declared in `services.api.environment` (operator-facing) or classified here."""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("backend", "memory")
OVERRIDES = ("docker/runner/compose.docker-runner.yml",)
# code that runs in ANOTHER service of the compose file (its own guard is at the end of this file)
OTHER_SERVICE = {"backend/shiprouter.py"}

# ---- variables that are deliberately NOT in services.api.environment ------------------
IMAGE = {  # set by docker/api.Dockerfile (ENV) — checked against the Dockerfile below
    "SOKKAN_DATA_DIR", "CLAUDE_CONFIG_DIR", "SOKKAN_AGENT_CWD", "SOKKAN_MEMORY_DIR",
    "SOKKAN_PROJECT_WD", "FASTEMBED_CACHE_PATH", "SOKKAN_COMMIT",
}
PER_SESSION = {  # set by the api for ONE session / run / helper process, never instance-wide
    "SOKKAN_SESSION_ID", "SOKKAN_SESSION_USER", "SOKKAN_SESSION_PROJECT", "SOKKAN_SESSION_SCOPE",
    "SOKKAN_AGENT_RUN", "SOKKAN_AGENT_NAME", "SOKKAN_AGENT_RUN_ID",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",            # session env built by llm.session_env
    "CORTHEXIS_RECALL_API_URL", "CORTHEXIS_RECALL_API_TIMEOUT", "CORTHEXIS_RECALL_TOKEN_FILE",
    "CORTHEXIS_RECALL_DEBUG", "CORTHEXIS_RECALL_SESSION_ID",   # recall hook of a session
    "SOKKAN_FORGE_API_URL", "SOKKAN_FORGE_SOCKET", "SOKKAN_FORGE_TICKET",   # git credential helper
    "SOKKAN_RELAY_ADDR", "SOKKAN_RELAY_TOKEN",          # MCP relay inside a session pod
    "SOKKAN_SESSION_ARGV", "SOKKAN_SESSION_BUFFER", "SOKKAN_SESSION_CWD", "SOKKAN_SESSION_TOKEN",
    "SOKKAN_SUPERVISOR_PORT", "SOKKAN_EGRESS_ALLOW", "SOKKAN_EGRESS_PORT",  # session pod / egress proxy
    "SOKKAN_OPERATOR", "SOKKAN_SECRETS_EXPORT_PASSFILE", "SOKKAN_SECRETS_EXPORT_PASSPHRASE",  # CLI run by a human
}
PATH_OVERRIDE = {  # where a store lives; defaults under SOKKAN_DATA_DIR — tests only
    "SOKKAN_AGENTS_DB", "SOKKAN_ALERTING_DB", "SOKKAN_AUDIT_DB", "SOKKAN_BOARD_DB", "SOKKAN_IAM_DB",
    "SOKKAN_PROJECTS_DB", "SOKKAN_SHARES_DB", "SOKKAN_TEAMS_DB", "SOKKAN_USAGE_DB", "SOKKAN_MEMORY_DB",
    "SOKKAN_CONNECT_AI_STORE", "SOKKAN_MODELKEYS_STORE", "SOKKAN_NAVPREFS_STORE", "SOKKAN_INSTANCE_CONFIG",
    "SOKKAN_LLM_CONFIG", "SOKKAN_PREVIEW_ENVS", "SOKKAN_PREVIEW_TRIGGER", "SOKKAN_FORGE_KEY_FILE",
    "SOKKAN_TEAMS_KEY_FILE", "SOKKAN_FORGE_SOCKET_DIR", "CORTHEXIS_MIGRATION_DIR", "CORTHEXIS_MEMORY_DIR",
    "CORTHEXIS_DATA_DIR", "CORTHEXIS_MEMORY_DB", "SOKKAN_APPLIANCE_DIR",
    "SOKKAN_LOCAL_ACCOUNTS_DB",
}
OTHER_RUNTIME = {  # not the compose install: Kubernetes chart, systemd / dev checkout, the OS
    "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "SOKKAN_K8S_API", "SOKKAN_K8S_CA",
    "SOKKAN_K8S_NAMESPACE", "SOKKAN_K8S_SECRETS_PREFIX", "SOKKAN_K8S_START_TIMEOUT_S",
    "SOKKAN_K8S_TOKEN_FILE", "SOKKAN_OPENBAO_K8S_JWT_FILE", "SOKKAN_OPENBAO_K8S_MOUNT",
    "SOKKAN_FORGE_POD_HELPER",
    "SOKKAN_CF_AUD", "SOKKAN_CF_TEAM", "SOKKAN_DEV_USER",   # cf-access mode: our own systemd instance
    "SOKKAN_PROJECT_DIR", "SOKKAN_PYTHON", "SOKKAN_API_PORT", "SOKKAN_RECALL_API_URL",  # fixed in the image
    "SOKKAN_SPAWN_TMUX", "SOKKAN_TTYD", "TMUX_PANE",        # tmux sessions: host installs only
    "DOCKER_HOST",
    "CORTHEXIS_ML_SERVICE_URL", "CORTHEXIS_LEGACY_MODEL",   # standalone CortHeXis names of ML_SERVICE_URL / SOKKAN_EMBED_MODEL
    "CORTHEXIS_MEMORY_BACKEND", "CORTHEXIS_EMBED_URL", "CORTHEXIS_EMBED_MODEL_ID",
    "CORTHEXIS_EMBED_MAX_TOKENS", "CORTHEXIS_RERANK_POLICY", "CORTHEXIS_SEARCH_RERANK",
    "CORTHEXIS_PRIORITY_BOOST", "CORTHEXIS_DATABASE_URL",
    "CORTHEXIS_RECALL_SEUIL",                               # pre-3.0 name of CORTHEXIS_RECALL_THRESHOLD
}
CLASSIFIED = IMAGE | PER_SESSION | PATH_OVERRIDE | OTHER_RUNTIME

_READ = re.compile(
    r"""(?:environ(?:\.get)?\s*[\(\[]\s*|getenv\(\s*|env_num\(\s*|\benv\.get\(\s*|Var\(\s*)["']([A-Z][A-Z0-9_]{2,})["']""")
_IN = re.compile(r"""["']([A-Z][A-Z0-9_]{2,})["']\s+in\s+os\.environ""")
# memory/: `env("X")`, `env_int("X")`, `_env("X")`… read CORTHEXIS_X, then SOKKAN_X
_MEM_ENV = re.compile(
    r"""\b(?:_env|env|env_int|env_bool|env_list|env_float|env_name)\(\s*["']([A-Z][A-Z0-9_]{2,})["']""")
_MEM_F = re.compile(r"""\bf\(\s*["']([A-Z][A-Z0-9_]{2,})["']\s*,""")
_FEATURE_CFG = re.compile(r'"((?:SOKKAN|CORTHEXIS)_[A-Z0-9_]+)"')


def env_reads() -> dict[str, set[str]]:
    """variable → files that read it (backend/ and memory/, tests excluded)."""
    out: dict[str, set[str]] = {}
    for d in CODE_DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            rel = f.relative_to(ROOT)
            if "tests" in rel.parts or "__pycache__" in rel.parts or rel.as_posix() in OTHER_SERVICE:
                continue
            src = f.read_text()
            names = set(_READ.findall(src)) | set(_IN.findall(src))
            names = {n for n in names if not n.endswith("_")}     # "CORTHEXIS_" + name
            if d == "memory":       # + the local `f("X", default)` closures of the from_env()
                names |= {"CORTHEXIS_" + n for n in _MEM_ENV.findall(src) + _MEM_F.findall(src)}
            if rel.as_posix() == "backend/features.py":      # `config=` of the registry
                names |= set(_FEATURE_CFG.findall(src))
            for n in names:
                out.setdefault(n, set()).add(rel.as_posix())
    if ROOT == Path(__file__).resolve().parent.parent:
        # the switches of the feature registry are read by name built at run time
        # (SOKKAN_FEATURE_<ID>): take them from the registry itself
        sys.path.insert(0, str(ROOT / "backend"))
        import features
        for f in features.REGISTRY:
            for n in (*(v.name for v in f.vars), *f.config):
                out.setdefault(n, set()).add("backend/features.py")
    return out


def api_env(overrides: bool = False) -> dict:
    """services.api.environment of docker-compose.yml (+ what the optional override files add:
    the docker runner sets its own variables when it is switched on)."""
    env = dict(yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["api"]["environment"])
    if overrides:
        for f in OVERRIDES:
            env.update(yaml.safe_load((ROOT / f).read_text())["services"].get("api", {}).get("environment") or {})
    return env


def _declared(name: str, env: dict) -> bool:
    if name in env:
        return True
    # memory reads CORTHEXIS_X then SOKKAN_X: either name in the compose file reaches it
    for a, b in (("CORTHEXIS_", "SOKKAN_"), ("SOKKAN_", "CORTHEXIS_")):
        if name.startswith(a) and b + name[len(a):] in env:
            return True
    return False


def test_every_variable_the_backend_reads_is_declared_or_classified():
    env, reads = api_env(overrides=True), env_reads()
    assert len(reads) > 150, "the scanner lost its patterns"
    missing = {n: sorted(f)[:2] for n, f in sorted(reads.items())
               if not _declared(n, env) and n not in CLASSIFIED}
    assert not missing, (
        "read by the backend but never passed to the api container — declare each in "
        "docker-compose.yml services.api.environment (and .env.example if an operator sets it), "
        f"or classify it in tests/test_compose_env.py with the reason: {missing}")


def test_the_scanner_sees_a_new_read(tmp_path, monkeypatch):
    """Mutation guard: a variable read in a new backend file is reported."""
    fake = tmp_path / "backend"
    fake.mkdir()
    (fake / "x.py").write_text('import os\nA = os.environ.get("SOKKAN_BRAND_NEW")\n'
                               'B = os.environ["SOKKAN_OTHER_NEW"]\nC = os.getenv(\'SOKKAN_THIRD\')\n')
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "y.py").write_text('v = _env("SHINY")\nw = f("KNOB", 1)\n')
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert set(env_reads()) == {"SOKKAN_BRAND_NEW", "SOKKAN_OTHER_NEW", "SOKKAN_THIRD", "CORTHEXIS_SHINY",
                                "CORTHEXIS_KNOB"}


def test_classification_is_honest():
    env, reads = api_env(), env_reads()
    stale = sorted(n for n in CLASSIFIED if n not in reads)
    assert not stale, f"classified but no longer read anywhere — remove from the lists: {stale}"
    both = sorted(n for n in (PER_SESSION | IMAGE) if n in env)
    assert not both, f"per-session / image variables must not be set on the api service: {both}"
    docker = (ROOT / "docker" / "api.Dockerfile").read_text()
    not_in_image = sorted(n for n in IMAGE if not re.search(rf"\b{n}=", docker))
    assert not not_in_image, f"classified as set by the image but absent from api.Dockerfile: {not_in_image}"


# variables the api container receives for a program that is not our python code
PASSED_THROUGH = {
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",   # read by the Claude CLI
    "CORTHEXIS_MODELS_DIR",                            # memory/core/models.py (`_env("MODELS_DIR")`)
}


def test_no_dead_declaration_in_the_api_block():
    """A variable declared for the api but read by nothing is a setting without effect."""
    env, reads = api_env(), env_reads()
    blob = "\n".join(p.read_text() for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")
                     if "__pycache__" not in p.parts)
    dead = sorted(n for n in env if n not in reads and n not in PASSED_THROUGH
                  and not re.search(rf"\b{n}\b", blob))
    assert not dead, f"declared in services.api.environment but read nowhere: {dead}"


def test_environment_reference_is_up_to_date():
    """docs/ENVIRONMENT.md lists every variable of the api block (default, where it is read,
    what the compose file says about it) — generated: scripts/gen-env-doc.py."""
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-env-doc.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = (ROOT / "docs" / "ENVIRONMENT.md").read_text()
    missing = sorted(n for n in api_env() if f"`{n}`" not in doc)
    assert not missing, missing


def test_router_service_receives_every_variable_shiprouter_reads():
    """Same guard for the appliance's `router` service (backend/shiprouter.py + llmerrors.py)."""
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["router"]
    env = set(svc["environment"])
    read = set()
    for f in ("shiprouter.py", "llmerrors.py"):
        src = (ROOT / "backend" / f).read_text()
        read |= set(_READ.findall(src)) | set(re.findall(r"""\be\(\s*["']([A-Z][A-Z0-9_]{2,})["']""", src))
    assert len(read) >= 15, read
    assert not sorted(read - env), f"declare in services.router.environment: {sorted(read - env)}"
    assert not sorted(env - read), f"declared for the router but read nowhere: {sorted(env - read)}"
    assert svc["profiles"] == ["appliance"] and "ports" not in svc and "volumes" not in svc
    assert svc["user"] == "1000:1000" and svc["read_only"] is True
