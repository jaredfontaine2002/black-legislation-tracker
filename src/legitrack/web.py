"""Public web app for the Black Legislation Tracker (SPEC sections 4, 6, 8).

Server-rendered FastAPI app — no JavaScript framework, no build step.
Every number on every page is read from the database at request time;
nothing is hardcoded (SPEC section 2: real data only).

Data access (Phase 1 deploy):
* When ``DATABASE_URL`` is set (Render wires it from the managed
  Postgres in render.yaml), pages read from Postgres (``schema.sql``)
  via psycopg — that is the production path, and the ingestion cron
  writes to the same database, so the deployed site shows live data.
* Otherwise reads the SQLite store at ``LEGITRACK_DB`` (default
  ``legitrack.db``), opened read-only so the web process can never
  mutate ingestion data. An explicitly passed ``db_path`` (tests) always
  selects SQLite, even if ``DATABASE_URL`` happens to be in the env.

Design: all colours, fonts, spacing and radii are CSS custom properties
in the single ``:root`` token block below. A Figma design pass re-skins
the whole site by swapping those tokens — markup and routes stay put.

Run locally:
    export LEGITRACK_DB=legitrack.db
    uvicorn legitrack.web:app --reload --port 8000
"""

from __future__ import annotations

import html
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .map_data import US_MAP_VIEWBOX, US_STATE_NAMES, US_STATE_PATHS

PRODUCT_NAME = "Black Legislation Tracker"

# ---------------------------------------------------------------------------
# Design tokens — THE re-skin point for the Figma design pass.
# Change values here and nowhere else; all components reference var(--...).
# ---------------------------------------------------------------------------
TOKENS_CSS = """
:root {
  --color-bg: #0b0f14;
  --color-surface: #131a22;
  --color-surface-raised: #1a232e;
  --color-border: #26313d;
  --color-text: #e8eef4;
  --color-text-muted: #93a3b1;
  --color-accent: #d9a441;        /* gold — primary accent */
  --color-accent-strong: #f0bc55;
  --color-positive: #3fb97f;
  --color-negative: #e05d5d;
  --color-info: #5aa2e0;
  --color-score-low: #e05d5d;      /* Friendliness Score 0 — most harmful */
  --color-score-mid: #e9c44c;      /* Friendliness Score 50 */
  --color-score-high: #3fb97f;     /* Friendliness Score 100 — most supportive */
  --color-map-untracked: #2b3644;  /* state with no data / not enough data */
  --color-map-stroke: #0b0f14;
  --font-display: "Georgia", "Times New Roman", serif;
  --font-body: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --space-xs: 0.25rem;
  --space-sm: 0.5rem;
  --space-md: 1rem;
  --space-lg: 1.5rem;
  --space-xl: 2.5rem;
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;
  --shadow-card: 0 1px 3px rgba(0, 0, 0, 0.4);
  --max-width: 1120px;
}
"""

BASE_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--color-bg); color: var(--color-text);
  font-family: var(--font-body); line-height: 1.55;
}
a { color: var(--color-accent-strong); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: var(--max-width); margin: 0 auto; padding: 0 var(--space-lg); }
header.site {
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}
header.site .wrap {
  display: flex; align-items: baseline; gap: var(--space-lg);
  padding-top: var(--space-md); padding-bottom: var(--space-md); flex-wrap: wrap;
}
.brand { font-family: var(--font-display); font-size: 1.35rem; color: var(--color-text); }
.brand:hover { text-decoration: none; }
nav.main { display: flex; gap: var(--space-lg); margin-left: auto; }
nav.main a { color: var(--color-text-muted); font-weight: 600; }
nav.main a:hover { color: var(--color-text); text-decoration: none; }
.hero { padding: var(--space-xl) 0 var(--space-lg); }
.hero h1 {
  font-family: var(--font-display); font-weight: 600;
  font-size: clamp(2rem, 5vw, 3.2rem); line-height: 1.12; margin: 0 0 var(--space-md);
}
.hero p.lede { color: var(--color-text-muted); font-size: 1.12rem; max-width: 46rem; margin: 0; }
.freshness {
  display: inline-flex; gap: var(--space-sm); align-items: center;
  background: var(--color-surface-raised); border: 1px solid var(--color-border);
  border-radius: 999px; padding: var(--space-xs) var(--space-md);
  font-size: 0.88rem; color: var(--color-text-muted); margin-bottom: var(--space-lg);
}
.freshness .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--color-positive); }
.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-md); margin: var(--space-lg) 0 var(--space-xl);
}
.stat {
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-md); padding: var(--space-lg); box-shadow: var(--shadow-card);
}
.stat .num { font-size: 2.1rem; font-weight: 700; line-height: 1; }
.stat .label { color: var(--color-text-muted); font-size: 0.9rem; margin-top: var(--space-sm); }
section.block { margin: 0 0 var(--space-xl); }
section.block h2 { font-family: var(--font-display); font-size: 1.5rem; margin: 0 0 var(--space-md); }
.bar-row { display: grid; grid-template-columns: 150px 1fr 60px; gap: var(--space-md); align-items: center; margin-bottom: var(--space-sm); }
.bar-track { background: var(--color-surface-raised); border-radius: 999px; height: 12px; overflow: hidden; }
.bar-fill { background: var(--color-accent); height: 100%; border-radius: 999px; }
.bar-row .n { font-family: var(--font-mono); text-align: right; color: var(--color-text-muted); }
table.data { width: 100%; border-collapse: collapse; background: var(--color-surface);
  border: 1px solid var(--color-border); border-radius: var(--radius-md); overflow: hidden; }
