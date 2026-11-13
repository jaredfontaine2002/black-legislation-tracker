# LegiScan Proof Pull — North Carolina — 2026-11-13

Key validated live (key stored by Jared, not recorded here). API requires a browser User-Agent — default Python UA gets 403 from Cloudflare; with UA header calls return 200.

## Calls made (5 queries total)
1. `getSessionList&state=NC` — OK
2. `getMasterList&id=2189` — OK (note: parameter is `id`, not `session`)
3. `getMasterList&id=2274` — OK
4. `getBill&id=1945804` — OK

## Findings
- NC sessions returned include:
  - 2189 — 2025-2026 Regular Session — **2,338 bills** in master list
  - 2274 — 2026 1st Special Session — 5 bills
  - 2032 — 2023-2024 Regular, 1814 — 2021-2022 Regular, etc.
  Sessions carry `dataset_hash` / `session_hash`.
- Master list fields per bill: `bill_id, number, change_hash, status, status_date, title, description, last_action, last_action_date, url`
  - `change_hash` present on every bill — change-detection design in SPEC §3/§4 validated.
- Bill detail (sample NC H2, bill_id 1945804) keys: `amendments, bill_number, bill_type, body, calendar, change_hash, committee, completed, history, progress, referrals, sasts, session, sponsors, subjects, supplements, texts, votes` + title/description/status/state_link/url
  - Counts for H2: sponsors 22, texts 3, votes 1, history 16, subjects 9
  - Sponsors include people_id, party, role, district, votesmart/ballotpedia IDs, and a bio block with capitol/district phone and email where published.
  - Texts include doc_id, type (Introduced etc.), mime, LegiScan URL, official state_link (ncleg.gov PDF), text_size, and `text_hash` — supports amendment diffing in Phase 2.
- Status is numeric (e.g. 2, 4) — will need the LegiScan status code table for the SPEC §6 Phase 1 status ladder normalisation.

## Verdict
LegiScan fields match the SPEC data model. Safe to write the full schema and Phase 1 ingestion around: sessions → master list (change_hash delta) → bill detail only for changed bills.

Next: OpenStates key (cross-check + legislator contacts), then schema + ingestion scaffold.
