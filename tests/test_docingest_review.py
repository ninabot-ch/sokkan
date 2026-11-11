"""doc_ingest — quarantine by batch through the real request path: sample, statistics,
approve / reject by batch or note by note, trust per source, and a journal of every decision."""
import json

import pytest

from docingest_helpers import FakeLLM, write_sources


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    for v in ("SOKKAN_DOC_INGEST_DIR", "SOKKAN_DOC_INGEST_CONFIG", "SOKKAN_MEMORY_QUARANTINE_DIR",
              "SOKKAN_DOC_INGEST_REVIEW_MIN_SAMPLE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SOKKAN_FEATURE_DOC_INGEST", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_AGENTS", "1")
    monkeypatch.setenv("SOKKAN_DOC_INGEST_CONCURRENCY", "1")
    import app as a
    import audit
    import auth
    import iam
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    for e, r in (("bob@x", "dev"), ("carol@x", "viewer"), ("admin@x", "admin")):
        iam.upsert_user(e, r)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    from docingest import config, state
    state._ready.clear()
    docs = tmp_path / "docs"
    (docs / "pv").mkdir(parents=True)
    write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(docs),
                                   "allow": ["pv"], "trust_after": 2}])
    who = {"email": "bob@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    c = TestClient(a.app)

    class W:
        root, memory, tmp = docs, tmp_path / "memory", tmp_path
        n = 0

        def as_(self, email):
            who["email"] = email
            return c

        def batch(self, docs_n=6, script=None, source="nas"):
            """One pass over `docs_n` NEW documents -> the batch id."""
            from docingest import runner
            for _ in range(docs_n):
                W.n += 1
                (docs / "pv" / f"pv-{W.n:03d}.md").write_text(
                    f"# PV de la séance {W.n}\n\nOrdre du jour de la séance {W.n} du comité.\n"
                    f"DECISION: la séance {W.n} valide le plan de formation du trimestre {W.n}\n")
            p = runner.Pass(config.get(source), client=FakeLLM(script),
                            memory_neighbours=lambda *x: []).run()
            assert p["status"] == "done", p
            return p["batch_id"]
    return W()


def _mem(world):
    return sorted(p.name for p in world.memory.glob("*.md")) if world.memory.exists() else []


def test_feature_off_is_404_and_roles_are_enforced(world, monkeypatch):
    bid = world.batch(2)
    c = world.as_("carol@x")                                    # viewer: reads nothing here
    assert c.get("/api/doc-ingest/batches").status_code == 403
    assert c.post(f"/api/doc-ingest/batches/{bid}/approve").status_code == 403
    c = world.as_("bob@x")                                      # dev: reviews, does not run
    assert c.get("/api/doc-ingest/batches").status_code == 200
    assert c.post("/api/doc-ingest/sources/nas/run").status_code == 403
    assert c.get("/api/doc-ingest/decisions").status_code == 403
    monkeypatch.setenv("SOKKAN_FEATURE_DOC_INGEST", "0")
    for url in ("/api/doc-ingest/status", "/api/doc-ingest/batches", f"/api/doc-ingest/batches/{bid}"):
        assert world.as_("admin@x").get(url).status_code == 404
    assert world.as_("admin@x").post(f"/api/doc-ingest/batches/{bid}/approve").status_code == 404


def test_statistics_then_a_sample_then_the_batch(world):
    bid = world.batch(6)
    c = world.as_("bob@x")
    lst = c.get("/api/doc-ingest/batches?status=pending").json()["batches"]
    assert [b["id"] for b in lst] == [bid] and lst[0]["counts"] == {"pending": 12}
    st = c.get(f"/api/doc-ingest/batches/{bid}").json()
    assert st["notes"] == 12 and st["by_type"] == {"reference": 6, "decision": 6}
    assert st["by_level"] == {"project": 12} and st["documents"] == 6 and st["flagged"] == 0
    assert st["pass"]["counters"]["noted"] == 6 and st["viewed_by_you"] == 0
    # approving a lot nobody looked at is refused — and nothing moved
    r = c.post(f"/api/doc-ingest/batches/{bid}/approve")
    assert r.status_code == 409 and "open at least 5 notes" in r.json()["detail"]
    assert _mem(world) == []
    # the sample: full text of the notes, recorded as seen by this person only
    s = c.get(f"/api/doc-ingest/batches/{bid}/sample?n=5").json()
    assert len(s["notes"]) == 5 and all(n["text"].startswith("---\nname: ") for n in s["notes"])
    assert s["viewed_by_you"] == 5
    assert world.as_("admin@x").post(f"/api/doc-ingest/batches/{bid}/approve").status_code == 409
    r = world.as_("bob@x").post(f"/api/doc-ingest/batches/{bid}/approve").json()
    assert r["approved"] == 12 and r["left_pending"] == [] and r["batch_status"] == "approved"
    files = _mem(world)
    assert len(files) == 12 and all("-" not in f for f in files), "file = name with underscores"
    text = (world.memory / files[0]).read_text()
    assert 'approved_by: "bob@x"' in text and "ingested_by" in text
    from core import notes as corenotes
    assert corenotes.parse_note(text, files[0]).yaml_ok
    # the quarantine directory of the batch is empty of notes
    from docingest import batches
    assert not list(batches.batch_dir("default", bid).glob("*.md"))


def test_note_by_note_and_the_journal(world):
    bid = world.batch(3)
    c = world.as_("bob@x")
    notes = c.get(f"/api/doc-ingest/batches/{bid}/notes").json()["notes"]
    a, b = notes[0]["name"], notes[1]["name"]
    assert c.post(f"/api/doc-ingest/batches/{bid}/notes/{a}/approve").json()["batch_status"] == "pending"
    r = c.post(f"/api/doc-ingest/batches/{bid}/notes/{b}/reject", json={"reason": "hors sujet"})
    assert r.status_code == 200
    assert c.post(f"/api/doc-ingest/batches/{bid}/notes/{b}/approve").status_code == 409
    assert c.post(f"/api/doc-ingest/batches/{bid}/notes/inconnue/approve").status_code == 404
    assert len(_mem(world)) == 1
    r = c.post(f"/api/doc-ingest/batches/{bid}/reject", json={"reason": "source mal choisie"}).json()
    assert r["rejected"] == 4 and r["batch_status"] == "partial"
    assert len(_mem(world)) == 1, "a rejected note never reaches the memory"
    # rejected files are kept aside (not deleted), out of the memory
    from docingest import batches, state
    assert len(list((batches.batch_dir("default", bid) / "rejected").glob("*.md"))) == 5
    # a rejected note is no longer a neighbour of anything
    assert state.index_get("default", b) is None and state.index_get("default", a)["status"] == "approved"
    # every decision is journaled: who, what, why — and exported for the study
    d = world.as_("admin@x").get(f"/api/doc-ingest/decisions?batch={bid}").json()["decisions"]
    got = [(x["user"], x["action"], x["note"], x["reason"]) for x in reversed(d)]
    assert got == [("bob@x", "approve", a, ""), ("bob@x", "reject", b, "hors sujet"),
                   ("bob@x", "reject_batch", "", "source mal choisie")]
    assert d[-1]["detail"]["type"] in ("reference", "decision") and d[0]["detail"] == {"rejected": 4}
    csv = world.as_("admin@x").get("/api/doc-ingest/decisions?format=csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].startswith("ts,iso,user,source,batch,note,action,reason")
    assert "hors sujet" in csv.text and len(csv.text.splitlines()) == 4
    import audit
    acts = [e["action"] for e in audit.recent(100)]
    assert "doc_ingest.approve" in acts and "doc_ingest.reject_batch" in acts


def _flagging(doc, user):
    from docingest_helpers import default_script
    out = default_script(doc, user)
    import re
    if int(re.search(r"séance (\d+)", doc).group(1)) % 3 == 2:
        out["decisions"][0]["quote"] = "une citation qui n'est pas dans le document"
    return out


def test_a_flagged_note_is_never_approved_in_bulk(world):
    bid = world.batch(3, _flagging)
    c = world.as_("bob@x")
    assert c.get(f"/api/doc-ingest/batches/{bid}").json()["flags"] == {"quote_not_found": 1}
    flagged = c.get(f"/api/doc-ingest/batches/{bid}/notes?flag=any").json()["notes"]
    assert len(flagged) == 1
    # the sample shows the flagged ones first
    s = c.get(f"/api/doc-ingest/batches/{bid}/sample?n=5").json()["notes"]
    assert flagged[0]["name"] in [n["name"] for n in s]
    r = c.post(f"/api/doc-ingest/batches/{bid}/approve").json()
    assert r["approved"] == 5 and r["left_pending"] == [flagged[0]["name"]]
    assert r["batch_status"] == "pending"
    # … only one by one, by someone who looked at it
    assert c.post(f"/api/doc-ingest/batches/{bid}/notes/{flagged[0]['name']}/approve"
                  ).json()["batch_status"] == "approved"


def test_trust_after_n_human_approved_batches_and_lost_on_a_rejection(world):
    from docingest import batches, config
    src = config.get("nas")
    c = world.as_("bob@x")
    assert batches.trust(src) == {"trusted": False, "need": 2, "have": 0,
                                  "reason": "0/2 consecutive batches approved by a person"}
    for i in (1, 2):
        bid = world.batch(3)
        assert batches.get(bid)["status"] == "pending", "not trusted yet: waits for a person"
        c.get(f"/api/doc-ingest/batches/{bid}/sample?n=5")
        assert c.post(f"/api/doc-ingest/batches/{bid}/approve").json()["batch_status"] == "approved"
        assert batches.trust(src)["have"] == i
    assert c.get("/api/doc-ingest/sources").json()["sources"][0]["trust"]["trusted"] is True
    # trusted: the next batch approves itself — unflagged notes only — and says so in the journal
    before = len(_mem(world))
    bid = world.batch(3, _flagging)
    st = c.get(f"/api/doc-ingest/batches/{bid}").json()
    assert st["by_status"] == {"approved": 5, "pending": 1} and len(_mem(world)) == before + 5
    d = world.as_("admin@x").get(f"/api/doc-ingest/decisions?batch={bid}").json()["decisions"]
    assert [(x["user"], x["action"]) for x in d] == [("policy:trust", "auto_approve_batch")]
    assert d[0]["detail"]["approved"] == 5 and d[0]["detail"]["left_pending"] == 1
    # batches the policy approved do not feed the trust; a human rejection takes it away
    assert batches.trust(src)["have"] == 2
    flagged = c.get(f"/api/doc-ingest/batches/{bid}/notes?status=pending").json()["notes"][0]["name"]
    c.post(f"/api/doc-ingest/batches/{bid}/notes/{flagged}/reject", json={"reason": "inventé"})
    assert batches.trust(src)["trusted"] is False and batches.trust(src)["have"] == 0
    bid = world.batch(2)
    assert batches.get(bid)["status"] == "pending" and c.get(
        f"/api/doc-ingest/batches/{bid}").json()["by_status"] == {"pending": 4}


def test_trust_off_by_default_and_capped_by_level(world):
    from docingest import batches, config
    write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(world.root),
                                   "allow": ["pv"]}])
    assert batches.trust(config.get("nas"))["reason"] == "trust policy off"
    # a trusted source whose notes are above trust_max_level: nothing approves itself
    write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(world.root),
                                   "allow": ["pv"], "trust_after": 1}])
    c = world.as_("bob@x")
    bid = world.batch(2)
    c.get(f"/api/doc-ingest/batches/{bid}/sample")
    c.post(f"/api/doc-ingest/batches/{bid}/approve")
    write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(world.root),
                                   "allow": ["pv"], "trust_after": 1,
                                   "classification": "confidential"}])
    bid = world.batch(2)
    assert batches.trust(config.get("nas"))["trusted"] is True
    assert c.get(f"/api/doc-ingest/batches/{bid}").json()["by_status"] == {"pending": 4}


