"""Plan from exact quantiles of the column (spec section 15).

Every row is read, so the plan is as good as the planner can make it, at a
cost: the queries scan the whole table three times and sort the column once.
A ``statement_timeout`` can bound each of them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, cast

from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.core.inputs import parse_input
from skewsplit.core.planner import plan
from skewsplit.postgres.catalog import find_column
from skewsplit.postgres.connection import check_timeout, connection, set_timeout

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan
    from skewsplit.postgres.catalog import Column

# The most histogram buckets: PostgreSQL's highest statistics target.
RESOLUTION: Final = 10000

# For this transaction only: the text forms of spec section 2, and no writes.
_SETTINGS: Final = """
SELECT pg_catalog.set_config('DateStyle', 'ISO', true),
       pg_catalog.set_config('TimeZone', 'UTC', true),
       pg_catalog.set_config('transaction_read_only', 'on', true)
"""
# The values that are neither NULL nor special (spec section 2).
_ORDINARY: Final = {
    "numeric": "s.v IS NOT NULL AND s.v NOT IN ('NaN', 'Infinity', '-Infinity')",
    "date": "s.v IS NOT NULL AND pg_catalog.isfinite(s.v)",
    "timestamp": "s.v IS NOT NULL AND pg_catalog.isfinite(s.v)",
    "timestamptz": "s.v IS NOT NULL AND pg_catalog.isfinite(s.v)",
}
_NOT_NULL: Final = "s.v IS NOT NULL"
_COUNTS: Final = """
SELECT count(*), count(s.v), count(*) FILTER (WHERE {ordinary})
FROM (SELECT {column} AS v FROM {table}) AS s
"""
_COMMON: Final = """
SELECT s.v::text, count(*)
FROM (SELECT {column} AS v FROM {table}) AS s
WHERE {ordinary}
GROUP BY s.v
HAVING count(*) >= %(least)s
ORDER BY s.v
"""
_BOUNDS: Final = """
SELECT count(*),
       (pg_catalog.percentile_disc(%(fractions)s::float8[]) WITHIN GROUP (ORDER BY s.v))::text[]
