"""3.2.0 — the credential helper where the api's loopback is out of reach.

* session container / pod (docker or kubernetes runner): the helper asks through the
  runner's authenticated relay — simulated in-process (always), and from a REAL docker
  container on its own network when a daemon and an image with git + python3 are there
  (SOKKAN_TEST_GIT_IMAGE, default `sokkan-api:latest`; otherwise SKIPPED with the reason);
* Bash inside bubblewrap without network: the helper and git go through the per-session
  Unix socket bound into the sandbox (bwrap or SOKKAN_TEST_BWRAP; otherwise SKIPPED);
* a ticket replayed on another session's channel, or after its session ended, gets nothing;
  the token is never in the environment, the command output (= a transcript), on disk.

No HTTP credential route runs in this module: a push that works proves the new channel.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fake_gitlab import FakeGitLab

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "backend" / "forge" / "git_credential_helper.py"
ALICE = "alice@x"
SID = "b" * 32
OTHER = "c" * 32
IMAGE = os.environ.get("SOKKAN_TEST_GIT_IMAGE", "sokkan-api:latest")
BW = os.environ.get("SOKKAN_TEST_BWRAP") or shutil.which("bwrap") or ""
IDENT = {"GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": ALICE,
         "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": ALICE}


def _free_port(host: str = "127.0.0.1") -> int:
    s = socket.socket()
    s.bind((host, 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def loop():
    lp = asyncio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()

    def run(coro):
        return asyncio.run_coroutine_threadsafe(coro, lp).result(60)
    yield run
    from forge import gitcred
    from runner import relay
    run(relay.stop())
    for sid in list(gitcred._socks):
        run(gitcred.close_socket(sid))
    if gitcred._sock_dir:
        shutil.rmtree(gitcred._sock_dir, ignore_errors=True)
        gitcred._sock_dir = None
    lp.call_soon_threadsafe(lp.stop)
    th.join(timeout=5)


def make_world(tmp_path, monkeypatch, host="127.0.0.1"):
    import audit
    import board
    import projects
    from forge import access, gitcred, links

    gl = FakeGitLab(tmp_path / "repos", host=host).start()
    gl.add_user("7", "alice")
    gl.add_repo("tv/api")
    for k, v in {"SOKKAN_DATA_DIR": str(tmp_path), "SOKKAN_AUTH_MODE": "oidc",
                 "SOKKAN_FEATURE_MULTI_PROJECT": "1", "SOKKAN_FEATURE_GITLAB": "1",
                 "SOKKAN_GITLAB_URL": gl.url, "SOKKAN_GITLAB_CLIENT_ID": "cid",
                 "SOKKAN_GITLAB_CLIENT_SECRET": "csecret"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SOKKAN_FORGE_API_URL", raising=False)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    projects.create("tv", "TV", access_source="forge")
    access.add_repo("tv", "gitlab", gl.url, "tv/api")
    board.add_sdk_session(SID, "backend", title="tv work", project="tv")
    board.add_sdk_session(OTHER, "backend", title="tv other", project="tv")
    live = {SID: ALICE, OTHER: ALICE}
    monkeypatch.setattr(gitcred, "LIVE_USER", lambda sid: live.get(sid))

    gl.set_level("tv/api", "7", 30)
    from forge.gitlab import GitLab
    v, ch = links.pkce_pair()
    t = GitLab(gl.url, "cid", "csecret").exchange(gl.consent("7", ch), v, "http://cockpit/cb")
    links.save(ALICE, "gitlab", gl.url, t, "7", "alice")
    access.resolve(ALICE, "tv", force=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    return {"gl": gl, "token": t.access_token, "live": live, "home": home, "tmp": tmp_path}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    yield w
    w["gl"].stop()


def _base_env(home: Path) -> dict:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("GIT_", "SOKKAN_FORGE", "SOKKAN_RELAY"))}
    base["HOME"] = str(home)
    base.pop("XDG_CONFIG_HOME", None)
    return {**base, **IDENT}


def _assert_token_nowhere(world, env, outputs, capfd=None):
    token = world["token"]
    assert token not in "\n".join(f"{k}={v}" for k, v in env.items())
    for o in outputs:
        assert token not in o
    assert not (world["home"] / ".git-credentials").exists()
    for p in world["tmp"].rglob("*"):
        if p.is_file() and not p.is_socket() and p.stat().st_size < 5_000_000:
            assert token.encode() not in p.read_bytes(), p
    if capfd is not None:
        out, err = capfd.readouterr()
        assert token not in out and token not in err


def _git_push_script(url: str, branch: str) -> str:
    return (f"set -e; git clone -q {url} repo-{branch}; cd repo-{branch}; "
            f"git checkout -q -b {branch}; echo {branch} > f.txt; git add f.txt; "
            f"git commit -q -m {branch}; git push -o merge_request.create origin {branch}")


def _pushed(world, branch: str) -> bool:
    r = subprocess.run(["git", "--git-dir", str(world["gl"].repos_dir / "tv/api.git"),
                        "rev-parse", "--verify", f"refs/heads/{branch}"], capture_output=True)
    return r.returncode == 0


def _relay_ask(addr: tuple[str, int], token: str, req: dict) -> dict:
    with socket.create_connection(addr, timeout=20) as s:
        s.sendall((json.dumps({"token": token, "op": "git-credential", "request": req})
                   + "\n").encode())
        return json.loads(s.makefile().readline() or "{}")


def _start_relay(loop, monkeypatch, host: str) -> tuple[str, int]:
    from runner import common, relay
    port = _free_port(host)
    monkeypatch.setenv("SOKKAN_RUNNER_SECRET", "forge-relay-test")
    monkeypatch.setenv("SOKKAN_RUNNER_RELAY_BIND", f"{host}:{port}")
    loop(relay.ensure_started())
    for sid in (SID, OTHER):
        relay.register(common.token_for(sid, "relay"), sid, {})
    return host, port


# ---- relay (docker / kubernetes runner) ------------------------------------------------

def test_relay_push_simulated_pod(world, loop, monkeypatch, capfd):
    """What a pod does: the helper on PATH, SOKKAN_RELAY_ADDR/TOKEN, no api URL."""
    from forge import gitcred
    from runner import common
    addr = _start_relay(loop, monkeypatch, "127.0.0.1")
    monkeypatch.setenv("SOKKAN_FORGE_POD_HELPER", f"'{sys.executable}' '{HELPER}'")
    extra = gitcred.session_env(SID, ALICE, "tv", base_env={}, transport="relay")
    assert "SOKKAN_FORGE_API_URL" not in extra and "SOKKAN_FORGE_SOCKET" not in extra
    env = {**_base_env(world["home"]), **extra,
           "SOKKAN_RELAY_ADDR": f"{addr[0]}:{addr[1]}",
           "SOKKAN_RELAY_TOKEN": common.token_for(SID, "relay")}
    r = subprocess.run(["bash", "-c", _git_push_script(world["gl"].url + "/tv/api.git", "pod-x")],
                       cwd=world["tmp"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert _pushed(world, "pod-x")
    _assert_token_nowhere(world, env, [r.stdout, r.stderr], capfd)


def test_relay_refuses_a_replayed_ticket(world, loop, monkeypatch, capfd):
    from forge import gitcred
    from runner import common
    addr = _start_relay(loop, monkeypatch, "127.0.0.1")
    host = world["gl"].url.split("//", 1)[1]
    req = {"ticket": gitcred.ticket(SID, ALICE), "action": "get", "protocol": "http",
           "host": host, "path": "tv/api.git"}
    ok = _relay_ask(addr, common.token_for(SID, "relay"), req)
    assert ok.get("password") == world["token"]          # its own channel: answered
    # the same ticket on ANOTHER session's relay channel (replayed / stolen)
    assert _relay_ask(addr, common.token_for(OTHER, "relay"), req) == {
        "reason": "ticket of another session"}
    # no relay token / a forged one
    assert _relay_ask(addr, "nope", req) == {"reason": "unknown relay token"}
    # a forged ticket
    assert _relay_ask(addr, common.token_for(SID, "relay"),
                      {**req, "ticket": req["ticket"][:-2] + "00"}) == {"reason": "invalid ticket"}
    # the session ended: its ticket is worth nothing
    world["live"].pop(SID)
    assert _relay_ask(addr, common.token_for(SID, "relay"), req) == {
        "reason": "no live session of this person"}
    capfd.readouterr()


def _docker_or_skip():
    if not shutil.which("docker"):
        pytest.skip("docker CLI not found")
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30)
    if r.returncode != 0:
        pytest.skip(f"no Docker daemon or no image {IMAGE} with git + python3 "
                    "(SOKKAN_TEST_GIT_IMAGE)")


def test_relay_push_from_a_real_docker_container(tmp_path, loop, monkeypatch, capfd):
    """A real container on its own bridge network: the api's loopback is NOT reachable, the
    relay and the forge are (on the gateway) — like a session container / pod."""
    _docker_or_skip()
    net = f"sokkan-fp-{os.getpid()}"
    subnet = os.environ.get("SOKKAN_TEST_SUBNET") or f"10.250.{os.getpid() % 200 + 20}.0/24"
    subprocess.run(["docker", "network", "create", "--subnet", subnet,
                    "--label", "sokkan.ch/test=1", net], check=True, capture_output=True)
    try:
        gw = json.loads(subprocess.run(["docker", "network", "inspect", net],
                                       capture_output=True, text=True, check=True).stdout
                        )[0]["IPAM"]["Config"][0]["Gateway"]
        world = make_world(tmp_path, monkeypatch, host=gw)
        try:
            from forge import gitcred
            from runner import common
            addr = _start_relay(loop, monkeypatch, gw)
            extra = gitcred.session_env(SID, ALICE, "tv", base_env={}, transport="relay")
            env = {**extra, **IDENT, "HOME": "/tmp",
                   "SOKKAN_RELAY_ADDR": f"{addr[0]}:{addr[1]}",
                   "SOKKAN_RELAY_TOKEN": common.token_for(SID, "relay")}
            args = ["docker", "run", "--rm", "--network", net, "--entrypoint", "bash",
                    "-v", f"{HELPER}:/usr/local/bin/sokkan-git-credential:ro", "-w", "/tmp"]
            for k in env:
                args += ["-e", k]            # values from the environment, not argv
            script = ("python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(2); "
                      "sys.exit(0 if s.connect_ex((\"127.0.0.1\", 8097)) else 1)' "
                      "&& echo LOOPBACK-API-UNREACHABLE; "
                      + _git_push_script(world["gl"].url + "/tv/api.git", "docker-x"))
            r = subprocess.run([*args, IMAGE, "-c", script], env={**os.environ, **env},
                               capture_output=True, text=True, timeout=300)
            assert r.returncode == 0, r.stderr
            assert "LOOPBACK-API-UNREACHABLE" in r.stdout
            assert _pushed(world, "docker-x")
            _assert_token_nowhere(world, env, [r.stdout, r.stderr], capfd)
        finally:
            world["gl"].stop()
    finally:
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


# ---- Unix socket (Bash inside bubblewrap) ----------------------------------------------

@pytest.fixture()
def bwrap_mode(monkeypatch):
    import sandbox
    if not BW:
        pytest.skip("bubblewrap not found (install bubblewrap or set SOKKAN_TEST_BWRAP)")
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "1")
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", BW)
    monkeypatch.delenv("SOKKAN_SANDBOX_NETWORK", raising=False)
    sandbox.reset()
    if sandbox.detect(force=True) != sandbox.BWRAP:
        pytest.skip("bubblewrap does not run here (user namespaces?)")
    yield sandbox
    sandbox.reset()


def test_bwrap_push_through_the_session_socket(world, loop, bwrap_mode, capfd):
    from forge import gitcred
    sandbox = bwrap_mode
    work = sandbox.project_dir("tv") / "work"
    work.mkdir(parents=True, exist_ok=True)
    sock = loop(gitcred.open_socket(SID))
    extra = gitcred.session_env(SID, ALICE, "tv", base_env={}, transport="socket")
    assert extra["SOKKAN_FORGE_SOCKET"] == gitcred.SANDBOX_SOCKET
    env = {**_base_env(world["home"]), **extra}
    script = sandbox.wrapper_script(SID, "tv", str(work), env_names=sorted({*extra, *IDENT}),
                                    forge_socket=sock)
    gl_port = int(world["gl"].url.rsplit(":", 1)[1])
    other = _free_port()
    cmd = (
        # no network: the forge is not reachable directly
        f"python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(2); "
        f"sys.exit(0 if s.connect_ex((\"127.0.0.1\", {gl_port})) else 1)' "
        "&& echo NO-DIRECT-NETWORK; "
        # the socket tunnels to the project's forge only
        f"curl -s -o /dev/null -w 'OTHER=%{{http_code}}\\n' -x http://127.0.0.1:"
        f"{gitcred.SANDBOX_FORWARD_PORT} http://127.0.0.1:{other}/ ; "
        + _git_push_script(world["gl"].url + "/tv/api.git", "bw-x"))
    r = subprocess.run([script, cmd], env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NO-DIRECT-NETWORK" in r.stdout and "OTHER=403" in r.stdout
    assert _pushed(world, "bw-x")
    _assert_token_nowhere(world, env, [r.stdout, r.stderr], capfd)


def test_session_socket_refuses_a_ticket_of_another_session(world, loop):
    from forge import gitcred
    sock = loop(gitcred.open_socket(SID))
    host = world["gl"].url.split("//", 1)[1]

    def ask(t):
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(sock)
            s.sendall((json.dumps({"ticket": t, "action": "get", "protocol": "http",
                                   "host": host}) + "\n").encode())
            return json.loads(s.makefile().readline())
    assert ask(gitcred.ticket(SID, ALICE)).get("password") == world["token"]
    assert ask(gitcred.ticket(OTHER, ALICE)) == {"reason": "ticket of another session"}
    assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
    loop(gitcred.close_socket(SID))
    assert not os.path.exists(sock)
