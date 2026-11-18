"""4.0 native formats — Excel with real formulas, Word on the organisation's template, one
document per recipient, PDF, and the new inputs (csv, eml, msg, photos).

Every produced file is OPENED with the library a person's software relies on (openpyxl,
python-docx, pypdf) and checked: values, formulas, number formats, styles of the template."""
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from simple_fixtures import DEFAULT_SID, put, world  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import outputs  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")
docx = pytest.importorskip("docx")
pytest.importorskip("reportlab")

FINANCE = """---
format: xlsx
---
# Rapprochement novembre

## Factures
| Facture | Client | Montant | Payé | Reste |
|---|---|---|---|---|
| F-101 | Régie du Lac | 18'350.00 | 6'000 | =[Montant]-[Payé] |
| F-102 | Les Cèdres | CHF 8'120.50 | 8120.50 | =C3-D3 |
| F-103 | Hôtel Bellerive \\| acompte | 31'200 | 0 | =[Montant]-[Payé] |
| **Total** | | =SUM(ABOVE) | =SUM(ABOVE) | =SUM(ABOVE) |

À confirmer : virement de 1'250.00 non identifié.

## Notes de frais
| Date | Article | Montant | Plafond | Dépassement |
|---|---|---|---|---|
| 17.11.2026 | Repas (4 pers.) | 381.30 | 360 | =MAX(0,C2-D2) |
| 18.11.2026 | Parking | 25 | 20 | =MAX(0;[Montant]-[Plafond]) |
| Total | | =SUM(C2:C3) | | =SUM(ABOVE) |
"""

LETTERS = """---
format: letters
template: lettre
---
# Relances de novembre

Madame, Monsieur,

Notre facture {{Facture}} du {{Date}}, d'un montant de CHF {{Montant}}, reste ouverte à ce jour.

Nous vous prions de bien vouloir la régler d'ici au 30.11.2026.

## Destinataires
| Destinataire | Facture | Date | Montant |
|---|---|---|---|
| Régie du Lac SA | F-101 | 02.10.2026 | 12'350.00 |
| Riviera Immobilier | F-104 | 09.10.2026 | 4'200.00 |
| Garage Favre | F-107 | 15.10.2026 | 980.00 |
"""


def _xlsx(name, text):
    data = outputs.to_xlsx(name, text)
    return (openpyxl.load_workbook(io.BytesIO(data)),
            openpyxl.load_workbook(io.BytesIO(data), data_only=True))


# ---- Excel ---------------------------------------------------------------------------------
def test_xlsx_has_real_formulas_computed_values_and_swiss_formats():
    wb, vals = _xlsx("Rapprochement.md", FINANCE)
    assert wb.sheetnames == ["Factures", "Notes de frais"]
    ws, wv = wb["Factures"], vals["Factures"]
    # the title on row 1, the header on row 3: the agent's table coordinates were shifted
    assert ws["A1"].value == "Rapprochement novembre" and ws["A1"].font.b
    assert [c.value for c in ws[3]] == ["Facture", "Client", "Montant", "Payé", "Reste"]
    assert all(c.font.b for c in ws[3]) and ws.freeze_panes == "A4"
    assert ws["E4"].value == "=C4-D4" and ws["E5"].value == "=C5-D5" and ws["E6"].value == "=C6-D6"
    assert ws["C7"].value == "=SUM(C4:C6)" and ws["E7"].value == "=SUM(E4:E6)"
    # values stored with the formulas (a viewer that does not recalculate shows them)
    assert (wv["E4"].value, wv["E5"].value, wv["E6"].value) == (12350, 0, 31200)
    assert (wv["C7"].value, wv["D7"].value, wv["E7"].value) == (57670.5, 14120.5, 43550)
    # figures are numbers, written « 18'350.00 », « CHF 8'120.50 », « 31'200 » alike
    assert ws["C4"].value == 18350 and ws["C5"].value == 8120.5 and ws["C6"].value == 31200
    assert {ws[f"C{r}"].number_format for r in range(4, 8)} == {"#,##0.00"}
    assert ws["E7"].number_format == "#,##0.00" and ws["A7"].font.b
    assert ws["B6"].value == "Hôtel Bellerive | acompte"
    assert wb.calculation.fullCalcOnLoad
    fr = vals["Notes de frais"]
    # « ; » as argument separator (French Excel habit) and A1 counted inside the table
    assert wb["Notes de frais"]["E2"].value == "=MAX(0,C2-D2)"
    assert round(fr["E2"].value, 2) == 21.3 and fr["E3"].value == 5
    assert fr["C4"].value == pytest.approx(406.3) and fr["E4"].value == pytest.approx(26.3)
    assert isinstance(wb["Notes de frais"]["A2"].value, __import__("datetime").datetime)
    assert wb["Notes de frais"]["A2"].number_format == "DD.MM.YYYY"


