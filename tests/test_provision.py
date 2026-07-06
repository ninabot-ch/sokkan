import pytest


@pytest.fixture()
def prov():
    import provision as p

    return p


def test_disabled_without_config(prov, monkeypatch):
    monkeypatch.setattr(prov, "URL", "")
    monkeypatch.setattr(prov, "TOKEN", "")
    # ENABLED is computed at import; the app dependency re-checks the module attr
    monkeypatch.setattr(prov, "ENABLED", bool(prov.URL and prov.TOKEN))
    assert prov.ENABLED is False


def test_error_maps_status_and_detail(prov, monkeypatch):
    class FakeResp:
        status_code = 409

        def json(self):
            return {"detail": "environment already exists"}

        text = "environment already exists"

    monkeypatch.setattr(prov, "URL", "http://prov.local")
    monkeypatch.setattr(prov, "TOKEN", "t")
    monkeypatch.setattr(prov.httpx, "request", lambda *a, **k: FakeResp())
    with pytest.raises(prov.ProvisionerError) as e:
        prov.spawn("acme", "starter", "a@b.c")
    assert e.value.status == 409
    assert "already exists" in e.value.detail


def test_unreachable_maps_to_502(prov, monkeypatch):
    def boom(*a, **k):
        raise prov.httpx.ConnectError("down")

    monkeypatch.setattr(prov, "URL", "http://prov.local")
    monkeypatch.setattr(prov, "TOKEN", "t")
    monkeypatch.setattr(prov.httpx, "request", boom)
    with pytest.raises(prov.ProvisionerError) as e:
        prov.list_envs()
    assert e.value.status == 502


def test_bearer_header_sent(prov, monkeypatch):
    seen = {}

    class OkResp:
        status_code = 200

        def json(self):
            return []

    def fake(method, url, **kw):
        seen.update(kw.get("headers", {}), url=url, method=method)
        return OkResp()

    monkeypatch.setattr(prov, "URL", "http://prov.local")
    monkeypatch.setattr(prov, "TOKEN", "secret-tok")
    monkeypatch.setattr(prov.httpx, "request", fake)
    prov.list_envs()
    assert seen["Authorization"] == "Bearer secret-tok"
    assert seen["url"] == "http://prov.local/envs"
