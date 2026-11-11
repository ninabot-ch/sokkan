"""doc_ingest — extraction: Office and PDF to text through a child process that can die
without taking the pass with it."""
import stat
import sys
import zipfile

import pytest

pytest.importorskip("markitdown", reason="pip install -r backend/requirements-docingest.txt")


@pytest.fixture()
def ex(monkeypatch):
    from docingest.config import Limits
    from docingest.extract import Extractor
    monkeypatch.setenv("SOKKAN_DOC_INGEST_DOCLING", "off")
    made = []

    def make(**kw):
        e = Extractor(Limits(**kw))
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


def _docx(path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.core_properties.author = "Claire Dubois"
    d.core_properties.title = "PV du comité pédagogique"
    d.add_heading("PV du comité pédagogique", 1)
    d.add_paragraph("Séance du 12 mars 2026.")
    d.add_heading("Décisions", 2)
    d.add_paragraph("Le comité décide de plafonner les groupes à 12 participants.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Module", "Durée"
    t.cell(1, 0).text, t.cell(1, 1).text = "Excel avancé", "3 jours"
    d.save(path)


def test_docx_text_headings_and_metadata(tmp_path, ex):
    p = tmp_path / "pv.docx"
    _docx(p)
    r = ex().extract(p)
    assert r["ok"] and r["extractor"] == "markitdown"
    assert "plafonner les groupes à 12 participants" in r["text"] and "Excel avancé" in r["text"]
    assert r["headings"][:2] == ["PV du comité pédagogique", "Décisions"]
    assert r["meta"]["author"] == "Claire Dubois" and r["title"] == "PV du comité pédagogique"
    assert len(r["text_sha"]) == 64 and not r["truncated"]


def test_pptx_and_xlsx(tmp_path, ex):
    pptx = pytest.importorskip("pptx")
    openpyxl = pytest.importorskip("openpyxl")
    pr = pptx.Presentation()
    s = pr.slides.add_slide(pr.slide_layouts[1])
    s.shapes.title.text = "Catalogue 2027"
    s.placeholders[1].text = "Nouveau module : cybersécurité pour les PME"
    pr.save(tmp_path / "cat.pptx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Planning"
    ws.append(["Cours", "Salle", "Date"])
    ws.append(["Excel avancé", "B12", "2027-02-03"])
    wb.save(tmp_path / "plan.xlsx")
    e = ex()
    a, b = e.extract(tmp_path / "cat.pptx"), e.extract(tmp_path / "plan.xlsx")
    assert a["ok"] and "cybersécurité pour les PME" in a["text"]
    assert b["ok"] and "B12" in b["text"] and "Planning" in b["text"]


def test_pdf_with_text_and_pdf_without(tmp_path, ex):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    c = canvas.Canvas(str(tmp_path / "proc.pdf"))
    for i, line in enumerate(["Procédure d'annulation", "Toute annulation à moins de 10 jours "
                              "est facturée à 50 %.", "Validée par la direction le 3 mai 2026.",
                              "Elle s'applique à toutes les sessions inter-entreprises dès le "
                              "1er juin 2026 et remplace la version précédente."] * 3):
        c.drawString(72, 780 - 18 * i, line)
    c.save()
    c = canvas.Canvas(str(tmp_path / "scan.pdf"))        # a page with a drawing, no text layer
    c.rect(50, 50, 400, 600, fill=1)
    c.save()
    e = ex()
    r = e.extract(tmp_path / "proc.pdf")
    assert r["ok"] and "facturée à 50 %" in r["text"] and r["meta"]["pages"] == 1
    s = e.extract(tmp_path / "scan.pdf")
    # no text layer and no Docling: consigned as needing OCR — not an empty note, not a crash
    assert not s["ok"] and s["kind"] == "needs_ocr"


def test_limits_big_file_never_opened_long_text_truncated(tmp_path, ex):
    big = tmp_path / "big.txt"
    big.write_text("x" * 300_000)
    r = ex(max_file_mb=0.1).extract(big)
    assert not r["ok"] and r["kind"] == "too_big"
    long = tmp_path / "long.md"
    long.write_text("# Début\n\n" + "mot " * 20_000 + "\n\nDécision finale en dernière ligne.")
    r = ex(max_doc_chars=5000).extract(long)
    assert r["ok"] and r["truncated"] and len(r["text"]) < 5200
    assert r["text"].startswith("# Début") and r["text"].endswith("dernière ligne.")


def test_zip_bomb_and_broken_office_files(tmp_path, ex):
    bomb = tmp_path / "bombe.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "0" * (60 * 1024 * 1024))
    e = ex()
    r = e.extract(bomb)
    assert not r["ok"] and r["kind"] == "refused" and "decompresses" in r["error"]
    broken = tmp_path / "casse.pptx"
    broken.write_bytes(b"PK\x03\x04 pas un vrai zip")
    r = e.extract(broken)
    assert not r["ok"] and r["kind"] == "unreadable"
    ok = tmp_path / "ok.md"
    ok.write_text("# Encore là\n\nLe même extracteur lit le fichier suivant.")
    assert e.extract(ok)["ok"]


def _fake_python(tmp_path, body: str):
    exe = tmp_path / "fakepy"
    exe.write_text(f"#!{sys.executable}\nimport sys, time, os, json\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_a_parser_that_hangs_is_killed_and_the_next_file_is_read(tmp_path, monkeypatch):
    from docingest.config import Limits
    from docingest.extract import Extractor
    f = tmp_path / "a.md"
    f.write_text("# Un document\n\nDu texte.")
    e = Extractor(Limits(extract_timeout_s=1), python=_fake_python(tmp_path, "time.sleep(60)"))
    r = e.extract(f)
    assert not r["ok"] and r["kind"] == "timeout"
    assert e._p is None, "the hung worker was killed"
    e.python = sys.executable                       # the next request gets a fresh worker
    assert e.extract(f)["ok"]
    e.close()


def test_a_parser_that_dies_is_an_error_of_that_file(tmp_path):
    from docingest.config import Limits
    from docingest.extract import Extractor
    f = tmp_path / "a.md"
    f.write_text("# Un document\n\nDu texte.")
    e = Extractor(Limits(), python=_fake_python(tmp_path, "sys.stdin.readline(); os._exit(139)"))
    r = e.extract(f)
    assert not r["ok"] and r["kind"] == "crash"
    e.python = sys.executable
    assert e.extract(f)["ok"]
    e.close()


def test_the_worker_never_treats_a_path_as_a_url(tmp_path, monkeypatch):
    """MarkItDown.convert() accepts URLs; the worker only ever calls convert_local()."""
    import markitdown

    from docingest import extract_worker
    called = []
    monkeypatch.setattr(markitdown.MarkItDown, "convert",
                        lambda self, *a, **k: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("convert() must not be used")))
    monkeypatch.setattr(extract_worker, "_md", None)
    p = tmp_path / "page.html"
    p.write_text("<html><body><h1>Titre</h1><p>Contenu de la page interne.</p></body></html>")
    r = extract_worker.extract(str(p))
    assert r["ok"] and "Contenu de la page interne" in r["text"] and not called


def test_a_parser_that_eats_memory_is_killed_on_its_resident_size(tmp_path):
    """The limit is on RESIDENT memory, watched by the parent (an address-space rlimit made
    every extraction hang on a many-core host: ONNX Runtime reserves space per core)."""
    from docingest.config import Limits
    from docingest.extract import Extractor
    f = tmp_path / "a.md"
    f.write_text("# Un document\n\nDu texte.")
    hog = "sys.stdin.readline(); x = bytearray(300 * 1024 * 1024); time.sleep(60)"
    e = Extractor(Limits(extract_mem_mb=120, extract_timeout_s=30), python=_fake_python(tmp_path, hog))
    r = e.extract(f)
    assert not r["ok"] and r["kind"] == "memory" and "limit 120 MB" in r["error"]
    assert e._p is None
    e.python = sys.executable
    e.limits = Limits()
    assert e.extract(f)["ok"]
    e.close()


def test_the_worker_sets_no_address_space_limit():
    import inspect

    from docingest import extract_worker
    assert "setrlimit" not in inspect.getsource(extract_worker)
