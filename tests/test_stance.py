"""Tests for rules-based stance v1 (legitrack.stance), its storage in
both stores, and its wiring into ingestion.

All bill texts are SYNTHETIC, invented for these tests. The store
round-trips use an in-memory SQLite store and a FAKE psycopg
connection (same pattern as test_postgres_store.py); no live API, no
server, and no credentials are used anywhere here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from legitrack.ingest import ingest_state
from legitrack.legiscan_client import MasterBill, Session
from legitrack.stance import (
    apply_stance,
    determine_stance,
    load_stance_rules,
)
from legitrack.store import LocalStore, PostgresStore
from legitrack.taxonomy import Classification


def _classification(**overrides: Any) -> Classification:
    base: dict[str, Any] = {
        "category_id": "voting_access",
        "category_name": "Voting access & election administration",
        "confidence": 0.9,
    }
    base.update(overrides)
    return Classification(**base)


# -- rules + verdicts -------------------------------------------------------


def test_rules_load_with_both_signal_lists() -> None:
    rules = load_stance_rules()
    assert rules.version == "stance-v1"
    assert rules.supportive_signals and rules.harmful_signals
    # Documented exclusion: bare "equity" must NOT be a supportive
    # signal (DEI-ban bills carry the word in their titles).
    assert "equity" not in rules.supportive_signals


def test_supportive_bill() -> None:
    stance = determine_stance(
        title="Automatic Voter Registration and Health Equity Act"
    )
    assert stance.label == "supportive"
    assert "automatic voter registration" in stance.supportive_terms
    assert "health equity" in stance.supportive_terms
    assert stance.harmful_terms == ()
    assert stance.rules_version == "stance-v1"


def test_harmful_bill() -> None:
    stance = determine_stance(
        title="An Act to Restrict Early Voting and Impose Voter Identification Requirements"
    )
    assert stance.label == "harmful"
    assert "restrict" in stance.harmful_terms
    assert "voter identification requirements" in stance.harmful_terms
    assert stance.supportive_terms == ()


def test_mixed_bill() -> None:
    stance = determine_stance(
        title="Expand Early Voting and Repeal Same-Day Registration Act"
    )
    assert stance.label == "mixed"
    assert "expand" in stance.supportive_terms
    assert "repeal" in stance.harmful_terms


def test_unknown_bill_is_never_a_guess() -> None:
    stance = determine_stance(title="Synthetic County Sports Fees Act", subjects=["Fees"])
    assert stance.label == "unknown"
    assert stance.supportive_terms == ()
    assert stance.harmful_terms == ()
    assert json.loads(stance.terms_json()) == {"supportive": [], "harmful": []}


def test_ban_the_box_is_supportive_not_mixed() -> None:
    # Span containment: the harmful "ban" inside the supportive phrase
    # "ban the box" must be dropped.
    stance = determine_stance(title="Ban the Box in State Hiring Act")
    assert stance.label == "supportive"
    assert stance.supportive_terms == ("ban the box",)
    assert stance.harmful_terms == ()


def test_dei_ban_is_harmful_despite_equity_in_title() -> None:
    stance = determine_stance(
        title="Prohibit Diversity, Equity, and Inclusion in Public Education Act"
    )
    assert stance.label == "harmful"
    assert "prohibit diversity" in stance.harmful_terms
    assert stance.supportive_terms == ()


def test_prohibit_discrimination_is_supportive() -> None:
    stance = determine_stance(title="Prohibit Discrimination in Housing and Employment Act")
    assert stance.label == "supportive"
    assert "prohibit discrimination" in stance.supportive_terms


def test_signals_from_subjects_and_matched_terms() -> None:
    by_subject = determine_stance(
        title="Synthetic Elections Act",
        subjects=[{"subject_name": "Automatic Voter Registration"}],
    )
    assert by_subject.label == "supportive"

    by_matched_term = determine_stance(
        title="Synthetic Local Election Changes Act",
        matched_terms=["purge of voter"],
    )
    assert by_matched_term.label == "harmful"
    assert "purge" in by_matched_term.harmful_terms


def test_apply_stance_is_per_classification_row() -> None:
    rows = apply_stance(
        [
            _classification(matched_terms=("purge of voter",)),
            _classification(category_id="education", category_name="Education"),
        ],
        title="Synthetic Local Election Changes Act",
        subjects=[],
    )
    # Same bill, same title: the row whose own matched terms carry a
    # harmful signal is harmful; the other row stays unknown.
    assert rows[0].stance == "harmful"
    assert json.loads(rows[0].stance_terms)["harmful"] == ["purge"]
    assert rows[1].stance == "unknown"


# -- LocalStore (SQLite) ------------------------------------------------------


def test_local_store_round_trips_stance() -> None:
    store = LocalStore(":memory:")
    tagged = _classification(
        stance="supportive",
        stance_terms='{"supportive": ["expand"], "harmful": []}',
    )
    store.replace_classifications(111, [tagged])
    row = store._conn.execute(  # test-only peek at the local store
        "SELECT stance, stance_terms FROM bill_classifications WHERE legiscan_bill_id = 111"
    ).fetchone()
    assert row["stance"] == "supportive"
    assert json.loads(row["stance_terms"]) == {"supportive": ["expand"], "harmful": []}

    # A classification that never went through the stance pass stores
    # NULLs — older rows and non-stance callers stay valid.
    store.replace_classifications(222, [_classification()])
    row = store._conn.execute(
        "SELECT stance, stance_terms FROM bill_classifications WHERE legiscan_bill_id = 222"
    ).fetchone()
    assert row["stance"] is None
    assert row["stance_terms"] is None
    store.close()


def test_local_store_adds_stance_columns_to_pre_stance_db(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE bill_classifications (
            legiscan_bill_id INTEGER NOT NULL,
            category_id TEXT NOT NULL,
            category_name TEXT NOT NULL,
            confidence REAL NOT NULL,
            method TEXT NOT NULL DEFAULT 'rule',
            matched_terms TEXT,
            PRIMARY KEY (legiscan_bill_id, category_id, method)
        )
        """
    )
    conn.execute(
        "INSERT INTO bill_classifications VALUES (1, 'education', 'Education', 0.9, 'rule', '[]')"
    )
    conn.commit()
    conn.close()

    store = LocalStore(path)
    columns = {
        r[1] for r in store._conn.execute("PRAGMA table_info(bill_classifications)")
    }
    assert {"stance", "stance_terms"} <= columns
    row = store._conn.execute(
        "SELECT stance, stance_terms FROM bill_classifications WHERE legiscan_bill_id = 1"
    ).fetchone()
    assert row["stance"] is None  # pre-stance row tolerated, not guessed
    assert row["stance_terms"] is None
    store.close()


