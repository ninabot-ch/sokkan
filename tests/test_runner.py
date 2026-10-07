"""Session runners (feature kubernetes_runner): selection, environment filtering, mounts,
the session supervisor (stream-json relay + reattach), the SDK transport, the MCP relay, the
egress proxy, and the docker / kubernetes runners against fake API clients."""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "runner" / "pod"))
FAKE_CLI = [sys.executable, str(ROOT / "tests" / "fixtures" / "fake_claude_cli.py")]

import runner  # noqa: E402
from runner import base, common, mounts  # noqa: E402
from runner.base import Handle, SessionRunner, SessionSpec  # noqa: E402

import egress_proxy  # noqa: E402
import supervisor as sup_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _runner_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SOKKAN_RUNNER_SECRET", "test-key")
    monkeypatch.delenv("SOKKAN_RUNNER", raising=False)
    monkeypatch.delenv("SOKKAN_FEATURE_KUBERNETES_RUNNER", raising=False)
    runner.set_runner(None)
    yield
    runner.set_runner(None)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


# ---- selection ----------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (None, "local"), ("local", "local"), ("docker", "docker"),
    ("kubernetes", "kubernetes"), ("bogus", "local")])
def test_runner_selection(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("SOKKAN_RUNNER", value)
    assert runner.selected() == expected


def test_feature_switch_off_forces_local(monkeypatch):
    monkeypatch.setenv("SOKKAN_RUNNER", "kubernetes")
    monkeypatch.setenv("SOKKAN_FEATURE_KUBERNETES_RUNNER", "0")
    assert runner.selected() == "local"
    import features
    assert not features.enabled("kubernetes_runner")


def test_feature_follows_sokkan_runner(monkeypatch):
    import features
    monkeypatch.setenv("SOKKAN_RUNNER", "docker")
    assert features.enabled("kubernetes_runner")
    monkeypatch.setenv("SOKKAN_RUNNER", "local")
    assert not features.enabled("kubernetes_runner")


def test_local_runner_leaves_the_sdk_path_unchanged():
    opts, transport = runner.client_args("abc", {"cwd": "/tmp", "allowed_tools": ["Read"]})
    assert transport is None
    assert opts.allowed_tools == ["Read"] and str(opts.cwd) == "/tmp"


# ---- environment and tokens --------------------------------------------------------------
def test_session_env_only_carries_what_the_session_adds_and_model_access():
    api = {"ANTHROPIC_API_KEY": "sk-a", "CORTHEXIS_DATABASE_URL": "postgres://secret",
           "SOKKAN_OIDC_CLIENT_SECRET": "oidc", "PATH": "/usr/bin", "HOME": "/data",
           "CLAUDE_CODE_OAUTH_TOKEN": ""}
    sess = {**api, "STRIPE_KEY": "vault-value", "ANTHROPIC_BASE_URL": "https://infer",
            "PATH": "/other", "SOKKAN_SESSION_TOKEN": "x"}
    env = base.session_env(sess, api)
    assert env["STRIPE_KEY"] == "vault-value"
    assert env["ANTHROPIC_API_KEY"] == "sk-a" and env["ANTHROPIC_BASE_URL"] == "https://infer"
    for leaked in ("CORTHEXIS_DATABASE_URL", "SOKKAN_OIDC_CLIENT_SECRET", "PATH", "HOME",
                   "SOKKAN_SESSION_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert leaked not in env, leaked


def test_tokens_are_stable_per_session_and_purpose():
    a = common.token_for("s1", "supervisor")
    assert a == common.token_for("s1", "supervisor")
    assert a != common.token_for("s2", "supervisor") != common.token_for("s1", "relay")


def test_safe_name_is_dns_1123():
    n = base.safe_name("ABC_def/0123456789" * 5)
    assert len(n) <= 63 and n == n.lower() and all(c.isalnum() or c == "-" for c in n)


# ---- mounts ----------------------------------------------------------------------------
def test_mount_plan_only_mounts_the_session_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    table = mounts.parse_mounts(f"{tmp_path}=pvc:sokkan-data")
    cwd = str(tmp_path / "projects" / "alpha" / "work")
    plan = mounts.plan(cwd, table)
    assert [(m.kind, m.source, m.subpath) for m in plan] == [
        ("pvc", "sokkan-data", "projects/alpha/work"),
        ("pvc", "sokkan-data", f"claude/projects/{base.claude_project_slug(cwd)}")]
    assert plan[0].target == cwd
    assert plan[1].target.startswith(mounts.SESSION_HOME + "/.claude/projects/")
    assert os.path.isdir(cwd)  # created by the api, not by the kubelet as root


def test_mount_longest_prefix_and_bind(tmp_path):
    table = mounts.parse_mounts("/data=volume:v,/workspace=bind:/srv/ws,/data/big=pvc:big")
    assert mounts.resolve("/workspace/x", table) == ("bind", "/srv/ws/x", "")
    assert mounts.resolve("/data/big/y", table) == ("pvc", "big", "y")
    assert mounts.resolve("/data", table) == ("volume", "v", "")
    with pytest.raises(ValueError):
        mounts.resolve("/etc", table)
    with pytest.raises(ValueError):
        mounts.parse_mounts("/data=nfs:x")


# ---- supervisor ------------------------------------------------------------------------
async def _start_sup(**kw):
    sup = sup_mod.Supervisor(FAKE_CLI, None, "tok", **kw)
    server = await asyncio.start_server(sup.handle, "127.0.0.1", 0, limit=sup_mod.LIMIT)
    return sup, server, server.sockets[0].getsockname()[1]


async def _read_until(chan, pred, n=20):
    seen = []
    async for m in chan.events():
        seen.append(m)
        if pred(m) or len(seen) >= n:
            break
    return seen


def _init(rid):
    return {"type": "control_request", "request_id": rid, "request": {"subtype": "initialize"}}


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def test_supervisor_refuses_a_bad_token():
    async def go():
        sup, server, port = await _start_sup()
        with pytest.raises(ConnectionError):
            await base.connect_supervisor("127.0.0.1", port, "wrong", timeout=1.5)
        assert sup.proc is None  # nothing spawned for an unauthenticated peer
        server.close()
    run(go())


def test_supervisor_relays_and_survives_a_disconnect():
    async def go():
        sup, server, port = await _start_sup()
        c1 = await base.connect_supervisor("127.0.0.1", port, "tok")
        assert c1.hello["resumed"] is False
        await c1.send(_init("req_1"))
        first = await _read_until(c1, lambda m: m.get("type") == "control_response")
        assert first[-1]["response"]["response"]["inits"] == 1
        await c1.send(_user("SLOW hello"))
        await c1.close()                       # the api goes away mid-turn
        await asyncio.sleep(1.5)               # the CLI answers while nobody listens
        assert sup.proc.returncode is None and sup.buffer
        c2 = await base.connect_supervisor("127.0.0.1", port, "tok")
        assert c2.hello["resumed"] is True
        await c2.send(_init("req_9"))          # a new SDK client always initializes
        msgs = await _read_until(c2, lambda m: m.get("type") == "control_response")
        texts = [m["message"]["content"][0]["text"] for m in msgs if m["type"] == "assistant"]
        assert texts == ["echo: SLOW hello"]   # replayed from the buffer
        resp = msgs[-1]["response"]
        assert resp["request_id"] == "req_9" and resp["response"]["inits"] == 1  # cached
        await c2.send(_user("again"))
        more = await _read_until(c2, lambda m: m.get("type") == "result")
        assert any(m.get("type") == "assistant" for m in more)
        await sup.terminate()
        server.close()
    run(go())


def test_supervisor_reports_the_cli_exit_code_and_stops():
    async def go():
        sup, server, port = await _start_sup()
        c = await base.connect_supervisor("127.0.0.1", port, "tok")
        await c.send(_user("EXIT"))
        async for _ in c.events():
            pass
        assert c.exited == 3
        await asyncio.wait_for(sup.done.wait(), 5)
        server.close()
    run(go())


def test_supervisor_idle_timeout_without_client():
    async def go():
        sup, server, port = await _start_sup(idle_s=0.3)
        c = await base.connect_supervisor("127.0.0.1", port, "tok")
        await c.send(_init("r"))
        await _read_until(c, lambda m: m.get("type") == "control_response")
        await c.close()
        await asyncio.wait_for(sup.watchdog(tick=0.1), 10)
        assert sup.done.is_set() and sup.proc.returncode is not None
        server.close()
    run(go())


# ---- the SDK over a runner (fake runner = an in-process supervisor) ----------------------
class InProcessRunner(SessionRunner):
    """A runner whose 'container' is a supervisor in this process (no docker, no cluster)."""
    name = "fake"

    def __init__(self):
        self.started: list[SessionSpec] = []
        self.stopped: list[str] = []
        self.live: dict[str, tuple] = {}

    async def start(self, spec):
        self.started.append(spec)
        if spec.sid in self.live:
            sup, server, port = self.live[spec.sid]
            return Handle(spec.sid, "fake", "127.0.0.1", port, spec.token, adopted=True)
        sup = sup_mod.Supervisor(FAKE_CLI, None, spec.token)
        server = await asyncio.start_server(sup.handle, "127.0.0.1", 0, limit=sup_mod.LIMIT)
        port = server.sockets[0].getsockname()[1]
        self.live[spec.sid] = (sup, server, port)
        return Handle(spec.sid, "fake", "127.0.0.1", port, spec.token)

    async def stop(self, handle):
        self.stopped.append(handle.sid)
        sup, server, _ = self.live.pop(handle.sid)
        await sup.terminate()
        server.close()

    async def status(self, handle):
        return "running" if handle.sid in self.live else "gone"


def test_sdk_session_over_a_runner_with_approval(monkeypatch):
    from claude_agent_sdk import ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny
    from claude_agent_sdk import AssistantMessage

    monkeypatch.setenv("SOKKAN_RUNNER_RELAY_BIND", "127.0.0.1:0")
    fake = InProcessRunner()
    runner.set_runner(fake)
    asked = []

    async def can_use_tool(name, inp, ctx):
        asked.append(name)
        return (PermissionResultAllow(updated_input=inp) if len(asked) == 1
                else PermissionResultDeny(message="no"))

    servers = {"sokkan-memory": {"command": sys.executable, "args": ["/x/memory.py"],
                                 "env": {"SOKKAN_SESSION_USER": "a@b"}}}

    async def go():
        opts, transport = runner.client_args(
            "sid-1", {"cwd": "/tmp", "can_use_tool": can_use_tool, "mcp_servers": servers,
                      "env": {**os.environ, "STRIPE_KEY": "v"}}, user="a@b", project="alpha")
        spec = transport.spec
        assert spec.argv[0] == "claude" and "--permission-prompt-tool" in spec.argv
        mcp = json.loads(spec.argv[spec.argv.index("--mcp-config") + 1])["mcpServers"]
        assert mcp == {"sokkan-memory": {"command": "sokkan-mcp-relay",
                                         "args": ["sokkan-memory"]}}
        assert spec.env["STRIPE_KEY"] == "v" and spec.project == "alpha"
        from runner import relay
        assert relay.lookup(spec.relay_token)["servers"] == servers
        texts = []
        async with ClaudeSDKClient(options=opts, transport=transport) as c:
            for q in ("hi", "ASK one", "ASK two"):
                await c.query(q)
                async for m in c.receive_response():
                    if isinstance(m, AssistantMessage):
                        texts.append(m.content[0].text)
        await relay.stop()
        return texts

    texts = run(go())
    assert texts == ["echo: hi", "echo: ASK one [allow]", "echo: ASK two [deny]"]
    assert asked == ["Bash", "Bash"]
    assert fake.stopped == ["sid-1"]  # closing the session removes its container


def test_channel_approve_verb():
    async def go():
        sup, server, port = await _start_sup()
        c = await base.connect_supervisor("127.0.0.1", port, "tok")
        await c.send(_user("ASK x"))
        req = (await _read_until(c, lambda m: m.get("type") == "control_request"))[-1]
        await SessionRunner.approve(c, req["request_id"], allow=False, message="nope")
        msgs = await _read_until(c, lambda m: m.get("type") == "result")
        assert msgs[0]["message"]["content"][0]["text"] == "echo: ASK x [deny]"
        await sup.terminate()
        server.close()
    run(go())


# ---- MCP relay ----------------------------------------------------------------------------
def test_mcp_relay_pipes_to_the_registered_server_only(monkeypatch):
    from runner import relay
    monkeypatch.setenv("SOKKAN_RUNNER_RELAY_BIND", "127.0.0.1:0")
    echo = [sys.executable, "-c",
            "import os,sys\nfor l in sys.stdin: print(os.environ['WHO'] + ':' + l.strip(), "
            "flush=True)"]

    async def go():
        await relay.stop()
        host, port = await relay.ensure_started()
        relay.register("good", "sid", {"mem": {"command": echo[0], "args": echo[1:],
                                               "env": {"WHO": "alice"}}})
        env = {**os.environ, "SOKKAN_RELAY_ADDR": f"127.0.0.1:{port}",
               "SOKKAN_RELAY_TOKEN": "good"}
        script = str(ROOT / "backend" / "runner" / "pod" / "mcp_relay.py")
        p = await asyncio.create_subprocess_exec(
            sys.executable, script, "mem", env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE)
        out, _ = await p.communicate(b"ping\n")
        env["SOKKAN_RELAY_TOKEN"] = "forged"
        p2 = await asyncio.create_subprocess_exec(
            sys.executable, script, "mem", env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE)
        out2, _ = await p2.communicate(b"ping\n")
        relay.unregister("good")
        await relay.stop()
        return out, out2

    out, out2 = run(go())
    assert out.decode().strip() == "alice:ping"   # identity set by the api, not the container
    assert out2 == b""                            # unknown token: nothing started


# ---- egress proxy -------------------------------------------------------------------------
def test_egress_allowlist():
    rules = egress_proxy.parse_allow("api.anthropic.com, *.sokkan.ch ,git.example.org:22")
    assert egress_proxy.allowed(rules, "api.anthropic.com", 443)
    assert egress_proxy.allowed(rules, "infer.sokkan.ch", 443)
    assert not egress_proxy.allowed(rules, "sokkan.ch.evil.com", 443)
    assert not egress_proxy.allowed(rules, "api.anthropic.com", 80)
    assert egress_proxy.allowed(rules, "git.example.org", 22)
    assert not egress_proxy.allowed(rules, "example.com", 443)


def test_egress_proxy_denies_and_relays():
    async def go():
        target = await asyncio.start_server(
            lambda r, w: (w.write(b"hello"), w.close()), "127.0.0.1", 0)
        tport = target.sockets[0].getsockname()[1]
        rules = egress_proxy.parse_allow(f"127.0.0.1:{tport}")
        proxy = await asyncio.start_server(egress_proxy.make_handler(rules), "127.0.0.1", 0)
        pport = proxy.sockets[0].getsockname()[1]

        async def connect(dest):
            r, w = await asyncio.open_connection("127.0.0.1", pport)
            w.write(f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n\r\n".encode())
            await w.drain()
            data = await asyncio.wait_for(r.read(200), 5)
            w.close()
            return data
        ok = await connect(f"127.0.0.1:{tport}")
        denied = await connect("example.com:443")
        proxy.close()
        target.close()
        return ok, denied
    ok, denied = run(go())
    assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"hello")
    assert denied.startswith(b"HTTP/1.1 403")


# ---- docker runner (fake Docker API) ---------------------------------------------------------
class FakeDocker:
    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def create(self, name, body):
        self.calls.append(("create", name))
        self.containers[name] = {"Id": "id-" + name, "Name": "/" + name, "body": body,
                                 "State": {"Running": False, "Status": "created"},
                                 "NetworkSettings": {"Networks": {"sokkan-sessions": {
                                     "IPAddress": "172.30.0.5"}}}}
        return "id-" + name

    def _find(self, cid):
        return next((c for n, c in self.containers.items()
                     if cid in (n, c["Id"])), None)

    def start(self, cid):
        self.calls.append(("start", cid))
        self._find(cid)["State"] = {"Running": True, "Status": "running"}

    def inspect(self, cid):
        return self._find(cid)

    def remove(self, cid):
        self.calls.append(("remove", cid))
        c = self._find(cid)
        if c:
            self.containers.pop(c["Name"].lstrip("/"))

    def list(self, labels):
        return [{"Id": c["Id"], "Names": [c["Name"]],
                 "State": "running" if c["State"].get("Running") else "exited",
                 "Labels": c["body"]["Labels"]} for c in self.containers.values()]

    def stats(self, cid):
        return {"cpu_stats": {"cpu_usage": {"total_usage": 3_000}, "system_cpu_usage": 20_000,
                              "online_cpus": 2},
                "precpu_stats": {"cpu_usage": {"total_usage": 1_000},
                                 "system_cpu_usage": 10_000},
                "memory_stats": {"usage": 400 * 2**20, "stats": {"inactive_file": 100 * 2**20}}}


def _docker_cfg(tmp_path):
    return {"image": "sokkan-session:t", "network": "sokkan-sessions", "relay_addr": "api:8098",
            "proxy": "http://session-egress:3128", "user": "1000:1000",
            "mounts": mounts.parse_mounts(f"{tmp_path}=volume:sokkan_sokkan-data")}


def _spec(tmp_path, sid="s1", project="alpha"):
    return SessionSpec(sid=sid, argv=["claude", "--output-format", "stream-json"],
                       cwd=str(tmp_path / "projects" / project / "work"), project=project,
                       env={"ANTHROPIC_API_KEY": "sk"}, token="T", relay_token="R")


def test_docker_container_is_locked_down(tmp_path, monkeypatch):
    from runner.docker_runner import container_body
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    b = container_body(_spec(tmp_path), _docker_cfg(tmp_path))
    hc = b["HostConfig"]
    assert b["User"] == "1000:1000" and hc["Privileged"] is False
    assert hc["CapDrop"] == ["ALL"] and "no-new-privileges:true" in hc["SecurityOpt"]
    assert hc["ReadonlyRootfs"] is True and hc["NetworkMode"] == "sokkan-sessions"
    assert hc["Memory"] == 2 * 2**30 == hc["MemorySwap"] and hc["NanoCpus"] == 10**9
    assert hc["PidsLimit"] > 0
    subs = [m["VolumeOptions"]["Subpath"] for m in hc["Mounts"]]
    assert subs[0] == "projects/alpha/work" and subs[1].startswith("claude/projects/")
    env = dict(e.split("=", 1) for e in b["Env"])
    assert env["HTTPS_PROXY"] == "http://session-egress:3128" and env["NO_PROXY"].startswith("api")
    assert json.loads(env["SOKKAN_SESSION_ARGV"])[0] == "claude"


def test_docker_runner_start_adopt_stop_reconcile(tmp_path, monkeypatch):
    from runner.docker_runner import DockerRunner
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    api = FakeDocker()
    r = DockerRunner(api, _docker_cfg(tmp_path))

    async def go():
        h = await r.start(_spec(tmp_path))
        assert not h.adopted and h.host == "172.30.0.5" and h.port == 7070
        assert h.token == common.token_for("s1", "supervisor")
        assert await r.status(h) == "running"
        h2 = await r.start(_spec(tmp_path))          # api restarted: adopt, no 2nd container
        assert h2.adopted and [c for c in api.calls if c[0] == "create"] == [("create", h.name)]
        u = await r.usage(h)
        assert u == {"cpu_millicores": 400, "memory_bytes": 300 * 2**20}
        api.containers[h.name]["State"] = {"Running": False, "Status": "exited", "ExitCode": 0}
        assert await r.status(h) == "succeeded"
        res = await r.reconcile()
        assert res == {"kept": [], "removed": [h.name]} and not api.containers
        await r.start(_spec(tmp_path, sid="s2"))
        live = await r.list_live()
        assert [x.sid for x in live] == ["s2"]
        await r.stop(live[0])
        assert not api.containers
    run(go())


# ---- kubernetes runner (fake API) ----------------------------------------------------------
class FakeKube:
    def __init__(self, phase_after_create="Running"):
        self.objs: dict[tuple, dict] = {}
        self.calls: list[tuple] = []
        self.phase = phase_after_create

    def create(self, kind, body):
        name = body["metadata"]["name"]
        self.calls.append(("create", kind, name))
        body = json.loads(json.dumps(body))
        body["metadata"]["uid"] = "uid-" + name
        if kind == "pods":
            body["status"] = {"phase": self.phase, "podIP": "10.42.0.9"}
        self.objs[(kind, name)] = body
        return body

    def get(self, kind, name):
        return self.objs.get((kind, name))

    def delete(self, kind, name):
        self.calls.append(("delete", kind, name))
        self.objs.pop((kind, name), None)

    def patch(self, kind, name, body):
        self.calls.append(("patch", kind, name))
        self.objs[(kind, name)]["metadata"].update(body["metadata"])
        return self.objs[(kind, name)]

    def list(self, kind, selector):
        return [o for (k, _), o in self.objs.items() if k == kind]

    def metrics(self, name):
        return {"containers": [{"usage": {"cpu": "123456789n", "memory": "300Mi"}}]}


def _kcfg(tmp_path, **kw):
    cfg = {"image": "sokkan-session:t", "relay_addr": "sokkan-api-relay:8098",
           "proxy": "http://sokkan-egress:3128", "run_as_user": None, "fs_group": None,
           "deadline_s": 3600, "service_account": "sokkan-session",
           "mounts": mounts.parse_mounts(f"{tmp_path}=pvc:sokkan-data")}
    cfg.update(kw)
    return cfg


def test_pod_manifest_is_restricted(tmp_path, monkeypatch):
    from runner.kubernetes_runner import pod_manifest
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    p = pod_manifest(_spec(tmp_path), _kcfg(tmp_path), "sokkan-s-s1", "sokkan-s-s1-env")
    spec = p["spec"]
    c = spec["containers"][0]
    sc = c["securityContext"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert "runAsUser" not in spec["securityContext"]      # OpenShift assigns the uid
    assert "fsGroup" not in spec["securityContext"]
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert sc["allowPrivilegeEscalation"] is False and sc["privileged"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]} and sc["readOnlyRootFilesystem"] is True
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never" and spec["activeDeadlineSeconds"] == 3600
    assert c["resources"]["limits"] == {"cpu": "1", "memory": "2Gi"}
    assert c["resources"]["requests"]["cpu"] and c["resources"]["requests"]["memory"]
    assert c["envFrom"] == [{"secretRef": {"name": "sokkan-s-s1-env"}}]
    assert not any("value" in e for e in c.get("env", []))  # no secret in the pod spec
    subs = [m.get("subPath") for m in c["volumeMounts"] if m["name"].startswith("data-")]
    assert subs[0] == "projects/alpha/work" and subs[1].startswith("claude/projects/")
    assert "hostNetwork" not in spec and "hostPID" not in spec
    assert p["metadata"]["labels"]["app.kubernetes.io/component"] == "session"
    p2 = pod_manifest(_spec(tmp_path), _kcfg(tmp_path, run_as_user=1000, fs_group=1000,
                                             affinity_api=True,
                                             api_selector={"app": "api"}),
                      "n", "s")
    assert p2["spec"]["securityContext"]["runAsUser"] == 1000
    assert p2["spec"]["affinity"]["podAffinity"]


def test_kubernetes_runner_lifecycle(tmp_path, monkeypatch):
    from runner.kubernetes_runner import KubernetesRunner
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    api = FakeKube()
    r = KubernetesRunner(api, _kcfg(tmp_path), poll_s=0.01)

    async def go():
        h = await r.start(_spec(tmp_path))
        assert [c[:2] for c in api.calls[:3]] == [("delete", "secrets"), ("create", "secrets"),
                                                  ("create", "pods")]
        sec = api.get("secrets", h.name + "-env")
        assert sec["metadata"]["ownerReferences"][0]["uid"] == "uid-" + h.name
        assert sec["stringData"]["SOKKAN_SESSION_TOKEN"] == "T"
        assert sec["stringData"]["ANTHROPIC_API_KEY"] == "sk"
        assert h.host == "10.42.0.9" and not h.adopted
        h2 = await r.start(_spec(tmp_path))         # api restart: reattach to the live pod
        assert h2.adopted and len([c for c in api.calls if c[:2] == ("create", "pods")]) == 1
        assert await r.usage(h) == {"cpu_millicores": 123, "memory_bytes": 300 * 2**20}
        api.objs[("pods", h.name)]["status"]["phase"] = "Succeeded"
        assert await r.status(h) == "succeeded"
        h3 = await r.start(_spec(tmp_path))         # finished: replaced by a new pod
        assert not h3.adopted
        await r.stop(h3)
        assert not api.objs
        await r.start(_spec(tmp_path, sid="s9"))
        api.objs[("pods", "sokkan-s-s9")]["status"]["phase"] = "Failed"
        assert await r.reconcile() == {"kept": [], "removed": ["sokkan-s-s9"]}
        assert not api.objs
    run(go())


def test_kubernetes_runner_reports_a_pod_that_cannot_start(tmp_path, monkeypatch):
    from runner.kubernetes_runner import KubernetesRunner
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    api = FakeKube(phase_after_create="Pending")
    r = KubernetesRunner(api, _kcfg(tmp_path), poll_s=0.01, start_timeout_s=0.1)
    with pytest.raises(TimeoutError):
        run(r.start(_spec(tmp_path)))


def test_session_image_runs_the_pod_scripts_with_python3_only():
    """The pod-side scripts are stdlib-only: the session image has no pip packages."""
    import ast
    stdlib = set(sys.stdlib_module_names)
    for f in (ROOT / "backend" / "runner" / "pod").glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for n in names:
                assert n.split(".")[0] in stdlib | {"__future__"}, f"{f.name}: {n}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_session_requests_never_exceed_limits(monkeypatch):
    """Found on a real cluster: a limit below the default request was refused (422)."""
    monkeypatch.setenv("SOKKAN_SESSION_MEMORY_LIMIT", "256Mi")
    monkeypatch.setenv("SOKKAN_SESSION_CPU_LIMIT", "100m")
    r = base.Resources.from_env()
    assert (r.memory_request, r.memory_limit) == ("256Mi", "256Mi")
    assert (r.cpu_request, r.cpu_limit) == ("100m", "100m")
    monkeypatch.setenv("SOKKAN_SESSION_MEMORY_LIMIT", "4Gi")
    assert base.Resources.from_env().memory_request == "512Mi"
