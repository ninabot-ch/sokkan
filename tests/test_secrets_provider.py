"""3.3 secrets providers — one contract for `file`, `openbao` and `kubernetes`, then OpenBao
for real: KV v2, transit envelope, AppRole, Kubernetes auth, token renewal, migration,
backup/restore, rotation.

OpenBao tests need a REAL server (they skip otherwise):

    docker run -d --name sokkan-bao-test -p 127.0.0.1:58200:8200 openbao/openbao:2.4.1 \\
        server -dev -dev-root-token-id=root -dev-listen-address=0.0.0.0:8200
    SOKKAN_TEST_OPENBAO_ADDR=http://127.0.0.1:58200 SOKKAN_TEST_OPENBAO_TOKEN=root pytest …
    docker rm -f sokkan-bao-test

The root token only SETS UP each test (mounts, policy, AppRole, transit key); SOKKAN itself
talks to OpenBao with an AppRole bound to the least-privilege policy of docs/enterprise/
SECRETS.md (or a Kubernetes-auth role). Every test uses its own instance id (own KV prefix,
own transit key). The Kubernetes API is `tests/fake_k8s.py` (Secrets + TokenReview).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_k8s import FakeK8s  # noqa: E402

BAO = os.environ.get("SOKKAN_TEST_OPENBAO_ADDR", "")
BAO_ROOT = os.environ.get("SOKKAN_TEST_OPENBAO_TOKEN", "")
needs_bao = pytest.mark.skipif(not (BAO and BAO_ROOT), reason="SOKKAN_TEST_OPENBAO_ADDR/_TOKEN not set")


# ---- OpenBao test setup (root token, setup only) --------------------------------------------
def bao(method, path, body=None, token=BAO_ROOT, ok=(200, 204), ns=""):
    h = {"X-Vault-Token": token}
    if ns:
        h["X-Vault-Namespace"] = ns
    r = httpx.request(method, f"{BAO}/v1/{path}", json=body, headers=h, timeout=10)
    assert r.status_code in ok, f"{method} {path}: {r.status_code} {r.text}"
    return r.json() if r.content else {}


def policy_hcl(inst: str, kv: str = "secret", transit: str = "transit") -> str:
    """The least-privilege policy of docs/enterprise/SECRETS.md § 3 (kept identical)."""
    return f'''
path "{kv}/data/sokkan/{inst}/*"     {{ capabilities = ["create", "read", "update", "delete"] }}
path "{kv}/metadata/sokkan/{inst}/*" {{ capabilities = ["list", "read", "delete"] }}
path "{transit}/encrypt/sokkan-{inst}" {{ capabilities = ["update"] }}
path "{transit}/decrypt/sokkan-{inst}" {{ capabilities = ["update"] }}
path "{transit}/rewrap/sokkan-{inst}"  {{ capabilities = ["update"] }}
path "{transit}/keys/sokkan-{inst}"    {{ capabilities = ["read"] }}
'''


_mounted = False


def _mounts(ns: str = ""):
    global _mounted
    if _mounted and not ns:
        return
    have = bao("GET", "sys/mounts", ns=ns)
    have = have.get("data", have)
    if "transit/" not in have:
        bao("POST", "sys/mounts/transit", {"type": "transit"}, ns=ns)
    if "secret/" not in have:
        bao("POST", "sys/mounts/secret", {"type": "kv", "options": {"version": "2"}}, ns=ns)
    auths = bao("GET", "sys/auth", ns=ns)
    auths = auths.get("data", auths)
    if "approle/" not in auths:
        bao("POST", "sys/auth/approle", {"type": "approle"}, ns=ns)
    if not ns:
        _mounted = True


def setup_instance(inst: str, token_ttl: str = "1h", ns: str = "") -> dict:
    _mounts(ns)
    bao("POST", f"transit/keys/sokkan-{inst}", {"type": "aes256-gcm96"}, ns=ns)
    bao("PUT", f"sys/policies/acl/sokkan-{inst}", {"policy": policy_hcl(inst)}, ns=ns)
    bao("POST", f"auth/approle/role/sokkan-{inst}",
        {"token_policies": [f"sokkan-{inst}"], "token_ttl": token_ttl, "token_max_ttl": "24h",
         "secret_id_ttl": "1h"}, ns=ns)
    rid = bao("GET", f"auth/approle/role/sokkan-{inst}/role-id", ns=ns)["data"]["role_id"]
    sid = bao("POST", f"auth/approle/role/sokkan-{inst}/secret-id", {}, ns=ns)["data"]["secret_id"]
    return {"role_id": rid, "secret_id": sid}


def operator_token(inst: str) -> str:
    """Rotation is an operator action: a token with the rotate policy, not the api's AppRole."""
    bao("PUT", f"sys/policies/acl/sokkan-{inst}-rotate", {"policy": policy_hcl(inst) + f'''
path "transit/keys/sokkan-{inst}/rotate" {{ capabilities = ["update"] }}
'''})
    return bao("POST", "auth/token/create", {"policies": [f"sokkan-{inst}-rotate"],
                                              "ttl": "10m"})["auth"]["client_token"]


