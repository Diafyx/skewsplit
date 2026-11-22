"""Check predicates against the real table: every row must match exactly one.

This is spec section 6 checked on the data. The check is one scan of the
table that evaluates every predicate on every row. Counting the rows of each
predicate, for the actual skew, adds one query per predicate, which can use an
index on the column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.postgres.catalog import find_column
from skewsplit.postgres.connection import check_timeout, connection, set_timeout

if TYPE_CHECKING:
    from collections.abc import Sequence

    import psycopg

    from skewsplit.core.plan import Plan

# Read-only, so the predicates cannot change anything, even with side effects.
_READ_ONLY: Final = "SELECT pg_catalog.set_config('transaction_read_only', 'on', true)"

# array_positions finds the predicates that are true: NULL counts as false,
# as in a WHERE clause.
_SCAN: Final = """
SELECT count(*), count(*) FILTER (WHERE matches = 0), count(*) FILTER (WHERE matches > 1)
FROM (
  SELECT pg_catalog.cardinality(pg_catalog.array_positions(ARRAY[{predicates}]::boolean[], true))
         AS matches
  FROM {table}
) AS t
"""

_COUNT: Final = "SELECT count(*) FROM {table} WHERE {predicate}"


@dataclass(frozen=True)
class Verification:
    """What a scan of the table found.

    Attributes:
        table: The schema-qualified table name, quoted where SQL needs it.
        total_rows: All rows of the table.
        unmatched_rows: Rows that no predicate matches.
        overlapping_rows: Rows that more than one predicate matches.
        predicate_rows: The rows each predicate matches, in the order given,
            or ``None`` if they were not counted.
    """

    table: str
    total_rows: int
    unmatched_rows: int
    overlapping_rows: int
    predicate_rows: tuple[int, ...] | None = None

    @property
    def ok(self) -> bool:
        """Whether every row matches exactly one predicate."""
        return self.unmatched_rows == 0 and self.overlapping_rows == 0

    def skew_milli(self, partitions: int) -> int | None:
        """The skew score of spec section 7, times 1000, from the counted rows.

        ``None`` if the rows were not counted or the table has none.
        """
        if self.predicate_rows is None or self.total_rows == 0:
            return None
        largest = max(self.predicate_rows, default=0)
        return (2 * largest * partitions * 1000 + self.total_rows) // (2 * self.total_rows)


def verify(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    predicates: Sequence[str],
    *,
    counts: bool = False,
    statement_timeout_ms: int | None = None,
) -> Verification:
    """Check that every row of ``table`` matches exactly one of ``predicates``.

    Args:
        database: A libpq connection string, or an open connection, which is
            left open and with its settings and transaction as they were.
        table: The table as SQL names it, as for :func:`find_column`.
        column: The column the predicates split, not quoted.
        predicates: SQL conditions, such as a plan's ``sql`` fields. They run
            in a read-only transaction that is rolled back afterwards.
        counts: Also count the rows of each predicate, one query each.
        statement_timeout_ms: A ``statement_timeout`` for each query, in
            milliseconds; by default the session's.

    Raises:
        CatalogError: If the table or column cannot be read.
        ValueError: If ``statement_timeout_ms`` is not positive.
        psycopg.Error: If a predicate is not valid SQL, or a query was
            cancelled by the timeout (``psycopg.errors.QueryCanceled``).
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        qualified = find_column(conn, table, column).qualified
        # find_column quoted the name with quote_ident.
        target = sql.SQL(qualified)
        # Each predicate on lines of its own, so a trailing -- comment ends with it.
        conditions = [sql.SQL("(\n" + text + "\n)") for text in predicates]
        with conn.transaction(force_rollback=True), conn.cursor(row_factory=tuple_row) as cursor:
            cursor.execute(_READ_ONLY)
            set_timeout(cursor, statement_timeout_ms)
            scan = sql.SQL(_SCAN).format(predicates=sql.SQL(", ").join(conditions), table=target)
            total, unmatched, overlapping = _one(cursor, scan)
            rows = None
            if counts:
                count = sql.SQL(_COUNT)
                rows = tuple(
                    _one(cursor, count.format(table=target, predicate=condition))[0]
                    for condition in conditions
                )
    return Verification(qualified, total, unmatched, overlapping, rows)


def verify_plan(
    database: str | psycopg.Connection[Any],
    table: str,
    plan: Plan,
    *,
    counts: bool = False,
    statement_timeout_ms: int | None = None,
) -> dict[str, object]:
    """Verify a plan, and report the actual rows next to the estimates.

    The result's ``ok`` is false if some row matches no predicate or more
    than one; the CLI exits with an error then.
    """
    found = verify(
        database,
        table,
        plan.column,
        plan.predicates,
        counts=counts,
        statement_timeout_ms=statement_timeout_ms,
    )
    estimates = [(part.sql, part.estimated_rows) for part in plan.parts]
    return verification_report(found, estimates, plan.requested_partitions, plan.skew_score)


def verification_report(
    found: Verification,
    estimates: Sequence[tuple[str, int]],
    partitions: int,
    estimated_skew: float | None,
) -> dict[str, object]:
    """Report what :func:`verify` found next to a plan's estimates.

    Args:
        found: The result of verifying the plan's predicates, in order.
        estimates: The SQL and the estimated rows of each predicate.
        partitions: The plan's requested partitions, ``N`` of spec section 7.
        estimated_skew: The plan's skew score.
    """
    actual = found.predicate_rows
    skew = found.skew_milli(partitions)
    return {
        "ok": found.ok,
        "table": found.table,
        "rows": {
            "total": found.total_rows,
            "unmatched": found.unmatched_rows,
            "overlapping": found.overlapping_rows,
        },
        "predicates": [
            {
                "sql": predicate,
                "estimated_rows": rows,
                "actual_rows": None if actual is None else actual[i],
            }
            for i, (predicate, rows) in enumerate(estimates)
        ],
        "skew_score": {
            "estimated": estimated_skew,
            "actual": None if skew is None else skew / 1000,
        },
    }


def _one(cursor: psycopg.Cursor[Any], query: sql.Composed) -> tuple[int, ...]:
    # Binary results make psycopg use the extended query protocol, which runs
    # exactly one statement, so no predicate can end the query and start another.
    # An aggregate without GROUP BY always returns one row.
    return cast("tuple[int, ...]", cursor.execute(query, binary=True).fetchone())