def test_preview_and_calc_summary_show_the_computed_figures():
    pv = outputs.preview("Rapprochement.md", FINANCE)
    assert "format:" not in pv and "=SUM" not in pv and "=[" not in pv
    assert "| **Total** |  | **57'670.50** | **14'120.50** | **43'550.00** |" in pv
    assert "| F-101 | Régie du Lac | 18'350.00 | 6'000.00 | 12'350.00 |" in pv
    s = outputs.calc_summary("Rapprochement.md", FINANCE)
    assert "Factures · Total · Reste = 43'550.00" in s and "Notes de frais · Total · Dépassement = 26.30" in s


def test_formula_errors_never_break_the_file():
    src = ("---\nformat: xlsx\n---\n# T\n\n| A | B |\n|---|---|\n| 1 | =B2 |\n| 2 | =A3/0 |\n"
           "| 3 | =VLOOKUP(1,A2:A4,1) |\n| x | =A5*2 |\n")
    wb, vals = _xlsx("t.md", src)
    ws = wb.active
    assert ws["B4"].value == "=B4" and ws["B6"].value.startswith("=VLOOKUP")
    pv = outputs.preview("t.md", src)
    assert "(calculé à l'ouverture)" in pv


def test_a_csv_deliverable_with_an_unquoted_separator_is_repaired_and_exported_for_swiss_excel():
    csv_text = ("Date;Libellé;Montant;Client\n17.11.2026;Repas; boissons;95.33;Cèdres\n"
                "18.11.2026;Parking;25;\"Favre; Garage\"\nTotal;;=SUM(ABOVE);\n")
    rows = outputs.csv_rows(csv_text)
    assert rows[1] == ["17.11.2026", "Repas; boissons", "95.33", "Cèdres"]
    assert rows[2][3] == "Favre; Garage"
    wb, vals = _xlsx("Frais.csv", csv_text)
    ws = wb.active
    assert ws["A1"].value == "Date" and ws["C4"].value == "=SUM(C2:C3)" and vals.active["C4"].value == 120.33
    out = outputs.to_csv("Frais.csv", csv_text).decode("utf-8")
    assert out.startswith("﻿") and "\r\n" in out
    assert '"Repas; boissons"' in out and '"Favre; Garage"' in out
    assert "Total;;120.33" in out
    assert outputs.formats_for("Frais.csv", csv_text) == ["xlsx", "pdf", "csv"]


def test_formulas_in_a_word_document_table_are_computed_too():
    src = ("# Offre Bellerive\n\n| Poste | Qté | Prix | Montant |\n|---|---|---|---|\n"
           "| Capteurs | 40 | 120.50 | =[Qté]*[Prix] |\n| Pose | 1 | 3'200 | =[Qté]*[Prix] |\n"
           "| **Total** | | | **=SUM(ABOVE)** |\n")
    d = docx.Document(io.BytesIO(outputs.to_docx(src)))
    t = d.tables[0]
    assert [c.text for c in t.rows[1].cells] == ["Capteurs", "40", "120.50", "4'820.00"]
    assert t.rows[3].cells[3].text == "8'020.00" and t.rows[3].cells[3].paragraphs[0].runs[0].bold
    assert "8'020.00" in outputs.preview("Offre.md", src)