# ---- fixtures ---------------------------------------------------------------------------------
@pytest.fixture()
def data(tmp_path, monkeypatch):
    import secrets_provider as sp
    import vault
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(d))
    monkeypatch.setattr(vault, "KEY_PATH", str(d / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(d / "vault.json"))
    for k in list(os.environ):
        if k.startswith(("SOKKAN_OPENBAO_", "SOKKAN_K8S_", "SOKKAN_SECRETS_PROVIDER",
                         "SOKKAN_FORGE_KEY_FILE", "SOKKAN_TEAMS_KEY_FILE")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SOKKAN_FEATURE_SECRETS_PROVIDER", "1")
    sp.reset()
    yield d
    sp.reset()


def use_openbao(monkeypatch, inst: str, creds: dict, ns: str = ""):
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "openbao")
    monkeypatch.setenv("SOKKAN_OPENBAO_ADDR", BAO)
    monkeypatch.setenv("SOKKAN_OPENBAO_ROLE_ID", creds["role_id"])
    monkeypatch.setenv("SOKKAN_OPENBAO_SECRET_ID", creds["secret_id"])
    monkeypatch.setenv("SOKKAN_INSTANCE_ID", inst)
    if ns:
        monkeypatch.setenv("SOKKAN_OPENBAO_NAMESPACE", ns)


@pytest.fixture()
def k8s(monkeypatch, tmp_path):
    fake = FakeK8s()
    tok = tmp_path / "sa-token"
    tok.write_text(fake.token)
    monkeypatch.setenv("SOKKAN_K8S_API", fake.url)
    monkeypatch.setenv("SOKKAN_K8S_NAMESPACE", "sokkan")
    monkeypatch.setenv("SOKKAN_K8S_TOKEN_FILE", str(tok))
    yield fake
    fake.close()


@pytest.fixture(params=["file", "openbao", "kubernetes"])
def provider(request, data, monkeypatch, tmp_path):
    import secrets_provider as sp
    name = request.param
    if name == "openbao":
        if not (BAO and BAO_ROOT):
            pytest.skip("no OpenBao test server")
        inst = "t" + secrets.token_hex(4)
        use_openbao(monkeypatch, inst, setup_instance(inst))
    elif name == "kubernetes":
        fake = FakeK8s()
        request.addfinalizer(fake.close)
        tok = tmp_path / "sa-token"
        tok.write_text(fake.token)
        monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "kubernetes")
        monkeypatch.setenv("SOKKAN_K8S_API", fake.url)
        monkeypatch.setenv("SOKKAN_K8S_NAMESPACE", "sokkan")
        monkeypatch.setenv("SOKKAN_K8S_TOKEN_FILE", str(tok))
    else:
        monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    p = sp.active()
    assert p.name == name
    return p


def _all_bytes(d: Path) -> bytes:
    return b"\n".join(p.read_bytes() for p in d.rglob("*") if p.is_file())


# ---- the contract (3 providers) ----------------------------------------------------------------
def test_contract_secrets_crud(provider):
    assert provider.list("default") == [] and provider.get("default", "NOPE") is None
    provider.set("default", "DB_PASSWORD", "s3cr3t é ✓")
    provider.set("default", "API_TOKEN", "tok-1")
    provider.set("radio", "API_TOKEN", "tok-radio")
    assert provider.get("default", "DB_PASSWORD") == "s3cr3t é ✓"
    assert provider.list("default") == ["API_TOKEN", "DB_PASSWORD"]
    assert provider.list("radio") == ["API_TOKEN"]
    assert provider.get("radio", "API_TOKEN") == "tok-radio"   # projects never mix
    assert set(provider.projects()) == {"default", "radio"}
    provider.set("default", "API_TOKEN", "tok-2")
    assert provider.get_all("default") == {"API_TOKEN": "tok-2", "DB_PASSWORD": "s3cr3t é ✓"}
    assert provider.get_all("default", only=["API_TOKEN"]) == {"API_TOKEN": "tok-2"}
    provider.delete("default", "API_TOKEN")
    provider.delete("default", "NEVER_SET")                       # idempotent
    assert provider.list("default") == ["DB_PASSWORD"]
    assert provider.get("default", "API_TOKEN") is None


