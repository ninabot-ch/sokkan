"""install.sh fails CLOSED when a release cannot be verified (4.0.2).

On 18.11.2026 the site stopped serving the `.sig` and `.sha256` of the current release for
about 40 minutes; the installer of the time (community profile, SOKKAN_REQUIRE_SIGNATURE=0 by
default) installed it WITHOUT any check. From 3.6.1 on, every release is published signed:
the installer now refuses it when its digest or signature is missing, unreadable or wrong, and
only an explicit SOKKAN_ALLOW_UNSIGNED=1 installs it anyway, with a warning that is logged.

End to end, with no external network and no Docker: a real HTTP server on 127.0.0.1 serves a
fake dist folder, a key pair generated for the test signs the release (the cosign key-pair
format: a base64 ECDSA P-256 / SHA-256 signature, checked here with openssl), and `docker` is
a stub that only logs. The real release key is never used to sign anything."""
import base64
import functools
import hashlib
import http.server
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "scripts"
if not all(shutil.which(x) for x in ("tar", "sh", "curl", "openssl")):
    pytest.skip("tar, sh, curl and openssl are required", allow_module_level=True)

VER = "4.0.2+e2e0001"                 # a signed-era release (>= 3.6.1)
OLD = "3.6.0+abc1234"                 # published before signatures existed


