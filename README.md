# skewsplit

[![CI](https://github.com/Diafyx/skewsplit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Diafyx/skewsplit/actions/workflows/ci.yml?query=branch%3Amain)
[![Python 3.9 to 3.15](https://img.shields.io/badge/python-3.9%20to%203.15-3776ab)](#supported-versions)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/Diafyx/skewsplit/badge)](https://scorecard.dev/viewer/?uri=github.com/Diafyx/skewsplit)

skewsplit reads a database column's distribution and gives Spark balanced,
index-friendly partition predicates for its JDBC reader, together with a
report of the skew that remains.

> **Status: pre-release.** Nothing is published yet and the API will change.
> The roadmap is in the [milestones](https://github.com/Diafyx/skewsplit/milestones).

## The problem

Spark's JDBC reader splits a table by `lowerBound`, `upperBound` and
`numPartitions` into ranges of equal *width*. Real columns are rarely uniform:
a few ranges get most of the rows, one task runs for an hour while the others
finish in seconds, and `NULL` rows end up in the first partition or are lost
to a hand-written filter. Spark's other option, a `predicates` list with one
`WHERE` clause per partition, avoids this, but someone has to write those
clauses.

## What skewsplit will do

The first release, python v0.1.0, is planned to:

- read the column's statistics (for PostgreSQL, `pg_stats`: most common values
  and an equi-depth histogram), so planning does not scan the table;
- plan ranges of roughly equal *row counts* that the database can answer from
  an index on the column;
- give `NULL` rows their own `col IS NULL` predicate, and give a value that is
  too frequent to split its own `col = value` predicate, with a warning;
- report the estimated skew of the plan, where the estimate came from and how
  fresh the statistics are;
- verify a plan in one scan: every row is matched by exactly one predicate.

The predicates will contain real values from the column, so treat the output
like the data it describes.

## Python API

These names, the `__all__` of `skewsplit` and `skewsplit.postgres`, are the
API. The planner's input and output are defined in the [spec](spec/SPEC.md).

| Name | What it is |
|------|------------|
| `skewsplit.plan(distribution, partitions)` | Plans from a distribution you already have: the input of spec section 1, without `partitions` |
| `skewsplit.Plan` | A plan. `predicates` is the list of SQL conditions that `spark.read.jdbc` takes, `report` the skew report as text, and `to_dict()` and `to_json()` the output of spec section 10. `to_pyspark()` and `to_scala()` give code that reads a table with the predicates. `parts` has each predicate with its estimated rows, and `skew_score` and `warnings` are as in the spec |
| `skewsplit.Predicate`, `skewsplit.Bound`, `skewsplit.PlanWarning` | The parts of a plan |
| `skewsplit.SpecError` | Invalid input, with the `code` of spec section 9 |
| `skewsplit.postgres.plan(conn_or_dsn, table, column, partitions)` | Plans from the column's statistics in `pg_stats`, without scanning the table |
| `skewsplit.postgres.distribution(conn_or_dsn, table, column)` | The distribution that `plan` reads from `pg_stats`, as `skewsplit.plan` takes it |
| `skewsplit.postgres.verify(conn_or_dsn, table, column, predicates)` | Scans the table once and returns a `Verification`: `ok` is true if every row matches exactly one predicate |
| `skewsplit.postgres.verify_plan(conn_or_dsn, table, plan)` | Verifies a plan, and reports each predicate's actual rows next to its estimate |
| `skewsplit.postgres.Verification` | What `verify` found |
| `skewsplit.postgres.CatalogError`, `skewsplit.postgres.CatalogErrorCode` | The table or column cannot be planned, with a `code` such as `statistics_missing` |

`skewsplit.postgres` needs the `postgres` extra, which installs psycopg.
`conn_or_dsn` is a libpq connection string or an open psycopg connection,
which is left open and as it was.

## Command line

```text
skewsplit plan   --table T --column C --partitions N [--format text|json|pyspark|scala]
skewsplit verify --table T (--plan FILE | --column C --partitions N)
                 [--counts] [--statement-timeout-ms MS] [--format text|json]
skewsplit stats  --table T --column C [--partitions N]
```

- `plan` prints the predicates and the skew report, the plan as JSON, or
  PySpark or Scala code that reads the table with the predicates.
- `verify` scans the table once to check that every row matches exactly one
  predicate: those of a plan that `plan --format json` wrote (`--plan -`
  reads it from standard input), or of a new plan. `--counts` also counts the
  rows of each predicate, one query each, for the actual skew.
- `stats` prints the column's distribution as JSON, which `skewsplit.plan`
  takes. With `--partitions`, it is the complete planner input of the spec.

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

## Supported versions

<!-- supported-versions:start -->
| Component | Versions | Notes |
|-----------|----------|-------|
| Python | 3.9 to 3.15 | 3.9 and 3.10 stay supported for Amazon EMR and AWS Glue users |
| Spark | 3.5, 4.1, 4.2, 4.3 | 4.0 reaches end of life on 23 November 2026 and is not tested |
| Java (for Spark) | 17 for Spark 3.5; 17 and 21 for Spark 4.x | Spark 3.5 does not support Java 21 |
| PostgreSQL | 15 to 19 | 14 reached end of life on 12 November 2026; 19 was released on 19 November 2026 |
| Operating systems | Linux, macOS, Windows | Database and Spark tests run on Linux |

Support for a version is dropped only in a minor release, announced in the
changelog one release ahead.
<!-- supported-versions:end -->

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE).
