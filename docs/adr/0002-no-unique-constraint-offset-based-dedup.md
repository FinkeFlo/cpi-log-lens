# 2. No UNIQUE constraint on logs; duplicate protection by file offset

Status: accepted

## Context

Log files on CPI grow over the day and are fetched repeatedly. An earlier version used a UNIQUE
constraint over (tenant, log_type, filename, timestamp, level, logger, message). Enforcing it
requires an index whose per-row checks slowed imports progressively. At about 6M rows imports
dropped from ~9.5 to ~3 files per hour at full CPU. It also dropped legitimate repeated lines
(heartbeats with identical text within the same second).

## Decision

No UNIQUE constraint and no secondary indexes on `logs`. For every imported file, `file_imports`
records how many parsed rows (after merging continuation lines) have been imported. A re-fetch
parses the file again and inserts only the rows past that offset. Rows and the new offset are
committed in the same transaction per batch.

## Consequences

- Imports stay fast regardless of table size, and repeated identical lines are kept.
- Correctness depends on files only growing at the end. If a multi-line message is still being
  written during a fetch, later continuation lines of that message are not picked up.
- Anything that inserts into `logs` outside this path must maintain `file_imports` itself.