table.data th, table.data td { text-align: left; padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--color-border); vertical-align: top; }
table.data th { color: var(--color-text-muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.05em; }
table.data tr:last-child td { border-bottom: none; }
.pill {
  display: inline-block; padding: 0 var(--space-sm); border-radius: 999px;
  background: var(--color-surface-raised); border: 1px solid var(--color-border);
  font-size: 0.8rem; color: var(--color-text-muted); white-space: nowrap;
}
.filters { display: flex; gap: var(--space-md); flex-wrap: wrap; margin-bottom: var(--space-lg); }
.filters label { display: flex; flex-direction: column; gap: var(--space-xs); font-size: 0.85rem; color: var(--color-text-muted); }
.filters select, .filters button {
  background: var(--color-surface-raised); color: var(--color-text);
  border: 1px solid var(--color-border); border-radius: var(--radius-sm);
  padding: var(--space-sm) var(--space-md); font: inherit;
}
.filters button { background: var(--color-accent); color: #14100a; font-weight: 700; cursor: pointer; align-self: end; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: var(--space-md); }
.person {
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-md); padding: var(--space-lg); box-shadow: var(--shadow-card);
  display: flex; gap: var(--space-md);
}
.person img { width: 64px; height: 64px; object-fit: cover; border-radius: 50%; border: 2px solid var(--color-border); }
.person .avatar-fallback {
  width: 64px; height: 64px; border-radius: 50%; flex: none;
  background: var(--color-surface-raised); border: 2px solid var(--color-border);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-display); font-size: 1.3rem; color: var(--color-accent);
}
.person h3 { margin: 0 0 var(--space-xs); font-size: 1.02rem; }
.person p { margin: 0; color: var(--color-text-muted); font-size: 0.88rem; }
.notice {
  border: 1px solid var(--color-accent); border-radius: var(--radius-md);
  padding: var(--space-md) var(--space-lg); margin: var(--space-lg) 0;
  background: var(--color-surface); color: var(--color-text-muted);
}
.us-map { width: 100%; height: auto; display: block; }
.us-map path { stroke: var(--color-map-stroke); stroke-width: 0.8; }
.us-map path.untracked { fill: var(--color-map-untracked); }
.map-legend {
  display: flex; flex-wrap: wrap; gap: var(--space-sm) var(--space-lg);
  align-items: center; margin: var(--space-md) 0 var(--space-lg);
  color: var(--color-text-muted); font-size: 0.9rem;
}
.legend-scale { display: flex; align-items: center; gap: var(--space-sm); }
.legend-bar {
  width: 220px; height: 12px; border-radius: 999px;
  background: linear-gradient(90deg, var(--color-score-low), var(--color-score-mid), var(--color-score-high));
  border: 1px solid var(--color-border);
}
.swatch {
  display: inline-block; width: 14px; height: 14px; border-radius: 3px;
  background: var(--color-map-untracked); border: 1px solid var(--color-border);
  vertical-align: -2px; margin-right: var(--space-xs);
}
.method { color: var(--color-text-muted); font-size: 0.92rem; max-width: 52rem; }
footer.site {
  border-top: 1px solid var(--color-border); color: var(--color-text-muted);
  padding: var(--space-lg) 0 var(--space-xl); font-size: 0.88rem; margin-top: var(--space-xl);
}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _fmt(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------------------
# Data access — read-only SQLite (Postgres: see TODO in module docstring)
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _empty_stats() -> dict[str, Any]:
    """Payload for a database that IS attached but has no schema yet.

    Distinct from ``{"available": False}`` (no database configured or
    reachable): the connection works, the ingestion pipeline simply has
    not run, so there is nothing to count. Zeroes here are real zeroes,
    read from a live connection — never invented numbers.
    """
    return {
        "available": True,
        "empty": True,
        "bills": 0,
        "bills_with_detail": 0,
        "legislators": 0,
        "states": 0,
        "classified_bills": 0,
        "status_breakdown": [],
        "category_counts": [],
        "last_run": None,
    }


def _stats(db_path: str) -> dict[str, Any]:
    conn = _connect(db_path)
    if conn is None:
        return {"available": False}
    try:
        has_bills = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bills'"
        ).fetchone()
        if has_bills is None:
            return _empty_stats()
        last_run = conn.execute(
            "SELECT job, status, started_at, finished_at FROM pipeline_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "available": True,
            "empty": False,
            "bills": _scalar(conn, "SELECT COUNT(*) FROM bills"),
            "bills_with_detail": _scalar(
                conn, "SELECT COUNT(*) FROM bills WHERE detail_json IS NOT NULL"
            ),
            "legislators": _scalar(conn, "SELECT COUNT(*) FROM legislators"),
            "states": _scalar(conn, "SELECT COUNT(DISTINCT state_abbr) FROM sessions"),
            "classified_bills": _scalar(
                conn, "SELECT COUNT(DISTINCT legiscan_bill_id) FROM bill_classifications"
            ),
            "status_breakdown": [
                (str(r[0]), int(r[1]))
                for r in conn.execute(
                    "SELECT public_status, COUNT(*) FROM bills "
                    "GROUP BY public_status ORDER BY COUNT(*) DESC"
                )
            ],
            "category_counts": [
                (str(r[0]), int(r[1]))
                for r in conn.execute(
                    "SELECT category_name, COUNT(*) FROM bill_classifications "
                    "GROUP BY category_name ORDER BY COUNT(*) DESC"
                )
            ],
            "last_run": dict(last_run) if last_run else None,
        }
    finally:
        conn.close()


