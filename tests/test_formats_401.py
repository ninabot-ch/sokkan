"""4.0.1 — Excel of an offer (defects seen while filming the 4.0 launch video): the sheet name
is never a cut subtitle, the client line stays on the sheet of its table, VAT and total incl.
VAT are formulas when nothing is ambiguous and « à vérifier » otherwise. Invented company."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import outputs  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")

OFFER = """---
format: xlsx
---
# Offre de maintenance informatique pour Atelier Alpaga Sàrl

Client : Atelier Alpaga Sàrl, Mme Léa Fictive, Chemin des Exemples 4, 1200 Genève
Offre n° OF-2026-042 du 18.11.2026, valable 30 jours.

## Prestations proposées

| Prestation | Quantité | Prix unitaire (CHF) | Montant (CHF) |
|---|---|---|---|
| Maintenance annuelle d'un poste de travail | 12 | 180.00 | =[Quantité]*[Prix unitaire (CHF)] |
| Sauvegarde externalisée (forfait annuel) | 1 | 960.00 | =[Quantité]*[Prix unitaire (CHF)] |
| Mise en service du nouveau serveur | 1 | 1'450.00 | =[Quantité]*[Prix unitaire (CHF)] |
| **Total HT** | | | =SUM(ABOVE) |
| TVA 8,1 % | | | 370.17 |
| **Total TTC** | | | 4'940.17 |

## Conditions

Paiement à 30 jours net.

## Points à confirmer