# ---- Word ----------------------------------------------------------------------------------
def test_docx_default_layout_titles_lists_tables_header_footer():
    src = ("---\nformat: docx\n---\n# Procès-verbal du comité\n\n## Décisions\n\n- **Budget** validé\n"
           "  - sous-point\n1. Première action\n\n| Action | Qui | Délai |\n|---|---|---|\n"
           "| Budget v1 | Sophie | 14.01.2027 |\n\nTexte *important* et `code`.\n")
    d = docx.Document(io.BytesIO(outputs.to_docx(src, None, "Morand Domotique SA")))
    styles = [p.style.name for p in d.paragraphs if p.text]
    assert styles[0] == "Title" and "Heading 1" in styles and "List Bullet" in styles
    assert "format:" not in "\n".join(p.text for p in d.paragraphs)
    t = d.tables[0]
    assert t.style.name == "Table Grid" and [c.text for c in t.rows[0].cells] == ["Action", "Qui", "Délai"]
    assert t.rows[0]._tr.xpath("./w:trPr/w:tblHeader")           # header repeated on each page
    sec = d.sections[0]
    assert sec.header.paragraphs[0].text == "Morand Domotique SA"
    assert "PAGE" in sec.footer._element.xml and "NUMPAGES" in sec.footer._element.xml
    assert round(sec.page_width.cm, 1) == 21.0 and d.core_properties.title == "Procès-verbal du comité"


def _template(tmp_path: Path, dotx: bool = True) -> Path:
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import RGBColor
    from PIL import Image
    t = docx.Document()
    t.styles["Heading 1"].font.color.rgb = RGBColor(0xC0, 0x10, 0x20)
    s = t.styles.add_style("Maison Corps", WD_STYLE_TYPE.PARAGRAPH)
    s.font.name = "Georgia"
    logo = tmp_path / "logo.png"
    Image.new("RGB", (240, 80), (12, 80, 160)).save(logo)
    hp = t.sections[0].header.paragraphs[0]
    hp.add_run().add_picture(str(logo))
    hp.add_run("  Morand Domotique SA")
    t.sections[0].footer.paragraphs[0].text = "Morand Domotique SA · Route de Lausanne 1 · 1023 Crissier"
    t.add_paragraph("CORPS DU MODELE A NE PAS GARDER")
    p = tmp_path / "maison.docx"
    t.save(p)
    if not dotx:
        return p
    out = tmp_path / "maison.dotx"
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in zin.infolist():
            data = zin.read(i.filename)
            if i.filename == "[Content_Types].xml":
                data = data.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.template.main+xml")
            zout.writestr(i, data)
    return out


def test_docx_on_the_house_template_keeps_its_styles_header_footer_and_logo(tmp_path):
    tpl = _template(tmp_path)
    d = docx.Document(io.BytesIO(outputs.to_docx("# Offre\n\n## Prix\n\nTexte.\n", tpl, "ignoré")))
    body = "\n".join(p.text for p in d.paragraphs)
    assert "CORPS DU MODELE" not in body and "Texte." in body
    assert d.styles["Heading 1"].font.color.rgb == docx.shared.RGBColor(0xC0, 0x10, 0x20)
    assert "Maison Corps" in [s.name for s in d.styles]
    hdr = d.sections[0].header
    assert "Morand Domotique SA" in hdr.paragraphs[0].text and "ignoré" not in hdr.paragraphs[0].text
    assert hdr.part.rels and any("image" in r.reltype for r in hdr.part.rels.values())
    assert "Crissier" in d.sections[0].footer.paragraphs[0].text
    fr = outputs.template_frame(tpl)
    assert fr["logo"] and fr["logo"][:4] == b"\x89PNG" and "Crissier" in fr["footer"]
    pdf = outputs.to_pdf("# Offre\n\nTexte.\n", tpl, "x", "Offre.md")
    assert pdf[:5] == b"%PDF-" and b"/Image" in pdf


