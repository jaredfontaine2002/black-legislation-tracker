"""Phase 1 LegiScan ingestion CLI (SPEC section 6, Phase 1 item 1).

Flow for one state:
    getSessionList -> pick a session -> getMasterList ->
    compare each bill's change_hash with the stored hash ->
    getBill detail ONLY for new/changed bills -> store detail +
    rules-v1 classification -> record a pipeline_runs row.

Quota discipline (SPEC section 3): a steady-state run costs 2 queries
plus one per actually-changed bill. --limit caps detail fetches and
--dry-run stops before any detail fetch or write, so a test run never
burns the monthly budget.

Usage:
    export LEGISCAN_API_KEY=...        # environment only, never a flag
    python -m legitrack.ingest --state NC --dry-run
    python -m legitrack.ingest --state NC --limit 25 --db legitrack.db
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .legiscan_client import LegiScanClient, MasterBill, Session
from .status import map_legiscan_status
from .store import LocalStore, PostgresStore, open_store
from .taxonomy import classify_legiscan_bill


class SessionSource(Protocol):
    """The slice of LegiScanClient that ingestion needs (fakeable in tests)."""

    queries_used: int

    def get_session_list(self, state: str) -> list[Session]: ...

    def get_master_list(self, session_id: int) -> list[MasterBill]: ...

    def get_bill(self, bill_id: int) -> dict: ...


@dataclass(frozen=True)
class RunSummary:
    """Counts from one ingestion run (also what the CLI prints)."""

    state: str
    session_id: int
    session_name: str
    bills_seen: int
    bills_changed: int
    bills_fetched: int
    queries_used: int
    dry_run: bool


def compute_changed_bills(
    master_bills: list[MasterBill], stored_hashes: dict[int, str]
) -> list[MasterBill]:
    """Delta logic: bills that are new, or whose change_hash differs from
    the stored hash. Bills with an empty source change_hash are treated as
    changed (we cannot prove they are unchanged)."""
    changed: list[MasterBill] = []
    for bill in master_bills:
        stored = stored_hashes.get(bill.bill_id)
        if stored is None or bill.change_hash == "" or stored != bill.change_hash:
            changed.append(bill)
    return changed


def pick_session(sessions: list[Session], session_id: int | None = None) -> Session:
    """Choose the session to ingest.

    Explicit --session-id wins. Otherwise prefer the most recent session
    whose tag says "Regular Session". The live NC run on 2026-11-13 showed
    why the tag, not the ``special`` flag, has to decide: the 2026 extra
    session (5 bills) carries ``special=0`` in the API despite its
    "1st Special Session" tag, and it is also the newest by year — both
    the flag and 'first row / newest' heuristics pick the wrong session.
    Falls back to non-special sessions, then to all sessions.
    """
    if not sessions:
        raise ValueError("LegiScan returned no sessions for this state")
    if session_id is not None:
        for session in sessions:
            if session.session_id == session_id:
                return session
        raise ValueError(f"session id {session_id} not found for this state")
    regular = [s for s in sessions if "regular" in s.session_tag.lower()]
    if not regular:
        regular = [s for s in sessions if not s.special]
    pool = regular or sessions
    return max(pool, key=lambda s: (s.year_start or 0, s.session_id))


def ingest_state(
    client: SessionSource,
    store: LocalStore | PostgresStore,
    state: str = "NC",
    *,
    session_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> RunSummary:
    """Run one ingestion pass. Pure orchestration: all I/O goes through
    the injected client and store, so tests run it with fakes and no keys.
    """
    state = state.upper()
    sessions = client.get_session_list(state)
    session = pick_session(sessions, session_id)
    master = client.get_master_list(session.session_id)
    stored = store.stored_hashes(session.session_id)
    changed = compute_changed_bills(master, stored)
    to_fetch = changed[:limit] if limit is not None else changed

    if dry_run:
        return RunSummary(
            state=state,
            session_id=session.session_id,
            session_name=session.session_name,
            bills_seen=len(master),
            bills_changed=len(changed),
            bills_fetched=0,
            queries_used=client.queries_used,
            dry_run=True,
        )

    run_id = store.start_run("legiscan_ingest", state, session.session_id)
    fetched = 0
    try:
        store.upsert_session(session)
        for bill in master:
            store.upsert_master_bill(
                session.session_id, bill, str(map_legiscan_status(bill.status))
            )
        for bill in to_fetch:
            detail = client.get_bill(bill.bill_id)
            store.store_detail(bill.bill_id, detail)
            store.replace_classifications(bill.bill_id, classify_legiscan_bill(detail))
            fetched += 1
    except Exception as exc:
        store.finish_run(
            run_id,
            status="error",
            bills_seen=len(master),
            bills_changed=len(changed),
            bills_fetched=fetched,
            queries_used=client.queries_used,
            error=f"{type(exc).__name__}: ingestion failed (details withheld from run log)",
        )
        raise
    store.finish_run(
        run_id,
        status="ok",
        bills_seen=len(master),
        bills_changed=len(changed),
        bills_fetched=fetched,
        queries_used=client.queries_used,
    )
    return RunSummary(
        state=state,
        session_id=session.session_id,
        session_name=session.session_name,
        bills_seen=len(master),
        bills_changed=len(changed),
        bills_fetched=fetched,
        queries_used=client.queries_used,
        dry_run=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="legitrack-ingest",
        description="Phase 1 LegiScan ingestion for the Black Legislation Tracker.",
    )
    parser.add_argument("--state", default="NC", help="two-letter state abbreviation (default NC)")
    parser.add_argument("--session-id", type=int, default=None, help="LegiScan session id override")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max bill-detail fetches this run (quota guard)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="sessions + master list only: no detail fetches, no writes",
    )
    parser.add_argument(
        "--db",
        default="legitrack.db",
        help="SQLite store path for local runs (default legitrack.db); "
        "ignored when DATABASE_URL is set — Postgres is used instead",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")

    client = LegiScanClient()  # key from LEGISCAN_API_KEY environment variable only
    store = open_store(Path(args.db))
    try:
        summary = ingest_state(
            client,
            store,
            args.state,
            session_id=args.session_id,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        store.close()
    mode = "DRY RUN" if summary.dry_run else "RUN"
    print(
        f"[{mode}] {summary.state} session {summary.session_id} ({summary.session_name}): "
        f"{summary.bills_seen} bills seen, {summary.bills_changed} changed, "
        f"{summary.bills_fetched} detail fetches, {summary.queries_used} queries used"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
