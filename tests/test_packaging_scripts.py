"""install.sh / upgrade.sh / rollback.sh / verify-release.sh — the packaging chain of 3.6,
without Docker (a logging stub) and without network (a `file://` dist folder).

D9  one upgrade path: install.sh on an existing install runs the release's own upgrade.sh
D10 a PINNED release (`--version <ver+hash>`), never « latest »
D11 the commit of the release reaches .env (image build arg, /api/version)
and: a release that fails verification changes NOTHING (fail closed when signatures are
required), a failed upgrade names the rollback."""
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "scripts"
if not all(shutil.which(x) for x in ("tar", "sh", "curl")):
    pytest.skip("tar, sh and curl are required", allow_module_level=True)

V_OLD, V_NEW = "3.4.6+3ea74f5", "3.6.0+abc1234"


def _release(ver: str, extra: dict | None = None) -> dict:
    semver = ver.split("+")[0]
    files = {
        "docker-compose.yml": f"services: {{}}  # {semver}\n", "VERSION": semver + "\n",
        ".env.example": "SOKKAN_LOCAL_TOKEN=\nSOKKAN_DB_PASSWORD=\nSOKKAN_PORT=3009\n",
        "backend/app.py": f"# {semver}\n", f"backend/only_{semver.replace('.', '_')}.py": "x\n",
        "frontend/package.json": "{}\n", "docs/UPGRADE.md": "doc\n",
    }
    if semver >= "3.6":
        for f in ("upgrade.sh", "rollback.sh", "verify-release.sh", "install.sh"):
            files[f"scripts/{f}"] = (S / f).read_text()
        files["deploy/appliance/env.appliance.example"] = \
            (ROOT / "deploy/appliance/env.appliance.example").read_text()
        files["docker/compose.images.yml"] = (ROOT / "docker/compose.images.yml").read_text()
    else:   # what 3.4.6 shipped: an upgrade.sh without verification nor health check
        files["scripts/upgrade.sh"] = "#!/bin/sh\necho OLD-UPGRADE-SH; exit 9\n"
    files.update(extra or {})
    return files


def _publish(dist: Path, ver: str, files: dict, sha=True, sig: str | None = None, latest=True) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    tb = dist / f"sokkan-{ver}.tar.gz"
    with tarfile.open(tb, "w:gz") as t:
        for rel, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(f"sokkan/{rel}")
            ti.size, ti.mode = len(data), 0o755 if rel.endswith(".sh") else 0o644
            t.addfile(ti, io.BytesIO(data))
    if sha:
        (dist / (tb.name + ".sha256")).write_text(hashlib.sha256(tb.read_bytes()).hexdigest() + "\n")
    if sig is not None:
        (dist / (tb.name + ".sig")).write_text(sig)
    if latest:
        (dist / "VERSION").write_text(ver + "\n")
        shutil.copy(tb, dist / "sokkan-latest.tar.gz")
    return tb