# ---- one document per recipient -------------------------------------------------------------
def test_letters_template_form_gives_one_word_file_per_recipient_in_a_zip(tmp_path):
    title, ls = outputs.letters(LETTERS)
    assert title == "Relances de novembre" and [x.name for x in ls] == [
        "Régie du Lac SA", "Riviera Immobilier", "Garage Favre"]
    assert "F-104" in ls[1].body and "{{" not in ls[1].body and "Destinataires" not in ls[1].body
    tpl = _template(tmp_path, dotx=False)
    data, name, mime = outputs.render("letters_docx", "Relances.md", LETTERS,
                                      template_for=lambda k: tpl if k == "lettre" else None)
    assert name == "Relances.zip" and mime == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist() == ["01 - Régie du Lac SA.docx", "02 - Riviera Immobilier.docx",
                            "03 - Garage Favre.docx", "Toutes les lettres.docx"]
    one = docx.Document(io.BytesIO(z.read("02 - Riviera Immobilier.docx")))
    text = "\n".join(p.text for p in one.paragraphs)
    assert "F-104" in text and "4'200.00" in text and "F-101" not in text
    assert "Crissier" in one.sections[0].footer.paragraphs[0].text
    allin = docx.Document(io.BytesIO(z.read("Toutes les lettres.docx")))
    assert allin.element.body.xml.count('w:type="page"') == 2
    pdfs = zipfile.ZipFile(io.BytesIO(outputs.render("letters_pdf", "Relances.md", LETTERS)[0]))
    assert len([n for n in pdfs.namelist() if n.endswith(".pdf")]) == 4
    assert outputs.formats_for("Relances.md", LETTERS)[0] == "letters_docx"


def test_letters_separated_by_rules():
    src = ("---\nformat: lettres\n---\n# Convocations\n\nÀ : Sophie Chevalley\n\nChère Sophie, …\n\n---\n\n"
           "À : Thomas Rey\n\nCher Thomas, …\n")
    _t, ls = outputs.letters(src)
    assert [x.name for x in ls] == ["Sophie Chevalley", "Thomas Rey"]
    pv = outputs.preview("c.md", src)
    assert "2 lettres" in pv and "1 / 2 — Sophie Chevalley" in pv


def test_pdf_of_a_document_and_of_a_table():
    pypdf = pytest.importorskip("pypdf")
    pdf = outputs.render("pdf", "Rapprochement.md", FINANCE, org="Morand Domotique SA")[0]
    text = "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    assert "57'670.50" in text and "Rapprochement novembre" in text and "=SUM" not in text
    pdf = outputs.render("pdf", "PV.md", "# PV\n\nLe comité « Bellerive » s’est réuni.\n")[0]
    assert "« Bellerive » s’est" in pypdf.PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


# ---- through the Atelier's routes -----------------------------------------------------------
def test_routes_offer_the_formats_and_serve_them(world):  # noqa: F811
    c = world["as"]("bob@x")
    folder = world["workspace"] / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    (folder / "Rapprochement novembre.md").write_text(FINANCE, encoding="utf-8")
    (folder / "Relances.md").write_text(LETTERS, encoding="utf-8")
    (folder / "PV.md").write_text("# PV\n\nTexte.\n", encoding="utf-8")
    base = f"/api/simple/sessions/{DEFAULT_SID}/deliverables"
    items = {i["name"]: i for i in c.get(base).json()["deliverables"]}
    assert items["Rapprochement novembre.md"]["formats"] == ["xlsx", "pdf", "csv"]
    assert items["Rapprochement novembre.md"]["kind"] == "spreadsheet"
    assert items["Rapprochement novembre.md"]["title"] == "Rapprochement novembre"
    assert items["Relances.md"]["kind"] == "letters" and items["PV.md"]["formats"] == ["docx", "pdf"]
    pv = c.get(f"{base}/Rapprochement novembre.md").json()["text"]
    assert "43'550.00" in pv and "=SUM" not in pv
    x = c.get(f"{base}/Rapprochement novembre.md/download", params={"format": "xlsx"})
    assert x.status_code == 200 and "Rapprochement%20novembre.xlsx" in x.headers["content-disposition"]
    assert openpyxl.load_workbook(io.BytesIO(x.content), data_only=True)["Factures"]["E7"].value == 43550
    z = c.get(f"{base}/Relances.md/download", params={"format": "letters_docx"})
    assert z.status_code == 200 and len(zipfile.ZipFile(io.BytesIO(z.content)).namelist()) == 4
    assert c.get(f"{base}/PV.md/download", params={"format": "pdf"}).content[:5] == b"%PDF-"
    assert c.get(f"{base}/PV.md/download", params={"format": "xlsx"}).status_code == 400
    raw = c.get(f"{base}/Rapprochement novembre.md/download")
    assert raw.content.decode().startswith("---\nformat: xlsx")      # the source, as validated
    assert world["audit"]("deliverable.download")


