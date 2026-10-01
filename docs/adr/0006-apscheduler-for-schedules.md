# 6. APScheduler 3 for fetch schedules and retention

Status: accepted

## Context

Scheduled fetches and the automatic retention ran in two hand-written loops. The schedule loop
polled every minute, started at most one schedule per tick, set `last_run_at` before the run (a
failed run counted as done), and dropped a due schedule when a fetch was already running until the
next tick. The retention loop slept a whole interval (24 h by default) when it found a fetch
running. Neither was testable without running the loop.

## Decision

Use APScheduler 3.11 (`AsyncIOScheduler`, in-memory job store) inside `ScheduleService`:

- Schedules stay in `fetch_schedules`; each enabled one becomes an interval job, first run at
  `next_run(last_run_at, interval)` (a pure, tested function), rebuilt after every change.
- A job starts its fetch through `FetchService` (shared lock with manual starts). If a fetch is
  running, the job tries again one minute later. `last_run_at` is set only when the run succeeded.
- Retention is an interval job that runs at start; with a fetch running it retries after five
  minutes.

APScheduler 4 is a pre-release with breaking changes and needs SQLAlchemy-backed data stores for
persistence; the 3.x line is stable and needs no persistence here.

## Consequences

- One small pure-Python dependency; scheduling behaviour is covered by tests with shortened
  minutes.
- `SCHEDULE_CHECK_SECONDS` is gone: schedules fire at their due time instead of being polled.
