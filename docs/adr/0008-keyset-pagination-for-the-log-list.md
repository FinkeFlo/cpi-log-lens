# 8. Keyset pagination for the log list

Status: accepted

## Context

The Browse list shows millions of entries, newest first. With LIMIT/OFFSET, DuckDB has to sort and
skip every entry before the page: on 20 million synthetic entries, the page at offset 100,000 took
about 0.6 s with a level filter (0.7 s at offset 1,000,000), against about 25 ms for the first
page. Every page also counted all matching entries. CPI timestamps have whole seconds, so many
entries share a timestamp, and sorting by timestamp alone left their order open.

## Decision

- The list is ordered by (timestamp, id), newest first; the id breaks ties.
- Pages are selected by a cursor on that key instead of an offset. Every answer of `GET /api/logs`
  carries `older_cursor` and `newer_cursor`; `at` starts the list at a time. The condition
  `timestamp <= ? AND (timestamp < ? OR id < ?)` repeats the plain timestamp bound so DuckDB can
  skip row groups by their min/max timestamp. With the LIMIT, its top-N then reads only a few row
  groups. No secondary index is added (ADR 2 still holds): without one, a page took 5-25 ms at
  any depth on 20 million synthetic entries, both with rows in time order and with rows in the
  order of their files.
- Counting is optional (`count=false`). The UI counts on the first page, after a jump and on
  refreshes; for a step to the next page it derives the range from the page before. When it
  counts, the count runs next to the page query on a second read cursor.
- The cursor text (kind, compact timestamp, id) is part of shared Browse links and stays stable.
  Page numbers stay available in the API for existing scripts.

## Consequences

- Paging costs the same at any depth. A text search is the exception to "a few milliseconds":
  each page evaluates ILIKE over the whole time window (24 hours by default), about 30-60 ms for
  670,000 entries in that window.
- A page's position (`offset`) needs a count. After a step without one it is derived, so while
  entries are imported it can be off by the new entries until the next refresh.
- The list stays ordered by time, not by import: entries imported later with timestamps inside a
  page's range appear in that page when it is loaded again.
