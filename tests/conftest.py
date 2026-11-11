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


@pytest.fixture(autouse=True)
def _schema_follows_the_database(monkeypatch):
    """`iam` and `board` create their tables once per PROCESS (`_initialized`) — right for an
    instance, whose database never moves. A test that points `DB` at its own file and calls
    `init(force=True)` leaves the flag up when its `DB` is put back: the next test that uses the
    default database, in the same process, finds no table (« no such table: users / sessions »).
    Seen on the public CI, which runs the whole suite in ONE process (3.6.0: 2 tests red there,
    green file by file). Each test starts with both flags down: the first access creates what is
    missing (`CREATE TABLE IF NOT EXISTS`) in whichever database the test uses."""
    import board
    import iam

    monkeypatch.setattr(iam, "_initialized", False)
    monkeypatch.setattr(board, "_initialized", False)
    yield


@pytest.fixture(autouse=True)
def _host_binaries_present(monkeypatch):
    """3.6: tmux and Preview are OFF on a host without their binary (features._which). The
    suite must not depend on what the machine that runs it has installed — found when the
    integration ran on a build host with no Chromium: `preview` off, hence `shared_review`
    off, hence 12 unrelated tests red. Default for every test: the binaries are there; the
    tests about their absence patch `_which` themselves."""
    import features

    monkeypatch.setattr(features, "_which", lambda binary: f"/usr/bin/{binary.rsplit('/', 1)[-1]}")
    yield


@pytest.fixture()
def model_credentials(monkeypatch):
    """An instance WITH model credentials explicitly configured (3.2 scheduler guard).
    Tests that drive the scheduler as a working instance use it; the guard itself is
    tested without it (test_scheduler_guard.py)."""
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    yield
