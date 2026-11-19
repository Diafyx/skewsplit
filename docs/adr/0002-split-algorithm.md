# 2. Split algorithm: statistics first, half-open ranges, isolated dominant values

- Status: accepted
- Date: 2026-11-18

## Context

Two open-source engines already split a table into ranges of a key column for
parallel reads: the incremental snapshot of
[Flink CDC](https://github.com/apache/flink-cdc/blob/8d3ccc8483a01d877dd39d22fd961a3d82b7d3ec/flink-cdc-connect/flink-cdc-source-connectors/flink-cdc-base/src/main/java/org/apache/flink/cdc/connectors/base/source/assigner/splitter/JdbcSourceChunkSplitter.java)
and the JDBC source of
[Apache SeaTunnel](https://github.com/apache/seatunnel/blob/f1ab36f0423c8aa17766959dfd6c6b3f5bc1426a/seatunnel-connectors-v2/connector-jdbc/src/main/java/org/apache/seatunnel/connectors/seatunnel/jdbc/source/DynamicChunkSplitter.java),
whose splitter derives from the same design. We read both before writing
[the spec](../../spec/SPEC.md). In short, as of the linked commits:

- Both start with `MIN` and `MAX` of the key and an approximate row count. If
  `(max - min + 1) / rows` falls between two configured bounds, the key is
  treated as evenly distributed and cut into fixed-width chunks.
- Otherwise Flink CDC walks the key with one query per chunk
  (`SELECT MAX(key) FROM (SELECT key ... WHERE key >= ? ORDER BY key LIMIT n)`).
  When the next chunk end equals the current start, it moves to the next larger
  value, so all rows of one repeated value end up in a single chunk.
- SeaTunnel can instead sample. Its default implementation runs
  `SELECT key FROM table`, streams every row to the client and keeps every
  n-th value, then cuts at the sampled values. A
  [SeaTunnel write-up](https://dev.to/seatunnel/uneven-primary-key-distribution-causing-multi-day-seatunnel-cdc-sync-lag-heres-the-optimization-4a7j)
  describes the same sampling in its MySQL CDC connector reading nothing for
  6 hours on a table of 60 to 70 million rows. The open
  [issue 10288](https://github.com/apache/seatunnel/issues/10288) proposes
  sampling inside the database instead.
- Chunks are half-open ranges: the first is `key < s1` (written as
  `key <= s1 AND NOT (key = s1)`), the middle ones `key >= si AND key < si+1`,
  the last `key >= sk`. When there is more than one chunk, no chunk matches a
  row whose key is NULL. SeaTunnel no longer picks a nullable column
  automatically for this reason, but a column the user names is used as is.

## Decision

### Taken from these engines

- Half-open ranges with an open first and last range. Every non-NULL value,
  including values outside the range seen when planning, falls into exactly
  one range.
- Equal boundaries are merged, so no two ranges share a split point.
- The expensive path has a cap. Our statistics strategy reads no table rows at
  all; the later sampling strategy (python v0.2.0) has a row limit.

### Avoided

- Streaming a whole column to the client to compute quantiles. The first
  strategy reads `pg_stats`: the most common values with their frequencies and
  an equi-depth histogram, already kept by `ANALYZE`.
- Dropping NULL rows. Every plan has a `col IS NULL` predicate, even when the
  statistics show no NULLs.
- Leaving a dominant value as one oversized chunk with no notice. A value that
  holds at least one partition's share of the rows gets its own `col = v`
  predicate and a warning that this column cannot split it further.
- Row estimates used as hidden switches. Every decision that depends on a
  threshold is reported as a warning, and the report carries the estimated
  rows of every predicate and a skew score.

### Our own choices

- One input shape for every strategy: NULL rows, most common values with row
  counts, and histogram bounds with the rows they cover. The planner never
  talks to a database.
- Every supported type maps to an integer ordinal, and all arithmetic is on
  integers with fixed rounding. Implementations in different languages give
  byte-identical output, checked by shared fixtures.
- The partition count is a target the plan never exceeds. When there are
  fewer distinct values than partitions, the plan has fewer predicates and a
  warning says so.

## Consequences

- Plans are only as good as the statistics. The report says where the
  estimate came from, and verify mode counts the actual rows.
- A range next to a dominant value can be empty, for example `col < v` when
  `v` is the smallest value. It stays, because a later row could fall into it,
  and it costs one index probe.
- A dominant value cannot be split on its own column. Splitting it needs a
  second column, which is out of scope for now.
- Columns without an ordinal mapping (text, UUID, floating point, Boolean)
  need another strategy, such as the hash fallback planned for python v0.2.0.
