"""Tests for OpenStates legislator parsing + ingestion.

All people data here is SYNTHETIC — invented names shaped like the real
OpenStates /people response validated in the 2026-11-13 proof pull.
No live API calls, no keys.
"""

from __future__ import annotations

from typing import Any

from legitrack.legislators import ingest_people, parse_person
from legitrack.store import LocalStore


def _person_row(idx: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": f"ocd-person/synthetic-{idx:04d}",
        "name": f"Synthetic Person {idx}",
        "given_name": "Synthetic",
        "family_name": f"Person{idx}",
        "party": "Democratic" if idx % 2 else "Republican",
        "email": f"synthetic.person{idx}@example.gov",
        "image": "https://example.gov/images/synthetic.jpg",
        "current_role": {
            "title": "Representative",
            "org_classification": "lower",
            "district": str(idx),
            "division_id": f"ocd-division/country:us/state:nc/sldl:{idx}",
        },
        "jurisdiction": {
            "id": "ocd-jurisdiction/country:us/state:nc/government",
            "name": "North Carolina",
            "classification": "state",
        },
    }
    row.update(overrides)
    return row


def test_parse_person_full() -> None:
    person = parse_person(_person_row(38), "NC")
    assert person.openstates_person_id == "ocd-person/synthetic-0038"
    assert person.name == "Synthetic Person 38"
    assert person.party == "Republican"  # even idx in the fixture builder
    assert person.chamber == "lower"
    assert person.role_title == "Representative"
    assert person.district == "38"
    assert person.email_published == "synthetic.person38@example.gov"
    assert person.photo_url.startswith("https://")
    assert person.state_abbr == "NC"


def test_parse_person_missing_optional_fields() -> None:
    row = _person_row(1, email="", image="", current_role=None, jurisdiction=None)
    person = parse_person(row, "nc")
    assert person.email_published == ""
    assert person.photo_url == ""
    assert person.chamber == ""
    assert person.state_abbr == "NC"  # normalised to upper


class FakePeopleClient:
    """Two pages of synthetic people; records calls."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.queries_used = 0

    def list_people(self, jurisdiction: str, *, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        self.queries_used += 1
        results = self._pages[page - 1] if page <= len(self._pages) else []
        return {
            "results": results,
            "pagination": {
                "per_page": per_page, "page": page,
                "max_page": len(self._pages),
                "total_items": sum(len(p) for p in self._pages),
            },
        }


def test_ingest_people_pages_and_upserts() -> None:
    pages = [[_person_row(1), _person_row(2)], [_person_row(3)]]
    client = FakePeopleClient(pages)
    store = LocalStore(":memory:")
    summary = ingest_people(client, store, "NC", per_page=2)  # type: ignore[arg-type]
    assert summary.people_seen == 3
    assert summary.people_stored == 3
    assert summary.queries_used == 2
    assert store.legislator_count("NC") == 3
    # Re-running must upsert, not duplicate.
    summary2 = ingest_people(client, store, "NC", per_page=2)  # type: ignore[arg-type]
    assert summary2.people_stored == 3
    assert store.legislator_count("NC") == 3
    runs = [r for r in store.runs() if r["job"] == "openstates_people"]
    assert len(runs) == 2 and all(r["status"] == "ok" for r in runs)
    store.close()


def test_ingest_people_skips_rows_without_id_or_name() -> None:
    bad = _person_row(9, id="", name="")
    client = FakePeopleClient([[_person_row(1), bad]])
    store = LocalStore(":memory:")
    summary = ingest_people(client, store, "NC")  # type: ignore[arg-type]
    assert summary.people_seen == 2
    assert summary.people_stored == 1
    store.close()