FROM (SELECT {column} AS v FROM {table}) AS s
WHERE {ordinary} AND s.v <> ALL (%(common)s::text[]::{values})
"""


@dataclass(frozen=True)
class Quantiles:
    """What the exact queries found in a column.

    Attributes:
        column: The column read.
        rows: The rows of the table.
        null_rows: The rows where the column is NULL.
        special_rows: The rows at a special value, such as ``infinity``.
        common: Each value with at least a ten-thousandth of the other rows,
            as text, in order, with its rows.
        histogram_rows: The rows of the values that are not common.
        bounds: The histogram bounds as text, or empty without a histogram.
    """

    column: Column
    rows: int
    null_rows: int
    special_rows: int
    common: tuple[tuple[str, int], ...]
    histogram_rows: int
    bounds: tuple[str, ...]

    def distribution(self) -> JSON:
        """Return the input of spec section 1 without ``partitions``.

        Raises:
            SpecError: If a value is not a valid input.
        """
        data: dict[str, object] = {"column": self.column.name, "type": self.column.type_name}
        if self.column.scale is not None:
            data["scale"] = self.column.scale
        histogram = {"bounds": list(self.bounds), "rows": self.histogram_rows}
        data.update(
            null_rows=self.null_rows,
            mcv=[{"value": value, "rows": rows} for value, rows in self.common],
            histogram=histogram if self.bounds else None,
            source={
                "statistics": "exact",
                "table": self.column.qualified,
                "rows": self.rows,
                "unestimated_rows": self.special_rows,
                "warnings": self.warnings(),
            },
        )
        # The planner checks it again; this catches a bad value for stats too.
        parse_input({**data, "partitions": 1})
        return data

    def warnings(self) -> list[dict[str, object]]:
        """Return ``special_values_left_out`` when some rows have no ordinal."""
        if not self.special_rows:
            return []
        return [
            {
                "code": "special_values_left_out",
                "rows": self.special_rows,
                "message": (
                    f"{self.special_rows} rows at special values such as infinity are left"
                    " out of the estimates; the predicates still read them"
                ),
            }
        ]


def least_common_rows(ordinary_rows: int) -> int:
    """The fewest rows of a common value: a ``RESOLUTION``-th of the ordinary rows, at least 1."""
    return max(1, -(-ordinary_rows // RESOLUTION))


def bound_fractions(rows: int) -> list[float]:
    """The ``percentile_disc`` fractions of the bounds of a histogram over ``rows`` rows.

    Bound ``j`` of ``m`` is the value at position ``j * (rows - 1) // m``,
    counting from 0, where ``ANALYZE`` puts it. ``percentile_disc(f)`` returns
    the value at position ``ceil(f * rows) - 1``, so the fraction asks for
    the middle of the position, which float rounding cannot move to the next.
    """
    buckets = min(RESOLUTION, max(1, rows - 1))
    return [(j * (rows - 1) // buckets + 0.5) / rows for j in range(buckets + 1)]


def read_quantiles(
    conn: psycopg.Connection[Any], column: Column, *, statement_timeout_ms: int | None = None
) -> Quantiles:
    """Read the rows, the common values and the histogram bounds of a column.

    The reading transaction is read-only and rolled back, so the caller's
    settings and transaction are left as they were.

    Args:
        conn: An open connection.
        column: The column, as :func:`find_column` found it.
        statement_timeout_ms: A ``statement_timeout`` for each query, in
            milliseconds; by default the session's.

    Raises:
        ValueError: If the timeout is not positive.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    check_timeout(statement_timeout_ms)
    names = {
        "column": sql.Identifier(column.name),
        # find_column quoted the name with quote_ident.
        "table": sql.SQL(column.qualified),
        "ordinary": sql.SQL(_ORDINARY.get(column.type_name, _NOT_NULL)),
    }
    with conn.transaction(force_rollback=True), conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_SETTINGS)
        set_timeout(cursor, statement_timeout_ms)
        counted = cursor.execute(sql.SQL(_COUNTS).format(**names)).fetchone()
        rows, non_null, ordinary = cast("tuple[int, int, int]", counted)
        least = {"least": least_common_rows(ordinary)}
        found = cursor.execute(sql.SQL(_COMMON).format(**names), least).fetchall()
        common = tuple(cast("list[tuple[str, int]]", found))
        rest = ordinary - sum(count for _, count in common)
        histogram_rows, bounds = 0, None
        if rest > 0:
            # The common values are left out as values of the column's type.
            values = sql.SQL("{}[]").format(sql.Identifier("pg_catalog", column.type_name))
            params = {
                "fractions": bound_fractions(rest),
                "common": [value for value, _ in common],
            }
            query = sql.SQL(_BOUNDS).format(**names, values=values)
            histogram = cursor.execute(query, params).fetchone()
            histogram_rows, bounds = cast("tuple[int, list[str] | None]", histogram)
    return Quantiles(
        column=column,
        rows=rows,
        null_rows=rows - non_null,
        special_rows=non_null - ordinary,
        common=common,
        histogram_rows=histogram_rows,
        # NULL when no row is left: they were deleted after they were counted.
        bounds=tuple(bounds or ()),
    )


def exact_distribution(
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    *,
    statement_timeout_ms: int | None = None,
) -> JSON:
    """Return a column's distribution, from every row of the table.

    It is the input of spec section 1 without ``partitions``, as
    :func:`skewsplit.postgres.distribution` returns it.

    Raises:
        CatalogError: If the column cannot be split.
        SpecError: If a value read does not convert into a valid input.
        ValueError: If the timeout is not positive.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        found = find_column(conn, table, column)
        quantiles = read_quantiles(conn, found, statement_timeout_ms=statement_timeout_ms)
    return quantiles.distribution()


def plan_from_exact(
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    partitions: int,
    *,
    statement_timeout_ms: int | None = None,
) -> Plan:
    """Plan ``partitions`` predicates for a column from every row of the table.

    The arguments are those of :func:`exact_distribution`, and the plan's
    ``source`` also has ``planning_ms``.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        start = perf_counter()
        data = exact_distribution(conn, table, column, statement_timeout_ms=statement_timeout_ms)
    result = plan({**data, "partitions": partitions})
    elapsed_ms = (perf_counter() - start) * 1000
    source = cast("JSON", result.source)
    return replace(result, source={**source, "planning_ms": round(elapsed_ms, 3)})