def test_contract_encrypt_per_context(provider):
    from cryptography.fernet import InvalidToken
    t = provider.encrypt("forge", "gitlab-oauth-token")
    assert t != "gitlab-oauth-token" and provider.decrypt("forge", t) == "gitlab-oauth-token"
    assert provider.decrypt("forge", provider.encrypt("forge", "x")) == "x"
    with pytest.raises(InvalidToken):
        provider.decrypt("teams", t)                 # contexts do not share a key
    # stable across a fresh process view (same data keys)
    import secrets_provider as sp
    sp.reset()
    assert sp.active().decrypt("forge", t) == "gitlab-oauth-token"


def test_contract_data_key_rotation(provider):
    from cryptography.fernet import InvalidToken
    old = provider.encrypt("vault", "before")
    k1 = provider.data_keys("vault")
    assert len(k1) == 1
    k2 = provider.add_data_key("vault")
    assert provider.data_keys("vault") == [k2, k1[0]]
    assert provider.decrypt("vault", old) == "before"            # old still opens
    new = provider.encrypt("vault", "after")
    re_old = provider.fernet("vault").rotate(old.encode()).decode()
    assert provider.add_data_key("vault") == k2                  # resumable, no third key
    assert provider.drop_old_data_keys("vault") == 1
    assert provider.data_keys("vault") == [k2]
    assert provider.decrypt("vault", new) == "after"
    assert provider.decrypt("vault", re_old) == "before"
    with pytest.raises(InvalidToken):
        provider.decrypt("vault", old)                           # the dropped key is gone


def test_contract_import_data_keys_idempotent(provider):
    from cryptography.fernet import Fernet

    import secrets_provider as sp
    k = Fernet.generate_key()
    provider.import_data_keys("teams", [k])
    provider.import_data_keys("teams", [k])                      # same: no-op
    assert provider.data_keys("teams", create=False) == [k]
    if provider.name != "file":
        with pytest.raises(sp.SecretsError):
            provider.import_data_keys("teams", [Fernet.generate_key()])


def test_contract_health_and_describe(provider):
    h = provider.health()
    assert h.ok, h.detail
    d = json.dumps(provider.describe())
    for leak in ("secret_id", "SECRET", "token\":", "role_id"):
        assert leak not in d


def test_no_clear_key_on_disk(provider, data):
    """openbao / kubernetes: after real use, no file of the data dir holds a data key."""
    if provider.name == "file":
        pytest.skip("the file provider IS key files")
    provider.set("default", "X", "y")
    for ctx in ("vault", "forge", "teams"):
        provider.encrypt(ctx, "v")
    blob = _all_bytes(data)
    for ctx in ("vault", "forge", "teams"):
        for k in provider.data_keys(ctx):
            assert k not in blob
            assert base64.urlsafe_b64decode(k) not in blob
    assert not list(data.glob("*.key"))


# ---- the cockpit's modules go through the provider ----------------------------------------------
def test_modules_use_the_provider(provider, data, monkeypatch, tmp_path):
    import modelkeys
    import vault
    from forge import gitcred, links
    from teams import signing, store

    monkeypatch.setattr(modelkeys, "store_path", lambda: str(data / "modelkeys.json"))
    monkeypatch.setenv("SOKKAN_TEAMS_DB", str(data / "teams.db"))
    vault.set_secret("STRIPE_KEY", "sk_live_x", project="default")
    assert vault.names("default") == ["STRIPE_KEY"]
    assert vault.session_env(["STRIPE_KEY"], project="default") == {"STRIPE_KEY": "sk_live_x"}
    assert vault.session_env([], project="default") == {}
    rec = modelkeys.set_key("instance", "anthropic", "sk-ant-api03-abcdefgh", "admin@x.ch")
    assert rec["masked"].endswith("efgh")
    assert modelkeys.get_plain("instance:anthropic") == "sk-ant-api03-abcdefgh"
    t = links._enc("glpat-123")
    assert links._dec(t) == "glpat-123"
    tk = gitcred.ticket("sid1", "Dev@x.ch")
    assert gitcred.verify(tk) == ("sid1", "dev@x.ch")
    store.put_token("app:x", "graph-token", time.time() + 3600)
    assert store.get_token("app:x") == "graph-token"
    appr = signing.issue("run", "7", "default", "dev@x.ch")
    assert str(signing.peek(appr).get("r", "7")) == "7"
    if provider.name != "file":
        assert "sk_live_x" not in _all_bytes(data).decode("latin-1")
        assert not (data / "vault.json").exists()


