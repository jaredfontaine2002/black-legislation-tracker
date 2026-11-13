"""Tests for the web app, against a small SYNTHETIC SQLite store.

All rows here are invented for tests (labelled synthetic in names/titles).
The live-DB smoke test is a separate manual step documented in README —
unit tests must never depend on the real pulled database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from legitrack.web import create_app  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (legiscan_session_id INTEGER PRIMARY KEY,
            state_abbr TEXT NOT NULL, year_start INTEGER, year_end INTEGER,
            session_name TEXT, dataset_hash TEXT);
        CREATE TABLE bills (legiscan_bill_id INTEGER PRIMARY KEY,
            legiscan_session_id INTEGER NOT NULL, number TEXT NOT NULL,
            title TEXT, change_hash TEXT NOT NULL, status_code INTEGER,
            public_status TEXT, status_date TEXT, last_action TEXT,
            last_action_date TEXT, url TEXT, detail_json TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
        CREATE TABLE bill_classifications (legiscan_bill_id INTEGER NOT NULL,
            category_id TEXT NOT NULL, category_name TEXT NOT NULL,
            confidence REAL NOT NULL, method TEXT NOT NULL DEFAULT 'rule',
            matched_terms TEXT,
            PRIMARY KEY (legiscan_bill_id, category_id, method));
        CREATE TABLE legislators (openstates_person_id TEXT PRIMARY KEY,
            name TEXT NOT NULL, first_name TEXT, last_name TEXT, party TEXT,
            chamber TEXT, role_title TEXT, district TEXT, email_published TEXT,
            photo_url TEXT, jurisdiction_id TEXT, state_abbr TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
        CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            job TEXT NOT NULL, state_abbr TEXT, legiscan_session_id INTEGER,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL
            DEFAULT 'running', bills_seen INTEGER NOT NULL DEFAULT 0,
            bills_changed INTEGER NOT NULL DEFAULT 0,
            bills_fetched INTEGER NOT NULL DEFAULT 0,
            queries_used INTEGER NOT NULL DEFAULT 0, error TEXT);
        """
    )
    conn.execute("INSERT INTO sessions VALUES (1, 'NC', 2025, 2026, 'Synthetic Session', 'x')")
    conn.execute(
        "INSERT INTO bills VALUES (101, 1, 'H999', 'Synthetic Voting Bill', 'h', 1,"
        " 'Introduced', '2026-01-01', '', '', 'https://example.gov/H999', '{}',"
        " '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO bills VALUES (102, 1, 'S888', 'Synthetic Roads Bill', 'h', 4,"
        " 'Signed', '2026-02-01', '', '', '', NULL, '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO bill_classifications VALUES (101, 'voting_access',"
        " 'Voting access & election administration', 0.9, 'rule', '[]')"
    )
    conn.execute(
        "INSERT INTO legislators VALUES ('ocd-person/synthetic-1', 'Synthetic Person',"
        " 'Synthetic', 'Person', 'Democratic', 'lower', 'Representative', '38',"
        " 'synthetic@example.gov', 'https://example.gov/p.jpg', '', 'NC',"
        " '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO pipeline_runs (job, state_abbr, started_at, finished_at, status)"
        " VALUES ('legiscan_ingest', 'NC', '2026-01-02T00:00:00+00:00',"
        " '2026-01-02T00:01:00+00:00', 'ok')"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db = tmp_path / "synthetic.db"
    _make_db(db)
    return TestClient(create_app(str(db)))


def test_landing_shows_real_counts_from_db(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Black Legislation Tracker" in page.text
    assert "bills tracked" in page.text
    assert ">2<" in page.text  # 2 synthetic bills — count came from the DB
    assert "legiscan_ingest" in page.text  # last-run freshness, from pipeline_runs


def test_bills_filter_by_status_and_category(client: TestClient) -> None:
    page = client.get("/bills", params={"status": "Introduced"})
    assert "Synthetic Voting Bill" in page.text
    assert "Synthetic Roads Bill" not in page.text
    page = client.get("/bills", params={"category": "Voting access & election administration"})
    assert "Synthetic Voting Bill" in page.text
    assert "Synthetic Roads Bill" not in page.text


def test_legislators_page(client: TestClient) -> None:
    page = client.get("/legislators")
    assert page.status_code == 200
    assert "Synthetic Person" in page.text
    assert "District 38" in page.text
    assert "https://example.gov/p.jpg" in page.text


def test_missing_db_is_honest_not_invented(tmp_path: Path) -> None:
    client = TestClient(create_app(str(tmp_path / "does-not-exist.db")))
    page = client.get("/")
    assert page.status_code == 200
    assert "No database is attached" in page.text
    health = client.get("/healthz")
    assert health.json()["data_available"] is False
