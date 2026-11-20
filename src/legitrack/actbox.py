"""Act Box v1 (SPEC section 7) — "email your own reps" from the tracker.

Visitor flow (no accounts, SPEC section 9 default):

1. ``GET /act`` — name + home address form.
2. ``POST /act/lookup`` — the address is geocoded ONCE by the US Census
   Geocoder (free, official, no key) to resolve the visitor's state,
   congressional district, and state senate / house districts. Their
   state legislators are then read from OUR OWN legislators store
   (OpenStates ingestion): district-matched where the stored district
   supports it, otherwise the chamber is listed in full and the page
   says district matching is partial. Nothing is ever claimed that the
   data does not support.
3. ``POST /act/compose`` — the visitor ticks bills (classified bills for
   their state, stance shown where the stance pass has tagged them) and
   recipients, and the box builds ONE message. Sending is the visitor's
   own email app via a ``mailto:`` link (SPEC 7.3 lane 1, published
   emails only). Recipients without a published email are listed with an
   official contact route (form assist, lane 3) — never silently
   dropped. The pattern-email lane (lane 2) is deliberately NOT in v1.

Privacy (stated on the page, enforced here):
* The address is used only for the geocoder lookup. It is never written
  to the database, never logged, and never leaves this request/response
  cycle except as the geocoder query itself. The lookup path opens the
  SQLite store READ-ONLY and issues only SELECTs against Postgres.
* Staff layer: ``staff_contacts`` rows exist only where a staffer was
  verified against an official source. Where none exist for the
  visitor's reps, the page says so in one honest line instead of
  inventing an LC/LD.

Federal delegation is NOT in our database yet: the lookup page shows
official finder links (house.gov / senate.gov) rather than names we do
not hold.

WIRING TODO (deliberately not done here — web.py is owned by the main
app): in ``legitrack.web.create_app`` add
``app.include_router(actbox.create_actbox_router(db_path))`` (passing the
same db_path/DATABASE_URL resolution the app already computed) and an
``<a href="/act">Act</a>`` nav link.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

try:  # Design tokens are web.py's documented re-skin point; reuse them.
    from .web import BASE_CSS, TOKENS_CSS
except Exception:  # pragma: no cover - fallback if web.py is mid-refactor
    TOKENS_CSS = """
