"""Plan hash predicates for a column of any type (spec section 16).

The predicates need only the row counts, which one query counts: it reads
the table once, and a read with the plan reads it once per partition.
"""

from __future__ import annotations

from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, cast

from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.core.hashing import hash_plan
from skewsplit.postgres.catalog import find_column
from skewsplit.postgres.connection import (
    check_timeout,
    connection,
    run,
    set_timeout,
    where_clause,
)

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan
    from skewsplit.postgres.catalog import Column

# For this transaction only: no writes.
_READ_ONLY: Final = "SELECT pg_catalog.set_config('transaction_read_only', 'on', true)"
_COUNTS: Final = "SELECT count(*), count({column}) FROM {table}{where}"


def count_rows(
    conn: psycopg.Connection[Any],
    column: Column,
    *,
    statement_timeout_ms: int | None = None,
    where: str | None = None,
) -> tuple[int, int]:
    """Count the rows of the column's table, and those where the column is not NULL.

    The reading transaction is read-only and rolled back, so the caller's
    settings and transaction are left as they were. With ``where``, only the
    rows the caller's filter keeps are counted (spec section 18); it is SQL
    from the caller, so only pass text you trust.

    Raises:
        ValueError: If the timeout is not positive.
        psycopg.Error: If the query fails or was cancelled by the timeout.
    """
    check_timeout(statement_timeout_ms)
    # find_column quoted the table name with quote_ident.
    query = sql.SQL(_COUNTS).format(
        column=sql.Identifier(column.name),
        table=sql.SQL(column.qualified),
        where=where_clause(where),
    )
    with conn.transaction(force_rollback=True), conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_READ_ONLY)
        set_timeout(cursor, statement_timeout_ms)
        counted = run(cursor, query).fetchone()
    return cast("tuple[int, int]", counted)


def plan_from_hash(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    partitions: int,
    *,
    statement_timeout_ms: int | None = None,
    where: str | None = None,
) -> Plan:
    """Plan ``partitions`` hash predicates for a column of any type.

    Args:
        database: A libpq connection string, or an open connection, which is
            left open and with its settings and transaction as they were.
        table: The table as SQL names it, as for :func:`find_column`.
        column: The column's exact name, not quoted.
        partitions: The number of hash predicates besides the NULL predicate.
        statement_timeout_ms: A ``statement_timeout`` for the counting query.
        where: A filter, so only the rows it keeps are counted.

    Raises:
        CatalogError: If the table or column does not exist or cannot be read.
        SpecError: If ``partitions`` is out of range.
        ValueError: If the timeout is not positive.
        psycopg.Error: If the query fails or was cancelled by the timeout.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        start = perf_counter()
        found = find_column(conn, table, column, any_type=True)
        rows, non_null = count_rows(
            conn, found, statement_timeout_ms=statement_timeout_ms, where=where
        )
    source: JSON = {"statistics": "count", "table": found.qualified, "rows": rows}
    if where is not None:
        source["where"] = where
    counts = {
        "column": found.name,
        "type": found.type_name,
        "partitions": partitions,
        "null_rows": rows - non_null,
        "non_null_rows": non_null,
        "source": source,
    }
    result = hash_plan(counts)
    elapsed_ms = (perf_counter() - start) * 1000
    return replace(result, source={**source, "planning_ms": round(elapsed_ms, 3)})
