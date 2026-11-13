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
    assert health.json()["database"] == "unavailable"


def test_populated_db_healthz_reports_connected(client: TestClient) -> None:
    health = client.get("/healthz").json()
    assert health["database"] == "connected"
    assert health["data_available"] is True
    assert health["bills"] == 2


# ---------------------------------------------------------------------------
# Connected-but-empty is NOT "no database attached": a freshly provisioned
# database (Render Postgres before the first ingestion, or a SQLite file
# with no schema applied) connects fine but has no tables yet.
# ---------------------------------------------------------------------------

def test_sqlite_file_without_tables_is_empty_not_unattached(tmp_path: Path) -> None:
    db = tmp_path / "no-schema.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    client = TestClient(create_app(str(db)))
    page = client.get("/")
    assert page.status_code == 200
    assert "The database is connected" in page.text
    assert "first ingestion run" in page.text
    assert "No database is attached" not in page.text
    health = client.get("/healthz").json()
    assert health["database"] == "connected_empty"
    assert health["data_available"] is False
    assert health["bills"] == 0


class _FakePgCursor:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _FakePgConnSchemaAbsent:
    """Connects fine, but to_regclass reports no bills table. Any other
    query would error on a real schema-less database, so it raises here —
    _stats_pg must return the empty payload before reaching them."""

    def __init__(self) -> None:
        self.closed = False

    def execute(self, sql: str, params: tuple = ()) -> _FakePgCursor:
        if "to_regclass" in sql:
            return _FakePgCursor({"t": None})
        raise AssertionError(f"unexpected query against schema-less DB: {sql}")

    def close(self) -> None:
        self.closed = True


def test_pg_schema_absent_is_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import legitrack.web as web

    monkeypatch.setattr(web, "_pg_connect", lambda dsn: _FakePgConnSchemaAbsent())
    s = web._stats_pg("postgresql://synthetic.invalid/db")
    assert s["available"] is True
    assert s["empty"] is True
    assert s["bills"] == 0
    assert s["legislators"] == 0
    assert s["states"] == 0
    assert s["classified_bills"] == 0
    assert s["status_breakdown"] == []
    assert s["category_counts"] == []
    assert s["last_run"] is None


def test_pg_connection_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import legitrack.web as web

    def _boom(dsn: str) -> object:
        raise RuntimeError("synthetic connect failure")

    monkeypatch.setattr(web, "_pg_connect", _boom)
    assert web._stats_pg("postgresql://synthetic.invalid/db") == {"available": False}


def test_pg_schema_absent_landing_and_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    import legitrack.web as web

    monkeypatch.setattr(web, "_pg_connect", lambda dsn: _FakePgConnSchemaAbsent())
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic.invalid/db")
    client = TestClient(web.create_app())
    page = client.get("/")
    assert page.status_code == 200
    assert "The database is connected" in page.text
    assert "first ingestion run" in page.text
    assert "No database is attached" not in page.text
    health = client.get("/healthz").json()
    assert health["database"] == "connected_empty"
    assert health["data_available"] is False
    assert health["bills"] == 0


# ---------------------------------------------------------------------------
# /map — Friendliness Score choropleth.
# Score = round(100 x supportive / (supportive + harmful)) over DISTINCT
# bills; a state is scored only with >= 5 tagged bills. NC in the stance
# fixture: 9 supportive + 5 harmful = score 64. CA: 2 + 1 (+ 1 mixed,
# excluded) = 3 tagged -> not enough data. Every other state: untracked.
# ---------------------------------------------------------------------------

