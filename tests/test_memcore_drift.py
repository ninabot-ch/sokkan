"""core.drift — "description != body": a fact corrected in the description only."""
from core.contract import SeenRecord
from core.drift import check_versions, terms

BODY = ("Supplier contact: Dana at the mill.\n\n"
        "Alert: the flour quota is at 90 % of the yearly contract, order before June.\n\n"
        "Deliveries on Tuesdays.")


def v(desc, body, at):
    return SeenRecord(name="flour", content_hash="h", first_seen=at, description=desc,
                      body=body, seen_at=at)


def test_terms_fold_accents_plurals_and_stopwords():
    t = terms("Les livraisons du Café, 90 % et deliveries")
    assert {"livraison", "cafe", "90", "deliverie"} <= t
    assert "les" not in t and "du" not in t


def test_description_corrected_but_paragraph_still_old_is_reported():
    f = check_versions("flour", [
        v("Flour quota alert at 90 % of the yearly contract", BODY, "t1"),
        v("Flour quota resolved, contract upgraded to unlimited", BODY, "t2"),
    ])
    assert f is not None and f.kind == "description-body-drift"
    assert f.paragraph_index == 1 and "90 %" in f.paragraph
    assert "90" in f.stale_terms and f.severity == "high"
    assert "unlimited" in f.missing_terms
    assert f.description_changed_at == "t2" and f.paragraph_unchanged_since == "t1"
    assert f.to_dict()["note"] == "flour"


def test_paragraph_corrected_too_is_fine():
    fixed = BODY.replace("Alert: the flour quota is at 90 % of the yearly contract, order "
                         "before June.", "The flour contract was upgraded to unlimited.")
    assert check_versions("flour", [
        v("Flour quota alert at 90 % of the yearly contract", BODY, "t1"),
        v("Flour quota resolved, contract upgraded to unlimited", fixed, "t2"),
    ]) is None


def test_rewording_is_not_drift():
    assert check_versions("flour", [
        v("Deploy runbook for the flour order", BODY, "t1"),
        v("Flour order deployment runbook", BODY, "t2"),
    ]) is None


def test_unrelated_paragraph_edit_does_not_hide_the_stale_one():
    other = BODY.replace("Deliveries on Tuesdays.", "Deliveries on Thursdays now.")
    f = check_versions("flour", [
        v("Flour quota alert at 90 % of the yearly contract", BODY, "t1"),
        v("Flour quota alert at 90 % of the yearly contract", BODY, "t1b"),
        v("Flour quota resolved, contract upgraded to unlimited", other, "t2"),
    ])
    assert f is not None and f.paragraph_index == 1


def test_finding_persists_until_the_paragraph_changes():
    hist = [
        v("Flour quota alert at 90 % of the yearly contract", BODY, "t1"),
        v("Flour quota resolved, contract upgraded to unlimited", BODY, "t2"),
        v("Flour quota resolved, contract upgraded to unlimited",
          BODY + "\n\nNew note on packaging.", "t3"),
    ]
    assert check_versions("flour", hist) is not None


def test_fact_only_added_to_the_description_is_low():
    f = check_versions("flour", [
        v("Flour supplier and quota", BODY, "t1"),
        v("Flour supplier and quota, invoices go to accounting@example.org", BODY, "t2"),
    ])
    assert f is not None and f.severity == "low" and "accounting" in f.missing_terms


def test_no_history_no_finding():
    assert check_versions("flour", [v("a b c", BODY, "t1")]) is None
    assert check_versions("flour", [v("same", BODY, "t1"), v("same", BODY + " x", "t2")]) is None
