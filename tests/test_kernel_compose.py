"""4.0 — the kernel engine for a REAL instance (docker-compose.kernel.yml, docs/KERNEL.md):
the configuration the api renders for the kernel (backend/harness/kernel_config.py), the
instance label of its sandboxes, the compose file's isolation (socket proxy, internal networks,
no port, read-only), the egress filter of the sandboxes, scripts/kernel.sh, and what a person
sees when the sandbox cannot start (503 sandbox_unavailable)."""
import asyncio
import importlib.util
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
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, os.path.dirname(__file__))

from harness import identity, kernel, kernel_config  # noqa: E402

KFILE = ROOT / "docker-compose.kernel.yml"
NEW_SERVICES = ("kernel-init", "kernel-db", "kernel-migrate", "kernel-dockerproxy",
                "kernel-egress-filter", "kernel-egress", "kernel")
ENV = {"SOKKAN_KERNEL_SANDBOX_IMAGE": "ghcr.io/example/sandbox:1.2.3",
       "SOKKAN_KERNEL_DATA_HOST": "/var/lib/docker/volumes/x_sokkan-data/_data",
       "SOKKAN_KERNEL_WORKSPACE_HOST": "/srv/sokkan/workspace"}
BASE = {"tiers": {"ship": {"base_url": "http://router:8096/v1", "model": "ship"}},
        "local": {"reasoning": False, "max_calls": 60, "memory_direct": True,
                  "code": {"backend": "local", "roots": ["/elsewhere"],
                           "host_volumes": {"/data": "vol"}}}}


def _compose() -> dict:
    return yaml.safe_load(KFILE.read_text())