- Nombre exact de postes
"""


def _xlsx(name, text):
    data = outputs.to_xlsx(name, text)
    return (openpyxl.load_workbook(io.BytesIO(data)),
            openpyxl.load_workbook(io.BytesIO(data), data_only=True))


def _find(ws, label):
    return next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == label)


def test_an_offer_is_one_sheet_with_the_client_above_its_table():
    wb, _v = _xlsx("Offre.md", OFFER)
    assert wb.sheetnames == ["Prestations proposées"]
    ws = wb.active
    col_a = [ws.cell(r, 1).value for r in range(1, ws.max_row + 1)]
    assert col_a[0] == "Offre de maintenance informatique pour Atelier Alpaga Sàrl"
    client, number, header = (col_a.index(x) for x in (
        "Client : Atelier Alpaga Sàrl, Mme Léa Fictive, Chemin des Exemples 4, 1200 Genève",
        "Offre n° OF-2026-042 du 18.11.2026, valable 30 jours.", "Prestation"))
    assert client < number < header                       # one line per row, above the table
    assert col_a.index("Conditions") > header and "• Nombre exact de postes" in col_a


def test_vat_and_total_incl_vat_are_formulas_with_the_written_rate():
    wb, vals = _xlsx("Offre.md", OFFER)
    ws, wv = wb.active, vals.active
    ht, vat, ttc = (_find(ws, x) for x in ("Total HT", "TVA 8,1 %", "Total TTC"))
    assert ws[f"D{ht}"].value == f"=SUM(D{ht - 3}:D{ht - 1})"
    assert ws[f"D{vat}"].value == f"=ROUND(D{ht}*8.1%,2)"
    assert ws[f"D{ttc}"].value == f"=D{ht}+D{vat}"
    assert (wv[f"D{ht}"].value, wv[f"D{vat}"].value, wv[f"D{ttc}"].value) == (4570, 370.17, 4940.17)
    assert ws[f"D{vat}"].comment is None and ws[f"E{vat}"].value is None
    s = outputs.calc_summary("Offre.md", OFFER)
    assert "TVA 8,1 % · Montant (CHF) = 370.17" in s and "Total TTC · Montant (CHF) = 4'940.17" in s
    assert "TO CHECK" not in s


@pytest.mark.parametrize("label,rate", [("TVA (8,1 %)", "8.1"), ("TVA 8.1%", "8.1"), ("MWST 2,6 %", "2.6"),
                                        ("TVA 7,7 %", "7.7")])
def test_the_rate_is_the_one_written_never_a_constant(label, rate):
    r = float(rate) / 100
    vat = round(1000 * r, 2)
    src = (f"---\nformat: xlsx\n---\n# Devis\n\n| Poste | Montant |\n|---|---|\n| Travaux | 1'000.00 |\n"
           f"| Total HT | =SUM(ABOVE) |\n| {label} | {vat:.2f} |\n| Total TTC | {1000 + vat:.2f} |\n")
    wb, vals = _xlsx("d.md", src)
    ws = wb.active
    assert ws["B6"].value == f"=ROUND(B5*{rate}%,2)" and ws["B7"].value == "=B5+B6"
    assert vals.active["B7"].value == pytest.approx(1000 + vat)


def test_vat_lines_written_under_the_table_join_it_as_formulas():
    src = ("---\nformat: xlsx\n---\n# Offre Fiduciaire Exemple\n\n| Prestation | Montant (CHF) |\n|---|---|\n"
           "| Audit | 2'000.00 |\n| Conseil | 500.00 |\n| **Total HT** | =SUM(ABOVE) |\n\n"
           "TVA 8,1 % : CHF 202.50\n**Total TTC : CHF 2'702.50**\n\nMerci de votre confiance.\n")
    wb, vals = _xlsx("o.md", src)
    ws = wb.active
    assert ws["A7"].value == "TVA 8,1 %" and ws["B7"].value == "=ROUND(B6*8.1%,2)"
    assert ws["A8"].value == "Total TTC" and ws["B8"].value == "=B6+B7"
    assert vals.active["B8"].value == 2702.5
    assert ws["A10"].value == "Merci de votre confiance."


def test_ambiguous_vat_keeps_the_figure_and_says_to_check_it():
    # the written VAT is not the computation: kept, marked, told to the agent
    wrong = OFFER.replace("| TVA 8,1 % | | | 370.17 |", "| TVA 8,1 % | | | 371.79 |") \
                 .replace("4'940.17", "4'941.79")
    wb, vals = _xlsx("Offre.md", wrong)
    ws = wb.active
    vat, ttc = _find(ws, "TVA 8,1 %"), _find(ws, "Total TTC")
    assert ws[f"D{vat}"].value == 371.79
    assert "À vérifier" in ws[f"D{vat}"].comment.text and "370.17" in ws[f"D{vat}"].comment.text
    assert ws[f"E{vat}"].value.startswith("À vérifier : TVA gardée telle qu'écrite")
    assert ws[f"D{ttc}"].value == f"=D{ttc - 2}+D{vat}"     # 4'570 + 371.79: consistent with the cell
    s = outputs.calc_summary("Offre.md", wrong)
    assert "TVA 8,1 % · Montant (CHF) = 371.79 TO CHECK, kept as written" in s
    pv = outputs.preview("Offre.md", wrong)
    assert "| TVA 8,1 % |  |  | 371.79 ⚠ |" in pv and "> ⚠ À vérifier : TVA gardée telle qu'écrite" in pv


def test_no_total_excl_vat_or_several_candidates_is_ambiguous():
    none = ("---\nformat: xlsx\n---\n# D\n\n| Poste | Montant |\n|---|---|\n| A | 100.00 |\n"
            "| TVA 8,1 % | 8.10 |\n| Total TTC | 108.10 |\n")
    wb, _v = _xlsx("d.md", none)
    ws = wb.active
    assert ws["B5"].value == 8.1 and ws["B5"].comment is not None and ws["B6"].value == 108.1
    assert "aucun total HT" in ws["C5"].value and ws["B6"].comment is not None
    two = ("---\nformat: xlsx\n---\n# D\n\n| Poste | Montant |\n|---|---|\n| Sous-total lot 1 | 100.00 |\n"
           "| Sous-total lot 2 | 200.00 |\n| TVA 8,1 % | 24.30 |\n| Total TTC | 324.30 |\n")
    wb, _v = _xlsx("d.md", two)
    assert wb.active["B6"].value == 24.3 and "plusieurs totaux HT" in wb.active["C6"].value
    no_rate = ("---\nformat: xlsx\n---\n# D\n\n| Poste | Montant |\n|---|---|\n| Total HT | 100.00 |\n"
               "| TVA | 8.10 |\n| Total TTC | 108.10 |\n")
    wb, _v = _xlsx("d.md", no_rate)
    assert wb.active["B5"].value == 8.1 and "taux" in wb.active["C5"].value


def test_a_rate_in_its_own_cell_is_referenced():
    src = ("---\nformat: xlsx\n---\n# D\n\n| Poste | Taux | Montant |\n|---|---|---|\n| Total HT | | 200.00 |\n"
           "| TVA | 8,1 % | 16.20 |\n| Total TTC | | 216.20 |\n")
    wb, vals = _xlsx("d.md", src)
    assert wb.active["C5"].value == "=ROUND(C4*B5,2)" and wb.active["C6"].value == "=C4+C5"
    assert vals.active["C6"].value == 216.2


def test_vat_rounded_to_five_centimes_is_understood():
    src = ("---\nformat: xlsx\n---\n# D\n\n| Poste | Montant |\n|---|---|\n| Total HT | 130.00 |\n"
           "| TVA 8,1 % | 10.55 |\n| Total TTC | 140.55 |\n")        # 10.53 → 10.55 at 5 centimes
    wb, vals = _xlsx("d.md", src)
    assert wb.active["B5"].value == "=ROUND(B4*8.1%*20,0)/20" and vals.active["B5"].value == 10.55
    assert vals.active["B6"].value == 140.55


def test_a_long_sheet_name_is_cut_at_a_word_and_never_a_cut_subtitle():
    name = outputs._sheet_name("Offre de maintenance informatique pour Atelier Alpaga", set())
    assert name == "Offre de maintenance…" and len(name) <= 31
    assert outputs._sheet_name("Prestations proposées", set()) == "Prestations proposées"
    used = set()
    a = outputs._sheet_name("Planification détaillée des interventions 2027", used)
    b = outputs._sheet_name("Planification détaillée des interventions 2028", used)
    assert a != b and len(b) <= 31 and b.endswith("(2)") and "…" in b
    src = ("---\nformat: xlsx\n---\n# Offre\n\n## Planification détaillée des interventions de maintenance\n\n"
           "| Mois | Heures |\n|---|---|\n| Janvier | 4 |\n\n## Récapitulatif\n\n| Poste | Montant |\n|---|---|\n"
           "| A | =SUM('Planification détaillée des interventions de maintenance'!B2:B2) |\n")
    wb, vals = _xlsx("o.md", src)
    first, second = wb.sheetnames
    assert first == "Planification détaillée des…" and len(first) <= 31
    assert wb[first]["A1"].value == "Offre"
    assert wb["Récapitulatif"]["B2"].value == "=SUM('Planification détaillée des…'!B4:B4)"
    assert vals["Récapitulatif"]["B2"].value == 4
    pv = outputs.preview("o.md", src)
    assert "## Planification détaillée des interventions de maintenance" in pv
    assert "## Planification détaillée des…" not in pv and "## Offre" not in pv
    csv = outputs.to_csv("o.md", src).decode("utf-8-sig")
    assert "\r\nRécapitulatif\r\n" in csv
    pdf, _n, _m = outputs.render("pdf", "o.md", src)
    assert pdf.startswith(b"%PDF")


def test_the_preview_never_shows_the_sheet_name_of_the_title_sheet():
    src = ("---\nformat: xlsx\n---\n# Offre de maintenance informatique pour Atelier Alpaga Sàrl\n\n"
           "Client : Atelier Alpaga Sàrl\n\n| Poste | Montant |\n|---|---|\n| A | 1 |\n\n## Annexe chiffrée\n\n"
           "| Poste | Montant |\n|---|---|\n| B | 2 |\n")
    pv = outputs.preview("o.md", src)
    assert pv.count("## ") == 1 and "## Annexe chiffrée" in pv
    assert "informatiq\n" not in pv and "Offre de maintenance…" not in pv
    wb, _v = _xlsx("o.md", src)
    assert wb.sheetnames == ["Offre de maintenance…", "Annexe chiffrée"]


def test_a_document_without_table_is_one_sheet():
    src = "---\nformat: xlsx\n---\n# Note\n\n## Contexte\n\nTexte.\n\n## Suite\n\nAutre texte.\n"
    wb, _v = _xlsx("n.md", src)
    assert len(wb.sheetnames) == 1
    assert [wb.active.cell(r, 1).value for r in range(1, 8)] == ["Note", None, "Contexte", "Texte.", "Suite",
                                                                 "Autre texte.", None]


def test_factcheck_shows_an_ambiguous_figure_on_the_card():
    import factcheck
    src = ("---\nformat: xlsx\n---\n# D\n\n| Poste | Montant |\n|---|---|\n| A | 100.00 |\n"
           "| TVA 8,1 % | 8.10 |\n| Total TTC | 108.10 |\n")
    _t, found = factcheck.with_computed("d.md", src)
    f = next(x for x in found if x["kind"] == "figure_to_check")
    assert f["severity"] == "warn" and "TVA 8,1 %" in f["fr"]
