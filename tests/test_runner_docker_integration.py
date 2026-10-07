"""docker runner, for real: the real Claude Code CLI in a real session container.

Needs a reachable Docker daemon and the session image (SOKKAN_TEST_SESSION_IMAGE, default
`sokkan-session:local`, built with `docker build -f docker/session.Dockerfile -t
sokkan-session:local .`). Otherwise SKIPPED with the reason. No credit is spent: the model is
tests/mock_anthropic.py on the test network's gateway. What it proves:

* a session turn through ClaudeSDKClient → RunnerTransport → container supervisor → CLI;
* the SOKKAN MCP servers reach the container through the relay (status `connected`);
* the transcript lands in the api's ~/.claude (History keeps working);
* reattach: the api drops its connection (restart), a new client adopts the SAME container;
* cleanup: closing the session removes the container;
* the resources one session container uses (printed; written to SOKKAN_RUNNER_MEASURE_OUT).
"""
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("SOKKAN_TEST_SESSION_IMAGE", "sokkan-session:local")


def _docker():
    from runner.docker_runner import DockerAPI, DockerError
    api = DockerAPI()
    try:
        api.request("GET", "/_ping")
    except (OSError, DockerError) as e:
        pytest.skip(f"no Docker daemon reachable ({e!r})")
    try:
        api.request("GET", f"/images/{IMAGE}/json")
    except DockerError:
        pytest.skip(f"session image {IMAGE} not built (docker build -f docker/session.Dockerfile"
                    f" -t {IMAGE} .)")
    return api


def _free_port(host: str) -> int:
    s = socket.socket()
    s.bind((host, 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def net():
    api = _docker()
    name = f"sokkan-rt-{os.getpid()}"
    # an explicit subnet: hosts with many compose projects exhaust Docker's default pools
    subnet = os.environ.get("SOKKAN_TEST_SUBNET") or f"10.250.{os.getpid() % 200 + 20}.0/24"
    api.request("POST", "/networks/create", {"Name": name, "Driver": "bridge",
                                             "Labels": {"sokkan.ch/test": "1"},
                                             "IPAM": {"Config": [{"Subnet": subnet}]}})
    info = api.request("GET", f"/networks/{name}")
    gw = info["IPAM"]["Config"][0]["Gateway"]
    try:
        yield api, name, gw
    finally:
        for c in api.list(["sokkan.ch/session"]):
            if name in json.dumps(c.get("NetworkSettings", {})):
                api.remove(c["Id"])
        api.request("DELETE", f"/networks/{name}")


def test_real_cli_in_a_docker_session_container(net, tmp_path, monkeypatch):
    import runner
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, SystemMessage
    from mock_anthropic import MockAnthropic
    from runner import relay
    from runner.docker_runner import DockerRunner
    from runner.mounts import parse_mounts

    api, network, gw = net
    for d in ("work", "claude"):
        (tmp_path / d).mkdir()
        os.chmod(tmp_path / d, 0o777)
    os.chmod(tmp_path, 0o755)
    # the api runs as uid 1000 like the sessions; this test runs as whoever runs pytest:
    # pre-create the transcript dir writable for the container's uid
    from runner.base import claude_project_slug
    tdir = tmp_path / "claude" / "projects" / claude_project_slug(str(tmp_path / "work"))
    tdir.mkdir(parents=True)
    os.chmod(tmp_path / "claude" / "projects", 0o777)
    os.chmod(tdir, 0o777)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("SOKKAN_RUNNER_SECRET", "it-key")
    relay_port = _free_port(gw)
    monkeypatch.setenv("SOKKAN_RUNNER_RELAY_BIND", f"{gw}:{relay_port}")
    r = DockerRunner(api, {"image": IMAGE, "network": network,
                           "relay_addr": f"{gw}:{relay_port}", "proxy": "",
                           "user": "1000:1000",
                           "mounts": parse_mounts(f"{tmp_path}=bind:{tmp_path}")})
    runner.set_runner(r)
    sid = f"it{os.getpid()}"
    measures: dict = {}

    with MockAnthropic(host=gw) as mock:
        env = {**os.environ, "ANTHROPIC_BASE_URL": mock.url, "ANTHROPIC_API_KEY": "sk-test",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"}
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        servers = {"echo": {"command": sys.executable,
                            "args": [str(ROOT / "tests" / "fixtures" / "echo_mcp_server.py")],
                            "env": {"SOKKAN_SESSION_USER": "alice@example.com"}}}

        def kwargs():
            return {"cwd": str(tmp_path / "work"), "env": env, "mcp_servers": servers,
                    "setting_sources": [], "allowed_tools": ["mcp__echo__whoami"]}

        async def go():
            opts, t = runner.client_args(sid, kwargs(), user="alice@example.com")
            c = ClaudeSDKClient(options=opts, transport=t)
            t0 = time.monotonic()
            await c.connect()
            measures["start_s"] = round(time.monotonic() - t0, 2)
            texts, mcp_status = [], {}
            await c.query("hello from the runner test")
            async for m in c.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    mcp_status = {s["name"]: s["status"] for s in m.data.get("mcp_servers", [])}
                if isinstance(m, AssistantMessage):
                    texts += [b.text for b in m.content if hasattr(b, "text")]
            measures["after_turn"] = await r.usage(t.handle)
            name = t.handle.name
            assert not t.handle.adopted
            # --- the api "restarts": its connection drops, the container keeps running
            await t.chan.close()
            await asyncio.sleep(1)
            assert await r.status(t.handle) == "running"
            opts2, t2 = runner.client_args(sid, kwargs(), user="alice@example.com")
            texts2 = []
            async with ClaudeSDKClient(options=opts2, transport=t2) as c2:
                assert t2.handle.adopted and t2.handle.name == name
                assert t2.chan.hello.get("resumed") is True
                await c2.query("second turn after reattach")
                async for m in c2.receive_response():
                    if isinstance(m, AssistantMessage):
                        texts2 += [b.text for b in m.content if hasattr(b, "text")]
                measures["after_reattach"] = await r.usage(t2.handle)
            # closing the session removed the container
            assert await r.status(t2.handle) == "gone"
            await relay.stop()
            return texts, texts2, mcp_status

        texts, texts2, mcp_status = asyncio.run(asyncio.wait_for(go(), 240))
    runner.set_runner(None)
    assert texts and texts2, (texts, texts2)
    assert mcp_status.get("echo") == "connected", mcp_status
    transcripts = list(tdir.glob("*.jsonl"))
    assert transcripts, "transcript not written to the api's CLAUDE_CONFIG_DIR"
    assert len(mock.main_requests()) >= 2
    print("\n[runner-measure]", json.dumps(measures))
    out = os.environ.get("SOKKAN_RUNNER_MEASURE_OUT")
    if out:
        Path(out).write_text(json.dumps(measures, indent=2))
