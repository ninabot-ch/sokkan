"""CortHeXis review engine (memory/core/review.py) and repairs (memory/core/repair.py).

The false-positive rate is measured on the fictional corpus of tests/fixtures/review_corpus
(expected.json lists the true problems; everything else flagged is a false positive).
The Postgres tests run when SOKKAN_TEST_PG_DSN is set.
"""
import datetime
import http.server
import json
import os
import sqlite3
import sys
import textwrap
import threading
import uuid
from pathlib import Path

import pytest

import review_fixture as rf
from core import repair
from core.memstore import InMemoryStore
from core.review import (AlertPolicy, ChainConfig, MemorySource, ReviewConfig, SqliteHistory,
                         SqliteSource, alert_decision, chain_checks, format_digest, health_score,
                         load_corpus, mark_alerted, run_review, suggest_target)

TARGET_FP = 0.10


def cfg_for(mem, repo, **kw):
    return ReviewConfig(memory_dir=mem, giant_words=600, dead_path_roots=[repo],
                        dead_path_prefixes=("scripts", "docs", "infra"), **kw)


@pytest.fixture()
def corpus(tmp_path):
    mem, repo = rf.build(tmp_path)
    st = InMemoryStore()
    rf.index(st, mem)
    return mem, repo, st


def test_false_positive_rate_on_fixture_corpus(corpus):
    mem, repo, st = corpus
    rep = run_review(cfg_for(mem, repo), MemorySource(st), now=rf.NOW)
    fp_rate, fps, missed = rf.false_positive_rate(rep)
    print(f"\nfixture corpus: {len(rf.flagged(rep))} flagged, FP {fp_rate:.1%} {sorted(fps)}, "
          f"missed {sorted(missed)}")
    assert fp_rate < TARGET_FP, sorted(fps)
    assert not missed, sorted(missed)


def test_report_never_contains_the_secret_value(corpus):
    mem, repo, st = corpus
    rep = run_review(cfg_for(mem, repo), MemorySource(st), now=rf.NOW)
    dump = json.dumps(rep)
    assert rf.fake_key() not in dump
    assert rf.fake_key()[:12] not in dump
    sec = next(f for f in rep["findings"] if f["id"] == "secrets")
    assert sec["severity"] == "crit" and sec["items"][0]["kind"] == "API key (sk-…)"
    assert sec["items"][0]["line"] >= 1


def test_actions_are_attached_to_findings(corpus):
    mem, repo, st = corpus
    rep = run_review(cfg_for(mem, repo), MemorySource(st), now=rf.NOW)
    by = {f["id"]: f for f in rep["findings"]}
    assert by["broken_links"]["action"] == "relink"
    assert by["broken_links"]["items"][0]["suggestion"] == "client-onboarding-checklist"
    assert by["near_duplicates"]["action"] == "merge"
    assert by["naming"]["action"] == "rename"
    assert by["naming"]["items"][0]["expected"] == "team_calendar.md"
    assert by["stale_open"]["action"] == "close"
    assert by["description_drift"]["judgement"] and by["secrets"]["judgement"]
    assert rep["flags"]["print-partner"] == ["stale_open"]


def test_dead_paths_are_skipped_when_not_configured(corpus):
    mem, _repo, st = corpus
    rep = run_review(ReviewConfig(memory_dir=mem, giant_words=600), MemorySource(st), now=rf.NOW)
    assert "dead_paths" in rep["skipped"]
    assert not any(f["id"] == "dead_paths" for f in rep["findings"])


def test_unreadable_index_is_a_finding_not_a_crash(tmp_path):
    mem, repo = rf.build(tmp_path)
    rep = run_review(cfg_for(mem, repo), SqliteSource(tmp_path / "nope.db"), now=rf.NOW)
    assert rep["findings"][0]["id"] == "index_unreadable"


