#!/usr/bin/env python3
"""Backfill rules-based stance v1 onto existing bill_classifications.

For every classification row in a LOCAL SQLite store, recompute stance
(legitrack.stance) from the bill's stored detail — title + subjects
from bills.detail_json, plus the row's stored matched_terms — and
write stance / stance_terms back. Rows for bills without stored
detail fall back to the master-list title and no subjects.

Idempotent: re-running recomputes the same values. Prints the tally
the map's Friendliness Score consumes (classification rows AND
distinct bills per stance label; a bill can carry several labels
across its rows).

Usage:
    python scripts/backfill_stance.py [--db legitrack.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legitrack.stance import LABELS, determine_stance  # noqa: E402


def ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bill_classifications)")}
    for column in ("stance", "stance_terms"):
        if column not in columns:
            conn.execute(f"ALTER TABLE bill_classifications ADD COLUMN {column} TEXT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="legitrack.db", help="SQLite store path")
    parser.add_argument("--dry-run", action="store_true", help="compute and report only")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    rows = conn.execute(
        """
        SELECT bc.legiscan_bill_id, bc.category_id, bc.matched_terms,
               b.title AS master_title, b.detail_json
        FROM bill_classifications bc
        JOIN bills b ON b.legiscan_bill_id = bc.legiscan_bill_id
        ORDER BY bc.legiscan_bill_id, bc.category_id
        """
    ).fetchall()

    updates: list[tuple[str, str, int, str]] = []
    row_tally: Counter[str] = Counter()
    bill_labels: dict[int, set[str]] = {}
    for row in rows:
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        title = html.unescape(str(detail.get("title") or row["master_title"] or ""))
        subjects = detail.get("subjects") or []
        matched_terms = json.loads(row["matched_terms"]) if row["matched_terms"] else []
        stance = determine_stance(
            title=title, subjects=subjects, matched_terms=matched_terms
        )
        row_tally[stance.label] += 1
        bill_labels.setdefault(int(row["legiscan_bill_id"]), set()).add(stance.label)
        updates.append(
            (stance.label, stance.terms_json(), int(row["legiscan_bill_id"]), row["category_id"])
        )

    if not args.dry_run:
        conn.executemany(
            """
            UPDATE bill_classifications SET stance = ?, stance_terms = ?
            WHERE legiscan_bill_id = ? AND category_id = ? AND method = 'rule'
            """,
            updates,
        )
        conn.commit()

    bill_tally: Counter[str] = Counter()
    for labels in bill_labels.values():
        for label in labels:
            bill_tally[label] += 1
    tagged = sum(1 for labels in bill_labels.values() if labels - {"unknown"})

    mode = "DRY RUN" if args.dry_run else "BACKFILL"
    print(f"[{mode}] {args.db}: {len(rows)} classification rows on {len(bill_labels)} bills")
    for label in LABELS:
        print(f"  rows  {label:<10} {row_tally.get(label, 0)}")
    for label in LABELS:
        print(f"  bills {label:<10} {bill_tally.get(label, 0)}")
    print(f"  bills with at least one non-unknown row: {tagged}")
    supportive = bill_tally.get("supportive", 0)
    harmful = bill_tally.get("harmful", 0)
    if supportive + harmful >= 5:
        score = round(100 * supportive / (supportive + harmful), 1)
        print(f"  Friendliness Score inputs: supportive={supportive} harmful={harmful} -> {score}")
    else:
        print(
            f"  Friendliness Score inputs: supportive={supportive} harmful={harmful} "
            "-> below the 5-tagged-bill threshold, state is unscored"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
