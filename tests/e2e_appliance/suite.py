#!/usr/bin/env python3
"""tests/e2e_appliance — the T1-T14 test plan of the on-prem appliance, played on a blank
« machine » (a Docker-in-Docker container: own engine, own network, own volumes).

Runs ON that machine (stdlib only), started by run.sh / inner.sh:

    suite.py prepare            sign the release with a throw-away key, build and push its images
    suite.py appliance          T1-T7, T11, T12, T14 on a fresh appliance install
    suite.py after-reboot       T13, after the machine's engine was restarted from outside
    suite.py upgrade            T8-T10: 3.4.6 → this release by both entries, rollback
    suite.py report             the table of results

Every test appends one record to /work/results.jsonl:
    {"id": "T4", "name": …, "pass": true|false|null, "measures": {…}, "detail": "…"}
pass = null means NOT PLAYED (with the reason) — never counted as a success.
PASS criteria are the assertions below, written out in README.md.
"""
from __future__ import annotations

import http.cookiejar as cookiejar
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

WORK = Path(os.environ.get("E2E_WORK", "/work"))
DISTS = Path(os.environ.get("E2E_DISTS", "/dists"))
HARNESS = Path(__file__).resolve().parent
BOX = Path("/opt/sokkan")
BASE = "http://127.0.0.1:3009"
DIST_URL = "http://127.0.0.1:8900"
SHIP, DEEP = "http://127.0.0.1:8004", "http://127.0.0.1:8791"
DEEP_EXT = "http://203.0.113.10:8791"        # the gateway's « public » address (TEST-NET-3 on lo)
REG = "localhost:5000/sokkan"
HOSTNAME = "sokkan.e2e.lan"
HTTPS_BASE = f"https://{HOSTNAME}"
NEW = (DISTS / "NEW_VERSION").read_text().strip() if (DISTS / "NEW_VERSION").exists() else ""
# the release the upgrade phase starts from: 3.4.6 (the oldest a client runs) unless E2E_OLD names
# another dist present in /dists (e.g. 3.5.1+6f7fee5 — the instances we run ourselves)
OLD = os.environ.get("E2E_OLD") or "3.4.6+3ea74f5"
RESULTS = WORK / "results.jsonl"
PEOPLE = [("ana@pilot.example", "Ana", "projet-ana"), ("ben@pilot.example", "Ben", "projet-ben"),
          ("cleo@pilot.example", "Cléo", "projet-cleo")]
ENV = {**os.environ, "SOKKAN_BASE": DIST_URL, "SOKKAN_COSIGN_PUB": "/etc/sokkan/cosign.pub",
       "SOKKAN_COSIGN_IMAGE_FLAGS": "--allow-insecure-registry", "SOKKAN_SKIP_HOST_SETUP": "1",
       "SOKKAN_VERIFY_NO_ENVFILE": "1", "SOKKAN_UPGRADE_WAIT_S": "1500"}


# ---------------------------------------------------------------- plumbing
def sh(cmd: str, cwd: str | Path | None = None, env: dict | None = None, timeout: int = 3600,
       check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, cwd=cwd, env=env or ENV, capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"$ {cmd}\nrc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r


def record(tid: str, name: str, ok: bool | None, measures: dict | None = None, detail: str = "") -> None:
    rec = {"id": tid, "name": name, "pass": ok, "measures": measures or {}, "detail": detail[:1500],
           "ts": int(time.time())}
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    mark = {True: "PASS", False: "FAIL", None: "NOT PLAYED"}[ok]
    print(f"RESULT {tid} {mark} — {name} {json.dumps(measures or {}, ensure_ascii=False)} {detail[:300]}", flush=True)


def test(tid: str, name: str):
    """Decorator: a test returns (measures, detail); an AssertionError / exception = FAIL."""
    def deco(fn):
        def run(*a, **k):
            t0 = time.time()
            try:
                out = fn(*a, **k) or {}
                record(tid, name, True, {**out, "test_s": round(time.time() - t0, 1)})
                return True
            except AssertionError as e:
                record(tid, name, False, {"test_s": round(time.time() - t0, 1)}, f"assertion: {e}")
            except Exception as e:  # noqa: BLE001
                record(tid, name, False, {"test_s": round(time.time() - t0, 1)}, f"{type(e).__name__}: {e}")
            return False
        return run
    return deco


def http(url: str, data: dict | None = None, method: str | None = None, timeout: float = 15):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"content-type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


class Person:
    """A browser: cookie jar, optional selected project."""

    def __init__(self, code: str, project: str = "", base: str = ""):
        # the appliance's session cookie is `Secure` (SOKKAN_PUBLIC_URL is https): a browser — and
        # this client — only sends it over HTTPS, with the box's CA. Plain HTTP is for phase B
        # (a community install of 3.4.6).
        self.code, self.project, self.base = code, project, base or HTTPS_BASE
        self.jar = cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base.startswith("https"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(WORK / "ca.crt"))))
        self.op = urllib.request.build_opener(*handlers)

    def call(self, path: str, data: dict | None = None, method: str | None = None, timeout: float = 30):
        h = {"content-type": "application/json"}
        if self.project:
            h["x-sokkan-project"] = self.project
        req = urllib.request.Request(self.base + path, headers=h, method=method,
                                     data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, OSError):
            return 0, None

    def login(self) -> int:
        return self.call("/api/auth/local", {"token": self.code})[0]

    def cookie(self) -> str:
        return next((c.value for c in self.jar if c.name == "sokkan_session"), "")

    def turn(self, prompt: str, timeout: int = 150) -> dict:
        """One session with one message: {text, events, error (the readable one), s}."""
        t0 = time.time()
        st, s = self.call("/api/spawn", {"prompt": prompt, "title": prompt[:40], "tag": "session"})
        assert st == 200, f"spawn → {st} {s}"
        sid = s["session_id"]
        r = sh(f"docker compose exec -T -u 1000 api python3 /tmp/ws_session.py {sid} '{self.cookie()}' "
               f"--timeout {timeout}", cwd=BOX, timeout=timeout + 30)
        evs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        text = " ".join(str(e.get("text") or "") for e in evs if e.get("type") in ("assistant", "text", "result"))
        err = next((e for e in evs if e.get("type") == "error" and e.get("code")), None)
        return {"sid": sid, "text": text, "events": evs, "error": err, "s": round(time.time() - t0, 1),
                "rc": r.returncode}


def thread_of(p: Person, sid: str) -> tuple[list, list]:
    """What that person reads when they open the work `sid` in the simple profile: (their own
    messages, the agent's sentences) of the replayed thread."""
    r = sh(f"docker compose exec -T -u 1000 api python3 /tmp/ws_session.py {sid} '{p.cookie()}' "
           "--replay --view simple --timeout 60", cwd=BOX, timeout=90)
    evs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    return ([e.get("text") or "" for e in evs if e.get("type") == "user"],
            [e.get("text") or "" for e in evs if e.get("type") == "text"])


THREAD_ASK = "Prépare la relance du devis de mars."


def seed_thread() -> None:
    """Before the reboot: one person starts a work in the simple profile, in their own project,
    and reads its thread. T13 opens the same work again after the reboot."""
    s = state()
    e, _, proj = PEOPLE[0]
    try:
        u = Person(s["codes"][e], proj)
        assert u.login() == 200, "sign-in"
        st, w = u.call("/api/simple/start", {"ask": THREAD_ASK, "lang": "fr"}, timeout=120)
        assert st == 200, f"start → {st} {w}"
        t0 = time.time()
        mine, agent = thread_of(u, w["session_id"])
        while not any("[ship] ok" in x for x in agent) and time.time() - t0 < 150:
            time.sleep(3)
            mine, agent = thread_of(u, w["session_id"])
        assert THREAD_ASK in mine and any("[ship] ok" in x for x in agent), f"thread before reboot: {mine} {agent}"
        state(thread={"email": e, "project": proj, "sid": w["session_id"]})
    except Exception as ex:  # noqa: BLE001 — said by T13, which cannot check the thread then
        state(thread={"error": repr(ex)[:300]})


def work_export_of(p: Person) -> tuple[int, list]:
    """GET /api/admin/work-export as that person: (status, the names inside the zip)."""
    try:
        with p.op.open(urllib.request.Request(p.base + "/api/admin/work-export"), timeout=120) as r:
            return r.status, zipfile.ZipFile(io.BytesIO(r.read())).namelist()
    except urllib.error.HTTPError as e:
        return e.code, []
    except (urllib.error.URLError, OSError, zipfile.BadZipFile):
        return 0, []


def env_of(d: Path = BOX) -> dict:
    return dict(x.split("=", 1) for x in (d / ".env").read_text().splitlines()
                if "=" in x and not x.startswith("#"))


