"""scripts/upgrade.sh — a release put in place without the files it no longer ships.

3.4.3, born on the public demo: `tar xz` over the tree kept `frontend/components/MemoryKB.tsx`
(removed in 3.0), Next type-checked it, `npm run build` failed. The script removes, inside
the code directories, every file absent from the new tarball — and never touches `.env`,
the override, the workspace, a local data folder, node_modules/.next, or the user's own
top-level files. Throw-away tree under tmp_path; no Docker (`--no-build`).
"""

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "upgrade.sh"
if not shutil.which("tar") or not shutil.which("sh"):
    pytest.skip("tar and sh are required", allow_module_level=True)


def _w(root: Path, rel: str, text: str = "x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _tarball(path: Path, files: dict[str, str]) -> Path:
    """A release as make-release.sh ships it: git archive with the `sokkan/` prefix."""
    with tarfile.open(path, "w:gz") as t:
        for rel, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(f"sokkan/{rel}")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return path


NEW_RELEASE = {
    "docker-compose.yml": "services: {}\n", "VERSION": "3.4.3\n", "README.md": "new\n",
    "scripts/upgrade.sh": SCRIPT.read_text(), "scripts/memory-setup.sh": "#!/bin/sh\n",
    "backend/app.py": "new app\n", "backend/new_module.py": "fresh\n",
    "frontend/components/Live.tsx": "new live\n", "frontend/components/New.tsx": "new\n",
    "frontend/package.json": "{}\n", "memory/core/store.py": "store\n",
    "docs/UPGRADE.md": "doc\n", "tests/test_x.py": "pass\n",
}


@pytest.fixture()
def tree(tmp_path):
    """An installed 3.4.2 tree with dead files, user files and data."""
    t = tmp_path / "sokkan"
    _w(t, ".env", "SOKKAN_LOCAL_TOKEN=abc\nSOKKAN_WORKSPACE=./workspace\nSOKKAN_DATA_DIR=./data\n"
                  "SOKKAN_VERSION=3.4.2+old\nSOKKAN_MEMORY_PROFILE=leger\n")
    _w(t, "docker-compose.yml", "old\n")
    _w(t, "docker-compose.override.yml", "services:\n  api:\n    ports: ['3010:3009']\n")
    _w(t, "README.md", "old\n")
    _w(t, "VERSION", "3.4.2\n")
    shutil.copy(SCRIPT, _w(t, "scripts/upgrade.sh"))
    _w(t, "scripts/memory-setup.sh", "#!/bin/sh\n")
    _w(t, "scripts/gone.sh", "dead script\n")
    _w(t, "backend/app.py", "old app\n")
    _w(t, "backend/dead.py", "removed in 3.4.3\n")
    _w(t, "backend/__pycache__/app.cpython-312.pyc", "pyc")
    _w(t, "backend/.env", "BACKEND_SECRET=1\n")                  # a user's own env file
    _w(t, "frontend/components/Live.tsx", "old live\n")
    _w(t, "frontend/components/MemoryKB.tsx", "dead — removed in 3.0\n")
    _w(t, "frontend/components/MemoryGraph.tsx", "dead\n")
    _w(t, "frontend/components/old-dir/Profile.tsx", "dead\n")
    _w(t, "frontend/package.json", "{}\n")
    _w(t, "frontend/node_modules/left-pad/index.js", "module\n")
    _w(t, "frontend/.next/cache/x", "cache\n")
    _w(t, "frontend/.env.local", "NEXT_PUBLIC_X=1\n")
    _w(t, "memory/core/store.py", "store\n")
    _w(t, "memory/old_indexer.py", "dead\n")
    _w(t, "docs/UPGRADE.md", "doc\n")
    _w(t, "docs/old-notes.md", "dead doc\n")
    _w(t, "tests/test_x.py", "pass\n")
    _w(t, "tests/test_dead.py", "dead test\n")
    _w(t, "workspace/myproject/main.py", "user code\n")           # SOKKAN_WORKSPACE
    _w(t, "data/memory.db", "sqlite bytes\n")                      # SOKKAN_DATA_DIR
    _w(t, "my-notes.md", "the user's own file\n")                  # not part of a release
    _w(t, "backups/2026-10-01.tar.gz", "backup\n")
    return t


def _run(tree: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SOKKAN_")}
    env["SOKKAN_BASE"] = "http://127.0.0.1:9"        # nothing listens: no download
    return subprocess.run(["sh", str(tree / "scripts" / "upgrade.sh"), *args], cwd=tree,
                          env=env, capture_output=True, text=True, check=check)


def test_dead_code_files_are_removed_and_data_kept(tree, tmp_path):
    tb = _tarball(tmp_path / "sokkan-3.4.3+abc1234.tar.gz", NEW_RELEASE)
    r = _run(tree, str(tb), "--no-build")
    assert "frontend/components/MemoryKB.tsx" in r.stdout and "backend/dead.py" in r.stdout
    # the dead code is gone, the release's files are in place (new content)
    for dead in ("frontend/components/MemoryKB.tsx", "frontend/components/MemoryGraph.tsx",
                 "frontend/components/old-dir/Profile.tsx", "frontend/components/old-dir",
                 "backend/dead.py", "backend/__pycache__", "scripts/gone.sh",
                 "memory/old_indexer.py", "docs/old-notes.md", "tests/test_dead.py"):
        assert not (tree / dead).exists(), dead
    assert (tree / "frontend/components/Live.tsx").read_text() == "new live\n"
    assert (tree / "frontend/components/New.tsx").exists()
    assert (tree / "backend/app.py").read_text() == "new app\n"
    assert (tree / "backend/new_module.py").exists()
    assert (tree / "README.md").read_text() == "new\n" and (tree / "VERSION").read_text() == "3.4.3\n"
    # never touched
    env = (tree / ".env").read_text()
    assert "SOKKAN_LOCAL_TOKEN=abc" in env and "SOKKAN_WORKSPACE=./workspace" in env
    assert "SOKKAN_VERSION=3.4.3+abc1234" in env and "SOKKAN_VERSION=3.4.2+old" not in env
    assert (tree / "docker-compose.override.yml").read_text().startswith("services:")
    assert (tree / "backend/.env").read_text() == "BACKEND_SECRET=1\n"
    assert (tree / "frontend/.env.local").exists()
    assert (tree / "frontend/node_modules/left-pad/index.js").exists()
    assert (tree / "frontend/.next/cache/x").exists()
    assert (tree / "workspace/myproject/main.py").read_text() == "user code\n"
    assert (tree / "data/memory.db").read_text() == "sqlite bytes\n"
    assert (tree / "my-notes.md").exists() and (tree / "backups/2026-10-01.tar.gz").exists()
    assert not list(tree.glob(".sokkan-update.*"))


def test_dry_run_changes_nothing(tree, tmp_path):
    tb = _tarball(tmp_path / "sokkan-latest.tar.gz", NEW_RELEASE)
    before = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))
    r = _run(tree, str(tb), "--dry-run")
    assert "frontend/components/MemoryKB.tsx" in r.stdout and "Nothing was changed" in r.stdout
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*")) == before
    assert (tree / "backend/app.py").read_text() == "old app\n"
    assert "SOKKAN_VERSION=3.4.2+old" in (tree / ".env").read_text()


def test_bad_tarball_or_failed_download_changes_nothing(tree, tmp_path):
    before = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))
    bad = _tarball(tmp_path / "sokkan-bad.tar.gz", {"README.md": "not a release\n"})
    r = _run(tree, str(bad), "--no-build", check=False)
    assert r.returncode != 0 and "nothing was changed" in r.stderr
    r = _run(tree, "9.9.9+nohash", "--no-build", check=False)       # download fails
    assert r.returncode != 0 and "nothing was changed" in r.stderr
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*")) == before
    assert not list(tree.glob(".sokkan-update.*"))


def test_refuses_outside_a_sokkan_folder(tmp_path):
    d = tmp_path / "elsewhere"
    shutil.copy(SCRIPT, _w(d, "scripts/upgrade.sh"))
    r = _run(d, "--no-build", check=False)
    assert r.returncode != 0 and "sokkan folder" in r.stderr
