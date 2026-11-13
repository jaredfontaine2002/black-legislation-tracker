"""Unit tests for PostgresStore against a FAKE psycopg connection.

The fake records every (SQL, params) pair and answers with canned rows,
so these tests pin the SQL shape — %s placeholders, ON CONFLICT upsert
keys, JSONB wrapping, date validation — without a server. The real
server-backed verification lives in test_postgres_live.py (skipped
unless LEGITRACK_PG_TEST_URL is set; it was run for real against an
embedded Postgres when the backend landed — see README).

No credentials are used anywhere here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psycopg")

import legitrack.store as store_module  # noqa: E402
from legitrack.legiscan_client import MasterBill, Session  # noqa: E402
from legitrack.legislators import Legislator  # noqa: E402
from legitrack.store import LocalStore, PostgresStore, open_store  # noqa: E402
from legitrack.taxonomy import Classification  # noqa: E402


class FakeCursor:
    def __init__(self, one: Any = None, all_rows: Any = None) -> None:
        self._one = one
        self._all = all_rows or []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> Any:
        return list(self._all)


class FakeConnection:
    """Records SQL + params; canned answers keyed on SQL fragments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple | None]] = []
        self.commits = 0
        self.closed = False

    def execute(self, sql: str, params: tuple | None = None) -> FakeCursor:
        self.calls.append((sql, params))
        if "INSERT INTO jurisdictions" in sql:
            return FakeCursor(one={"id": 7})
        if "INSERT INTO pipeline_runs" in sql:
            return FakeCursor(one={"id": 42})
        if "SELECT id, jurisdiction_id FROM sessions" in sql:
            return FakeCursor(one={"id": 3, "jurisdiction_id": 7})
        if "SELECT id FROM bills" in sql:
            return FakeCursor(one={"id": 99})
        if "legiscan_bill_id, b.change_hash" in sql:
            return FakeCursor(all_rows=[{"legiscan_bill_id": 111, "change_hash": "hash-aaa"}])
        if "SELECT * FROM pipeline_runs" in sql:
            return FakeCursor(all_rows=[{"id": 42, "job": "legiscan_ingest", "status": "ok"}])
        if "COUNT(*)" in sql and "FROM legislators" in sql:
            return FakeCursor(one={"n": 5})
        if "COUNT(*)" in sql:
            return FakeCursor(one={"n": 3})
        return FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    def sql_text(self) -> str:
        return "\n".join(sql for sql, _ in self.calls)


def _store(conn: FakeConnection) -> PostgresStore:
    return PostgresStore(connection=conn, apply_schema=False)


def _session() -> Session:
    return Session(
        session_id=9999, state_abbr="NC", year_start=2025, year_end=2026,
        session_name="Synthetic Session", session_tag="Regular Session",
        special=False, sine_die=False, dataset_hash="synthetic-hash",
    )


def _bill() -> MasterBill:
    return MasterBill(
        bill_id=111, number="H1", change_hash="hash-aaa", status=1,
        title="Synthetic Voting Access Act", description="Synthetic description",
        status_date="2025-01-15", last_action="Filed", last_action_date="",
        url="https://example.invalid/H1", raw={"bill_id": 111, "number": "H1"},
    )


def test_schema_applied_on_construction(tmp_path: Path) -> None:
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE sentinel_table (id INTEGER);", encoding="utf-8")
    conn = FakeConnection()
    PostgresStore(connection=conn, schema_path=schema)
    assert "sentinel_table" in conn.sql_text()
    assert conn.commits >= 1


def test_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        PostgresStore()


