"""Stores for Phase 1: the SQLite local store and the Postgres backend.

``LocalStore`` is the lean SQLite mirror used for local runs and tests.
``PostgresStore`` implements the same ingestion interface against the
Render-managed Postgres (DDL in ``schema.sql``, applied idempotently on
construction). ``open_store()`` is the factory both ingestion CLIs use:
Postgres when ``DATABASE_URL`` is set, SQLite otherwise — so local
behaviour is unchanged unless the env var is present.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .legiscan_client import MasterBill, Session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    legiscan_session_id INTEGER PRIMARY KEY,
    state_abbr TEXT NOT NULL,
    year_start INTEGER,
    year_end INTEGER,
    session_name TEXT,
    dataset_hash TEXT
);
CREATE TABLE IF NOT EXISTS bills (
    legiscan_bill_id INTEGER PRIMARY KEY,
    legiscan_session_id INTEGER NOT NULL,
    number TEXT NOT NULL,
    title TEXT,
    change_hash TEXT NOT NULL,
    status_code INTEGER,
    public_status TEXT,
    status_date TEXT,
    last_action TEXT,
    last_action_date TEXT,
    url TEXT,
    detail_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bill_classifications (
    legiscan_bill_id INTEGER NOT NULL,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    confidence REAL NOT NULL,
    method TEXT NOT NULL DEFAULT 'rule',
    matched_terms TEXT,
    stance TEXT,
    stance_terms TEXT,
    PRIMARY KEY (legiscan_bill_id, category_id, method)
);
CREATE TABLE IF NOT EXISTS legislators (
    openstates_person_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    party TEXT,
    chamber TEXT,
    role_title TEXT,
    district TEXT,
    email_published TEXT,
    photo_url TEXT,
    jurisdiction_id TEXT,
    state_abbr TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legislator_openstates_id TEXT NOT NULL REFERENCES legislators(openstates_person_id),
    role TEXT NOT NULL CHECK (role IN ('LC', 'LD', 'Chief of Staff', 'District Director')),
    name TEXT NOT NULL,
    email TEXT,
    source_url TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    state_abbr TEXT,
    legiscan_session_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    bills_seen INTEGER NOT NULL DEFAULT 0,
    bills_changed INTEGER NOT NULL DEFAULT 0,
    bills_fetched INTEGER NOT NULL DEFAULT 0,
    queries_used INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


def utc_now_iso() -> str:
    """Current UTC time as an ISO string (stored on every row/run)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Staff roles the Act Box staff layer recognises (SPEC sections 7 / 12).
#: LC = Legislative Correspondent, LD = Legislative Director.
STAFF_ROLES = ("LC", "LD", "Chief of Staff", "District Director")