def test_a_note_above_the_default_level_needs_the_store_and_nothing_is_half_written(world):
    """sqlite (2.x) index: a confidential note could not be indexed with its level — the
    approval is refused (409), the note stays in quarantine, the memory stays empty."""
    from docingest import config
    write_sources(config.home(), [{"id": "nas", "kind": "folder", "root": str(world.root),
                                   "allow": ["pv"], "classification": "confidential"}])
    bid = world.batch(2)
    c = world.as_("bob@x")
    name = c.get(f"/api/doc-ingest/batches/{bid}/notes").json()["notes"][0]["name"]
    r = c.post(f"/api/doc-ingest/batches/{bid}/notes/{name}/approve")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "memory_store_required"
    c.get(f"/api/doc-ingest/batches/{bid}/sample")
    r = c.post(f"/api/doc-ingest/batches/{bid}/approve").json()
    assert r["approved"] == 0 and len(r["failed"]) == 4 and r["batch_status"] == "pending"
    assert _mem(world) == []


def test_above_the_reviewers_clearance_does_not_exist():
    """Unit level (the API passes classification.ctx_clearance()): a reviewer cleared for
    `project` neither sees, samples nor approves a confidential note."""
    import tempfile

    import pytest as _p
    mp = _p.MonkeyPatch()
    tmp = __import__("pathlib").Path(tempfile.mkdtemp())
    try:
        mp.setenv("SOKKAN_DATA_DIR", str(tmp / "data"))
        mp.setenv("SOKKAN_MEMORY_DIR", str(tmp / "memory"))
        mp.delenv("SOKKAN_MEMORY_QUARANTINE_DIR", raising=False)
        mp.delenv("SOKKAN_DOC_INGEST_DIR", raising=False)
        import audit
        mp.setattr(audit, "DB", tmp / "audit.db")
        from docingest import batches, state
        state._ready.clear()
        bid = batches.open_batch("nas", "default", 0)
        for name, lvl in (("note-projet", 2), ("note-confidentielle", 3)):
            batches.put_note(bid, "default", "nas", f"{name}.md", name=name, kind="reference",
                             level=lvl, title=name, description=name,
                             text=f"---\nname: {name}\ndescription: \"x\"\nmetadata:\n  "
                                  "ingested_by: \"doc-ingest\"\n---\ncorps\n", body="corps", flags=[])
        batches.close_batch(bid)
        assert [n["name"] for n in batches.notes_of(bid, max_level=2)] == ["note-projet"]
        assert batches.note(bid, "note-confidentielle", max_level=2) is None
        assert batches.stats(bid, max_level=2)["hidden_above_clearance"] == 1
        assert [n["name"] for n in batches.sample(bid, "bob@x", max_level=2)] == ["note-projet"]
        with _p.raises(KeyError):
            batches.approve_note(bid, "note-confidentielle", "bob@x", max_level=2)
        out = batches.approve_batch(bid, "bob@x", max_level=2)
        assert out["approved"] == 1 and out["batch_status"] == "pending"
        assert batches.note(bid, "note-confidentielle")["status"] == "pending"
    finally:
        mp.undo()
        from docingest import state
        state._ready.clear()