def set_env(key: str, value: str, d: Path = BOX) -> None:
    lines = [x for x in (d / ".env").read_text().splitlines() if not x.startswith(key + "=")]
    (d / ".env").write_text("\n".join(lines + [f"{key}={value}"]) + "\n")


def seen(base: str) -> dict:
    return http(base + "/_seen")[1] or {}


def mode(base: str, status: int) -> None:
    http(base + "/_mode", {"status": status})


def wait_health(limit: int = 600, want: str = "") -> float:
    t0 = time.time()
    while time.time() - t0 < limit:
        st, _ = http(BASE + "/api/health", timeout=4)
        if st == 200:
            if not want:
                return time.time() - t0
            v = http(BASE + "/api/version", timeout=4)[1] or {}
            if v.get("dist") == want or v.get("version") == want:
                return time.time() - t0
        time.sleep(2)
    raise AssertionError(f"no healthy answer{' as ' + want if want else ''} within {limit}s")


def mem_peak_mb() -> int:
    """Peak memory of the whole machine (the dind container's cgroup), MiB."""
    for p in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.max_usage_in_bytes"):
        try:
            return int(Path(p).read_text()) // (1 << 20)
        except (OSError, ValueError):
            continue
    return -1


def mem_now_mb() -> int:
    """Memory the machine's processes hold NOW (anonymous + shared memory, MiB) — without the
    page cache, which fills whatever the cap allows after an image build and says nothing."""
    try:
        st = dict(x.split() for x in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
        return (int(st["anon"]) + int(st.get("shmem", 0))) // (1 << 20)
    except (OSError, ValueError, KeyError):
        return -1


def listeners() -> list[str]:
    out = sh("ss -ltnH 2>/dev/null || netstat -ltn").stdout
    return sorted({ln.split()[3] for ln in out.splitlines() if len(ln.split()) > 3})


def start_mocks() -> None:
    sh("ip addr add 203.0.113.10/32 dev lo 2>/dev/null; true")
    sh(f"grep -q ' {HOSTNAME}$' /etc/hosts || echo '127.0.0.1 {HOSTNAME}' >> /etc/hosts")
    for name, port in (("ship", 8004), ("deep", 8791)):
        if http(f"http://127.0.0.1:{port}/health", timeout=2)[0] != 200:
            subprocess.Popen(["python3", str(HARNESS / "mock_llm.py"), name, str(port)],
                             stdout=open(WORK / f"mock-{name}.log", "a"), stderr=subprocess.STDOUT,
                             start_new_session=True)
    for port in (8004, 8791):
        for _ in range(30):
            if http(f"http://127.0.0.1:{port}/health", timeout=2)[0] == 200:
                break
            time.sleep(0.5)


def stop_mock(port: int) -> None:
    sh(f"for p in $(ss -ltnpH 'sport = :{port}' | sed -n 's/.*pid=\\([0-9]*\\).*/\\1/p' | sort -u); do kill $p; done")
    time.sleep(1)


def serve_dist() -> None:
    if http(DIST_URL + "/dist/VERSION", timeout=2)[0] == 0 and sh(f"curl -fs {DIST_URL}/dist/VERSION").returncode:
        subprocess.Popen(["python3", "-m", "http.server", "8900", "--bind", "127.0.0.1", "--directory",
                          str(WORK / "srv")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(1.5)


# ---------------------------------------------------------------- prepare (publisher side)
def prepare() -> None:
    """What the publisher does at a release — here with a THROW-AWAY key pair: sign the tarball,
    build the images FROM the tarball, push them to a registry, sign them by digest."""
    t0 = time.time()
    srv = WORK / "srv" / "dist"
    srv.mkdir(parents=True, exist_ok=True)
    for f in DISTS.glob("sokkan-*.tar.gz"):
        shutil.copy(f, srv / f.name)
    (srv / "VERSION").write_text(NEW + "\n")
    shutil.copy(srv / f"sokkan-{NEW}.tar.gz", srv / "sokkan-latest.tar.gz")
    shutil.copy(DISTS / "install-new.sh", WORK / "srv" / "install.sh")
    keys = WORK / "keys"
    keys.mkdir(exist_ok=True)
    if not (keys / "cosign.key").exists():
        sh("COSIGN_PASSWORD=e2e cosign generate-key-pair", cwd=keys, check=True)
    Path("/etc/sokkan").mkdir(exist_ok=True)
    shutil.copy(keys / "cosign.pub", "/etc/sokkan/cosign.pub")
    sh("docker inspect e2e-registry >/dev/null 2>&1 || docker run -d --name e2e-registry --restart unless-stopped "
       "-p 127.0.0.1:5000:5000 registry:2", check=True)
    # an S3-compatible store for T11 (SeaweedFS: MinIO's image left Docker Hub in 2026)
    sh("docker inspect e2e-s3 >/dev/null 2>&1 || docker run -d --name e2e-s3 --restart unless-stopped "
       "-p 127.0.0.1:9000:8333 chrislusf/seaweedfs:latest server -dir=/data -s3", check=True)
    src = WORK / "newsrc"
    shutil.rmtree(src, ignore_errors=True)
    src.mkdir()
    sh(f"tar xzf {srv}/sokkan-{NEW}.tar.gz -C {src} --strip-components=1", check=True)
    env = {**ENV, "SOKKAN_COSIGN_KEY": str(keys / "cosign.key"), "COSIGN_PASSWORD": "e2e"}
    r = sh(f"sh {src}/scripts/release-artifacts.sh {srv}/sokkan-{NEW}.tar.gz --images {REG}", env=env, timeout=5400)
    ok = r.returncode == 0 and (srv / f"sokkan-{NEW}.tar.gz.images").exists()
    if ok:
        # the machine that installs is not the one that built: drop the images from this engine
        # so that T1 really pulls them from the registry (the build cache is another store)
        # (every tag of this and of earlier runs: the engine's cache volume may be reused)
        sh("for i in $(docker images --format '{{.Repository}}:{{.Tag}}@{{.ID}}' | "
           "grep -E '^(localhost:5000/sokkan/|sokkan-api:|sokkan-web:)' | sed 's/.*@//' | sort -u); "
           "do docker rmi -f $i; done")
        left = sh(f"docker images -q {REG}/sokkan-api").stdout.strip()
        assert not left, f"the built image is still in the engine: {left}"
    record("T0", "publisher side: release signed, images built from the tarball, pushed and signed", ok,
           {"build_sign_s": round(time.time() - t0), "images": (srv / f"sokkan-{NEW}.tar.gz.images").read_text().split()
            if ok else []}, (r.stdout + r.stderr)[-600:] if not ok else "")
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------- phase A: the appliance
ANSWERS = {"SOKKAN_ORG_NAME": "Pilot E2E", "SOKKAN_OWNER_EMAIL": "admin@pilot.example", "SOKKAN_OWNER_NAME": "Admin",
           "SOKKAN_BOX_NAME": HOSTNAME, "SOKKAN_BOX_IP": "127.0.0.1", "SOKKAN_SHIP_URL": "http://host.docker.internal:8004",
           "SOKKAN_SHIP_MODEL": "qwen3.8-27b", "SOKKAN_DEEP_URL": DEEP_EXT, "SOKKAN_DEEP_TOKEN": "sik_e2e_instance",
           "SOKKAN_INSTANCE_ID": "pilot-e2e", "SOKKAN_HEARTBEAT_URL": DEEP_EXT + "/hb", "SOKKAN_HEARTBEAT_TOKEN": "hb-e2e",
           "SOKKAN_ACCEPT_GEMMA_TERMS": "0", "SOKKAN_ACCOUNTS": ",".join(f"{e}:{n}:{p}" for e, n, p in PEOPLE),
           # the bench never touches a GPU: a privileged container sees the host's cards and the
           # installer would pick the gpu profile — the CPU profile is forced, and checked
           "SOKKAN_MEMORY_PROFILE": "leger"}


def no_gpu(d: Path) -> None:
    e = env_of(d)
    assert "sycl" not in e.get("COMPOSE_FILE", "") and "cuda" not in e.get("COMPOSE_FILE", ""), \
        f"a GPU override is configured in {d}: {e.get('COMPOSE_FILE')}"
    assert e.get("SOKKAN_EMBED_ACCEL", "cpu") == "cpu", e.get("SOKKAN_EMBED_ACCEL")
    assert not Path("/dev/dri").exists(), "the test machine exposes /dev/dri"
STATE = WORK / "state.json"


def state(**kw) -> dict:
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    if kw:
        s.update(kw)
        STATE.write_text(json.dumps(s, indent=1))
    return s


def install_appliance(target: Path = BOX) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.time()
    r = sh(f"sh {WORK}/srv/install.sh --profile appliance --version '{NEW}' --dir {target}",
           env={**ENV, **ANSWERS}, timeout=3600)
    (WORK / f"install-{target.name}.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr)
    return r, time.time() - t0


def host_side() -> None:
    """What scripts/appliance/host-setup.sh does on a real box (systemd is absent here): the
    shared folder, writable by the app."""
    vol = sh("docker volume inspect -f '{{.Mountpoint}}' sokkan_sokkan-data").stdout.strip()
    sh(f"install -d -m 0775 -o 1000 -g 1000 {vol}/appliance", check=True)
    Path("/etc/sokkan/appliance.env").write_text(
        f"SOKKAN_APPLIANCE_DIR={vol}/appliance\nSOKKAN_SUPPORT_MAX_HOURS=4\nSOKKAN_REQUIRE_SIGNATURE=1\n")
    sh("docker compose cp /harness/e2e_appliance/ws_session.py api:/tmp/ws_session.py", cwd=BOX, check=True)


@test("T1", "blank machine → appliance install, pinned + signed release, prebuilt images")
def t1():
    # 1a. fail closed first: the same release WITHOUT its signature must leave nothing behind
    sig = WORK / "srv" / "dist" / f"sokkan-{NEW}.tar.gz.sig"
    hidden = sig.with_suffix(".sig.hidden")
    sig.rename(hidden)
    try:
        r, _ = install_appliance(Path("/opt/unsigned"))
        assert r.returncode != 0 and "REFUSED" in r.stderr, f"unsigned release was accepted: {r.stderr[-300:]}"
        assert not Path("/opt/unsigned").exists(), "an unsigned install left files behind"
    finally:
        hidden.rename(sig)
    # 1b. the real install
    r, dur = install_appliance()
    assert r.returncode == 0, f"installer rc={r.returncode}: {(r.stderr or r.stdout)[-600:]}"
    no_gpu(BOX)
    v = http(BASE + "/api/version")[1] or {}
    assert v.get("dist") == NEW, f"/api/version says {v} — expected dist {NEW}"
    assert v.get("commit") == NEW.split("+")[1], v
    e = env_of()
    assert e["SOKKAN_VERSION"] == NEW and e["SOKKAN_IMAGES"] == "1" and e["SOKKAN_REQUIRE_SIGNATURE"] == "1"
    assert oct((BOX / ".env").stat().st_mode & 0o777) == "0o600", ".env is readable by others"
    assert not [ln for ln in (BOX / ".env").read_text().splitlines() if not ln.startswith("#") and "=<" in ln]
    # the containers run the SIGNED images (by digest), nothing was built here
    want = dict(x.split("=", 1) for x in (WORK / "srv" / "dist" / f"sokkan-{NEW}.tar.gz.images").read_text().split())
    for svc, name in (("api", "api"), ("web", "web"), ("router", "api")):
        cid = sh(f"docker compose ps -q {svc}", cwd=BOX).stdout.strip()
        img = sh(f"docker inspect -f '{{{{.Image}}}}' {cid}").stdout.strip()
        ref = sh(f"docker inspect -f '{{{{.Id}}}}' {want[name]}").stdout.strip()
        assert cid and img == ref, f"{svc} does not run the signed image ({img} vs {ref})"
    assert "verify: image signature OK" in r.stderr, "image signatures were not verified"
    # what listens: plain HTTP on the loopback only, HTTPS for the network, nothing else of SOKKAN
    ls = listeners()
    assert "127.0.0.1:3009" in ls and not any(x.endswith(":3009") and not x.startswith("127.") for x in ls), ls
    assert any(x.endswith(":443") for x in ls), ls
    for port in (":8097", ":8096", ":5432", ":8080"):
        assert not any(x.endswith(port) for x in ls), f"{port} is published: {ls}"
    codes = dict(re.findall(r"^\s+(\S+@\S+)\t(sk-local-\S+)", r.stdout, re.M))
    assert len(codes) == 3, f"3 personal codes expected in the installer's output, got {len(codes)}"
    owner = re.search(r"access code — shown once.*\n\s+([0-9a-f]{48})", r.stdout)
    assert owner, "owner code not printed"
    state(owner=owner.group(1), codes=codes, install_s=round(dur))
    host_side()
    return {"install_s": round(dur), "ram_after_install_mb": mem_now_mb(),
            "listeners": [x for x in ls if x.split(":")[-1] in ("3009", "443")]}


def export_ca() -> None:
    """The box's certificate authority, as the technician hands it to the workstations."""
    for _ in range(30):
        sh(f"curl -sk -m 5 -o /dev/null --resolve {HOSTNAME}:443:127.0.0.1 https://{HOSTNAME}/api/health")  # first certificate
        r = sh("sh scripts/appliance/export-ca.sh /work/ca.crt", cwd=BOX)
        if r.returncode == 0:
            return
        time.sleep(3)
    raise AssertionError(f"CA export failed: {r.stderr}")


@test("T2", "first launch: HTTPS, 3 named people, local engine ready, no dead screen")
def t2():
    s = state()
    # HTTPS with the box's own CA; the plain port is not reachable from the network address
    export_ca()
    c = sh(f"curl -s -o /dev/null -w '%{{http_code}}' --cacert /work/ca.crt --resolve {HOSTNAME}:443:127.0.0.1 "
           f"https://{HOSTNAME}/api/health")
    assert c.stdout.strip() == "200", f"https with the exported CA → {c.stdout} {c.stderr}"
    bad = sh(f"curl -s -o /dev/null -w '%{{http_code}}' --resolve {HOSTNAME}:443:127.0.0.1 https://{HOSTNAME}/api/health")
    assert bad.stdout.strip() != "200", "https answered without trusting the CA (system store?)"
    ip = sh("ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1").stdout.strip()
    lan = sh(f"curl -s -m 4 -o /dev/null -w '%{{http_code}}' http://{ip}:3009/api/health").stdout.strip()
    assert lan in ("000", ""), f"plain HTTP answers on the network address {ip}: {lan}"
    owner = Person(s["owner"])
    assert owner.login() == 200
    me = owner.call("/api/me")[1]
    assert me["email"] == "admin@pilot.example" and me["instance_role"] == "owner", me
    assert owner.call("/api/instance")[1]["org_name"] == "Pilot E2E"
    llm = owner.call("/api/llm")[1]
    assert llm["configured"] and llm.get("engine") == "appliance" and llm["model"] == "sokkan-ship", llm
    inf = owner.call("/api/appliance/inference")[1]
    assert inf.get("ship") is True and inf.get("deep") is True, inf
    f = owner.call("/api/features")[1]
    dead = {k: f.get(k) for k in ("tmux", "preview", "magnitude", "missions_link", "alerting", "observe", "infra")}
    assert not any(dead.values()), f"screens that cannot work here are on: {dead}"
    assert f["multi_project"] is True
    # a wrong code is refused; each person is themselves, in their own project only
    assert Person("sk-local-nope").login() == 401
    for (e, n, p) in PEOPLE:
        u = Person(s["codes"][e], p)
        assert u.login() == 200, e
        m = u.call("/api/me")[1]
        assert m["email"] == e and m["name"] == n and m["instance_role"] == "none", m
        slugs = {x["slug"] for x in u.call("/api/projects")[1]["projects"]}
        assert p in slugs and not slugs & {q for _, _, q in PEOPLE if q != p}, slugs
    t = owner.turn("Bonjour, réponds simplement.")
    assert "[ship] ok" in t["text"], f"no answer from the local model: rc={t['rc']} {t['text'][:200]}"
    return {"first_answer_s": t["s"], "https": "200 with exported CA", "people": 3}


@test("T3", "3 people, 3 simultaneous sessions, isolated, each under their own name")
def t3():
    s = state()
    http(SHIP + "/_reset", {})
    people = [Person(s["codes"][e], p) for e, _, p in PEOPLE]
    for u in people:
        assert u.login() == 200
    out: list = [None] * 3
    base_mem = mem_now_mb()

    def go(i: int) -> None:
        try:
            out[i] = people[i].turn(f"Session de {PEOPLE[i][1]} : résume ce document en trois points.")
        except Exception as e:  # noqa: BLE001
            out[i] = {"text": "", "exc": repr(e), "s": -1}

    th = [threading.Thread(target=go, args=(i,)) for i in range(3)]
    t0 = time.time()
    [x.start() for x in th]
    peak = base_mem
    while any(x.is_alive() for x in th):
        peak = max(peak, mem_now_mb())
        time.sleep(0.5)
    wall = time.time() - t0
    for i, o in enumerate(out):
        assert o and "[ship] ok" in o["text"], f"{PEOPLE[i][0]}: {o}"
    # isolation: nobody sees a session of another project
    for i, u in enumerate(people):
        mine = {x["session_id"] for x in u.call("/api/sessions")[1]}
        assert out[i]["sid"] in mine
        other = Person(s["codes"][PEOPLE[(i + 1) % 3][0]], PEOPLE[i][2])
        other.login()
        assert other.call("/api/sessions")[0] == 404, "another person reads this project"
    owner = Person(s["owner"])
    owner.login()
    ev = owner.call("/api/audit?limit=400")[1]
    who = {e["user"] for e in ev if e["action"] == "session.spawn"}
    assert {e for e, _, _ in PEOPLE} <= who, f"journal names: {who}"
    assert {e["user"] for e in ev if e["action"] == "auth.login"} >= {e for e, _, _ in PEOPLE}
    return {"wall_s": round(wall, 1), "slowest_s": max(o["s"] for o in out), "ram_before_mb": base_mem,
            "ram_peak_3_sessions_mb": peak, "ship_max_concurrent": seen(SHIP).get("max_concurrent")}


@test("T4", "forced escalation: only the request that asks for it leaves for Deep")
def t4():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    b_deep = seen(DEEP)["count"]
    a = owner.turn("Analyse ce contrat en profondeur /deep")
    assert "[deep] ok" in a["text"], a["text"][:200]
    d = seen(DEEP)
    assert d["count"] > b_deep and d["models"][-1] == "sokkan-deep" and d["auth"] == "sik_e2e_inst", d
    ship_mid = seen(SHIP)["count"]
    b = owner.turn("Et maintenant une question simple.")
    assert "[ship] ok" in b["text"], "the next request did not come back to the local model (sticky?)"
    assert seen(DEEP)["count"] == d["count"] or seen(SHIP)["count"] > ship_mid
    counts = owner.call("/api/appliance/inference")[1]["counts"]
    assert counts.get("deep:keyword", 0) >= 1, counts
    return {"deep_s": a["s"], "local_s": b["s"], "router_counts": counts}


@test("T5", "quota reached: 402 and 429 from the gateway read as sentences, in French")
def t5():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    got = {}
    try:
        for status, code, needle in ((402, "quota_exhausted", "Crédit d'inférence épuisé"),
                                     (429, "rate_limited", "Trop de requêtes")):
            mode(DEEP, status)
            t = owner.turn("Question difficile /deep")
            assert t["error"], f"{status}: no readable error event — events: {[e.get('type') for e in t['events']]}"
            assert t["error"]["code"] == code and needle in t["error"]["message"], t["error"]
            assert "Rien n'est perdu" in t["error"]["message"]
            raw = json.dumps(t["events"], ensure_ascii=False)
            assert "wallet empty" not in raw and "Traceback" not in raw, "upstream detail shown to the person"
            got[str(status)] = {"s": t["s"], "title": t["error"]["title"]}
    finally:
        mode(DEEP, 200)
    ok = owner.turn("Après recharge /deep")
    assert "[deep] ok" in ok["text"], "no recovery once the gateway answers again"
    return got


@test("T6", "local model down: answered from Deep, then a readable message when both are down")
def t6():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    stop_mock(8004)
    try:
        assert http(SHIP + "/health", timeout=2)[0] == 0, "the local model mock is still up"
        a = owner.turn("Question simple pendant la panne du modèle local.")
        assert "[deep] ok" in a["text"], f"no fallback to Deep: {a['text'][:200]} {a['error']}"
        mode(DEEP, 503)
        b = owner.turn("Question pendant que tout est en panne.")
        assert b["error"] and b["error"]["code"] in ("unreachable", "overloaded"), b
        assert "Rien n'est perdu" in b["error"]["message"]
        st, _ = owner.call("/api/health")
        assert st == 200, "the cockpit itself went down with the model"
        inf = owner.call("/api/appliance/inference")[1]
        assert inf["ship"] is False, inf
    finally:
        mode(DEEP, 200)
        start_mocks()
    c = owner.turn("Le modèle local est revenu ?")
    assert "[ship] ok" in c["text"], "no recovery without restarting SOKKAN"
    return {"fallback_s": a["s"], "both_down_message_s": b["s"], "code": b["error"]["code"], "recovered_s": c["s"]}


@test("T7", "network cut: work on the box goes on, a Deep request says why it cannot")
def t7():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    rules = ["INPUT -d 203.0.113.10 -j DROP", "OUTPUT ! -d 127.0.0.0/8 -m addrtype ! --dst-type LOCAL -o eth0 -j DROP",
             "FORWARD -o eth0 -j DROP"]
    for r in rules:
        sh(f"iptables -I {r}", check=True)
    try:
        assert sh("curl -s -m 4 -o /dev/null https://sokkan.ch").returncode != 0, "the machine still reaches the Internet"
        a = owner.turn("Travail local sans réseau.")
        assert "[ship] ok" in a["text"], f"local work stopped with the network: {a['text'][:200]} {a['error']}"
        b = owner.turn("Besoin du grand modèle /deep", timeout=200)
        assert b["error"] and b["error"]["code"] == "unreachable", b["error"] or b["text"][:200]
        assert "injoignable" in b["error"]["message"]
        assert owner.call("/api/sessions")[0] == 200 and owner.call("/api/memory/search?q=test&k=1")[0] == 200
        hb = owner.call("/api/appliance/heartbeat")[1]
        assert hb["state"]["enabled"] is True
    finally:
        for r in rules:
            sh(f"iptables -D {r}")
    c = owner.turn("Le réseau est revenu /deep")
    assert "[deep] ok" in c["text"], "Deep not reachable again after the network came back"
    return {"local_s": a["s"], "deep_refusal_s": b["s"], "after_s": c["s"]}


NOTES = {
    "fournisseur-farine": ("Le fournisseur de farine est le Moulin de Sévery ; il livre chaque mardi avant 7 h. "
                           "Contact : Mme Rochat. En cas de retard, prévenir l'atelier la veille."),
    "procedure-inscription": ("Procédure d'inscription des volontaires : formulaire signé, accord de participation, "
                              "création du compte par l'administrateur, remise du code d'accès en main propre."),
    "decision-budget-2027": ("Décision du comité du 3 novembre : le budget de formation 2027 est fixé à 42 000 CHF, "
                             "dont 12 000 CHF pour les ateliers d'intelligence artificielle."),
}


@test("T12", "memory with documents: notes are indexed and found, by the person and by the session")
def t12():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    mdir = "/data/claude/projects/-workspace/memory"
    for name, body in NOTES.items():
        text = f"---\nname: {name}\ndescription: >-\n  {body[:80]}\n---\n\n{body}\n"
        w = subprocess.run(
            ["docker", "compose", "exec", "-T", "-u", "1000", "api", "sh", "-c",
             f"mkdir -p {mdir} && cat > {mdir}/{name.replace('-', '_')}.md"], cwd=BOX, input=text, text=True,
            capture_output=True, timeout=60)
        assert w.returncode == 0, f"cannot write the note: {w.stderr}"
    queries = {"fournisseur-farine": "qui livre la farine et quel jour ?",
               "procedure-inscription": "comment inscrire un volontaire ?",
               "decision-budget-2027": "quel est le budget de formation 2027 ?"}
    t0 = time.time()
    found: dict = {}
    while time.time() - t0 < 420 and len(found) < 3:
        for name, q in queries.items():
            if name in found:
                continue
            st, r = owner.call("/api/memory/search?k=3&q=" + urllib.request.quote(q), timeout=60)
            if st == 200 and r and name in [x.get("note_name") for x in r[:3]]:
                found[name] = round(time.time() - t0)
        if len(found) < 3:
            time.sleep(5)
    assert len(found) == 3, f"notes found in the top 3: {found} (of 3) after {round(time.time() - t0)}s"
    lat = []
    for q in queries.values():
        t1_ = time.time()
        owner.call("/api/memory/search?k=3&q=" + urllib.request.quote(q), timeout=60)
        lat.append(round(1000 * (time.time() - t1_)))
    # the session gets the note without being asked (recall injected into what the model reads)
    owner.turn("Quel jour le Moulin livre-t-il la farine ?")
    hit = http(SHIP + "/_grep?needle=" + urllib.request.quote("Moulin de Sévery"))[1] or {}
    assert hit.get("found"), "the recalled note never reached the model"
    st, mig = owner.call("/api/memory/migration")
    return {"indexed_within_s": max(found.values()), "search_ms": lat, "recall_in_session": True,
            "store": (mig or {}).get("serving") if st == 200 else None, "profile": env_of().get("SOKKAN_MEMORY_PROFILE")}


@test("T11", "encrypted backup to S3, restored on a NEW machine with the client's key only")
def t11():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    assert owner.call("/api/board/card", {"title": "Carte témoin de la sauvegarde", "description": "x"})[0] == 200
    before = {"sessions": len(owner.call("/api/sessions")[1]),
              "audit": len(owner.call("/api/audit?limit=1000")[1])}
    Path("/etc/sokkan/backup.env").write_text(
        "RESTIC_REPOSITORY=s3:http://127.0.0.1:9000/sokkan-backups/pilot-e2e\n"
        "AWS_ACCESS_KEY_ID=e2e-access\nAWS_SECRET_ACCESS_KEY=e2e-secret-key\n")
    os.chmod("/etc/sokkan/backup.env", 0o600)
    r = sh("sh scripts/appliance/backup-offsite.sh init", cwd=BOX)
    assert r.returncode == 0 and "BACKUP KEY" in r.stdout, r.stderr[-400:]
    key = Path("/etc/sokkan/backup.key").read_text()
    assert len(key) >= 40 and oct(Path("/etc/sokkan/backup.key").stat().st_mode & 0o777) == "0o600"
    t0 = time.time()
    r = sh("sh scripts/appliance/backup-offsite.sh run", cwd=BOX, timeout=1800)
    bdur = time.time() - t0
    assert r.returncode == 0, f"backup failed: {r.stderr[-600:]}"
    st = json.loads(sh("sh scripts/appliance/backup-offsite.sh status", cwd=BOX).stdout)
    assert st["last_error"] == "" and st["snapshot"] and st["size_bytes"] > 0, st
    api = owner.call("/api/appliance/status")[1]["backup"]
    assert api.get("age_s") is not None and api["age_s"] < 300, f"the cockpit does not see the backup: {api}"
    # what is in the bucket is unreadable without the key (encrypted client-side)
    bad = sh("RESTIC_PASSWORD=wrong-key restic -r s3:http://127.0.0.1:9000/sokkan-backups/pilot-e2e snapshots",
             env={**ENV, "AWS_ACCESS_KEY_ID": "e2e-access", "AWS_SECRET_ACCESS_KEY": "e2e-secret-key"})
    assert bad.returncode != 0, "the repository opens with a wrong key"
    leak = sh("docker exec e2e-s3 sh -c \"grep -rl 'Moulin de S' /data 2>/dev/null | head -n 1\"").stdout.strip()
    assert not leak, f"client text readable in the bucket: {leak}"
    # ---- the machine is lost: volumes and install gone; only the bucket and the client's key remain
    client_key = key
    sh("docker compose down -v --remove-orphans -t 10", cwd=BOX, timeout=600, check=True)
    shutil.rmtree(BOX)
    shutil.rmtree("/var/backups/sokkan", ignore_errors=True)
    os.remove("/etc/sokkan/backup.key")
    r, idur = install_appliance()
    assert r.returncode == 0, f"reinstall failed: {(r.stderr or r.stdout)[-400:]}"
    host_side()
    export_ca()          # a new machine has a new certificate authority (it is not in the backup)
    new_owner = re.search(r"access code — shown once.*\n\s+([0-9a-f]{48})", r.stdout).group(1)
    assert Person(new_owner).login() == 200, "the owner cannot sign in on the new machine"
    old = Person(s["codes"][PEOPLE[0][0]]).login()
    assert old == 401, f"the new machine already knows the old accounts? (login → {old})"
    Path("/etc/sokkan/backup.key").write_text(client_key)
    os.chmod("/etc/sokkan/backup.key", 0o600)
    t0 = time.time()
    r = sh("sh scripts/appliance/backup-offsite.sh restore latest --yes", cwd=BOX, timeout=1800)
    assert r.returncode == 0, f"restore failed: {(r.stderr + r.stdout)[-800:]}"
    wait_health(600)
    rdur = time.time() - t0
    host_side()
    # the people sign in with the codes they already hold; their work is there
    for (e, n, p) in PEOPLE:
        assert Person(s["codes"][e], p).login() == 200, f"{e} cannot sign in after the restore"
    owner2 = Person(new_owner)       # the owner token is the NEW machine's (.env is not in the backup)
    assert owner2.login() == 200
    after = {"sessions": len(owner2.call("/api/sessions")[1]), "audit": len(owner2.call("/api/audit?limit=1000")[1])}
    assert after["sessions"] == before["sessions"], f"sessions {before['sessions']} → {after['sessions']}"
    assert after["audit"] >= before["audit"], after
    cards = owner2.call("/api/board")[1]
    assert "Carte témoin de la sauvegarde" in json.dumps(cards, ensure_ascii=False)
    t0 = time.time()
    hit = False
    while time.time() - t0 < 300 and not hit:
        st2, r2 = owner2.call("/api/memory/search?k=3&q=" + urllib.request.quote("qui livre la farine ?"), timeout=60)
        hit = st2 == 200 and bool(r2) and "fournisseur-farine" in [x.get("note_name") for x in r2[:3]]
        time.sleep(0 if hit else 5)
    assert hit, "the memory did not come back with the restore"
    state(owner=new_owner)
    return {"backup_s": round(bdur), "backup_bytes": st["size_bytes"], "reinstall_s": round(idur),
            "restore_s": round(rdur), "sessions": after["sessions"], "audit_events": after["audit"]}


@test("T14", "every line of the appliance .env has an effect one can observe")
def t14():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    e = env_of()
    feats = owner.call("/api/features")[1]
    reg = {f["id"]: f for f in feats["registry"]["items"]}
    inst = owner.call("/api/instance")[1]
    ver = http(BASE + "/api/version")[1]
    ls = listeners()
    psv = sh("docker compose ps --format '{{.Service}}'", cwd=BOX).stdout.split()
    hb = owner.call("/api/appliance/heartbeat")[1]
    # the first heartbeat leaves 60 s after the api started: a fast run gets here before it does
    posts: dict = {}
    for _ in range(45):
        posts = seen(DEEP).get("posts") or {}
        if posts.get("/hb"):
            break
        time.sleep(2)
    cert = sh(f"echo | openssl s_client -connect 127.0.0.1:443 -servername {HOSTNAME} 2>/dev/null | "
              "openssl x509 -noout -ext subjectAltName 2>/dev/null").stdout
    cookie = sh(f"curl -s -D - -o /dev/null --cacert /work/ca.crt --resolve {HOSTNAME}:443:127.0.0.1 "
                f"-H 'content-type: application/json' -d '{{\"token\":\"{s['owner']}\"}}' https://{HOSTNAME}/api/auth/local").stdout
    # from ANOTHER container, over the compose network (inside its own container Postgres trusts
    # the loopback, whatever the password)
    def pg(password: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "exec", "-T", "-e", f"PGPW={password}", "api", "python3", "-c",
             "import os, psycopg; c = psycopg.connect(host='db', user='sokkan', dbname='sokkan', "
             "password=os.environ['PGPW'], connect_timeout=5); print(c.execute('select 1').fetchone()[0])"],
            cwd=BOX, capture_output=True, text=True, timeout=60)
    pg_ok, pg_bad = pg(e["SOKKAN_DB_PASSWORD"]), pg("not-the-password")
    sup = owner.call("/api/appliance/support", {"action": "open", "hours": 99})
    images = sh("docker compose images", cwd=BOX).stdout
    # « Export our work »: the zip for the instance's admin, a refusal for a person who is not one
    someone = Person(s["codes"][PEOPLE[0][0]], PEOPLE[0][2])
    someone.login()
    exp, exp_other = work_export_of(owner), work_export_of(someone)

    def feat_off(fid: str, var: str) -> bool:
        return reg[fid]["enabled"] is False and var in reg[fid]["reason"]

    checks = {
        "SOKKAN_VERSION": ver["dist"] == e["SOKKAN_VERSION"],
        "SOKKAN_COMMIT": ver["commit"] == e["SOKKAN_COMMIT"],
        "SOKKAN_IMAGES": "sokkan-api" in images and "sokkan-web" in images,
        "SOKKAN_IMAGE_TAG": e["SOKKAN_IMAGE_TAG"] in images and e["SOKKAN_IMAGE_TAG"] == NEW.replace("+", "-"),
        "SOKKAN_REQUIRE_SIGNATURE": True,        # proven by T1 (unsigned release refused) — re-read below
        "COMPOSE_FILE": "docker/compose.images.yml" in e["COMPOSE_FILE"] and "sokkan-web" in images,
        "COMPOSE_PATH_SEPARATOR": sh("docker compose config -q", cwd=BOX).returncode == 0,
        "COMPOSE_PROFILES": "router" in psv and "tls" in psv,
        "SOKKAN_ORG_NAME": inst["org_name"] == e["SOKKAN_ORG_NAME"],
        "SOKKAN_OWNER_EMAIL": owner.call("/api/me")[1]["email"] == e["SOKKAN_OWNER_EMAIL"],
        "SOKKAN_OWNER_NAME": owner.call("/api/me")[1]["name"] == e["SOKKAN_OWNER_NAME"],
        "SOKKAN_LOCAL_TOKEN": Person(e["SOKKAN_LOCAL_TOKEN"]).login() == 200 and Person(e["SOKKAN_LOCAL_TOKEN"][:-1] + "x").login() == 401,
        "SOKKAN_AUTH_MODE": http(BASE + "/api/auth/info")[1] == {"mode": "local", "login_required": True},
        "SOKKAN_DEFAULT_ROLE": owner.call("/api/iam/access-codes")[1][0]["role"] == "none",
        "SOKKAN_FEATURE_MULTI_PROJECT": feats["multi_project"] is True and "SOKKAN_FEATURE_MULTI_PROJECT" in reg["multi_project"]["reason"],
        # reaches the api and is a zone it can load (what dates in Costs and the agents' schedules use)
        "SOKKAN_TZ": sh("docker compose exec -T api python3 -c \"import os, zoneinfo; "
                        "print(zoneinfo.ZoneInfo(os.environ['SOKKAN_TZ']).key)\"", cwd=BOX).stdout.strip() == e["SOKKAN_TZ"],
        "SOKKAN_ERROR_LANG": True,               # proven by T5/T6/T7 (messages in French)
        "SOKKAN_BIND": "127.0.0.1:3009" in ls and "0.0.0.0:3009" not in ls,
        "SOKKAN_PORT": any(x.endswith(":" + e["SOKKAN_PORT"]) for x in ls),
        "SOKKAN_TLS_SITES": HOSTNAME in cert,
        "SOKKAN_PUBLIC_URL": "secure" in cookie.lower() and "sokkan_session=" in cookie,
        "SOKKAN_SHIP_URL": seen(SHIP)["count"] > 0,
        "SOKKAN_SHIP_MODEL": "qwen3.8-27b" in seen(SHIP)["models"],
        "SOKKAN_DEEP_URL": seen(DEEP)["count"] > 0,
        "SOKKAN_DEEP_TOKEN": seen(DEEP)["auth"] == "sik_e2e_inst",
        "SOKKAN_DEEP_MODEL": "sokkan-deep" in seen(DEEP)["models"],
        "SOKKAN_FEATURE_ASSISTANT": feats.get("assistant") is True,
        "SOKKAN_ASSISTANT_LLM_URL": feats.get("assistant") is True,
        "SOKKAN_ASSISTANT_LLM_MODEL": feats.get("assistant") is True,
        "SOKKAN_ASSISTANT_LLM_API": feats.get("assistant") is True,
        "SOKKAN_ASSISTANT_LLM_TOKEN": feats.get("assistant") is True,
        # 3.6 pilot: what the people of the site get — each switch named by its own variable
        "CLAUDE_CODE_EFFORT_LEVEL": sh("docker compose exec -T -w /app/backend api python3 -c "
                                       "\"import llm; print(llm.session_env('x@pilot.example')['CLAUDE_CODE_EFFORT_LEVEL'])\"",
                                       cwd=BOX).stdout.strip() == e["CLAUDE_CODE_EFFORT_LEVEL"] == "medium",
        "SOKKAN_FEATURE_DOCUMENTS": feats["documents"] is True and "SOKKAN_FEATURE_DOCUMENTS" in reg["documents"]["reason"],
        "SOKKAN_FEATURE_SIMPLE_PROFILE": feats["simple_profile"] is True and not reg["simple_profile"]["problem"],
        "SOKKAN_PROFILE_DEFAULT": owner.call("/api/me/prefs")[1]["profile"] == e["SOKKAN_PROFILE_DEFAULT"] == "simple",
        "SOKKAN_FEATURE_STUDY_MODE": reg["study_mode"]["enabled"] is True and owner.call("/api/study/me")[1]["enabled"] is True,
        "SOKKAN_FEATURE_WORK_EXPORT": reg["work_export"]["enabled"] is True
                                      and "SOKKAN_FEATURE_WORK_EXPORT" in reg["work_export"]["reason"]
                                      and exp[0] == 200 and {"README.md", "manifest.json"} <= set(exp[1])
                                      and exp_other[0] == 403,
        "SOKKAN_FEATURE_DOC_INGEST": reg["doc_ingest"]["enabled"] is True and not reg["doc_ingest"]["problem"]
                                     and owner.call("/api/doc-ingest/status")[1]["enabled"] is True,
        "SOKKAN_DOC_INGEST_LLM_URL": owner.call("/api/doc-ingest/status")[1]["model"]["configured"] is True
                                     and not owner.call("/api/doc-ingest/status")[1]["model"]["not_ready"],
        "SOKKAN_DOC_INGEST_LLM_MODEL": owner.call("/api/doc-ingest/status")[1]["model"]["model"] == e["SOKKAN_DOC_INGEST_LLM_MODEL"],
        "SOKKAN_DOC_INGEST_CONCURRENCY": sh("docker compose exec -T -w /app/backend api python3 -c "
                                            "\"from docingest import config; print(config.Limits.from_env().concurrency)\"",
                                            cwd=BOX).stdout.strip() == e["SOKKAN_DOC_INGEST_CONCURRENCY"],
        "SOKKAN_DB_PASSWORD": pg_ok.stdout.strip() == "1",
        "CORTHEXIS_MEMORY_BACKEND": (owner.call("/api/memory/migration")[1] or {}).get("serving") in ("store", "postgres", None),
        "SOKKAN_FEATURE_TMUX": feat_off("tmux", "SOKKAN_FEATURE_TMUX=0"),
        "SOKKAN_FEATURE_PREVIEW": feat_off("preview", "SOKKAN_FEATURE_PREVIEW=0"),
        "SOKKAN_FEATURE_MAGNITUDE": feat_off("magnitude", "SOKKAN_FEATURE_MAGNITUDE=0"),
        "SOKKAN_FEATURE_MISSIONS_LINK": feat_off("missions_link", "SOKKAN_FEATURE_MISSIONS_LINK=0"),
        "SOKKAN_FEATURE_ALERTING": feat_off("alerting", "SOKKAN_FEATURE_ALERTING=0"),
        "SOKKAN_FEATURE_AGENT_INCIDENTS": feat_off("agent_incidents", "SOKKAN_FEATURE_AGENT_INCIDENTS=0"),
        "SOKKAN_UPDATE_CHECK": inst["update"]["check_enabled"] is False,
        "SOKKAN_HEARTBEAT_URL": hb["state"]["enabled"] is True and posts.get("/hb", 0) >= 1,
        "SOKKAN_HEARTBEAT_TOKEN": seen(DEEP).get("hb_auth") == "Bearer hb-e2e",
        "SOKKAN_HEARTBEAT_INTERVAL_S": hb["state"]["interval_s"] == int(e["SOKKAN_HEARTBEAT_INTERVAL_S"]),
        "SOKKAN_INSTANCE_ID": hb["payload"]["instance"] == e["SOKKAN_INSTANCE_ID"],
        "SOKKAN_SUPPORT_MAX_HOURS": sup[0] == 400,
    }
    if e.get("SOKKAN_MEMORY_PROFILE"):
        checks["SOKKAN_MEMORY_PROFILE"] = "corthexis-embed" in " ".join(psv) or e["SOKKAN_MEMORY_PROFILE"] == "leger"
    if e.get("SOKKAN_EMBED_ACCEL"):
        checks["SOKKAN_EMBED_ACCEL"] = (e["SOKKAN_EMBED_ACCEL"] == "cpu") == ("compose.sycl" not in e.get("COMPOSE_FILE", "")
                                                                             and "compose.cuda" not in e.get("COMPOSE_FILE", ""))
    assert pg_bad.returncode != 0, "Postgres accepts a wrong password from another container"
    # heartbeat body = the documented fields, and nothing about the people
    body = seen(DEEP).get("hb_body") or {}
    assert set(body) == set(hb["state"]["fields"]), f"heartbeat sent {sorted(body)}"
    assert "pilot.example" not in json.dumps(body) and "Moulin" not in json.dumps(body)
    # two switches proven by flipping them (router only, 5 s each)
    flips = {}
    for var, prompt, expect_before, expect_after in (
            ("SOKKAN_ESCALATION", "Encore une question /deep", "[deep] ok", "[ship] ok"),):
        assert expect_before in owner.turn(prompt)["text"]
        set_env(var, "0")
        sh("docker compose up -d --no-build router", cwd=BOX, check=True)
        time.sleep(6)
        flips[var] = expect_after in owner.turn(prompt)["text"]
        set_env(var, "1")
    set_env("SOKKAN_SHIP_FALLBACK_DEEP", "0")
    sh("docker compose up -d --no-build router", cwd=BOX, check=True)
    time.sleep(6)
    stop_mock(8004)
    try:
        t = owner.turn("Modèle local arrêté, sans repli.")
        flips["SOKKAN_SHIP_FALLBACK_DEEP"] = bool(t["error"]) and t["error"]["code"] == "unreachable" and "[deep] ok" not in t["text"]
    finally:
        start_mocks()
        set_env("SOKKAN_SHIP_FALLBACK_DEEP", "1")
        sh("docker compose up -d --no-build router", cwd=BOX, check=True)
        time.sleep(6)
    checks.update(flips)
    template = [x.split("=", 1)[0] for x in (BOX / "deploy/appliance/env.appliance.example").read_text().splitlines()
                if "=" in x and not x.startswith("#")]
    not_covered = [k for k in template if k not in checks and k in e]
    failed = sorted(k for k, v in checks.items() if not v)
    assert not failed, f"no observable effect for: {failed}"
    assert not not_covered, f"variables of the template without a check: {not_covered}"
    return {"variables_checked": len(checks), "flipped": sorted(flips), "template_lines": len(template),
            "work_export": {"admin": exp[0], "files_in_zip": len(exp[1]), "not_admin": exp_other[0]}}


@test("T2b", "support tunnel: opened by the client for a bounded time, journaled, closes alone")
def t_support():
    s = state()
    owner = Person(s["owner"])
    owner.login()
    flag = WORK / "tunnel.up"
    env = {**ENV, "SOKKAN_TUNNEL_UP_CMD": f"touch {flag}", "SOKKAN_TUNNEL_DOWN_CMD": f"rm -f {flag}",
           "SOKKAN_TUNNEL_PROBE_CMD": f"test -f {flag}"}
    env.pop("SOKKAN_VERIFY_NO_ENVFILE", None)
    tool = "sh scripts/appliance/support-tunnel.sh"
    sh(f"{tool} close --by system:install", cwd=BOX, env=env)
    st = owner.call("/api/appliance/status")[1]["support"]
    assert st["open"] is False and st["host_agent"] is True, st
    # a person who is not an administrator cannot ask for it
    ana = Person(s["codes"][PEOPLE[0][0]], PEOPLE[0][2])
    ana.login()
    assert ana.call("/api/appliance/support", {"action": "open", "hours": 1})[0] == 403
    assert owner.call("/api/appliance/support", {"action": "open", "hours": 2})[0] == 200
    assert sh(f"{tool} apply", cwd=BOX, env=env).returncode == 0      # what the systemd path unit runs
    assert flag.exists(), "the tunnel was not brought up"
    st = owner.call("/api/appliance/status")[1]["support"]
    assert st["open"] is True and 7000 < st["remaining_s"] <= 7200, st
    # the deadline passes (clock moved 2 h 1 min ahead): the timer's command closes it
    later = int(time.time()) + 7260
    assert sh(f"SOKKAN_SUPPORT_NOW={later} {tool} expire", cwd=BOX, env=env).returncode == 0
    assert not flag.exists(), "the tunnel is still up after its deadline"
    time.sleep(1)
    owner.call("/api/appliance/status")
    acts = [(e["action"], e["user"]) for e in owner.call("/api/audit?limit=200")[1] if e["action"].startswith("support.tunnel")]
    names = [a for a, _ in acts]
    for a in ("support.tunnel.request", "support.tunnel.opened", "support.tunnel.expired"):
        assert a in names, f"{a} missing from the journal: {names}"
    assert ("support.tunnel.opened", "admin@pilot.example") in acts, acts
    return {"journal": sorted(set(names)), "note": "tunnel command = a stub (no Tailscale on the test machine)"}


def appliance() -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    serve_dist()
    start_mocks()
    if not t1():
        return
    # the first heartbeat leaves 60 s after the api started: T14 checks it arrived
    t2()
    t_support()
    t3()
    t4()
    t5()
    t6()
    t7()
    t12()
    t14()
    t11()
    seed_thread()
    state(phase="appliance-done")


@test("T13", "reboot: everything comes back alone, nobody types a command")
def t13():
    s = state()
    t_down = float(os.environ.get("E2E_REBOOT_T0") or time.time())
    up = wait_health(900, NEW)                      # NO `compose up` here: restart policies only
    total = time.time() - t_down
    serve_dist()
    start_mocks()                                   # on a real box: sokkan-ship.service (systemd)
    owner = Person(s["owner"])
    assert owner.login() == 200
    for (e, n, p) in PEOPLE:
        assert Person(s["codes"][e], p).login() == 200, e
    c = sh(f"curl -s -o /dev/null -w '%{{http_code}}' --cacert /work/ca.crt --resolve {HOSTNAME}:443:127.0.0.1 "
           f"https://{HOSTNAME}/api/health").stdout.strip()
    assert c == "200", f"https after reboot → {c} (the CA must be the same as before)"
    running = set(sh("docker compose ps --status running --format '{{.Service}}'", cwd=BOX).stdout.split())
    assert {"api", "web", "db", "router", "tls"} <= running, running
    sh("docker compose cp /harness/e2e_appliance/ws_session.py api:/tmp/ws_session.py", cwd=BOX)
    t = owner.turn("Après le redémarrage, tout va bien ?")
    assert "[ship] ok" in t["text"], t["text"][:200]
    # the conversation of a work started before the reboot is still there for its author (3.6.3:
    # it opened empty — the deliverables were kept, the thread was not read back)
    th = s.get("thread") or {}
    assert th.get("sid"), f"no work was started before the reboot: {th.get('error')}"
    author = Person(s["codes"][th["email"]], th["project"])
    assert author.login() == 200
    mine, agent = thread_of(author, th["sid"])
    assert THREAD_ASK in mine and any("[ship] ok" in x for x in agent), \
        f"the thread of a work is not readable after the reboot: {mine} {agent}"
    st2, r2 = owner.call("/api/memory/search?k=3&q=" + urllib.request.quote("qui livre la farine ?"), timeout=90)
    assert st2 == 200 and r2 and "fournisseur-farine" in [x.get("note_name") for x in r2[:3]], "memory after reboot"
    return {"healthy_after_engine_start_s": round(up), "reboot_to_healthy_s": round(total),
            "thread_after_reboot": {"own_messages": len(mine), "agent_sentences": len(agent)},
            "note": "engine restart of the test machine; the systemd units (model, tunnel closed at boot, "
                    "backup timer) need a real machine — not played here"}


def after_reboot() -> None:
    t13()


# ---------------------------------------------------------------- phase B: upgrade, both entries, rollback
def downtime_probe(stop: threading.Event, out: list) -> None:
    while not stop.is_set():
        st, _ = http(BASE + "/api/health", timeout=3)
        out.append((time.time(), st))
        time.sleep(1)


def longest_outage(samples: list) -> float:
    worst, start = 0.0, None
    for t, st in samples:
        if st != 200 and start is None:
            start = t
        if st == 200 and start is not None:
            worst, start = max(worst, t - start), None
    if start is not None and samples:
        worst = max(worst, samples[-1][0] - start)
    return round(worst, 1)


def old_install(d: Path, ver: str) -> str:
    """A 3.4.6 install as a client made it: the installer of that time, a build on the machine."""
    shutil.rmtree(d, ignore_errors=True)
    srv = WORK / "srv" / "dist"
    (srv / "VERSION").write_text(ver + "\n")
    shutil.copy(srv / f"sokkan-{ver}.tar.gz", srv / "sokkan-latest.tar.gz")
    r = sh(f"sh {DISTS}/install-old.sh", cwd=d.parent, env={**ENV, "SOKKAN_DIR": d.name, "SOKKAN_ACCEPT_GEMMA_TERMS": "0"},
           timeout=1800)
    assert r.returncode == 0, f"old installer: {(r.stderr or r.stdout)[-400:]}"
    tok = env_of(d)["SOKKAN_LOCAL_TOKEN"]
    set_env("ANTHROPIC_API_KEY", "sk-ant-e2e-not-a-real-key", d)
    # never a GPU on the bench (see ANSWERS): back to the CPU profile BEFORE anything starts
    sh("sh scripts/memory-setup.sh --non-interactive --decline --profile leger", cwd=d, check=True)
    no_gpu(d)
    r = sh("docker compose up -d --build", cwd=d, timeout=5400)
    assert r.returncode == 0, f"build of {ver}: {r.stderr[-600:]}"
    wait_health(900)
    (srv / "VERSION").write_text(NEW + "\n")
    shutil.copy(srv / f"sokkan-{NEW}.tar.gz", srv / "sokkan-latest.tar.gz")
    return tok


def seed_old(tok: str) -> dict:
    u = Person(tok, base=BASE)
    assert u.login() == 200
    assert u.call("/api/board/card", {"title": "Carte posée en 3.4.6", "description": "x"})[0] == 200
    return {"cards": json.dumps(u.call("/api/board")[1], ensure_ascii=False).count("Carte posée en 3.4.6"),
            "audit": len(u.call("/api/audit?limit=1000")[1] or [])}


def check_kept(tok: str, before: dict) -> None:
    u = Person(tok, base=BASE)
    assert u.login() == 200, "the access token no longer works after the upgrade"
    assert "Carte posée en 3.4.6" in json.dumps(u.call("/api/board")[1], ensure_ascii=False), "board card lost"
    assert len(u.call("/api/audit?limit=1000")[1] or []) >= before["audit"], "journal shorter after the upgrade"


def newer_only() -> list:
    """The files NEW carries and OLD does not, by comparing the two tarballs (from 3.4.6 or 3.5.1
    scripts/verify-release.sh is one of them; from 3.6.0 it is not, so one name cannot be the
    check). After a rollback none may remain: neither inside a release folder nor at the top
    level (docker-compose.doc-ingest.yml stayed there until 3.6.2)."""
    def names(ver: str) -> set:
        with tarfile.open(WORK / "srv" / "dist" / f"sokkan-{ver}.tar.gz") as t:
            return {m.name.split("/", 1)[1] for m in t.getmembers() if m.isfile() and "/" in m.name}
    return sorted(names(NEW) - names(OLD))


def tree_of(d: Path) -> dict:
    skip = {".env", ".sokkan-previous", "workspace", "node_modules", ".next"}
    out = {}
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if p.is_file() and not (set(rel.parts) & skip) and "__pycache__" not in rel.parts:
            out[str(rel)] = p.stat().st_size
    return out


def upgrade() -> None:
    serve_dist()
    if (BOX / "docker-compose.yml").exists():       # the appliance of phase A leaves the machine
        sh("docker compose down -v --remove-orphans -t 10", cwd=BOX, timeout=600)
        shutil.rmtree(BOX, ignore_errors=True)
    d = Path("/home/user/sokkan")
    d.parent.mkdir(parents=True, exist_ok=True)
    ctx: dict = {}

    @test("T8", f"in-place upgrade {OLD} → {NEW} by the installer (entry A)")
    def t8():
        t0 = time.time()
        tok = old_install(d, OLD)
        ctx["tok"], ctx["before"] = tok, seed_old(tok)
        ctx["old_install_s"] = round(time.time() - t0)
        assert (http(BASE + "/api/version")[1] or {}).get("version") == OLD.split("+")[0]
        stop, samples = threading.Event(), []
        th = threading.Thread(target=downtime_probe, args=(stop, samples))
        th.start()
        t0 = time.time()
        r = sh(f"sh {WORK}/srv/install.sh --dir {d}", cwd=d.parent, env={**ENV, "SOKKAN_ACCEPT_GEMMA_TERMS": "0"}, timeout=5400)
        dur = time.time() - t0
        time.sleep(3)
        stop.set()
        th.join()
        (WORK / "upgrade-A.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr)
        assert r.returncode == 0, f"upgrade rc={r.returncode}: {(r.stderr or r.stdout)[-500:]}"
        v = http(BASE + "/api/version")[1]
        assert v["dist"] == NEW, f"after the upgrade /api/version says {v}"
        check_kept(tok, ctx["before"])
        assert (d / ".sokkan-previous").read_text().strip() == OLD
        assert env_of(d)["SOKKAN_COMMIT"] == NEW.split("+")[1]
        ctx["tree_A"] = tree_of(d)
        return {"upgrade_s": round(dur), "interruption_s": longest_outage(samples),
                "ram_after_upgrade_mb": mem_now_mb(), "source_install_s": ctx["old_install_s"]}

    @test("T10", f"rollback {NEW} → {OLD}, then forward again: one command each way")
    def t10():
        stop, samples = threading.Event(), []
        th = threading.Thread(target=downtime_probe, args=(stop, samples))
        th.start()
        t0 = time.time()
        r = sh("sh scripts/rollback.sh", cwd=d, timeout=5400)
        dur = time.time() - t0
        time.sleep(3)
        stop.set()
        th.join()
        (WORK / "rollback.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr)
        assert r.returncode == 0, f"rollback rc={r.returncode}: {(r.stderr or r.stdout)[-500:]}"
        v = http(BASE + "/api/version")[1] or {}
        assert v.get("version") == OLD.split("+")[0], f"after rollback: {v}"
        left = [f for f in newer_only() if (d / f).exists()]
        assert not left, f"files of the newer release were left behind: {left[:8]}"
        check_kept(ctx["tok"], ctx["before"])
        assert (d / ".sokkan-previous").read_text().strip() == NEW
        return {"rollback_s": round(dur), "interruption_s": longest_outage(samples),
                "newer_only_files_checked": len(newer_only()), "left_at_top_level": left}

    @test("T9", f"in-place upgrade {OLD} → {NEW} by scripts/upgrade.sh (entry B) — same result as entry A")
    def t9():
        # the tree is back on 3.4.6 (T10): its own upgrade.sh, as an operator would type it
        assert (http(BASE + "/api/version")[1] or {}).get("version") == OLD.split("+")[0], f"T10 did not leave a {OLD}"
        stop, samples = threading.Event(), []
        th = threading.Thread(target=downtime_probe, args=(stop, samples))
        th.start()
        t0 = time.time()
        r = sh(f"sh scripts/upgrade.sh '{NEW}'", cwd=d, timeout=5400)
        dur = time.time() - t0
        try:
            wait_health(600, NEW)
        finally:
            stop.set()
            th.join()
        (WORK / "upgrade-B.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr)
        assert r.returncode == 0, f"upgrade rc={r.returncode}: {(r.stderr or r.stdout)[-500:]}"
        v = http(BASE + "/api/version")[1]
        assert v["version"] == NEW.split("+")[0] and v["dist"] == NEW, v
        check_kept(ctx["tok"], ctx["before"])
        a, b = ctx.get("tree_A") or {}, tree_of(d)
        diff = sorted(set(a) ^ set(b)) + sorted(k for k in set(a) & set(b) if a[k] != b[k])
        assert a and not diff, f"entry A and entry B leave different trees: {diff[:8]}"
        return {"upgrade_s": round(dur), "interruption_s": longest_outage(samples), "files": len(b),
                "dist": v["dist"], "note": f"a {OLD.split('+')[0]} tree runs ITS upgrade.sh (no verification, no health gate); "
                "from 3.6 on both entries are the same script"}

    if t8():
        if t10():
            t9()
    else:
        record("T10", "rollback", None, detail="not played: T8 failed")
        record("T9", "upgrade by scripts/upgrade.sh", None, detail="not played: T8 failed")
    sh("docker compose down -v --remove-orphans -t 10", cwd=d, timeout=600)


def report() -> None:
    rows = [json.loads(x) for x in RESULTS.read_text().splitlines()] if RESULTS.exists() else []
    last: dict = {}
    for r in rows:
        last[r["id"]] = r
    print(f"\n{'id':5} {'':10} name / measures")
    for k in sorted(last, key=lambda x: (len(x), x)):
        r = last[k]
        mark = {True: "PASS", False: "FAIL", None: "NOT PLAYED"}[r["pass"]]
        print(f"{k:5} {mark:10} {r['name']}\n{'':16} {json.dumps(r['measures'], ensure_ascii=False)}"
              + (f"\n{'':16} {r['detail'][:400]}" if r["detail"] else ""))
    (WORK / "results.json").write_text(json.dumps(list(last.values()), indent=1, ensure_ascii=False))


def only() -> None:
    """suite.py only T3,T4 — replay chosen tests on the machine as it is (debugging)."""
    serve_dist()
    start_mocks()
    table = {"T1": t1, "T2": t2, "T2b": t_support, "T3": t3, "T4": t4, "T5": t5, "T6": t6, "T7": t7,
             "T11": t11, "T12": t12, "T13": t13, "T14": t14}
    for tid in (sys.argv[2] if len(sys.argv) > 2 else "").split(","):
        table[tid.strip()]()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"prepare": prepare, "appliance": appliance, "after-reboot": after_reboot, "upgrade": upgrade,
     "report": report, "only": only}.get(cmd, lambda: (print(__doc__), sys.exit(2)))()