def test_word_templates_admin_routes_and_use(world, tmp_path):  # noqa: F811
    tpl = _template(tmp_path).read_bytes()
    h = {"content-type": "application/octet-stream"}
    bob = world["as"]("bob@x")
    assert bob.put("/api/admin/word-templates/default", params={"name": "m.dotx"}, content=tpl,
                   headers=h).status_code == 403
    adm = world["as"]("admin@x")
    r = adm.put("/api/admin/word-templates/default", params={"name": "Maison.dotx"}, content=tpl, headers=h)
    assert r.status_code == 200, r.text
    assert [t["kind"] for t in r.json()["templates"]] == ["default"]
    assert adm.put("/api/admin/word-templates/Bad Kind!", params={"name": "m.docx"}, content=tpl,
                   headers=h).status_code in (400, 404)
    assert adm.put("/api/admin/word-templates/lettre", params={"name": "m.docm"}, content=tpl,
                   headers=h).status_code == 415
    assert adm.put("/api/admin/word-templates/lettre", params={"name": "m.docx"}, content=b"PK\x03\x04 nope",
                   headers=h).status_code == 415
    assert world["audit"]("word_template.set") and world["audit"]("word_template.refused")
    folder = world["workspace"] / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    (folder / "Offre.md").write_text("# Offre\n\nTexte.\n", encoding="utf-8")
    d = world["as"]("bob@x").get(f"/api/simple/sessions/{DEFAULT_SID}/deliverables/Offre.md/download",
                                 params={"format": "docx"})
    doc = docx.Document(io.BytesIO(d.content))
    assert "Crissier" in doc.sections[0].footer.paragraphs[0].text
    assert "Maison Corps" in [s.name for s in doc.styles]
    assert world["as"]("admin@x").delete("/api/admin/word-templates/default").json()["templates"] == []


def test_brief_names_the_formats_and_the_rule_of_the_figures(world):  # noqa: F811
    import simple
    b = simple.brief(ask="Fais-moi le rapprochement en Excel", lang="fr", project="default", sid=DEFAULT_SID)
    assert "format: xlsx" in b and "format: letters" in b and "=SUM(ABOVE)" in b
    assert "never tell them you can only save Markdown" in b and ".calc/" in b


def test_card_draft_carries_a_computed_preview():
    import plain
    v = plain.View(deliv_dir="/tmp/x/deliverables/" + DEFAULT_SID)
    d = v._draft("Write", {"file_path": f"/tmp/x/deliverables/{DEFAULT_SID}/R.md", "content": FINANCE})
    assert d["text"] == FINANCE[: plain.DRAFT_MAX] and d["kind"] == "spreadsheet"
    assert "43'550.00" in d["preview"]
    assert "preview" not in v._draft("Write", {"file_path": "/tmp/a.md", "content": "# T\n\nx\n"})


def test_calc_file_and_legacy_hook_hand_the_figures_to_the_model(tmp_path):
    import asyncio

    folder = tmp_path / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    f = folder / "R.md"
    f.write_text(FINANCE, encoding="utf-8")
    from harness import legacy
    hook = legacy.calc_hooks()[0].hooks[0]
    out = asyncio.run(hook({"hook_event_name": "PostToolUse", "tool_name": "Write",
                            "tool_input": {"file_path": str(f)}}, "t1", None))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Factures · Total · Reste = 43'550.00" in ctx
    assert (folder / ".calc" / "R.md.txt").read_text(encoding="utf-8") == ctx
    f.write_text("# Sans formule\n", encoding="utf-8")
    assert asyncio.run(hook({"hook_event_name": "PostToolUse", "tool_input": {"file_path": str(f)}},
                            "t2", None)) == {}
    # bench n°7: the brief says to read `.calc/<name>.txt` after every save — without a
    # formula the file is still there and says so (the model never reads a missing file)
    assert (folder / ".calc" / "R.md.txt").read_text(encoding="utf-8") == outputs.NO_FORMULA
    assert asyncio.run(hook({"hook_event_name": "PostToolUse",
                             "tool_input": {"file_path": str(tmp_path / "x.md")}}, "t3", None)) == {}