:root {
  --color-bg: #0a0c0e; --color-surface: #14181d; --color-surface-raised: #1c222a;
  --color-border: #2c3e50; --color-text: #f4f3ef; --color-text-muted: #9fa6b2;
  --color-accent: #d9a441; --color-accent-strong: #f0bc55; --color-red: #c1272d; --color-green: #1a7a3a;
  --color-positive: #3fb97f; --color-negative: #e05d5d; --color-info: #5aa2e0;
  --font-display: "Georgia", "Times New Roman", serif;
  --font-body: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --space-xs: 0.25rem; --space-sm: 0.5rem; --space-md: 1rem; --space-lg: 1.5rem;
  --space-xl: 2.5rem; --radius-sm: 6px; --radius-md: 10px; --radius-lg: 16px;
  --shadow-card: 0 1px 3px rgba(0, 0, 0, 0.4); --max-width: 1120px;
}
"""
    BASE_CSS = ""

PRODUCT_NAME = "Black Legislation Tracker"

# ---------------------------------------------------------------------------
# Geography constants
# ---------------------------------------------------------------------------

STATES: tuple[tuple[str, str], ...] = (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"),
    ("DE", "Delaware"), ("DC", "District of Columbia"), ("FL", "Florida"),
    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"),
    ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"),
    ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"),
    ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
)
STATE_NAMES = dict(STATES)

#: Official federal finders — linked, never scraped or replaced by names
#: we do not hold (the federal delegation is not in our database yet).
HOUSE_FINDER_URL = "https://www.house.gov/representatives/find-your-representative"
SENATE_FINDER_URL = "https://www.senate.gov/senators/senators-contact.htm"
#: Official USA.gov finder, the form-assist fallback for a state
#: legislator with no published email whose legislature site we have not
#: yet added to the (SPEC 7.2) registry.
USAGOV_OFFICIALS_URL = "https://www.usa.gov/elected-officials"
#: Official state legislature sites verified for the registry so far.
STATE_LEGISLATURE_URLS: dict[str, str] = {
    "NC": "https://www.ncleg.gov",
}

#: mailto: URLs get unreliable past roughly this length in common mail
#: clients; past it the composer degrades honestly (see build_mailto).
MAILTO_LIMIT = 1800

PRIVACY_NOTE = (
    "Your address is used once, only to find your districts, and is "
    "never stored, logged, or shared. Nothing is sent from this site — "
    "the Act Box opens your own email app, so the message comes from you."
)


def state_name(abbr: str) -> str:
    return STATE_NAMES.get(abbr.upper(), abbr.upper())


def form_assist_url(state_abbr: str) -> str:
    """Official contact route for a recipient with no published email."""
    return STATE_LEGISLATURE_URLS.get(state_abbr.upper(), USAGOV_OFFICIALS_URL)


# ---------------------------------------------------------------------------
# Census Geocoder (free, official, no key)
# ---------------------------------------------------------------------------

CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 LegiTrack/0.1"
)

#: Transport hook for tests: (url) -> decoded JSON payload.
CensusTransport = Callable[[str], dict[str, Any]]


class CensusError(RuntimeError):
    """The Census Geocoder call failed. Messages never contain the address."""


@dataclass(frozen=True)
class Districts:
    """What the geocoder resolved for one address."""

    state_abbr: str
    city: str                       # as the geocoder reports it (upper-case)
    zip_code: str
    matched_address: str
    congressional_district: str | None = None   # plain number, e.g. '2'
    congressional_label: str | None = None      # e.g. 'Congressional District 2'
    state_senate_district: str | None = None
    state_house_district: str | None = None


def census_url(address_line: str) -> str:
    params = {
        "address": address_line,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "all",
        "format": "json",
    }
    return CENSUS_GEOCODER_URL + "?" + urllib.parse.urlencode(params)


def _urllib_transport(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CensusError(f"Census Geocoder returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CensusError("Census Geocoder request failed") from exc


def _layer_entry(geographies: dict[str, Any], fragment: str) -> dict[str, Any] | None:
    """First entry of the geography layer whose key contains ``fragment``.

    Layer keys carry vintage prefixes that change over time ('120th
    Congressional Districts', '2026 State Legislative Districts - Upper'),
    so matching is by substring, never by exact key.
    """
    for key, entries in geographies.items():
        if fragment in key and entries:
            return dict(entries[0])
    return None


def _district_of(entry: dict[str, Any] | None) -> str | None:
    """Plain district identifier from a geography entry.

    BASENAME is the un-padded district number ('2', '14', '38') — the
    same shape OpenStates stores. 'ZZ' is the Census code for an area
    with no such district (e.g. at-large delegate seats); treat as none.
    """
    if not entry:
        return None
    base = str(entry.get("BASENAME", "") or "").strip()
    if not base or base.upper() == "ZZ":
        return None
    return base


def parse_geocoder_payload(payload: dict[str, Any]) -> Districts | None:
    """Parse a Census Geocoder geographies payload.

    Returns None when the address did not match — the caller must say so
    honestly rather than guess districts.
    """
    matches = (payload.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    first = matches[0]
    components = first.get("addressComponents") or {}
    geographies = first.get("geographies") or {}

    states_entry = _layer_entry(geographies, "States") or {}
    state = str(components.get("state") or states_entry.get("STUSAB") or "").upper()
    if not state:
        return None

    cd_entry = _layer_entry(geographies, "Congressional District")
    senate_entry = _layer_entry(geographies, "Legislative Districts - Upper")
    house_entry = _layer_entry(geographies, "Legislative Districts - Lower")
    return Districts(
        state_abbr=state,
        city=str(components.get("city") or ""),
        zip_code=str(components.get("zip") or ""),
        matched_address=str(first.get("matchedAddress") or ""),
        congressional_district=_district_of(cd_entry),
        congressional_label=(str(cd_entry.get("NAME")) if cd_entry else None),
        state_senate_district=_district_of(senate_entry),
        state_house_district=_district_of(house_entry),
    )


def geocode_address(
    address_line: str,
    *,
    transport: CensusTransport | None = None,
    timeout: float = 20.0,
) -> Districts | None:
    """Geocode one free-form address line. None = no match (not an error)."""
    fetch = transport or _urllib_transport
    payload = fetch(census_url(address_line))
    if not isinstance(payload, dict):
        raise CensusError("Census Geocoder returned an unreadable response")
    return parse_geocoder_payload(payload)


# ---------------------------------------------------------------------------
# Representative matching (pure — unit-testable without a database)
# ---------------------------------------------------------------------------

def _norm_district(value: Any) -> str:
    """Normalise a district label for comparison: '014' -> '14'."""
    text = str(value or "").strip()
    if text.isdigit():
        return str(int(text))
    return text.upper()


@dataclass
class ChamberMatch:
    chamber: str                       # 'upper' | 'lower' | 'legislature'
    label: str                         # 'State Senate' | 'State House'
    district: str | None               # the visitor's district, if resolved
    matched: list[dict[str, Any]] = field(default_factory=list)
    everyone: list[dict[str, Any]] = field(default_factory=list)


_CHAMBER_ORDER = (("upper", "State Senate"), ("lower", "State House"),
                  ("legislature", "State Legislature"))


def match_representatives(
    people: list[dict[str, Any]], districts: Districts
) -> list[ChamberMatch]:
    """Match stored legislators to the visitor's districts, per chamber.

    A person is 'matched' only when their stored district equals the
    geocoded district for their chamber. Chambers with no district match
    keep everyone listed so the page can say matching is partial instead
    of silently dropping the visitor's actual rep.
    """
    wanted = {
        "upper": districts.state_senate_district,
        "lower": districts.state_house_district,
        # Unicameral legislatures (e.g. Nebraska) sit in the upper layer.
        "legislature": districts.state_senate_district,
    }
    out: list[ChamberMatch] = []
    for chamber, label in _CHAMBER_ORDER:
        members = [p for p in people if str(p.get("chamber") or "") == chamber]
        if not members:
            continue
        district = wanted.get(chamber)
        matched = [
            p for p in members
            if district and _norm_district(p.get("district")) == _norm_district(district)
        ]
        out.append(ChamberMatch(
            chamber=chamber, label=label, district=district,
            matched=matched, everyone=members,
        ))
    return out


# ---------------------------------------------------------------------------
# Message + mailto builders (pure)
# ---------------------------------------------------------------------------

def default_position(stance: str | None) -> str:
    """Default constituent position from a bill's stance tag.

    supportive -> 'support', harmful -> 'oppose'. Anything else (mixed,
    unknown, stance pass not run) defaults to 'support' — and the choice
    is shown in an editable select next to every bill, never hidden.
    """
    return "oppose" if str(stance or "").lower() == "harmful" else "support"


def build_subject(bills: list[dict[str, Any]]) -> str:
    numbers = [str(b.get("number") or "") for b in bills if b.get("number")]
    shown = ", ".join(numbers[:4])
    if len(numbers) > 4:
        shown += f" + {len(numbers) - 4} more"
    return f"Constituent message: {shown}" if shown else "Constituent message"


def _greeting(recipients: list[dict[str, Any]], state_abbr: str) -> str:
    reps = [r for r in recipients if r.get("kind") == "rep"]
    has_staff = any(r.get("kind") == "staff" for r in recipients)
    names: list[str] = []
    for r in reps[:3]:
        title = r.get("role_title") or (
            "Senator" if r.get("chamber") == "upper" else "Representative"
        )
        last = str(r.get("name") or "").split()[-1] if r.get("name") else ""
        names.append(f"{title} {last}".strip())
    if names and len(reps) <= 3:
        text = "Dear " + ", ".join(names)
        if has_staff:
            text += ", and staff"
        return text + ":"
    if reps:
        return f"Dear members of the {state_name(state_abbr)} Legislature:"
    return "Dear legislative staff:"


def build_body(
    *,
    visitor_name: str,
    city: str,
    state_abbr: str,
    zip_code: str,
    bills: list[dict[str, Any]],
    recipients: list[dict[str, Any]],
) -> str:
    lines = [
        _greeting(recipients, state_abbr),
        "",
        f"My name is {visitor_name}, and I am your constituent in "
        f"{city}, {state_abbr} {zip_code}.",
        "",
        "I am writing about bills the Black Legislation Tracker is "
        f"following in the {state_name(state_abbr)} legislature:",
        "",
    ]
    for b in bills:
        lines.append(
            f"- {b.get('number')} - {b.get('title') or ''} "
            f"I {b.get('position', 'support')} this bill."
        )
    lines += [
        "",
        "Thank you for your work, and for hearing from the people you represent.",
        "",
        "Sincerely,",
        visitor_name,
        f"{city}, {state_abbr} {zip_code}",
    ]
    return "\n".join(lines)


@dataclass
class MailtoResult:
    url: str | None
    #: 'all' | 'first' | 'first_no_body' | 'none'
    mode: str
    included_emails: list[str]
    total_recipients: int


def build_mailto(
    emails: list[str],
    subject: str,
    body: str,
    *,
    limit: int = MAILTO_LIMIT,
) -> MailtoResult:
    """Build one mailto: URL addressed to every emailable recipient.

    Long URLs break mail clients, so past ``limit`` the composer
    degrades in honest steps: all recipients -> first recipient only ->
    first recipient without the body (the page then relies on its
    copy-text box and says exactly why).
    """
    seen: set[str] = set()
    unique: list[str] = []
    for e in emails:
        addr = str(e or "").strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            unique.append(addr)
    if not unique:
        return MailtoResult(None, "none", [], 0)

    def make(addrs: list[str], with_body: bool) -> str:
        to = ",".join(urllib.parse.quote(a, safe="@.") for a in addrs)
        params = {"subject": subject}
        if with_body:
            params["body"] = body
        return "mailto:" + to + "?" + urllib.parse.urlencode(
            params, quote_via=urllib.parse.quote
        )

    full = make(unique, True)
    if len(full) <= limit:
        return MailtoResult(full, "all", unique, len(unique))
    first = make(unique[:1], True)
    if len(first) <= limit:
        return MailtoResult(first, "first", unique[:1], len(unique))
    bare = make(unique[:1], False)
    if len(bare) <= limit:
        return MailtoResult(bare, "first_no_body", unique[:1], len(unique))
    return MailtoResult(None, "none", [], len(unique))


# ---------------------------------------------------------------------------
# Data access — read-only, mirroring web.py's two backends.
# SELECT-only. The visitor's address never touches these functions.
# ---------------------------------------------------------------------------

def _ro_connect(db_path: str) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _people_sqlite(db_path: str, state: str) -> list[dict[str, Any]]:
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        if not _sqlite_table_exists(conn, "legislators"):
            return []
        rows = conn.execute(
            "SELECT openstates_person_id AS person_id, name, party, chamber, "
            "role_title, district, email_published, photo_url "
            "FROM legislators WHERE state_abbr = ? "
            "ORDER BY chamber, CAST(district AS INTEGER), name",
            (state.upper(),),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _staff_sqlite(
    db_path: str, person_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    if not person_ids:
        return {}
    conn = _ro_connect(db_path)
    if conn is None:
        return {}
    try:
        if not _sqlite_table_exists(conn, "staff_contacts"):
            return {}
        placeholders = ", ".join("?" for _ in person_ids)
        rows = conn.execute(
            f"SELECT id, legislator_openstates_id AS person_id, role, name, "
            f"email, source_url, verified_at FROM staff_contacts "
            f"WHERE legislator_openstates_id IN ({placeholders}) "
            f"ORDER BY CASE role WHEN 'LD' THEN 0 WHEN 'LC' THEN 1 "
            f"WHEN 'Chief of Staff' THEN 2 ELSE 3 END, name",
            tuple(person_ids),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r["person_id"]), []).append(dict(r))
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _bills_sqlite(db_path: str, state: str, limit: int = 200) -> list[dict[str, Any]]:
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        if not _sqlite_table_exists(conn, "bills"):
            return []
        stance_expr = "NULL"
        if "stance" in _sqlite_columns(conn, "bill_classifications"):
            stance_expr = (
                "(SELECT c2.stance FROM bill_classifications c2 "
                "WHERE c2.legiscan_bill_id = b.legiscan_bill_id "
                "AND c2.stance IS NOT NULL ORDER BY c2.category_id LIMIT 1)"
            )
        rows = conn.execute(
            f"SELECT b.legiscan_bill_id AS bill_id, b.number, b.title, "
            f"b.public_status, b.url, "
            f"GROUP_CONCAT(DISTINCT c.category_name) AS categories, "
            f"{stance_expr} AS stance "
            f"FROM bills b "
            f"JOIN sessions s ON s.legiscan_session_id = b.legiscan_session_id "
            f"JOIN bill_classifications c "
            f"ON c.legiscan_bill_id = b.legiscan_bill_id "
            f"WHERE s.state_abbr = ? "
            f"GROUP BY b.legiscan_bill_id ORDER BY b.number LIMIT ?",
            (state.upper(), limit),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _tracked_states_sqlite(db_path: str) -> list[str]:
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        if not _sqlite_table_exists(conn, "legislators"):
            return []
        return [
            str(r[0]) for r in conn.execute(
                "SELECT DISTINCT state_abbr FROM legislators "
                "WHERE state_abbr IS NOT NULL ORDER BY state_abbr"
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _people_by_ids_sqlite(
    db_path: str, state: str, person_ids: list[str]
) -> list[dict[str, Any]]:
    if not person_ids:
        return []
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        placeholders = ", ".join("?" for _ in person_ids)
        rows = conn.execute(
            f"SELECT openstates_person_id AS person_id, name, party, chamber, "
            f"role_title, district, email_published, photo_url "
            f"FROM legislators WHERE state_abbr = ? "
            f"AND openstates_person_id IN ({placeholders}) "
            f"ORDER BY CASE chamber WHEN 'upper' THEN 0 WHEN 'lower' THEN 1 "
            f"ELSE 2 END, CAST(district AS INTEGER), name",
            (state.upper(), *person_ids),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _staff_by_ids_sqlite(
    db_path: str, state: str, staff_ids: list[int]
) -> list[dict[str, Any]]:
    if not staff_ids:
        return []
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        if not _sqlite_table_exists(conn, "staff_contacts"):
            return []
        placeholders = ", ".join("?" for _ in staff_ids)
        rows = conn.execute(
            f"SELECT sc.id, sc.legislator_openstates_id AS person_id, sc.role, "
            f"sc.name, sc.email, sc.source_url, sc.verified_at, "
            f"l.name AS legislator_name FROM staff_contacts sc "
            f"JOIN legislators l "
            f"ON l.openstates_person_id = sc.legislator_openstates_id "
            f"WHERE l.state_abbr = ? AND sc.id IN ({placeholders})",
            (state.upper(), *staff_ids),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _bills_by_ids_sqlite(
    db_path: str, state: str, bill_ids: list[int]
) -> list[dict[str, Any]]:
    if not bill_ids:
        return []
    conn = _ro_connect(db_path)
    if conn is None:
        return []
    try:
        stance_expr = "NULL"
        if "stance" in _sqlite_columns(conn, "bill_classifications"):
            stance_expr = (
                "(SELECT c2.stance FROM bill_classifications c2 "
                "WHERE c2.legiscan_bill_id = b.legiscan_bill_id "
                "AND c2.stance IS NOT NULL ORDER BY c2.category_id LIMIT 1)"
            )
        placeholders = ", ".join("?" for _ in bill_ids)
        rows = conn.execute(
            f"SELECT b.legiscan_bill_id AS bill_id, b.number, b.title, "
            f"b.public_status, b.url, {stance_expr} AS stance "
            f"FROM bills b "
            f"JOIN sessions s ON s.legiscan_session_id = b.legiscan_session_id "
            f"WHERE s.state_abbr = ? AND b.legiscan_bill_id IN ({placeholders}) "
            f"ORDER BY b.number",
            (state.upper(), *bill_ids),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# -- Postgres mirrors (same return shapes) -----------------------------------

def _pg_connect(dsn: str):  # type: ignore[no-untyped-def]
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def _pg_has_column(conn, table: str, column: str) -> bool:  # type: ignore[no-untyped-def]
    row = conn.execute(
        "SELECT 1 AS x FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return row is not None


def _people_pg(dsn: str, state: str) -> list[dict[str, Any]]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT openstates_person_id AS person_id, name, party, chamber, "
            "role_title, district, email_published, photo_url "
            "FROM legislators WHERE state_abbr = %s "
            "ORDER BY chamber, "
            "CASE WHEN district ~ '^[0-9]+$' THEN CAST(district AS INTEGER) END "
            "NULLS LAST, name",
            (state.upper(),),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _staff_pg(dsn: str, person_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not person_ids:
        return {}
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return {}
    try:
        placeholders = ", ".join("%s" for _ in person_ids)
        rows = conn.execute(
            f"SELECT sc.id, l.openstates_person_id AS person_id, sc.role, "
            f"sc.name, sc.email, sc.source_url, sc.verified_at::text AS verified_at "
            f"FROM staff_contacts sc JOIN legislators l ON l.id = sc.legislator_id "
            f"WHERE l.openstates_person_id IN ({placeholders}) "
            f"ORDER BY CASE sc.role WHEN 'LD' THEN 0 WHEN 'LC' THEN 1 "
            f"WHEN 'Chief of Staff' THEN 2 ELSE 3 END, sc.name",
            tuple(person_ids),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r["person_id"]), []).append(dict(r))
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def _bills_pg(dsn: str, state: str, limit: int = 200) -> list[dict[str, Any]]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        stance_expr = "NULL"
        if _pg_has_column(conn, "bill_classifications", "stance"):
            stance_expr = (
                "(SELECT c2.stance FROM bill_classifications c2 "
                "WHERE c2.bill_id = b.id AND c2.stance IS NOT NULL "
                "ORDER BY c2.category LIMIT 1)"
            )
        rows = conn.execute(
            f"SELECT b.legiscan_bill_id AS bill_id, b.bill_number AS number, "
            f"b.title, b.public_status, b.legiscan_url AS url, "
            f"STRING_AGG(DISTINCT COALESCE(c.category_name, c.category), ',') "
            f"AS categories, {stance_expr} AS stance "
            f"FROM bills b "
            f"JOIN sessions s ON s.id = b.session_id "
            f"JOIN jurisdictions j ON j.id = s.jurisdiction_id "
            f"JOIN bill_classifications c ON c.bill_id = b.id "
            f"WHERE j.state_abbr = %s "
            f"GROUP BY b.id ORDER BY b.bill_number LIMIT %s",
            (state.upper(), limit),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _tracked_states_pg(dsn: str) -> list[str]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        return [
            str(r["state_abbr"]) for r in conn.execute(
                "SELECT DISTINCT state_abbr FROM legislators "
                "WHERE state_abbr IS NOT NULL ORDER BY state_abbr"
            )
        ]
    except Exception:
        return []
    finally:
        conn.close()


def _people_by_ids_pg(
    dsn: str, state: str, person_ids: list[str]
) -> list[dict[str, Any]]:
    if not person_ids:
        return []
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        placeholders = ", ".join("%s" for _ in person_ids)
        rows = conn.execute(
            f"SELECT openstates_person_id AS person_id, name, party, chamber, "
            f"role_title, district, email_published, photo_url "
            f"FROM legislators WHERE state_abbr = %s "
            f"AND openstates_person_id IN ({placeholders}) "
            f"ORDER BY CASE chamber WHEN 'upper' THEN 0 WHEN 'lower' THEN 1 "
            f"ELSE 2 END, name",
            (state.upper(), *person_ids),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _staff_by_ids_pg(
    dsn: str, state: str, staff_ids: list[int]
) -> list[dict[str, Any]]:
    if not staff_ids:
        return []
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        placeholders = ", ".join("%s" for _ in staff_ids)
        rows = conn.execute(
            f"SELECT sc.id, l.openstates_person_id AS person_id, sc.role, "
            f"sc.name, sc.email, sc.source_url, sc.verified_at::text AS verified_at, "
            f"l.name AS legislator_name FROM staff_contacts sc "
            f"JOIN legislators l ON l.id = sc.legislator_id "
            f"WHERE l.state_abbr = %s AND sc.id IN ({placeholders})",
            (state.upper(), *staff_ids),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _bills_by_ids_pg(
    dsn: str, state: str, bill_ids: list[int]
) -> list[dict[str, Any]]:
    if not bill_ids:
        return []
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        stance_expr = "NULL"
        if _pg_has_column(conn, "bill_classifications", "stance"):
            stance_expr = (
                "(SELECT c2.stance FROM bill_classifications c2 "
                "WHERE c2.bill_id = b.id AND c2.stance IS NOT NULL "
                "ORDER BY c2.category LIMIT 1)"
            )
        placeholders = ", ".join("%s" for _ in bill_ids)
        rows = conn.execute(
            f"SELECT b.legiscan_bill_id AS bill_id, b.bill_number AS number, "
            f"b.title, b.public_status, b.legiscan_url AS url, "
            f"{stance_expr} AS stance "
            f"FROM bills b "
            f"JOIN sessions s ON s.id = b.session_id "
            f"JOIN jurisdictions j ON j.id = s.jurisdiction_id "
            f"WHERE j.state_abbr = %s AND b.legiscan_bill_id IN ({placeholders}) "
            f"ORDER BY b.bill_number",
            (state.upper(), *bill_ids),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

ACT_CSS = """
.act-form { display: grid; gap: var(--space-md); max-width: 640px; border: 2px solid var(--color-accent); border-top: 10px solid var(--color-red, #c1272d); border-radius: var(--radius-md); padding: var(--space-lg); background: var(--color-surface); }
.act-form label { display: flex; flex-direction: column; gap: var(--space-xs);
  font-size: 0.9rem; color: var(--color-text-muted); }
.act-form input, .act-form select {
  background: var(--color-surface-raised); color: var(--color-text);
  border: 1px solid var(--color-border); border-radius: var(--radius-sm);
  padding: var(--space-sm) var(--space-md); font: inherit;
}
.act-cta {
  display: inline-block; background: var(--color-red, #c1272d); color: #fff;
  font-weight: 700; border: none; border-radius: var(--radius-sm);
  padding: var(--space-md) var(--space-lg); font: inherit; cursor: pointer;
  font-size: 1.05rem;
}
.act-cta:hover { text-decoration: none; background: #a31f24; }
.rep-row, .bill-row {
  display: flex; gap: var(--space-md); align-items: flex-start;
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-md); padding: var(--space-md) var(--space-lg);
  margin-bottom: var(--space-sm);
}
.rep-row input, .bill-row input { margin-top: 0.3rem; width: 1.1rem; height: 1.1rem; }
.rep-row .who, .bill-row .what { flex: 1; }
.rep-row .who strong, .bill-row .what strong { color: var(--color-text); }
.rep-row .meta, .bill-row .meta { color: var(--color-text-muted); font-size: 0.88rem; }
details.more { margin-bottom: var(--space-md); }
details.more summary { cursor: pointer; color: var(--color-accent-strong); margin-bottom: var(--space-sm); }
.chips { display: flex; gap: var(--space-sm); flex-wrap: wrap; margin: var(--space-md) 0; }
.chip {
  background: var(--color-surface-raised); border: 1px solid var(--color-border);
  border-radius: 999px; padding: var(--space-xs) var(--space-md); font-size: 0.9rem;
}
.stance-supportive { color: var(--color-positive); }
.stance-harmful { color: var(--color-negative); }
textarea.copybox {
  width: 100%; min-height: 260px; background: var(--color-surface);
  color: var(--color-text); border: 1px solid var(--color-border);
  border-radius: var(--radius-md); padding: var(--space-md);
  font-family: var(--font-mono); font-size: 0.88rem;
}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _act_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — {_esc(PRODUCT_NAME)}</title>
<style>{TOKENS_CSS}{BASE_CSS}{ACT_CSS}</style>
</head>
<body>
<div class="panstripe" aria-hidden="true"></div>
<header class="site"><div class="wrap">
  <a class="brand" href="/">{_esc(PRODUCT_NAME)}</a>
  <nav class="main">
    <a href="/">Overview</a><a href="/bills">Bills</a><a href="/legislators">Legislators</a><a href="/act">Act</a>
  </nav>
</div></header>
<main class="wrap">{body}</main>
<footer class="site"><div class="wrap">
  Data: LegiScan + OpenStates, pulled by the tracker's own daily pipeline.
  District resolution: US Census Geocoder. The Act Box never sends email
  for you — it opens your own email app.
</div></footer>
</body>
</html>"""


def _person_meta(person: dict[str, Any]) -> str:
    bits = [
        str(person.get("role_title") or ""),
        str(person.get("party") or ""),
        f"District {person['district']}" if person.get("district") else "",
    ]
    return " · ".join(b for b in bits if b)


def _rep_checkbox(person: dict[str, Any], *, checked: bool, badge: str = "") -> str:
    email_note = (
        f"Published email: {_esc(person['email_published'])}"
        if person.get("email_published")
        else "No published email on file — form follow-up"
    )
    chk = " checked" if checked else ""
    badge_html = f' <span class="pill">{_esc(badge)}</span>' if badge else ""
    return (
        f'<label class="rep-row"><input type="checkbox" name="rep" '
        f'value="{_esc(person["person_id"])}"{chk}>'
        f'<span class="who"><strong>{_esc(person["name"])}</strong>{badge_html}<br>'
        f'<span class="meta">{_esc(_person_meta(person))}<br>{email_note}</span>'
        f"</span></label>"
    )


def _staff_checkbox(member: dict[str, Any], legislator_name: str) -> str:
    verified = (
        f" · verified {_esc(str(member['verified_at'])[:10])}"
        if member.get("verified_at") else ""
    )
    return (
        f'<label class="rep-row"><input type="checkbox" name="staff" '
        f'value="{_esc(member["id"])}" checked>'
        f'<span class="who"><strong>{_esc(member["name"])}</strong> '
        f'<span class="pill">{_esc(member["role"])}</span><br>'
        f'<span class="meta">Staff for {_esc(legislator_name)} · '
        f'{_esc(member.get("email"))}{verified}</span></span></label>'
    )


def _bill_row(bill: dict[str, Any]) -> str:
    stance = str(bill.get("stance") or "")
    stance_html = ""
    if stance:
        stance_html = (
            f' <span class="pill stance-{_esc(stance)}">'
            f"tracker stance: {_esc(stance)}</span>"
        )
    default = default_position(stance or None)
    sel_support = " selected" if default == "support" else ""
    sel_oppose = " selected" if default == "oppose" else ""
    cats = ", ".join(c for c in (bill.get("categories") or "").split(",") if c)
    meta = " · ".join(x for x in [_esc(bill.get("public_status")), _esc(cats)] if x)
    return (
        f'<div class="bill-row"><input type="checkbox" name="bill" '
        f'value="{_esc(bill["bill_id"])}">'
        f'<span class="what"><strong>{_esc(bill["number"])}</strong> — '
        f'{_esc(bill["title"])}{stance_html}<br>'
        f'<span class="meta">{meta}</span></span>'
        f'<label>I <select name="pos_{_esc(bill["bill_id"])}">'
        f'<option value="support"{sel_support}>support</option>'
        f'<option value="oppose"{sel_oppose}>oppose</option>'
        f"</select> this bill</label></div>"
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_actbox_router(
    db_path: str | None = None,
    *,
    census_transport: CensusTransport | None = None,
) -> APIRouter:
    """Build the Act Box router.

    Backend resolution mirrors web.py: an explicit ``db_path`` means the
    SQLite store (tests); otherwise ``DATABASE_URL`` selects Postgres and
    ``LEGITRACK_DB`` / 'legitrack.db' is the SQLite default.
    """
    explicit = db_path is not None
    resolved = db_path or os.environ.get("LEGITRACK_DB", "legitrack.db")
    database_url = "" if explicit else os.environ.get("DATABASE_URL", "")
    use_pg = bool(database_url)

    def people(state: str) -> list[dict[str, Any]]:
        return _people_pg(database_url, state) if use_pg else _people_sqlite(resolved, state)

    def staff_map(person_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return _staff_pg(database_url, person_ids) if use_pg else _staff_sqlite(resolved, person_ids)

    def bills(state: str) -> list[dict[str, Any]]:
        return _bills_pg(database_url, state) if use_pg else _bills_sqlite(resolved, state)

    def tracked_states() -> list[str]:
        return _tracked_states_pg(database_url) if use_pg else _tracked_states_sqlite(resolved)

    def people_by_ids(state: str, ids: list[str]) -> list[dict[str, Any]]:
        if use_pg:
            return _people_by_ids_pg(database_url, state, ids)
        return _people_by_ids_sqlite(resolved, state, ids)

    def staff_by_ids(state: str, ids: list[int]) -> list[dict[str, Any]]:
        if use_pg:
            return _staff_by_ids_pg(database_url, state, ids)
        return _staff_by_ids_sqlite(resolved, state, ids)

    def bills_by_ids(state: str, ids: list[int]) -> list[dict[str, Any]]:
        if use_pg:
            return _bills_by_ids_pg(database_url, state, ids)
        return _bills_by_ids_sqlite(resolved, state, ids)

    router = APIRouter()

    @router.get("/act", response_class=HTMLResponse)
    def act_form() -> HTMLResponse:
        states_options = "".join(
            f'<option value="{abbr}">{_esc(name)}</option>' for abbr, name in STATES
        )
        live = tracked_states()
        if live:
            live_note = (
                "Legislator data is live today for: "
                + ", ".join(_esc(state_name(s)) for s in live)
                + ". Other states resolve your districts but show as "
                "not tracked yet."
            )
        else:
            live_note = (
                "No state legislator data has been ingested yet, so "
                "lookups will resolve your districts but show reps as "
                "not tracked."
            )
        body = f"""
<div class="hero"><h1>Act: email your own representatives</h1>
<p class="lede">Enter your home address once. We find your districts with
the US Census Geocoder, match your state senator and representative from
the tracker's own database, and draft one message about the bills you
pick — addressed to all of them, ready to send from your own email.</p>
</div>
<div class="notice">{_esc(PRIVACY_NOTE)}</div>
<form class="act-form" method="post" action="/act/lookup">
  <label>Your full name
    <input name="full_name" required autocomplete="name" placeholder="e.g. Jordan Smith">
  </label>
  <label>Street address
    <input name="street" required autocomplete="street-address" placeholder="e.g. 16 W Jones St">
  </label>
  <label>City
    <input name="city" required autocomplete="address-level2" placeholder="e.g. Raleigh">
  </label>
  <label>State
    <select name="state">{states_options}</select>
  </label>
  <label>ZIP code
    <input name="zip_code" required autocomplete="postal-code" inputmode="numeric" placeholder="e.g. 27601">
  </label>
  <button class="act-cta" type="submit">Find my representatives</button>
</form>
<p class="lede" style="margin-top:1rem">{live_note}</p>
"""
        return HTMLResponse(_act_page("Act", body))

    @router.post("/act/lookup", response_class=HTMLResponse)
    def act_lookup(
        full_name: str = Form(default=""),
        street: str = Form(default=""),
        city: str = Form(default=""),
        state: str = Form(default=""),
        zip_code: str = Form(default=""),
    ) -> HTMLResponse:
        name = full_name.strip()
        address_line = f"{street.strip()}, {city.strip()}, {state.strip().upper()} {zip_code.strip()}"
        if not name or not street.strip() or not city.strip() or not zip_code.strip():
            return HTMLResponse(_act_page("Act", (
                '<div class="hero"><h1>Almost there</h1>'
                '<div class="notice">Please fill in your name, street '
                "address, city, and ZIP so we can find your districts. "
                '<a href="/act">Back to the form</a>.</div></div>'
            )))
        try:
            districts = geocode_address(address_line, transport=census_transport)
        except CensusError:
            return HTMLResponse(_act_page("Act", (
                '<div class="hero"><h1>Lookup hiccup</h1>'
                '<div class="notice">The US Census Geocoder did not '
                "answer just now, so we could not resolve your districts. "
                "Nothing was stored. Please try again in a moment — "
                '<a href="/act">back to the form</a>.</div></div>'
            )))
        if districts is None:
            return HTMLResponse(_act_page("Act", (
                '<div class="hero"><h1>Address not matched</h1>'
                '<div class="notice">The US Census Geocoder could not '
                "match that address, so we will not guess your districts. "
                "Check the spelling (a full street address with number "
                "works best) and <a href=\"/act\">try again</a>.</div></div>"
            )))

        st = districts.state_abbr
        chips = [f"State: {state_name(st)}"]
        if districts.congressional_label:
            chips.append(f"US House: {_esc(districts.congressional_label)}")
        if districts.state_senate_district:
            chips.append(f"State Senate district: {_esc(districts.state_senate_district)}")
        if districts.state_house_district:
            chips.append(f"State House district: {_esc(districts.state_house_district)}")
        chips_html = "".join(f'<span class="chip">{c}</span>' for c in chips)

        federal = f"""
<section class="block"><h2>Your federal delegation</h2>
<div class="notice">The tracker does not hold federal legislators yet, so
rather than invent names, use the official finders:
<a href="{HOUSE_FINDER_URL}">Find your US Representative (house.gov)</a> ·
<a href="{SENATE_FINDER_URL}">US Senators (senate.gov)</a>.
Your congressional district from the Census is shown above.</div></section>
"""

        folks = people(st)
        if not folks:
            body = f"""
<div class="hero"><h1>Your districts</h1>
<p class="lede">Matched address: {_esc(districts.matched_address)}</p>
<div class="chips">{chips_html}</div>
<div class="notice">{_esc(PRIVACY_NOTE)}</div></div>
<section class="block"><h2>Your state legislators</h2>
<div class="notice">{_esc(state_name(st))} is not tracked yet — the
tracker has not ingested its legislators, so we cannot name your state
senator or representative honestly. States are being added one at a time.</div></section>
{federal}
"""
            return HTMLResponse(_act_page("Your districts", body))

        matches = match_representatives(folks, districts)
        person_ids = [str(p["person_id"]) for p in folks]
        staff = staff_map(person_ids)

        rep_sections = ""
        partial_notes: list[str] = []
        for m in matches:
            heading = m.label
            if m.district:
                heading += f" — district {_esc(m.district)}"
            section = f'<section class="block"><h2>Your {heading}</h2>'
            if m.matched:
                for p in m.matched:
                    section += _rep_checkbox(p, checked=True, badge="district-matched")
                others = [p for p in m.everyone if p not in m.matched]
                if others:
                    section += (
                        '<details class="more"><summary>See everyone else in the '
                        f"{_esc(m.label)}</summary>"
                        + "".join(_rep_checkbox(p, checked=False) for p in others)
                        + "</details>"
                    )
            else:
                partial_notes.append(m.label)
                section += (
                    '<div class="notice">District matching is partial for '
                    f"the {_esc(m.label)}: our data could not pin your exact "
                    "member, so the full chamber is listed — tick your member "
                    "if you recognise them.</div>"
                )
                section += "".join(_rep_checkbox(p, checked=False) for p in m.everyone)
            section += "</section>"
            rep_sections += section

        # Staff layer: checkboxes for verified staff of displayed reps.
        staff_rows: list[str] = []
        for p in folks:
            for member in staff.get(str(p["person_id"]), []):
                if member.get("email"):
                    staff_rows.append(_staff_checkbox(member, str(p["name"])))
        if staff_rows:
            staff_section = (
                '<section class="block"><h2>Also send to their staff</h2>'
                '<p class="lede">Legislative staff — the LD (Legislative '
                "Director) and LC (Legislative Correspondent) — are who "
                "actually read constituent mail. These contacts are on "
                "file because they were verified against an official "
                "source.</p>" + "".join(staff_rows) + "</section>"
            )
        else:
            staff_section = (
                '<section class="block"><h2>Also send to their staff</h2>'
                '<div class="notice">Staff contacts (LC/LD) are added as '
                "we verify them — none on file for your reps yet.</div></section>"
            )

        state_bills = bills(st)
        if state_bills:
            bills_section = (
                '<section class="block"><h2>Pick the bills</h2>'
                '<p class="lede">Classified bills for '
                f"{_esc(state_name(st))} from the tracker. Stance is shown "
                "where the tracker's stance pass has tagged the bill; the "
                "suggested position follows the stance and you can flip "
                "any of them.</p>"
                + "".join(_bill_row(b) for b in state_bills)
                + "</section>"
            )
        else:
            bills_section = (
                '<section class="block"><h2>Pick the bills</h2>'
                '<div class="notice">No classified bills for '
                f"{_esc(state_name(st))} yet — classifications land with "
                "ingestion.</div></section>"
            )

        body = f"""
<div class="hero"><h1>Your representatives</h1>
<p class="lede">Matched address: {_esc(districts.matched_address)}</p>
<div class="chips">{chips_html}</div>
<div class="notice">{_esc(PRIVACY_NOTE)}</div></div>
<form method="post" action="/act/compose">
  <input type="hidden" name="full_name" value="{_esc(name)}">
  <input type="hidden" name="city" value="{_esc(city.strip())}">
  <input type="hidden" name="state" value="{_esc(st)}">
  <input type="hidden" name="zip_code" value="{_esc(zip_code.strip())}">
  {rep_sections}
  {staff_section}
  {bills_section}
  <p><button class="act-cta" type="submit">Build my email</button></p>
</form>
{federal}
"""
        return HTMLResponse(_act_page("Your representatives", body))

    @router.post("/act/compose", response_class=HTMLResponse)
    async def act_compose(request: Request) -> HTMLResponse:
        form = await request.form()
        name = str(form.get("full_name") or "").strip()
        city = str(form.get("city") or "").strip()
        state = str(form.get("state") or "").strip().upper()
        zip_code = str(form.get("zip_code") or "").strip()
        rep_ids = [str(v) for v in form.getlist("rep")]
        staff_ids: list[int] = []
        for v in form.getlist("staff"):
            try:
                staff_ids.append(int(v))
            except (TypeError, ValueError):
                continue
        bill_ids: list[int] = []
        for v in form.getlist("bill"):
            try:
                bill_ids.append(int(v))
            except (TypeError, ValueError):
                continue

        if not name or not state or not bill_ids:
            return HTMLResponse(_act_page("Act", (
                '<div class="hero"><h1>Nothing to send yet</h1>'
                '<div class="notice">Pick at least one bill so there is '
                "something to write about. <a href=\"/act\">Start over</a>."
                "</div></div>"
            )))

        chosen_reps = people_by_ids(state, rep_ids)
        chosen_staff = staff_by_ids(state, staff_ids)
        chosen_bills = bills_by_ids(state, bill_ids)
        for b in chosen_bills:
            pos = str(form.get(f"pos_{b['bill_id']}") or "")
            b["position"] = pos if pos in ("support", "oppose") else default_position(b.get("stance"))

        # Staff without a published email for the chosen reps join the
        # form-assist list automatically — never silently dropped.
        chosen_person_ids = [str(p["person_id"]) for p in chosen_reps]
        staff_all = staff_map(chosen_person_ids)
        staff_by_id = {int(s["id"]): s for s in chosen_staff}

        recipients: list[dict[str, Any]] = []
        for p in chosen_reps:
            recipients.append({
                "kind": "rep", "name": p["name"], "chamber": p.get("chamber"),
                "role_title": p.get("role_title"),
                "label": _person_meta(p), "email": p.get("email_published") or None,
            })
        for s in chosen_staff:
            recipients.append({
                "kind": "staff", "name": s["name"], "chamber": None,
                "role_title": None,
                "label": f"{s['role']} for {s.get('legislator_name') or 'your legislator'}",
                "email": s.get("email") or None,
            })

        emailable = [r for r in recipients if r.get("email")]
        assist: list[dict[str, Any]] = [r for r in recipients if not r.get("email")]
        for pid in chosen_person_ids:
            for s in staff_all.get(pid, []):
                if not s.get("email") and int(s["id"]) not in staff_by_id:
                    assist.append({
                        "kind": "staff", "name": s["name"], "chamber": None,
                        "role_title": None,
                        "label": f"{s['role']} for "
                                 f"{next((p['name'] for p in chosen_reps if str(p['person_id']) == pid), 'your legislator')}",
                        "email": None,
                    })

        subject = build_subject(chosen_bills)
        body_text = build_body(
            visitor_name=name, city=city, state_abbr=state, zip_code=zip_code,
            bills=chosen_bills, recipients=recipients,
        )
        result = build_mailto([str(r["email"]) for r in emailable], subject, body_text)

        if result.mode == "all" and result.url:
            cta = (
                f'<p><a class="act-cta" href="{_esc(result.url)}">'
                "Open in my email — addressed to all of them</a></p>"
                f"<p class=\"meta\" style=\"color:var(--color-text-muted)\">"
                f"One email, {len(emailable)} recipient(s), from your own "
                "mail app. Review it there before you send — nothing has "
                "been sent yet.</p>"
            )
        elif result.mode in ("first", "first_no_body") and result.url:
            first_name = emailable[0]["name"] if emailable else "the first recipient"
            reason = (
                "the full message was too long for a single email link, "
                "so the button opens your mail app addressed to "
                f"{_esc(first_name)} only"
            )
            if result.mode == "first_no_body":
                reason += ", without the message text — paste it from the copy box below"
            cta = (
                f'<p><a class="act-cta" href="{_esc(result.url)}">'
                "Open in my email</a></p>"
                f'<div class="notice">Heads up: {reason}. The copy box '
                "below has the full text for everyone else on your list.</div>"
            )
        else:
            cta = (
                '<div class="notice">No recipient with a published email '
                "was selected, so there is no email link to open. Use the "
                "copy box below and the contact routes listed under it.</div>"
            )

        addressed = "".join(
            f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['label'])}</td>"
            f"<td>{_esc(r['email'])}</td></tr>"
            for r in emailable
        ) or '<tr><td colspan="3">None with a published email.</td></tr>'

        assist_html = ""
        if assist:
            rows = "".join(
                f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['label'])}</td>"
                f'<td><a href="{_esc(form_assist_url(state))}">'
                "Official contact route</a></td></tr>"
                for r in assist
            )
            assist_html = f"""
<section class="block"><h2>No published email — form follow-up</h2>
<p class="lede">These recipients have no published email in our data, so
they are not on the email above. They are never silently dropped: use
their official contact route and paste the same message text.</p>
<table class="data"><tr><th>Name</th><th>Role</th><th>Contact</th></tr>{rows}</table>
</section>
"""

        body = f"""
<div class="hero"><h1>Your message is ready</h1>
<p class="lede">Subject: {_esc(subject)}</p></div>
{cta}
<section class="block"><h2>Addressed to</h2>
<table class="data"><tr><th>Name</th><th>Role</th><th>Email</th></tr>{addressed}</table>
</section>
<section class="block"><h2>Copy the message text</h2>
<p class="lede">Works anywhere — webmail, a contact form, or a staffer's
inbox.</p>
<textarea class="copybox" readonly>Subject: {_esc(subject)}

{_esc(body_text)}</textarea>
</section>
{assist_html}
"""
        return HTMLResponse(_act_page("Your message", body))

    return router


#: Ready-to-mount router with default (env-resolved) backend.
router = create_actbox_router()