def _egress_filter():
    spec = importlib.util.spec_from_file_location("egress_filter", ROOT / "deploy/kernel/egress_filter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the configuration rendered for the kernel ------------------------------------------

def test_render_puts_the_docker_sandbox_and_the_host_flags_on_top_of_the_operators_file():
    cfg = kernel_config.render(BASE, ENV, "sokkan-0123456789abcdef")
    assert cfg["tiers"] == BASE["tiers"]                    # the operator's part is kept
    local = cfg["local"]
    assert local["instance"] == "sokkan-0123456789abcdef"
    assert local["state_dir"] == "/opt/data" and local["token_path"] == "/run/kernel/local-token"
    for flag in ("surfaces_gate", "strict_resume", "mcp_session_headers", "conclude_on_green"):
        assert local[flag] is True, flag
    assert local["chain_max_calls"] == 60 and "memory_direct" not in local
    code = local["code"]
    assert code["backend"] == "docker" and code["require_sandbox"] is True   # never `local`
    assert code["image"] == ENV["SOKKAN_KERNEL_SANDBOX_IMAGE"]
    assert code["roots"] == ["/workspace", "/data/projects", "/data/engine-roots"]
    d = ENV["SOKKAN_KERNEL_DATA_HOST"]
    assert code["host_roots"] == {"/workspace": ENV["SOKKAN_KERNEL_WORKSPACE_HOST"],
                                  "/data/projects": f"{d}/projects",
                                  "/data/engine-roots": f"{d}/engine-roots"}
    assert "host_volumes" not in code       # the socket proxy refuses a named volume
    # caps on by default, network off by default
    assert (code["memory"], code["cpus"], code["idle_ttl_s"]) == ("2g", 2, 1800)
    assert code["network"] is False and code["network_name_env"] == "NOKKER_SANDBOX_NETWORK"
    assert BASE["local"]["code"]["backend"] == "local"      # the input is not modified
    # every root the kernel may hand a sandbox is one the proxy lets it mount
    assert set(code["host_roots"].values()) == set(kernel_config.proxy_workspaces(ENV).split(":"))


@pytest.mark.parametrize("net, on", [("none", False), ("registries", True), ("open", True)])
def test_the_sandbox_network_follows_the_setting(net, on):
    cfg = kernel_config.render({}, {**ENV, "SOKKAN_KERNEL_SANDBOX_NET": net}, "sokkan-0123456789abcdef")
    assert cfg["local"]["code"]["network"] is on


def test_caps_are_settings_of_the_env():
    env = {**ENV, "SOKKAN_KERNEL_SANDBOX_MEMORY": "512m", "SOKKAN_KERNEL_SANDBOX_CPUS": "1.5",
           "SOKKAN_KERNEL_SANDBOX_IDLE_S": "0", "SOKKAN_KERNEL_CHAIN_MAX_CALLS": "30"}
    cfg = kernel_config.render({}, env, "sokkan-0123456789abcdef")
    code = cfg["local"]["code"]
    assert (code["memory"], code["cpus"], code["idle_ttl_s"]) == ("512m", 1.5, 0)
    assert cfg["local"]["chain_max_calls"] == 30


@pytest.mark.parametrize("name, value", [
    ("SOKKAN_KERNEL_DATA_HOST", ""), ("SOKKAN_KERNEL_DATA_HOST", "relative/path"),
    ("SOKKAN_KERNEL_DATA_HOST", "/"), ("SOKKAN_KERNEL_WORKSPACE_HOST", "/a/../b"),
    ("SOKKAN_KERNEL_SANDBOX_IMAGE", ""), ("SOKKAN_KERNEL_SANDBOX_MEMORY", "2gb"),
    ("SOKKAN_KERNEL_SANDBOX_MEMORY", "32m"), ("SOKKAN_KERNEL_SANDBOX_CPUS", "0"),
    ("SOKKAN_KERNEL_SANDBOX_CPUS", "1.234"), ("SOKKAN_KERNEL_SANDBOX_IDLE_S", "30"),
    ("SOKKAN_KERNEL_SANDBOX_NET", "lan"), ("SOKKAN_KERNEL_CHAIN_MAX_CALLS", "0"),
])
def test_a_wrong_setting_stops_with_its_name(name, value):
    env = {**ENV, name: value}
    if value == "":
        env.pop(name)
    with pytest.raises(kernel_config.ConfigError) as e:
        kernel_config.render({}, env, "sokkan-0123456789abcdef")
    assert name in str(e.value)


def test_main_writes_the_kernels_file_and_prepares_the_mounted_folders(tmp_path, monkeypatch):
    src, out, data = tmp_path / "src", tmp_path / "out", tmp_path / "data"
    src.mkdir(), out.mkdir()
    shutil.copy(ROOT / "deploy/kernel/config.example.yaml", src / "config.example.yaml")
    for k, v in {**ENV, "SOKKAN_DATA_DIR": str(data), "SOKKAN_KERNEL_CONFIG_SRC": str(src),
                 "SOKKAN_KERNEL_CONFIG_OUT": str(out / "config.yaml")}.items():
        monkeypatch.setenv(k, v)
    assert kernel_config.main() == 0
    cfg = yaml.safe_load((out / "config.yaml").read_text())
    assert cfg["local"]["code"]["backend"] == "docker"
    assert cfg["local"]["instance"] == identity.instance_label("kernel")
    assert cfg["tiers"]["ship"]["base_url"] == "http://router:8096/v1"     # from the example
    assert (data / "projects").is_dir() and (data / "engine-roots").is_dir()
    # the operator's own file wins over the example
    (src / "config.yaml").write_text("tiers: {ship: {base_url: 'http://mine/v1', model: m}}\n")
    assert kernel_config.main() == 0
    assert yaml.safe_load((out / "config.yaml").read_text())["tiers"]["ship"]["base_url"] == "http://mine/v1"
    # a wrong setting: exit 2, nothing new written (the entrypoint removes the old file)
    monkeypatch.setenv("SOKKAN_KERNEL_SANDBOX_NET", "everything")
    assert kernel_config.main() == 2


# ---- local.instance: stable, per instance, never a path ---------------------------------

def test_the_instance_label_is_stable_per_instance_and_never_from_a_path(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(a))
    one = identity.instance_label("kernel")
    assert re.fullmatch(r"sokkan-[0-9a-f]{16}", one)
    assert identity.instance_label("kernel") == one                       # stable
    assert identity.instance_label("other") != one                        # per purpose
    # another instance (its own key) on the same daemon: another label
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(b))
    two = identity.instance_label("kernel")
    assert two != one
    # the SAME instance moved elsewhere (its data restored to another folder): the same label —
    # the label follows the key, not the path
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.copy(a / "engine-identity.key", moved / "engine-identity.key")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(moved))
    assert identity.instance_label("kernel") == one
    assert stat.S_IMODE((moved / "engine-identity.key").stat().st_mode) in (0o600, 0o644, 0o640)


