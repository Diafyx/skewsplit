"""Plan from a sample of the column, read with ``TABLESAMPLE`` (spec section 14).

This reads part of the table, so it works when ``pg_stats`` has no
statistics or they are out of date. The database counts the sampled rows of
each value, so only one row per distinct value reaches the client, and a
``LIMIT`` caps the rows the sample reads.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.core.planner import plan
from skewsplit.core.sample import from_sample
from skewsplit.postgres.catalog import find_column
from skewsplit.postgres.connection import check_timeout, connection, set_timeout

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan
    from skewsplit.postgres.catalog import Column

Method = Literal["all", "bernoulli", "system"]

# The rows ANALYZE reads at the default statistics target of 100.
DEFAULT_SAMPLE_ROWS: Final = 30000
# The default cap is this many times the target sample size.
DEFAULT_LIMIT_FACTOR: Final = 10
# From this percentage up, BERNOULLI reads at most 10 times the pages SYSTEM
# would, and picks each row on its own instead of whole pages.
_BERNOULLI_PERCENT: Final = 10

# For this transaction only: the text forms of spec section 2, and no writes.
_SETTINGS: Final = """
SELECT pg_catalog.set_config('DateStyle', 'ISO', true),
       pg_catalog.set_config('TimeZone', 'UTC', true),
       pg_catalog.set_config('transaction_read_only', 'on', true)
"""
# The planner's estimate of the rows of the table and of its partitions and
# inheritance children. Without statistics, it comes from the table's size.
_ESTIMATE: Final = "EXPLAIN (FORMAT JSON) SELECT FROM {table}"
# The LIMIT applies to the sampled rows, before they are counted.
_SAMPLE: Final = """
SELECT s.v::text, count(*)
FROM (SELECT {column} AS v FROM {table}{sampling} LIMIT %(limit)s) AS s
GROUP BY s.v
"""


@dataclass(frozen=True)
class Sample:
    """What a sample of a column found, and how it was taken.

    Attributes:
        column: The column sampled.
        method: ``all`` if every row was read, otherwise the ``TABLESAMPLE``
            method: ``bernoulli`` or ``system``.
        percent: The ``TABLESAMPLE`` percentage, or ``None`` for ``all``.
        seed: The ``REPEATABLE`` seed, or ``None``.
        planner_rows: The planner's estimate of the table's rows, ``E`` of
            spec section 14.
        limit: The most rows the sample may read, ``C``.
        null_rows: The sampled rows where the column is NULL.
        values: Each distinct non-NULL value as text, with its sampled rows.
    """

    column: Column
    method: Method
    percent: float | None
    seed: int | None
    planner_rows: int
    limit: int
    null_rows: int
    values: tuple[tuple[str, int], ...]

    @property
    def rows(self) -> int:
        """The rows the sample read, ``n``."""
        return self.null_rows + sum(rows for _, rows in self.values)

    @property
    def reached_limit(self) -> bool:
        """Whether the sample stopped at the limit, so it may hold the rows read first only."""
        return self.rows >= self.limit

    @property
    def rows_estimate(self) -> int:
        """The estimated rows of the table, ``T``."""
        if self.reached_limit:
            return max(self.planner_rows, self.rows)
        if self.percent is None:
            return self.rows
        return math.floor(self.rows * 100 / self.percent + 0.5)

    def conversion_input(self, partitions: int) -> dict[str, object]:
        """Return the conversion input of spec section 14."""
        data: dict[str, object] = {"column": self.column.name, "type": self.column.type_name}
        if self.column.scale is not None:
            data["scale"] = self.column.scale
        data.update(
            partitions=partitions,
            rows_estimate=self.rows_estimate,
            sample_null_rows=self.null_rows,
            sample_values=[{"value": value, "rows": rows} for value, rows in self.values],
        )
        return data

    def report(self) -> dict[str, object]:
        """Return the fields this adapter adds to the plan's ``source``."""
        return {
            "table": self.column.qualified,
            "method": self.method,
            "percent": self.percent,
            "seed": self.seed,
            "planner_rows": self.planner_rows,
            "sample_limit": self.limit,
        }

    def warnings(self) -> list[dict[str, object]]:
        """Return the warnings this adapter adds after those of the conversion."""
        if not self.reached_limit:
            return []
        return [
            {
                "code": "sample_limit_reached",
                "rows": self.limit,
                "message": (
                    f"the sample stopped at its limit of {self.limit} rows, so it may hold"
                    " only the rows read first and the row estimate is the planner's;"
                    " raise the limit or lower the sample size"
                ),
            }
        ]


def sampling_method(planner_rows: int, sample_rows: int) -> tuple[Method, float | None]:
    """Choose how to sample a table of ``planner_rows`` rows for about ``sample_rows`` rows.

    Returns:
        The method and the ``TABLESAMPLE`` percentage, ``None`` when every
        row is read.
    """
    if planner_rows <= sample_rows:
        return "all", None
    percent = 100 * sample_rows / planner_rows
    return ("bernoulli" if percent >= _BERNOULLI_PERCENT else "system"), percent


