# 3. Bulk insert through Arrow batches; native TIMESTAMP column

Status: accepted

## Context

Parameterized multi-row `INSERT … VALUES (?, …)` cost about 1.2 ms per row in pure parameter
binding, independent of batch size — around 20 minutes of CPU for a million rows. Timestamps were
stored as text, which prevented DuckDB from skipping row groups for time-range filters.

## Decision

Parsed rows are inserted in batches of 20,000 by registering a pyarrow table and running
`INSERT INTO logs SELECT … FROM <batch>`. `logs.timestamp` is a native `TIMESTAMP`.

## Consequences

- Inserting costs about 0.0015 ms per row (~800× faster), and memory is bounded by one batch.
- Time-range filters benefit from DuckDB's min/max statistics per row group, most when rows are
  stored roughly in time order.
- pyarrow is the largest dependency of the image (~120 MB). Replacing it needs a bulk path of
  comparable speed, measured first.
