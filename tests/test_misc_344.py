"""3.4.4 — défauts trouvés au smoke e2e du 09.10.2026 (lot misc)."""
import asyncio
import os
import tempfile
import threading

import pytest

_TMP = tempfile.mkdtemp()
os.environ.update(
    SOKKAN_DATA_DIR=_TMP, CLAUDE_CONFIG_DIR=f"{_TMP}/claude",
    SOKKAN_MEMORY_DIR=f"{_TMP}/memory", SOKKAN_AGENT_CWD=_TMP,
    SOKKAN_LOCAL_TOKEN="", SOKKAN_OWNER_EMAIL="owner@localhost",
    SOKKAN_UPDATE_CHECK="0",
)


@pytest.fixture()
def api_loop():
    """La boucle de l'API, comme le lifespan la pose, dans son propre thread."""
    import app as a
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    saved = getattr(a, "_main_loop", None)
    a._main_loop = loop
    try:
        yield loop
    finally:
        a._main_loop = saved
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)
        loop.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import board
    import iam
    saved = {"iam": iam.DB, "board": board.DB}
    iam.DB, board.DB = tmp_path / "iam.db", tmp_path / "board.db"
    iam.init(force=True)
    board.init(force=True)
    try:
        yield TestClient(a.app)  # sans lifespan : la boucle vient de api_loop
    finally:
        iam.DB, board.DB = saved["iam"], saved["board"]


def test_bg_from_a_sync_route_runs_on_the_api_loop(api_loop):
    """Une route `def` tourne dans le pool de threads : pas de boucle courante.
    Avant 3.4.4 : RuntimeError « no running event loop » → 500."""
    import app as a
    ran = threading.Event()

    async def job():
        ran.set()

    out = {}

    def from_thread():
        try:
            out["f"] = a._bg(job())
        except Exception as e:  # noqa: BLE001
            out["err"] = e

    t = threading.Thread(target=from_thread)
    t.start()
    t.join(2)
    assert "err" not in out, out.get("err")
    assert ran.wait(2)


def test_memory_digest_spawns_and_seeds(client, api_loop, monkeypatch):
    import agentchat
    import app as a
    seeded = threading.Event()

    class FakeSession:
        def _emit(self, ev):
            pass

        async def handle_user(self, text):
            seeded.set()

    monkeypatch.setattr(agentchat, "get_or_create", lambda *a_, **k: FakeSession())
    monkeypatch.setattr(a, "_memory_preseed", lambda *a_, **k: "")
    r = client.post("/api/memory/digest")
    assert r.status_code == 200, r.text
    assert seeded.wait(2), "the digest session was created but never seeded"


def test_session_cost_is_not_re_added_each_turn():
    """Smoke 09.10 : total_cost_usd est cumulé par processus CLI ; 4 tours affichaient
    0,0402 $ pour 0,0122 $ réels (×3,3, croît en n²)."""
    pytest.importorskip("claude_agent_sdk")
    import agentchat
    from claude_agent_sdk import ResultMessage

    s = agentchat.AgentSession("sid-cost-344")
    for total in (0.0069, 0.0090, 0.0111, 0.0132):
        s._translate(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                   is_error=False, num_turns=1, session_id="x",
                                   total_cost_usd=total, usage={"input_tokens": 10}))
    assert round(s.cost_usd, 4) == 0.0132
    res = [e for e in s.events if e["type"] == "result"]
    assert round(res[-1]["cost_usd"], 4) == 0.0021 and res[-1]["session_cost_usd"] == 0.0132
    # processus relancé (resume après redémarrage) : son compteur repart de 0
    s._cli_cost_seen = 0.0
    s._translate(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                               is_error=False, num_turns=1, session_id="x",
                               total_cost_usd=0.003, usage={}))
    assert round(s.cost_usd, 4) == 0.0162


def test_update_check_reads_version_file_and_compares_versions(monkeypatch):
    """Smoke 09.10 : sans SOKKAN_VERSION (install systemd) la version locale valait
    « dev » → jamais de mise à jour signalée ; et dist/VERSION porte « x.y.z+commit »."""
    import importlib

    import updatecheck
    monkeypatch.delenv("SOKKAN_VERSION", raising=False)
    u = importlib.reload(updatecheck)
    from pathlib import Path
    assert u.LOCAL == (Path(u.__file__).resolve().parent.parent / "VERSION").read_text().strip()

    class R:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr(u, "LOCAL", "3.4.3")
    monkeypatch.setattr(u.urllib.request, "urlopen", lambda *a, **k: R(b"3.4.4+abc1234\n"))
    u._check_once()
    assert u._state["update_available"] is True
    monkeypatch.setattr(u.urllib.request, "urlopen", lambda *a, **k: R(b"3.4.3+b22bce3\n"))
    u._check_once()
    assert u._state["update_available"] is False  # même version, autre suffixe


def test_fastembed_model_loaded_once_per_process(monkeypatch, tmp_path):
    """Smoke 09.10 : store_backend.embedder() reconstruit un LegacyEmbedder toutes les 10 s ;
    chacun rechargeait TextEmbedding (4-13 s) → rappel mémoire > délai du hook (5 s)."""
    import sys
    import types

    from core import embed
    loads = []

    class FakeTE:
        def __init__(self, model_name, cache_dir):
            loads.append(model_name)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTE))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))
    if hasattr(embed, "_FASTEMBED"):
        monkeypatch.setattr(embed, "_FASTEMBED", {})
    a = embed.LegacyEmbedder(None, "sentence-transformers/all-MiniLM-L6-v2")
    b = embed.LegacyEmbedder(None, "sentence-transformers/all-MiniLM-L6-v2")
    assert a._fastembed() is b._fastembed()
    assert len(loads) == 1


def test_usage_refresh_is_serialised(monkeypatch):
    """Prod 09.10 : Costs en 500 au 1er chargement (proxy coupé pendant le re-parse).
    3.4.4 : rafraîchisseur en fond + refresh() sérialisé — jamais deux parses à la fois."""
    import time as _t

    import usage
    live, peak = [0], [0]

    def slow():
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        _t.sleep(0.2)
        live[0] -= 1

    monkeypatch.setattr(usage, "_refresh", slow, raising=False)
    monkeypatch.setattr(usage, "_con", lambda: (_ for _ in ()).throw(AssertionError("parsed")),
                        raising=False)
    ts = [threading.Thread(target=usage.refresh) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(3)
    assert peak[0] == 1


def test_snap_chromium_shoots_outside_hidden_dirs(tmp_path, monkeypatch):
    """Smoke 09.10 : chromium snap n'écrit pas dans ~/.local/share/sokkan/preview (caché)
    → Preview « screenshot failed » (502) sur sokkan.ninabot.ch."""
    from pathlib import Path

    import preview
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hidden = Path("/root/.local/share/sokkan/preview/x.png")
    p = preview._snap_safe_path("/snap/bin/chromium", hidden)
    assert not any(part.startswith(".") for part in p.parts) and p.name == "x.png"
    assert preview._snap_safe_path("/usr/bin/chromium", hidden) == hidden   # not a snap
    plain = Path("/srv/sokkan/preview/x.png")
    assert preview._snap_safe_path("/snap/bin/chromium", plain) == plain    # already fine
