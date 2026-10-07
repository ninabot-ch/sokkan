"""Lot 5 — a session pushes with the PERSON's GitLab token, through the credential helper,
with real git against a fake GitLab (git http-backend). Proves:

* Developer: pushes a branch with ``-o merge_request.create`` (the option reaches GitLab),
  cannot push the protected ``main``; Maintainer can; Reporter cannot push at all;
* no linked account → the push fails at once (no prompt) and says why;
* the token is never in the session's environment, never in git's output (= what a
  transcript would hold), never on disk (no credential store, even when the user's git
  config asks for one), never in the API's logs.
"""
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from fake_gitlab import FakeGitLab

ALICE = "alice@x"
SID = "a" * 32


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def world(tmp_path, monkeypatch, capfd):
    import uvicorn
    from fastapi import FastAPI

    import audit
    import board
    import projects
    from forge import access, links
    from forge import routes as froutes

    gl = FakeGitLab(tmp_path / "repos").start()
    gl.add_user("7", "alice")
    gl.add_repo("tv/api")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_GITLAB", "1")
    monkeypatch.setenv("SOKKAN_GITLAB_URL", gl.url)
    monkeypatch.setenv("SOKKAN_GITLAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SOKKAN_GITLAB_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    projects.create("tv", "TV", access_source="forge")
    access.add_repo("tv", "gitlab", gl.url, "tv/api")
    board.add_sdk_session(SID, "backend", title="tv work", project="tv")

    # the API's forge routes on a real loopback port (the helper speaks HTTP to it)
    live = {SID: ALICE}
    api = FastAPI()

    async def close(_e):
        return 0
    api.include_router(froutes.router(lambda: {"email": ALICE, "role": "none"},
                                      lambda r: (lambda: {"email": ALICE}),
                                      lambda sid: live.get(sid), close))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    monkeypatch.setenv("SOKKAN_FORGE_API_URL", f"http://127.0.0.1:{port}")

    def link(level):
        """Alice links her account (what the callback route does) at that level."""
        gl.set_level("tv/api", "7", level)
        from forge.gitlab import GitLab
        v, ch = links.pkce_pair()
        p = GitLab(gl.url, "cid", "csecret")
        t = p.exchange(gl.consent("7", ch), v, "http://cockpit/cb")
        links.save(ALICE, "gitlab", gl.url, t, "7", "alice")
        access.resolve(ALICE, "tv", force=True)
        return t.access_token

    # a HOME whose git config asks to STORE credentials: the session must not obey it
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n"
                                     "[user]\n\tname = Alice\n\temail = alice@x\n")
    yield {"gl": gl, "link": link, "home": home, "tmp": tmp_path, "live": live}
    server.should_exit = True
    th.join(timeout=5)
    gl.stop()


def _session_env(home: Path) -> dict:
    from forge import gitcred
    base = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SOKKAN_FORGE_T"))}
    base["HOME"] = str(home)
    base.pop("XDG_CONFIG_HOME", None)
    extra = gitcred.session_env(SID, ALICE, "tv", base_env=base)
    return {**base, **extra}


def _git(env, cwd, *args, ok=True):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=60)
    if ok:
        assert r.returncode == 0, r.stderr
    return r


def _clone_commit(world, env, branch):
    work = world["tmp"] / f"work-{branch}"
    url = world["gl"].url + "/tv/api.git"
    _git(env, world["tmp"], "clone", "-q", url, str(work))
    _git(env, work, "checkout", "-q", "-b", branch)
    (work / "f.txt").write_text(branch)
    _git(env, work, "add", "f.txt")
    _git(env, work, "commit", "-q", "-m", branch)
    return work


