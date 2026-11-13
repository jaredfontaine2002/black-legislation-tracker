#!/usr/bin/env python3
"""PASS 1 all-states pull: LegiScan master lists for all 50 states + US
Congress into the LOCAL SQLite store, with title/description-based
classification + stance.

Design notes (read before changing):
* The API is reached ONLY through the Secure Vault connector helper
  (``legiscan_call.py``) as a subprocess. No raw key exists anywhere in
  this process — the helper holds a surrogate the runtime swaps.
* Session choice reuses ``legitrack.ingest.pick_session`` verbatim
  (prefer session_tag containing "Regular", latest year) and bill
  storage reuses ``legitrack.store.LocalStore`` upsert paths and
  ``legitrack.ingest.compute_changed_bills`` for the delta count.
  Nothing here writes SQL for storage; the only direct SQL is
  read-only reporting (existing-classification lookup + tallies).
* Master-list rows carry title + description but NO subjects (checked
  live on WY 2026: 0 of 335 rows had subjects; NC proof pull agrees).
  Classification is therefore ``classify_bill(title, description)``
  and stance is ``apply_stance(..., title only)``. Master
  classification is stored through the same
  ``replace_classifications`` path as detail classification — the
  rows are honest title-evidence rows (method='rule', matched terms
  carried), NOT faked detail classifications.
* Bills that already carry rule classifications (e.g. NC's 25
  detail-fetched bills) are NOT reclassified: their detail-based rows
  (subject evidence, higher confidence) are strictly richer than a
  title-only pass would produce, and replace_classifications would
  overwrite them.
* Budget: exactly 2 helper calls per state (getSessionList +
  getMasterList) => ~102 calls for 51 jurisdictions, against the
  10,000/month free cap. Small delay between calls; the run stops
  after 3 consecutive state failures and reports.
* Resumable: progress is written to
  ``~/workspace/legislation-tracker-project/pull-all-states-progress.json``
  after every state; a re-run skips states recorded as done.

Usage:
    python3 scripts/pull_all_states.py [--states NC,CA] [--force]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legitrack.ingest import compute_changed_bills, pick_session  # noqa: E402
from legitrack.legiscan_client import parse_master_list, parse_session_list  # noqa: E402
from legitrack.stance import apply_stance, load_stance_rules  # noqa: E402
from legitrack.status import map_legiscan_status  # noqa: E402
from legitrack.store import LocalStore  # noqa: E402
from legitrack.taxonomy import classify_bill, load_taxonomy  # noqa: E402

HELPER = Path.home() / "workspace/custom-connectors/custom.legiscan/bin/legiscan_call.py"
APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = APP_DIR / "legitrack.db"
PROGRESS_FILE = (
    Path.home()
    / "workspace/legislation-tracker-project/pull-all-states-progress.json"
)

STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "US",
]

CALL_DELAY_SECONDS = 1.0
MAX_CONSECUTIVE_FAILURES = 3


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HelperError(RuntimeError):
    """A legiscan_call.py invocation failed (message holds no secrets:
    the helper never sees a raw key and neither do we)."""


def helper_call(op: str, arg: str) -> dict:
    """Run one connector-helper call and return its decoded JSON."""
    result = subprocess.run(
        [sys.executable, str(HELPER), op, arg],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        raise HelperError(
            f"{op} {arg} exited {result.returncode}: {tail[-1] if tail else 'no stderr'}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HelperError(f"{op} {arg} returned non-JSON output") from exc


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_progress(progress: dict) -> None:
    tmp = PROGRESS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)


def existing_classified_bill_ids(db_path: Path, session_id: int) -> set[int]:
    """Read-only: bills of this session that already carry rule
    classifications (their richer rows must not be overwritten)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT bc.legiscan_bill_id
            FROM bill_classifications bc
            JOIN bills b ON b.legiscan_bill_id = bc.legiscan_bill_id
            WHERE b.legiscan_session_id = ? AND bc.method = 'rule'
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return {int(r[0]) for r in rows}