# ---- the compose file -------------------------------------------------------------------

def test_the_kernel_never_gets_the_docker_socket_and_nothing_publishes_a_port():
    c = _compose()
    svcs = c["services"]
    for name in NEW_SERVICES:
        assert svcs[name]["profiles"] == ["kernel"], name
        assert "ports" not in svcs[name], name
    sock = [n for n, s in svcs.items() for v in s.get("volumes", []) if "docker.sock" in str(v)]
    assert sock == ["kernel-dockerproxy"]
    k = svcs["kernel"]
    assert k["environment"]["DOCKER_HOST"] == "tcp://kernel-dockerproxy:2375"
    assert k["user"] == "1000:1000" and "group_add" not in k
    # a tier without a key is « no model » for the kernel: never empty by default
    assert k["environment"]["SOKKAN_KERNEL_MODEL_KEY"].endswith(":-local}")
    for name in ("kernel", "kernel-migrate", "kernel-dockerproxy", "kernel-egress-filter", "kernel-egress"):
        s = svcs[name]
        assert s["read_only"] is True and s["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in s["security_opt"], name
        assert "cap_add" not in s and not s.get("privileged"), name
    assert svcs["kernel-dockerproxy"]["environment"]["NOKKER_SANDBOX_EGRESS"] == "http://kernel-egress-filter:3128"


def test_networks_isolate_the_sandboxes_and_the_socket_proxy():
    c = _compose()
    nets, svcs = c["networks"], c["services"]
    assert nets["kernel-dockerapi"]["internal"] is True
    assert nets["kernel-sandbox"]["internal"] is True and nets["kernel-egress"]["internal"] is True
    assert "${SOKKAN_KERNEL_SANDBOX_NETWORK" in nets["kernel-sandbox"]["name"]
    on = {n: set(s.get("networks", [])) for n, s in svcs.items() if n in NEW_SERVICES}
    assert on["kernel-dockerproxy"] == {"kernel-dockerapi"}
    assert on["kernel"] == {"default", "kernel-dockerapi"}           # never the sandboxes' network
    assert on["kernel-egress-filter"] == {"kernel-sandbox", "kernel-egress"}
    assert on["kernel-egress"] == {"kernel-egress", "kernel-outbound"}
    who = {n for n, s in on.items() if "kernel-sandbox" in s}
    assert who == {"kernel-egress-filter"}
    # the kernel and the proxy agree on the network's name, and the kernel reads it from its env
    proxy_net = svcs["kernel-dockerproxy"]["environment"]["NOKKER_SANDBOX_NETWORK"]
    assert svcs["kernel"]["environment"][kernel_config.NETWORK_ENV] == proxy_net
    assert proxy_net in nets["kernel-sandbox"]["name"]


def test_the_kernel_sees_the_projects_and_nothing_else_of_sokkans_data():
    k = _compose()["services"]["kernel"]
    data = [v for v in k["volumes"] if isinstance(v, dict) and v.get("source") == "sokkan-data"]
    assert sorted((v["target"], v["volume"]["subpath"]) for v in data) == [
        ("/data/engine-roots", "engine-roots"), ("/data/projects", "projects")]
    assert not [v for v in k["volumes"] if isinstance(v, str) and v.startswith("sokkan-data")]
    assert "kernel-config:/etc/kernel:ro" in k["volumes"]
    # the folders the kernel sees are the roots of its configuration
    roots = {kernel_config.WORKSPACE, kernel_config.PROJECTS, kernel_config.ENGINE_ROOTS}
    seen = {v["target"] for v in data} | {v.rsplit(":", 1)[1] for v in k["volumes"]
                                          if isinstance(v, str) and v.endswith(":/workspace")}
    assert seen == roots


def test_every_setting_the_render_reads_reaches_the_api_and_the_filter():
    svcs = _compose()["services"]
    api_env = svcs["api"]["environment"]
    src = (ROOT / "backend/harness/kernel_config.py").read_text()
    read = set(re.findall(r'"(SOKKAN_KERNEL_[A-Z_]+)"', src))
    assert len(read) >= 10, read
    assert not sorted(read - set(api_env)), f"declare in the api of {KFILE.name}: {sorted(read - set(api_env))}"
    declared = {n for n in api_env if n.startswith("SOKKAN_KERNEL_")}
    used = read | set(re.findall(r'"(SOKKAN_KERNEL_[A-Z_]+)"', (ROOT / "backend/harness/kernel.py").read_text()))
    assert not sorted(declared - used), f"declared for the api, read nowhere: {sorted(declared - used)}"
    flt = (ROOT / "deploy/kernel/egress_filter.py").read_text()
    fread = set(re.findall(r'environ\.get\("([A-Z_]+)"', flt)) - {"LOG_LEVEL", "LISTEN_PORT"}
    assert fread == set(svcs["kernel-egress-filter"]["environment"])
    # the same default for the network setting on both sides
    assert api_env["SOKKAN_KERNEL_SANDBOX_NET"] == svcs["kernel-egress-filter"]["environment"]["SOKKAN_KERNEL_SANDBOX_NET"]
    assert kernel_config.DEFAULTS["SOKKAN_KERNEL_SANDBOX_NET"] == "none"


def test_the_api_entrypoint_renders_the_kernels_configuration():
    ep = (ROOT / "scripts/api-entrypoint.sh").read_text()
    assert "python -m harness.kernel_config" in ep and 'rm -f "$SOKKAN_KERNEL_CONFIG_OUT"' in ep
    assert ep.index("harness.kernel_config") < ep.index("exec uvicorn")


@pytest.mark.skipif(not shutil.which("docker"), reason="docker CLI absent")
def test_compose_accepts_the_files_together(tmp_path):
    env = {**os.environ, **ENV, "SOKKAN_KERNEL_IMAGE": "example/kernel:1", "COMPOSE_PROFILES": "kernel",
           "SOKKAN_KERNEL_SANDBOXD_IMAGE": "example/sandboxd:1",
           "SOKKAN_KERNEL_DB_OWNER_PASSWORD": "a", "SOKKAN_KERNEL_DB_APP_PASSWORD": "b"}
    r = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "-f", KFILE.name, "config", "--quiet"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    if "is not a docker command" in r.stderr:
        pytest.skip("docker compose plugin absent")
    assert r.returncode == 0, r.stderr
    # without the host paths: a clear refusal naming the script that writes them
    env.pop("SOKKAN_KERNEL_DATA_HOST")
    r = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "-f", KFILE.name, "config", "--quiet"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "scripts/kernel.sh enable" in r.stderr


# ---- the egress filter of the sandboxes -------------------------------------------------

def test_the_filter_decides_by_name_port_and_mode():
    f = _egress_filter()
    allow = f.allow_list(None)
    assert "pypi.org" in allow and "registry.npmjs.org" in allow
    assert f.decide("registries", allow, "pypi.org", 443) is None
    assert f.decide("registries", allow, "files.pythonhosted.org", 443) is None
    assert f.decide("registries", allow, "PyPI.org.", 443) is None
    assert f.decide("registries", allow, "evilpypi.org", 443)            # not a sub-domain
    assert f.decide("registries", allow, "pypi.org.evil.com", 443)
    assert f.decide("registries", allow, "pypi.org", 22)
    assert f.decide("registries", allow, "151.101.0.223", 443)           # an address, never
    assert f.decide("registries", allow, "example.com", 443)
    assert "none" in f.decide("none", allow, "pypi.org", 443)
    assert f.decide("open", allow, "example.com", 8443) is None          # private ranges: upstream
    assert f.allow_list("a.com, B.org ,") == ("a.com", "b.org")


def test_the_filter_reads_proxy_requests_only():
    f = _egress_filter()
    assert f.target("CONNECT pypi.org:443 HTTP/1.1") == ("CONNECT", "pypi.org", 443)
    assert f.target("GET http://deb.debian.org/x HTTP/1.1") == ("GET", "deb.debian.org", 80)
    for bad in ("GET /x HTTP/1.1", "CONNECT pypi.org HTTP/1.1", "GET https://a/ HTTP/1.1", "junk"):
        with pytest.raises(ValueError):
            f.target(bad)
    head, n = f.one_request(b"POST http://a/x HTTP/1.1\r\nHost: a\r\nConnection: keep-alive\r\n"
                            b"Content-Length: 3\r\n\r\n")
    assert n == 3 and b"keep-alive" not in head and head.endswith(b"Connection: close\r\n\r\n")
    with pytest.raises(ValueError):
        f.one_request(b"POST http://a/x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")


def test_the_filter_forwards_an_allowed_connect_to_the_kernels_egress_and_refuses_the_rest():
    f = _egress_filter()

    async def go():
        seen = []

        async def upstream(r, w):           # the kernel's egress: answers the CONNECT, then echoes
            head = await r.readuntil(b"\r\n\r\n")
            seen.append(head.split(b"\r\n")[0].decode())
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()
            data = await r.read(5)
            w.write(data.upper())
            await w.drain()
            w.close()
        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = up.sockets[0].getsockname()[1]
        flt = f.Filter("registries", f.allow_list(None), f"http://127.0.0.1:{port}")
        srv = await asyncio.start_server(flt.handle, "127.0.0.1", 0)
        fport = srv.sockets[0].getsockname()[1]

        async def ask(line: bytes, payload: bytes = b"") -> bytes:
            r, w = await asyncio.open_connection("127.0.0.1", fport)
            w.write(line + b"\r\n\r\n" + payload)
            await w.drain()
            out = await asyncio.wait_for(r.read(-1), 5)
            w.close()
            return out
        ok = await ask(b"CONNECT pypi.org:443 HTTP/1.1", b"hello")
        no = await ask(b"CONNECT example.com:443 HTTP/1.1")
        bad = await ask(b"GET /local HTTP/1.1")
        up.close(), srv.close()
        return seen, ok, no, bad
    seen, ok, no, bad = asyncio.new_event_loop().run_until_complete(go())
    assert seen == ["CONNECT pypi.org:443 HTTP/1.1"]
    assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"HELLO")
    assert no.startswith(b"HTTP/1.1 403") and b"package registry" in no
    assert bad.startswith(b"HTTP/1.1 400")