@pytest.fixture()
def world(tmp_path):
    """A dist folder, a stub docker that logs, a stub cosign that accepts the signature "GOOD"."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/docker.log\n'
                                 'case "$1 $2" in "compose version"|"info ") exit 0;; esac\nexit 0\n')
    (bin_ / "cosign").write_text(
        '#!/bin/sh\n# stub: the signature file must hold GOOD, the key must exist\n'
        'while [ $# -gt 0 ]; do case "$1" in --signature) s="$2"; shift;; --key) k="$2"; shift;; esac; shift; done\n'
        '[ -r "$k" ] && [ "$(cat "$s")" = GOOD ]\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    pub = tmp_path / "cosign.pub"
    pub.write_text("PUBLIC KEY\n")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "SOKKAN_BASE": f"file://{tmp_path}", "SOKKAN_UPGRADE_NO_WAIT": "1",
           "SOKKAN_ROLLBACK_NO_WAIT": "1", "SOKKAN_SKIP_HOST_SETUP": "1",
           "SOKKAN_ACCEPT_GEMMA_TERMS": "0", "SOKKAN_COSIGN_PUB": str(pub),
           "SOKKAN_VERIFY_NO_ENVFILE": "1", "TMPDIR": str(tmp_path)}
    env.pop("SOKKAN_REQUIRE_SIGNATURE", None)
    return tmp_path, env


def _sh(script, *args, cwd, env, stdin=subprocess.DEVNULL):
    return subprocess.run(["sh", str(script), *args], cwd=cwd, env=env, capture_output=True,
                          text=True, stdin=stdin, timeout=120)


def _env(d: Path) -> dict:
    return dict(x.split("=", 1) for x in (d / ".env").read_text().splitlines()
                if "=" in x and not x.startswith("#"))


def _tree(d: Path) -> dict:
    skip = (".env", ".sokkan-previous")
    return {str(p.relative_to(d)): p.read_text() for p in sorted(d.rglob("*"))
            if p.is_file() and p.name not in skip}


def _installed(tmp: Path, env: dict, ver: str, name="sokkan") -> Path:
    """An existing install of `ver`, as the installer of that time left it."""
    d = tmp / name
    with tarfile.open(tmp / "dist" / f"sokkan-{ver}.tar.gz") as t:
        t.extractall(tmp / f"x-{name}")
    shutil.move(str(tmp / f"x-{name}" / "sokkan"), d)
    (d / ".env").write_text(f"SOKKAN_LOCAL_TOKEN=keepme\nSOKKAN_VERSION={ver}\nSOKKAN_MEMORY_PROFILE=leger\n")
    (d / "workspace").mkdir()
    (d / "workspace" / "mine.txt").write_text("mine")
    return d


# ---------------------------------------------------------------- D10: a pinned release

def test_install_pins_the_release_and_records_version_and_commit(world):
    tmp, env = world
    _publish(tmp / "dist", V_OLD, _release(V_OLD), latest=False)
    _publish(tmp / "dist", V_NEW, _release(V_NEW))                   # the « latest »
    r = _sh(S / "install.sh", "--version", V_OLD, "--dir", "pinned", cwd=tmp, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    e = _env(tmp / "pinned")
    assert e["SOKKAN_VERSION"] == V_OLD and e["SOKKAN_COMMIT"] == "3ea74f5"       # D11
    assert "# 3.4.6" in (tmp / "pinned" / "docker-compose.yml").read_text()
    assert len(e["SOKKAN_LOCAL_TOKEN"]) == 48 and e["SOKKAN_DB_PASSWORD"]
    assert oct((tmp / "pinned" / ".env").stat().st_mode & 0o777) == "0o600"


def test_install_without_version_resolves_latest_to_its_exact_name(world):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW))
    r = _sh(S / "install.sh", "--dir", "cur", cwd=tmp, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert _env(tmp / "cur")["SOKKAN_VERSION"] == V_NEW and "latest" not in (tmp / "cur" / ".env").read_text()


def test_install_of_an_unknown_release_changes_nothing(world):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW))
    r = _sh(S / "install.sh", "--version", "9.9.9+deadbee", "--dir", "nope", cwd=tmp, env=env)
    assert r.returncode != 0 and "not found" in r.stderr and not (tmp / "nope").exists()


# ---------------------------------------------------------------- D9: one upgrade path

def test_both_upgrade_entries_give_the_same_install(world):
    """`install.sh` on an existing 3.4.6 and `upgrade.sh <version>` from a 3.6 tree run the
    same script: same files, same .env keys, same compose command."""
    tmp, env = world
    _publish(tmp / "dist", V_OLD, _release(V_OLD), latest=False)
    _publish(tmp / "dist", V_NEW, _release(V_NEW))
    a = _installed(tmp, env, V_OLD, "via-installer")
    b = _installed(tmp, env, V_OLD, "via-upgrade")
    ra = _sh(S / "install.sh", "--dir", "via-installer", cwd=tmp, env=env)
    assert ra.returncode == 0, ra.stderr + ra.stdout
    assert "OLD-UPGRADE-SH" not in ra.stdout            # not the script of the installed 3.4.6
    # path B: the operator copies nothing — the installed tree gets the new script by hand
    # here because a 3.4.6 `upgrade.sh` predates the unification (documented in UPGRADE.md)
    shutil.copy(S / "upgrade.sh", b / "scripts" / "upgrade.sh")
    rb = _sh(b / "scripts" / "upgrade.sh", V_NEW, cwd=b, env=env)
    assert rb.returncode == 0, rb.stderr + rb.stdout
    assert _tree(a) == _tree(b)
    assert not (a / "backend" / "only_3_4_6.py").exists()          # the dead file is gone
    for d in (a, b):
        e = _env(d)
        assert e["SOKKAN_VERSION"] == V_NEW and e["SOKKAN_COMMIT"] == "abc1234"
        assert e["SOKKAN_LOCAL_TOKEN"] == "keepme"
        assert (d / "workspace" / "mine.txt").read_text() == "mine"
        assert (d / ".sokkan-previous").read_text().strip() == V_OLD
        assert not list(d.glob(".sokkan-update*")) and not (d / ".sokkan-stage").exists()
        assert not list((d / "scripts").glob(".upgrade-*"))
    log = (tmp / "docker.log").read_text()
    assert log.count("compose up -d --build --remove-orphans") == 2


def test_rollback_without_argument_goes_back_to_the_previous_version(world):
    tmp, env = world
    _publish(tmp / "dist", V_OLD, _release(V_OLD), latest=False)
    _publish(tmp / "dist", V_NEW, _release(V_NEW))
    d = _installed(tmp, env, V_OLD)
    assert _sh(S / "install.sh", "--dir", "sokkan", cwd=tmp, env=env).returncode == 0
    r = _sh(d / "scripts" / "rollback.sh", cwd=d, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    e = _env(d)
    assert e["SOKKAN_VERSION"] == V_OLD and e["SOKKAN_COMMIT"] == "3ea74f5"
    assert "# 3.4.6" in (d / "docker-compose.yml").read_text()
    assert (d / ".sokkan-previous").read_text().strip() == V_NEW     # and forward again is one command
    assert not (d / "backend" / "only_3_6_0.py").exists()


# ---------------------------------------------------------------- verification, fail closed

def _snapshot(d: Path) -> dict:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_a_tampered_tarball_is_refused_and_nothing_changes(world):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW), latest=False)
    d = _installed(tmp, env, V_NEW)
    v2 = "3.6.1+bad0000"
    tb = _publish(tmp / "dist", v2, _release(v2))
    tb.write_bytes(tb.read_bytes() + b"\0")                          # after the sha256 was published
    before = _snapshot(d)
    for r in (_sh(d / "scripts" / "upgrade.sh", v2, cwd=d, env=env),
              _sh(S / "install.sh", "--version", v2, "--dir", "sokkan", cwd=tmp, env=env)):
        assert r.returncode != 0 and "sha256" in r.stderr, r.stderr
    assert _snapshot(d) == before
    assert "compose up" not in (tmp / "docker.log").read_text()      # nothing was started


@pytest.mark.parametrize("sig,sha,ok", [
    ("GOOD", True, True),          # signed by the key on this machine
    ("FORGED", True, False),       # a signature that does not match
    (None, True, False),           # no signature at all
    ("GOOD", False, False),        # no digest published
])
def test_required_signature_fails_closed(world, sig, sha, ok):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW), latest=False)
    d = _installed(tmp, env, V_NEW)
    (d / ".env").write_text((d / ".env").read_text() + "SOKKAN_REQUIRE_SIGNATURE=1\n")
    v2 = "3.6.1+5151515"
    _publish(tmp / "dist", v2, _release(v2), sha=sha, sig=sig)
    before = _snapshot(d)
    r = _sh(d / "scripts" / "upgrade.sh", v2, cwd=d, env=env)
    if ok:
        assert r.returncode == 0, r.stderr
        assert _env(d)["SOKKAN_VERSION"] == v2 and "signature OK" in r.stderr
    else:
        assert r.returncode == 3 and "REFUSED" in r.stderr, r.stderr + r.stdout
        assert _snapshot(d) == before and not (tmp / "docker.log").exists()


def test_the_policy_of_the_box_cannot_be_lowered_from_the_environment(world):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW), latest=False)
    d = _installed(tmp, env, V_NEW)
    (d / ".env").write_text((d / ".env").read_text() + "SOKKAN_REQUIRE_SIGNATURE=1\n")
    v2 = "3.6.1+5151515"
    _publish(tmp / "dist", v2, _release(v2), sig=None)
    r = _sh(d / "scripts" / "upgrade.sh", v2, cwd=d, env={**env, "SOKKAN_REQUIRE_SIGNATURE": "0"})
    assert r.returncode == 3 and _env(d)["SOKKAN_VERSION"] == V_NEW


@pytest.mark.parametrize("missing", ["cosign", "key"])
def test_required_signature_without_the_means_to_check_is_a_refusal(world, missing):
    tmp, env = world
    tb = _publish(tmp / "dist", V_NEW, _release(V_NEW), sig="GOOD")
    e = dict(env, SOKKAN_REQUIRE_SIGNATURE="1")
    if missing == "cosign":
        (tmp / "bin" / "cosign").unlink()
        e["PATH"] = f"{tmp}/bin:/usr/bin:/bin"
    else:
        e["SOKKAN_COSIGN_PUB"] = str(tmp / "no-such.pub")
    r = _sh(S / "verify-release.sh", str(tb), cwd=tmp, env=e)
    assert r.returncode == 3 and "REFUSED" in r.stderr
    r = _sh(S / "install.sh", "--version", V_NEW, "--dir", "x", cwd=tmp, env=e)
    assert r.returncode != 0 and "REFUSED" in r.stderr and not (tmp / "x").exists()


def test_unsigned_release_is_accepted_with_a_warning_when_not_required(world):
    tmp, env = world
    tb = _publish(tmp / "dist", V_NEW, _release(V_NEW), sha=False)
    r = _sh(S / "verify-release.sh", str(tb), cwd=tmp, env=env)
    assert r.returncode == 0 and "WARNING" in r.stderr and "no signature" in r.stderr
    (tb.parent / (tb.name + ".sig")).write_text("FORGED")            # but a BAD one never is
    assert _sh(S / "verify-release.sh", str(tb), cwd=tmp, env=env).returncode == 3


def test_an_image_is_only_verified_by_digest(world):
    tmp, env = world
    r = _sh(S / "verify-release.sh", "--image", "registry.example/sokkan-api:3.6.0", cwd=tmp, env=env)
    assert r.returncode == 3 and "by digest" in r.stderr


# ---------------------------------------------------------------- images mode

def test_images_mode_pulls_and_verifies_before_touching_the_code(world):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW), latest=False)
    d = _installed(tmp, env, V_NEW)
    v2 = "3.6.1+5151515"
    tb = _publish(tmp / "dist", v2, _release(v2), sig="GOOD")
    dg = "sha256:" + "a" * 64
    (tb.parent / (tb.name + ".images")).write_text(f"api=reg.example/sokkan-api@{dg}\nweb=reg.example/sokkan-web@{dg}\n")
    # the image signature check fails (stub cosign: `verify` has no --signature → refuses)
    before = _snapshot(d)
    r = _sh(d / "scripts" / "upgrade.sh", v2, "--images", cwd=d, env=dict(env, SOKKAN_REQUIRE_SIGNATURE="1"))
    assert r.returncode == 3 and "image api did not pass verification" in r.stderr, r.stderr
    assert _snapshot(d) == before
    log = (tmp / "docker.log").read_text()
    assert "pull -q reg.example/sokkan-api@" in log and "compose up" not in log
    # with a cosign that accepts the image: tags, override file, no build
    (tmp / "bin" / "cosign").write_text('#!/bin/sh\nexit 0\n')
    r = _sh(d / "scripts" / "upgrade.sh", v2, "--images", cwd=d, env=dict(env, SOKKAN_REQUIRE_SIGNATURE="1"))
    assert r.returncode == 0, r.stderr + r.stdout
    e = _env(d)
    tag = v2.replace("+", "-")                       # a docker tag cannot hold `+` (found by the e2e)
    assert e["SOKKAN_IMAGES"] == "1" and e["SOKKAN_IMAGE_TAG"] == tag
    assert e["COMPOSE_FILE"] == "docker-compose.yml:docker/compose.images.yml"
    log = (tmp / "docker.log").read_text()
    assert f"tag reg.example/sokkan-api@{dg} sokkan-api:{tag}" in log
    assert "+" not in "".join(x for x in log.splitlines() if x.startswith("tag ")).split(" sokkan-api:")[-1]
    assert "compose up -d --no-build --remove-orphans" in log and "--build --remove" not in log.replace("--no-build", "")


def test_images_mode_without_an_image_list_is_refused(world):
    tmp, env = world
    _publish(tmp / "dist", V_NEW, _release(V_NEW), latest=False)
    d = _installed(tmp, env, V_NEW)
    v2 = "3.6.1+5151515"
    _publish(tmp / "dist", v2, _release(v2))
    r = _sh(d / "scripts" / "upgrade.sh", v2, "--images", cwd=d, env=env)
    assert r.returncode == 1 and "no image list" in r.stderr and _env(d)["SOKKAN_VERSION"] == V_NEW


# ---------------------------------------------------------------- appliance profile

ANSWERS = {"SOKKAN_ORG_NAME": "Pilot SA", "SOKKAN_OWNER_EMAIL": "admin@pilot.example",
           "SOKKAN_OWNER_NAME": "Admin", "SOKKAN_BOX_NAME": "sokkan.pilot.lan", "SOKKAN_BOX_IP": "192.168.50.10",
           "SOKKAN_DEEP_TOKEN": "sik_test", "SOKKAN_INSTANCE_ID": "pilot-a", "SOKKAN_HEARTBEAT_URL": "",
           "SOKKAN_SHIP_URL": "http://host.docker.internal:8004", "SOKKAN_SHIP_MODEL": "qwen3.8-27b"}


def _appliance(world, **over):
    tmp, env = world
    files = _release(V_NEW, {"scripts/memory-setup.sh": "#!/bin/sh\nexit 0\n"})
    tb = _publish(tmp / "dist", V_NEW, files, sig="GOOD")
    e = {**env, **ANSWERS, **over}
    return tmp, e, tb


def test_appliance_profile_writes_a_complete_env_with_no_placeholder(world):
    tmp, e, tb = _appliance(world)
    r = _sh(S / "install.sh", "--profile", "appliance", "--version", V_NEW, "--dir", str(tmp / "box"),
            cwd=tmp, env=e)
    assert r.returncode == 0, r.stderr + r.stdout
    box = tmp / "box"
    text = (box / ".env").read_text()
    assert not [ln for ln in text.splitlines() if not ln.startswith("#") and "<" in ln]
    assert oct((box / ".env").stat().st_mode & 0o777) == "0o600"
    got = _env(box)
    template = [x.split("=", 1)[0] for x in (ROOT / "deploy/appliance/env.appliance.example").read_text().splitlines()
                if "=" in x and not x.startswith("#")]
    builds_here = {"SOKKAN_IMAGE_TAG", "COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR"}      # no images published
    decided_by_the_machine = {"SOKKAN_MEMORY_PROFILE", "SOKKAN_EMBED_ACCEL", "SOKKAN_ACCEPT_GEMMA_TERMS"}
    missing = [k for k in template if k not in got and k not in builds_here | decided_by_the_machine]
    assert not missing, missing
    assert got["SOKKAN_VERSION"] == V_NEW and got["SOKKAN_COMMIT"] == "abc1234"
    assert got["SOKKAN_REQUIRE_SIGNATURE"] == "1" and got["SOKKAN_BIND"] == "127.0.0.1"
    assert got["SOKKAN_TLS_SITES"] == "https://sokkan.pilot.lan, https://192.168.50.10, https://localhost"
    assert got["SOKKAN_PUBLIC_URL"] == "https://sokkan.pilot.lan"
    assert got["SOKKAN_DEFAULT_ROLE"] == "none" and got["SOKKAN_FEATURE_MULTI_PROJECT"] == "1"
    assert got["SOKKAN_FEATURE_TMUX"] == "0" and got["SOKKAN_FEATURE_MISSIONS_LINK"] == "0"
    assert got["SOKKAN_ASSISTANT_LLM_URL"] == "http://host.docker.internal:8004/v1"
    assert "appliance" in got["COMPOSE_PROFILES"].split(",")
    assert len(got["SOKKAN_LOCAL_TOKEN"]) == 48 and got["SOKKAN_LOCAL_TOKEN"] in r.stdout
    assert got["SOKKAN_IMAGES"] == "0" and "BUILT on this machine" in r.stdout
    assert "compose up -d --build --remove-orphans" in (tmp / "docker.log").read_text()


def test_appliance_install_refuses_an_unsigned_release(world):
    tmp, e, tb = _appliance(world)
    (tb.parent / (tb.name + ".sig")).unlink()
    r = _sh(S / "install.sh", "--profile", "appliance", "--version", V_NEW, "--dir", str(tmp / "box"),
            cwd=tmp, env=e)
    assert r.returncode != 0 and "REFUSED" in r.stderr and not (tmp / "box").exists()


def test_every_template_variable_is_a_declared_one():
    """A line of the appliance template that no service reads would be a setting without effect."""
    import yaml
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    known = set()
    for svc in c["services"].values():
        known |= set(svc.get("environment") or {})
    raw = (ROOT / "docker-compose.yml").read_text() + (ROOT / "docker/compose.images.yml").read_text()
    compose_level = {"COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "COMPOSE_PROFILES"}
    scripts = "".join((S / f).read_text() for f in ("upgrade.sh", "install.sh", "verify-release.sh",
                                                     "memory-setup.sh", "appliance/host-setup.sh"))
    for line in (ROOT / "deploy/appliance/env.appliance.example").read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k = line.split("=", 1)[0]
        assert k in known or k in compose_level or ("${" + k) in raw or k in scripts, \
            f"{k} is in the appliance template but nothing reads it"