def test_a_note_written_by_a_person_is_never_overwritten(world):
    from docingest import batches
    # before the pass: the agent picks another name
    world.memory.mkdir(parents=True, exist_ok=True)
    human = world.memory / "pv_de_la_seance_1.md"
    human.write_text("---\nname: pv-de-la-seance-1\ndescription: \"écrite à la main\"\n---\nà moi\n")
    bid = world.batch(1)
    names = [n["name"] for n in batches.notes_of(bid)]
    assert "pv-de-la-seance-1" not in names and "pv-de-la-seance-1-2" in names
    # after the pass, before the approval: someone writes a note with the agent's name
    ref = "pv-de-la-seance-1-2"
    squat = world.memory / "pv_de_la_seance_1_2.md"
    squat.write_text("---\nname: pv-de-la-seance-1-2\ndescription: \"aussi à la main\"\n---\nà moi\n")
    c = world.as_("bob@x")
    r = c.post(f"/api/doc-ingest/batches/{bid}/notes/{ref}/approve")
    assert r.status_code == 409 and "does not come from the ingestion" in r.json()["detail"]
    assert "à moi" in squat.read_text() and "à moi" in human.read_text()


def test_status_and_sources_say_what_is_missing(world, monkeypatch):
    monkeypatch.delenv("SOKKAN_DOC_INGEST_LLM_URL", raising=False)
    monkeypatch.delenv("SOKKAN_ASSISTANT_LLM_URL", raising=False)
    c = world.as_("admin@x")
    st = c.get("/api/doc-ingest/status").json()
    assert st["model"]["configured"] is False and "no model endpoint" in st["model"]["not_ready"]
    assert c.post("/api/doc-ingest/sources/nas/run").status_code == 409
    assert c.post("/api/doc-ingest/sources/inconnue/run").status_code == 404
    s = c.get("/api/doc-ingest/sources").json()["sources"][0]
    assert s["id"] == "nas" and s["allow"] == ["pv"] and s["classification"] == "project"
    assert json.dumps(s).count("rclone.conf") == 0