# ---- sandbox_unavailable: a clear error, never a dead session ----------------------------

def test_a_503_sandbox_unavailable_becomes_a_named_engine_error():
    body = '{"error": {"code": "sandbox_unavailable", "message": "plafond non appliqué"}}'
    e = kernel._refusal(503, body)
    assert isinstance(e, kernel.EngineUnavailable) and e.code == "sandbox_unavailable"
    assert "docs/KERNEL.md" in str(e) and "plafond non appliqué" in str(e)
    other = kernel._refusal(400, '{"error": {"code": "bad"}}')
    assert not isinstance(other, kernel.EngineUnavailable) and "(400)" in str(other)
    assert not isinstance(kernel._refusal(503, "<html>"), kernel.EngineUnavailable)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import agentchat
    import audit
    import board
    import usage
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    monkeypatch.setattr(agentchat, "_registry", {})
    return tmp_path


def test_the_session_says_why_the_engine_did_not_start_and_tries_again_next_time(world, monkeypatch):
    import agentchat
    import board
    import plain
    from harness import registry
    from harness.conformance import driver, reference

    class NoSandbox(reference.ReferenceHarness):
        def start(self, spec):
            self._spec = spec

            class S:
                def __aiter__(s):
                    return s

                async def __anext__(s):
                    raise kernel.EngineUnavailable("sandbox_unavailable", kernel.SANDBOX_UNAVAILABLE + "x")
            return S()
    model = reference.ReferenceModel()
    model.script.append([driver.say("ok")])
    made = []

    def create(name=None):
        made.append(NoSandbox(model) if not made else reference.ReferenceHarness(model))
        return made[-1]
    monkeypatch.setattr(registry, "create", create)
    board.add_sdk_session("sid-nb", "t")
    s = agentchat.AgentSession("sid-nb", cwd=str(world), user="alice@x")

    async def go():
        await s.handle_user("hello")
        first = list(s.events)
        await s.handle_user("hello again")      # the engine is tried again, from scratch
        await s.close()
        return first
    first = asyncio.new_event_loop().run_until_complete(go())
    errs = [e for e in first if e["type"] == "error"]
    assert errs and errs[0]["code"] == "sandbox_unavailable" and "docs/KERNEL.md" in errs[0]["message"]
    assert first[-1] == {"type": "status", "state": "idle"}
    assert len(made) == 2 and [e for e in s.events if e["type"] == "result"]
    # the simple profile: its own sentence, never « you can send it again »
    out = plain.View().feed(errs[0])
    assert out and out[0]["type"] == "notice" and out[0]["code"] == "sandbox_unavailable"


