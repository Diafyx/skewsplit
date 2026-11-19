# Benchmark

Does splitting a JDBC read by the column's distribution beat the usual
alternatives on an indexed, skewed column? This spike measures it before the
PostgreSQL adapter is built. The results are in [RESULTS.md](RESULTS.md).

## The table

[`skewed_table.sql`](skewed_table.sql) builds `bench_orders`, 15 million rows
by default, with the same data on every run:

- `customer_id bigint`: Zipf-like. About 4% of the rows have customer 1, and
  the lowest 1% of the key range holds 71% of the rows.
- `created_at timestamp`: 60% of the rows fall in the last 7 days of a span of
  about 3 years.

Each column has 1% NULLs and a B-tree index. The script never drops a table: it
fails if `bench_orders` already exists.

## The methods

[`run.py`](run.py) reads the whole table into Spark's noop sink, split on each
column in three ways:

| Method | Predicates |
|--------|------------|
| default bounds | Spark's `partitionColumn` with `lowerBound` and `upperBound` from `min` and `max`: equal-width ranges, NULLs in the first |
| hash-mod | `col IS NULL`, then `((hashtextextended(col::text, 0) % N) + N) % N = i`; every predicate scans the whole table |
| quantile ranges | `col IS NULL`, then half-open ranges between the `percentile_disc` quantiles computed in SQL |

Every read uses `fetchsize` 10000. The methods run in turn, three times each,
and the report shows medians. It records:

- the time of the planning query (`min` and `max`, or the quantiles);
- the longest task and the whole job, from Spark's status API;
- the rows each task read. A run whose tasks did not read exactly the table's
  row count fails.

## Running it

Run it from the Actions tab: **Bench**, then **Run workflow**. Pull requests
that change `bench/` run it too. It runs `make bench` on a GitHub-hosted runner
with PostgreSQL 18 as a service container. Locally, `make bench` stops with a
message, as the other database targets do.

The job summary shows the report, and the `bench-results` artifact holds
`RESULTS.md` and the raw `bench.json`. To update the results, copy that
`RESULTS.md` here.