def _bills(db_path: str, status: str, category: str, limit: int) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        sql = (
            "SELECT b.number, b.title, b.public_status, b.status_date, b.url, "
            "GROUP_CONCAT(DISTINCT c.category_name) AS categories "
            "FROM bills b LEFT JOIN bill_classifications c "
            "ON c.legiscan_bill_id = b.legiscan_bill_id WHERE 1=1"
        )
        params: list[Any] = []
        if status:
            sql += " AND b.public_status = ?"
            params.append(status)
        if category:
            sql += (
                " AND b.legiscan_bill_id IN (SELECT legiscan_bill_id "
                "FROM bill_classifications WHERE category_name = ?)"
            )
            params.append(category)
        sql += " GROUP BY b.legiscan_bill_id ORDER BY b.number LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _filter_options(db_path: str) -> tuple[list[str], list[str]]:
    conn = _connect(db_path)
    if conn is None:
        return [], []
    try:
        statuses = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT public_status FROM bills WHERE public_status IS NOT NULL "
                "ORDER BY public_status"
            )
        ]
        categories = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT category_name FROM bill_classifications ORDER BY category_name"
            )
        ]
        return statuses, categories
    finally:
        conn.close()


def _legislators(db_path: str, limit: int) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT name, party, chamber, role_title, district, photo_url, state_abbr "
            "FROM legislators ORDER BY chamber, CAST(district AS INTEGER), name LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Minimum stance-tagged (supportive + harmful) bills before a state earns
# a Friendliness Score on the /map page — below this it stays grey.
MAP_MIN_SAMPLE = 5


def _empty_state_scores() -> dict[str, Any]:
    """Same distinction as _empty_stats: attached but schema-less."""
    return {"available": True, "empty": True, "counts": {}}


