# OpenStates Proof Pull — North Carolina — 2026-11-13

Key validated live (key held by Jared, not recorded here). Auth: `X-API-KEY` header to `https://v3.openstates.org`.

## Calls made (3 queries)
1. `GET /jurisdictions` — 200
2. `GET /people?jurisdiction=ocd-jurisdiction/country:us/state:nc/government` — 200
3. `GET /bills?jurisdiction=...nc...&session=2025` — 200

## Findings
- NC people total: **173** — each record includes `id (ocd-person/…)`, name, party, `current_role` (title, chamber classification, district, division_id), **published email** (sample: a sitting NC representative @ncleg.gov), and **photo image URL** from ncleg.gov. This covers the SPEC legislator/contact layer and part of the Act Box verified-email lane.
- NC bills total for session 2025: **2,338** — exactly matching the LegiScan master list count for NC session 2189 (2025-2026 Regular). Strong cross-source confirmation that both feeds describe the same universe.
- Bill fields: `id (ocd-bill/…)`, identifier (e.g. HB 105), title, session, jurisdiction, classification, first/latest action dates + descriptions, latest passage date, subject (can be null — do not rely on OpenStates subjects alone), openstates_url.
- People fields also include given/family name, gender, birth_date, extras, created/updated timestamps.

## Verdict
OpenStates works as designed in SPEC §3: cross-check counts + legislator profiles/contacts/photos. LegiScan stays primary for change_hash delta pulls and bill text; OpenStates fills legislators and published emails.

Both keys now validated. Next build step (no Jared input needed): Postgres schema from SPEC §5 + Phase 1 ingestion scaffold (sessions → master list → changed-bill detail), plus status-code normalisation table.