def test_session_and_bill_upsert_sql_and_params() -> None:
    conn = FakeConnection()
    store = _store(conn)
    store.upsert_session(_session())
    sql = conn.sql_text()
    assert "INSERT INTO jurisdictions" in sql
    assert "ON CONFLICT (state_abbr)" in sql
    assert "INSERT INTO sessions" in sql
    assert "ON CONFLICT (legiscan_session_id)" in sql

    hashes = store.stored_hashes(9999)
    assert hashes == {111: "hash-aaa"}
    join_sql = [s for s, _ in conn.calls if "change_hash" in s][0]
    assert "JOIN sessions" in join_sql  # PG bills link via session PK, not a state column

    conn.calls.clear()
    store.upsert_master_bill(9999, _bill(), "Introduced")
    sql, params = conn.calls[-1]
    assert "ON CONFLICT (legiscan_bill_id)" in sql
    assert "?" not in sql and "%s" in sql  # psycopg paramstyle, not sqlite
    assert params is not None
    assert params[0] == 111 and params[1] == 3 and params[2] == 7  # bill, session PK, jurisdiction
    assert params[8] == "2025-01-15"  # valid status_date kept
    assert params[10] is None  # empty last_action_date validated to NULL, not ''
    raw_param = params[13]
    assert getattr(raw_param, "obj", None) == {"bill_id": 111, "number": "H1"}  # Jsonb-wrapped

    store.store_detail(111, {"bill_id": 111, "title": "Detail"})
    sql, params = conn.calls[-1]
    assert "UPDATE bills SET detail" in sql
    assert getattr(params[0], "obj", None) == {"bill_id": 111, "title": "Detail"}

    assert store.bill_count() == 3
    assert store.bill_count(session_id=9999) == 3


def test_classifications_legislator_and_runs() -> None:
    conn = FakeConnection()
    store = _store(conn)
    classification = Classification(
        category_id="voting_access",
        category_name="Voting access & election administration",
        confidence=0.9,
        matched_terms=("voting",),
        matched_subjects=("Elections",),
    )
    store.replace_classifications(111, [classification])
    sql = conn.sql_text()
    assert "DELETE FROM bill_classifications WHERE bill_id" in sql
    assert "ON CONFLICT (bill_id, category, method)" in sql
    insert_params = [p for s, p in conn.calls if "INSERT INTO bill_classifications" in s][0]
    assert insert_params is not None
    assert insert_params[0] == 99  # internal bill PK resolved from legiscan id 111
    assert insert_params[1] == "voting_access"
    assert getattr(insert_params[5], "obj", None) == ["voting", "Elections"]

    person = Legislator(
        openstates_person_id="ocd-person/synthetic-1", name="Synthetic Person",
        first_name="Synthetic", last_name="Person", party="Democratic",
        chamber="lower", role_title="Representative", district="38",
        email_published="synthetic@example.gov", photo_url="https://example.gov/p.jpg",
        jurisdiction_id="ocd-jurisdiction/country:us/state:nc/government", state_abbr="NC",
    )
    store.upsert_legislator(person)
    sql, params = conn.calls[-1]
    assert "ON CONFLICT (openstates_person_id)" in sql
    assert params is not None and params[1] == "ocd-person/synthetic-1"
    assert store.legislator_count("NC") == 5

    run_id = store.start_run("legiscan_ingest", "NC", 9999)
    assert run_id == 42
    store.finish_run(run_id, status="ok", bills_seen=3, bills_changed=3,
                     bills_fetched=3, queries_used=5)
    sql, params = conn.calls[-1]
    assert "UPDATE pipeline_runs SET finished_at = now()" in sql
    assert params == ("ok", 3, 3, 3, 5, None, 42)
    assert store.runs()[0]["job"] == "legiscan_ingest"


def test_open_store_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # DATABASE_URL set -> PostgresStore (stubbed here so no server is needed)
    created: dict[str, Any] = {}

    class StubPG:
        def __init__(self) -> None:
            created["pg"] = True

    monkeypatch.setattr(store_module, "PostgresStore", StubPG)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    assert isinstance(open_store(tmp_path / "ignored.db"), StubPG)

    # No DATABASE_URL -> SQLite at the explicit path, as before
    monkeypatch.delenv("DATABASE_URL")
    sqlite_store = open_store(tmp_path / "local.db")
    assert isinstance(sqlite_store, LocalStore)
    sqlite_store.close()