# ---- scripts/kernel.sh ------------------------------------------------------------------

@pytest.fixture()
def install(tmp_path):
    if not shutil.which("sh"):
        pytest.skip("sh required")
    d = tmp_path / "sokkan"
    (d / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/kernel.sh", d / "scripts/kernel.sh")
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / ".env").write_text("SOKKAN_LOCAL_TOKEN=keep\nCOMPOSE_PROFILES=appliance,rerank\n"
                            "COMPOSE_FILE=docker-compose.yml:docker/compose.images.yml\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "docker.log"
    (bin_ / "docker").write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        'case "$1 $2" in\n'
        '  "version --format") echo 29.4.1 ;;\n'
        '  "compose config") echo "name: sokkan" ;;\n'
        '  "volume inspect") case "$*" in *Mountpoint*) echo /var/lib/docker/volumes/sokkan_sokkan-data/_data ;; esac ;;\n'
        'esac\nexit 0\n')
    (bin_ / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    env.pop("SOKKAN_KERNEL_IMAGE", None)
    return d, env, log


def _dotenv(d: Path) -> dict:
    return dict(x.split("=", 1) for x in (d / ".env").read_text().splitlines() if "=" in x)


def test_kernel_sh_enables_and_disables_without_touching_the_rest(install):
    d, env, log = install
    r = subprocess.run(["sh", "scripts/kernel.sh", "enable"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "--image" in r.stderr                      # no image: refused
    r = subprocess.run(["sh", "scripts/kernel.sh", "enable", "--image", "x/kernel:latest"], cwd=d, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "never latest" in r.stderr
    r = subprocess.run(["sh", "scripts/kernel.sh", "enable", "--image", "x/kernel:1.0"], cwd=d, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "--sandboxd-image" in r.stderr              # the proxy's image too
    r = subprocess.run(["sh", "scripts/kernel.sh", "enable", "--image", "x/kernel:1.0", "--net", "registries",
                        "--sandboxd-image", "x/sandboxd:1.0"],
                       cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    e = _dotenv(d)
    assert e["COMPOSE_FILE"] == "docker-compose.yml:docker/compose.images.yml:docker-compose.kernel.yml"
    assert e["COMPOSE_PROFILES"] == "appliance,rerank,kernel" and e["SOKKAN_ENGINE_RUNTIME"] == "kernel"
    assert e["SOKKAN_KERNEL_DATA_HOST"] == "/var/lib/docker/volumes/sokkan_sokkan-data/_data"
    assert e["SOKKAN_KERNEL_WORKSPACE_HOST"] == str((d / "workspace").resolve())
    assert e["SOKKAN_KERNEL_SANDBOX_NETWORK"] == "sokkan-kernel-sandbox"
    assert e["SOKKAN_KERNEL_SANDBOX_NET"] == "registries" and e["SOKKAN_KERNEL_IMAGE"] == "x/kernel:1.0"
    pw = (e["SOKKAN_KERNEL_DB_OWNER_PASSWORD"], e["SOKKAN_KERNEL_DB_APP_PASSWORD"])
    assert all(len(p) == 36 for p in pw) and pw[0] != pw[1]
    assert e["SOKKAN_LOCAL_TOKEN"] == "keep" and stat.S_IMODE((d / ".env").stat().st_mode) == 0o600
    # enable again (an upgrade): idempotent, passwords kept
    r = subprocess.run(["sh", "scripts/kernel.sh", "refresh"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    e2 = _dotenv(d)
    assert e2["COMPOSE_FILE"].count("docker-compose.kernel.yml") == 1
    assert (e2["SOKKAN_KERNEL_DB_OWNER_PASSWORD"], e2["SOKKAN_KERNEL_DB_APP_PASSWORD"]) == pw
    # disable: back to the legacy engine, the rest as it was
    r = subprocess.run(["sh", "scripts/kernel.sh", "disable"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    e3 = _dotenv(d)
    assert e3["COMPOSE_FILE"] == "docker-compose.yml:docker/compose.images.yml"
    assert e3["COMPOSE_PROFILES"] == "appliance,rerank" and "SOKKAN_ENGINE_RUNTIME" not in e3
    assert e3["SOKKAN_KERNEL_DB_OWNER_PASSWORD"] == pw[0]                     # kept for a re-enable
    r = subprocess.run(["sh", "scripts/kernel.sh", "refresh"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "not enabled" in r.stdout


def test_kernel_sh_refuses_an_old_docker(install):
    d, env, log = install
    (Path(env["PATH"].split(":")[0]) / "docker").write_text(
        '#!/bin/sh\ncase "$1 $2" in "version --format") echo 24.0.7 ;; esac\nexit 0\n')
    r = subprocess.run(["sh", "scripts/kernel.sh", "enable", "--image", "x/kernel:1.0",
                        "--sandboxd-image", "x/sandboxd:1.0"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and ">= 26" in r.stderr
    assert "COMPOSE_PROFILES=appliance,rerank\n" in (d / ".env").read_text()


def test_the_installers_offer_the_kernel_without_making_it_the_default():
    inst = (ROOT / "scripts/install.sh").read_text()
    up = (ROOT / "scripts/upgrade.sh").read_text()
    assert "--kernel) KERNEL_IMAGE=" in inst and 'KERNEL_IMAGE="${SOKKAN_KERNEL_IMAGE:-}"' in inst
    assert "SOKKAN_KERNEL_ENABLE_IMAGE" in up and "kernel.sh refresh" in up
    tmpl = (ROOT / "deploy/appliance/env.appliance.example").read_text()
    assert "kernel" not in re.search(r"^COMPOSE_PROFILES=(.*)$", tmpl, re.M).group(1)
    assert "SOKKAN_ENGINE_RUNTIME=kernel" not in tmpl and "docker-compose.kernel.yml" not in tmpl
    assert "docker-compose.kernel.yml" in (ROOT / "scripts/rollback.sh").read_text()



def test_the_proxy_and_the_kernel_serve_the_instance_of_the_rendered_configuration():
    """The kernel's socket proxy serves ONE instance (NOKKER_INSTANCE, kernel n°26): the label
    the api rendered from the instance key (`local.instance`), read from the same file by the
    proxy and the kernel — one value, one source."""
    svcs = _compose()["services"]
    for name in ("kernel-dockerproxy", "kernel"):
        cmd = " ".join(svcs[name]["entrypoint"])
        assert "NOKKER_INSTANCE=" in cmd and "s/^  instance: //p' /etc/kernel/config.yaml" in cmd, name
        assert "kernel-config:/etc/kernel:ro" in svcs[name]["volumes"], name
    assert svcs["kernel-dockerproxy"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert "dockerproxy.py" in " ".join(svcs["kernel-dockerproxy"]["entrypoint"])
    assert not (ROOT / "deploy/kernel/dockerproxy_compat.py").exists()
    # the rendered file holds the label on the line the entrypoints read
    text = yaml.safe_dump(kernel_config.render({}, ENV, "sokkan-0123456789abcdef"), sort_keys=False)
    assert "\n  instance: sokkan-0123456789abcdef\n" in text