def test_sqlite_source_2x_index(tmp_path):
    """The 2.x SQLite file: notes / chunks (JSON embeddings), no history."""
    mem, repo = rf.build(tmp_path)
    db = tmp_path / "memory.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE notes(name TEXT PRIMARY KEY, description TEXT, type TEXT, "
                      "mtime REAL, source_path TEXT, priority INTEGER DEFAULT 0);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, note_name TEXT, chunk_idx "
                      "INTEGER, body TEXT, embedding TEXT);")
    emb = rf.BowEmbedder()
    for n in load_corpus(mem):
        if n.name == "late-note":
            continue
        con.execute("INSERT INTO notes VALUES(?,?,?,?,?,0)",
                    (n.name, n.description, n.type, n.mtime, str(n.path)))
        con.execute("INSERT INTO chunks(note_name, chunk_idx, body, embedding) VALUES(?,0,?,?)",
                    (n.name, n.body, json.dumps(emb.embed_docs([n.body])[0])))
    con.commit()
    con.close()
    rep = run_review(cfg_for(mem, repo, accept_kebab_files=True), SqliteSource(db), now=rf.NOW)
    ids = {f["id"] for f in rep["findings"]}
    assert "near_duplicates" in ids and "index_desync" not in ids
    assert "description_drift" not in ids          # no history in 2.x


def test_score_and_signature():
    f = [{"id": "a", "severity": "crit", "count": 1, "notes": ["x"]}]
    assert health_score([]) == 100
    assert health_score(f) == 100 - round(18 * (1 + 0.693 / 3))
    assert health_score(f * 10) == 0


def test_suggest_target():
    names = ["client-onboarding-checklist", "client-onboarding", "pricing-2026"]
    assert suggest_target("onboarding-checklist", names, {}) == "client-onboarding-checklist"
    # two candidates equally close: no guess
    assert suggest_target("client-onboard", ["client-onboarding", "client-onboarded"], {}) is None
    assert suggest_target("old-name", names, {"old-name": "pricing-2026"}) == "pricing-2026"
    assert suggest_target("something-else", names, {}) is None


# ------------------------------------------------------------------------- chain checks

FAKE_MCP = textwrap.dedent("""
    import json, sys
    tools = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] else []
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("id") == 1:
            print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
        elif m.get("id") == 2:
            print(json.dumps({"jsonrpc": "2.0", "id": 2,
                              "result": {"tools": [{"name": t} for t in tools]}}), flush=True)
""")


@pytest.fixture()
def health_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_chain_all_good(tmp_path, health_server):
    srv = tmp_path / "srv.py"
    srv.write_text(FAKE_MCP)
    conf = tmp_path / "mcp.json"
    conf.write_text(json.dumps({"mcpServers": {"mem": {"command": "x"}}}))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "python recall_hook.py"}]}]}}))
    out = chain_checks(ChainConfig(
        server_name="mem", mcp_configs=[("project", conf)],
        launch={"command": sys.executable, "args": [str(srv), "memory_search,memory_get"]},
        hook_settings=[settings], hook_pattern="recall_hook", expect_hook=True,
        embed_urls=[health_server]))
    assert out == []


def test_chain_failures(tmp_path, health_server):
    srv = tmp_path / "srv.py"
    srv.write_text(FAKE_MCP)
    conf = tmp_path / "mcp.json"
    conf.write_text(json.dumps({"mcpServers": {"browser": {"command": "x"}}}))
    out = chain_checks(ChainConfig(
        server_name="mem", mcp_configs=[("project", conf)],
        launch={"command": sys.executable, "args": [str(srv), "memory_search"]},
        hook_settings=[tmp_path / "missing.json"], hook_pattern="recall_hook", expect_hook=True,
        embed_urls=["http://127.0.0.1:9", health_server], handshake_timeout=10))
    ids = {f.id: f for f in out}
    assert set(ids) == {"mcp_not_declared", "mcp_unreachable", "hook_missing", "embed_fallback"}
    assert "memory_get" in ids["mcp_unreachable"].detail
    out = chain_checks(ChainConfig(embed_urls=["http://127.0.0.1:9"],
                                   launch={"command": "/nonexistent/server"}))
    assert {f.id for f in out} == {"mcp_unreachable", "embed_down"}


def test_chain_findings_count_in_the_report(corpus, tmp_path):
    mem, repo, st = corpus
    chain = chain_checks(ChainConfig(embed_urls=["http://127.0.0.1:9"]))
    rep = run_review(cfg_for(mem, repo), MemorySource(st), chain=chain, now=rf.NOW)
    assert rep["findings"][0]["category"] == "chain"


