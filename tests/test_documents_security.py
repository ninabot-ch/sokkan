"""Document drop (feature `documents`) through the real request path: what is refused and
why — path traversal, a disguised type, an oversize body, another project — and what an
accepted document leaves behind (place, text copy, journal)."""
import io
import os
import zipfile

import pytest

import simple_fixtures as _fx
from simple_fixtures import DEFAULT_SID, PDF, RADIO_SID, docx, pptx, put, xlsx

world = _fx.world      # the shared fixture (people, projects, no model)


def _stored(world_):
    return [p for p in world_["tmp"].rglob("*") if p.is_file() and "documents" in p.parts
            and p.suffix not in (".db", ".json", ".key")]


# ---- accepted --------------------------------------------------------------------------------
def test_upload_is_stored_in_the_project_space_with_a_text_copy_and_logged(world):
    c = world["as"]("bob@x")
    r = put(c, "Brief client.docx", docx(("# Brief", "Douze assistantes, deux jours.")),
            modified=1_700_000_000_000)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "Brief client.docx" and d["ext"] == "docx" and d["text_state"] == "ok"
    assert d["modified_at"] == 1_700_000_000 and "path" not in d
    folder = world["workspace"] / "documents" / d["id"]
    assert (folder / "Brief client.docx").is_file()
    assert "Douze assistantes" in (folder / "Brief client.docx.txt").read_text()
    ev = world["audit"]("document.upload")
    assert ev and ev[0]["user"] == "bob@x" and ev[0]["resource"] == d["id"] and ev[0]["project"] == "default"
    listed = c.get("/api/documents").json()["documents"]
    assert [x["id"] for x in listed] == [d["id"]]
    dl = c.get(f"/api/documents/{d['id']}/content")
    assert dl.status_code == 200 and dl.headers["content-disposition"].startswith("attachment")
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert world["audit"]("document.download")


@pytest.mark.parametrize("name,data", [
    ("Référentiel 2025.pdf", PDF), ("Module 3.pptx", pptx()), ("Programme.xlsx", xlsx()),
    ("notes.txt", "Rappel : salle B.\n".encode()), ("LISEZ-MOI.MD", b"# Titre\n\ntexte\n"),
])
def test_every_accepted_type(world, name, data):
    r = put(world["as"]("bob@x"), name, data)
    assert r.status_code == 200, r.text
    assert r.json()["ext"] == name.rsplit(".", 1)[1].lower()


# ---- path traversal --------------------------------------------------------------------------
@pytest.mark.parametrize("evil,kept", [
    ("../../../etc/cron.d/x.txt", "x.txt"),
    ("..\\..\\windows\\system32\\evil.md", "evil.md"),
    ("/etc/passwd.txt", "passwd.txt"),
    ("....//....//secret.txt", "secret.txt"),
    (".hidden.txt", "hidden.txt"),
    ("a\x00b/../c.txt", "c.txt"),
    ("%2e%2e%2fup.txt", "%2e%2e%2fup.txt"),
    ("x" * 400 + ".txt", None),
])
def test_a_name_is_a_label_never_a_path(world, evil, kept):
    c = world["as"]("bob@x")
    before = set(world["tmp"].rglob("*"))
    r = put(c, evil, b"contenu\n")
    assert r.status_code == 200, r.text
    d = r.json()
    if kept:
        assert d["name"] == kept
    assert "/" not in d["name"] and "\\" not in d["name"] and not d["name"].startswith(".")
    assert len(d["name"]) <= 120
    new = [p for p in set(world["tmp"].rglob("*")) - before if p.is_file() and p.suffix in (".txt", ".md")]
    docs = (world["workspace"] / "documents").resolve()
    assert new and all(docs in p.resolve().parents for p in new)
    assert not os.path.exists("/etc/cron.d/x.txt")


def test_ids_are_not_paths(world):
    c = world["as"]("bob@x")
    (world["workspace"] / "secret.txt").write_text("workspace secret")
    for bad in ("..", "../secret.txt", "..%2Fsecret.txt", "%2e%2e", "0" * 15, "g" * 16, "a" * 17):
        for suffix in ("", "/content", "/text"):
            r = c.get(f"/api/documents/{bad}{suffix}")
            assert r.status_code in (404, 405), (bad, suffix, r.status_code)
            assert "workspace secret" not in r.text


