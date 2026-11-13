"""Tests for Act Box v1 (legitrack.actbox).

Everything here is SYNTHETIC except the Census fixture's *shape*, which
was recorded from a real US Census Geocoder response (trimmed — see the
fixture's _comment). Names, emails, and bills are invented for tests and
use example.gov-style addresses. No live API calls: the geocoder
transport is faked, and the address used is a published government
street address (the NC State Legislative Building block).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from legitrack.actbox import (  # noqa: E402
    build_mailto,
    build_subject,
    census_url,
    create_actbox_router,
    default_position,
    geocode_address,
    match_representatives,
    parse_geocoder_payload,
)
from legitrack.legiscan_client import MasterBill, Session  # noqa: E402
from legitrack.legislators import Legislator  # noqa: E402
from legitrack.store import LocalStore, PostgresStore  # noqa: E402
from legitrack.taxonomy import Classification  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

SENATOR_ID = "ocd-person/synthetic-senator-14"
REP_ID = "ocd-person/synthetic-rep-38"
OTHER_REP_ID = "ocd-person/synthetic-rep-99"
NOEMAIL_SENATOR_ID = "ocd-person/synthetic-senator-16"


def _census_payload() -> dict[str, Any]:
    return json.loads(
        (FIXTURES / "census_geocoder_nc.json").read_text(encoding="utf-8")
    )


def _fake_transport(payload: dict[str, Any], seen: list[str] | None = None):
    def transport(url: str) -> dict[str, Any]:
        if seen is not None:
            seen.append(url)
        return payload

    return transport


# ---------------------------------------------------------------------------
# Census parsing
# ---------------------------------------------------------------------------

def test_census_url_uses_official_endpoint_and_params() -> None:
    url = census_url("16 W Jones St, Raleigh, NC 27601")
    assert url.startswith(
        "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?"
    )
    assert "benchmark=Public_AR_Current" in url
    assert "vintage=Current_Current" in url
    assert "format=json" in url
    assert "16+W+Jones+St" in url


def test_parse_geocoder_payload_extracts_districts() -> None:
    districts = parse_geocoder_payload(_census_payload())
    assert districts is not None
    assert districts.state_abbr == "NC"
    assert districts.zip_code == "27601"
    assert districts.congressional_district == "2"
    assert districts.congressional_label == "Congressional District 2"
    # BASENAME carries the plain number, matching OpenStates districts.
    assert districts.state_senate_district == "14"
    assert districts.state_house_district == "38"
    assert districts.matched_address.startswith("16 W JONES ST")


def test_parse_geocoder_payload_no_match_is_none_not_a_guess() -> None:
    assert parse_geocoder_payload({"result": {"addressMatches": []}}) is None
    assert parse_geocoder_payload({"result": {}}) is None
    assert parse_geocoder_payload({}) is None


def test_geocode_address_uses_injected_transport() -> None:
    seen: list[str] = []
    districts = geocode_address(
        "16 W Jones St, Raleigh, NC 27601",
        transport=_fake_transport(_census_payload(), seen),
    )
    assert districts is not None and districts.state_abbr == "NC"
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Matching + defaults (pure functions)
# ---------------------------------------------------------------------------

def _person(pid: str, chamber: str, district: str, **kw: Any) -> dict[str, Any]:
    person: dict[str, Any] = {
        "person_id": pid, "name": pid, "party": "Democratic",
        "chamber": chamber, "role_title": "", "district": district,
        "email_published": "x@example.gov", "photo_url": "",
    }
    person.update(kw)
    return person


def test_match_representatives_by_district() -> None:
    districts = geocode_address(
        "x", transport=_fake_transport(_census_payload())
    )
    assert districts is not None
    people = [
        _person(SENATOR_ID, "upper", "14"),
        _person(NOEMAIL_SENATOR_ID, "upper", "16"),
        _person(REP_ID, "lower", "38"),
        _person(OTHER_REP_ID, "lower", "99"),
        _person("ocd-person/gov", "executive", ""),
    ]
    matches = {m.chamber: m for m in match_representatives(people, districts)}
    assert [p["person_id"] for p in matches["upper"].matched] == [SENATOR_ID]
    assert [p["person_id"] for p in matches["lower"].matched] == [REP_ID]
    # The full chambers stay listed (partial-matching honesty), and the
    # executive is never presented as a district rep.
    assert len(matches["upper"].everyone) == 2
    assert len(matches["lower"].everyone) == 2
    assert "executive" not in matches


def test_match_representatives_normalises_leading_zeros() -> None:
    districts = geocode_address(
        "x", transport=_fake_transport(_census_payload())
    )
    assert districts is not None
    people = [_person(SENATOR_ID, "upper", "014")]
    matches = match_representatives(people, districts)
    assert matches[0].matched and matches[0].matched[0]["person_id"] == SENATOR_ID


def test_default_position_follows_stance() -> None:
    assert default_position("supportive") == "support"
    assert default_position("harmful") == "oppose"
    assert default_position("mixed") == "support"
    assert default_position("unknown") == "support"
    assert default_position(None) == "support"


def test_build_subject_lists_bill_numbers() -> None:
    subject = build_subject([{"number": "H1"}, {"number": "S2"}])
    assert subject == "Constituent message: H1, S2"
    many = build_subject([{"number": f"H{i}"} for i in range(7)])
    assert "+ 3 more" in many


# ---------------------------------------------------------------------------
# Mailto builder
# ---------------------------------------------------------------------------

def test_build_mailto_all_recipients_and_encoding() -> None:
    result = build_mailto(
        ["senator@example.gov", "rep@example.gov", "SENATOR@example.gov"],
        "Constituent message: H1 & S2",
        "Dear Senator:\n\nLine two.",
    )
    assert result.mode == "all"
    assert result.url is not None
    # Case-insensitive dedupe: 2 unique recipients.
    assert result.included_emails == ["senator@example.gov", "rep@example.gov"]
    assert result.url.startswith("mailto:senator@example.gov,rep@example.gov?")
    assert "H1%20%26%20S2" in result.url          # subject percent-encoded
    assert "%0A%0A" in result.url                  # body newlines encoded
    assert " " not in result.url


def test_build_mailto_length_fallback_first_recipient() -> None:
    emails = [f"person{i}@example.gov" for i in range(6)]
    body = "x" * 150
    result = build_mailto(emails, "Subject", body, limit=220)
    assert result.mode == "first"
    assert result.included_emails == emails[:1]
    assert result.total_recipients == 6
    assert result.url is not None and len(result.url) <= 220


def test_build_mailto_extreme_length_drops_body() -> None:
    result = build_mailto(["a@example.gov"], "S" * 40, "b" * 400, limit=80)
    assert result.mode == "first_no_body"
    assert result.url is not None and "body=" not in result.url


def test_build_mailto_no_recipients() -> None:
    result = build_mailto([], "Subject", "Body")
    assert result.mode == "none" and result.url is None


# ---------------------------------------------------------------------------
# Fixture database (built through the real store, like ingestion does)
# ---------------------------------------------------------------------------

def _legislator(pid: str, name: str, chamber: str, district: str,
                email: str = "") -> Legislator:
    return Legislator(
        openstates_person_id=pid, name=name, first_name=name.split()[0],
        last_name=name.split()[-1], party="Democratic", chamber=chamber,
        role_title="Senator" if chamber == "upper" else "Representative",
        district=district, email_published=email, photo_url="",
        jurisdiction_id="ocd-jurisdiction/country:us/state:nc/government",
        state_abbr="NC",
    )


def _make_db(path: Path) -> LocalStore:
    store = LocalStore(path)
    store.upsert_session(Session(
        session_id=9999, state_abbr="NC", year_start=2025, year_end=2026,
        session_name="Synthetic Session", session_tag="Regular Session",
        special=False, sine_die=False, dataset_hash="synthetic-hash",
    ))
    store.upsert_legislator(_legislator(
        SENATOR_ID, "Synthetic Senator", "upper", "14", "senator14@example.gov"))
    store.upsert_legislator(_legislator(
        NOEMAIL_SENATOR_ID, "Synthetic Sixteen", "upper", "16", ""))
    store.upsert_legislator(_legislator(
        REP_ID, "Synthetic Rep", "lower", "38", "rep38@example.gov"))
    store.upsert_legislator(_legislator(
        OTHER_REP_ID, "Synthetic NinetyNine", "lower", "99", "rep99@example.gov"))

    def add_bill(bill_id: int, number: str, title: str, stance: str | None) -> None:
        store.upsert_master_bill(9999, MasterBill(
            bill_id=bill_id, number=number, change_hash=f"h{bill_id}", status=1,
            title=title, description="", status_date="2025-01-15",
            last_action="Filed", last_action_date="",
            url=f"https://example.gov/{number}", raw={},
        ), "Introduced")
        store.replace_classifications(bill_id, [Classification(
            category_id="voting_access",
            category_name="Voting access & election administration",
            confidence=0.9, matched_terms=("voting",), stance=stance,
        )])

    add_bill(101, "H101", "Synthetic Voting Access Expansion Act", "supportive")
    add_bill(102, "S102", "Synthetic Voter Roll Purge Act", "harmful")
    add_bill(103, "H103", "Synthetic Untagged Study Act", None)
    return store


def _client(db_path: Path, payload: dict[str, Any] | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(create_actbox_router(
        str(db_path),
        census_transport=_fake_transport(payload or _census_payload()),
    ))
    return TestClient(app)


LOOKUP_FORM = {
    "full_name": "Test Voter",
    "street": "16 W Jones St",
    "city": "Raleigh",
    "state": "NC",
    "zip_code": "27601",
}


def _db_snapshot(path: Path) -> tuple[str, dict[str, int]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables
        }
    finally:
        conn.close()
    return digest, counts


# ---------------------------------------------------------------------------
# HTTP flow
# ---------------------------------------------------------------------------

def test_act_form_has_privacy_note_and_states(tmp_path: Path) -> None:
    store = _make_db(tmp_path / "t.db")
    store.close()
    client = _client(tmp_path / "t.db")
    page = client.get("/act")
    assert page.status_code == 200
    assert "never stored, logged, or shared" in page.text
    assert "North Carolina" in page.text          # live-state note from DB
    assert '<option value="TX">Texas</option>' in page.text


def test_lookup_matches_reps_and_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    store.close()
    before = _db_snapshot(db)

    client = _client(db)
    page = client.post("/act/lookup", data=LOOKUP_FORM)
    assert page.status_code == 200
    text = page.text
    # District chips from the Census fixture.
    assert "Congressional District 2" in text
    assert "State Senate district: 14" in text
    assert "State House district: 38" in text
    # District-matched reps, pre-checked.
    assert "Synthetic Senator" in text and "district-matched" in text
    assert "Synthetic Rep" in text
    assert f'value="{SENATOR_ID}" checked' in text
    assert f'value="{REP_ID}" checked' in text
    # The other chamber members are listed but NOT pre-checked.
    assert f'value="{OTHER_REP_ID}"' in text
    assert f'value="{OTHER_REP_ID}" checked' not in text
    # Federal delegation: official finders, no invented names.
    assert "house.gov/representatives/find-your-representative" in text
    assert "senate.gov/senators/senators-contact.htm" in text
    # Staff layer: honest empty line, exactly as specified.
    assert ("Staff contacts (LC/LD) are added as we verify them "
            "— none on file for your reps yet.") in text
    # Bill picker shows all three classified bills with stance pills.
    assert "Synthetic Voting Access Expansion Act" in text
    assert "tracker stance: supportive" in text
    assert "tracker stance: harmful" in text

    # The address (and the whole lookup) wrote NOTHING to the database:
    # same bytes, same per-table row counts.
    after = _db_snapshot(db)
    assert after == before


def test_lookup_unmatched_address_is_honest(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    store.close()
    client = _client(db, {"result": {"addressMatches": []}})
    page = client.post("/act/lookup", data=LOOKUP_FORM)
    assert page.status_code == 200
    assert "could not" in page.text and "match" in page.text
    assert "Synthetic Senator" not in page.text


def test_lookup_untracked_state_is_honest(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    store.close()
    payload = _census_payload()
    match = payload["result"]["addressMatches"][0]
    match["addressComponents"]["state"] = "TX"
    match["geographies"]["States"][0]["STUSAB"] = "TX"
    client = _client(db, payload)
    page = client.post("/act/lookup", data=LOOKUP_FORM)
    assert page.status_code == 200
    assert "not tracked yet" in page.text
    assert "Synthetic Senator" not in page.text


def test_compose_builds_one_mailto_to_all_selected(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    store.close()
    client = _client(db)
    page = client.post("/act/compose", data={
        "full_name": "Test Voter", "city": "Raleigh", "state": "NC",
        "zip_code": "27601",
        "rep": [SENATOR_ID, REP_ID],
        "bill": ["101", "102"],
        "pos_101": "support", "pos_102": "oppose",
    })
    assert page.status_code == 200
    text = page.text
    assert "Open in my email — addressed to all of them" in text
    assert "mailto:senator14@example.gov,rep38@example.gov" in text
    # The copy box carries the constituent framing and per-bill positions.
    assert "I am your constituent in Raleigh, NC 27601" in text
    assert "H101" in text and "I support this bill" in text
    assert "S102" in text and "I oppose this bill" in text
    assert "Constituent message: H101, S102" in text


def test_compose_defaults_position_from_stance(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    store.close()
    client = _client(db)
    # No pos_* fields at all: stance drives the default (harmful -> oppose).
    page = client.post("/act/compose", data={
        "full_name": "Test Voter", "city": "Raleigh", "state": "NC",
        "zip_code": "27601", "rep": [REP_ID], "bill": ["102"],
    })
    assert page.status_code == 200
    assert "I oppose this bill" in page.text


def test_compose_no_email_recipient_goes_to_form_assist(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    store.close()
    client = _client(db)
    page = client.post("/act/compose", data={
        "full_name": "Test Voter", "city": "Raleigh", "state": "NC",
        "zip_code": "27601",
        "rep": [NOEMAIL_SENATOR_ID], "bill": ["101"], "pos_101": "support",
    })
    assert page.status_code == 200
    text = page.text
    assert "No published email — form follow-up" in text
    assert "Synthetic Sixteen" in text
    assert "ncleg.gov" in text                     # official NC legislature route
    assert "Open in my email" not in text          # no emailable recipient selected


# ---------------------------------------------------------------------------
# Staff layer (store round-trip + UI)
# ---------------------------------------------------------------------------

def test_staff_contacts_sqlite_round_trip(tmp_path: Path) -> None:
    store = _make_db(tmp_path / "t.db")
    ld_id = store.add_staff_contact(
        SENATOR_ID, role="LD", name="Synthetic Director",
        email="ld.senate14@example.gov",
        source_url="https://example.gov/staff-directory",
        verified_at="2026-11-13",
    )
    lc_id = store.add_staff_contact(
        SENATOR_ID, role="LC", name="Synthetic Correspondent", email=None,
    )
    rows = store.staff_contacts_for([SENATOR_ID])
    # LD sorts before LC (the memo writer is the primary target).
    assert [r["id"] for r in rows] == [ld_id, lc_id]
    assert rows[0]["role"] == "LD"
    assert rows[0]["legislator_openstates_id"] == SENATOR_ID
    assert rows[1]["email"] is None
    assert store.staff_contacts_for([]) == []
    with pytest.raises(ValueError, match="unknown staff role"):
        store.add_staff_contact(SENATOR_ID, role="Intern", name="X")
    store.close()


def test_staff_appears_in_lookup_and_compose(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = _make_db(db)
    staff_id = store.add_staff_contact(
        SENATOR_ID, role="LD", name="Synthetic Director",
        email="ld.senate14@example.gov",
        source_url="https://example.gov/staff-directory",
        verified_at="2026-11-13",
    )
    store.add_staff_contact(
        REP_ID, role="LC", name="Synthetic NoEmail", email=None,
    )
    store.close()

    client = _client(db)
    lookup = client.post("/act/lookup", data=LOOKUP_FORM)
    assert lookup.status_code == 200
    assert "Synthetic Director" in lookup.text
    assert "LD" in lookup.text and "verified 2026-11-13" in lookup.text
    assert "none on file for your reps yet" not in lookup.text

    page = client.post("/act/compose", data={
        "full_name": "Test Voter", "city": "Raleigh", "state": "NC",
        "zip_code": "27601",
        "rep": [SENATOR_ID, REP_ID], "staff": [str(staff_id)],
        "bill": ["101"], "pos_101": "support",
    })
    assert page.status_code == 200
    # The ticked staffer with a published email is on the mailto.
    assert "ld.senate14@example.gov" in page.text
    # The rep's LC without an email is in form-assist, not dropped.
    assert "Synthetic NoEmail" in page.text
    assert "No published email — form follow-up" in page.text


# ---------------------------------------------------------------------------
# Staff layer — Postgres SQL shape (fake connection, like test_postgres_store)
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, one: Any = None, all_rows: Any = None) -> None:
        self._one = one
        self._all = all_rows or []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> Any:
        return list(self._all)


class _FakePGConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple | None]] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple | None = None) -> _FakeCursor:
        self.calls.append((sql, params))
        if "SELECT id FROM legislators" in sql:
            return _FakeCursor(one={"id": 7})
        if "INSERT INTO staff_contacts" in sql:
            return _FakeCursor(one={"id": 42})
        if "FROM staff_contacts" in sql:
            return _FakeCursor(all_rows=[{
                "id": 42, "legislator_openstates_id": SENATOR_ID,
                "role": "LD", "name": "Synthetic Director",
                "email": "ld@example.gov", "source_url": None,
                "verified_at": "2026-11-13",
            }])
        return _FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        pass


def test_staff_contacts_postgres_sql_shape() -> None:
    pytest.importorskip("psycopg")
    conn = _FakePGConnection()
    store = PostgresStore(connection=conn, apply_schema=False)
    new_id = store.add_staff_contact(
        SENATOR_ID, role="LD", name="Synthetic Director",
        email="ld@example.gov", verified_at="2026-11-13",
    )
    assert new_id == 42
    insert_sql = next(s for s, _ in conn.calls if "INSERT INTO staff_contacts" in s)
    assert "legislator_id" in insert_sql and "%s" in insert_sql
    rows = store.staff_contacts_for([SENATOR_ID])
    assert rows and rows[0]["legislator_openstates_id"] == SENATOR_ID
    join_sql = next(s for s, _ in conn.calls if "FROM staff_contacts" in s and "JOIN" in s)
    assert "openstates_person_id" in join_sql
    with pytest.raises(ValueError, match="unknown staff role"):
        store.add_staff_contact(SENATOR_ID, role="Intern", name="X")