# ------------------------------------------------------------------------- history, alerts

def _rep(at, findings, score=50):
    from core.review import signature
    return {"at": at, "score": score, "signature": signature(findings), "notes_total": 3,
            "counts": {s: sum(1 for f in findings if f["severity"] == s)
                       for s in ("crit", "warn", "info")}, "findings": findings}


def test_sqlite_history_tracks_problems(tmp_path):
    h = SqliteHistory(tmp_path / "h.db")
    a = {"id": "orphans", "severity": "info", "count": 2, "notes": ["x", "y"], "title": "t",
         "detail": "d"}
    h.record(_rep("2026-10-01T08:00:00+00:00", [a]))
    a2 = dict(a, notes=["y"], count=1)
    h.record(_rep("2026-10-09T08:00:00+00:00", [a2]))
    assert [p["note"] for p in h.open_problems()] == ["y"]
    s = h.summary(now=datetime.datetime(2026, 10, 9, 9, tzinfo=datetime.timezone.utc))
    assert s == {"open": 1, "open_over_7d": 1, "fixed": 1, "mean_fix_hours": 192.0}
    assert [p["score"] for p in h.series()] == [50, 50]
    assert h.last_report()["at"].startswith("2026-10-09")


def test_alert_decision(tmp_path):
    h = SqliteHistory(tmp_path / "h.db")
    pol = AlertPolicy(8, 20, 6.0, "UTC")
    warn = {"id": "orphans", "severity": "warn", "count": 1, "notes": ["x"], "title": "t",
            "detail": "d"}
    crit = {"id": "secrets", "severity": "crit", "count": 1, "notes": ["k"], "title": "Secret",
            "detail": "d"}
    t = datetime.datetime(2026, 10, 3, 7, 0, tzinfo=datetime.timezone.utc)
    r1 = _rep(t.isoformat(), [warn])
    assert alert_decision(r1, h, pol, now=t) is None              # before the digest slot
    assert alert_decision(r1, h, pol, now=t.replace(hour=9)) == "digest"
    mark_alerted(r1, h, "digest", now=t.replace(hour=9))
    assert alert_decision(r1, h, pol, now=t.replace(hour=10)) is None   # same findings
    r2 = _rep(t.isoformat(), [crit, warn])
    assert alert_decision(r2, h, pol, now=t.replace(hour=11)) is None   # cooldown 6 h
    assert alert_decision(r2, h, pol, now=t.replace(hour=16)) == "critical"
    mark_alerted(r2, h, "critical", now=t.replace(hour=16))
    r3 = _rep(t.isoformat(), [crit])                                     # warn fixed
    assert alert_decision(r3, h, pol, now=t.replace(hour=23)) is None   # crit already known
    assert alert_decision(r3, h, pol, now=t + datetime.timedelta(days=1, hours=2)) == "digest"
    title, body = format_digest(r2, "https://cockpit.example/?tab=corthexis", "critical")
    assert "critical" in title and "Secret" in body and body.endswith("corthexis")


# ------------------------------------------------------------------------- repairs

def _review(mem, repo, st):
    return run_review(cfg_for(mem, repo), MemorySource(st), now=rf.NOW)


def test_relink_repair(corpus):
    mem, repo, st = corpus
    p = repair.propose_relink(mem, "onboarding-checklist", "client-onboarding-checklist")
    assert [c.path for c in p.changes] == ["pricing_2026.md"]
    assert "-Every new client gets the [[onboarding-checklist]]" in p.changes[0].diff
    assert "+Every new client gets the [[client-onboarding-checklist]]" in p.changes[0].diff
    repair.apply(mem, p)
    assert "broken_links" not in {f["id"] for f in _review(mem, repo, st)["findings"]}