def read_sample(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    conn: psycopg.Connection[Any],
    column: Column,
    *,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
) -> Sample:
    """Read a sample of a column, counted by value in the database.

    The reading transaction is read-only and rolled back, so the caller's
    settings and transaction are left as they were.

    Args:
        conn: An open connection.
        column: The column, as :func:`find_column` found it.
        sample_rows: About how many rows to sample.
        max_sample_rows: The most rows the sample may read; by default 10
            times ``sample_rows``.
        seed: A ``REPEATABLE`` seed, so the same table gives the same sample.
        statement_timeout_ms: A ``statement_timeout`` for each query, in
            milliseconds; by default the session's.

    Raises:
        ValueError: If a size or the timeout is not positive, or the limit is
            below ``sample_rows``.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    limit = _limit(sample_rows, max_sample_rows)
    check_timeout(statement_timeout_ms)
    # find_column quoted the name with quote_ident.
    table = sql.SQL(column.qualified)
    with conn.transaction(force_rollback=True), conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_SETTINGS)
        set_timeout(cursor, statement_timeout_ms)
        explained = cursor.execute(sql.SQL(_ESTIMATE).format(table=table)).fetchone()
        planner_rows = _plan_rows(cast("tuple[object]", explained)[0])
        method, percent = sampling_method(planner_rows, sample_rows)
        params: dict[str, object] = {"limit": limit}
        sampling: sql.Composable = sql.SQL("")
        if percent is not None:
            sampling = sql.SQL(" TABLESAMPLE {method} (%(percent)s)").format(
                method=sql.SQL(method.upper())
            )
            params["percent"] = percent
            if seed is not None:
                sampling = sql.Composed([sampling, sql.SQL(" REPEATABLE (%(seed)s)")])
                params["seed"] = seed
        query = sql.SQL(_SAMPLE).format(
            column=sql.Identifier(column.name), table=table, sampling=sampling
        )
        counted = cast("list[tuple[str | None, int]]", cursor.execute(query, params).fetchall())
    return Sample(
        column=column,
        method=method,
        percent=percent,
        # Every row is read without TABLESAMPLE, so there is nothing to repeat.
        seed=None if percent is None else seed,
        planner_rows=planner_rows,
        limit=limit,
        null_rows=sum(rows for value, rows in counted if value is None),
        values=tuple((value, rows) for value, rows in counted if value is not None),
    )


def sample_distribution(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    *,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
) -> JSON:
    """Return a column's distribution, from a sample of the table.

    It is the input of spec section 1 without ``partitions``, as
    :func:`skewsplit.postgres.distribution` returns it; the arguments are
    those of :func:`read_sample`.

    Raises:
        CatalogError: If the column cannot be split.
        SpecError: If a sampled value does not convert into a valid input.
        ValueError: If an option is out of range.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    with connection(database) as conn:
        found = find_column(conn, table, column)
        sample = read_sample(
            conn,
            found,
            sample_rows=sample_rows,
            max_sample_rows=max_sample_rows,
            seed=seed,
            statement_timeout_ms=statement_timeout_ms,
        )
    # The conversion does not depend on the partitions; any valid number will do.
    data = from_sample(sample.conversion_input(1))
    del data["partitions"]
    source = cast("JSON", data["source"])
    warnings = cast("list[object]", source["warnings"]) + sample.warnings()
    data["source"] = {**source, **sample.report(), "warnings": warnings}
    return data


def plan_from_sample(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    partitions: int,
    *,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
) -> Plan:
    """Plan ``partitions`` predicates for a column from a sample of the table.

    The arguments are those of :func:`sample_distribution`, and the plan's
    ``source`` also has ``planning_ms``.
    """
    with connection(database) as conn:
        start = perf_counter()
        data = sample_distribution(
            conn,
            table,
            column,
            sample_rows=sample_rows,
            max_sample_rows=max_sample_rows,
            seed=seed,
            statement_timeout_ms=statement_timeout_ms,
        )
    result = plan({**data, "partitions": partitions})
    elapsed_ms = (perf_counter() - start) * 1000
    source = cast("JSON", result.source)
    return replace(result, source={**source, "planning_ms": round(elapsed_ms, 3)})


def _limit(sample_rows: int, max_sample_rows: int | None) -> int:
    if sample_rows <= 0:
        msg = f"sample_rows must be positive, not {sample_rows}"
        raise ValueError(msg)
    limit = DEFAULT_LIMIT_FACTOR * sample_rows if max_sample_rows is None else max_sample_rows
    if limit < sample_rows:
        msg = f"max_sample_rows must be at least sample_rows ({sample_rows}), not {limit}"
        raise ValueError(msg)
    return limit


def _plan_rows(explained: object) -> int:
    """Return the ``Plan Rows`` of ``EXPLAIN (FORMAT JSON)``: at least 1."""
    document = json.loads(explained) if isinstance(explained, str) else explained
    rows = cast("list[dict[str, dict[str, float]]]", document)[0]["Plan"]["Plan Rows"]
    return max(1, math.floor(float(rows) + 0.5))
