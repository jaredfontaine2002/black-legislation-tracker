"""LegiTrack -- Black Legislation Tracker, Phase 1 engine package.

Modules:
    legiscan_client   LegiScan API (primary feed), key from environment only
    openstates_client OpenStates API v3 (cross-check / legislators)
    status            LegiScan status codes -> public status ladder
    taxonomy          rules-based classification v1 (SPEC 6.2)
    store             lean SQLite local store (Postgres target: schema.sql)
    ingest            sessions -> master list -> change-hash delta -> detail
"""

__version__ = "0.1.0"