class LocalStore:
    """SQLite-backed Phase 1 store. Pass ':memory:' for tests."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive guards for databases created before a column existed
        (CREATE TABLE IF NOT EXISTS above cannot add columns to an
        existing table). NULL stance on old rows means "stance pass has
        not run for this row" and reads tolerate it."""
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(bill_classifications)")
        }
        for column in ("stance", "stance_terms"):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE bill_classifications ADD COLUMN {column} TEXT"
                )

    def close(self) -> None:
        self._conn.close()

    # -- sessions -----------------------------------------------------------

    def upsert_session(self, session: Session) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions (legiscan_session_id, state_abbr, year_start,
                                  year_end, session_name, dataset_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(legiscan_session_id) DO UPDATE SET
                state_abbr=excluded.state_abbr, year_start=excluded.year_start,
                year_end=excluded.year_end, session_name=excluded.session_name,
                dataset_hash=excluded.dataset_hash
            """,
            (
                session.session_id,
                session.state_abbr,
                session.year_start,
                session.year_end,
                session.session_name,
                session.dataset_hash,
            ),
        )
        self._conn.commit()

    # -- bills ----------------------------------------------------------------

    def stored_hashes(self, session_id: int) -> dict[int, str]:
        """bill_id -> change_hash currently stored for one session."""
        rows = self._conn.execute(
            "SELECT legiscan_bill_id, change_hash FROM bills WHERE legiscan_session_id = ?",
            (session_id,),
        ).fetchall()
        return {int(r["legiscan_bill_id"]): str(r["change_hash"]) for r in rows}

    def upsert_master_bill(self, session_id: int, bill: MasterBill, public_status: str) -> None:
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO bills (legiscan_bill_id, legiscan_session_id, number, title,
                               change_hash, status_code, public_status, status_date,
                               last_action, last_action_date, url, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(legiscan_bill_id) DO UPDATE SET
                legiscan_session_id=excluded.legiscan_session_id,
                number=excluded.number, title=excluded.title,
                change_hash=excluded.change_hash, status_code=excluded.status_code,
                public_status=excluded.public_status, status_date=excluded.status_date,
                last_action=excluded.last_action, last_action_date=excluded.last_action_date,
                url=excluded.url, last_seen_at=excluded.last_seen_at
            """,
            (
                bill.bill_id,
                session_id,
                bill.number,
                bill.title,
                bill.change_hash,
                bill.status,
                public_status,
                bill.status_date,
                bill.last_action,
                bill.last_action_date,
                bill.url,
                now,
                now,
            ),
        )
        self._conn.commit()

    def store_detail(self, bill_id: int, detail: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE bills SET detail_json = ? WHERE legiscan_bill_id = ?",
            (json.dumps(detail), int(bill_id)),
        )
        self._conn.commit()

    def bill_count(self, session_id: int | None = None) -> int:
        if session_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM bills WHERE legiscan_session_id = ?", (session_id,)
            ).fetchone()
        return int(row["n"])

    # -- legislators ----------------------------------------------------------

    def upsert_legislator(self, person: Any) -> None:
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO legislators (openstates_person_id, name, first_name, last_name,
                                     party, chamber, role_title, district,
                                     email_published, photo_url, jurisdiction_id,
                                     state_abbr, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(openstates_person_id) DO UPDATE SET
                name=excluded.name, first_name=excluded.first_name,
                last_name=excluded.last_name, party=excluded.party,
                chamber=excluded.chamber, role_title=excluded.role_title,
                district=excluded.district, email_published=excluded.email_published,
                photo_url=excluded.photo_url, jurisdiction_id=excluded.jurisdiction_id,
                state_abbr=excluded.state_abbr, last_seen_at=excluded.last_seen_at
            """,
            (
                person.openstates_person_id, person.name, person.first_name,
                person.last_name, person.party, person.chamber, person.role_title,
                person.district, person.email_published, person.photo_url,
                person.jurisdiction_id, person.state_abbr, now, now,
            ),
        )
        self._conn.commit()

    def legislator_count(self, state_abbr: str | None = None) -> int:
        if state_abbr is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM legislators").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM legislators WHERE state_abbr = ?",
                (state_abbr.upper(),),
            ).fetchone()
        return int(row["n"])

    # -- staff contacts (Act Box staff layer) ---------------------------------
    # Rows enter ONLY from verified sources (a future staff ingestion);
    # nothing here constructs or guesses an address. Starts empty.

    def add_staff_contact(
        self,
        openstates_person_id: str,
        *,
        role: str,
        name: str,
        email: str | None = None,
        source_url: str | None = None,
        verified_at: str | None = None,
    ) -> int:
        """Store one verified staff contact; returns the new row id."""
        if role not in STAFF_ROLES:
            raise ValueError(f"unknown staff role {role!r}; expected one of {STAFF_ROLES}")
        now = utc_now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO staff_contacts (legislator_openstates_id, role, name,
                                        email, source_url, verified_at,
                                        created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                openstates_person_id, role, name, email or None,
                source_url or None, verified_at or None, now, now,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def staff_contacts_for(self, openstates_person_ids: list[str]) -> list[dict[str, Any]]:
        """All stored staff contacts for the given legislators, role order
        LD first (the staffer who writes the memo), then LC, CoS, DD."""
        if not openstates_person_ids:
            return []
        placeholders = ", ".join("?" for _ in openstates_person_ids)
        rows = self._conn.execute(
            f"""
            SELECT id, legislator_openstates_id, role, name, email,
                   source_url, verified_at
            FROM staff_contacts
            WHERE legislator_openstates_id IN ({placeholders})
            ORDER BY CASE role WHEN 'LD' THEN 0 WHEN 'LC' THEN 1
                     WHEN 'Chief of Staff' THEN 2 ELSE 3 END, name
            """,
            tuple(openstates_person_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- classifications --------------------------------------------------------

    def replace_classifications(self, bill_id: int, classifications: list[Any]) -> None:
        self._conn.execute(
            "DELETE FROM bill_classifications WHERE legiscan_bill_id = ? AND method = 'rule'",
            (int(bill_id),),
        )
        for c in classifications:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO bill_classifications
                    (legiscan_bill_id, category_id, category_name, confidence, method,
                     matched_terms, stance, stance_terms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(bill_id),
                    c.category_id,
                    c.category_name,
                    c.confidence,
                    c.method,
                    json.dumps(list(c.matched_terms) + list(getattr(c, "matched_subjects", ()))),
                    getattr(c, "stance", None),
                    getattr(c, "stance_terms", "") or None,
                ),
            )
        self._conn.commit()

    # -- pipeline runs -------------------------------------------------------------

    def start_run(self, job: str, state_abbr: str, session_id: int | None) -> int:
        cur = self._conn.execute(
            "INSERT INTO pipeline_runs (job, state_abbr, legiscan_session_id, started_at, status)"
            " VALUES (?, ?, ?, ?, 'running')",
            (job, state_abbr, session_id, utc_now_iso()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        bills_seen: int,
        bills_changed: int,
        bills_fetched: int,
        queries_used: int,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE pipeline_runs SET finished_at = ?, status = ?, bills_seen = ?,
                bills_changed = ?, bills_fetched = ?, queries_used = ?, error = ?
            WHERE id = ?
            """,
            (
                utc_now_iso(),
                status,
                bills_seen,
                bills_changed,
                bills_fetched,
                queries_used,
                error,
                run_id,
            ),
        )
        self._conn.commit()

    def runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM pipeline_runs ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Postgres backend (Render production store)
