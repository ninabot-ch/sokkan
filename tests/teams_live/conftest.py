"""tests/teams_live — the Teams checks that only a REAL tenant can answer (TEAMS.md § 6 and § 8).

Skipped unless ``SOKKAN_TEAMS_LIVE=1`` (no network otherwise). ``manual`` tests also need
``SOKKAN_TEAMS_LIVE_MANUAL=1`` and a person in Teams: they print what to do and wait for the
event in the instance's journal. See README.md.
"""
import os

import pytest

from _live import need

LIVE = os.environ.get("SOKKAN_TEAMS_LIVE") == "1"
MANUAL = os.environ.get("SOKKAN_TEAMS_LIVE_MANUAL") == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "manual: needs a person acting in Microsoft Teams")


def pytest_collection_modifyitems(config, items):
    here = os.path.dirname(__file__)
    for it in items:
        if not str(it.fspath).startswith(here):
            continue
        if not LIVE:
            it.add_marker(pytest.mark.skip(reason="live Teams tenant checks: set SOKKAN_TEAMS_LIVE=1 "
                                                  "(tests/teams_live/README.md)"))
        elif it.get_closest_marker("manual") and not MANUAL:
            it.add_marker(pytest.mark.skip(reason="manual step in Teams: set SOKKAN_TEAMS_LIVE_MANUAL=1"))


@pytest.fixture()
def live(monkeypatch, tmp_path):
    """The backend `teams` package against Microsoft's real endpoints (public cloud), with a
    throw-away data dir (token cache, keys)."""
    v = need("SOKKAN_TEAMS_APP_ID", "SOKKAN_TEAMS_APP_PASSWORD", "SOKKAN_TEAMS_TENANT_ID")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_TEAMS_DB", str(tmp_path / "teams.db"))
    for k in ("SOKKAN_TEAMS_OPENID_URL", "SOKKAN_TEAMS_LOGIN_URL", "SOKKAN_TEAMS_GRAPH_URL"):
        monkeypatch.delenv(k, raising=False)
    import teams
    monkeypatch.setattr(teams, "TRANSPORT", None)
    return v


@pytest.fixture()
def instance():
    """An httpx client to the SOKKAN instance (admin cookie when given)."""
    import httpx
    v = need("SOKKAN_TEAMS_LIVE_INSTANCE")
    cookies = {}
    if os.environ.get("SOKKAN_TEAMS_LIVE_ADMIN_COOKIE"):
        cookies["sokkan_session"] = os.environ["SOKKAN_TEAMS_LIVE_ADMIN_COOKIE"]
    with httpx.Client(base_url=v["SOKKAN_TEAMS_LIVE_INSTANCE"].rstrip("/"), cookies=cookies,
                      timeout=20) as c:
        yield c