def _openssl(*args, data: bytes | None = None) -> bytes:
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def _keypair(d: Path, name: str) -> tuple[Path, Path]:
    key, pub = d / f"{name}.key", d / f"{name}.pub"
    key.write_bytes(_openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout"))
    pub.write_bytes(_openssl("pkey", "-in", str(key), "-pubout"))
    return key, pub


def _sign(key: Path, f: Path) -> str:
    """What `cosign sign-blob --key` writes: base64 of the DER ECDSA signature of SHA-256(f)."""
    return base64.b64encode(_openssl("dgst", "-sha256", "-sign", str(key), str(f))).decode()


def _release(dist: Path, ver: str) -> Path:
    files = {"docker-compose.yml": "services: {}\n", "VERSION": ver.split("+")[0] + "\n",
             ".env.example": "SOKKAN_LOCAL_TOKEN=\nSOKKAN_PORT=3009\n", "backend/app.py": "# app\n"}
    dist.mkdir(parents=True, exist_ok=True)
    tb = dist / f"sokkan-{ver}.tar.gz"
    with tarfile.open(tb, "w:gz") as t:
        for rel, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(f"sokkan/{rel}")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    (dist / "VERSION").write_text(ver + "\n")
    return tb


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture()
def site(tmp_path):
    """A dist served over HTTP on 127.0.0.1 (missing file = 404, as on the incident)."""
    root = tmp_path / "www"
    (root / "dist").mkdir(parents=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/docker.log\nexit 0\n')
    (bin_ / "docker").chmod(0o755)
    key, pub = _keypair(tmp_path, "test-release")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "SOKKAN_BASE": f"http://127.0.0.1:{srv.server_port}", "SOKKAN_COSIGN_PUB": str(pub),
           "SOKKAN_ACCEPT_GEMMA_TERMS": "0", "NO_COLOR": "1", "http_proxy": "", "https_proxy": "",
           "HTTP_PROXY": "", "HTTPS_PROXY": "", "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
    for k in ("SOKKAN_REQUIRE_SIGNATURE", "SOKKAN_ALLOW_UNSIGNED", "SOKKAN_COSIGN", "SOKKAN_PROFILE",
              "SOKKAN_RELEASE", "SOKKAN_DIR"):
        env.pop(k, None)
    yield {"tmp": tmp_path, "dist": root / "dist", "key": key, "env": env}
    srv.shutdown()
    srv.server_close()


def _publish(site, ver=VER, sha="good", sig="good") -> Path:
    tb = _release(site["dist"], ver)
    if sha == "good":
        (tb.parent / (tb.name + ".sha256")).write_text(hashlib.sha256(tb.read_bytes()).hexdigest() + "\n")
    elif sha == "garbage":
        (tb.parent / (tb.name + ".sha256")).write_text("<html>not found</html>\n")
    if sig == "good":
        (tb.parent / (tb.name + ".sig")).write_text(_sign(site["key"], tb))
    elif sig == "other-key":
        other, _ = _keypair(site["tmp"], "someone-else")
        (tb.parent / (tb.name + ".sig")).write_text(_sign(other, tb))
    elif sig == "empty":
        (tb.parent / (tb.name + ".sig")).write_text("")
    return tb


def _install(site, *args, **env):
    e = {**site["env"], **env}
    return subprocess.run(["sh", str(S / "install.sh"), "--dir", "inst", *args], cwd=site["tmp"], env=e,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


def _nothing_installed(site):
    assert not (site["tmp"] / "inst").exists()
    log = site["tmp"] / "docker.log"
    assert not log.exists() or "compose up" not in log.read_text()


# ---------------------------------------------------------------- the four cases of the brief

@pytest.mark.parametrize("sha,sig,what", [
    ("good", None, ".sig"),            # the incident: .sig 404
    (None, None, ".sha256"),           # both 404
    (None, "good", ".sha256"),         # digest 404
    ("good", "empty", ".sig"),         # an empty file
    ("garbage", "good", ".sha256"),    # an HTML page instead of a digest
])
def test_missing_or_unreadable_files_are_refused_and_nothing_is_installed(site, sha, sig, what):
    _publish(site, sha=sha, sig=sig)
    r = _install(site)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "REFUSED" in r.stderr and "REFUSÉE" in r.stderr                      # English and French
    url = f"{site['env']['SOKKAN_BASE']}/dist/sokkan-{VER}.tar.gz{what}"
    assert url in r.stderr, r.stderr                                           # which URL failed
    assert "https://sokkan.ch/dist/" in r.stderr and "SOKKAN_ALLOW_UNSIGNED=1" in r.stderr   # what to do
    _nothing_installed(site)


@pytest.mark.parametrize("allow", ["0", "1"])
def test_a_wrong_signature_is_refused_even_with_the_explicit_override(site, allow):
    _publish(site, sig="other-key")
    r = _install(site, SOKKAN_ALLOW_UNSIGNED=allow)
    assert r.returncode != 0 and "does not match" in r.stderr and "REFUSED" in r.stderr, r.stderr
    _nothing_installed(site)


def test_a_wrong_digest_is_refused_even_with_the_explicit_override(site):
    tb = _publish(site)
    (tb.parent / (tb.name + ".sha256")).write_text("0" * 64 + "\n")
    r = _install(site, SOKKAN_ALLOW_UNSIGNED="1")
    assert r.returncode != 0 and "sha256 mismatch" in r.stderr, r.stderr
    _nothing_installed(site)


def test_the_explicit_override_installs_with_a_logged_warning(site):
    _publish(site, sig=None)
    r = _install(site, SOKKAN_ALLOW_UNSIGNED="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING" in r.stderr and "AVERTISSEMENT" in r.stderr
    assert f"sokkan-{VER}.tar.gz.sig" in r.stderr and "Risk" in r.stderr and "SOKKAN_ALLOW_UNSIGNED=1" in r.stderr
    inst = site["tmp"] / "inst"
    assert (inst / "docker-compose.yml").exists() and f"SOKKAN_VERSION={VER}" in (inst / ".env").read_text()
    log = (inst / ".sokkan-install.log").read_text()
    assert "WARNING SOKKAN_ALLOW_UNSIGNED=1" in log and f"sokkan-{VER}.tar.gz.sig" in log
    assert oct((inst / ".sokkan-install.log").stat().st_mode & 0o777) == "0o600"


def test_a_complete_valid_release_installs(site):
    _publish(site)
    r = _install(site)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "signature verified" in r.stdout and "WARNING" not in r.stderr
    assert not (site["tmp"] / "inst" / ".sokkan-install.log").exists()


# ---------------------------------------------------------------- the switches around it

def test_require_signature_0_no_longer_lets_an_unsigned_release_through(site):
    _publish(site, sig=None)
    r = _install(site, SOKKAN_REQUIRE_SIGNATURE="0")
    assert r.returncode != 0 and "REFUSED" in r.stderr
    _nothing_installed(site)


def test_require_signature_1_stays_strict_over_the_override(site):
    _publish(site, sig=None)
    r = _install(site, SOKKAN_REQUIRE_SIGNATURE="1", SOKKAN_ALLOW_UNSIGNED="1")
    assert r.returncode != 0 and "REFUSED" in r.stderr and "is ignored" in r.stderr
    _nothing_installed(site)


def test_a_release_older_than_signatures_still_installs_with_a_warning(site):
    _publish(site, ver=OLD, sha=None, sig=None)
    r = _install(site, "--version", OLD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "before signatures existed" in r.stdout
    # but SOKKAN_REQUIRE_SIGNATURE=1 refuses it, as before
    shutil.rmtree(site["tmp"] / "inst")
    r = _install(site, "--version", OLD, SOKKAN_REQUIRE_SIGNATURE="1")
    assert r.returncode != 0 and "REFUSED" in r.stderr


def test_without_a_key_of_its_own_the_machine_uses_the_release_key_of_the_installer(site):
    """No SOKKAN_COSIGN_PUB, no /etc/sokkan/cosign.pub: the key written in install.sh is used — the
    real release key, which did NOT sign this test release, so it is refused as a wrong signature."""
    if Path("/etc/sokkan/cosign.pub").exists():
        pytest.skip("this machine has its own /etc/sokkan/cosign.pub")
    _publish(site)
    env = dict(site["env"])
    env.pop("SOKKAN_COSIGN_PUB")
    r = subprocess.run(["sh", str(S / "install.sh"), "--dir", "inst"], cwd=site["tmp"], env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert r.returncode != 0 and "does not match" in r.stderr, r.stderr
    _nothing_installed(site)


def test_verify_release_applies_the_same_rules(site):
    """scripts/verify-release.sh (upgrade.sh, rollback.sh of an installed 4.0.2+)."""
    tb = _publish(site, sig=None)
    e = site["env"]
    r = subprocess.run(["sh", str(S / "verify-release.sh"), str(tb)], env=e, capture_output=True, text=True)
    assert r.returncode == 3 and "REFUSED" in r.stderr
    logf = site["tmp"] / "inst.log"
    r = subprocess.run(["sh", str(S / "verify-release.sh"), str(tb)], capture_output=True, text=True,
                       env={**e, "SOKKAN_ALLOW_UNSIGNED": "1", "SOKKAN_INSTALL_LOG": str(logf)})
    assert r.returncode == 0 and "WARNING" in r.stderr and "WARNING SOKKAN_ALLOW_UNSIGNED=1" in logf.read_text()
    (tb.parent / (tb.name + ".sig")).write_text(_sign(site["key"], tb))
    r = subprocess.run(["sh", str(S / "verify-release.sh"), str(tb)], env=e, capture_output=True, text=True)
    assert r.returncode == 0 and "signature OK" in r.stderr, r.stderr


# ---------------------------------------------------------------- the copies stay in step

def test_the_key_in_the_installer_is_the_release_key():
    m = re.search(r"^RELEASE_KEY='([^']*)'", (S / "install.sh").read_text(), re.M)
    assert m, "scripts/install.sh no longer embeds RELEASE_KEY"
    assert m.group(1).strip() == (ROOT / "deploy/appliance/cosign.pub").read_text().strip()


def test_both_scripts_agree_on_the_first_signed_release():
    first = {re.search(r"^FIRST_SIGNED=(\S+)", (S / f).read_text(), re.M).group(1)
             for f in ("install.sh", "verify-release.sh")}
    assert first == {"3.6.1"}
