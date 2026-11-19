"""The ways the benchmark splits a JDBC read, as SQL text for Spark.

Standard library only, so the unit tests run without Spark.
"""

from __future__ import annotations

TABLE = "bench_orders"
# The split columns and their PostgreSQL types.
COLUMNS = {"customer_id": "bigint", "created_at": "timestamp"}
METHODS = ("default bounds", "hash-mod", "quantile ranges")


def quote(name: str) -> str:
    """Quote a PostgreSQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def literal(text: str, pg_type: str) -> str:
    """A constant of the column's type, so the comparison can use its index."""
    return "'" + text.replace("'", "''") + "'::" + pg_type


def bounds_query(column: str) -> str:
    """The lowest and highest value, as a user of ``partitionColumn`` would read them."""
    col, table = quote(column), quote(TABLE)
    return f"SELECT min({col})::text AS lo, max({col})::text AS hi FROM {table}"  # noqa: S608 -- fixed, quoted identifiers


def quantile_query(column: str, partitions: int) -> str:
    """The ``partitions - 1`` inner quantiles of the column, as text, in order."""
    fractions = ", ".join(repr(i / partitions) for i in range(1, partitions))
    col = quote(column)
    return (
        f"SELECT (percentile_disc(ARRAY[{fractions}]::float8[]) WITHIN GROUP"  # noqa: S608 -- fixed, quoted identifiers
        f" (ORDER BY {col}))::text[] AS bounds FROM {quote(TABLE)}"
    )


def hash_mod(column: str, partitions: int) -> list[str]:
    """A NULL predicate, then one predicate per hash bucket. Each scans the whole table."""
    col = quote(column)
    bucket = f"((hashtextextended({col}::text, 0) % {partitions}) + {partitions}) % {partitions}"
    return [f"{col} IS NULL", *(f"{bucket} = {i}" for i in range(partitions))]


def quantile_ranges(column: str, pg_type: str, bounds: list[str]) -> list[str]:
    """A NULL predicate, then half-open ranges between the distinct quantiles.

    Equal neighbouring quantiles (a value that fills more than one partition)
    collapse into one boundary, so there can be fewer ranges than partitions.
    """
    col = quote(column)
    cuts = [literal(b, pg_type) for i, b in enumerate(bounds) if i == 0 or b != bounds[i - 1]]
    if not cuts:
        return [f"{col} IS NULL", f"{col} IS NOT NULL"]
    inner = [f"{col} >= {lo} AND {col} < {hi}" for lo, hi in zip(cuts, cuts[1:])]
    return [f"{col} IS NULL", f"{col} < {cuts[0]}", *inner, f"{col} >= {cuts[-1]}"]