def test_rotation_of_a_context_keeps_every_module_readable(provider, data, monkeypatch):
    """scripts/secrets-rotate.py --data-keys: new key, every value re-encrypted, old key gone."""
    import modelkeys
    from forge import gitcred, links
    from secrets_provider import cli
    from teams import store

    monkeypatch.setattr(modelkeys, "store_path", lambda: str(data / "modelkeys.json"))
    monkeypatch.setenv("SOKKAN_TEAMS_DB", str(data / "teams.db"))
    import vault
    vault.set_secret("A", "alpha")
    modelkeys.set_key("instance", "openai", "sk-openai-12345678", "admin@x.ch")
    store.put_token("app:y", "tok-y", time.time() + 3600)
    con = links._con()
    con.execute("INSERT INTO forge_links(email, provider, base_url, forge_user_id, forge_username,"
                " token_enc, refresh_enc, scopes, expires_at, linked_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("dev@x.ch", "gitlab", "https://gl.example", "1", "dev", links._enc("acc"),
                 links._enc("ref"), "api", time.time() + 999, time.time()))
    con.commit()
    con.close()
    before = {c: provider.data_keys(c) for c in ("vault", "forge", "teams")}
    tk = gitcred.ticket("s", "dev@x.ch")
    assert cli.main(["rotate", "--data-keys"]) == 0
    for c in ("vault", "forge", "teams"):
        after = provider.data_keys(c)
        assert len(after) == 1 and after != before[c]
    assert vault.session_env(["A"]) == {"A": "alpha"}
    assert modelkeys.get_plain("instance:openai") == "sk-openai-12345678"
    assert store.get_token("app:y") == "tok-y"
    row = links.get("dev@x.ch", "gitlab", "https://gl.example")
    assert links._dec(row["token_enc"]) == "acc" and links._dec(row["refresh_enc"]) == "ref"
    assert gitcred.verify(tk) is None          # tickets of the old forge key: sessions re-issue
    log = (data / "secrets-ops.log").read_text()
    assert '"event": "secrets.rotate"' in log and "alpha" not in log


# ---- selection ----------------------------------------------------------------------------------
def test_selection_rules(monkeypatch):
    import secrets_provider as sp
    env = {"SOKKAN_EDITION": "enterprise"}
    assert sp.selected(env)[0] == "file" and sp.warning(env)
    env["SOKKAN_OPENBAO_ADDR"] = "https://bao:8200"
    assert sp.selected(env) == ("openbao", "SOKKAN_OPENBAO_ADDR is set")
    assert sp.warning(env) is None
    env["SOKKAN_SECRETS_PROVIDER"] = "kubernetes"
    assert sp.selected(env)[0] == "kubernetes"
    community = {"SOKKAN_OPENBAO_ADDR": "https://bao:8200", "SOKKAN_SECRETS_PROVIDER": "openbao"}
    name, why = sp.selected(community)                # feature off by default in community
    assert name == "file" and "ignored" in why
    assert sp.warning(community) is None              # community: no warning
    community["SOKKAN_FEATURE_SECRETS_PROVIDER"] = "1"
    assert sp.selected(community)[0] == "openbao"
    assert sp.selected({"SOKKAN_FEATURE_SECRETS_PROVIDER": "1",
                        "SOKKAN_SECRETS_PROVIDER": "nope"})[0] == "file"


def test_file_provider_refuses_to_mint_over_wrapped_keys(data, monkeypatch):
    """Feature switched off on an OpenBao instance: never a fresh key (values unreadable)."""
    import secrets_provider as sp
    (data / "forge.key.wrapped").write_text("{}")
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    with pytest.raises(sp.SecretsError, match="wrapped by OpenBao"):
        sp.encrypt("forge", "x")
    assert not (data / "forge.key").exists()


def test_openbao_refuses_clear_keys_not_migrated(data, monkeypatch):
    import secrets_provider as sp
    from secrets_provider.openbao import BaoClient, OpenBaoProvider
    (data / "vault.key").write_bytes(b"x" * 44)
    p = OpenBaoProvider(BaoClient("http://127.0.0.1:1", auth="token", token="t"))
    with pytest.raises(sp.SecretsError, match="secrets-migrate"):
        p.data_keys("vault")


def test_openbao_tls_rules():
    import secrets_provider as sp
    from secrets_provider.openbao import BaoClient
    with pytest.raises(sp.SecretsError, match="plain http"):
        BaoClient("http://bao.internal:8200")
    BaoClient("http://bao.internal:8200", allow_http=True)
    BaoClient("https://bao.internal:8200", cacert="/etc/ssl/ca.pem")
    with pytest.raises(sp.SecretsError):
        BaoClient("bao:8200")


def test_secrets_error_is_a_503_without_detail_leak(data, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import secrets_provider as sp
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "openbao")
    monkeypatch.setenv("SOKKAN_OPENBAO_ADDR", "http://127.0.0.1:9")   # nothing listens
    monkeypatch.setenv("SOKKAN_OPENBAO_TOKEN", "s.never-shown")
    monkeypatch.setenv("SOKKAN_OPENBAO_TIMEOUT_S", "1")
    sp.reset()

    @a.app.get("/__t_secret")
    def _t():
        sp.encrypt("vault", "x")

    r = TestClient(a.app).get("/__t_secret")
    assert r.status_code in (401, 503)
    assert "never-shown" not in r.text


