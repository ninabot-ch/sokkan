"""doc_ingest — sources: nothing outside the allow-list is read, nothing is ever written to a
source, the rclone mirror only goes one way."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from docingest_helpers import FakeLLM, write_sources


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    for v in ("SOKKAN_DOC_INGEST_DIR", "SOKKAN_DOC_INGEST_CONFIG", "SOKKAN_DOC_INGEST_RCLONE",
              "SOKKAN_DOC_INGEST_RCLONE_CONFIG", "SOKKAN_MEMORY_QUARANTINE_DIR"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SOKKAN_DOC_INGEST_CONCURRENCY", "1")
    import audit
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    from docingest import state
    state._ready.clear()
    return tmp_path


def _src(**kw):
    from docingest import config
    return config.parse_source({"id": "nas", "kind": "folder", "root": "/srv/docs",
                                "allow": ["Cours"], **kw})


def _tree(root):
    for rel in ("Cours/plan.md", "Cours/2026/pv.md", "Cours/Brouillons/idee.md",
                "Cours/~$plan.md", "Cours/.cache/x.md", "RH/salaires.md", "Coursives/x.md",
                "secret.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n\ncontenu de {rel}\n")


# ---- the allow-list ---------------------------------------------------------------------------
def test_only_allow_listed_paths_are_listed(env):
    from docingest import config, sources
    root = env / "docs"
    _tree(root)
    cfg = config.parse_source({"id": "nas", "kind": "folder", "root": str(root),
                               "allow": ["Cours"], "deny": ["**/Brouillons/**"]})
    got = sorted(f.rel for f in sources.open_source(cfg).iter_files())
    assert got == ["Cours/2026/pv.md", "Cours/plan.md"]
    # `Cours` is a directory, not a prefix of names; office lock files and dot-dirs are denied
    assert not sources.allowed(cfg, "Coursives/x.md")
    assert not sources.allowed(cfg, "Cours/~$plan.md") and not sources.allowed(cfg, "Cours/.cache/x.md")
    assert not sources.allowed(cfg, "Cours/../RH/salaires.md") and not sources.allowed(cfg, "/etc/passwd")


def test_globs_do_not_cross_directories_unless_double_star(env):
    from docingest import sources
    cfg = _src(allow=["PV/*/final/*.pdf", "Qualite/**/*.docx"])
    assert sources.allowed(cfg, "PV/2026/final/pv.pdf")
    assert not sources.allowed(cfg, "PV/2026/brouillon/final/pv.pdf")
    assert sources.allowed(cfg, "Qualite/a/b/c/proc.docx") and sources.allowed(cfg, "Qualite/proc.docx")
    assert not sources.allowed(cfg, "Qualite/a/proc.pdf")


def test_a_symlink_cannot_lead_out_of_the_root(env):
    from docingest import config, sources
    root = env / "docs"
    (root / "Cours").mkdir(parents=True)
    (root / "Cours/plan.md").write_text("# plan\n")
    outside = env / "ailleurs"
    outside.mkdir()
    (outside / "secret.md").write_text("# secret\n")
    os.symlink(outside / "secret.md", root / "Cours/lien.md")
    os.symlink(outside, root / "Cours/dossier")
    cfg = config.parse_source({"id": "nas", "kind": "folder", "root": str(root), "allow": ["Cours"]})
    assert [f.rel for f in sources.open_source(cfg).iter_files()] == ["Cours/plan.md"]


@pytest.mark.parametrize("bad, why", [
    ({"allow": []}, "allow"), ({"allow": ["../RH"]}, "relative"), ({"allow": ["/etc"]}, "relative"),
    ({"root": "docs"}, "absolute"), ({"root": "/"}, "cannot be /"), ({"kind": "ftp"}, "unknown source kind"),
    ({"id": "Bad Id"}, "kebab"), ({"schedule": "tous les soirs"}, "schedule"),
    ({"project": "../x"}, "project"), ({"extensions": ["exe"]}, "extensions"),
])
def test_an_invalid_source_is_refused_never_widened(env, bad, why):
    from docingest import config
    with pytest.raises(config.ConfigError, match=why):
        _src(**bad)


def test_a_broken_config_file_raises_and_a_missing_one_reads_nothing(env):
    from docingest import config
    assert config.load() == []
    config.home().mkdir(parents=True)
    config.config_path().write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.load()
    write_sources(config.home(), [{"id": "aa", "kind": "folder", "root": "/srv/a", "allow": ["x"]},
                                  {"id": "aa", "kind": "folder", "root": "/srv/b", "allow": ["x"]}])
    with pytest.raises(config.ConfigError, match="duplicate"):
        config.load()


def test_require_readonly_refuses_a_writable_mount(env, monkeypatch):
    from docingest import config, sources
    root = env / "docs"
    (root / "Cours").mkdir(parents=True)
    cfg = config.parse_source({"id": "nas", "kind": "folder", "root": str(root), "allow": ["Cours"],
                               "require_readonly": True})
    with pytest.raises(sources.SourceError, match="not mounted read-only"):
        sources.open_source(cfg).prepare()
    monkeypatch.setattr(sources, "is_readonly_mount", lambda p: True)
    assert sources.open_source(cfg).prepare() == {"readonly_mount": True}


def test_a_pass_never_modifies_the_source(env):
    """Bytes, dates and the list of files of the source are identical after a pass — and the
    source may be read-only for us (0555 / 0444): the pass still works."""
    from docingest import config, runner
    root = env / "docs"
    _tree(root)

    def snapshot():
        out = {}
        for d, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                st = os.stat(p)
                out[p] = (hashlib.sha256(open(p, "rb").read()).hexdigest(), st.st_mtime_ns, st.st_size)
        return out
    for d, dirs, files in os.walk(root):
        for f in files:
            os.chmod(os.path.join(d, f), 0o444)
        os.chmod(d, 0o555)
    try:
        before = snapshot()
        write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(root),
                                       "allow": ["Cours"]}])
        p = runner.Pass(config.get("nas"), client=FakeLLM(), memory_neighbours=lambda *a: []).run()
        assert p["status"] == "done" and p["counters"]["noted"] == 3
        assert snapshot() == before
    finally:
        for d, dirs, files in os.walk(root):
            os.chmod(d, 0o755)


def test_a_new_connector_plugs_in_but_cannot_widen_the_allow_list(env):
    from docingest import config, runner, sources, state
    root = env / "docs"
    _tree(root)

    class Everything(sources.FolderSource):
        """A sloppy connector that lists the whole tree."""
        def iter_files(self):
            for d, _dirs, files in os.walk(self.cfg.root):
                for f in sorted(files):
                    p = sources.Path(d) / f
                    st = p.stat()
                    yield sources.FileRef(self.cfg.id, p.relative_to(self.cfg.root).as_posix(), p,
                                          st.st_size, st.st_mtime)
    sources.register("everything", Everything)
    try:
        write_sources(config.home(), [{"id": "nas", "kind": "everything", "root": str(root),
                                       "allow": ["Cours/plan.md"]}])
        llm = FakeLLM()
        runner.Pass(config.get("nas"), client=llm, memory_neighbours=lambda *a: []).run()
        assert len(llm.calls) == 1 and "Cours/plan.md" in llm.calls[0]["user"]
        assert sorted(state.files_of("nas")) == ["Cours/plan.md"]
    finally:
        sources._KINDS.pop("everything", None)


# ---- rclone -------------------------------------------------------------------------------------
def _rc(**kw):
    from docingest import config
    return config.parse_source({"id": "m365", "kind": "rclone", "remote": "tenant",
                                "allow": ["Qualite/Procedures", "Direction/PV"], **kw})


def test_rclone_command_is_one_way_into_the_mirror(env):
    from docingest import sources
    cfg = _rc()
    cmd = sources.rclone_sync_command(cfg, "Qualite/Procedures")
    mirror = str(sources.mirror_root(cfg).resolve())
    assert cmd[1] == "sync" and cmd[2] == "tenant:Qualite/Procedures"
    assert cmd[3] == mirror + "/Qualite/Procedures"
    assert "--include" in cmd and "*.docx" in cmd and "--max-size" in cmd
    # the destination is the 4th word and nothing after it is a path of the remote
    assert not any(a.startswith("tenant:") for a in cmd[3:])


@pytest.mark.parametrize("entry", ["RH", "Qualite", "Qualite/Procedures/../../RH", "", "*"])
def test_rclone_refuses_what_is_not_in_the_allow_list(env, entry):
    from docingest import config, sources
    with pytest.raises(config.ConfigError):
        sources.rclone_sync_command(_rc(), entry)


@pytest.mark.parametrize("bad", [
    {"allow": ["Qualite/*"]}, {"remote": "tenant:evil"}, {"remote": ""},
    {"options": {"flags": ["--backup-dir=tenant:x"]}}, {"options": {"flags": ["--config=/etc/x"]}},
    {"options": {"flags": ["--delete-excluded"]}}, {"options": {"flags": ["--drive-impersonate=x"]}},
])
def test_rclone_source_validation(env, bad):
    from docingest import config
    with pytest.raises(config.ConfigError):
        _rc(**bad)
    assert _rc(options={"flags": ["--tpslimit=4", "--drive-shared-with-me"]}).kind == "rclone"


FAKE_RCLONE = r'''#!/usr/bin/env python3
"""Stand-in for the rclone binary: `sync remote:path dest` from a local directory tree
($FAKE_REMOTE_ROOT/<remote>/<path>), honouring --include and --max-size, logging its argv."""
import json, os, shutil, sys
a = sys.argv[1:]
open(os.environ["FAKE_RCLONE_LOG"], "a").write(json.dumps(a) + "\n")
if a[0] != "sync":
    sys.exit(2)
remote, path = a[1].split(":", 1)
src = os.path.join(os.environ["FAKE_REMOTE_ROOT"], remote, path)
if not os.path.isdir(src):
    sys.stderr.write("directory not found\n"); sys.exit(3)
inc = [a[i + 1].lstrip("*") for i, x in enumerate(a) if x == "--include"]
kept = set()
for d, _dirs, files in os.walk(src):
    for f in files:
        if inc and not f.endswith(tuple(inc)):
            continue
        s = os.path.join(d, f); rel = os.path.relpath(s, src); t = os.path.join(a[2], rel)
        kept.add(rel)
        if os.path.exists(t) and os.path.getsize(t) == os.path.getsize(s) and \
                int(os.path.getmtime(t)) == int(os.path.getmtime(s)):
            continue
        os.makedirs(os.path.dirname(t), exist_ok=True); shutil.copy2(s, t)
for d, _dirs, files in os.walk(a[2]):
    for f in files:
        if os.path.relpath(os.path.join(d, f), a[2]) not in kept:
            os.unlink(os.path.join(d, f))
'''


def _real_rclone():
    return os.environ.get("SOKKAN_TEST_RCLONE") or shutil.which("rclone")


@pytest.fixture(params=["fake", "real"])
def remote(request, env, monkeypatch):
    """A remote named `tenant` holding a small tree — served by the stand-in script, and by
    the REAL rclone binary (an `alias` remote on a local directory) when one is installed."""
    tree = env / "remote-root" / "tenant"
    for rel in ("Qualite/Procedures/inscription.md", "Qualite/Procedures/annulation.md",
                "Qualite/Brouillons/idee.md", "Direction/PV/pv-mars.md", "RH/salaires.md",
                "Qualite/Procedures/logo.png"):
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n\nProcédure ou procès-verbal de test : {rel}, contenu durable.\n")
    log = env / "rclone.log"
    if request.param == "fake":
        exe = env / "rclone"
        exe.write_text(FAKE_RCLONE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("SOKKAN_DOC_INGEST_RCLONE", str(exe))
        monkeypatch.setenv("FAKE_REMOTE_ROOT", str(env / "remote-root"))
        monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    else:
        real = _real_rclone()
        if not real:
            pytest.skip("rclone is not installed (SOKKAN_TEST_RCLONE=/path/to/rclone)")
        conf = env / "rclone.conf"
        conf.write_text(f"[tenant]\ntype = alias\nremote = {tree}\n")
        monkeypatch.setenv("SOKKAN_DOC_INGEST_RCLONE", real)
        monkeypatch.setenv("SOKKAN_DOC_INGEST_RCLONE_CONFIG", str(conf))
    from docingest import config
    write_sources(config.home(), [{"id": "m365", "kind": "rclone", "remote": "tenant",
                                   "allow": ["Qualite/Procedures", "Direction/PV"],
                                   "classification": "confidential"}])
    return {"tree": tree, "log": log, "kind": request.param}


def test_rclone_mirror_is_incremental_one_way_and_allow_listed(remote, env):
    from docingest import batches, config, runner, sources, state

    def snap():
        return {str(p.relative_to(remote["tree"])): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in remote["tree"].rglob("*") if p.is_file()}
    before = snap()
    cfg = config.get("m365")
    llm = FakeLLM()
    p = runner.Pass(cfg, client=llm, memory_neighbours=lambda *a: []).run()
    assert p["status"] == "done", p
    mirror = sources.mirror_root(cfg)
    got = sorted(str(x.relative_to(mirror)) for x in mirror.rglob("*") if x.is_file())
    # only the allow-listed directories, only the extensions we read
    assert got == ["Direction/PV/pv-mars.md", "Qualite/Procedures/annulation.md",
                   "Qualite/Procedures/inscription.md"]
    assert p["counters"]["noted"] == 3 and len(llm.calls) == 3
    assert not any("salaires" in c["user"] or "idee" in c["user"] for c in llm.calls)
    # the notes inherit the source's floor
    assert {n["level"] for n in batches.notes_of(p["batch_id"])} == {3}
    # nothing changed upstream -> nothing fetched again, nothing read again
    stamp = {x: x.stat().st_mtime_ns for x in mirror.rglob("*") if x.is_file()}
    p2 = runner.Pass(cfg, client=llm, memory_neighbours=lambda *a: []).run()
    assert len(llm.calls) == 3 and p2["counters"]["unchanged"] == 3
    assert stamp == {x: x.stat().st_mtime_ns for x in mirror.rglob("*") if x.is_file()}
    # one document changes upstream, one disappears -> exactly that is seen
    f = remote["tree"] / "Qualite/Procedures/inscription.md"
    f.write_text(f.read_text() + "\nDECISION: le délai d'inscription passe à 10 jours ouvrables\n")
    os.utime(f, (f.stat().st_mtime + 100, f.stat().st_mtime + 100))
    (remote["tree"] / "Direction/PV/pv-mars.md").unlink()
    changed = snap()
    p3 = runner.Pass(cfg, client=llm, memory_neighbours=lambda *a: []).run()
    assert len(llm.calls) == 4 and "10 jours" in llm.calls[3]["doc"]
    assert p3["counters"]["gone"] == 1 and state.file_get("m365", "Direction/PV/pv-mars.md")["gone_at"]
    # ONE WAY: the remote is byte for byte what its owner left
    assert snap() == changed and {k: v for k, v in before.items() if k in changed and
                                  "inscription" not in k} == {k: v for k, v in changed.items()
                                                              if "inscription" not in k}
    if remote["kind"] == "fake":
        for line in remote["log"].read_text().splitlines():
            argv = json.loads(line)
            assert argv[0] == "sync" and argv[1].startswith("tenant:")
            assert argv[2].startswith(str(mirror.resolve()))


def test_rclone_failure_is_reported_without_leaking_the_config(remote, env, monkeypatch):
    from docingest import config, runner
    write_sources(config.home(), [{"id": "m365", "kind": "rclone", "remote": "tenant",
                                   "allow": ["Nope/Absent"]}])
    p = runner.Pass(config.get("m365"), client=FakeLLM(), memory_neighbours=lambda *a: []).run()
    assert p["status"] == "failed" and "rclone sync failed" in p["error"]
    assert "rclone.conf" not in p["error"]


def test_rclone_missing_binary_is_a_clear_failure(env, monkeypatch):
    from docingest import config, runner
    monkeypatch.setenv("SOKKAN_DOC_INGEST_RCLONE", str(env / "no-such-rclone"))
    write_sources(config.home(), [{"id": "m365", "kind": "rclone", "remote": "tenant",
                                   "allow": ["Qualite"]}])
    p = runner.Pass(config.get("m365"), client=FakeLLM(), memory_neighbours=lambda *a: []).run()
    assert p["status"] == "failed" and "rclone is not installed" in p["error"]


def test_subprocess_is_never_given_a_shell(env):
    """The rclone command is an argv list: a remote path with shell characters stays data."""
    from docingest import sources
    cfg = _rc(allow=["Qualite/$(touch pwned); rm -rf"])
    cmd = sources.rclone_sync_command(cfg, "Qualite/$(touch pwned); rm -rf")
    assert isinstance(cmd, list) and cmd[2] == "tenant:Qualite/$(touch pwned); rm -rf"
    src = open(sources.__file__).read()
    assert "shell=True" not in src and subprocess is not None
