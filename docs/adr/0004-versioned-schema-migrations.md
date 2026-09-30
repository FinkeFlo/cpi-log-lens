# 4. Versioned schema migrations without a migration framework

Status: accepted

## Context

The schema used to be created with `CREATE … IF NOT EXISTS` plus ad-hoc checks on every start that
detected older layouts (a UNIQUE constraint, TEXT timestamps, a missing column) and fixed them.
Every schema change needed a new detection heuristic, nobody could tell which schema a file had,
and a file written by a newer app version was opened as if nothing was wrong.

## Decision

A `schema_version` table and numbered migration files in `backend/app/migrations/`
(`vNNNN_name.sql`, or `vNNNN_name.py` with `upgrade(conn)`), applied in order by a small runner when
the database is opened. Each migration runs in one transaction together with its `schema_version`
row (DuckDB DDL is transactional). Migration 0001 is the former schema (idempotent), 0002 the former
ad-hoc checks, so files from before this change reach the baseline the same way new files do. A
database with a version above the newest known migration stops the start with a clear message.

Alembic was not used: it needs SQLAlchemy and the third-party `duckdb-engine` dialect, and its main
feature, autogenerate, requires ORM models the app does not have. yoyo-migrations has no DuckDB
backend.

## Consequences

- One file per schema change, reviewed like code; the naming follows the common Flyway convention.
- A failed migration leaves no partial state and runs again on the next start.
- Downgrades are not supported: going back means restoring a backup taken before the upgrade.
- Migrations that rewrite large tables make the start take longer; their release notes must say so.
