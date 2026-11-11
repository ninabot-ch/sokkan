"""3.6.3: the memory of a project created OUTSIDE the api process gets its indexer.

The appliance creates a person's project from its command line (`accounts.sh add --project`,
the installer's SOKKAN_ACCOUNTS). No route of the running api is called, and the indexers of
the project memories were started at boot and by the « create a project » route only: the
notes of such a project stayed on disk — in nobody's list, in no recall — until the api
restarted. The api now looks for new projects every minute."""
import threading

import pytest


@pytest.fixture()
def api(tmp_path, monkeypatch):
    import app as a
    import core.indexer as indexer
    import projects
    import store_backend

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(projects, "DB", tmp_path / "projects.db")
    projects.init(force=True)
    started: list[str] = []

    class Runner:
        def __init__(self, _store, _embedder, cfg, **_kw):
            self.cfg = cfg

        def start(self):
            started.append(self.cfg.project)

    class Store:
        def active_generation(self):
            return object()

    monkeypatch.setattr(indexer, "IndexRunner", Runner)
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    monkeypatch.setattr(store_backend, "get_store", lambda: Store())
    monkeypatch.setattr(a, "_index_runner", object())
    monkeypatch.setattr(a, "_project_runners", {})
    return a, projects, started, tmp_path


def _settle():
    for t in threading.enumerate():
        if t.name.startswith("sokkan-index-") and t is not threading.current_thread():
            t.join(timeout=5)


def test_a_project_created_by_another_process_gets_its_indexer(api):
    a, projects, started, tmp = api
    a._sync_project_indexers()                       # what the boot does
    _settle()
    at_boot = set(started)
    assert "atelier" not in at_boot
    # the appliance's command line creates the project: another process, no route
    projects.create("atelier", "Atelier", created_by="cli")
    a._project_indexers_loop(every=0.01, rounds=2)   # the minute loop, two looks
    _settle()
    assert started.count("atelier") == 1, started    # started once, not once per look
    assert (tmp / "projects" / "atelier" / "memory").is_dir()
    assert set(started) - at_boot == {"atelier"}


def test_a_project_whose_folder_cannot_be_prepared_is_tried_again(api, monkeypatch):
    a, projects, started, _ = api
    import store_backend
    projects.create("atelier", "Atelier", created_by="cli")
    real = store_backend.memory_dir_for
    calls = {"n": 0}

    def flaky(slug):
        if slug == "atelier":
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk not ready")
        return real(slug)

    monkeypatch.setattr(store_backend, "memory_dir_for", flaky)
    a._project_indexers_loop(every=0.01, rounds=3)   # the first look fails and is said, not fatal
    _settle()
    assert started.count("atelier") == 1, started
