-- History of fetch jobs. The old fetch_runs table was never written to, so it is
-- replaced instead of migrated.
DROP TABLE IF EXISTS fetch_runs;
DROP SEQUENCE IF EXISTS fetch_runs_id_seq;

CREATE TABLE fetch_runs (
    id            TEXT PRIMARY KEY,           -- the job id
    trigger       TEXT NOT NULL,              -- manual | demo | schedule:<schedule id>
    params        TEXT NOT NULL,              -- JSON: tenants, log_types, hours
    status        TEXT NOT NULL,              -- running | done | error | cancelled | interrupted
    started_at    TIMESTAMP NOT NULL,         -- UTC
    finished_at   TIMESTAMP,                  -- UTC
    files_total   INTEGER NOT NULL DEFAULT 0,
    files_done    INTEGER NOT NULL DEFAULT 0,
    rows_imported BIGINT NOT NULL DEFAULT 0,
    warnings      INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    error         TEXT                        -- final error, or the last per-tenant error
);