def _state_scores(db_path: str) -> dict[str, Any]:
    """Per-state Friendliness Score inputs from the SQLite store.

    Returns counts[abbr] = {"classified", "supportive", "harmful"} with
    DISTINCT-bill counts, classifications joined through bills -> sessions.

    Defensive about the stance engine's schema: bill_classifications may
    not have a ``stance`` column yet (it lands with the stance pass), and
    the table may not exist at all. Absence is "no stance data" — never
    an error, never invented numbers.
    """
    conn = _connect(db_path)
    if conn is None:
        return {"available": False}
    try:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "bills" not in tables:
            return _empty_state_scores()
        counts: dict[str, dict[str, int]] = {}
        if "bill_classifications" in tables and "sessions" in tables:
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(bill_classifications)")
            }
            join = (
                "FROM bill_classifications c "
                "JOIN bills b ON b.legiscan_bill_id = c.legiscan_bill_id "
                "JOIN sessions s ON s.legiscan_session_id = b.legiscan_session_id "
            )
            if "stance" in cols:
                sql = (
                    "SELECT s.state_abbr AS abbr, "
                    "COUNT(DISTINCT c.legiscan_bill_id) AS classified, "
                    "COUNT(DISTINCT CASE WHEN c.stance = 'supportive' "
                    "THEN c.legiscan_bill_id END) AS supportive, "
                    "COUNT(DISTINCT CASE WHEN c.stance = 'harmful' "
                    "THEN c.legiscan_bill_id END) AS harmful "
                    + join
                    + "GROUP BY s.state_abbr"
                )
            else:
                sql = (
                    "SELECT s.state_abbr AS abbr, "
                    "COUNT(DISTINCT c.legiscan_bill_id) AS classified, "
                    "0 AS supportive, 0 AS harmful "
                    + join
                    + "GROUP BY s.state_abbr"
                )
            for r in conn.execute(sql):
                counts[str(r["abbr"])] = {
                    "classified": int(r["classified"]),
                    "supportive": int(r["supportive"]),
                    "harmful": int(r["harmful"]),
                }
        return {"available": True, "empty": False, "counts": counts}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data access — read-only Postgres (used when DATABASE_URL is set)
# Same return shapes as the SQLite functions above, so the rendering
# code below is backend-agnostic. Connections are per-request and
# failures degrade to the same honest "not available" payloads as a
# missing SQLite file — never invented numbers, never a 500.
# ---------------------------------------------------------------------------

def _pg_connect(dsn: str):  # type: ignore[no-untyped-def]
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def _pg_scalar(conn, sql: str, params: tuple = ()) -> int:  # type: ignore[no-untyped-def]
    row = conn.execute(sql, params).fetchone()
    if not row:
        return 0
    value = row["n"] if isinstance(row, dict) else row[0]
    return int(value) if value is not None else 0


def _iso(value: Any) -> Any:
    """Render timestamps the way the SQLite store returns them (ISO text)."""
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return value


def _stats_pg(dsn: str) -> dict[str, Any]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return {"available": False}
    try:
        # A freshly provisioned database connects fine but has no tables
        # yet — schema.sql is applied by the ingestion store, never by
        # this read-only app. That is "connected but empty", NOT "no
        # database attached", and the two must not be conflated.
        reg = conn.execute("SELECT to_regclass('public.bills') AS t").fetchone()
        reg_value = reg["t"] if isinstance(reg, dict) else reg[0]
        if reg_value is None:
            return _empty_stats()
        last_run = conn.execute(
            "SELECT job, status, started_at, finished_at FROM pipeline_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last = None
        if last_run:
            last = {k: _iso(v) for k, v in dict(last_run).items()}
        return {
            "available": True,
            "empty": False,
            "bills": _pg_scalar(conn, "SELECT COUNT(*) AS n FROM bills"),
            "bills_with_detail": _pg_scalar(
                conn, "SELECT COUNT(*) AS n FROM bills WHERE detail IS NOT NULL"
            ),
            "legislators": _pg_scalar(conn, "SELECT COUNT(*) AS n FROM legislators"),
            "states": _pg_scalar(
                conn,
                "SELECT COUNT(DISTINCT j.state_abbr) AS n FROM sessions s "
                "JOIN jurisdictions j ON j.id = s.jurisdiction_id",
            ),
            "classified_bills": _pg_scalar(
                conn, "SELECT COUNT(DISTINCT bill_id) AS n FROM bill_classifications"
            ),
            "status_breakdown": [
                (str(r["public_status"]), int(r["n"]))
                for r in conn.execute(
                    "SELECT public_status, COUNT(*) AS n FROM bills "
                    "GROUP BY public_status ORDER BY COUNT(*) DESC"
                )
            ],
            "category_counts": [
                (str(r["cat"]), int(r["n"]))
                for r in conn.execute(
                    "SELECT COALESCE(category_name, category) AS cat, COUNT(*) AS n "
                    "FROM bill_classifications GROUP BY cat ORDER BY COUNT(*) DESC"
                )
            ],
            "last_run": last,
        }
    except Exception:
        return {"available": False}
    finally:
        conn.close()


def _bills_pg(dsn: str, status: str, category: str, limit: int) -> list[dict[str, Any]]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        sql = (
            "SELECT b.bill_number AS number, b.title, b.public_status, "
            "b.status_date::text AS status_date, b.legiscan_url AS url, "
            "STRING_AGG(DISTINCT COALESCE(c.category_name, c.category), ',') AS categories "
            "FROM bills b LEFT JOIN bill_classifications c ON c.bill_id = b.id WHERE 1=1"
        )
        params: list[Any] = []
        if status:
            sql += " AND b.public_status = %s"
            params.append(status)
        if category:
            sql += (
                " AND b.id IN (SELECT bill_id FROM bill_classifications "
                "WHERE COALESCE(category_name, category) = %s)"
            )
            params.append(category)
        sql += " GROUP BY b.id ORDER BY b.bill_number LIMIT %s"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params)]
    except Exception:
        return []
    finally:
        conn.close()