def test_relink_leaves_code_alone(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n[[old]] and `[[old]]`\n")
    (tmp_path / "b.md").write_text("---\nname: b\ndescription: d\n---\nb\n")
    p = repair.propose_relink(tmp_path, "old", "b")
    assert p.changes[0].after.endswith("[[b]] and `[[old]]`\n")


def test_merge_repair_with_diff_and_backup(corpus, tmp_path):
    mem, repo, st = corpus
    p = repair.propose_merge(mem, "printer-setup", "printer-configuration", now=rf.NOW)
    kinds = {c.path: (c.before is not None, c.after is not None) for c in p.changes}
    assert kinds["printer_setup.md"] == (True, True)
    assert kinds["printer_configuration.md"] == (True, False)
    keep = next(c for c in p.changes if c.path == "printer_setup.md")
    assert "## Merged from printer-configuration" in keep.after
    assert "modified: 2026-10-03" in keep.after
    assert "+## Merged from printer-configuration" in keep.diff
    backup = tmp_path / "trash"
    repair.apply(mem, p, backup_dir=backup)
    assert not (mem / "printer_configuration.md").exists()
    assert len(list(backup.rglob("*.md"))) == 2
    assert "near_duplicates" not in {f["id"] for f in _review(mem, repo, st)["findings"]}


def test_rename_repair_rewrites_links(corpus):
    mem, repo, st = corpus
    p = repair.propose_rename(mem, "Team Calendar")
    paths = {c.path: c for c in p.changes}
    assert paths["TeamCalendar.md"].after is None
    assert "name: team-calendar" in paths["team_calendar.md"].after
    repair.apply(mem, p)
    assert (mem / "team_calendar.md").exists()
    ids = {f["id"] for f in _review(mem, repo, st)["findings"]}
    assert "naming" not in ids and "broken_links" in ids   # pricing-2026's link, not this one


def test_close_repair(corpus):
    mem, repo, st = corpus
    p = repair.propose_close(mem, "print-partner", now=rf.NOW)
    after = p.changes[0].after
    assert "> **Closed on 2026-10-03**" in after and "  modified: 2026-10-03" in after
    assert after.count("modified:") == 1
    repair.apply(mem, p)
    assert "stale_open" not in {f["id"] for f in _review(mem, repo, st)["findings"]}


def test_apply_refuses_a_file_changed_since_the_proposal(corpus):
    mem, _repo, _st = corpus
    p = repair.propose_close(mem, "print-partner", now=rf.NOW)
    f = mem / "print_partner.md"
    f.write_text(f.read_text() + "\nA sister session wrote this.\n")
    with pytest.raises(repair.Conflict):
        repair.apply(mem, p)
    assert "Closed on" not in f.read_text()


def test_proposal_roundtrip():
    c = repair.FileChange("a.md", "x\n", "y\n")
    p = repair.Proposal("close", "t", "s", [c], {"name": "a"})
    q = repair.Proposal.from_dict(json.loads(json.dumps(p.to_dict())))
    assert q.id == p.id and q.changes[0].diff == c.diff


def test_set_frontmatter_field_variants():
    t = "---\nname: a\ndescription: d\n---\nbody\n"
    assert "metadata:\n  modified: 2026" in repair.set_frontmatter_field(t, "modified", "2026",
                                                                         meta=True)
    t2 = "---\nname: a\nmetadata:\n  type: project\n  modified: 2020-01-01\n---\nbody\n"
    out = repair.set_frontmatter_field(t2, "modified", "2026-10-03", meta=True)
    assert "  modified: 2026-10-03" in out and "2020" not in out and "  type: project" in out
    assert repair.set_frontmatter_field("body only\n", "name", "x").startswith("---\nname: x\n---")


# ------------------------------------------------------------------------- Postgres

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


@pytest.fixture()
def pg_store():
    psycopg = pytest.importorskip("psycopg")
    from core.store import Store
    name = "review_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    s = Store(DSN.rsplit("/", 1)[0] + "/" + name)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pg
def test_pg_false_positive_rate_and_knn(pg_store, tmp_path):
    from core.review import PgSource
    mem, repo = rf.build(tmp_path)
    rf.index(pg_store, mem)
    src = PgSource(pg_store)
    rep = run_review(cfg_for(mem, repo), src, now=rf.NOW)
    fp_rate, fps, missed = rf.false_positive_rate(rep)
    print(f"\nfixture corpus (Postgres): FP {fp_rate:.1%} {sorted(fps)}, missed {sorted(missed)}")
    assert fp_rate < TARGET_FP and not missed
    # the k-NN pairs are the exact ones
    exact = MemorySource(InMemoryStore())
    pairs = src.near_duplicates(0.86, 5)
    cents = src.centroids()
    from core.review import _cos_pairs_exact
    assert [(a, b) for a, b, _ in pairs] == [(a, b) for a, b, _ in _cos_pairs_exact(cents, 0.86)]
    assert exact.label == "in-memory store"


@pg
def test_pg_knn_uses_the_hnsw_index(pg_store, tmp_path):
    """Above the exact-scan size the neighbour query must go through HNSW (not O(n²))."""
    from core.review import PgSource
    mem, repo = rf.build(tmp_path)
    rf.index(pg_store, mem)
    g = pg_store.active_generation()
    assert g.index_built
    with pg_store.pool.connection() as con, con.transaction():
        con.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in con.execute(
            f"EXPLAIN SELECT note_id FROM {g.table} ORDER BY (embedding::halfvec({g.dim})) <#> "
            f"(SELECT avg(embedding::vector) FROM {g.table})::halfvec({g.dim}) LIMIT 15"))
    assert "hnsw" in plan.lower()
    assert PgSource(pg_store).renames() == {}