def state_tallies(db_path: Path, session_id: int) -> dict:
    """Read-only: per-state map inputs after the pull — classified
    bills and DISTINCT bills per stance label (the same aggregation
    legitrack.web._state_scores performs)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT bc.legiscan_bill_id) AS classified,
                   COUNT(DISTINCT CASE WHEN bc.stance = 'supportive'
                        THEN bc.legiscan_bill_id END) AS supportive,
                   COUNT(DISTINCT CASE WHEN bc.stance = 'harmful'
                        THEN bc.legiscan_bill_id END) AS harmful,
                   COUNT(DISTINCT CASE WHEN bc.stance = 'mixed'
                        THEN bc.legiscan_bill_id END) AS mixed,
                   COUNT(DISTINCT CASE WHEN bc.stance = 'unknown'
                        THEN bc.legiscan_bill_id END) AS unknown,
                   COUNT(*) AS rows_total
            FROM bill_classifications bc
            JOIN bills b ON b.legiscan_bill_id = bc.legiscan_bill_id
            WHERE b.legiscan_session_id = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return {k: int(row[k]) for k in row.keys()}


def pull_state(store: LocalStore, db_path: Path, state: str, taxonomy, rules) -> dict:
    """One jurisdiction: 2 helper calls, upserts, classification."""
    queries_before = 0  # helper calls are counted locally per state
    sessions_payload = helper_call("getSessionList", state)
    queries_before += 1
    sessions = parse_session_list(sessions_payload)
    session = pick_session(sessions)
    time.sleep(CALL_DELAY_SECONDS)

    master_payload = helper_call("getMasterList", str(session.session_id))
    queries_before += 1
    master = parse_master_list(master_payload)

    stored = store.stored_hashes(session.session_id)
    changed = compute_changed_bills(master, stored)

    run_id = store.start_run("legiscan_master_pull", state, session.session_id)
    try:
        store.upsert_session(session)
        for bill in master:
            store.upsert_master_bill(
                session.session_id, bill, str(map_legiscan_status(bill.status))
            )
        skip_ids = existing_classified_bill_ids(db_path, session.session_id)
        newly_classified = 0
        for bill in master:
            if bill.bill_id in skip_ids:
                continue
            classifications = apply_stance(
                classify_bill(
                    title=bill.title,
                    description=bill.description,
                    subjects=[],
                    taxonomy=taxonomy,
                ),
                title=bill.title,
                subjects=[],
                rules=rules,
            )
            if classifications:
                store.replace_classifications(bill.bill_id, classifications)
                newly_classified += 1
    except Exception as exc:
        store.finish_run(
            run_id,
            status="error",
            bills_seen=len(master),
            bills_changed=len(changed),
            bills_fetched=0,
            queries_used=queries_before,
            error=f"{type(exc).__name__}: master pull failed (details withheld)",
        )
        raise
    store.finish_run(
        run_id,
        status="ok",
        bills_seen=len(master),
        bills_changed=len(changed),
        bills_fetched=0,
        queries_used=queries_before,
    )
    tallies = state_tallies(db_path, session.session_id)
    return {
        "state": state,
        "session_id": session.session_id,
        "session_name": session.session_name,
        "session_tag": session.session_tag,
        "bills_stored": len(master),
        "bills_changed": len(changed),
        "newly_classified_bills": newly_classified,
        "skipped_already_classified": len(skip_ids),
        "queries_used": queries_before,
        "tallies": tallies,
        "status": "done",
        "finished_at": utc_now_iso(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite store path")
    parser.add_argument(
        "--states",
        default=None,
        help="comma-separated subset (default: NC first, then all 50 states + US)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run states already recorded as done in the progress file",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.states:
        order = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    else:
        # NC first: its delta must read 0 changed — the idempotency proof
        # is cheapest to fail early on.
        order = ["NC"] + [s for s in STATES if s != "NC"]

    progress = load_progress()
    progress.setdefault("states", {})
    progress.setdefault("started_at", utc_now_iso())
    taxonomy = load_taxonomy()
    rules = load_stance_rules()

    store = LocalStore(db_path)
    total_queries = 0
    consecutive_failures = 0
    try:
        for state in order:
            prior = progress["states"].get(state, {})
            if prior.get("status") == "done" and not args.force:
                print(f"[skip] {state}: already done ({prior.get('session_name')})", flush=True)
                continue
            print(f"[pull] {state}: getSessionList + getMasterList ...", flush=True)
            try:
                record = pull_state(store, db_path, state, taxonomy, rules)
            except Exception as exc:  # noqa: BLE001 — recorded, counted, reported
                consecutive_failures += 1
                progress["states"][state] = {
                    "state": state,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "finished_at": utc_now_iso(),
                }
                save_progress(progress)
                print(f"[fail] {state}: {type(exc).__name__}: {exc}", flush=True)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"[stop] {MAX_CONSECUTIVE_FAILURES} consecutive failures — stopping run",
                        flush=True,
                    )
                    break
                continue
            consecutive_failures = 0
            total_queries += record["queries_used"]
            progress["states"][state] = record
            progress["total_queries"] = sum(
                int(s.get("queries_used", 0))
                for s in progress["states"].values()
                if s.get("status") == "done"
            )
            progress["updated_at"] = utc_now_iso()
            save_progress(progress)
            t = record["tallies"]
            print(
                f"[done] {state} {record['session_name']}: {record['bills_stored']} bills, "
                f"{record['bills_changed']} changed, {t['classified']} classified bills "
                f"(supportive {t['supportive']} / harmful {t['harmful']} / "
                f"mixed {t['mixed']} / unknown {t['unknown']}), "
                f"{record['queries_used']} queries",
                flush=True,
            )
            time.sleep(CALL_DELAY_SECONDS)
    finally:
        store.close()
    done = sum(1 for s in progress["states"].values() if s.get("status") == "done")
    failed = [s for s, v in progress["states"].items() if v.get("status") == "failed"]
    print(
        f"[summary] {done} jurisdictions done, {len(failed)} failed "
        f"({', '.join(sorted(failed)) or 'none'}), "
        f"{progress.get('total_queries', 0)} queries recorded in progress file",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