# ---- disguised type --------------------------------------------------------------------------
def _plain_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("payload.exe", b"MZ\x90\x00")
    return buf.getvalue()


@pytest.mark.parametrize("name,data", [
    ("invoice.pdf", b"MZ\x90\x00\x03\x00\x00\x00 this is a Windows executable"),
    ("photo.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
    ("archive.docx", _plain_zip()),
    ("slides.pptx", docx()),                        # a Word package called .pptx
    ("sheet.xlsx", b"PK\x03\x04 not really a zip"),
    ("macro.docx", docx(extra={"word/vbaProject.bin": b"\x00\x01"})),
    ("tool.txt", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32),
    ("blob.md", b"some text\x00\x00\x01\x02 binary tail"),
    ("script.txt", b"#!/bin/sh\nrm -rf /\n"),
    ("empty.pdf", b""),
])
def test_content_must_match_the_extension(world, name, data):
    c = world["as"]("bob@x")
    r = put(c, name, data)
    assert r.status_code in (400, 415), (name, r.status_code, r.text)
    assert r.json()["detail"]["code"] in ("disguised", "empty")
    assert c.get("/api/documents").json()["documents"] == []
    assert not _stored(world), "a refused file must not stay on disk"
    assert world["audit"]("document.refused")[0]["user"] == "bob@x"


@pytest.mark.parametrize("name", ["run.exe", "page.html", "a.pdf.exe", "noextension", "x.docm",
                                  "y.svg", "z.zip", "shell.sh", ".txt", "evil.txt/"])
def test_types_outside_the_list_are_refused_before_a_byte_is_stored(world, name):
    c = world["as"]("bob@x")
    r = put(c, name, b"%PDF-1.4 whatever")
    assert r.status_code == 415, (name, r.status_code)
    assert r.json()["detail"]["code"] == "type"
    assert not _stored(world)


def test_a_zip_bomb_shaped_package_is_refused(world, monkeypatch):
    import documents
    monkeypatch.setattr(documents, "MAX_UNZIPPED", 1024 * 1024)
    big = docx(extra={"word/media/pad.bin": b"\x00" * (3 * 1024 * 1024)})
    assert len(big) < 100_000          # tiny on the wire, 3 MB once expanded
    r = put(world["as"]("bob@x"), "bomb.docx", big)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_large"
    assert not _stored(world)


# ---- size ------------------------------------------------------------------------------------
def test_oversize_is_refused_on_the_declared_length_and_while_streaming(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_DOCUMENTS_MAX_MB", "1")
    c = world["as"]("bob@x")
    big = b"a" * (1024 * 1024 + 1)
    r = put(c, "gros.txt", big)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_large"

    def chunks():                       # no Content-Length: only the stream can tell
        for _ in range(40):
            yield b"b" * 65536
    r = c.put("/api/documents", params={"name": "flux.txt"}, content=chunks())
    assert r.status_code == 413, r.text
    assert not _stored(world)
    assert len(world["audit"]("document.refused")) == 2
    assert put(c, "ok.txt", b"a" * 1024).status_code == 200      # just under: accepted


def test_project_quota(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_DOCUMENTS_MAX_PER_PROJECT", "2")
    c = world["as"]("bob@x")
    assert put(c, "a.txt", b"a").status_code == 200
    assert put(c, "b.txt", b"b").status_code == 200
    r = put(c, "c.txt", b"c")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "quota"


# ---- roles and projects ----------------------------------------------------------------------
def test_another_projects_document_does_not_exist(world):
    alice = world["as"]("alice@x", "radio")
    d = put(alice, "Grille radio.txt", b"antenne 6h-9h\n").json()
    assert (world["tmp"] / "projects" / "radio" / "work" / "documents" / d["id"]).is_dir()
    # bob has no role in `radio`: neither through the id, nor by naming the project
    bob = world["as"]("bob@x")
    for path in ("", "/content", "/text"):
        assert bob.get(f"/api/documents/{d['id']}{path}").status_code == 404
    assert bob.delete(f"/api/documents/{d['id']}").status_code == 404
    assert bob.post(f"/api/documents/{d['id']}/attach", json={"session_id": DEFAULT_SID}).status_code == 404
    assert d["id"] not in [x["id"] for x in bob.get("/api/documents").json()["documents"]]
    bob_radio = world["as"]("bob@x", "radio")
    assert bob_radio.get("/api/documents").status_code == 404
    assert put(bob_radio, "intrus.txt", b"x").status_code == 404
    # alice cannot push a default-project document into her radio session either
    mine = put(world["as"]("bob@x"), "interne.txt", b"interne\n").json()
    alice = world["as"]("alice@x", "radio")
    assert alice.post(f"/api/documents/{mine['id']}/attach", json={"session_id": RADIO_SID}).status_code == 404
    assert alice.get(f"/api/documents/{mine['id']}/content").status_code == 404


def test_a_viewer_reads_but_does_not_add_or_remove(world):
    d = put(world["as"]("bob@x"), "plan.txt", b"plan\n").json()
    carol = world["as"]("carol@x")
    assert carol.get(f"/api/documents/{d['id']}/content").status_code == 200
    assert put(carol, "x.txt", b"x").status_code == 403
    assert carol.delete(f"/api/documents/{d['id']}").status_code == 403
    # another dev does not remove bob's document; an admin does
    assert world["as"]("dave@x").delete(f"/api/documents/{d['id']}").status_code == 403
    assert world["as"]("admin@x").delete(f"/api/documents/{d['id']}").status_code == 200
    assert world["as"]("bob@x").get(f"/api/documents/{d['id']}").status_code == 404
    assert not (world["workspace"] / "documents" / d["id"]).exists()
    assert world["audit"]("document.delete")[0]["user"] == "admin@x"


def test_attach_only_to_ones_own_session_of_the_same_project(world):
    c = world["as"]("bob@x")
    r = put(c, "brief.txt", b"brief\n", session=DEFAULT_SID)
    assert r.status_code == 200 and r.json()["attached_to"] == DEFAULT_SID
    import documents
    assert [d["name"] for d in documents.of_session(DEFAULT_SID)] == ["brief.txt"]
    # dave (same project, not the owner) cannot feed bob's session
    assert put(world["as"]("dave@x"), "x.txt", b"x", session=DEFAULT_SID).status_code == 403
    # nor can bob attach to a session of another project
    assert put(world["as"]("bob@x"), "y.txt", b"y", session=RADIO_SID).status_code == 404


def test_classified_documents_follow_the_clearance(world, monkeypatch):
    import classification
    import documents
    d = put(world["as"]("bob@x"), "strategie.txt", b"confidentiel\n").json()
    con = documents._con()
    con.execute("UPDATE documents SET level=? WHERE id=?", (documents._default_level() + 1, d["id"]))
    con.commit()
    con.close()
    monkeypatch.setattr(classification, "enabled", lambda: True)
    monkeypatch.setattr(classification, "clearance", lambda user, project: documents._default_level())
    bob = world["as"]("bob@x")
    assert bob.get(f"/api/documents/{d['id']}/content").status_code == 404
    assert bob.get("/api/documents").json()["documents"] == []
    monkeypatch.setattr(classification, "clearance", lambda user, project: documents._default_level() + 1)
    assert bob.get(f"/api/documents/{d['id']}").status_code == 200
    # readable by the cleared person, but never handed to an agent session
    r = bob.post(f"/api/documents/{d['id']}/attach", json={"session_id": DEFAULT_SID})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "classified"


def test_feature_off_means_no_route(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_FEATURE_DOCUMENTS", "0")
    c = world["as"]("bob@x")
    assert c.get("/api/documents").status_code == 404
    assert put(c, "a.txt", b"a").status_code == 404
    assert c.get("/api/documents/limits").status_code == 404
    assert not _stored(world)


def test_safe_name_unit():
    import documents
    assert documents.safe_name("Plan de  formation\t– v2.DOCX") == ("Plan de formation – v2.docx", "docx")
    assert documents.safe_name("con.txt")[0] == "con-document.txt"
    assert documents.safe_name('a<b>:"c"|d?.md')[0] == "a b c d.md"
    assert documents.safe_name("été – élève.pdf")[0] == "été – élève.pdf"
    with pytest.raises(documents.DocumentError):
        documents.safe_name("")