@pg
def test_pg_history(pg_store):
    from core.review import PgHistory
    h = PgHistory(pg_store)
    a = {"id": "orphans", "severity": "info", "count": 2, "notes": ["x", "y"], "title": "t",
         "detail": "d"}
    h.record(_rep("2026-10-01T08:00:00+00:00", [a]))
    h.record(_rep("2026-10-09T08:00:00+00:00", [dict(a, notes=["y"], count=1)]))
    assert [p["note"] for p in h.open_problems()] == ["y"]
    s = h.summary(now=datetime.datetime(2026, 10, 9, 9, tzinfo=datetime.timezone.utc))
    assert s["open_over_7d"] == 1 and s["mean_fix_hours"] == 192.0
    assert len(h.series()) == 2 and h.last_report()["notes_total"] == 3
    h.set("k", "v")
    h.set("k", "w")
    assert h.get("k") == "w" and h.get("nope") is None


@pg
def test_pg_renames_feed_link_suggestions(pg_store, tmp_path):
    from core.review import PgSource
    mem, repo = rf.build(tmp_path)
    rf.index(pg_store, mem)
    # the note is renamed by hand (file + name), the old name stays cited
    old = mem / "lonely_fact.md"
    text = old.read_text().replace("name: lonely-fact", "name: meeting-room")
    old.unlink()
    (mem / "meeting_room.md").write_text(text)
    (mem / "untyped_note.md").write_text((mem / "untyped_note.md").read_text()
                                         + "\nMeetings: [[lonely-fact]].\n")
    from core.indexer import IndexConfig, Indexer
    Indexer(pg_store, rf.BowEmbedder(), IndexConfig(memory_dir=mem, normalize=False,
                                                     write_index=False),
            clock=lambda: rf.NOW, log=lambda m: None).run()
    assert PgSource(pg_store).renames().get("lonely-fact") == "meeting-room"
    rep = run_review(cfg_for(mem, repo), PgSource(pg_store), now=rf.NOW)
    bl = next(f for f in rep["findings"] if f["id"] == "broken_links")
    assert {"note": "untyped-note", "target": "lonely-fact",
            "suggestion": "meeting-room"} in bl["items"]


def test_cli_entrypoint_runs(tmp_path, monkeypatch, capsys):
    from core import review
    mem, _repo = rf.build(tmp_path)
    monkeypatch.setenv("CORTHEXIS_MEMORY_DIR", str(mem))
    monkeypatch.delenv("CORTHEXIS_DATABASE_URL", raising=False)
    monkeypatch.delenv("SOKKAN_DATABASE_URL", raising=False)
    assert review.main([]) == 1          # secrets + no description = critical
    out = json.loads(capsys.readouterr().out)
    assert out["notes_total"] == len(list(Path(mem).glob("*.md")))
