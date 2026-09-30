# 5. ZSTD-compressed log texts in storage version v1.5.0

Status: accepted

## Context

`message` and `raw_line` make up most of the database. DuckDB compresses such strings with FSST
by default, which roughly halves them. ZSTD compresses repetitive log text much better, but DuckDB
only uses it in files with storage version v1.2.0 or newer; in older files a column declared ZSTD
silently stays *uncompressed* and grows. The storage version of a file is set when it is created
and cannot be changed in place, and a file in a newer storage version cannot be opened by older
DuckDB releases.

Measured on a synthetic database with 20M log rows (DuckDB 1.5.6, 4 threads, warm cache): 7.2 GB →
1.7 GB (4.3×), converted in about 20 s; list pages and filters stay around 10 ms, full-text
searches over all rows became 3–25 % slower. Real logs are more varied; an overall reduction of
2.5–4× is expected.

## Decision

- New database files are created in storage version v1.5.0, and migration 0003 declares
  `USING COMPRESSION zstd` for `logs.message` and `logs.raw_line` there.
- Existing files keep their format (migration 0003 leaves them alone) until the user converts them
  explicitly: `DB_STORAGE_UPGRADE=true` on start or `python -m app.storage`. The conversion copies
  the schema with `COPY FROM DATABASE (SCHEMA)`, recreates `logs` with the compressed columns and
  its id sequence past the highest id, copies every table (log rows sorted by time, which helps
  zone-map pruning on time filters), verifies row counts and the compression of the new file, then
  swaps the files and keeps the old one as `<name>.bak-<time>`.
- The conversion is not automatic because it needs free disk space of about the file size, takes
  minutes on large files, and cannot be undone except by restoring the old file.

## Consequences

- New installations get the smaller files without any action.
- Converted and new files need DuckDB ≥ 1.5 (the app pins it); older DuckDB tools cannot open them.
- The compression is checked with `pragma_storage_info` after the conversion, so the silent
  fallback to uncompressed storage cannot go unnoticed.
