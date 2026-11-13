"""Tests for master-list parsing, change-hash delta logic, session picking,
and a full ingestion pass against a fake client. All fixtures are
SYNTHETIC (labelled in the fixture files); no live API is called and no
API key is used anywhere in this suite.
"""

import json
from pathlib import Path
from typing import Any

from legitrack.ingest import compute_changed_bills, ingest_state, pick_session
from legitrack.legiscan_client import MasterBill, Session, parse_master_list, parse_session_list
from legitrack.store import LocalStore

FIXTURES = Path(__file__).parent / "fixtures"


def _master_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "master_list_synthetic.json").read_text(encoding="utf-8"))


def _session_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "session_list_synthetic.json").read_text(encoding="utf-8"))


def test_parse_master_list_skips_session_entry() -> None:
    bills = parse_master_list(_master_payload())
    assert [b.bill_id for b in bills] == [111, 222, 333]
    assert bills[0].number == "H1"
    assert bills[0].change_hash == "hash-aaa"
    assert bills[2].status == 2


def test_parse_session_list() -> None:
    sessions = parse_session_list(_session_payload())
    assert [s.session_id for s in sessions] == [9001, 9999]
    assert sessions[1].special is False


def test_pick_session_prefers_regular_over_special() -> None:
    sessions = parse_session_list(_session_payload())
    picked = pick_session(sessions)
    # The special session is listed first and is newer; the regular
    # session must still win by default.
    assert picked.session_id == 9999
    assert pick_session(sessions, session_id=9001).session_id == 9001


def test_pick_session_uses_tag_when_special_flag_is_zero() -> None:
    # Regression from the live NC run 2026-11-13: the real extra session
    # carries special=0 despite a "Special Session" tag, and is newest.
    from legitrack.legiscan_client import Session

    sessions = [
        Session(session_id=2274, state_abbr="NC", year_start=2026, year_end=2026,
                session_name="Extra", session_tag="1st Special Session", special=False),
        Session(session_id=2189, state_abbr="NC", year_start=2025, year_end=2026,
                session_name="Regular", session_tag="Regular Session", special=False),
    ]
    assert pick_session(sessions).session_id == 2189


def test_delta_all_new_when_store_empty() -> None:
    bills = parse_master_list(_master_payload())
    changed = compute_changed_bills(bills, stored_hashes={})
    assert len(changed) == 3


def test_delta_only_changed_hashes() -> None:
    bills = parse_master_list(_master_payload())
    stored = {111: "hash-aaa", 222: "hash-bbb", 333: "hash-ccc-v1"}  # 333 changed upstream
    changed = compute_changed_bills(bills, stored_hashes=stored)
    assert [b.bill_id for b in changed] == [333]


def test_delta_empty_when_nothing_changed() -> None:
    bills = parse_master_list(_master_payload())
    stored = {b.bill_id: b.change_hash for b in bills}
    assert compute_changed_bills(bills, stored_hashes=stored) == []


class FakeLegiScanClient:
    """In-memory stand-in with the ingest-facing client interface."""

    def __init__(self) -> None:
        self.queries_used = 0
        self.bill_calls: list[int] = []

    def get_session_list(self, state: str) -> list[Session]:
        self.queries_used += 1
        return parse_session_list(_session_payload())

    def get_master_list(self, session_id: int) -> list[MasterBill]:
        self.queries_used += 1
        return parse_master_list(_master_payload())

    def get_bill(self, bill_id: int) -> dict[str, Any]:
        self.queries_used += 1
        self.bill_calls.append(bill_id)
        master = {b.bill_id: b for b in parse_master_list(_master_payload())}
        bill = master[bill_id]
        return {
            "bill_id": bill.bill_id,
            "bill_number": bill.number,
            "title": bill.title,
            "description": bill.description,
            "status": bill.status,
            "subjects": [],
            "texts": [],
            "votes": [],
            "sponsors": [],
            "history": [],
        }


def _store() -> LocalStore:
    return LocalStore(":memory:")


def test_ingest_dry_run_fetches_no_details_and_writes_nothing() -> None:
    client = FakeLegiScanClient()
    store = _store()
    summary = ingest_state(client, store, "NC", dry_run=True)
    assert summary.bills_seen == 3
    assert summary.bills_changed == 3
    assert summary.bills_fetched == 0
    assert client.bill_calls == []
    assert store.bill_count() == 0
    assert store.runs() == []  # dry runs record no pipeline run


def test_ingest_first_run_fetches_all_then_second_run_fetches_none() -> None:
    client = FakeLegiScanClient()
    store = _store()
    first = ingest_state(client, store, "NC")
    assert (first.bills_seen, first.bills_changed, first.bills_fetched) == (3, 3, 3)
    assert store.bill_count(session_id=9999) == 3

    second_client = FakeLegiScanClient()
    second = ingest_state(second_client, store, "NC")
    assert second.bills_changed == 0
    assert second.bills_fetched == 0
    assert second_client.bill_calls == []

    runs = store.runs()
    assert len(runs) == 2
    assert all(r["status"] == "ok" for r in runs)
    assert runs[0]["bills_fetched"] == 3


def test_ingest_limit_caps_detail_fetches() -> None:
    client = FakeLegiScanClient()
    store = _store()
    summary = ingest_state(client, store, "NC", limit=1)
    assert summary.bills_changed == 3
    assert summary.bills_fetched == 1
    assert len(client.bill_calls) == 1


def test_ingest_stores_rules_classification_for_changed_bills() -> None:
    client = FakeLegiScanClient()
    store = _store()
    ingest_state(client, store, "NC")
    rows = store._conn.execute(  # test-only peek at the local store
        "SELECT legiscan_bill_id, category_id FROM bill_classifications ORDER BY legiscan_bill_id"
    ).fetchall()
    pairs = {(r["legiscan_bill_id"], r["category_id"]) for r in rows}
    assert (111, "voting_access") in pairs
    assert (333, "crown_act") in pairs
    # Synthetic bill 222 (sports fees) must stay unclassified: no guessing.
    assert all(bill_id != 222 for bill_id, _ in pairs)
