# skewsplit

[![CI](https://github.com/Diafyx/skewsplit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Diafyx/skewsplit/actions/workflows/ci.yml?query=branch%3Amain)
[![Python 3.9 to 3.15](https://img.shields.io/badge/python-3.9%20to%203.15-3776ab)](#supported-versions)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/Diafyx/skewsplit/badge)](https://scorecard.dev/viewer/?uri=github.com/Diafyx/skewsplit)

skewsplit reads a database column's distribution and gives Spark balanced,
index-friendly partition predicates for its JDBC reader, together with a
report of the skew that remains.

> **Status: early development (pre-alpha).** The API can change in any 0.x
> release. The roadmap is in the
> [milestones](https://github.com/Diafyx/skewsplit/milestones).

## The problem

Spark's JDBC reader splits a table by `lowerBound`, `upperBound` and
`numPartitions` into ranges of equal *width*. Real columns are rarely uniform:
a few ranges get most of the rows, one task runs for an hour while the others
finish in seconds, and `NULL` rows end up in the first partition or are lost
to a hand-written filter. Spark's other option, a `predicates` list with one
`WHERE` clause per partition, avoids this, but someone has to write those
clauses.

## How it works

skewsplit writes those clauses. It:

- reads the column's statistics (for PostgreSQL, `pg_stats`: most common values
  and an equi-depth histogram), so planning does not scan the table, or, when
  they are missing or out of date, a sample of the table, which the database
  counts so that only one row per value reaches skewsplit, or, when accuracy
  matters more than planning time, exact quantiles of every row;
- plans ranges of roughly equal *row counts* that the database can answer from
  an index on the column, or, for a column of a type without a usable order,
  hash predicates, which read the whole table for every partition;
- gives `NULL` rows their own `col IS NULL` predicate, and gives a value that
  is too frequent to split its own `col = value` predicate, with a warning;
- reports the estimated skew of the plan, where the estimate came from and how
  fresh the statistics are;
- verifies a plan in one scan: every row is matched by exactly one predicate.

The predicates match every row exactly once whatever the data, even when the
statistics are wrong; only the balance depends on them.

**The output contains real values from the column**: split points and frequent
values. Treat plans, reports, generated code and logs like the data they
describe.

## Install

```sh
pip install "skewsplit[postgres]"
```

The `postgres` extra installs psycopg, which also needs libpq: add
`psycopg[binary]` if your system has none. Without the extra, skewsplit has no
dependencies and plans from a distribution you give it.

## Quick start

Plan a read of `public.orders` split on `customer_id`, from the column's
statistics:

```python
from skewsplit import postgres

plan = postgres.plan("host=db dbname=shop user=reader", "public.orders", "customer_id", 16)
print(plan.report)
df = spark.read.jdbc(url, "public.orders", predicates=plan.predicates, properties=properties)
```

Spark runs one task per predicate. `url` and `properties` are your JDBC URL and
connection properties, as for any `spark.read.jdbc`.

The planner itself needs no database. Here it plans 4 partitions for a column
with 100 NULL rows, the value 42 in 500 rows, the value 7 in 60, and 400 more
rows spread from 1 to 1000:

```python
>>> import skewsplit
>>> plan = skewsplit.plan(
...     {
...         "column": "customer_id",
...         "type": "int4",
...         "null_rows": 100,
...         "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
...         "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
...     },
...     4,
... )
>>> plan.predicates
['"customer_id" IS NULL', '"customer_id" < 42', '"customer_id" = 42', '"customer_id" > 42 AND "customer_id" < 520', '"customer_id" >= 520']
>>> print(plan.report)
customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887
rows  predicate
 100  "customer_id" IS NULL
  76  "customer_id" < 42
 500  "customer_id" = 42
 192  "customer_id" > 42 AND "customer_id" < 520
 192  "customer_id" >= 520
warning dominant_value: 42 holds 500 rows, at least one partition's share; it cannot be split further on this column
>>> plan.skew_score
1.887

```

The skew score is the largest predicate's rows over an even share, the total
rows divided by the partitions: 1.0 is perfect balance. Here the value 42 alone
holds almost twice an even share.

`to_pyspark()` and `to_scala()` give the code that reads the table with these
predicates, ready to paste:

```python
>>> print(plan.to_pyspark())
# skewsplit: 4 of 4 partitions plus one for NULL, with an estimated skew score of 1.887
# spark, url, table and properties are yours: the session, the JDBC URL, the table,
# and the connection properties such as the user and password.
predicates = [
    '"customer_id" IS NULL',
    '"customer_id" < 42',
    '"customer_id" = 42',
    '"customer_id" > 42 AND "customer_id" < 520',
    '"customer_id" >= 520',
]
df = spark.read.jdbc(url, table, predicates=predicates, properties=properties)

```

## Python API

These names, the `__all__` of `skewsplit` and `skewsplit.postgres`, are the
API. The planner's input and output are defined in the [spec](spec/SPEC.md).

| Name | What it is |
|------|------------|
| `skewsplit.plan(distribution, partitions)` | Plans from a distribution you already have: the input of spec section 1, without `partitions` |
| `skewsplit.hash_plan(counts, partitions)` | Plans hash predicates from a column's name, type and NULL and non-NULL rows: the input of spec section 16, without `partitions` |
| `skewsplit.Plan` | A plan. `predicates` is the list of SQL conditions that `spark.read.jdbc` takes, `report` the skew report as text, and `to_dict()` and `to_json()` the output of spec section 10. `to_pyspark()` and `to_scala()` give code that reads a table with the predicates. `parts` has each predicate with its estimated rows, and `skew_score` and `warnings` are as in the spec |
| `skewsplit.Predicate`, `skewsplit.Bound`, `skewsplit.PlanWarning` | The parts of a plan |
| `skewsplit.SpecError` | Invalid input, with the `code` of spec section 9 |
| `skewsplit.postgres.plan(conn_or_dsn, table, column, partitions)` | Plans from the column's statistics in `pg_stats`, without scanning the table. With `strategy="sample"`, from a sample of the table instead; `sample_rows`, `max_sample_rows` and `seed` set it up. With `strategy="exact"`, from every row. With `method="hash"`, hash predicates for a column of any type instead of ranges. `statement_timeout_ms` bounds each query of any strategy |
| `skewsplit.postgres.distribution(conn_or_dsn, table, column)` | The distribution that `plan` reads, as `skewsplit.plan` takes it. It takes the same options |
| `skewsplit.postgres.verify(conn_or_dsn, table, column, predicates)` | Scans the table once and returns a `Verification`: `ok` is true if every row matches exactly one predicate |
| `skewsplit.postgres.verify_plan(conn_or_dsn, table, plan)` | Verifies a plan, and reports each predicate's actual rows next to its estimate |
| `skewsplit.postgres.Verification` | What `verify` found |
| `skewsplit.postgres.CatalogError`, `skewsplit.postgres.CatalogErrorCode` | The table or column cannot be planned, with a `code` such as `statistics_missing` |

`skewsplit.postgres` needs the `postgres` extra, which installs psycopg.
`conn_or_dsn` is a libpq connection string or an open psycopg connection,
which is left open and as it was.

## Command line

```text
skewsplit plan   --table T --column C --partitions N [--method range|hash]
                 [--format text|json|pyspark|scala]
skewsplit verify --table T (--plan FILE | --column C --partitions N [--method range|hash])
                 [--counts] [--statement-timeout-ms MS] [--format text|json]
skewsplit stats  --table T --column C [--partitions N]

planning options of plan, stats, and verify without --plan:
  [--strategy statistics|sample|exact] [--sample-rows N] [--max-sample-rows N] [--seed N]
  [--statement-timeout-ms MS]
```

- `plan` prints the predicates and the skew report, the plan as JSON, or
  PySpark or Scala code that reads the table with the predicates.
- `verify` scans the table once to check that every row matches exactly one
  predicate: those of a plan that `plan --format json` wrote (`--plan -`
  reads it from standard input), or of a new plan. `--counts` also counts the
  rows of each predicate, one query each, for the actual skew.
- `stats` prints the column's distribution as JSON, which `skewsplit.plan`
  takes. With `--partitions`, it is the complete planner input of the spec.

By default, planning reads `pg_stats`. When the statistics are missing or out
of date, `--strategy sample` reads a sample of the table instead, as spec
section 14 describes:

- the planner's row estimate sets the share to sample, for about
  `--sample-rows` rows (30000 by default, what `ANALYZE` reads);
- a table that small is read whole; up to ten times larger, `TABLESAMPLE
  BERNOULLI` picks rows one by one but reads every page; larger still,
  `TABLESAMPLE SYSTEM` reads only that share of the pages;
- `--max-sample-rows` (ten times `--sample-rows` by default) caps the rows
  the sample reads, and the database counts them by value, so only one row per
  distinct value reaches skewsplit;
- `--seed` makes the sample the same on every run while the table does not
  change.

The report says how many rows the sample read and how long planning took.

`--strategy exact` reads every row instead, as spec section 15 describes, for
when accuracy matters more than planning time:

- values with at least a ten-thousandth of the rows get their exact counts,
  from `GROUP BY ... HAVING`;
- `percentile_disc` reads the bounds of a histogram of up to 10000 buckets
  over the other rows, so a range reads within two buckets, a five-thousandth
  of those rows, of its estimate, and a table of fewer than 10000 rows is
  planned exactly;
- **it scans the whole table three times and sorts the column**, so on a large
  table it takes far longer than the other strategies. Set
  `--statement-timeout-ms` to bound each query.

For a column with the statistics of the quick start, `plan` prints:

```text
$ skewsplit plan --table public.orders --column customer_id --partitions 4
customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887
rows  predicate
 100  "customer_id" IS NULL
  76  "customer_id" < 42
 500  "customer_id" = 42
 192  "customer_id" > 42 AND "customer_id" < 520
 192  "customer_id" >= 520
warning dominant_value: 42 holds 500 rows, at least one partition's share; it cannot be split further on this column
```

Each command connects with `--dsn`, a libpq connection string or URI, or
without it with the `PG*` environment variables. Keep a password in
`PGPASSWORD` or `~/.pgpass` rather than on the command line, where other users
can see it. Error messages hide the password of `--dsn` and of `PGPASSWORD`,
and never quote a `--dsn` that is not valid.

| Exit code | Meaning |
|-----------|---------|
| 0 | Success |
| 1 | `verify` found rows that match no predicate, or more than one |
| 2 | The command line is wrong, or `skewsplit[postgres]` is not installed |
| 3 | The column cannot be planned (for example, it has no statistics), or the plan file is not valid |
| 4 | The database could not be reached, or a query failed or timed out |

## When to use hash-mod instead

The usual alternative splits on a hash of the column, one predicate per
remainder. `--method hash` (`method="hash"` in Python) plans it, as spec
section 16 describes:

```text
"session_id" IS NULL
((pg_catalog.hashtextextended("session_id"::text, 0) % 16) + 16) % 16 = 0
...
((pg_catalog.hashtextextended("session_id"::text, 0) % 16) + 16) % 16 = 15
```

It works on a column of any type and needs no statistics: planning counts the
rows with one query. But **no index can serve a hash predicate, so every
partition reads the whole table**, and the plan always warns `full_scans`.
Prefer it when:

- the column's type is not one ranges support (see below), such as `text`,
  `uuid` or `boolean`;
- the table has no statistics you can trust, you cannot run `ANALYZE`, and a
  sample (`--strategy sample`) would cost too much;
- the table is small enough that reading it once per partition costs nothing.

The estimates assume every value has the same number of rows. Neither method
can split a value that holds more rows than a partition should: every row of
that value hashes to the same partition, too. Split on another column, such
as the primary key, instead. `verify --counts` shows the real sizes.

The hash is of the value's text, so every partition's query must produce the
same text for the same value. For most types it always does, but the text of
`timestamptz` depends on the session's `TimeZone`, which the PostgreSQL JDBC
driver takes from each executor's JVM. For such a column, fix the time zone
in the JDBC URL, for example `?options=-c%20TimeZone=UTC`. Spec section 16
lists the settings that matter.

## Limitations

- **Statistics.** A plan from `pg_stats` is as balanced as the statistics are
  accurate:
  - a range can read up to two histogram buckets more or fewer rows than its
    estimate;
  - on a large table, `ANALYZE` reads a sample, and the sampling error comes on
    top. A higher statistics target (`ALTER TABLE ... ALTER COLUMN ... SET
    STATISTICS`, then `ANALYZE`) gives a finer histogram;
  - when more than a tenth of the rows changed since the last `ANALYZE`, the
    plan warns `stale_statistics`. Run `ANALYZE` and plan again, or plan from
    a sample.

  A plan from a sample has its sampling error. `TABLESAMPLE SYSTEM` takes
  whole pages, so on a column whose values follow the order of the rows on
  disk, such as an increasing key, a larger `--sample-rows` helps more than it
  seems to. The predicates still match every row exactly once, and
  `skewsplit verify` checks that on the real table, with `--counts` the actual
  skew too. `--strategy exact` has no sampling error, but its estimates are
  only as accurate as at the time it read the table.
- **Dominant values.** A value with at least one partition's share of the rows
  gets its own `col = value` predicate and the warning `dominant_value`. No
  plan on this column can make that partition smaller. With too few
  partitions to give it a predicate of its own, the warning is
  `dominant_value_not_isolated`.
- **NULL rows** always get their own predicate. When they hold more than one
  partition's share, the plan warns `nulls_exceed_share`.
- **Types.** Ranges split `smallint`, `integer`, `bigint`, `numeric` with a
  declared scale, `date`, `timestamp` and `timestamptz`. Any other type, and
  `numeric` without a scale, is the error `unsupported_type`, unless you plan
  hash predicates with `--method hash`, which work on any type.
- **Tables.** Tables, partitioned tables, materialized views and foreign
  tables, in PostgreSQL only. The role needs `SELECT` on the column, and no
  row-level security may apply to it: otherwise `pg_stats` hides the
  statistics, and planning fails with `permission_denied`. The sample and
  exact strategies check the same. PostgreSQL cannot apply `TABLESAMPLE` to a
  foreign table, so the sample strategy fails on one larger than the sample
  size, with PostgreSQL's error.

## Supported versions

<!-- supported-versions:start -->
| Component | Versions | Notes |
|-----------|----------|-------|
| Python | 3.9 to 3.15 | 3.9 and 3.10 stay supported for Amazon EMR and AWS Glue users |
| Spark | 3.5, 4.1, 4.2, 4.3 | 4.0 reaches end of life on 23 November 2026 and is not tested |
| Python for Spark | 3.9 to 3.11 for Spark 3.5; 3.10 to 3.14 for 4.1 and 4.2; 3.11 to 3.14 for 4.3 | The Pythons each PySpark line supports; no PySpark line supports 3.15 yet |
| Java (for Spark) | 17 for Spark 3.5; 17 and 21 for Spark 4.x | Spark 3.5 does not support Java 21 |
| PostgreSQL | 15 to 19 | 14 reached end of life on 12 November 2026; 19 was released on 19 November 2026 |
| Operating systems | Linux, macOS, Windows | Database and Spark tests run on Linux |

Support for a version is dropped only in a minor release, announced in the
changelog one release ahead.
<!-- supported-versions:end -->

## Benchmark

Before the adapter was built, a spike measured whether ranges of equal row
counts beat the alternatives on an indexed, skewed column. It read a table of
15 million rows into Spark 4.3 with 16 partitions, from PostgreSQL 18:

| Column | Largest task, default bounds | Largest task, quantile ranges | Ranges against hash-mod: slowest task | Ranges against hash-mod: whole job |
|--------|-----------------------------:|------------------------------:|--------------------------------------:|-----------------------------------:|
| `customer_id` (Zipf-like) | 83% of the rows | 8% | 32% faster | 20% faster |
| `created_at` (recent-heavy) | 62% of the rows | 6% | 37% faster | 43% faster |

The spike computed exact quantiles with a query that scans the column, which
took 2.7 and 4.7 seconds; skewsplit plans the same kind of ranges from
`pg_stats` without a scan. The method and every number are in
[bench/README.md](bench/README.md) and [bench/RESULTS.md](bench/RESULTS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE).