# -- PostgresStore (fake connection) -------------------------------------------


class _FakeCursor:
    def __init__(self, one: Any = None) -> None:
        self._one = one

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list:
        return []


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> _FakeCursor:
        self.calls.append((sql, params))
        if "SELECT id FROM bills" in sql:
            return _FakeCursor(one={"id": 99})
        return _FakeCursor()

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_postgres_store_writes_stance_as_text() -> None:
    conn = _FakeConnection()
    store = PostgresStore(connection=conn, apply_schema=False)
    tagged = _classification(
        matched_terms=("voter registration",),
        stance="supportive",
        stance_terms='{"supportive": ["expand", "restore"], "harmful": []}',
    )
    store.replace_classifications(111, [tagged])
    inserts = [(s, p) for s, p in conn.calls if "INSERT INTO bill_classifications" in s]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "stance" in sql and "stance_terms" in sql
    assert params is not None
    assert params[0] == 99  # internal bill PK
    assert params[6] == "supportive"
    # TEXT column: the JSON evidence goes in as a plain string,
    # not a Jsonb wrapper.
    assert params[7] == '{"supportive": ["expand", "restore"], "harmful": []}'

    conn.calls.clear()
    store.replace_classifications(111, [_classification()])
    _, params = [(s, p) for s, p in conn.calls if "INSERT INTO bill_classifications" in s][0]
    assert params is not None
    assert params[6] is None and params[7] is None  # no stance pass -> NULLs


# -- ingestion wiring -----------------------------------------------------------


class _FakeClient:
    """One-session, one-bill stand-in with stance-signalling language."""

    def __init__(self) -> None:
        self.queries_used = 0

    def get_session_list(self, state: str) -> list[Session]:
        self.queries_used += 1
        return [
            Session(
                session_id=9999, state_abbr="NC", year_start=2025, year_end=2026,
                session_name="Synthetic Session", session_tag="Regular Session",
                special=False, sine_die=False, dataset_hash="synthetic-hash",
            )
        ]

    def get_master_list(self, session_id: int) -> list[MasterBill]:
        self.queries_used += 1
        return [
            MasterBill(
                bill_id=111, number="H1", change_hash="hash-aaa", status=1,
                title="Expand Voter Registration and Restore the Vote Act",
                description="", status_date="2025-01-15", last_action="Filed",
                last_action_date="2025-01-15", url="https://example.invalid/H1",
                raw={"bill_id": 111},
            )
        ]

    def get_bill(self, bill_id: int) -> dict[str, Any]:
        self.queries_used += 1
        return {
            "bill_id": bill_id,
            "bill_number": "H1",
            "title": "Expand Voter Registration and Restore the Vote Act",
            "description": "",
            "status": 1,
            "subjects": [{"subject_name": "Elections"}],
            "texts": [],
            "votes": [],
            "sponsors": [],
            "history": [],
        }


def test_ingest_stores_stance_with_classifications() -> None:
    store = LocalStore(":memory:")
    ingest_state(_FakeClient(), store, "NC")
    rows = store._conn.execute(
        "SELECT category_id, stance, stance_terms FROM bill_classifications"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["category_id"] == "voting_access"
    assert row["stance"] == "supportive"
    payload = json.loads(row["stance_terms"])
    assert "expand" in payload["supportive"]
    assert "restore" in payload["supportive"]
    assert payload["harmful"] == []
    store.close()