def test_both_engines_get_the_calc_file_from_the_event_stream(tmp_path, monkeypatch):
    import agentchat
    from harness import contract
    folder = tmp_path / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    (folder / "R.md").write_text(FINANCE, encoding="utf-8")
    s = agentchat.AgentSession.__new__(agentchat.AgentSession)
    s.cwd = str(tmp_path)
    # the kernel engine names the file by the sandbox's path
    monkeypatch.setattr(agentchat, "_is_deliverable_path", agentchat._is_deliverable_path)
    ev = contract.ToolCalled(turn_id="t", call_id="c1", name="write_file",
                             input={"path": f"/workspace/deliverables/{DEFAULT_SID}/R.md"},
                             classification="file_write")
    s._note_deliverable_call(ev)
    s._deliverable_saved(contract.ToolResult(call_id="c1", output="ok", ok=True))
    assert "43'550.00" in (folder / ".calc" / "R.md.txt").read_text(encoding="utf-8")


def test_a_document_without_formula_still_has_its_calc_file_for_both_engines(tmp_path):
    """Bench n°7 (A5) and integration n°3 (legacy A4 p3): an agent told to read
    `.calc/<name>.txt` read a file that did not exist — a failure line in the thread. The
    cockpit (both engines, agentchat `_deliverable_saved`) and the legacy hook always write it."""
    import asyncio

    import agentchat
    from harness import contract, legacy
    folder = tmp_path / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    plan = folder / "Plan.md"
    plan.write_text("# Plan de formation\n\n## Objectifs\n- comprendre\n", encoding="utf-8")
    s = agentchat.AgentSession.__new__(agentchat.AgentSession)
    s.cwd = str(tmp_path)
    ev = contract.ToolCalled(turn_id="t", call_id="c1", name="write_file",
                             input={"path": f"/workspace/deliverables/{DEFAULT_SID}/Plan.md"},
                             classification="file_write")
    s._note_deliverable_call(ev)
    s._deliverable_saved(contract.ToolResult(call_id="c1", output="ok", ok=True))
    calc = folder / ".calc" / "Plan.md.txt"
    assert calc.read_text(encoding="utf-8") == outputs.NO_FORMULA
    calc.unlink()
    hook = legacy.calc_hooks()[0].hooks[0]
    out = asyncio.run(hook({"hook_event_name": "PostToolUse", "tool_name": "Write",
                            "tool_input": {"file_path": str(plan)}}, "t1", None))
    assert out == {}                                    # nothing to add to the context
    assert calc.read_text(encoding="utf-8") == outputs.NO_FORMULA


# ---- inputs ----------------------------------------------------------------------------------
EML = (b"From: Garage Favre <compta@garage-favre.example>\r\nTo: Claire <claire@example.ch>\r\n"
       b"Date: Mon, 16 Nov 2026 09:12:00 +0100\r\nSubject: Facture F-107\r\nMIME-Version: 1.0\r\n"
       b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"Bonjour, le paiement part demain.\r\n--XX\r\nContent-Type: text/csv; name=\"releve.csv\"\r\n"
       b"Content-Disposition: attachment; filename=\"releve.csv\"\r\n\r\nDate;Montant\r\n16.11.2026;980.00\r\n--XX--\r\n")


def test_csv_eml_inputs_are_accepted_with_a_text_copy(world):  # noqa: F811
    c = world["as"]("bob@x")
    r = put(c, "Export banque.csv", "Date;Libellé;Montant\n02.11.2026;Virement Régie du Lac;6000.00\n".encode("cp1252"))
    assert r.status_code == 200 and r.json()["text_state"] == "ok"
    import documents
    t = documents.text_path_of(documents.get(r.json()["id"])).read_text(encoding="utf-8")
    assert "| 02.11.2026 | Virement Régie du Lac | 6000.00 |" in t
    r = put(c, "Mail Favre.eml", EML)
    assert r.status_code == 200, r.text
    t = documents.text_path_of(documents.get(r.json()["id"])).read_text(encoding="utf-8")
    assert "Subject: Facture F-107" in t and "le paiement part demain" in t
    assert "## Attachment: releve.csv" in t and "| 16.11.2026 | 980.00 |" in t
    h = c.get("/api/simple/home").json()
    assert {"csv", "eml", "msg", "jpg", "png"} <= set(h["limits"]["extensions"])


