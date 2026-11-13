"""Live Postgres end-to-end test — SKIPPED unless a server is provided.

Set ``LEGITRACK_PG_TEST_URL`` to a Postgres connection string for a
throwaway database and this runs the real path: PostgresStore applies
schema.sql, a fake-client LegiScan ingestion + OpenStates people
ingestion write to it, and the web app reads the counts back through
its Postgres path (DATABASE_URL set, create_app() with no db_path).

How it was verified when the backend landed (2026-11-13): an embedded
Postgres from the pip ``pgserver`` package was started in a throwaway
venv, its URI exported as LEGITRACK_PG_TEST_URL, and this file run —
all tests passed against the real server. No API keys are involved;
both API clients are the same synthetic fakes the SQLite suite uses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

PG_URL = os.environ.get("LEGITRACK_PG_TEST_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="LEGITRACK_PG_TEST_URL not set — no live Postgres provided"
)

pytest.importorskip("psycopg")
pytest.importorskip("fastapi")

from legitrack.ingest import ingest_state  # noqa: E402
from legitrack.legiscan_client import MasterBill, Session, parse_master_list, parse_session_list  # noqa: E402
from legitrack.legislators import ingest_people  # noqa: E402
from legitrack.store import PostgresStore  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeLegiScanClient:
    def __init__(self) -> None:
        self.queries_used = 0
        self.bill_calls: list[int] = []

    def get_session_list(self, state: str) -> list[Session]:
        self.queries_used += 1
        return parse_session_list(_load("session_list_synthetic.json"))

    def get_master_list(self, session_id: int) -> list[MasterBill]:
        self.queries_used += 1
        return parse_master_list(_load("master_list_synthetic.json"))

    def get_bill(self, bill_id: int) -> dict[str, Any]:
        self.queries_used += 1
        self.bill_calls.append(bill_id)
        master = {b.bill_id: b for b in parse_master_list(_load("master_list_synthetic.json"))}
        bill = master[bill_id]
        return {
            "bill_id": bill.bill_id, "bill_number": bill.number, "title": bill.title,
            "description": bill.description, "status": bill.status, "subjects": [],
            "texts": [], "votes": [], "sponsors": [], "history": [],
        }


class FakePeopleClient:
    def __init__(self) -> None:
        self.queries_used = 0

    def list_people(self, jurisdiction: str, *, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        self.queries_used += 1
        row = {
            "id": "ocd-person/synthetic-pg-1", "name": "Synthetic PG Person",
            "given_name": "Synthetic", "family_name": "Pgperson", "party": "Democratic",
            "email": "synthetic.pg@example.gov", "image": "https://example.gov/pg.jpg",
            "current_role": {"title": "Representative", "org_classification": "lower", "district": "38"},
            "jurisdiction": {"id": jurisdiction, "name": "North Carolina", "classification": "state"},
        }
        return {"results": [row] if page == 1 else [],
                "pagination": {"per_page": per_page, "page": page, "max_page": 1, "total_items": 1}}


@pytest.fixture()
def pg_store() -> Any:
    store = PostgresStore(PG_URL)
    yield store
    store.close()


def test_live_postgres_ingestion_and_delta(pg_store: PostgresStore) -> None:
    first = ingest_state(FakeLegiScanClient(), pg_store, "NC")
    assert (first.bills_seen, first.bills_changed, first.bills_fetched) == (3, 3, 3)
    assert pg_store.bill_count() == 3
    assert pg_store.bill_count(session_id=9999) == 3
    assert pg_store.stored_hashes(9999)[111] == "hash-aaa"

    second = ingest_state(FakeLegiScanClient(), pg_store, "NC")
    assert second.bills_changed == 0 and second.bills_fetched == 0

    summary = ingest_people(FakePeopleClient(), pg_store, "NC")
    assert summary.people_stored == 1
    assert pg_store.legislator_count("NC") == 1
    assert all(r["status"] == "ok" for r in pg_store.runs())


def test_live_postgres_web_reads(pg_store: PostgresStore, monkeypatch: pytest.MonkeyPatch) -> None:
    ingest_state(FakeLegiScanClient(), pg_store, "NC")
    ingest_people(FakePeopleClient(), pg_store, "NC")

    from fastapi.testclient import TestClient

    from legitrack.web import _stats_pg, create_app

    stats = _stats_pg(PG_URL)
    assert stats["available"] is True
    assert stats["bills"] == 3
    assert stats["bills_with_detail"] == 3
    assert stats["legislators"] == 1
    assert stats["classified_bills"] == 2

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    client = TestClient(create_app())  # no db_path -> Postgres via DATABASE_URL
    assert client.get("/healthz").json() == {
        "status": "ok", "data_available": True, "bills": 3,
    }
    assert "Synthetic PG Person" in client.get("/legislators").text
    bills_page = client.get(
        "/bills", params={"category": "Voting access & election administration"}
    )
    assert "Synthetic Voting Access Act" in bills_page.text