# ---- OpenBao, for real ----------------------------------------------------------------------------
@needs_bao
def test_openbao_kv_layout_and_policy(data, monkeypatch):
    """Values land in KV v2 under sokkan/<instance>/<project>/<NAME>; the AppRole's policy
    cannot read another instance's prefix nor rotate the transit key."""
    import secrets_provider as sp
    inst, other = "t" + secrets.token_hex(4), "t" + secrets.token_hex(4)
    use_openbao(monkeypatch, inst, setup_instance(inst))
    setup_instance(other)
    bao("POST", f"secret/data/sokkan/{other}/default/FOREIGN", {"data": {"value": "no"}})
    sp.reset()
    p = sp.active()
    p.set("default", "DB_PASSWORD", "pw")
    j = bao("GET", f"secret/data/sokkan/{inst}/default/DB_PASSWORD")
    assert j["data"]["data"] == {"value": "pw"}
    with pytest.raises(sp.SecretsError, match="403"):
        p.c.call("GET", f"secret/data/sokkan/{other}/default/FOREIGN")
    with pytest.raises(sp.SecretsError, match="403"):
        p.c.call("POST", f"transit/keys/sokkan-{inst}/rotate", {})
    # the wrapped file holds a transit ciphertext only
    p.encrypt("forge", "x")
    w = json.loads((data / "forge.key.wrapped").read_text())
    assert w["keys"][0]["ciphertext"].startswith("vault:v1:")
    assert (data / "forge.key.wrapped").stat().st_mode & 0o777 == 0o600


@needs_bao
def test_openbao_token_renewal_and_relogin(data, monkeypatch):
    import secrets_provider as sp
    inst = "t" + secrets.token_hex(4)
    use_openbao(monkeypatch, inst, setup_instance(inst, token_ttl="6s"))
    sp.reset()
    p = sp.active()
    p.set("default", "A", "1")
    first = p.c._tok
    assert p.c._ttl <= 6 and p.c._renewable
    time.sleep(4.2)                                  # past 2/3 of the TTL → renew-self
    assert p.get("default", "A") == "1"
    assert p.c._tok == first and p.c.token_info()["age_s"] < 2
    bao("POST", "auth/token/revoke", {"token": first})   # revoked behind our back
    assert p.get("default", "A") == "1"              # 403 → one fresh AppRole login
    assert p.c._tok != first


