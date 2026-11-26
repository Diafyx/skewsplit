# Benchmark

Does splitting a JDBC read by the column's distribution beat the usual
alternatives on an indexed, skewed column? This benchmark reads the same
table into Spark with skewsplit's predicates and without them. The results
are in [RESULTS.md](RESULTS.md), and the README's headline comes from the
same run.

## The table

[`skewed_table.sql`](skewed_table.sql) builds `bench_orders`, 15 million rows
by default. `setseed(0.42)` and a fixed reference time make the data the same
on every run:

- `customer_id bigint`: Zipf-like. About 4% of the rows have customer 1, and
  the lowest 1% of the key range holds 71% of the rows.
- `created_at timestamp`: 60% of the rows fall in the last 7 days of a span of
  about 3 years.

Each column has 1% NULLs and a B-tree index, and the script ends with
`VACUUM ANALYZE`. It never drops a table: it fails if `bench_orders` already
exists.

## The methods

[`run.py`](run.py) reads the whole table into Spark's noop sink, split on each
column in four ways:

| Method | Predicates |
|--------|------------|
| default bounds | Spark's `partitionColumn` with `lowerBound` and `upperBound` from `min` and `max`: equal-width ranges, NULLs in the first |
| hash-mod | `col IS NULL`, then `((hashtextextended(col::text, 0) % N) + N) % N = i`, written by hand; every predicate scans the whole table |
| skewsplit statistics | `skewsplit.postgres.plan(..., strategy="statistics")`: ranges from `pg_stats`, without reading the table |
| skewsplit sample | `skewsplit.postgres.plan(..., strategy="sample", seed=1)`: ranges from a sample of about 30000 rows |

[`plan.py`](plan.py) plans skewsplit's predicates with the package itself,
in the project environment, before Spark starts, and writes each plan as
`skewsplit plan --format json` would. The plan's own `planning_ms` is the
planning time reported for these methods. For the other methods, it is the
time of the `min` and `max` query, or nothing.

Every read uses `fetchsize` 10000. The methods run in turn, three times each,
and the report shows medians. It records:

- the planning time;
- the longest task and the whole job, from Spark's status API;
- the rows each task read. A run whose tasks did not read exactly the table's
  row count fails;
- Spark's event log, from which [`bench_report.py`](bench_report.py) draws
  `tasks.svg`: each task's duration, for the median run of each method. It
  checks that the event log has as many tasks as the status API reported.

## Reproducing it

- The runner is GitHub-hosted `ubuntu-latest` with 4 CPUs and 16 GB. Spark
  runs in local mode on it, with PostgreSQL 18 as a service container, so
  the database and Spark share the machine. RESULTS.md names the runner
  image and its processor, and the Spark, Java, Python and PostgreSQL
  versions.
- The data comes from `setseed(0.42)`, and the sample from `--seed 1`, so both
  are the same on every run. `ANALYZE` takes a random sample of its own, so
  the statistics strategy's plan can differ a little between runs.
- Shared runners vary from run to run. Compare the medians, and the share of
  rows in the largest task, which does not depend on the runner.

## Running it

Run it from the Actions tab: **Bench**, then **Run workflow**. Pull requests
that change `bench/` run it too. It runs `make bench`. Locally, `make bench`
stops with a message, as the other database targets do.

The job summary shows the results, and the `bench-results` artifact holds
`bench.json`, `bench/RESULTS.md`, `bench/headline.md`, `bench/tasks.svg`,
skewsplit's plans and Spark's event log. To update the published results:

1. Copy `RESULTS.md` and `tasks.svg` here.
2. Replace the README's headline with `headline.md`.
3. Write the new baseline:
   `python bench/bench_compare.py --baseline bench.json bench/baseline.json`.

## Nightly

`nightly.yml` runs the benchmark every night and compares skewsplit's median
max task time with [`baseline.json`](baseline.json), written from the headline's run.
Runners differ in speed, so seconds from two runs do not compare: it compares
skewsplit's time as a share of the default bounds' in the same run, a read
that does not use skewsplit. When that share moves by more than 10% either
way, `bench_compare.py` writes the issue, and
`scripts/tracking_issue.py` opens it or comments on the one already open.