def _filter_options_pg(dsn: str) -> tuple[list[str], list[str]]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return [], []
    try:
        statuses = [
            str(r["public_status"])
            for r in conn.execute(
                "SELECT DISTINCT public_status FROM bills WHERE public_status IS NOT NULL "
                "ORDER BY public_status"
            )
        ]
        categories = [
            str(r["cat"])
            for r in conn.execute(
                "SELECT DISTINCT COALESCE(category_name, category) AS cat "
                "FROM bill_classifications ORDER BY cat"
            )
        ]
        return statuses, categories
    except Exception:
        return [], []
    finally:
        conn.close()


def _legislators_pg(dsn: str, limit: int) -> list[dict[str, Any]]:
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT name, party, chamber, role_title, district, photo_url, state_abbr "
            "FROM legislators ORDER BY chamber, "
            "CASE WHEN district ~ '^[0-9]+$' THEN CAST(district AS INTEGER) END NULLS LAST, "
            "name LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _state_scores_pg(dsn: str) -> dict[str, Any]:
    """Postgres twin of _state_scores — same payload shape, same
    defensive stance-column handling (checked via information_schema)."""
    try:
        conn = _pg_connect(dsn)
    except Exception:
        return {"available": False}
    try:
        reg = conn.execute("SELECT to_regclass('public.bills') AS t").fetchone()
        reg_value = reg["t"] if isinstance(reg, dict) else reg[0]
        if reg_value is None:
            return _empty_state_scores()
        counts: dict[str, dict[str, int]] = {}
        reg = conn.execute(
            "SELECT to_regclass('public.bill_classifications') AS t"
        ).fetchone()
        reg_value = reg["t"] if isinstance(reg, dict) else reg[0]
        if reg_value is not None:
            stance_row = conn.execute(
                "SELECT 1 AS x FROM information_schema.columns "
                "WHERE table_name = 'bill_classifications' AND column_name = 'stance'"
            ).fetchone()
            join = (
                "FROM bill_classifications c "
                "JOIN bills b ON b.id = c.bill_id "
                "JOIN sessions s ON s.id = b.session_id "
                "JOIN jurisdictions j ON j.id = s.jurisdiction_id "
            )
            if stance_row:
                sql = (
                    "SELECT j.state_abbr AS abbr, "
                    "COUNT(DISTINCT c.bill_id) AS classified, "
                    "COUNT(DISTINCT CASE WHEN c.stance = 'supportive' "
                    "THEN c.bill_id END) AS supportive, "
                    "COUNT(DISTINCT CASE WHEN c.stance = 'harmful' "
                    "THEN c.bill_id END) AS harmful "
                    + join
                    + "GROUP BY j.state_abbr"
                )
            else:
                sql = (
                    "SELECT j.state_abbr AS abbr, "
                    "COUNT(DISTINCT c.bill_id) AS classified, "
                    "0 AS supportive, 0 AS harmful "
                    + join
                    + "GROUP BY j.state_abbr"
                )
            for r in conn.execute(sql):
                row = dict(r)
                counts[str(row["abbr"])] = {
                    "classified": int(row["classified"]),
                    "supportive": int(row["supportive"]),
                    "harmful": int(row["harmful"]),
                }
        return {"available": True, "empty": False, "counts": counts}
    except Exception:
        return {"available": False}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — {_esc(PRODUCT_NAME)}</title>
<style>{TOKENS_CSS}{BASE_CSS}</style>
</head>
<body>
<header class="site"><div class="wrap">
  <a class="brand" href="/">{_esc(PRODUCT_NAME)}</a>
  <nav class="main">
    <a href="/">Overview</a><a href="/bills">Bills</a><a href="/legislators">Legislators</a><a href="/map">Map</a><a href="/act">Act</a>
  </nav>
</div></header>
<main class="wrap">{body}</main>
<footer class="site"><div class="wrap">
  Data: LegiScan + OpenStates, pulled by the tracker's own daily pipeline.
  Classifications on this site are rules-based (v1) and published with their
  method — see the project README for what is proven vs still being built.
