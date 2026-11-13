"""OpenStates legislator ingestion (SPEC section 5 legislators table).

Flow for one state:
    jurisdiction id for the state -> GET /people page by page ->
    parse each person -> upsert into the local store keyed on the
    OpenStates person id -> record a pipeline_runs row.

Validated live on 2026-11-13 (PROOF-PULL-OPENSTATES-NC-2026-11-13.md):
NC returned 173 people with party, current_role (title / chamber
classification / district), published email and photo URL. Only
published fields are stored — nothing is constructed or guessed here;
pattern-constructed contacts belong to the Act Box registry (SPEC 7.2),
not to this ingestion.

Usage:
    export OPENSTATES_API_KEY=...      # environment only, never a flag
    python -m legitrack.legislators --state NC --db legitrack.db
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .openstates_client import OpenStatesClient, jurisdiction_id_for_state
from .store import LocalStore, PostgresStore, open_store


@dataclass(frozen=True)
class Legislator:
    """One legislator row parsed from an OpenStates /people result."""

    openstates_person_id: str
    name: str
    first_name: str = ""
    last_name: str = ""
    party: str = ""
    chamber: str = ""          # lower / upper / legislature (org_classification)
    role_title: str = ""       # e.g. Representative / Senator
    district: str = ""
    email_published: str = ""  # published address only, may be empty
    photo_url: str = ""
    jurisdiction_id: str = ""  # OCD jurisdiction id string
    state_abbr: str = ""


class PeopleSource(Protocol):
    """The slice of OpenStatesClient that ingestion needs (fakeable)."""

    queries_used: int

    def list_people(
        self, jurisdiction: str, *, page: int = 1, per_page: int = 20
    ) -> dict[str, Any]: ...


def parse_person(row: dict[str, Any], state_abbr: str = "") -> Legislator:
    """Parse one OpenStates person object into a Legislator."""
    role = row.get("current_role") or {}
    jurisdiction = row.get("jurisdiction") or {}
    return Legislator(
        openstates_person_id=str(row.get("id", "")),
        name=str(row.get("name", "")),
        first_name=str(row.get("given_name", "")),
        last_name=str(row.get("family_name", "")),
        party=str(row.get("party", "")),
        chamber=str(role.get("org_classification", "")),
        role_title=str(role.get("title", "")),
        district=str(role.get("district", "")),
        email_published=str(row.get("email", "") or ""),
        photo_url=str(row.get("image", "") or ""),
        jurisdiction_id=str(jurisdiction.get("id", "")),
        state_abbr=state_abbr.upper(),
    )


@dataclass(frozen=True)
class PeopleRunSummary:
    state: str
    people_seen: int
    people_stored: int
    queries_used: int


def ingest_people(
    client: PeopleSource,
    store: LocalStore | PostgresStore,
    state: str = "NC",
    *,
    per_page: int = 100,
) -> PeopleRunSummary:
    """Fetch all people pages for a state and upsert them."""
    state = state.upper()
    jurisdiction = jurisdiction_id_for_state(state)
    run_id = store.start_run("openstates_people", state, None)
    seen = 0
    stored = 0
    try:
        page = 1
        while True:
            payload = client.list_people(jurisdiction, page=page, per_page=per_page)
            results = payload.get("results", [])
            for row in results:
                person = parse_person(row, state)
                if not person.openstates_person_id or not person.name:
                    continue
                store.upsert_legislator(person)
                stored += 1
            seen += len(results)
            pagination = payload.get("pagination", {}) or {}
            max_page = int(pagination.get("max_page", page) or page)
            if page >= max_page or not results:
                break
            page += 1
    except Exception:
        store.finish_run(
            run_id, status="error", bills_seen=seen, bills_changed=0,
            bills_fetched=stored, queries_used=client.queries_used,
            error="openstates people ingestion failed (details withheld from run log)",
        )
        raise
    store.finish_run(
        run_id, status="ok", bills_seen=seen, bills_changed=0,
        bills_fetched=stored, queries_used=client.queries_used,
    )
    return PeopleRunSummary(
        state=state, people_seen=seen, people_stored=stored,
        queries_used=client.queries_used,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="legitrack-legislators",
        description="OpenStates legislator ingestion for the Black Legislation Tracker.",
    )
    parser.add_argument("--state", default="NC", help="two-letter state abbreviation (default NC)")
    parser.add_argument("--db", default="legitrack.db", help="SQLite store path (default legitrack.db); ignored when DATABASE_URL is set — Postgres is used instead")
    parser.add_argument("--per-page", type=int, default=100, help="page size (default 100)")
    args = parser.parse_args(argv)

    client = OpenStatesClient()  # key from OPENSTATES_API_KEY environment only
    store = open_store(Path(args.db))
    try:
        summary = ingest_people(client, store, args.state, per_page=args.per_page)
    finally:
        store.close()
    print(
        f"[RUN] {summary.state} legislators: {summary.people_seen} seen, "
        f"{summary.people_stored} stored, {summary.queries_used} queries used"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