# ---------------------------------------------------------------------------

def _pg_date(value: Any) -> str | None:
    """ISO date string for a DATE column, or None for blanks/garbage.

    LegiScan date fields arrive as '' when unset; passing them through
    unchecked would fail the whole upsert on the Postgres side, where
    SQLite happily stored the empty string.
    """
    if not value:
        return None
    text = str(value)[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _jsonb_param(value: Any) -> Any:
    """Wrap a value for a JSONB column (psycopg Jsonb when available)."""
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    except Exception:  # psycopg not installed (fake-connection tests)
        return json.dumps(value)


def _row_id(row: Any) -> int:
    if isinstance(row, dict):
        return int(row["id"])
    return int(row[0])


class PostgresStore:
    """Postgres implementation of the ingestion store interface.

    Same method set and upsert keys as ``LocalStore``; the differences
    are all schema-mapping, handled here:

    * ``jurisdictions``/``sessions`` are normalised in schema.sql, so a
      session upsert first upserts its jurisdiction (keyed on
      ``state_abbr``; the jurisdiction ``name`` is seeded with the
      abbreviation — enrichment with full state names is a later step).
      Bills reference the internal session PK, resolved per call.
    * Bill detail JSON lives in ``bills.detail`` (JSONB; the SQLite
      mirror calls the same payload ``detail_json``).
    * Classifications reference the internal bill PK and store both the
      taxonomy ``category`` id and its ``category_name`` label.
    * Legislators are keyed on ``openstates_person_id`` and linked to
      their jurisdiction via the OCD jurisdiction id OpenStates sends.
    * Timestamps are database-side ``now()``; ``runs()`` therefore
      returns ``datetime`` objects where SQLite returns ISO strings.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        connection: Any = None,
        apply_schema: bool = True,
        schema_path: str | Path | None = None,
    ) -> None:
        if connection is not None:
            self._conn = connection
        else:
            import psycopg
            from psycopg.rows import dict_row

            url = database_url or os.environ.get("DATABASE_URL", "")
            if not url:
                raise ValueError(
                    "DATABASE_URL is not set; PostgresStore needs a connection "
                    "string (or an injected connection for tests)"
                )
            self._conn = psycopg.connect(url, row_factory=dict_row)
        if apply_schema:
            self._apply_schema(schema_path)

    def _apply_schema(self, schema_path: str | Path | None) -> None:
        path = (
            Path(schema_path)
            if schema_path
            else Path(__file__).resolve().parents[2] / "schema.sql"
        )
        sql = path.read_text(encoding="utf-8")
        # schema.sql is pure DDL with no parameters; Postgres accepts the
        # whole idempotent script in a single execute.
        self._conn.execute(sql)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- jurisdictions / sessions -------------------------------------------

    def _jurisdiction_id(self, state_abbr: str, openstates_id: str | None = None) -> int:
        row = self._conn.execute(
            """
            INSERT INTO jurisdictions (state_abbr, name, openstates_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (state_abbr) DO UPDATE SET
                openstates_id = COALESCE(EXCLUDED.openstates_id, jurisdictions.openstates_id),
                updated_at = now()
            RETURNING id
            """,
            (state_abbr.upper(), state_abbr.upper(), openstates_id or None),
        ).fetchone()
        return _row_id(row)

    def upsert_session(self, session: Session) -> None:
        jurisdiction_id = self._jurisdiction_id(session.state_abbr)
        self._conn.execute(
            """
            INSERT INTO sessions (jurisdiction_id, legiscan_session_id, year_start,
                                  year_end, session_name, session_tag, is_special,
                                  sine_die, dataset_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (legiscan_session_id) DO UPDATE SET
                jurisdiction_id=EXCLUDED.jurisdiction_id,
                year_start=EXCLUDED.year_start, year_end=EXCLUDED.year_end,
                session_name=EXCLUDED.session_name, session_tag=EXCLUDED.session_tag,
                is_special=EXCLUDED.is_special, sine_die=EXCLUDED.sine_die,
                dataset_hash=EXCLUDED.dataset_hash, updated_at=now()
            """,
            (
                jurisdiction_id,
                session.session_id,
                session.year_start,
                session.year_end,
                session.session_name,
                session.session_tag or None,
                bool(session.special),
                bool(session.sine_die),
                session.dataset_hash or None,
            ),
        )
        self._conn.commit()

    def _session_row(self, legiscan_session_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, jurisdiction_id FROM sessions WHERE legiscan_session_id = %s",
            (int(legiscan_session_id),),
        ).fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return {"id": int(row["id"]), "jurisdiction_id": row.get("jurisdiction_id")}
        return {"id": int(row[0]), "jurisdiction_id": row[1]}

    # -- bills ----------------------------------------------------------------

    def stored_hashes(self, session_id: int) -> dict[int, str]:
        """bill_id -> change_hash currently stored for one session."""
        rows = self._conn.execute(
            """
            SELECT b.legiscan_bill_id, b.change_hash
            FROM bills b JOIN sessions s ON s.id = b.session_id
            WHERE s.legiscan_session_id = %s
            """,
            (int(session_id),),
        ).fetchall()
        out: dict[int, str] = {}
        for r in rows:
            if isinstance(r, dict):
                out[int(r["legiscan_bill_id"])] = str(r["change_hash"])
            else:
                out[int(r[0])] = str(r[1])
        return out

    def upsert_master_bill(self, session_id: int, bill: MasterBill, public_status: str) -> None:
        session_row = self._session_row(session_id)
        session_pk = session_row["id"] if session_row else None
        jurisdiction_id = session_row["jurisdiction_id"] if session_row else None
        self._conn.execute(
            """
            INSERT INTO bills (legiscan_bill_id, session_id, jurisdiction_id,
                               bill_number, title, description, status_code,
                               public_status, status_date, last_action,
                               last_action_date, change_hash, legiscan_url, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (legiscan_bill_id) DO UPDATE SET
                session_id=EXCLUDED.session_id,
                jurisdiction_id=EXCLUDED.jurisdiction_id,
                bill_number=EXCLUDED.bill_number, title=EXCLUDED.title,
                description=EXCLUDED.description, status_code=EXCLUDED.status_code,
                public_status=EXCLUDED.public_status,
                status_date=EXCLUDED.status_date, last_action=EXCLUDED.last_action,
                last_action_date=EXCLUDED.last_action_date,
                change_hash=EXCLUDED.change_hash, legiscan_url=EXCLUDED.legiscan_url,
                raw=EXCLUDED.raw, last_seen_at=now(), updated_at=now()
            """,
            (
                bill.bill_id,
                session_pk,
                jurisdiction_id,
                bill.number,
                bill.title or None,
                bill.description or None,
                bill.status,
                public_status,
                _pg_date(bill.status_date),
                bill.last_action or None,
                _pg_date(bill.last_action_date),
                bill.change_hash,
                bill.url or None,
                _jsonb_param(bill.raw or {}),
            ),
        )
        self._conn.commit()

    def store_detail(self, bill_id: int, detail: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE bills SET detail = %s, updated_at = now() WHERE legiscan_bill_id = %s",
            (_jsonb_param(detail), int(bill_id)),
        )
        self._conn.commit()

    def bill_count(self, session_id: int | None = None) -> int:
        if session_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM bills b
                JOIN sessions s ON s.id = b.session_id
                WHERE s.legiscan_session_id = %s
                """,
                (int(session_id),),
            ).fetchone()
        if isinstance(row, dict):
            return int(row["n"])
        return int(row[0])

    # -- legislators ----------------------------------------------------------

    def upsert_legislator(self, person: Any) -> None:
        jurisdiction_id = None
        if getattr(person, "state_abbr", ""):
            jurisdiction_id = self._jurisdiction_id(
                person.state_abbr, getattr(person, "jurisdiction_id", "") or None
            )
        self._conn.execute(
            """
            INSERT INTO legislators (jurisdiction_id, openstates_person_id, name,
                                     first_name, last_name, party, chamber,
                                     role_title, district, photo_url,
                                     email_published, state_abbr, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'openstates')
            ON CONFLICT (openstates_person_id) DO UPDATE SET
                jurisdiction_id=EXCLUDED.jurisdiction_id, name=EXCLUDED.name,
                first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name,
                party=EXCLUDED.party, chamber=EXCLUDED.chamber,
                role_title=EXCLUDED.role_title, district=EXCLUDED.district,
                photo_url=EXCLUDED.photo_url,
                email_published=EXCLUDED.email_published,
                state_abbr=EXCLUDED.state_abbr, updated_at=now()
            """,
            (
                jurisdiction_id,
                person.openstates_person_id, person.name,
                person.first_name or None, person.last_name or None,
                person.party or None, person.chamber or None,
                person.role_title or None, person.district or None,
                person.photo_url or None, person.email_published or None,
                (person.state_abbr or "").upper() or None,
            ),
        )
        self._conn.commit()

    def legislator_count(self, state_abbr: str | None = None) -> int:
        if state_abbr is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM legislators").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM legislators WHERE state_abbr = %s",
                (state_abbr.upper(),),
            ).fetchone()
        if isinstance(row, dict):
            return int(row["n"])
        return int(row[0])

    # -- staff contacts (Act Box staff layer) ---------------------------------
    # Same interface as LocalStore; schema.sql keys staff_contacts on the
    # internal legislators PK, so inserts resolve it from the OpenStates
    # person id the rest of the interface uses. Rows enter ONLY from
    # verified sources; nothing here constructs or guesses an address.

    def add_staff_contact(
        self,
        openstates_person_id: str,
        *,
        role: str,
        name: str,
        email: str | None = None,
        source_url: str | None = None,
        verified_at: str | None = None,
    ) -> int:
        """Store one verified staff contact; returns the new row id."""
        if role not in STAFF_ROLES:
            raise ValueError(f"unknown staff role {role!r}; expected one of {STAFF_ROLES}")
        row = self._conn.execute(
            "SELECT id FROM legislators WHERE openstates_person_id = %s",
            (openstates_person_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown legislator {openstates_person_id!r}")
        legislator_pk = _row_id(row)
        row = self._conn.execute(
            """
            INSERT INTO staff_contacts (legislator_id, role, name, email,
                                        source_url, verified_at)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                legislator_pk, role, name, email or None,
                source_url or None, verified_at or None,
            ),
        ).fetchone()
        self._conn.commit()
        return _row_id(row)

    def staff_contacts_for(self, openstates_person_ids: list[str]) -> list[dict[str, Any]]:
        """All stored staff contacts for the given legislators (LD first)."""
        if not openstates_person_ids:
            return []
        placeholders = ", ".join("%s" for _ in openstates_person_ids)
        rows = self._conn.execute(
            f"""
            SELECT sc.id, l.openstates_person_id AS legislator_openstates_id,
                   sc.role, sc.name, sc.email, sc.source_url,
                   sc.verified_at::text AS verified_at
            FROM staff_contacts sc
            JOIN legislators l ON l.id = sc.legislator_id
            WHERE l.openstates_person_id IN ({placeholders})
            ORDER BY CASE sc.role WHEN 'LD' THEN 0 WHEN 'LC' THEN 1
                     WHEN 'Chief of Staff' THEN 2 ELSE 3 END, sc.name
            """,
            tuple(openstates_person_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- classifications --------------------------------------------------------

    def replace_classifications(self, bill_id: int, classifications: list[Any]) -> None:
        row = self._conn.execute(
            "SELECT id FROM bills WHERE legiscan_bill_id = %s", (int(bill_id),)
        ).fetchone()
        if row is None:
            # Postgres has a real FK (SQLite's mirror table does not), so a
            # classification for an unknown bill cannot be stored. The
            # ingestion flow always upserts the master bill first.
            return
        bill_pk = _row_id(row)
        self._conn.execute(
            "DELETE FROM bill_classifications WHERE bill_id = %s AND method = 'rule'",
            (bill_pk,),
        )
        for c in classifications:
            matched = list(c.matched_terms) + list(getattr(c, "matched_subjects", ()))
            self._conn.execute(
                """
                INSERT INTO bill_classifications
                    (bill_id, category, category_name, confidence, method,
                     matched_terms, stance, stance_terms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bill_id, category, method) DO UPDATE SET
                    category_name=EXCLUDED.category_name,
                    confidence=EXCLUDED.confidence,
                    matched_terms=EXCLUDED.matched_terms,
                    stance=EXCLUDED.stance,
                    stance_terms=EXCLUDED.stance_terms
                """,
                (
                    bill_pk,
                    c.category_id,
                    c.category_name,
                    c.confidence,
                    c.method,
                    _jsonb_param(matched),
                    getattr(c, "stance", None),
                    # stance_terms is a TEXT column holding a JSON string —
                    # passed through as-is, NOT Jsonb-wrapped.
                    getattr(c, "stance_terms", "") or None,
                ),
            )
        self._conn.commit()

    # -- pipeline runs -------------------------------------------------------------

    def start_run(self, job: str, state_abbr: str, session_id: int | None) -> int:
        row = self._conn.execute(
            "INSERT INTO pipeline_runs (job, state_abbr, legiscan_session_id, status)"
            " VALUES (%s, %s, %s, 'running') RETURNING id",
            (job, state_abbr, session_id),
        ).fetchone()
        self._conn.commit()
        return _row_id(row)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        bills_seen: int,
        bills_changed: int,
        bills_fetched: int,
        queries_used: int,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE pipeline_runs SET finished_at = now(), status = %s, bills_seen = %s,
                bills_changed = %s, bills_fetched = %s, queries_used = %s, error = %s
            WHERE id = %s
            """,
            (status, bills_seen, bills_changed, bills_fetched, queries_used, error, run_id),
        )
        self._conn.commit()

    def runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM pipeline_runs ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def open_store(db_path: str | Path | None = None) -> LocalStore | PostgresStore:
    """Store factory used by the ingestion CLIs.

    ``DATABASE_URL`` set (Render) -> :class:`PostgresStore`. Otherwise the
    existing SQLite behaviour, unchanged: an explicit ``db_path`` (the
    CLIs' ``--db`` flag) wins, then ``LEGITRACK_DB``, then the default
    ``legitrack.db`` in the working directory.
    """
    if os.environ.get("DATABASE_URL"):
        return PostgresStore()
    if db_path is not None:
        return LocalStore(db_path)
    return LocalStore(os.environ.get("LEGITRACK_DB", "legitrack.db"))