def _dummy_ca() -> str:
    """A PEM the config requires (the fake API is plain http on the docker bridge)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-k8s")])
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key())
         .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
         .sign(k, hashes.SHA256()))
    return c.public_bytes(serialization.Encoding.PEM).decode()


@needs_bao
def test_openbao_kubernetes_auth(data, monkeypatch, tmp_path):
    """Kubernetes auth: OpenBao reviews the ServiceAccount JWT with the (fake) TokenReview API."""
    import secrets_provider as sp
    inst = "t" + secrets.token_hex(4)
    setup_instance(inst)
    fake = FakeK8s(bind="0.0.0.0")
    try:
        host = os.environ.get("SOKKAN_TEST_OPENBAO_HOST_FROM_CONTAINER", "172.17.0.1")
        mount = f"k8s-{inst}"
        bao("POST", f"sys/auth/{mount}", {"type": "kubernetes"})
        bao("POST", f"auth/{mount}/config", {"kubernetes_host": f"http://{host}:{fake.port}",
                                              "disable_local_ca_jwt": True,
                                              "kubernetes_ca_cert": _dummy_ca(),
                                              "token_reviewer_jwt": fake.token})
        bao("POST", f"auth/{mount}/role/sokkan", {
            "bound_service_account_names": ["sokkan-api"],
            "bound_service_account_namespaces": ["sokkan"],
            "token_policies": [f"sokkan-{inst}"], "token_ttl": "1h"})
        jwt = fake.sa_jwt("sokkan", "sokkan-api")
        (tmp_path / "jwt").write_text(jwt)
        monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "openbao")
        monkeypatch.setenv("SOKKAN_OPENBAO_ADDR", BAO)
        monkeypatch.setenv("SOKKAN_OPENBAO_AUTH", "kubernetes")
        monkeypatch.setenv("SOKKAN_OPENBAO_K8S_ROLE", "sokkan")
        monkeypatch.setenv("SOKKAN_OPENBAO_K8S_MOUNT", mount)
        monkeypatch.setenv("SOKKAN_OPENBAO_K8S_JWT_FILE", str(tmp_path / "jwt"))
        monkeypatch.setenv("SOKKAN_INSTANCE_ID", inst)
        sp.reset()
        p = sp.active()
        assert p.c.auth == "kubernetes"
        p.set("default", "K", "v")
        assert p.get("default", "K") == "v"
        assert fake.reviews and fake.reviews[-1] == jwt
        # another ServiceAccount of the namespace is refused by the role binding
        (tmp_path / "jwt").write_text(fake.sa_jwt("sokkan", "intruder"))
        sp.reset()
        with pytest.raises(sp.SecretsError, match="login"):
            sp.active().get("default", "K")
    finally:
        fake.close()


@needs_bao
def test_openbao_namespace(data, monkeypatch):
    import secrets_provider as sp
    ns = "tenant" + secrets.token_hex(3)
    r = httpx.post(f"{BAO}/v1/sys/namespaces/{ns}", headers={"X-Vault-Token": BAO_ROOT},
                   json={}, timeout=10)
    if r.status_code not in (200, 204):
        pytest.skip(f"namespaces not available on this server ({r.status_code})")
    inst = "t" + secrets.token_hex(4)
    use_openbao(monkeypatch, inst, setup_instance(inst, ns=ns), ns=ns)
    sp.reset()
    p = sp.active()
    p.set("default", "IN_NS", "1")
    assert bao("GET", f"secret/data/sokkan/{inst}/default/IN_NS", ns=ns)["data"]["data"]["value"] == "1"
    assert httpx.get(f"{BAO}/v1/secret/data/sokkan/{inst}/default/IN_NS",
                     headers={"X-Vault-Token": BAO_ROOT}).status_code == 404


def _populate_file_instance(data: Path, monkeypatch):
    import modelkeys
    import vault
    from teams import store
    monkeypatch.setattr(modelkeys, "store_path", lambda: str(data / "modelkeys.json"))
    monkeypatch.setenv("SOKKAN_TEAMS_DB", str(data / "teams.db"))
    vault.set_secret("DB_PASSWORD", "pw-default", project="default")
    vault.set_secret("API_TOKEN", "tok-default", project="default")
    import secrets_provider as sp
    sp.active().set("radio", "RADIO_KEY", "pw-radio")       # a 2nd project's namespace
    modelkeys.set_key("instance", "anthropic", "sk-ant-api03-zzzzzzzz", "admin@x.ch")
    forge_ct = sp.encrypt("forge", "glpat-forge")
    store.put_token("app:graph", "graph-tok", time.time() + 3600)
    return forge_ct


@needs_bao
def test_migration_file_to_openbao_and_back(data, monkeypatch, tmp_path):
    import modelkeys
    import secrets_provider as sp
    import vault
    from secrets_provider import cli
    from teams import store

    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    forge_ct = _populate_file_instance(data, monkeypatch)
    clear = {k: (data / k).read_bytes() for k in ("vault.key", "forge.key", "teams.key")}

    inst = "t" + secrets.token_hex(4)
    use_openbao(monkeypatch, inst, setup_instance(inst))
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")    # the api still runs on files
    sp.reset()
    assert cli.main(["migrate", "--from", "file", "--to", "openbao", "--dry-run"]) == 0
    assert not (data / "vault.key.wrapped").exists()
    assert cli.main(["migrate", "--from", "file", "--to", "openbao"]) == 0
    assert cli.main(["migrate", "--from", "file", "--to", "openbao"]) == 0   # idempotent
    # a value changed in files after the copy is REPORTED, not overwritten
    sp.make("file").set("default", "API_TOKEN", "tok-changed")
    assert cli.main(["migrate", "--from", "file", "--to", "openbao", "--check"]) == 1
    assert cli.main(["migrate", "--from", "file", "--to", "openbao", "--overwrite"]) == 0
    assert cli.main(["migrate", "--from", "file", "--to", "openbao", "--purge"]) == 0

    # no clear key left; vault.json kept aside as ciphertext; marker written
    for k in clear:
        assert not (data / k).exists()
    assert (data / "vault.json.pre-openbao").exists() and not (data / "vault.json").exists()
    assert json.loads((data / "secrets-provider.json").read_text())["provider"] == "openbao"
    blob = _all_bytes(data)
    for k in clear.values():
        assert k not in blob

    # the api restarted on openbao reads everything
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "openbao")
    sp.reset()
    assert vault.session_env(None, project="default") == {"DB_PASSWORD": "pw-default",
                                                          "API_TOKEN": "tok-changed"}
    assert sp.active().get("radio", "RADIO_KEY") == "pw-radio"
    assert modelkeys.get_plain("instance:anthropic") == "sk-ant-api03-zzzzzzzz"
    assert sp.decrypt("forge", forge_ct) == "glpat-forge"
    assert store.get_token("app:graph") == "graph-tok"

    # passphrase export (openbao → bundle) and import back to files (rollback)
    monkeypatch.setenv("SOKKAN_SECRETS_EXPORT_PASSPHRASE", "correct horse battery")
    bundle = tmp_path / "export.enc"
    assert cli.main(["export", "--out", str(bundle)]) == 0
    raw = bundle.read_text()
    assert "pw-default" not in raw and bundle.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("SOKKAN_SECRETS_EXPORT_PASSPHRASE", "wrong passphrase!!")
    assert cli.main(["import", "--in", str(bundle), "--to", "file"]) == 2
    monkeypatch.setenv("SOKKAN_SECRETS_EXPORT_PASSPHRASE", "correct horse battery")
    assert cli.main(["migrate", "--from", "openbao", "--to", "file"]) == 0
    for k, v in clear.items():
        assert (data / k).read_bytes() == v           # the same data keys, back in files
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    for w in data.glob("*.key.wrapped"):
        w.unlink()                                    # rollback done: back to 3.2
    sp.reset()
    assert vault.session_env(None, project="default")["API_TOKEN"] == "tok-changed"
    assert sp.decrypt("forge", forge_ct) == "glpat-forge"
    assert cli.main(["import", "--in", str(bundle), "--to", "file"]) == 0   # idempotent


@needs_bao
def test_master_rotation_rewraps(data, monkeypatch):
    import secrets_provider as sp
    from secrets_provider import cli
    inst = "t" + secrets.token_hex(4)
    use_openbao(monkeypatch, inst, setup_instance(inst))
    sp.reset()
    ct = sp.encrypt("vault", "kept")
    before = sp.all_data_keys("vault")
    assert cli.main(["rotate", "--master"]) == 2          # the api's AppRole cannot rotate
    monkeypatch.delenv("SOKKAN_OPENBAO_ROLE_ID")
    monkeypatch.delenv("SOKKAN_OPENBAO_SECRET_ID")
    monkeypatch.setenv("SOKKAN_OPENBAO_TOKEN", operator_token(inst))
    sp.reset()
    assert cli.main(["rotate", "--master"]) == 0
    w = json.loads((data / "vault.key.wrapped").read_text())
    assert w["keys"][0]["ciphertext"].startswith("vault:v2:")
    sp.reset()
    assert sp.all_data_keys("vault") == before and sp.decrypt("vault", ct) == "kept"
    assert '"latest_version": 2' in (data / "secrets-ops.log").read_text()


@needs_bao
def test_backup_restore_openbao_mode(data, monkeypatch, tmp_path):
    """backup.sh in openbao mode: no key in the set (not even inside data.tgz); restore.sh
    checks the wrapped keys unwrap BEFORE touching anything, and --import-secrets puts the KV
    values back."""
    import secrets_provider as sp
    from secrets_provider import cli
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    forge_ct = _populate_file_instance(data, monkeypatch)
    inst = "t" + secrets.token_hex(4)
    creds = setup_instance(inst)
    use_openbao(monkeypatch, inst, creds)
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    assert cli.main(["migrate", "--from", "file", "--to", "openbao", "--purge"]) == 0
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "openbao")
    sp.reset()
    keys = [k for c in ("vault", "forge", "teams") for k in sp.all_data_keys(c)]

    env = {k: v for k, v in os.environ.items() if not k.startswith(("SOKKAN_", "CORTHEXIS_"))}
    env.update({k: v for k, v in os.environ.items() if k.startswith(("SOKKAN_OPENBAO_",
                                                                     "SOKKAN_SECRETS_",
                                                                     "SOKKAN_FEATURE_SECRETS"))})
    env.update(SOKKAN_BACKUP_MODE="local", SOKKAN_DATA_DIR=str(data), SOKKAN_INSTANCE_ID=inst,
               SOKKAN_PYTHON=sys.executable, SOKKAN_BACKUP_KEY_PASSPHRASE="unused here")
    out = tmp_path / "out"
    r = subprocess.run(["sh", str(ROOT / "scripts/backup.sh"), str(out)], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    (s,) = [p for p in out.iterdir() if p.name.startswith("sokkan-backup-")]
    names = {p.name for p in s.iterdir()}
    assert {"MANIFEST", "data.tgz", "OPENBAO_REQUIRED.txt", "secrets.openbao.json"} <= names
    assert not [n for n in names if n.endswith((".key", ".key.enc"))]
    m = (s / "MANIFEST").read_text()
    assert "secrets_provider: openbao" in m and "vault_key: openbao" in m
    every = _all_bytes(s)
    with tarfile.open(s / "data.tgz") as t:
        members = t.getnames()
        every += b"".join(t.extractfile(x).read() for x in t.getmembers() if x.isfile())
    assert any(x.endswith("vault.key.wrapped") for x in members)
    for k in keys:
        assert k not in every and base64.urlsafe_b64decode(k) not in every
    for v in (b"pw-default", b"pw-radio", b"glpat-forge", b"sk-ant-api03"):
        assert v not in every
    r = subprocess.run(["sh", str(ROOT / "scripts/backup.sh"), str(out), "--include-plain-key"],
                       env=env, cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode != 0 and "--include-plain-key" in r.stderr

    # disaster: data dir and KV values lost
    shutil.rmtree(data)
    data.mkdir()
    p = sp.active()
    for proj in p.projects():
        for n in p.list(proj):
            p.delete(proj, n)
    assert p.projects() == []

    # a restore against an OpenBao that cannot unwrap (other instance's AppRole) touches nothing
    other = "t" + secrets.token_hex(4)
    oc = setup_instance(other)
    bad = dict(env, SOKKAN_OPENBAO_ROLE_ID=oc["role_id"], SOKKAN_OPENBAO_SECRET_ID=oc["secret_id"],
               SOKKAN_INSTANCE_ID=other)
    (data / "sentinel").write_text("x")
    r = subprocess.run(["sh", str(ROOT / "scripts/restore.sh"), str(s), "--yes"], env=bad,
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode != 0 and "nothing was changed" in r.stderr
    assert (data / "sentinel").exists()

    r = subprocess.run(["sh", str(ROOT / "scripts/restore.sh"), str(s), "--yes",
                        "--import-secrets"], env=env, cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stderr
    assert not (data / "sentinel").exists() and not list(data.glob("*.key"))
    sp.reset()
    import vault
    assert vault.session_env(None, project="default") == {"DB_PASSWORD": "pw-default",
                                                          "API_TOKEN": "tok-default"}
    assert sp.active().get("radio", "RADIO_KEY") == "pw-radio"
    assert sp.decrypt("forge", forge_ct) == "glpat-forge"


def test_backup_file_mode_keeps_every_key_file_out_of_data(data, tmp_path, monkeypatch):
    """3.3 also closes a 3.2 gap: forge.key / teams.key were inside data.tgz in clear."""
    import secrets_provider as sp
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    for c in ("vault", "forge", "teams"):
        sp.encrypt(c, "x")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SOKKAN_", "CORTHEXIS_"))}
    env.update(SOKKAN_BACKUP_MODE="local", SOKKAN_DATA_DIR=str(data),
               SOKKAN_BACKUP_KEY_PASSPHRASE="correct horse battery staple")
    out = tmp_path / "out"
    r = subprocess.run(["sh", str(ROOT / "scripts/backup.sh"), str(out)], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    (s,) = list(out.iterdir())
    assert {"vault.key.enc", "forge.key.enc", "teams.key.enc"} <= {p.name for p in s.iterdir()}
    with tarfile.open(s / "data.tgz") as t:
        assert not [n for n in t.getnames() if n.endswith(".key")]
    keys = {k: (data / k).read_bytes() for k in ("vault.key", "forge.key", "teams.key")}
    shutil.rmtree(data)
    r = subprocess.run(["sh", str(ROOT / "scripts/restore.sh"), str(s), "--yes"], env=env,
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    for k, v in keys.items():
        assert (data / k).read_bytes() == v and (data / k).stat().st_mode & 0o777 == 0o600


def test_setup_secrets_routes(data, monkeypatch):
    """GET state + POST « Test connection » (instance admin), journaled, nothing secret."""
    from fastapi.testclient import TestClient

    import app as a
    import audit
    import auth
    import secrets_provider as sp
    monkeypatch.setattr(audit, "DB", data / "audit.db")
    monkeypatch.setenv("SOKKAN_EDITION", "enterprise")
    monkeypatch.setenv("SOKKAN_SECRETS_PROVIDER", "file")
    sp.reset()
    who = {"u": {"email": "admin@x.ch", "role": "admin"}}
    a.app.dependency_overrides[auth.current_user] = lambda: who["u"]
    monkeypatch.setattr(auth, "instance_user", lambda _r: who["u"])
    try:
        c = TestClient(a.app)
        st = c.get("/api/admin/secrets-provider").json()
        assert st["provider"] == "file" and "OpenBao" in (st["warning"] or "")
        t = c.post("/api/admin/secrets-provider/test").json()
        assert t["ok"] and t["provider"] == "file"
        assert c.get("/api/admin/secrets-provider").json()["last_test"]["ok"]
        who["u"] = {"email": "dev@x.ch", "role": "dev"}
        assert c.get("/api/admin/secrets-provider").status_code == 403
        assert c.post("/api/admin/secrets-provider/test").status_code == 403
    finally:
        a.app.dependency_overrides.clear()
    assert any(r["action"] == "secrets.test" for r in audit.recent(limit=10))
