# LegiTrack — Black Legislation Tracker, Phase 1 engine

A daily-updating pipeline that tracks state legislation affecting Black
Americans across all 50 state legislatures (plus Congress): what each bill
does, where it stands, and — in later phases — how to act on it. Full
product spec: `../SPEC.md`. This folder is the **Phase 1 engine only**:
ingestion, status normalisation, and rules-based classification v1.

## Architecture

```
Render Cron (daily, Phase 1 deploy step -- not yet scheduled)
        |
        v
legitrack.ingest -- LegiScanClient.getSessionList(state)
                 -> LegiScanClient.getMasterList(session_id)   [param is `id`]
                 -> change_hash delta vs stored hashes
                 -> LegiScanClient.getBill(bill_id)  ONLY for changed bills
                 -> status.map_legiscan_status()   (public ladder)
                 -> taxonomy.classify_legiscan_bill()  (rules v1)
                 -> open_store()  (SQLite locally; PostgresStore when
                    DATABASE_URL is set — the Render production path)
                 -> pipeline_runs row

OpenStatesClient -- cross-check counts + legislators/contacts/photos
                    (legislator ingest wired via legitrack.legislators,
                    same open_store() backend choice)
```

Why the delta design matters: LegiScan's free tier is quota-limited.
One master-list pull returns the `change_hash` of every bill in a
session, so a daily run costs ~2 queries per state plus one per bill
that actually changed — not one per bill.

## Setup

Python 3.11+. The only runtime dependency is `psycopg[binary]`, and it
is imported lazily — only when `DATABASE_URL` is set (the Postgres
backend); local SQLite runs never touch it. Keys live in the
**environment only** — never in code, flags, files, or this repo:

```bash
export LEGISCAN_API_KEY="..."      # from https://legiscan.com/legiscan (free account)
export OPENSTATES_API_KEY="..."    # from https://open.pluralpolicy.com/accounts/profile/
```

## Run ingestion

```bash
cd app
python -m legitrack.ingest --state NC --dry-run     # 2 queries, no writes, no detail fetches
python -m legitrack.ingest --state NC --limit 25    # cap detail fetches (quota guard)
python -m legitrack.ingest --state NC --session-id 2189 --db legitrack.db
```

`--dry-run` prints how many bills were seen and how many changed versus
the local store, and records nothing. A real run upserts the master list,
fetches detail only for changed bills, classifies them with rules v1,
and writes a `pipeline_runs` row (also on failure, with status `error`).

Session choice: an explicit `--session-id` wins; otherwise the most
recent **regular** (non-special) session is used — LegiScan lists tiny
special sessions first, so "first row" is the wrong default.

## Run the tests

```bash
cd app
python -m pytest        # 58 tests pass, 2 skip; no network, no keys -- fake transports + synthetic fixtures
```

The 2 skipped tests are the live-Postgres end-to-end tests
(`tests/test_postgres_live.py`): they run only when
`LEGITRACK_PG_TEST_URL` points at a throwaway Postgres. They were run
for real when the backend landed (embedded Postgres via the pip
`pgserver` package): schema applied, fake-client ingestion written,
delta verified, web counts read back through the Postgres path.

## Web app

Server-rendered FastAPI app in `legitrack/web.py` (the ingestion engine
stays standard-library only; the web deps are in `requirements.txt`):

```bash
cd app
pip install -r requirements.txt
export LEGITRACK_DB=legitrack.db      # SQLite store to read (read-only)
uvicorn legitrack.web:app --port 8000  # PYTHONPATH=src if not installed
```

If `DATABASE_URL` is set in the environment instead, the app reads
Postgres (the Render production path) and `LEGITRACK_DB` is ignored —
an explicitly passed `db_path` in code/tests still wins, so the test
suite stays deterministic.

Pages: `/` landing with live counts read from the database at request
time (bills tracked, legislators, states ingested, classified bills,
last pipeline run time/status, status-ladder breakdown, category
counts) — numbers are never hardcoded, and with no database attached
the page says so instead of inventing figures. `/bills?status=&category=`
is the filterable bill list; `/legislators` shows name, party, chamber,
district and photo; `/healthz` returns JSON for uptime checks.

Design: a dark, professional placeholder skin. **All** colours, fonts,
spacing and radii are CSS custom properties in one `:root` token block
at the top of `web.py` — the Figma design pass re-skins the whole site
by swapping those token values; routes and markup do not change.

Verified 2026-11-13 against a copy of the live NC database: `/healthz`
reported `{"bills": 2338}`, the landing page showed 2,338 bills / 173
legislators / last run 2026-11-13 11:56 UTC (status ok), the Education
category filter returned its 9 bills, and the original database file's
checksum was identical before and after (the app opens it read-only).

## Deploy to Render

`render.yaml` (repo root of this app folder) is a Render Blueprint
defining: the free web service above, a daily cron job
(`0 11 * * *` UTC ≈ 06:00 ET) running the NC ingestion
(`--limit 100`) plus legislator ingestion, a free Postgres database,
and the two API keys declared `sync: false`.

**Jared's steps (the only manual part):**

1. Push this repo to GitHub (the live `legitrack.db` is gitignored —
   never commit it).
2. In the Render dashboard: **New → Blueprint**, select the GitHub repo,
   click **Apply**.
3. When prompted for the unsynced env vars, paste the values:
   `LEGISCAN_API_KEY` (from legiscan.com/legiscan) and
   `OPENSTATES_API_KEY` (from open.pluralpolicy.com) — into the cron
   service's environment. They exist nowhere in the repo.