</div></footer>
</body>
</html>"""


_CHAMBER_LABELS = {"lower": "House", "upper": "Senate", "executive": "Executive", "legislature": "Legislature"}


def _friendliness_score(supportive: int, harmful: int) -> int | None:
    """100 x supportive / (supportive + harmful), or None below the
    minimum sample (MAP_MIN_SAMPLE tagged bills) — None means the state
    stays grey, never a score built on a handful of bills."""
    tagged = supportive + harmful
    if tagged < MAP_MIN_SAMPLE:
        return None
    return round(100 * supportive / tagged)


# Friendliness Score ramp on an absolute 0-100 scale: red (0) -> amber
# (50) -> green (100). Hex values mirror the --color-score-* tokens.
_SCORE_STOPS = ((0, (224, 93, 93)), (50, (233, 196, 76)), (100, (63, 185, 127)))


def _score_color(score: int) -> str:
    for (lo, c_lo), (hi, c_hi) in zip(_SCORE_STOPS, _SCORE_STOPS[1:]):
        if lo <= score <= hi:
            frac = (score - lo) / (hi - lo) if hi > lo else 0.0
            rgb = tuple(round(a + (b - a) * frac) for a, b in zip(c_lo, c_hi))
            return "#{:02x}{:02x}{:02x}".format(*rgb)
    return "#{:02x}{:02x}{:02x}".format(*_SCORE_STOPS[-1][1])


def create_app(db_path: str | None = None) -> FastAPI:
    """App factory.

    An explicit ``db_path`` always means the SQLite store at that path
    (tests rely on this). Without one, ``DATABASE_URL`` (Render) selects
    Postgres; otherwise the SQLite path from ``LEGITRACK_DB`` /
    'legitrack.db' is used, exactly as before.
    """
    explicit = db_path is not None
    resolved = db_path or os.environ.get("LEGITRACK_DB", "legitrack.db")
    database_url = "" if explicit else os.environ.get("DATABASE_URL", "")
    use_pg = bool(database_url)

    def stats() -> dict[str, Any]:
        return _stats_pg(database_url) if use_pg else _stats(resolved)

    def bills_list(status: str, category: str, limit: int) -> list[dict[str, Any]]:
        if use_pg:
            return _bills_pg(database_url, status, category, limit)
        return _bills(resolved, status, category, limit)

    def filter_options() -> tuple[list[str], list[str]]:
        return _filter_options_pg(database_url) if use_pg else _filter_options(resolved)

    def legislators_list(limit: int) -> list[dict[str, Any]]:
        return _legislators_pg(database_url, limit) if use_pg else _legislators(resolved, limit)

    def state_scores() -> dict[str, Any]:
        return _state_scores_pg(database_url) if use_pg else _state_scores(resolved)

    app = FastAPI(title=PRODUCT_NAME)

    from .actbox import create_actbox_router

    app.include_router(create_actbox_router(db_path if explicit else None))

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        s = stats()
        if not s.get("available"):
            database = "unavailable"
        elif s.get("empty"):
            database = "connected_empty"
        else:
            database = "connected"
        return JSONResponse(
            {"status": "ok",
             "data_available": bool(s.get("available")) and not s.get("empty"),
             "database": database,
             "bills": s.get("bills", 0)}
        )

    @app.get("/", response_class=HTMLResponse)
    def landing() -> HTMLResponse:
        s = stats()
        if not s.get("available"):
            body = (
                '<div class="hero"><h1>Legislation that affects Black Americans, '
                "tracked daily.</h1>"
                '<div class="notice">No database is attached to this deployment yet, '
                "so no counts are shown — this page never displays invented numbers. "
                "Run the ingestion pipeline against the configured database "
                "(DATABASE_URL / LEGITRACK_DB) and the live counts appear here "
                "automatically.</div></div>"
            )
            return HTMLResponse(_page("Overview", body))

        if s.get("empty"):
            body = (
                '<div class="hero"><h1>Legislation that affects Black Americans, '
                "tracked daily.</h1>"
                '<div class="notice">The database is connected — it just '
                "hasn&rsquo;t received its first ingestion run yet, so there are "
                "no bills to count. This page never displays invented numbers; "
                "live counts appear automatically once the pipeline runs."
                "</div></div>"
            )
            return HTMLResponse(_page("Overview", body))

        last = s["last_run"] or {}
        last_when = last.get("finished_at") or last.get("started_at") or "—"
        freshness = (
            f'<div class="freshness"><span class="dot"></span>'
            f'Last pipeline run: {_esc(last_when)} · status {_esc(last.get("status", "—"))}'
            f' · {_esc(last.get("job", ""))}</div>'
        )
        stats_html = "".join(
            f'<div class="stat"><div class="num">{_fmt(n)}</div>'
            f'<div class="label">{_esc(label)}</div></div>'
            for n, label in [
                (s["bills"], "bills tracked"),
                (s["legislators"], "legislators in the database"),
                (s["states"], "states ingested so far"),
                (s["classified_bills"], "bills classified (rules v1)"),
            ]
        )
        total = max(s["bills"], 1)
        bars = "".join(
            f'<div class="bar-row"><span>{_esc(name)}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{count * 100 // total}%"></div></div>'
            f'<span class="n">{_fmt(count)}</span></div>'
            for name, count in s["status_breakdown"]
        )
        cat_rows = "".join(
            f"<tr><td>{_esc(name)}</td><td>{_fmt(count)}</td></tr>"
            for name, count in s["category_counts"]
        ) or '<tr><td colspan="2">No classifications stored yet.</td></tr>'
        body = f"""
