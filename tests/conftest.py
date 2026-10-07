import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "memory"))

# Every module resolves its SQLite paths from SOKKAN_DATA_DIR at import. Several test modules
# set it only at their own import, after another module may already have been imported with
# the default (~/.local/share/sokkan = a real instance's data on a host that runs SOKKAN).
# A throw-away default for the whole run: a test never reads or writes a real instance.
os.environ.setdefault("SOKKAN_DATA_DIR", tempfile.mkdtemp(prefix="sokkan-tests-"))


@pytest.fixture(autouse=True)
def _isolated_projects_db(tmp_path, monkeypatch):
    """3.2: one projects.db per test (the default project is created on first use)."""
    import projects

    monkeypatch.setattr(projects, "DB", tmp_path / "projects-isolated.db")
    monkeypatch.setattr(projects, "_initialized_for", None)
    yield
