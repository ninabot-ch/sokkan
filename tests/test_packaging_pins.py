"""D1 of the on-prem audit: the image of a release must be reproducible — the Claude Code CLI
and the SDK that drives it are pinned, in every place that installs them, to the same versions;
Python packages install under a constraints file. Plus: the image carries no feature switch
(compose passes them all, an image default is silently overridden by "")."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = r"\d+\.\d+\.\d+"


def _api() -> str:
    return (ROOT / "docker" / "api.Dockerfile").read_text()


def test_claude_code_cli_is_pinned_in_the_api_image():
    d = _api()
    m = re.search(rf"^ARG CLAUDE_CODE_VERSION=({SEMVER})$", d, re.M)
    assert m, "docker/api.Dockerfile: ARG CLAUDE_CODE_VERSION=<x.y.z>"
    assert '@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}' in d
    assert not re.search(r"npm install -g @anthropic-ai/claude-code(\s|$)", d), "unpinned npm install"
    assert "claude --version | grep -F" in d, "the build must check the version it installed"


def test_agent_sdk_is_pinned_and_the_session_image_follows():
    req = (ROOT / "backend" / "requirements.txt").read_text()
    m = re.search(rf"^claude-agent-sdk==({SEMVER})$", req, re.M)
    assert m, "backend/requirements.txt: claude-agent-sdk==<x.y.z>"
    s = (ROOT / "docker" / "session.Dockerfile").read_text()
    ms = re.search(rf"^ARG CLAUDE_AGENT_SDK_VERSION=({SEMVER})$", s, re.M)
    assert ms and ms.group(1) == m.group(1), "docker/session.Dockerfile must pin the same SDK as the api"
    c = ROOT / "docker" / "constraints.txt"
    mc = re.search(rf"^claude-agent-sdk==({SEMVER})$", c.read_text(), re.M)
    assert mc and mc.group(1) == m.group(1), "docker/constraints.txt disagrees with requirements.txt"


def test_python_packages_install_under_constraints():
    d = _api()
    assert "COPY docker/constraints.txt" in d and "-c /tmp/constraints.txt" in d
    pins = [x for x in (ROOT / "docker" / "constraints.txt").read_text().splitlines()
            if x.strip() and not x.startswith("#")]
    assert len(pins) >= 40, "constraints.txt must list the whole closure (scripts/freeze-constraints.sh)"
    loose = [p for p in pins if "==" not in p]
    assert not loose, loose
    for direct in ("fastapi", "uvicorn", "httpx", "mcp", "fastembed", "psycopg", "cryptography"):
        assert any(p.lower().startswith(direct + "==") for p in pins), f"{direct} missing from constraints"


def test_base_images_carry_a_version_tag():
    for f in ("api.Dockerfile", "web.Dockerfile", "session.Dockerfile"):
        for line in (ROOT / "docker" / f).read_text().splitlines():
            m = re.match(r"FROM\s+(\S+)", line)
            if m and ":" not in m.group(1) and m.group(1) not in ("build", "cli", "scratch"):
                raise AssertionError(f"{f}: {line} has no tag")


def test_the_image_sets_no_feature_switch():
    assert not re.search(r"SOKKAN_FEATURE_[A-Z_]+=", _api()), (
        "an ENV SOKKAN_FEATURE_* in the image is overridden by the empty value compose passes")


def test_missions_counter_is_not_fetched_when_off(monkeypatch):
    import importlib
    import sys
    sys.path.insert(0, str(ROOT / "backend"))
    monkeypatch.delenv("SOKKAN_FEATURE_MISSIONS_LINK", raising=False)
    monkeypatch.delenv("SOKKAN_EDITION", raising=False)
    saved = sys.modules.pop("missions", None)
    try:
        missions = importlib.import_module("missions")
        monkeypatch.setattr(missions.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("outbound call")))
        assert missions.ENABLED is False and missions.stats() == {"open": None}
    finally:
        sys.modules.pop("missions", None)
        if saved is not None:
            sys.modules["missions"] = saved