<div class="hero">
  {freshness}
  <h1>Legislation that affects Black Americans, tracked daily.</h1>
  <p class="lede">Every bill in every state legislature, normalised onto one
  status ladder, classified against a published taxonomy, and refreshed by a
  scheduled pipeline — no hand-updated spreadsheets.</p>
</div>
<div class="stats">{stats_html}</div>
<section class="block"><h2>Where bills stand</h2>{bars}</section>
<section class="block"><h2>Classified bills by issue area (rules v1)</h2>
<table class="data"><tr><th>Category</th><th>Bills</th></tr>{cat_rows}</table>
<p><a href="/bills">Browse the bills &rarr;</a> &nbsp;·&nbsp;
<a href="/legislators">Meet the legislators &rarr;</a></p></section>
"""
        return HTMLResponse(_page("Overview", body))

    @app.get("/bills", response_class=HTMLResponse)
    def bills(
        status: str = Query(default=""),
        category: str = Query(default=""),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> HTMLResponse:
        statuses, categories = filter_options()
        rows = bills_list(status, category, limit)

        def _options(values: list[str], current: str) -> str:
            opts = '<option value="">All</option>'
            for v in values:
                sel = " selected" if v == current else ""
                opts += f'<option value="{_esc(v)}"{sel}>{_esc(v)}</option>'
            return opts

        trs = ""
        for b in rows:
            cats = ", ".join(
                f'<span class="pill">{_esc(c)}</span>'
                for c in (b.get("categories") or "").split(",") if c
            )
            title = _esc(b["title"])
            if b.get("url"):
                title = f'<a href="{_esc(b["url"])}">{title}</a>'
            trs += (
                f"<tr><td><strong>{_esc(b['number'])}</strong></td><td>{title}</td>"
                f"<td>{_esc(b['public_status'])}</td><td>{cats}</td></tr>"
            )
        if not trs:
            trs = '<tr><td colspan="4">No bills match — or no database is attached yet.</td></tr>'
        body = f"""
<div class="hero"><h1>Bills</h1>
<p class="lede">Filter by pipeline status or rules-v1 category. Showing {len(rows)} bill(s).</p></div>
<form class="filters" method="get" action="/bills">
  <label>Status <select name="status">{_options(statuses, status)}</select></label>
  <label>Category <select name="category">{_options(categories, category)}</select></label>
  <button type="submit">Filter</button>
</form>
<table class="data"><tr><th>Bill</th><th>Title</th><th>Status</th><th>Categories</th></tr>{trs}</table>
"""
        return HTMLResponse(_page("Bills", body))

    @app.get("/legislators", response_class=HTMLResponse)
    def legislators(limit: int = Query(default=300, ge=1, le=1000)) -> HTMLResponse:
        people = legislators_list(limit)
        cards = ""
        for p in people:
            initials = "".join(w[0] for w in str(p["name"]).split()[:2]).upper()
            if p.get("photo_url"):
                visual = f'<img src="{_esc(p["photo_url"])}" alt="Photo of {_esc(p["name"])}" loading="lazy">'
            else:
                visual = f'<div class="avatar-fallback">{_esc(initials)}</div>'
            chamber = _CHAMBER_LABELS.get(str(p.get("chamber") or ""), str(p.get("chamber") or ""))
            role = p.get("role_title") or chamber
            meta = " · ".join(
                x for x in [
                    _esc(p.get("party")),
                    _esc(role),
                    f"District {_esc(p['district'])}" if p.get("district") else "",
                    _esc(p.get("state_abbr")),
                ] if x
            )
            cards += (
                f'<div class="person">{visual}<div><h3>{_esc(p["name"])}</h3>'
                f"<p>{meta}</p></div></div>"
            )
        if not cards:
            cards = '<div class="notice">No legislators stored yet — run the OpenStates people ingestion.</div>'
        body = f"""