@pytest.mark.parametrize("name,data", [
    ("photo.png", b"\xff\xd8\xff\xe0 a jpeg called png"),
    ("photo.jpg", b"MZ\x90\x00 exe"),
    ("mail.eml", b"just some text without any header\n"),
    ("mail.msg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),
    ("bank.csv", b"\x7fELF\x02\x01"),
])
def test_new_types_must_be_what_they_say(world, name, data):  # noqa: F811
    r = put(world["as"]("bob@x"), name, data)
    assert r.status_code == 415 and r.json()["detail"]["code"] == "disguised", (name, r.text)


def test_a_word_document_renamed_msg_is_refused(world, tmp_path):  # noqa: F811
    olefile = pytest.importorskip("olefile")  # noqa: F841
    # an OLE file that is not an Outlook message (a legacy .doc / .xls)
    from simple_fixtures import docx as fake_docx
    r = put(world["as"]("bob@x"), "x.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    assert r.status_code == 415
    assert put(world["as"]("bob@x"), "x.msg", fake_docx()).status_code == 415


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_a_photo_of_a_receipt_is_read_by_ocr_on_the_machine(world):  # noqa: F811
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1400, 420), "white")
    dr = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 56)
    except OSError:
        font = ImageFont.load_default(size=56)
    dr.text((40, 60), "Restaurant du Lac", fill="black", font=font)
    dr.text((40, 200), "TOTAL CHF 381.30", fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    r = put(world["as"]("bob@x"), "Ticket repas.png", buf.getvalue())
    assert r.status_code == 200 and r.json()["text_state"] == "ok", r.text
    import documents
    d = documents.get(r.json()["id"])
    t = documents.text_path_of(d).read_text(encoding="utf-8")
    assert "381.30" in t and "character recognition" in t
    line = documents.agent_lines([d])[0]
    assert "a photo" in line and "check every figure" in line


def test_a_photo_without_ocr_says_so_to_the_agent(world, monkeypatch):  # noqa: F811
    from PIL import Image
    import doctext
    monkeypatch.setattr(doctext, "ocr_languages", lambda: "")
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, "JPEG")
    r = put(world["as"]("bob@x"), "Ticket.jpg", buf.getvalue())
    assert r.status_code == 200 and r.json()["text_state"] == "unavailable"
    import documents
    line = documents.agent_lines([documents.get(r.json()["id"])])[0]
    assert "could not be read" in line and "never guess" in line


def test_front_matter_is_only_at_the_top():
    assert outputs.front_matter("# T\n\n---\nformat: xlsx\n---\n")[0] == {}
    assert outputs.kind_of("a.md", "---\nformat: Excel\n---\n# T\n") == "spreadsheet"
    assert outputs.kind_of("a.md", "---\nnot front matter at all\n---\n") == "document"
    assert json.dumps(outputs.formats_for("a.md", "# T")) == '["docx", "pdf"]'


# what the 27B wrote on a real run (17.11.2026): row labels in brackets, another sheet's table
RUN_SUMMARY = """---
format: xlsx
---
# Rapprochement

## Factures émises
| Facture | Montant | Reçu | Reste |
|---|---|---|---|
| F-101 | 18350.00 | 6000.00 | =[Montant]-[Reçu] |
| F-103 | 31200.00 | 10000.00 | =[Montant]-[Reçu] |
| Total | =SUM(ABOVE) | =SUM(ABOVE) | =SUM(ABOVE) |

## Résumé
| Poste | Montant (CHF) |
|---|---|
| Total facturé | =SUM('Factures émises'!B2:B3) |
| Total reçu | =SUM('Factures émises'!C2:C3) |
| Total encore ouvert | =[Total facturé]-[Total reçu] |
| Écart | =[Total inconnu]*2 |
"""