def _assert_token_nowhere(world, token, env, outputs, capfd):
    assert token not in "\n".join(f"{k}={v}" for k, v in env.items())     # session env
    for o in outputs:                                                    # transcript material
        assert token not in (o.stdout + o.stderr)
    assert not (world["home"] / ".git-credentials").exists()             # no store helper
    for p in world["tmp"].rglob("*"):                                    # nothing on disk
        if p.is_file() and p.stat().st_size < 5_000_000:
            assert token.encode() not in p.read_bytes(), p
    out, err = capfd.readouterr()                                        # API / test logs
    assert token not in out and token not in err


def test_developer_pushes_a_branch_and_opens_a_merge_request(world, capfd):
    token = world["link"](30)
    env = _session_env(world["home"])
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["SOKKAN_FORGE_TICKET"]
    work = _clone_commit(world, env, "feature-x")
    r1 = _git(env, work, "push", "-o", "merge_request.create", "-o",
              "merge_request.target=main", "origin", "feature-x")
    log = (world["gl"].repos_dir / "tv/api.git" / "push_options.log").read_text()
    assert "merge_request.create" in log and "merge_request.target=main" in log
    # the protected branch: Developer refused by GitLab
    r2 = _git(env, work, "push", "origin", "feature-x:main", ok=False)
    assert r2.returncode != 0 and "protected branches" in r2.stderr
    # every request to GitLab's git endpoint carried ALICE's token (no technical account)
    git_auth = [h.get("Authorization", "") for m, p, h in world["gl"].requests
                if ".git/" in p and h.get("Authorization")]
    assert git_auth and all(a.startswith("Basic ") for a in git_auth)
    import base64
    assert {base64.b64decode(a[6:]).decode() for a in git_auth} == {f"oauth2:{token}"}
    _assert_token_nowhere(world, token, env, [r1, r2], capfd)


def test_maintainer_pushes_the_protected_branch(world, capfd):
    token = world["link"](40)
    env = _session_env(world["home"])
    work = _clone_commit(world, env, "hotfix")
    r = _git(env, work, "push", "origin", "hotfix:main")
    _assert_token_nowhere(world, token, env, [r], capfd)


def test_reporter_cannot_push(world, capfd):
    token = world["link"](20)
    env = _session_env(world["home"])
    work = _clone_commit(world, env, "nope")          # Reporter reads (clone works)
    r = _git(env, work, "push", "origin", "nope", ok=False)
    assert r.returncode != 0
    _assert_token_nowhere(world, token, env, [r], capfd)


def test_push_without_a_linked_account_fails_at_once_and_says_why(world, tmp_path):
    world["gl"].set_level("tv/api", "7", 30)
    env = _session_env(world["home"])
    t0 = time.time()
    r = _git(env, tmp_path, "clone", "-q", world["gl"].url + "/tv/api.git", "x", ok=False)
    assert r.returncode != 0 and time.time() - t0 < 20
    assert "no gitlab account linked" in r.stderr.lower() or "no access" in r.stderr.lower()
    assert "terminal prompts disabled" in r.stderr or "could not read Username" in r.stderr


def test_a_ticket_without_its_live_session_gets_nothing(world, capfd):
    token = world["link"](30)
    env = _session_env(world["home"])
    world["live"].clear()                              # the session was closed
    r = _git(env, world["tmp"], "clone", "-q", world["gl"].url + "/tv/api.git", "y", ok=False)
    assert r.returncode != 0 and "no live session" in r.stderr
    _assert_token_nowhere(world, token, env, [r], capfd)


def test_session_env_is_empty_outside_forge_projects(world, monkeypatch):
    from forge import gitcred
    assert gitcred.session_env(SID, ALICE, "default") == {}
    env = gitcred.session_env(SID, ALICE, "tv", base_env={"GIT_CONFIG_COUNT": "2"})
    assert env["GIT_CONFIG_KEY_2"].startswith("credential.") and env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_VALUE_2"] == ""             # helper list reset first
    monkeypatch.setenv("SOKKAN_FEATURE_GITLAB", "0")
    assert gitcred.session_env(SID, ALICE, "tv") == {}