<div class="hero"><h1>Legislators</h1>
<p class="lede">{len(people)} legislators with published contact data, from OpenStates.</p></div>
<div class="cards">{cards}</div>
"""
        return HTMLResponse(_page("Legislators", body))

    @app.get("/map", response_class=HTMLResponse)
    def map_page() -> HTMLResponse:
        data = state_scores()
        if not data.get("available"):
            body = (
                '<div class="hero"><h1>Friendliness by state.</h1>'
                '<div class="notice">No database is attached to this deployment yet, '
                "so no counts are shown — this page never displays invented numbers. "
                "Run the ingestion pipeline against the configured database "
                "(DATABASE_URL / LEGITRACK_DB) and the live counts appear here "
                "automatically.</div></div>"
            )
            return HTMLResponse(_page("Map", body))

        counts = data.get("counts", {})
        paths_html = ""
        ranked: list[tuple[str, dict[str, int], int | None]] = []
        for abbr, name in US_STATE_NAMES.items():
            info = counts.get(abbr)
            if not info or info["classified"] == 0:
                tooltip = f"{name} — not tracked yet"
                paths_html += (
                    f'<path d="{US_STATE_PATHS[abbr]}" class="untracked">'
                    f"<title>{_esc(tooltip)}</title></path>"
                )
                continue
            sup, harm = info["supportive"], info["harmful"]
            tagged = sup + harm
            score = _friendliness_score(sup, harm)
            if score is None:
                bill_word = "bill" if tagged == 1 else "bills"
                tooltip = f"{name} — not enough data yet ({tagged} tagged {bill_word})"
                paths_html += (
                    f'<path d="{US_STATE_PATHS[abbr]}" class="untracked">'
                    f"<title>{_esc(tooltip)}</title></path>"
                )
            else:
                tooltip = f"{name} — score {score} ({sup} supportive · {harm} harmful)"
                paths_html += (
                    f'<path d="{US_STATE_PATHS[abbr]}" style="fill:{_score_color(score)}">'
                    f"<title>{_esc(tooltip)}</title></path>"
                )
            ranked.append((name, info, score))

        # Ranked table — the accessible alternative to the colour scale:
        # scored states first (best score first), then states still below
        # the minimum sample (most tagged bills first).
        ranked.sort(
            key=lambda item: (
                item[2] is None,
                -(item[2] or 0),
                -(item[1]["supportive"] + item[1]["harmful"]),
                item[0],
            )
        )
        rows = ""
        for name, info, score in ranked:
            score_cell = str(score) if score is not None else "Not enough data yet"
            rows += (
                f"<tr><td>{_esc(name)}</td><td>{_fmt(info['supportive'])}</td>"
                f"<td>{_fmt(info['harmful'])}</td><td>{score_cell}</td></tr>"
            )
        if not rows:
            rows = '<tr><td colspan="4">No states tracked yet.</td></tr>'

        notice = ""
        if data.get("empty"):
            notice = (
                '<div class="notice">The database is connected — it just '
                "hasn&rsquo;t received its first ingestion run yet, so there is "
                "no stance data to map yet. Scores appear automatically once "
                "the pipeline runs.</div>"
            )
        body = f"""
<div class="hero"><h1>Friendliness by state</h1>
<p class="lede">How supportive is each state&rsquo;s legislation for Black
Americans? Colour shows the Friendliness Score, computed from the stance of
classified bills affecting Black Americans in each state.</p></div>
{notice}
<svg class="us-map" viewBox="{US_MAP_VIEWBOX}" role="img" aria-label="Map of the United States coloured by Friendliness Score">{paths_html}</svg>
<div class="map-legend">
  <span class="legend-scale"><span>0 · most harmful</span><span class="legend-bar"></span><span>100 · most supportive</span></span>
  <span><span class="swatch"></span>Not tracked yet</span>
  <span><span class="swatch"></span>Not enough data yet</span>
</div>
<p class="method"><strong>How the score works.</strong> Score = share of
stance-tagged bills that are supportive: 100 &times; supportive &divide;
(supportive + harmful). Stance is rules-based (v1) from bill language; mixed
and unclear bills are excluded. States need at least 5 tagged bills to be
scored.</p>
<section class="block"><h2>States ranked by Friendliness Score</h2>
<table class="data"><tr><th>State</th><th>Supportive bills</th><th>Harmful bills</th><th>Friendliness Score</th></tr>{rows}</table>
</section>
"""
        return HTMLResponse(_page("Map", body))

    return app


app = create_app()


def run() -> None:
    """Console entry point (legitrack-web). Render uses uvicorn directly."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
