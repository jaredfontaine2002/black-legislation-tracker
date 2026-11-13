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

## Act Box (v1)

`legitrack/actbox.py` — a self-contained FastAPI router (`/act`,
`/act/lookup`, `/act/compose`) implementing SPEC section 7's action
feature. It reuses the web app's design tokens and mirrors its
SQLite/Postgres read paths. **Wiring is the one remaining step:** add
`app.include_router(actbox.create_actbox_router(db_path))` in
`web.create_app` plus an "Act" nav link (noted as a TODO in the
module docstring).

**What works**

- **Address → districts.** The visitor enters name + home address;
  the US Census Geocoder (free, official, no key) resolves state,
  congressional district, and state senate/house districts. Unmatched
  addresses get an honest error — districts are never guessed. The
  address is used only for that lookup: never stored, never logged
  (the lookup path is read-only; a test proves the database file is
  byte-identical before and after).
- **Your own reps only** (SPEC 7.1 constituent principle). State
  legislators come from our own OpenStates-ingested store,
  district-matched where the stored district supports it. Where a
  chamber cannot be district-matched, the page says district matching
  is partial and lists the full chamber for the visitor to pick from —
  a match is never claimed that the data does not support. States with
  no ingested legislators (everything except NC today) show a
  "not tracked yet" state.
- **Staff layer (LC/LD).** New `staff_contacts` table (schema.sql +
  both stores): legislator FK, role ∈ LC / LD / Chief of Staff /
  District Director, name, published email, source URL, verified-at.
  It is seeded with **nothing** — rows enter only from verified
  sources. The lookup page offers an "Also send to their staff"
  checkbox group only where verified rows exist; otherwise it shows
  one honest line: *"Staff contacts (LC/LD) are added as we verify
  them — none on file for your reps yet."*
- **Compose + send from the visitor's own mail app.** Bill picker =
  classified bills for the visitor's state with stance shown where
  tagged; each bill gets a support/oppose choice defaulting from its
  stance (supportive → support, harmful → oppose, always editable).
  The send button is one `mailto:` addressed to every selected
  recipient with a published email (reps + ticked staff), subject/body
  percent-encoded. Past ~1,800 characters the composer degrades
  honestly — first recipient only, then first recipient without the
  body — and says why; a copy-text box is always present. Recipients
  without a published email are listed separately with an official
  contact route (form assist) — never silently dropped. **No
  server-side sending in v1.**

**What is stubbed**

- **Federal delegation** is not in the database: the lookup page links
  the official house.gov / senate.gov finders instead of naming anyone.
- **Staff data population** — the table and UI exist; the verified
  staff ingestion that fills them is future work.
- **The pattern-email lane (SPEC 7.3 lane 2) is NOT in v1 sends** —
  the mailto uses published emails only. Form assist links point at
  the official legislature site where the registry has one (NC today)
  and at USA.gov's official finder elsewhere, pending the SPEC 7.2
  50-state registry.

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
bill_classifications, votes (lean: roll-call summaries), pipeline_runs,
staff_contacts (Act Box staff layer — verified LC/LD/staff rows only,
starts empty).
`legitrack.store.PostgresStore` applies it on construction and maps
the ingestion interface onto the normalised tables (jurisdictions
resolved from `state_abbr`, bills keyed on `legiscan_bill_id`, detail
payloads in the `bills.detail` JSONB column, classifications linked by
internal bill PK with both category id and label). The local run uses
the SQLite mirror in the same module, selected by
`legitrack.store.open_store()` whenever `DATABASE_URL` is unset, with
the same upsert keys. Columns were added to schema.sql for this
parity (each commented in the DDL): `bills.detail`,
`bill_classifications.category_name`, `legislators.role_title` +
`legislators.state_abbr`, and — for the stance engine —
`bill_classifications.stance` + `bill_classifications.stance_terms`.

## Status ladder

LegiScan codes: 0 Not introduced, 1 Introduced, 2 Engrossed,
3 Enrolled, 4 Passed, 5 Vetoed, 6 Failed. `legitrack/status.py` maps
them onto the public ladder Introduced → Committee → Passed chamber →
Passed both → Signed/Vetoed → Dead — conservatively: **the status code
carries no committee stage**, so code 1 stays "Introduced" rather than
guessing "Committee", and unknown codes map to "Unknown", never a guess.

## Friendliness Score & stance (v1)

The map colours each state by a **Friendliness Score**:

    score = 100 × supportive ÷ (supportive + harmful)

counted over **distinct tagged bills**. `mixed` and `unknown` bills
are excluded from the formula entirely, and a state needs **at least
5 tagged (supportive or harmful) bills** before it gets a score at
all — below that it shows as unscored rather than wearing a colour
computed from one or two bills.

Every classification row carries a stance tag from
`legitrack.stance` (rules v1, `stance.json` — the signal lists are
inspectable without reading code). Signal terms are matched against
the bill's **title + subjects**, plus that classification row's own
matched terms, and the terms that fired are stored in
`bill_classifications.stance_terms` as
`{"supportive": [...], "harmful": [...]}` — the same auditability
pattern as the taxonomy. Verdicts: only supportive signals →
`supportive`; only harmful → `harmful`; both → `mixed`; neither →
`unknown`, never a guess. Stance is per classification row, not per
bill, and there are deliberately **no category→stance defaults** — a
voting bill can cut either way, so the signals decide. Matching uses
span containment, so the supportive phrase "ban the box" is not
poisoned by the harmful term "ban" inside it, and bare "equity" is
intentionally not a supportive signal (bills banning diversity
programs name "Diversity, Equity and Inclusion" in their titles).

**Limits (v1):** the pass reads titles and subjects only — no bill
full text, no model judgement. A bill whose direction lives only in
its text reads `unknown` until the Phase 2 full-text pass lands, and
naive terms cut both ways ("repeal" scores harmful even when the
repealed law was itself discriminatory). Treat v1 scores as a
transparent first draft a reviewer can audit row by row, not as
ground truth. Existing local rows were tagged with
`scripts/backfill_stance.py`; new ingestion runs tag automatically.
First backfill (NC, 2026-11-13): 20 classification rows on 14 bills →
2 supportive rows (1 bill, the Fair Maps Act), 0 harmful, 0 mixed,
18 unknown — NC is below the 5-bill threshold and therefore unscored
for now, which is the honest result for 14 mostly-local bills.

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
- Rules v1 classification: keyword/subject matching, plus the
  rules-based stance v1 above (title/subject signals only). No
  summaries, no model pass (Phase 2). Taxonomy is the SPEC 6.2
  draft, pending Jared's sign-off.
- Web UI exists now (see "Web app" + "Deploy to Render" above) as a
  token-skinned placeholder ahead of the Figma design pass; locally it
  serves the real NC data. Act Box v1 exists as a module (see "Act Box
  (v1)" above) pending its one-line router wiring into the app; no
  prediction/copycat/scorecard features yet (Phases 2–4 in the spec).
  Votes are roll-call summaries only.
- Committee rung of the status ladder needs history/progress events from
  bill detail — designed, not implemented.
