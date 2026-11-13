-- Black Legislation Tracker -- Phase 1 Postgres schema (SPEC section 5)
-- Idempotent: safe to re-run. Target database for the Render deployment;
-- the Phase 1 local run uses the lean SQLite mirror in legitrack.store.
-- No credentials or API keys belong in this file.

CREATE TABLE IF NOT EXISTS jurisdictions (
    id                  BIGSERIAL PRIMARY KEY,
    state_abbr          TEXT NOT NULL UNIQUE,          -- 'NC', 'US' for Congress
    name                TEXT NOT NULL,                 -- 'North Carolina'
    classification      TEXT NOT NULL DEFAULT 'state', -- state / federal / territory
    legiscan_state_id   INTEGER,
    openstates_id       TEXT UNIQUE,                   -- ocd-jurisdiction/...
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id                  BIGSERIAL PRIMARY KEY,
    jurisdiction_id     BIGINT NOT NULL REFERENCES jurisdictions(id),
    legiscan_session_id INTEGER UNIQUE,                -- e.g. 2189 = NC 2025-2026 Regular
    openstates_session  TEXT,                          -- e.g. '2025'
    year_start          INTEGER,
    year_end            INTEGER,
    session_name        TEXT,
    session_title       TEXT,
    session_tag         TEXT,
    is_special          BOOLEAN NOT NULL DEFAULT FALSE,
    sine_die            BOOLEAN NOT NULL DEFAULT FALSE,
    dataset_hash        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sessions_jurisdiction ON sessions(jurisdiction_id);

CREATE TABLE IF NOT EXISTS legislators (
    id                    BIGSERIAL PRIMARY KEY,
    jurisdiction_id       BIGINT REFERENCES jurisdictions(id),
    legiscan_people_id    INTEGER UNIQUE,              -- LegiScan people_id
    openstates_person_id  TEXT UNIQUE,                 -- ocd-person/...
    name                  TEXT NOT NULL,
    first_name            TEXT,
    last_name             TEXT,
    party                 TEXT,
    chamber               TEXT,                        -- lower / upper / legislature
    role_title            TEXT,                        -- OpenStates current_role.title (added for PostgresStore parity with the SQLite mirror / web cards)
    district              TEXT,
    photo_url             TEXT,                        -- sourced URL only, never generated
    email_published       TEXT,                        -- published address only (Act Box lane 1)
    state_abbr            TEXT,                        -- denormalised state (added for PostgresStore parity: legislator_count(state) + web cards without a join)
    source                TEXT,                        -- which feed supplied the row
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_legislators_jurisdiction ON legislators(jurisdiction_id);

CREATE TABLE IF NOT EXISTS bills (
    id                  BIGSERIAL PRIMARY KEY,
    legiscan_bill_id    BIGINT NOT NULL UNIQUE,        -- source ID, upsert key
    openstates_bill_id  TEXT,                          -- ocd-bill/... when cross-matched
    session_id          BIGINT REFERENCES sessions(id),
    jurisdiction_id     BIGINT REFERENCES jurisdictions(id),
    sponsor_legislator_id BIGINT REFERENCES legislators(id),
    bill_number         TEXT NOT NULL,                 -- 'H2', 'HB 105'
    title               TEXT,
    description         TEXT,
    status_code         INTEGER,                       -- raw LegiScan numeric status (see legitrack.status)
    public_status       TEXT,                          -- normalised SPEC ladder label
    status_date         DATE,
    last_action         TEXT,
    last_action_date    DATE,
    change_hash         TEXT NOT NULL,                 -- delta-pull primitive; detail fetched only when it changes
    legiscan_url        TEXT,
    state_link          TEXT,                          -- official state bill page
    raw                 JSONB,                         -- last master-list row as received
    detail              JSONB,                         -- last getBill detail payload (added for PostgresStore parity: SQLite mirror stores this as bills.detail_json)
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, bill_number)
);
CREATE INDEX IF NOT EXISTS idx_bills_session ON bills(session_id);
CREATE INDEX IF NOT EXISTS idx_bills_public_status ON bills(public_status);
CREATE INDEX IF NOT EXISTS idx_bills_change_hash ON bills(change_hash);

CREATE TABLE IF NOT EXISTS bill_versions (
    id                  BIGSERIAL PRIMARY KEY,
    bill_id             BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    legiscan_doc_id     BIGINT,                        -- LegiScan texts[].doc_id
    version_type        TEXT,                          -- 'Introduced', 'Engrossed', ...
    text_url            TEXT,                          -- LegiScan text URL
    state_link          TEXT,                          -- official state text URL
    mime                TEXT,
    text_size           INTEGER,
    text_hash           TEXT,                          -- LegiScan texts[].text_hash; drives Phase 2 diffing
    fetched_text        TEXT,                          -- NULL until the Phase 2 text fetch runs
    version_date        DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bill_id, legiscan_doc_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_versions_bill ON bill_versions(bill_id);

CREATE TABLE IF NOT EXISTS bill_classifications (
    id                  BIGSERIAL PRIMARY KEY,
    bill_id             BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    category            TEXT NOT NULL,                 -- taxonomy category id (legitrack.taxonomy)
    category_name       TEXT,                          -- human label (added for PostgresStore parity: SQLite mirror + web pages display the label, not the id)
    direction           TEXT,                          -- expands / restricts / funds / studies / NULL in rules v1
    summary             TEXT,                          -- plain-English summary; NULL in rules v1 (Phase 2 model pass)
    confidence          NUMERIC(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    method              TEXT NOT NULL DEFAULT 'rule',  -- rule / model / human
    reviewer_status     TEXT NOT NULL DEFAULT 'pending',
    matched_terms       JSONB,                         -- terms/subjects that fired, for auditability
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bill_id, category, method)
);
CREATE INDEX IF NOT EXISTS idx_classifications_bill ON bill_classifications(bill_id);
CREATE INDEX IF NOT EXISTS idx_classifications_category ON bill_classifications(category);

-- Lean in Phase 1: roll-call summaries only. Per-legislator positions are
-- added by the Phase 2 scorecard work when getRollCall is wired in.
CREATE TABLE IF NOT EXISTS votes (
    id                  BIGSERIAL PRIMARY KEY,
    bill_id             BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    legiscan_roll_call_id BIGINT UNIQUE,               -- LegiScan votes[].roll_call_id
    chamber             TEXT,
    vote_date           DATE,
    motion              TEXT,
    result              TEXT,                          -- pass / fail as reported by source
    yeas                INTEGER,
    nays                INTEGER,
    not_voting          INTEGER,
    absent              INTEGER,
    source_url          TEXT,
    raw                 JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_votes_bill ON votes(bill_id);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                  BIGSERIAL PRIMARY KEY,
    job                 TEXT NOT NULL,                 -- 'legiscan_ingest'
    state_abbr          TEXT,
    legiscan_session_id INTEGER,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running', -- running / ok / error / dry_run
    bills_seen          INTEGER NOT NULL DEFAULT 0,
    bills_changed       INTEGER NOT NULL DEFAULT 0,
    bills_fetched       INTEGER NOT NULL DEFAULT 0,
    queries_used        INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    details             JSONB
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at DESC);

-- Parity upgrades for databases created from an earlier revision of this
-- file (the columns above were added when the Postgres backend landed so
-- PostgresStore matches the SQLite mirror's interface). Idempotent.
ALTER TABLE bills ADD COLUMN IF NOT EXISTS detail JSONB;
ALTER TABLE bill_classifications ADD COLUMN IF NOT EXISTS category_name TEXT;
ALTER TABLE legislators ADD COLUMN IF NOT EXISTS role_title TEXT;
ALTER TABLE legislators ADD COLUMN IF NOT EXISTS state_abbr TEXT;