4. Wait for the first web deploy, open the service URL, and check
   `/healthz`.

**What goes live:** the public web app (free tier — it sleeps when
idle, so the first hit after a quiet spell is slow), the scheduled
daily ingestion job, and the managed Postgres wired into both
services as `DATABASE_URL`. The Postgres backend is real: ingestion
writes to Postgres via `PostgresStore` (which applies `schema.sql`
idempotently on startup) and the web app reads from the same
database, so once the first cron run lands, the site shows live
counts. Before that first run the landing page honestly says no data
is available yet — it never displays invented numbers.

**Known limitations of this Phase 1 deploy (stated honestly):**
- The site shows data only after the first ingestion run populates
  Postgres (trigger the cron once from the Render dashboard after
  deploy, or wait for the 11:00 UTC schedule).
- Ingestion is NC-only in the cron command; all-state rollout is a
  command change to the cron, not a code change.
- Free Postgres on Render expires after ~30 days on the free plan —
  upgrade or export before then if the deploy becomes permanent.
- Local/dev runs still default to SQLite; Postgres is used only when
  `DATABASE_URL` is present in the environment.

## Database schema

`schema.sql` is the Postgres DDL (idempotent `CREATE TABLE IF NOT
EXISTS`): jurisdictions, sessions, legislators, bills (source IDs +
`change_hash`), bill_versions (with `text_hash` for Phase 2 diffing),
bill_classifications, votes (lean: roll-call summaries), pipeline_runs.
`legitrack.store.PostgresStore` applies it on construction and maps
the ingestion interface onto the normalised tables (jurisdictions
resolved from `state_abbr`, bills keyed on `legiscan_bill_id`, detail
payloads in the `bills.detail` JSONB column, classifications linked by
internal bill PK with both category id and label). The local run uses
the SQLite mirror in the same module, selected by
`legitrack.store.open_store()` whenever `DATABASE_URL` is unset, with
the same upsert keys. Three columns were added to schema.sql for this
parity (each commented in the DDL): `bills.detail`,
`bill_classifications.category_name`, `legislators.role_title` +
`legislators.state_abbr`.

## Status ladder

LegiScan codes: 0 Not introduced, 1 Introduced, 2 Engrossed,
3 Enrolled, 4 Passed, 5 Vetoed, 6 Failed. `legitrack/status.py` maps
them onto the public ladder Introduced → Committee → Passed chamber →
Passed both → Signed/Vetoed → Dead — conservatively: **the status code
carries no committee stage**, so code 1 stays "Introduced" rather than
guessing "Committee", and unknown codes map to "Unknown", never a guess.

## What is proven vs stubbed

**Proven**
- Both API shapes were validated live on 2026-11-13 before this code was
  written (see `../PROOF-PULL-NC-2026-11-13.md` and
  `../PROOF-PULL-OPENSTATES-NC-2026-11-13.md`): NC regular session =
  2,338 bills on **both** feeds; `change_hash` on every master-list bill;
  `text_hash` on bill texts; OpenStates NC legislators = 173 with
  published emails and photos.
- The delta logic, parsers, status mapping, taxonomy, clients
  (env-key handling, browser User-Agent, no key leakage in errors), and a
  full fake-client ingestion pass are covered by the test suite.
- **Postgres backend verified end-to-end against a real server**
  (2026-11-13): with an embedded Postgres (pip `pgserver`) as the
  target, `PostgresStore` applied schema.sql, a fake-client ingestion
  wrote 3 synthetic bills + classifications + 1 legislator, a second
  run confirmed the hash delta (0 changed), and the web app served the
  counts through its Postgres path (`/healthz` → bills: 3). The
  durable version of this check is `tests/test_postgres_live.py`
  (2 tests, skip without `LEGITRACK_PG_TEST_URL`); SQL shape is also
  pinned server-free by fake-connection tests in
  `tests/test_postgres_store.py`.

**Stubbed / not yet built**
- First live run completed 2026-11-13 (see ../LIVE-RUN-NC-2026-11-13.md):
  NC master list (2,338 bills) ingested, 25 details fetched/classified,
  delta verified live (second run: 0 changed, 2 queries), and 173 NC
  legislators stored from OpenStates. Known limitation logged there:
  a --limit run marks un-fetched bills' hashes stored, so a full
  backfill needs a detail-where-NULL pass.
- Postgres IS the wired production backend (see Proven); what is
  still schema-only in Postgres is the Phase 2 surface — bill_versions,
  votes and sponsor links are DDL'd but nothing populates them yet.
- OpenStates legislator ingestion IS wired (legitrack.legislators,
  added 2026-11-13, tested with synthetic fakes): /people pages are
  parsed and upserted into the local store keyed on the OpenStates
  person id, storing only published fields. Its first live run follows
  the same one-command pattern as the LegiScan ingestion.
- Rules v1 classification: keyword/subject matching only, no direction
  tags, no summaries, no model pass (Phase 2). Taxonomy is the SPEC 6.2
  draft, pending Jared's sign-off.
- Web UI exists now (see "Web app" + "Deploy to Render" above) as a
  token-skinned placeholder ahead of the Figma design pass; locally it
  serves the real NC data. No Act Box, no prediction/copycat/scorecard
  features (Phases 2–4 in the spec). Votes are roll-call summaries only.
- Committee rung of the status ladder needs history/progress events from
  bill detail — designed, not implemented.