def test_row_labels_and_other_sheets_are_understood_and_a_bad_formula_never_breaks_excel():
    wb, vals = _xlsx("R.md", RUN_SUMMARY)
    s = wb["Résumé"]
    # the first sheet has the title on top: the other sheet's table coordinates are shifted
    assert s["B2"].value == "=SUM('Factures émises'!B4:B5)"
    assert s["B4"].value == "=B2-B3"
    v = vals["Résumé"]
    assert (v["B2"].value, v["B3"].value, v["B4"].value) == (49550, 16000, 33550)
    assert s["B5"].data_type == "s" and s["B5"].value.startswith("=[Total inconnu]")
    pv = outputs.preview("R.md", RUN_SUMMARY)
    assert "| **Total encore ouvert** | **33'550.00** |" in pv or "| Total encore ouvert | 33'550.00 |" in pv and "(formule à revoir)" in pv
    summ = outputs.calc_summary("R.md", RUN_SUMMARY)
    assert "Résumé · Total encore ouvert · Montant (CHF) = 33'550.00" in summ
    assert "FORMULA NOT UNDERSTOOD" in summ


def test_two_templates_in_one_source_give_every_letter():
    """A real run: a courteous reminder for two clients, a firmer one for the third."""
    src = ("---\nformat: letters\n---\n\n# Relance de facture\n\nMadame, Monsieur,\n\nLa facture {{Facture}} "
           "de {{Montant}} CHF est échue.\n\n## Destinataires\n\n| Facture | Client | Montant |\n|---|---|---|\n"
           "| F-103 | Hôtel Bellerive | 21'200.00 |\n| F-104 | Riviera Immobilier | 4'200.00 |\n\n---\n\n"
           "# Deuxième rappel de facture\n\nMalgré notre premier rappel, la facture {{Facture}} de {{Montant}} CHF "
           "reste impayée.\n\n## Destinataires\n\n| Facture | Client | Montant |\n|---|---|---|\n"
           "| F-101 | Régie du Lac SA | 12'350.00 |\n")
    title, ls = outputs.letters(src)
    assert title == "Relance de facture"
    assert [x.name for x in ls] == ["Hôtel Bellerive", "Riviera Immobilier", "Régie du Lac SA"]
    assert "Deuxième rappel" not in ls[2].body and "Malgré notre premier rappel" in ls[2].body
    assert "F-101" in ls[2].body and "12'350.00" in ls[2].body and "F-103" not in ls[2].body
    # the template, a rule, then the recipients' table alone
    src2 = ("---\nformat: letters\n---\n# R\n\nFacture {{Facture}}.\n\n---\n\n## Destinataires\n\n"
            "| Facture | Nom |\n|---|---|\n| F-1 | A |\n| F-2 | B |\n")
    assert [x.name for x in outputs.letters(src2)[1]] == ["A", "B"]


def test_a_balance_column_of_formulas_takes_the_amounts_format():
    wb, _v = _xlsx("R.md", RUN_SUMMARY)
    assert wb["Résumé"]["B4"].number_format == "#,##0.00"


def test_the_organisation_logo_goes_into_word_and_pdf(tmp_path, monkeypatch):
    """Integration n°2 (orgmemory × formats): the logo of the organisation profile is in the
    header of a Word document without template and on the PDF."""
    import zipfile as _zip
    from PIL import Image
    import orgprofile
    import outputs
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    buf = io.BytesIO()
    Image.new("RGB", (120, 40), (20, 90, 160)).save(buf, "PNG")
    orgprofile.save_logo(buf.getvalue(), "logo.png")
    logo = orgprofile.document_logo()
    assert logo and logo[:8] == b"\x89PNG\r\n\x1a\n"
    data, _n, _m = outputs.render("docx", "Offre.md", "# Offre\n\nTexte.", org="Exemple SA", logo=logo)
    z = _zip.ZipFile(io.BytesIO(data))
    assert any(n.startswith("word/media/") for n in z.namelist())
    rels = " ".join(z.read(n).decode() for n in z.namelist() if n.startswith("word/_rels/header"))
    assert "media/" in rels
    pdf, _n, _m = outputs.render("pdf", "Offre.md", "# Offre\n\nTexte.", org="Exemple SA", logo=logo)
    plain, _n, _m = outputs.render("pdf", "Offre.md", "# Offre\n\nTexte.", org="Exemple SA")
    assert b"/Subtype /Image" in pdf and b"/Subtype /Image" not in plain
