"""Tests for rules-based classification v1 on SYNTHETIC sample bills.

Every bill text below is invented for these tests and labelled synthetic;
no real bill, sponsor, or jurisdiction data is used.
"""

from legitrack.taxonomy import classify_bill, classify_legiscan_bill, load_taxonomy


def _ids(results: list) -> set[str]:
    return {r.category_id for r in results}


def test_taxonomy_has_the_11_spec_categories() -> None:
    tax = load_taxonomy()
    assert tax.version == "rules-v1"
    assert len(tax.categories) == 11


def test_synthetic_voting_bill() -> None:
    results = classify_bill(
        title="Synthetic Voting Access Act",
        description="Expands early voting and same-day voter registration.",
    )
    assert "voting_access" in _ids(results)
    top = results[0]
    assert top.category_id == "voting_access"
    assert top.method == "rule"
    assert top.confidence >= 0.8  # keyword hit in the title
    assert "early voting" in top.matched_terms


def test_synthetic_crown_act_bill() -> None:
    results = classify_bill(
        title="Synthetic CROWN Act",
        description="Prohibits hair discrimination based on natural hair and protective hairstyles.",
    )
    assert "crown_act" in _ids(results)


def test_synthetic_hbcu_bill_is_education() -> None:
    results = classify_bill(
        title="Synthetic HBCU Funding Act",
        description="Appropriates funds for historically black colleges and universities.",
    )
    assert "education" in _ids(results)


def test_synthetic_reparations_study_bill() -> None:
    results = classify_bill(
        title="Synthetic Act",
        description="Creates a reparations study commission on slavery reparations.",
    )
    assert "reparations" in _ids(results)


def test_multi_label_bill() -> None:
    results = classify_bill(
        title="Synthetic Fair Housing and Employment Act",
        description="Strengthens fair housing enforcement and bans employment discrimination.",
    )
    ids = _ids(results)
    assert "discrimination_enforcement" in ids


def test_subject_match_is_strong_evidence() -> None:
    results = classify_bill(
        title="Synthetic Procedures Act",
        description="General procedures.",
        subjects=[{"subject_name": "Elections"}],
    )
    assert results and results[0].category_id == "voting_access"
    assert results[0].confidence == 0.90
    assert results[0].matched_subjects == ("Elections",)


def test_unrelated_synthetic_bill_gets_no_guess() -> None:
    results = classify_bill(
        title="Synthetic County Sports Entry Fees",
        description="Sets entry fees for interscholastic sports events.",
    )
    assert results == []


def test_classify_legiscan_bill_shape() -> None:
    bill = {
        "title": "Synthetic Police Accountability Act",
        "description": "Limits qualified immunity and requires use of force reporting.",
        "subjects": [{"subject_name": "Law Enforcement"}],
    }
    results = classify_legiscan_bill(bill)
    assert "criminal_justice" in _ids(results)


def test_confidence_is_capped() -> None:
    results = classify_bill(
        title="voter registration early voting absentee ballot polling place",
        description="voter identification provisional ballot ballot access",
    )
    assert all(0 < r.confidence <= 0.95 for r in results)
