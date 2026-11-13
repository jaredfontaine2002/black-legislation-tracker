# First Live Run — North Carolina — 2026-11-13

Run from the packaged code in `app/` (not proof scripts). Keys supplied by Jared in the build chat, passed via environment only, not stored in any file.

## Bug found and fixed before the run
`pick_session` originally preferred non-special sessions by the API's `special` flag. Live data showed NC's 2026 extra session carries `special=0` despite a "1st Special Session" tag — the picker chose the 5-bill extra session. Fixed to prefer the `session_tag` ("Regular Session"), regression test added. Suite: 49 tests passing after the fix + legislator tests.

## LegiScan ingestion (legitrack.ingest --state NC --limit 25)
- Dry run: session 2189 (2025-2026), 2,338 seen / 2,338 changed, 2 queries.
- Real run: 2,338 master bills upserted, **25 detail fetches, 27 queries total**, run row status ok.
- Verification dry run immediately after: **0 changed, 2 queries** — change_hash delta confirmed live.
- Status ladder across all 2,338: Introduced 1,837 · Passed chamber 293 · Signed 202 · Vetoed 6.
- Rules-v1 classifications on the 25 detailed bills: 20 classifications (education 9, voting access 4, small business 2, criminal justice 2, redistricting 1, health equity 1, discrimination enforcement 1).

## OpenStates legislator ingestion (legitrack.legislators --state NC)
- First attempt at per_page=100 timed out at 30s (server-side slowness, recorded as an error run — honest log). Retried at per_page=20: **173 seen / 173 stored, 9 queries**.
- Of the 173: 172 have a published email, 171 a photo URL.

## Known limitation (must fix before full backfill)
A `--limit` run upserts master hashes for all bills before fetching detail for the limited set, so the remaining 2,313 bills show as "unchanged" next run despite having no detail stored. The daily steady-state flow is unaffected (new changes arrive with new hashes), but a full backfill needs a separate pass fetching detail where `detail_json IS NULL`, ignoring hash state. Logged here, not hidden.

## Local data
SQLite store at `app/legitrack.db` (2,338 bills, 25 details, 20 classifications, 173 legislators, 3 pipeline run rows incl. the timed-out attempt).