def _make_stance_db(path: Path) -> None:
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
            matched_terms TEXT, stance TEXT, stance_terms TEXT,
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
    conn.execute("INSERT INTO sessions VALUES (1, 'NC', 2025, 2026, 'Synthetic NC Session', 'x')")
    conn.execute("INSERT INTO sessions VALUES (2, 'CA', 2025, 2026, 'Synthetic CA Session', 'x')")

    def add_bill(bill_id: int, session_id: int, stance: str | None) -> None:
        conn.execute(
            "INSERT INTO bills VALUES (?, ?, ?, 'Synthetic Bill', 'h', 1,"
            " 'Introduced', '2026-01-01', '', '', '', NULL,"
            " '2026-01-01', '2026-01-01')",
            (bill_id, session_id, f"B{bill_id}"),
        )
        if stance is not None:
            conn.execute(
                "INSERT INTO bill_classifications VALUES (?, 'voting_access',"
                " 'Voting access & election administration', 0.9, 'rule', '[]', ?, '[]')",
                (bill_id, stance),
            )

    for i in range(9):  # NC: 9 supportive
        add_bill(101 + i, 1, "supportive")
    for i in range(5):  # NC: 5 harmful
        add_bill(110 + i, 1, "harmful")
    add_bill(201, 2, "supportive")  # CA: 2 supportive, 1 harmful, 1 mixed
    add_bill(202, 2, "supportive")
    add_bill(203, 2, "harmful")
    add_bill(204, 2, "mixed")
    conn.commit()
    conn.close()


@pytest.fixture()
def stance_client(tmp_path: Path) -> TestClient:
    db = tmp_path / "stance.db"
    _make_stance_db(db)
    return TestClient(create_app(str(db)))


def test_map_scores_state_with_enough_tagged_bills(stance_client: TestClient) -> None:
    page = stance_client.get("/map")
    assert page.status_code == 200
    assert "North Carolina — score 64 (9 supportive · 5 harmful)" in page.text
    assert "Friendliness Score" in page.text
    assert "at least 5 tagged bills" in page.text


def test_map_below_minimum_sample_is_not_scored(stance_client: TestClient) -> None:
    page = stance_client.get("/map")
    assert page.status_code == 200
    # CA has 3 tagged bills (the mixed one is excluded from the formula).
    assert "California — not enough data yet (3 tagged bills)" in page.text


def test_map_untracked_states_marked_not_tracked(stance_client: TestClient) -> None:
    page = stance_client.get("/map")
    assert page.status_code == 200
    assert "Texas — not tracked yet" in page.text
    assert "Florida — not tracked yet" in page.text
    assert 'class="untracked"' in page.text


def test_map_ranked_table_has_counts_and_score(stance_client: TestClient) -> None:
    page = stance_client.get("/map")
    assert page.status_code == 200
    assert "<td>North Carolina</td><td>9</td><td>5</td><td>64</td>" in page.text
    assert (
        "<td>California</td><td>2</td><td>1</td><td>Not enough data yet</td>"
        in page.text
    )
    # Ranked: the scored state sorts above the not-enough-data state.
    assert page.text.index("North Carolina</td>") < page.text.index("California</td>")


def test_map_defensive_when_stance_column_missing(client: TestClient) -> None:
    # The shared synthetic DB has classifications but NO stance column —
    # the stance engine has not landed there. The map must treat that as
    # "no stance data", not an error: NC is tracked, with 0 tagged bills.
    page = client.get("/map")
    assert page.status_code == 200
    assert "North Carolina — not enough data yet (0 tagged bills)" in page.text
    assert "Texas — not tracked yet" in page.text


def test_map_empty_db_shows_no_data_notice_and_grey_map(tmp_path: Path) -> None:
    db = tmp_path / "no-schema.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    client = TestClient(create_app(str(db)))
    page = client.get("/map")
    assert page.status_code == 200
    assert "first ingestion run" in page.text
    assert "no stance data to map yet" in page.text
    assert '<svg class="us-map"' in page.text
    assert "North Carolina — not tracked yet" in page.text


def test_map_missing_db_is_honest_not_invented(tmp_path: Path) -> None:
    client = TestClient(create_app(str(tmp_path / "does-not-exist.db")))
    page = client.get("/map")
    assert page.status_code == 200
    assert "No database is attached" in page.text


def test_nav_links_to_map(client: TestClient) -> None:
    assert 'href="/map"' in client.get("/").text
