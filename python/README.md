# skewsplit

skewsplit reads a database column's distribution and returns balanced,
index-friendly range predicates for the `predicates` argument of Spark's JDBC
reader, together with a report of the remaining skew.

For PostgreSQL, it plans from the column's statistics in `pg_stats`, without
scanning the table. `NULL` rows get their own predicate, a value too frequent
to split gets its own `col = value` predicate, and the predicates match every
row exactly once whatever the data.

> **Status: early development (pre-alpha).** The API can change in any 0.x
> release.

## Install

```sh
pip install "skewsplit[postgres]"
```

The `postgres` extra installs psycopg, which also needs libpq: add
`psycopg[binary]` if your system has none.

## Use

```python
from skewsplit import postgres

plan = postgres.plan("host=db dbname=shop user=reader", "public.orders", "customer_id", 16)
print(plan.report)
df = spark.read.jdbc(url, "public.orders", predicates=plan.predicates, properties=properties)
```

That reads the column's statistics in `pg_stats`. When they are missing or
out of date, plan from a sample of the table instead:

```python
plan = postgres.plan(dsn, "public.orders", "customer_id", 16, strategy="sample", seed=7)
```

The planner itself needs no database:

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
>>> for predicate in plan.predicates:
...     print(predicate)
"customer_id" IS NULL
"customer_id" < 42
"customer_id" = 42
"customer_id" > 42 AND "customer_id" < 520
"customer_id" >= 520

```

From the command line, `skewsplit plan --table public.orders --column
customer_id --partitions 16` prints the same report, `--strategy sample`
plans from a sample, and `--format pyspark` prints the code that reads the
table.

**The output contains real values from the column**: treat plans, reports and
logs like the data they describe.

The [repository](https://github.com/Diafyx/skewsplit#readme) has the full
documentation: the command line, when to use hash-mod instead, limitations and
supported versions.
