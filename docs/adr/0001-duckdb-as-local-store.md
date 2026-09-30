# 1. DuckDB as the local store

Status: accepted

## Context

The app stores tens of millions of log entries on the user's machine (a few GB) and has to support
bulk imports, text search over messages, filters by tenant, level, IFlow and time, aggregations
for the Stats page, and deleting old entries. It runs as a single self-hosted container, and
configuration (tenants, schedules, settings) lives in the same store.

## Decision

Use DuckDB, embedded in the app process, with explicit resource settings (`memory_limit`,
`threads`, `checkpoint_threshold`) and version ≥ 1.5.

## Consequences

- One process, one file; backups are file copies. No database server to operate.
- Columnar storage makes scans and aggregations fast. On 20M synthetic rows, DuckDB 1.5 serves a
  filtered first page in tens of milliseconds and the Stats aggregations in well under a second.
- Searching for rare terms is a full scan (hundreds of milliseconds at 20M rows). A separate text
  index becomes worth it only at much larger volumes.
- DuckDB allows one writer at a time; the app serializes writes through a single writer thread and
  reads through a small, bounded pool of cursors.
- Compared alternatives: SQLite + FTS5 imported 4× slower, needed ~12× the disk space and was far
  slower on frequent terms and aggregations. ClickHouse was faster only for rare-term search, but
  needs a second process and a rewrite of the data layer. Neither was clearly better overall.
