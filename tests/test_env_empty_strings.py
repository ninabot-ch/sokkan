"""docker compose passes `${VAR:-}` as an empty string: every numeric setting must treat "" as its default
(the 3.1.2 demo rollout crashed on `int('')` for SOKKAN_ASSISTANT_DAILY_LIMIT)."""
import importlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def test_env_num_treats_empty_as_default(monkeypatch):
    import features
    monkeypatch.setenv("SOKKAN_X", "")
    assert features.env_num("SOKKAN_X", 50) == 50
    monkeypatch.setenv("SOKKAN_X", " 7 ")
    assert features.env_num("SOKKAN_X", 50) == 7
    monkeypatch.delenv("SOKKAN_X")
    assert features.env_num("SOKKAN_X", 2.5, float) == 2.5


def test_backend_imports_with_every_compose_var_empty(monkeypatch, tmp_path):
    """Set every SOKKAN_* variable declared in docker-compose.yml to "" and import the modules that read numbers."""
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in sorted(set(re.findall(r"^\s+(SOKKAN_[A-Z0-9_]+):", compose, re.M))):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    for mod in ("features", "notify", "assistant", "agents_runtime"):
        sys.modules.pop(mod, None)
        importlib.import_module(mod)


def test_no_bare_numeric_env_reads():
    """Guard: no `int(os.environ.get(...))` / `float(os.environ.get(...))` left in backend/."""
    bad = []
    for p in (ROOT / "backend").rglob("*.py"):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if re.search(r"\b(int|float)\(os\.environ\.get\(", line) and " or " not in line:
                bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, bad
